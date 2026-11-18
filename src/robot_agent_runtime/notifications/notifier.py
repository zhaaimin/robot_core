"""Optional bounded delivery consumers, independent of the task kernel."""

import asyncio
import hashlib
import hmac
import json
import math
import urllib.request
from collections import OrderedDict
from collections.abc import AsyncIterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

from robot_agent_runtime.errors import (
    AgentRuntimeError,
    EventStreamOverflow,
    ExtensionCapacityExceeded,
    InternalError,
    InvalidRequest,
)
from robot_agent_runtime.models import Event, PlatformEvent, TaskState


NotificationEvent = Event | PlatformEvent


@dataclass(frozen=True)
class EventFilter:
    """An empty type set selects all events; terminal filtering is task-only."""

    event_types: frozenset[str] = frozenset()
    terminal_only: bool = False

    def matches(self, event: NotificationEvent) -> bool:
        if self.event_types and event.event_type not in self.event_types:
            return False
        if self.terminal_only:
            if not isinstance(event, Event):
                return False
            try:
                return TaskState(event.data.get("state")).is_terminal
            except (TypeError, ValueError):
                return False
        return True


@dataclass
class WebhookEndpoint:
    """HTTP configuration; sequence is diagnostic, never a global dedup cursor."""

    url: str
    secret: str = ""
    filter: EventFilter = field(default_factory=EventFilter)
    last_acked_sequence: int = 0


class DeliveryChannel(Protocol):
    """One subscriber's ordered output, with no transport-specific address.

    The notifier owns channel cleanup. A successful delivery only confirms the
    channel's transport contract; it does not establish a business-level ACK.
    Implementations must not block their event loop with synchronous I/O.
    """

    async def deliver(self, event: NotificationEvent) -> bool: ...

    async def close(self) -> None: ...


class WebhookChannel:
    """HMAC-signed HTTP delivery on a separate, bounded-submission executor.

    A permit belongs to the native HTTP future until it really completes, even
    when its asyncio waiter is cancelled. At most ``max_workers`` requests can
    be submitted; cancellation cannot accumulate an unbounded executor queue.
    Native socket activity ends according to the HTTP timeout, not cancellation.
    """

    def __init__(
        self, endpoint: WebhookEndpoint, *, timeout_s: float = 5.0,
        max_workers: int = 4,
    ) -> None:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise InvalidRequest("timeout_s must be positive and finite")
        if type(max_workers) is not int or max_workers <= 0:
            raise InvalidRequest("max_workers must be a positive integer")
        self._endpoint = endpoint
        self._timeout_s = timeout_s
        self._closed = False
        self._slots = asyncio.Semaphore(max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="event-webhook",
        )

    async def deliver(self, event: NotificationEvent) -> bool:
        if self._closed:
            return False
        body = json.dumps(event.model_dump(mode="json"), separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "X-Event-Id": event.event_id}
        if isinstance(event, Event):
            headers["X-Event-Sequence"] = str(event.sequence)
        else:
            headers["X-Event-Cursor"] = str(event.cursor)
        if self._endpoint.secret:
            headers["X-Webhook-Signature"] = hmac.new(
                self._endpoint.secret.encode(), body, hashlib.sha256,
            ).hexdigest()
        request = urllib.request.Request(
            self._endpoint.url, data=body, headers=headers, method="POST",
        )
        await self._slots.acquire()
        if self._closed:
            self._slots.release()
            return False
        loop = asyncio.get_running_loop()

        def post() -> None:
            with urllib.request.urlopen(request, timeout=self._timeout_s):
                pass

        def release_slot(completed: Future[None]) -> None:
            try:
                loop.call_soon_threadsafe(self._slots.release)
            except RuntimeError:
                # The native request may outlive its owner loop during exit.
                pass

        try:
            future = self._executor.submit(post)
        except Exception:
            self._slots.release()
            return False
        future.add_done_callback(release_slot)
        try:
            await asyncio.wrap_future(future)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


@dataclass(eq=False)
class _Subscriber:
    subscriber_id: str
    channel: DeliveryChannel
    filter: EventFilter
    queue: asyncio.Queue[NotificationEvent]
    endpoint: WebhookEndpoint | None = None
    pending: set[tuple[type[Event] | type[PlatformEvent], str]] = field(default_factory=set)
    seen: OrderedDict[tuple[type[Event] | type[PlatformEvent], str], None] = field(default_factory=OrderedDict)
    error: AgentRuntimeError | None = None
    retired: bool = False
    worker: asyncio.Task[None] | None = None
    closer: asyncio.Task[None] | None = None


class EventNotifier:
    """Fan out an explicitly consumed stream without wiring into kernel internals.

    There is one worker and at most one cleanup task per subscriber. Retired
    workers that resist cancellation keep occupying capacity until they really
    finish. Deduplication covers queued IDs and the last ``dedup_size`` delivered
    IDs per subscriber; it is bounded, in-memory and not a durable ACK ledger.
    Failed subscribers remain registered for ``subscriber_error`` inspection
    until removed or replaced. Applications must inspect that status and resume
    their authoritative stream as appropriate; this output is best effort.
    """

    def __init__(
        self, *, retry_delays_s: tuple[float, ...] = (1.0, 3.0, 10.0),
        queue_size: int = 1000, endpoint_queue_size: int = 1000,
        drain_timeout_s: float = 5.0, max_subscribers: int = 128,
        dedup_size: int = 1000,
    ) -> None:
        for name, value in (
            ("queue_size", queue_size), ("endpoint_queue_size", endpoint_queue_size),
            ("max_subscribers", max_subscribers), ("dedup_size", dedup_size),
        ):
            if type(value) is not int or value <= 0:
                raise InvalidRequest(f"{name} must be a positive integer")
        if not math.isfinite(drain_timeout_s) or drain_timeout_s <= 0:
            raise InvalidRequest("drain_timeout_s must be positive and finite")
        if any(not math.isfinite(delay) or delay < 0 for delay in retry_delays_s):
            raise InvalidRequest("retry delays must be nonnegative and finite")
        self._retry_delays_s = retry_delays_s
        self._endpoint_queue_size = endpoint_queue_size
        self._drain_timeout_s = drain_timeout_s
        self._max_subscribers = max_subscribers
        self._dedup_size = dedup_size
        self._subscribers: dict[str, _Subscriber] = {}
        self._retired: set[_Subscriber] = set()
        self._queue: asyncio.Queue[NotificationEvent] = asyncio.Queue(maxsize=queue_size)
        self._pump_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False

    def add_subscriber(
        self, subscriber_id: str, channel: DeliveryChannel, *,
        event_filter: EventFilter | None = None,
    ) -> None:
        if self._closed:
            raise InvalidRequest("Notifier is closed")
        if not subscriber_id:
            raise InvalidRequest("subscriber_id must not be empty")
        if len(self._subscribers) + len(self._retired) >= self._max_subscribers:
            raise ExtensionCapacityExceeded("Notification subscriber capacity is exhausted")
        self.remove_subscriber(subscriber_id)
        subscriber = _Subscriber(
            subscriber_id, channel, event_filter or EventFilter(),
            asyncio.Queue(maxsize=self._endpoint_queue_size),
        )
        self._subscribers[subscriber_id] = subscriber
        if self._started:
            self._start_worker(subscriber)

    def add_webhook(
        self, subscriber_id: str, endpoint: WebhookEndpoint, *,
        channel: DeliveryChannel | None = None,
    ) -> None:
        self.add_subscriber(
            subscriber_id, channel or WebhookChannel(endpoint), event_filter=endpoint.filter,
        )
        self._subscribers[subscriber_id].endpoint = endpoint

    def remove_subscriber(self, subscriber_id: str) -> None:
        subscriber = self._subscribers.pop(subscriber_id, None)
        if subscriber is not None:
            self._retired.add(subscriber)
            self._retire(subscriber)

    def remove_webhook(self, subscriber_id: str) -> None:
        self.remove_subscriber(subscriber_id)

    def subscriber_error(self, subscriber_id: str) -> AgentRuntimeError | None:
        """Return the registered subscriber's first failure, or None.

        None also covers unknown IDs and is not evidence of business processing.
        Removal/replacement discards this status; callers own recovery and any
        durable record of the last event they actually processed.
        """
        subscriber = self._subscribers.get(subscriber_id)
        return subscriber.error if subscriber is not None else None

    def _start_worker(self, subscriber: _Subscriber) -> None:
        subscriber.worker = asyncio.create_task(
            self._endpoint_worker(subscriber), name=f"event-worker-{subscriber.subscriber_id}",
        )
        subscriber.worker.add_done_callback(lambda task: self._task_done(subscriber, task))

    def _task_done(self, subscriber: _Subscriber, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        if (subscriber.worker is None or subscriber.worker.done()) and (
            subscriber.closer is not None and subscriber.closer.done()
        ):
            self._retired.discard(subscriber)

    def _retire(self, subscriber: _Subscriber) -> None:
        subscriber.retired = True
        if subscriber.worker is not None and not subscriber.worker.done():
            subscriber.worker.cancel()
        if subscriber.closer is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            subscriber.closer = loop.create_task(
                self._close_channel(subscriber), name=f"event-close-{subscriber.subscriber_id}",
            )
            subscriber.closer.add_done_callback(lambda task: self._task_done(subscriber, task))
        if (subscriber.worker is None or subscriber.worker.done()) and subscriber.closer.done():
            self._retired.discard(subscriber)

    async def _close_channel(self, subscriber: _Subscriber) -> None:
        try:
            await subscriber.channel.close()
        except Exception as error:
            if subscriber.error is None:
                subscriber.error = InternalError(
                    "Notification channel cleanup failed",
                    {"subscriber_id": subscriber.subscriber_id, "reason": str(error)},
                )

    async def start(self) -> None:
        if self._closed:
            raise InvalidRequest("Notifier is closed")
        if self._started:
            return
        self._started = True
        self._pump_task = asyncio.create_task(self._pump(), name="event-notifier")
        for subscriber in self._subscribers.values():
            self._start_worker(subscriber)
        for subscriber in tuple(self._retired):
            self._retire(subscriber)

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._started = False
        tasks: set[asyncio.Task[None]] = set()
        if self._pump_task is not None:
            self._pump_task.cancel()
            tasks.add(self._pump_task)
        for subscriber in tuple(self._subscribers.values()) + tuple(self._retired):
            self._retire(subscriber)
            tasks.update(
                task for task in (subscriber.worker, subscriber.closer) if task is not None
            )
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self._drain_timeout_s)
            for task in done:
                if not task.cancelled():
                    task.exception()
            # Pending native/channel activity is not proof of stopped side effects.
            for task in pending:
                task.add_done_callback(self._consume_result)
        while not self._queue.empty():
            self._queue.get_nowait()

    @staticmethod
    def _consume_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def consume(self, events: AsyncIterator[NotificationEvent]) -> None:
        """Consume and close an owned SDK stream; overflow propagates to its owner."""
        try:
            async for event in events:
                self.publish(event)
        finally:
            close = getattr(events, "aclose", None)
            if close is not None:
                await close()

    def publish(self, event: NotificationEvent) -> None:
        """Queue a snapshot without blocking; input overflow raises immediately."""
        if not self._started:
            raise InvalidRequest("Notifier is not running")
        try:
            self._queue.put_nowait(event.model_copy(deep=True))
        except asyncio.QueueFull:
            raise EventStreamOverflow(
                "Notification input queue overflow; resume the authoritative stream",
                {"event_id": event.event_id, "queue_size": self._queue.maxsize},
            ) from None

    async def _pump(self) -> None:
        while True:
            event = await self._queue.get()
            key = (type(event), event.event_id)
            for subscriber in tuple(self._subscribers.values()):
                if subscriber.error is not None or not subscriber.filter.matches(event):
                    continue
                if key in subscriber.pending or key in subscriber.seen:
                    continue
                try:
                    subscriber.queue.put_nowait(event.model_copy(deep=True))
                    subscriber.pending.add(key)
                except asyncio.QueueFull:
                    subscriber.error = EventStreamOverflow(
                        "Notification subscriber overflow; resume the authoritative stream",
                        {"subscriber_id": subscriber.subscriber_id, "event_id": event.event_id},
                    )
                    self._retire(subscriber)

    async def _endpoint_worker(self, subscriber: _Subscriber) -> None:
        while not subscriber.retired:
            event = await subscriber.queue.get()
            key = (type(event), event.event_id)
            try:
                delivered = await self._deliver_with_retry(subscriber.channel, event)
                if subscriber.retired:
                    return
            except Exception as error:
                subscriber.error = InternalError(
                    "Notification delivery failed",
                    {"subscriber_id": subscriber.subscriber_id, "reason": str(error)},
                )
                self._retire(subscriber)
                return
            finally:
                subscriber.pending.discard(key)
            if not delivered:
                subscriber.error = InternalError(
                    "Notification delivery was not acknowledged",
                    {"subscriber_id": subscriber.subscriber_id, "event_id": event.event_id},
                )
                self._retire(subscriber)
                return
            subscriber.seen[key] = None
            while len(subscriber.seen) > self._dedup_size:
                subscriber.seen.popitem(last=False)
            if subscriber.endpoint is not None and isinstance(event, Event):
                subscriber.endpoint.last_acked_sequence = max(
                    subscriber.endpoint.last_acked_sequence, event.sequence,
                )

    async def _deliver_with_retry(
        self, channel: DeliveryChannel, event: NotificationEvent,
    ) -> bool:
        if await channel.deliver(event):
            return True
        for delay in self._retry_delays_s:
            await asyncio.sleep(delay)
            if await channel.deliver(event):
                return True
        return False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
