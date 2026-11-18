import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from robot_agent_runtime.errors import AgentRuntimeError, EventStreamOverflow, ExtensionCapacityExceeded, InternalError, InvalidRequest
from robot_agent_runtime.hosting.boundary import bounded_snapshot
from robot_agent_runtime.models import PlatformEvent
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.ports import PlatformEventRepository


@dataclass(eq=False)
class _Subscriber:
    queue: asyncio.Queue[PlatformEvent | BaseException | None]
    delivered: int
    event_types: frozenset[str]
    overflowed: bool = False


class PlatformEventService:
    """Publish only saved facts and recover replay/live gaps with journal cursors.

    This object has one asyncio owner, supplied by NotificationHost. Payloads
    and errors remain opaque; source interpretation and network delivery live
    outside this service. It never drops an event without a recoverable error.
    """

    def __init__(self, repository: PlatformEventRepository, *, options: RuntimeOptions | None = None) -> None:
        self._repository = repository
        self._options = options or RuntimeOptions()
        self._subscribers: set[_Subscriber] = set()
        self._write_lock = asyncio.Lock()
        self._started = False
        self._finished = False
        self._closed = False
        self._failure: AgentRuntimeError | None = None
        self._latest = 0

    def _snapshot(self, value: PlatformEvent) -> PlatformEvent:
        copied = bounded_snapshot(value, max_bytes=self._options.max_snapshot_bytes, max_depth=self._options.max_snapshot_depth)
        if type(copied) is not PlatformEvent:
            raise InvalidRequest("Expected a PlatformEvent")
        return copied

    async def start(self) -> None:
        if self._closed:
            raise InvalidRequest("Platform event service is closed")
        if self._started:
            return
        await self._repository.start()
        _, self._latest = await self._repository.platform_event_bounds()
        self._started = True

    async def publish(self, event: PlatformEvent) -> PlatformEvent:
        value = self._snapshot(event)
        async with self._write_lock:
            if not self._started or self._finished:
                raise InvalidRequest("Platform event service is not accepting events")
            stored = self._snapshot(await self._repository.append_platform_event(value))
            if stored.cursor <= 0 or stored.event_id != value.event_id:
                raise InternalError("Journal returned an invalid platform event")
            if stored.cursor <= self._latest:
                return stored
            self._latest = stored.cursor
            for subscriber in tuple(self._subscribers):
                if subscriber.overflowed or (subscriber.event_types and stored.event_type not in subscriber.event_types):
                    continue
                if subscriber.queue.qsize() >= self._options.max_event_buffer:
                    self._overflow(subscriber)
                else:
                    subscriber.queue.put_nowait(self._snapshot(stored))
            return stored

    def _overflow(self, subscriber: _Subscriber) -> None:
        subscriber.overflowed = True
        while not subscriber.queue.empty():
            subscriber.queue.get_nowait()
        subscriber.queue.put_nowait(EventStreamOverflow(
            "Platform event stream overflow; resume from the last delivered cursor",
            {"after_cursor": subscriber.delivered, "queue_size": self._options.max_event_buffer},
        ))

    async def finish(self, error: AgentRuntimeError | None = None) -> None:
        async with self._write_lock:
            if self._finished:
                return
            self._finished = True
            self._failure = error
            for subscriber in tuple(self._subscribers):
                if not subscriber.overflowed:
                    subscriber.queue.put_nowait(error)

    async def close(self) -> None:
        if self._closed:
            return
        await self.finish()
        self._closed = True
        await self._repository.close()

    async def subscribe(self, after_cursor: int = 0, event_types: set[str] | frozenset[str] | None = None) -> AsyncIterator[PlatformEvent]:
        if type(after_cursor) is not int or after_cursor < 0:
            raise InvalidRequest("after_cursor must be a nonnegative integer")
        if event_types is not None and (not isinstance(event_types, (set, frozenset)) or any(type(value) is not str or not value.strip() for value in event_types)):
            raise InvalidRequest("event_types must be a set of nonempty strings")
        if not self._started or self._closed:
            raise InvalidRequest("Platform event service is not running")
        if len(self._subscribers) >= self._options.max_subscriptions:
            raise ExtensionCapacityExceeded("Platform event subscription capacity is exhausted")
        subscriber = _Subscriber(asyncio.Queue(maxsize=self._options.max_event_buffer + 1), after_cursor, frozenset(event_types or ()))
        self._subscribers.add(subscriber)
        watermark = after_cursor
        try:
            while True:
                if subscriber.overflowed:
                    raise EventStreamOverflow("Platform event stream overflow; resume from the last delivered cursor", {"after_cursor": subscriber.delivered, "queue_size": self._options.max_event_buffer})
                page = await self._repository.list_platform_events(watermark, self._options.event_page_size)
                if not page:
                    break
                for event in page:
                    if subscriber.overflowed:
                        raise EventStreamOverflow("Platform event stream overflow; resume from the last delivered cursor", {"after_cursor": subscriber.delivered, "queue_size": self._options.max_event_buffer})
                    if type(event) is not PlatformEvent or event.cursor <= watermark:
                        raise InternalError("Journal returned an unordered platform event page")
                    watermark = event.cursor
                    if not subscriber.event_types or event.event_type in subscriber.event_types:
                        subscriber.delivered = event.cursor
                        yield self._snapshot(event)
                await asyncio.sleep(0)
            if self._finished and subscriber.queue.empty():
                if self._failure is not None:
                    raise self._failure
                return
            while True:
                value = await subscriber.queue.get()
                if value is None:
                    return
                if isinstance(value, EventStreamOverflow):
                    raise EventStreamOverflow(value.info.message, {**value.info.details, "after_cursor": subscriber.delivered})
                if isinstance(value, BaseException):
                    raise value
                if value.cursor > watermark:
                    watermark = value.cursor
                    subscriber.delivered = value.cursor
                    yield self._snapshot(value)
        finally:
            self._subscribers.discard(subscriber)
