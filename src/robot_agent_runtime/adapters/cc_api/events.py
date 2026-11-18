import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from typing import Any, TypeVar

from robot_agent_runtime.adapters.cc_api.driver import CcApiDriver
from robot_agent_runtime.errors import (
    AgentRuntimeError,
    EventStreamOverflow,
    InvalidRequest,
    PlatformEventSourceUnavailable,
    RobotResponseError,
)
from robot_agent_runtime.hosting.boundary import bounded_snapshot
from robot_agent_runtime.models import PlatformEvent
from robot_agent_runtime.options import RuntimeOptions


T = TypeVar("T")


class CcApiEventSource:
    """Keep vendor fault observations separate from task and safety semantics.

    The application starts this source before starting the runtime, so native
    updates are already buffered when an isolated watcher arrives. All driver
    and queue operations stay on the application loop; the watcher receives
    detached values through a cancellation-aware concurrent-future bridge.

    Firmware fault schemas and a snapshot/update ordering fence are unknown.
    Payloads therefore remain opaque JSON, carry an unverified ordering marker,
    and never imply raised/cleared state, severity, or that a robot is safe.
    Buffered distinct updates survive the initial query. Only consecutive equal
    update payloads are merged. A communication failure is a separate observed
    fact followed by an explicit source error, with no reconnect or retry.
    """

    def __init__(self, driver: CcApiDriver, *, options: RuntimeOptions | None = None) -> None:
        self._driver = driver
        self._options = options or RuntimeOptions()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready: asyncio.Event | None = None
        self._started = False
        self._closed = False
        self._detach: list[Callable[[], None]] = []
        self._buffer: deque[PlatformEvent] = deque()
        self._failure: AgentRuntimeError | None = None
        self._terminal_event: PlatformEvent | None = None
        self._watcher: object | None = None
        self._snapshot_task: asyncio.Task[None] | None = None
        self._snapshot_done = False
        self._snapshot_event: PlatformEvent | None = None
        self._last_payload: str | None = None

    def _owner(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise InvalidRequest("CC event source lifecycle must run on its owning loop")
        return loop

    async def start(self) -> None:
        self._owner()
        if self._closed or self._failure is not None:
            raise PlatformEventSourceUnavailable("CC event source cannot be restarted")
        if self._started:
            return
        self._ready = asyncio.Event()
        try:
            self._detach.append(self._driver.subscribe_fault(self._received))
            self._detach.append(self._driver.subscribe_failure(self._disconnected))
            await self._driver.start_fault_events()
        except Exception:
            for detach in self._detach:
                detach()
            self._detach.clear()
            raise
        self._started = True

    async def close(self) -> None:
        self._owner()
        self._closed = True
        for detach in self._detach:
            detach()
        self._detach.clear()
        self._wake()

    def _wake(self) -> None:
        if self._ready is not None:
            self._ready.set()

    def _payload(self, payload: Any) -> tuple[Any, str]:
        values = bounded_snapshot(payload, max_bytes=self._options.max_snapshot_bytes,
                                  max_depth=self._options.max_snapshot_depth)
        try:
            canonical = json.dumps(values, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise RobotResponseError("CC fault payload is not JSON") from None
        return json.loads(canonical), canonical

    def _event(self, event_type: str, data: dict[str, Any]) -> PlatformEvent:
        event = PlatformEvent(source="cc_api", event_type=event_type, data=data)
        return bounded_snapshot(event, max_bytes=self._options.max_snapshot_bytes,
                                max_depth=self._options.max_snapshot_depth)

    def _fault_event(self, event_type: str, payload: Any) -> tuple[PlatformEvent, str]:
        values, canonical = self._payload(payload)
        return self._event(event_type, {
            "payload": values,
            "sync": {"initial_snapshot_order": "unverified"},
        }), canonical

    def _fail(self, error: AgentRuntimeError) -> None:
        if not self._closed and self._failure is None:
            self._failure = error
            self._wake()

    def _received(self, payload: Any) -> None:
        if self._closed or self._failure is not None:
            return
        try:
            event, canonical = self._fault_event("robot.fault.changed", payload)
            if canonical == self._last_payload:
                return
            if len(self._buffer) >= self._options.max_event_buffer:
                self._fail(EventStreamOverflow("CC fault source buffer overflow"))
                return
            self._buffer.append(event)
            self._last_payload = canonical
            self._wake()
        except Exception as error:
            self._fail(PlatformEventSourceUnavailable("CC fault update is invalid",
                       details={"cause": type(error).__name__}))

    def _disconnected(self, error: AgentRuntimeError) -> None:
        if self._closed or self._failure is not None:
            return
        try:
            self._terminal_event = self._event("robot.connection.lost", {"error": error.to_dict()})
        except Exception:
            self._terminal_event = None
        self._fail(PlatformEventSourceUnavailable("CC event source lost its connection",
                   details={"cause": error.info.code}))

    async def _collect_initial(self) -> None:
        try:
            payload = await self._driver.request("cc.api.fault.current.get", {})
            if not self._closed and self._failure is None:
                event, canonical = self._fault_event("robot.fault.snapshot", payload)
                self._snapshot_event = event
                if not self._buffer:
                    self._last_payload = canonical
        except Exception as error:
            self._fail(PlatformEventSourceUnavailable("Could not collect current CC faults",
                       details={"cause": type(error).__name__}))
        finally:
            self._snapshot_done = True
            self._wake()

    async def _begin_watch(self, token: object) -> None:
        if not self._started or self._closed:
            raise PlatformEventSourceUnavailable("CC event source is unavailable")
        if self._watcher is not None:
            raise InvalidRequest("CC event source already has an active watcher")
        self._watcher = token
        if self._snapshot_task is None and self._failure is None:
            self._snapshot_task = asyncio.create_task(self._collect_initial())

    async def _next(self) -> PlatformEvent | None:
        while True:
            if self._snapshot_event is not None:
                event, self._snapshot_event = self._snapshot_event, None
                return deepcopy(event)
            if self._snapshot_done or self._failure is not None or self._closed:
                if self._buffer:
                    return deepcopy(self._buffer.popleft())
                if self._terminal_event is not None:
                    event, self._terminal_event = self._terminal_event, None
                    return deepcopy(event)
                if self._failure is not None:
                    raise self._failure
                if self._closed:
                    return None
            ready = self._ready
            if ready is None:
                raise PlatformEventSourceUnavailable("CC event source was not started")
            ready.clear()
            await ready.wait()

    async def _finish_watch(self, token: object) -> None:
        if self._watcher is token:
            self._watcher = None

    async def _on_owner(self, operation: Callable[[], Awaitable[T]]) -> T:
        owner = self._loop
        if owner is None or owner.is_closed():
            raise PlatformEventSourceUnavailable("CC event source owner loop is unavailable")
        if owner is asyncio.get_running_loop():
            return await operation()
        coroutine = operation()
        try:
            completed = asyncio.run_coroutine_threadsafe(coroutine, owner)
        except RuntimeError:
            coroutine.close()
            raise PlatformEventSourceUnavailable("CC event source owner loop is unavailable") from None
        return await asyncio.wrap_future(completed)

    async def watch(self) -> AsyncIterator[PlatformEvent]:
        token = object()
        try:
            await self._on_owner(lambda: self._begin_watch(token))
            while True:
                event = await self._on_owner(self._next)
                if event is None:
                    return
                yield event
        finally:
            await self._on_owner(lambda: self._finish_watch(token))
