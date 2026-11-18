import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, cast

from robot_agent_capabilities.errors import (
    CapabilityConfigurationError,
    CapabilityEventCleanupTimeout,
    CapabilityEventsUnsupported,
    CapabilityEventTransportError,
)
from robot_agent_capabilities.ports import PlatformEventRuntime, TaskEventRuntime, TaskRuntime
from robot_agent_runtime.errors import InvalidRequest
from robot_agent_runtime.models import Event, PlatformEvent, TaskState
from robot_agent_runtime.sdk.remote import RemoteTransportError


CapabilityEvent = Event | PlatformEvent
EventKind = Literal["platform", "task"]
_CLEANUP_TIMEOUT_S = 0.2


def _strings(values: set[str] | None, name: str, *, nonempty: bool = False) -> frozenset[str] | None:
    if values is None:
        return None
    if type(values) not in (set, frozenset) or any(type(value) is not str or not value.strip() for value in values):
        raise InvalidRequest(f"{name} must be a set of nonempty strings")
    if nonempty and not values:
        raise InvalidRequest(f"{name} must not be empty")
    return frozenset(values)


@dataclass(frozen=True)
class _Selection:
    sources: frozenset[str]
    event_types: frozenset[str] | None
    after_cursor: int
    task_ids: frozenset[str] | None
    after_sequences: dict[str, int]
    task_terminal_only: bool


def _selection(sources: set[str] | None, after_cursor: int, event_types: set[str] | None,
               task_ids: set[str] | None, after_sequences: dict[str, int] | None,
               task_terminal_only: bool) -> _Selection:
    chosen = _strings(sources, "sources", nonempty=True)
    if chosen is None:
        chosen = frozenset({"platform"})
    if not chosen <= {"platform", "task"}:
        raise InvalidRequest("sources may contain only platform and task")
    kinds = _strings(event_types, "event_types")
    identities = _strings(task_ids, "task_ids", nonempty=True)
    if type(after_cursor) is not int or after_cursor < 0:
        raise InvalidRequest("after_cursor must be a nonnegative integer")
    if type(task_terminal_only) is not bool:
        raise InvalidRequest("task_terminal_only must be a boolean")
    if "task" not in chosen and (identities is not None or after_sequences is not None or task_terminal_only):
        raise InvalidRequest("Task selection parameters require the task source")
    if "platform" not in chosen and after_cursor != 0:
        raise InvalidRequest("after_cursor applies only to platform events")
    if after_sequences is not None and (
        type(after_sequences) is not dict or identities is None
        or any(type(key) is not str or key not in identities or type(value) is not int or value < 0
               for key, value in after_sequences.items())
    ):
        raise InvalidRequest("after_sequences must map selected task IDs to nonnegative integers")
    positions = {identity: (after_sequences or {}).get(identity, 0) for identity in identities or ()}
    return _Selection(chosen, kinds, after_cursor, identities, positions, task_terminal_only)


@dataclass(frozen=True)
class _Plan:
    kind: EventKind
    open: Callable[[], AsyncIterator[CapabilityEvent]]
    task_id: str | None = None

    def scope(self) -> dict[str, str]:
        return {"source": self.kind, **({"task_id": self.task_id} if self.task_id is not None else {})}


def _plans(runtime: TaskRuntime, selected: _Selection) -> list[_Plan]:
    plans: list[_Plan] = []
    if "platform" in selected.sources:
        subscribe = getattr(cast(PlatformEventRuntime, runtime), "subscribe_platform_events", None)
        if not callable(subscribe):
            raise CapabilityEventsUnsupported("This backend does not support platform events", {"source": "platform"})
        plans.append(_Plan("platform", partial(subscribe, after_cursor=selected.after_cursor,
                           event_types=set(selected.event_types) if selected.event_types is not None else None)))
    if "task" in selected.sources:
        if selected.task_ids is None:
            subscribe = getattr(cast(TaskEventRuntime, runtime), "subscribe_all_task_events", None)
            if not callable(subscribe):
                raise CapabilityEventsUnsupported("This backend does not support all-task live events; select task_ids for replay", {"source": "task"})
            plans.append(_Plan("task", partial(subscribe,
                               event_types=set(selected.event_types) if selected.event_types is not None else None)))
        else:
            subscribe = getattr(runtime, "subscribe_task_events", None)
            if not callable(subscribe):
                raise CapabilityEventsUnsupported("This backend does not support task history events", {"source": "task"})
            plans.extend(_Plan("task", partial(subscribe, identity, max(0, selected.after_sequences[identity] - 1)), identity)
                         for identity in sorted(selected.task_ids))
    return plans


@dataclass(frozen=True)
class _Next:
    event: CapabilityEvent | None = None
    error: BaseException | None = None
    ended: bool = False


@dataclass(eq=False)
class _Stream:
    """Keep one pull per iterator and close it after actual late completion.

    A cancelled source can still produce a value. Closing never starts another
    pull or races aclose against an active anext. Late values are abandoned;
    late cleanup errors go to the owning loop's exception handler.
    """

    plan: _Plan
    iterator: AsyncIterator[CapabilityEvent]
    pending: asyncio.Task[_Next] | None = None
    reading: bool = False
    closing: bool = False
    close_started: bool = False
    cleanup_error: BaseException | None = None
    abandoned: bool = False
    reported: set[tuple[str, int]] = field(default_factory=set)

    def report(self, error: BaseException, phase: str) -> None:
        key = phase, id(error)
        if isinstance(error, asyncio.CancelledError) or key in self.reported:
            return
        self.reported.add(key)
        asyncio.get_running_loop().call_exception_handler({
            "message": f"Late event subscription {phase} failed", "exception": error,
            "phase": phase, **self.plan.scope(),
        })

    def consume(self, task: asyncio.Task[Any]) -> None:
        failure: BaseException | None = None
        if not task.cancelled():
            failure = task.exception()
            if failure is None and isinstance(result := task.result(), _Next):
                failure = result.error
        if self.abandoned:
            if failure is not None:
                self.report(failure, "read")
            if self.cleanup_error is not None:
                self.report(self.cleanup_error, "cleanup")

    def start(self) -> None:
        self.pending = asyncio.create_task(self.read(), name=f"capability-{self.plan.kind}-events")
        self.pending.add_done_callback(self.consume)

    async def read(self) -> _Next:
        self.reading = True
        try:
            try:
                return _Next(event=await anext(self.iterator))
            except StopAsyncIteration:
                return _Next(ended=True)
            except BaseException as error:
                return _Next(error=error)
        finally:
            self.reading = False
            if self.closing:
                await self.close()

    async def close(self) -> None:
        if self.close_started:
            return
        self.close_started = True
        try:
            if (close := getattr(self.iterator, "aclose", None)) is not None:
                await close()
        except BaseException as error:
            self.cleanup_error = error


async def _close_streams(streams: list[_Stream]) -> list[tuple[_Plan | None, BaseException]]:
    waiting: set[asyncio.Task[Any]] = set()
    jobs: dict[_Stream, asyncio.Task[Any]] = {}
    for stream in streams:
        stream.closing = True
        if stream.pending is not None:
            if not stream.pending.done():
                stream.pending.cancel()
            waiting.add(stream.pending)
            jobs[stream] = stream.pending
        if not stream.reading:
            close = asyncio.create_task(stream.close())
            close.add_done_callback(stream.consume)
            waiting.add(close)
            jobs[stream] = close
    try:
        if waiting:
            await asyncio.wait(waiting, timeout=_CLEANUP_TIMEOUT_S)
    finally:
        for stream, job in jobs.items():
            stream.abandoned = not job.done()
    errors: list[tuple[_Plan | None, BaseException]] = []
    for stream in streams:
        if stream.pending is not None and stream.pending.done() and not stream.pending.cancelled():
            result = stream.pending.result()
            if result.error is not None and not isinstance(result.error, asyncio.CancelledError):
                errors.append((stream.plan, result.error))
        if stream.cleanup_error is not None:
            errors.append((stream.plan, stream.cleanup_error))
    unfinished = [stream.plan.scope() for stream in streams if stream.abandoned]
    if unfinished:
        errors.append((None, CapabilityEventCleanupTimeout(
            "Event observation cleanup exceeded its deadline; pending pulls may finish later",
            {"sources": unfinished, "timeout_s": _CLEANUP_TIMEOUT_S},
        )))
    return errors


@dataclass
class _Positions:
    selected: _Selection
    platform: int
    tasks: dict[str, int]
    last_task: dict[str, str | int] | None = None

    def delivered(self, event: CapabilityEvent) -> None:
        if isinstance(event, PlatformEvent):
            self.platform = event.cursor
        elif self.selected.task_ids is not None:
            self.tasks[event.task_id] = event.sequence
        else:
            self.last_task = {"task_id": event.task_id, "sequence": event.sequence}

    def error(self, error: BaseException, plan: _Plan | None) -> BaseException:
        if isinstance(error, NotImplementedError):
            return CapabilityEventsUnsupported("This backend does not implement the requested event source", plan.scope() if plan else None)
        if not isinstance(error, RemoteTransportError):
            return error
        details: dict[str, Any] = {}
        if "platform" in self.selected.sources:
            details.update(requested_after_cursor=self.selected.after_cursor, last_delivered_cursor=self.platform)
        if "task" in self.selected.sources:
            details.update(plan.scope() if plan else {"source": "task"})
            if self.selected.task_ids is not None:
                details.update(requested_after_sequences=dict(self.selected.after_sequences), last_delivered_sequences=dict(self.tasks))
            else:
                details["last_delivered_task"] = self.last_task
        return CapabilityEventTransportError(
            "Event transport failed; resume from the application's saved processed positions", details,
        )


def _matches(event: CapabilityEvent | None, stream: _Stream, selected: _Selection) -> bool:
    expected = PlatformEvent if stream.plan.kind == "platform" else Event
    if not isinstance(event, expected) or (stream.plan.task_id is not None and event.task_id != stream.plan.task_id):
        raise CapabilityConfigurationError("Event backend returned a mismatched event", stream.plan.scope())
    if stream.plan.kind == "platform":
        return True
    if stream.plan.task_id is not None and event.sequence <= selected.after_sequences[stream.plan.task_id]:
        return False
    if selected.event_types and event.event_type not in selected.event_types:
        return False
    if selected.task_terminal_only:
        try:
            return TaskState(event.data.get("state")).is_terminal
        except (TypeError, ValueError):
            return False
    return True


def _cleanup_note(plan: _Plan | None, error: BaseException) -> str:
    details = error.info.details if isinstance(error, CapabilityEventCleanupTimeout) else (plan.scope() if plan else {})
    return f"Event subscription cleanup also failed ({type(error).__name__}): {details}"


async def subscribe_events(
    runtime: TaskRuntime, *, sources: set[str] | None = None, after_cursor: int = 0,
    event_types: set[str] | None = None, task_ids: set[str] | None = None,
    after_sequences: dict[str, int] | None = None, task_terminal_only: bool = False,
) -> AsyncGenerator[CapabilityEvent, None]:
    """Merge independent source orders without a synthetic global checkpoint.

    Each source has at most one pending read; no output queue or task catalogue
    is accumulated. Known task positions are bounded by the explicit selection;
    all-task live mode retains only its last diagnostic task position. The
    application owns visibility, processed positions and any reconnect policy.
    """
    selected = _selection(sources, after_cursor, event_types, task_ids, after_sequences, task_terminal_only)
    positions = _Positions(selected, after_cursor, dict(selected.after_sequences))
    streams: list[_Stream] = []
    current: _Plan | None = None
    primary: BaseException | None = None
    try:
        for plan in _plans(runtime, selected):
            current = plan
            stream = _Stream(plan, plan.open())
            streams.append(stream)
            stream.start()
        while pending := {stream.pending for stream in streams if stream.pending is not None}:
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for stream in streams:
                task = stream.pending
                if task is None or task not in done:
                    continue
                current = stream.plan
                result = task.result()
                stream.pending = None
                if result.error is not None:
                    raise result.error
                if result.ended:
                    continue
                if _matches(result.event, stream, selected):
                    assert result.event is not None
                    positions.delivered(result.event)
                    yield result.event
                stream.start()
    except BaseException as error:
        primary = positions.error(error, current)
        if primary is error:
            raise
        raise primary from error
    finally:
        try:
            errors = await _close_streams(streams)
        except asyncio.CancelledError as error:
            if primary is None or isinstance(primary, GeneratorExit):
                raise
            primary.add_note(f"Event subscription cleanup also failed ({type(error).__name__})")
        else:
            if errors:
                if primary is not None and not isinstance(primary, GeneratorExit):
                    for plan, error in errors:
                        primary.add_note(_cleanup_note(plan, error))
                else:
                    plan, error = errors[0]
                    normalized = positions.error(error, plan)
                    for other_plan, other_error in errors[1:]:
                        normalized.add_note(_cleanup_note(other_plan, other_error))
                    if normalized is error:
                        raise error
                    raise normalized from error
