import asyncio
import threading
from collections import deque
from collections.abc import AsyncGenerator, Callable
from typing import Any

from robot_agent_runtime.errors import EventStreamOverflow
from robot_agent_runtime.hosting.boundary import bounded_snapshot
from robot_agent_runtime.models import Event, PlatformEvent
from robot_agent_runtime.options import RuntimeOptions


_EMPTY = object()
_END = object()


class HostedEventStream:
    """Keep generator ownership on its host loop and bound shutdown backlog.

    Buffered events remain readable after the owning service stops. Overflow reports
    the last event actually delivered to this consumer, so replay can recover
    every omitted event even when shutdown finished before the consumer resumed.
    """

    def __init__(
        self, source: Callable[[], AsyncGenerator[Event | PlatformEvent, None]], task_id: str,
        after_sequence: int, options: RuntimeOptions, *, cursor_attribute: str = "sequence",
    ) -> None:
        self._source_factory = source
        self._cursor_attribute = cursor_attribute
        self._source: AsyncGenerator[Event | PlatformEvent, None] | None = None
        self._task_id = task_id
        self._options = options
        self._pull_lock = asyncio.Lock()
        self._buffer_lock = threading.Lock()
        self._buffer: deque[Event | PlatformEvent] = deque()
        self._error: BaseException | None = None
        self._overflowed = False
        self._incomplete = False
        self._delivered = after_sequence
        self._offline = False
        self._ended = False

    def _snapshot(self, event: Event | PlatformEvent) -> Event | PlatformEvent:
        return bounded_snapshot(
            event, max_bytes=self._options.max_snapshot_bytes,
            max_depth=self._options.max_snapshot_depth,
        )

    def delivered(self, event: Event | PlatformEvent) -> None:
        with self._buffer_lock:
            self._delivered = getattr(event, self._cursor_attribute)

    def _position(self) -> dict[str, Any]:
        if self._cursor_attribute == "cursor":
            return {"after_cursor": self._delivered}
        if self._task_id == "*":
            return {"stream": "all_task_events", "recovery": "replay tasks individually"}
        return {"task_id": self._task_id, "after_sequence": self._delivered}

    def abort_close(self) -> None:
        with self._buffer_lock:
            if not self._offline:
                self._incomplete = True
                self._offline = True

    def offline_next(self) -> Event | PlatformEvent | object:
        with self._buffer_lock:
            if not self._offline:
                return _EMPTY
            if self._buffer:
                return self._buffer.popleft()
            if self._overflowed:
                self._overflowed = False
                raise EventStreamOverflow(
                    "Shutdown event buffer overflow; resubscribe to replay missed events",
                    details={
                        **self._position(),
                        "queue_size": self._options.max_event_buffer,
                    },
                )
            if self._incomplete:
                self._incomplete = False
                raise EventStreamOverflow(
                    "Event stream shutdown was incomplete; resubscribe to replay missed events",
                    details={
                        **self._position(),
                        "reason": "shutdown_incomplete",
                    },
                )
            if self._error is not None:
                error, self._error = self._error, None
                raise error
            return _END

    async def pull(self) -> Event | PlatformEvent | object:
        async with self._pull_lock:
            buffered = self.offline_next()
            if buffered is not _EMPTY:
                return buffered
            if self._ended:
                return _END
            if self._source is None:
                self._source = self._source_factory()
            try:
                return self._snapshot(await anext(self._source))
            except StopAsyncIteration:
                self._ended = True
                return _END

    async def drain(self) -> None:
        async with self._pull_lock:
            try:
                with self._buffer_lock:
                    offline = self._offline
                if not self._ended and not offline and self._source is not None:
                    async for event in self._source:
                        with self._buffer_lock:
                            if self._offline:
                                break
                            if len(self._buffer) >= self._options.max_event_buffer:
                                self._overflowed = True
                                break
                            self._buffer.append(self._snapshot(event))
            except Exception as error:
                with self._buffer_lock:
                    if not self._offline:
                        self._error = error
            finally:
                if self._source is not None:
                    await self._source.aclose()
                with self._buffer_lock:
                    self._offline = True
                self._ended = True

    async def close(self) -> None:
        async with self._pull_lock:
            if self._source is not None:
                await self._source.aclose()
            self._ended = True
            with self._buffer_lock:
                self._buffer.clear()
                self._error = None
                self._overflowed = False
                self._incomplete = False
                self._offline = True


