import asyncio
import inspect
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Future
from contextvars import ContextVar
from typing import Any, Literal

from robot_agent_capabilities.errors import CapabilityEventsUnsupported
from robot_agent_capabilities.events import CapabilityEvent, subscribe_events
from robot_agent_capabilities.ports import ListenerHandle, TaskRuntime
from robot_agent_runtime.errors import InvalidRequest, ListenerCallbackError, ListenerCapacityExceeded, TaskEventStreamEnded
from robot_agent_runtime.models import Event, PlatformEvent, Task, TaskState
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.sdk.callback_executor import CallbackExecutor
from robot_agent_runtime.sdk.listener_execution import (
    CALLBACK_ORIGIN, CallbackLoop, ListenerExecution, ListenerLoops, on_callback_owner,
)
from robot_agent_runtime.sdk.listeners import TaskListener


TaskEventCallback = Callable[[Event], Awaitable[None] | None]
TaskDoneCallback = Callable[[Task], Awaitable[None] | None]
TaskErrorCallback = Callable[[str | None, Exception], Awaitable[None] | None]
PlatformEventCallback = Callable[[PlatformEvent], Awaitable[None] | None]
PlatformErrorCallback = Callable[[Exception], Awaitable[None] | None]
_ORIGIN: ContextVar[tuple[object, ListenerHandle, asyncio.Task[Any] | None] | None] = ContextVar("capability_listener_origin", default=None)
_GRACE_S = 0.2
_MAX_LISTENERS = 128


def _consume(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


def _callbacks(*callbacks: Callable[..., Any] | None) -> None:
    if all(callback is None for callback in callbacks):
        raise InvalidRequest("Provide at least one listener callback")
    if any(callback is not None and not callable(callback) for callback in callbacks):
        raise InvalidRequest("Listener callbacks must be callable")


def _names(values: set[str] | None, name: str, *, nonempty: bool = False) -> frozenset[str] | None:
    if values is None:
        return None
    if type(values) not in (set, frozenset) or any(type(value) is not str or not value.strip() for value in values):
        raise InvalidRequest(f"{name} must be a set of nonempty strings")
    if nonempty and not values:
        raise InvalidRequest(f"{name} must not be empty")
    return frozenset(values)


def _running_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        raise InvalidRequest("Continuous and explicit-task listeners require a running event loop") from None


class _StreamListener:
    """Consume one existing stream without owning transport or robot lifecycle.

    Source failures and callback failures end this observer. close revokes new
    callback claims before cancellation and waits only a bounded grace; native
    callbacks already claimed can outlive the handle. Continuous wait is not a
    promise that robot tasks completed. Global done deduplication is bounded.
    """

    def __init__(self, kind: Literal["task", "platform"], runtime: TaskRuntime,
                 source: Callable[[], AsyncIterator[CapabilityEvent]],
                 detach: Callable[[ListenerHandle], Awaitable[None]], *,
                 on_event: Callable[..., Awaitable[None]] | None,
                 on_done: Callable[..., Awaitable[None]] | None,
                 on_error: Callable[..., Awaitable[None]] | None,
                 event_types: frozenset[str] | None, cancel_grace_s: float = _GRACE_S) -> None:
        self._kind = kind
        self._runtime = runtime
        self._source = source
        self._detach = detach
        self._on_event = on_event
        self._on_done = on_done
        self._on_error = on_error
        self._event_types = event_types
        self._cancel_grace_s = cancel_grace_s
        self._loop = _running_loop()
        self._task: asyncio.Task[None] | None = None
        self._completion: Future[None] = Future()
        self._lock = threading.Lock()
        self._closed = False
        self._last_task_id: str | None = None
        self._done_ids: OrderedDict[str, None] = OrderedDict()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"capability-{self._kind}-listener")
        self._task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._default_error(error)
        if not self._completion.done():
            self._completion.set_result(None)

    async def wait(self) -> None:
        origin = CALLBACK_ORIGIN.get()
        if asyncio.current_task() is self._task or origin is not None and origin[0] is self:
            raise InvalidRequest("A listener callback cannot wait for its own listener")
        wrapped = asyncio.wrap_future(self._completion)
        wrapped.add_done_callback(_consume)
        await asyncio.shield(wrapped)

    async def close(self) -> None:
        if await on_callback_owner(self.close):
            return
        with self._lock:
            self._closed = True
        origin = CALLBACK_ORIGIN.get()
        current = origin[1] if origin is not None and origin[0] is self else asyncio.current_task()
        try:
            await self._detach(self)
        finally:
            if current is not self._task and self._task is not None:
                try:
                    self._loop.call_soon_threadsafe(self._task.cancel)
                except RuntimeError:
                    pass
        if current is self._task:
            return
        wrapped = asyncio.wrap_future(self._completion)
        wrapped.add_done_callback(_consume)
        protected = asyncio.shield(wrapped)
        try:
            await asyncio.wait({protected}, timeout=self._cancel_grace_s)
        finally:
            protected.cancel()

    def _default_error(self, error: BaseException, original: Exception | None = None) -> None:
        asyncio.get_running_loop().call_exception_handler({
            "message": "Capability listener failed", "exception": error,
            "original_error": original, "source": self._kind,
            "task_id": self._last_task_id, "listener": self,
        })

    async def _report(self, error: Exception) -> None:
        if self.closed:
            return
        if self._on_error is None:
            self._default_error(error)
            return
        try:
            if self._kind == "task":
                await self._on_error(self._last_task_id, error)
            else:
                await self._on_error(error)
        except Exception as handler_error:
            self._default_error(handler_error, error)

    async def _deliver(self, event: CapabilityEvent) -> None:
        if isinstance(event, Event):
            self._last_task_id = event.task_id
        if self._on_event is not None and (not self._event_types or event.event_type in self._event_types):
            await self._on_event(event.model_copy(deep=True))
        if self.closed or self._kind != "task" or self._on_done is None:
            return
        try:
            terminal = TaskState(event.data.get("state")).is_terminal
        except (TypeError, ValueError):
            terminal = False
        if not terminal or event.task_id in self._done_ids:
            return
        final = await self._runtime.get_task_status(event.task_id)
        if not final.state.is_terminal:
            raise TaskEventStreamEnded("Terminal event was not followed by a terminal task snapshot", {"task_id": event.task_id})
        if not self.closed:
            await self._on_done(final.model_copy(deep=True))
            self._done_ids[event.task_id] = None
            while len(self._done_ids) > 1000:
                self._done_ids.popitem(last=False)

    async def _run(self) -> None:
        stream: AsyncIterator[CapabilityEvent] | None = None
        try:
            if self.closed:
                return
            stream = self._source()
            async for event in stream:
                if self.closed:
                    return
                await self._deliver(event)
                self._last_task_id = None
                if self.closed:
                    return
        except asyncio.CancelledError:
            if not self.closed and not asyncio.current_task().cancelling():
                await self._report(ListenerCallbackError("Event observation cancelled itself"))
            else:
                raise
        except Exception as error:
            await self._report(error)
        finally:
            try:
                if stream is not None and (close := getattr(stream, "aclose", None)) is not None:
                    await close()
            except Exception as error:
                self._default_error(error)
            finally:
                with self._lock:
                    self._closed = True
                await self._detach(self)


class CapabilityListeners:
    """Share bounded callback resources while keeping observers independent.

    The manager never owns runtime startup/shutdown. Individual close releases
    observation; close() terminates all registrations and the client callback
    pool, with no replacement pool after blocked native callbacks are abandoned.
    Registration bookkeeping is protected without running user code under locks.
    SDK callback-loop owners supply group/dedicated execution; groups and their
    budgets belong to this capability client, independently of its backend SDK.
    """

    def __init__(self, runtime: TaskRuntime, *, options: RuntimeOptions | None = None) -> None:
        self._runtime = runtime
        self._options = options or RuntimeOptions()
        self._executor = CallbackExecutor()
        self._loops = ListenerLoops(self._options)
        self._lock = threading.Lock()
        self._handles: set[ListenerHandle] = set()
        self._automatic: set[TaskListener] = set()
        self._owners: dict[ListenerHandle, CallbackLoop] = {}
        self._reserved: set[object] = set()
        self._closed = False
        self._completion: Future[None] | None = None
        self._cleanup: asyncio.Task[None] | None = None

    def _reserve(self) -> object:
        with self._lock:
            if self._closed:
                raise InvalidRequest("Capability listeners are closed")
            if len(self._handles) + len(self._reserved) >= _MAX_LISTENERS:
                raise ListenerCapacityExceeded("Capability listener registration capacity is exhausted")
            token = object()
            self._reserved.add(token)
            return token

    def _commit(self, token: object, handle: ListenerHandle, *, automatic: bool = False,
                execution: ListenerExecution = "caller", group: str | None = None) -> None:
        with self._lock:
            self._reserved.discard(token)
            if self._closed:
                raise InvalidRequest("Capability listeners are closed")
            owner = self._loops.acquire(execution, group)
            self._handles.add(handle)
            if owner is not None:
                self._owners[handle] = owner
            if automatic:
                self._automatic.add(handle)

    async def _detach(self, handle: ListenerHandle) -> None:
        with self._lock:
            self._handles.discard(handle)
            self._automatic.discard(handle)
            owner = self._owners.pop(handle, None)
        if owner is not None:
            await self._loops.release(owner)

    def _wrapped(self, callback: Callable[..., Any] | None, owner: list[ListenerHandle],
                 event_types: frozenset[str] | None = None) -> Callable[..., Awaitable[None]] | None:
        if callback is None:
            return None

        async def invoke(*args: Any) -> None:
            handle = owner[0]
            if handle.closed or (event_types and args[0].event_type not in event_types):
                return
            token = _ORIGIN.set((self, handle, asyncio.current_task()))
            try:
                with self._lock:
                    callback_owner = self._owners.get(handle)
                if callback_owner is None:
                    await self._executor.invoke(callback, *args, can_start=lambda: not handle.closed)
                else:
                    async def deliver(*values: Any) -> None:
                        callback_token = _ORIGIN.set((self, handle, asyncio.current_task()))
                        try:
                            result = callback(*values)
                            if inspect.isawaitable(result):
                                await result
                        finally:
                            _ORIGIN.reset(callback_token)

                    await callback_owner.invoke(handle, deliver, args, can_start=lambda: not handle.closed)
            finally:
                _ORIGIN.reset(token)

        return invoke

    def add_task(self, *, on_event: TaskEventCallback | None = None,
                 on_done: TaskDoneCallback | None = None, on_error: TaskErrorCallback | None = None,
                 task_ids: set[str] | None = None, all_tasks: bool = False,
                 event_types: set[str] | None = None, execution: ListenerExecution = "caller",
                 group: str | None = None) -> ListenerHandle:
        _callbacks(on_event, on_done, on_error)
        identities = _names(task_ids, "task_ids", nonempty=True)
        selected = _names(event_types, "event_types")
        if type(all_tasks) is not bool or (all_tasks and identities is not None):
            raise InvalidRequest("all_tasks must be a boolean and cannot combine with task_ids")
        if all_tasks:
            if not callable(getattr(self._runtime, "subscribe_all_task_events", None)):
                raise CapabilityEventsUnsupported("This backend does not support all-task live events", {"source": "task"})
            return self._add_stream("task", lambda: subscribe_events(self._runtime, sources={"task"}),
                                    on_event, on_done, on_error, selected, execution, group)
        if not callable(getattr(self._runtime, "subscribe_task_events", None)) or not callable(getattr(self._runtime, "get_task_status", None)):
            raise CapabilityEventsUnsupported("This backend does not support task observation", {"source": "task"})
        if identities is not None:
            _running_loop()
        token = self._reserve()
        holder: list[ListenerHandle] = []

        async def detach(handle: TaskListener) -> None:
            await self._detach(handle)

        try:
            listener = TaskListener(
                self._runtime.subscribe_task_events, self._runtime.get_task_status, detach, self._executor,
                on_event=self._wrapped(on_event, holder, selected),
                on_done=self._wrapped(on_done, holder), on_error=self._wrapped(on_error, holder),
                cancel_grace_s=self._options.cancel_grace_s,
            )
            holder.append(listener)
            self._commit(token, listener, automatic=identities is None, execution=execution, group=group)
            for identity in identities or ():
                listener.track(identity)
            return listener
        finally:
            with self._lock:
                self._reserved.discard(token)

    def add_platform(self, *, on_event: PlatformEventCallback | None = None,
                     on_error: PlatformErrorCallback | None = None, after_cursor: int = 0,
                     event_types: set[str] | None = None, execution: ListenerExecution = "caller",
                     group: str | None = None) -> ListenerHandle:
        _callbacks(on_event, on_error)
        selected = _names(event_types, "event_types")
        if type(after_cursor) is not int or after_cursor < 0:
            raise InvalidRequest("after_cursor must be a nonnegative integer")
        if not callable(getattr(self._runtime, "subscribe_platform_events", None)):
            raise CapabilityEventsUnsupported("This backend does not support platform events", {"source": "platform"})
        return self._add_stream("platform", lambda: subscribe_events(
            self._runtime, sources={"platform"}, after_cursor=after_cursor,
            event_types=set(selected) if selected is not None else None,
        ), on_event, None, on_error, None, execution, group)

    def _add_stream(self, kind: Literal["task", "platform"], source: Callable[[], AsyncIterator[CapabilityEvent]],
                    on_event: Callable[..., Any] | None, on_done: Callable[..., Any] | None,
                    on_error: Callable[..., Any] | None, event_types: frozenset[str] | None,
                    execution: ListenerExecution, group: str | None) -> ListenerHandle:
        _running_loop()
        token = self._reserve()
        holder: list[ListenerHandle] = []
        try:
            listener = _StreamListener(kind, self._runtime, source, self._detach,
                                       on_event=self._wrapped(on_event, holder),
                                       on_done=self._wrapped(on_done, holder),
                                       on_error=self._wrapped(on_error, holder), event_types=event_types,
                                       cancel_grace_s=self._options.cancel_grace_s)
            holder.append(listener)
            self._commit(token, listener, execution=execution, group=group)
            listener.start()
            return listener
        finally:
            with self._lock:
                self._reserved.discard(token)

    def track(self, task_id: str, *, validate: Callable[[Task], None] | None = None) -> None:
        with self._lock:
            listeners = tuple(self._automatic) if not self._closed else ()
        for listener in listeners:
            listener.track(task_id, validate=validate)

    async def close(self) -> None:
        if await on_callback_owner(self.close):
            return
        with self._lock:
            first = self._completion is None
            if first:
                self._completion = Future()
                self._closed = True
            completion = self._completion
            handles = tuple(self._handles) if first else ()
        if first:
            origin = _ORIGIN.get()
            callback_origin = CALLBACK_ORIGIN.get()
            current = (origin[1] if origin is not None and origin[0] is self and (
                origin[2] is asyncio.current_task()
                or callback_origin is not None and callback_origin[0] is origin[1]
            ) else None)
            errors: list[Exception] = []
            interrupted: asyncio.CancelledError | None = None
            try:
                if current in handles:
                    await current.close()
            except asyncio.CancelledError as error:
                interrupted = error
            except Exception as error:
                errors.append(error)
            finally:
                self._cleanup = asyncio.create_task(self._finish_close(tuple(handle for handle in handles if handle is not current), completion, errors))
                self._cleanup.add_done_callback(_consume)
            if interrupted is not None:
                raise interrupted
        wrapped = asyncio.wrap_future(completion)
        wrapped.add_done_callback(_consume)
        await asyncio.shield(wrapped)

    async def _finish_close(self, handles: tuple[ListenerHandle, ...], completion: Future[None],
                            errors: list[Exception]) -> None:
        try:
            results = await asyncio.gather(*(handle.close() for handle in handles), return_exceptions=True)
            errors.extend(error if isinstance(error, Exception) else ListenerCallbackError("Listener cleanup was interrupted")
                          for error in results if isinstance(error, BaseException))
        except BaseException as error:
            errors.append(error if isinstance(error, Exception) else ListenerCallbackError("Listener cleanup was interrupted"))
        finally:
            for close in (self._executor.close, self._loops.close):
                try:
                    await close()
                except Exception as error:
                    errors.append(error)
            if not completion.done():
                if errors:
                    completion.set_exception(ExceptionGroup("Capability listener cleanup failed", errors))
                else:
                    completion.set_result(None)
