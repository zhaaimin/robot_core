import asyncio
from collections.abc import AsyncIterator

from robot_agent_runtime.errors import EventStreamOverflow, InternalError, InvalidRequest
from robot_agent_runtime.models import Event, TaskState
from robot_agent_runtime.ports import TaskRepository


class EventBus:
    """Persistent replay followed by live, per-task event delivery."""

    def __init__(
        self, repository: TaskRepository, *, page_size: int = 128, queue_size: int = 1000,
    ) -> None:
        self._repository = repository
        self._queue_size = queue_size
        self._page_size = page_size
        self._subscribers: dict[
            str, set[asyncio.Queue[Event | EventStreamOverflow | None]]
        ] = {}
        self._watermarks: dict[asyncio.Queue[Event | EventStreamOverflow | None], int] = {}
        self._overflowed: set[asyncio.Queue[Event | EventStreamOverflow | None]] = set()
        self._closed = False
        self._all_subscribers: dict[asyncio.Queue[Event | EventStreamOverflow | None], frozenset[str]] = {}

    def publish(self, event: Event) -> None:
        if self._closed:
            return
        for queue, event_types in tuple(self._all_subscribers.items()):
            if queue in self._overflowed or (event_types and event.event_type not in event_types):
                continue
            if queue.qsize() >= self._queue_size:
                self._overflow("*", queue)
            else:
                queue.put_nowait(event.model_copy(deep=True))
        for queue in tuple(self._subscribers.get(event.task_id, ())):
            if queue in self._overflowed:
                continue
            if queue.qsize() >= self._queue_size:
                self._overflow(event.task_id, queue)
            else:
                queue.put_nowait(event)

    def _overflow(
        self,
        task_id: str,
        queue: asyncio.Queue[Event | EventStreamOverflow | None],
    ) -> None:
        self._overflowed.add(queue)
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(
            EventStreamOverflow(
                "Event stream overflow; resubscribe to replay missed events",
                details={
                    **({"stream": "all_task_events", "recovery": "replay tasks individually"}
                       if task_id == "*" else {"task_id": task_id, "after_sequence": self._watermarks.get(queue, 0)}),
                    "queue_size": self._queue_size,
                },
            )
        )

    async def history(self, task_id: str, after_sequence: int = 0) -> AsyncIterator[Event]:
        page_method = getattr(self._repository, "list_events_page", None)
        watermark = after_sequence
        while True:
            page = (await page_method(task_id, watermark, self._page_size) if page_method is not None
                    else (await self._repository.list_events(task_id, watermark))[:self._page_size])
            for event in page:
                if event.sequence <= watermark:
                    raise InternalError("Repository returned an unordered event page")
                watermark = event.sequence
                yield event
            if len(page) < self._page_size:
                return
            await asyncio.sleep(0)

    async def subscribe_all(self, event_types: set[str] | None = None) -> AsyncIterator[Event]:
        """Observe all future task facts without inventing a global sequence.

        Registration happens on the first pull. This live-only observation has
        no cross-task replay cursor; recover task histories through subscribe.
        """
        if event_types is not None and (type(event_types) not in (set, frozenset) or any(type(value) is not str or not value.strip() for value in event_types)):
            raise InvalidRequest("event_types must be a set of nonempty strings")
        if self._closed:
            return
        queue: asyncio.Queue[Event | EventStreamOverflow | None] = asyncio.Queue(maxsize=self._queue_size + 1)
        self._all_subscribers[queue] = frozenset(event_types or ())
        try:
            while True:
                value = await queue.get()
                if value is None:
                    return
                if isinstance(value, EventStreamOverflow):
                    raise value
                yield value
        finally:
            self._all_subscribers.pop(queue, None)
            self._overflowed.discard(queue)

    async def subscribe(
        self, task_id: str, after_sequence: int = 0
    ) -> AsyncIterator[Event]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if self._closed:
            return

        queue: asyncio.Queue[Event | EventStreamOverflow | None] = asyncio.Queue(
            maxsize=self._queue_size + 1
        )
        self._subscribers.setdefault(task_id, set()).add(queue)
        watermark = after_sequence
        self._watermarks[queue] = watermark
        try:
            # Registration deliberately precedes replay. Events published while the
            # repository read is in flight are queued and removed by the watermark.
            async for event in self.history(task_id, after_sequence):
                if event.sequence > watermark:
                    watermark = event.sequence
                    self._watermarks[queue] = watermark
                    yield event
                    if self._is_terminal(event):
                        return

            # A close sentinel follows all previously published events. Keep
            # draining even after closure so terminal execution events survive.
            while True:
                queued = await queue.get()
                if queued is None:
                    break
                if isinstance(queued, EventStreamOverflow):
                    raise queued
                if queued.sequence <= watermark:
                    continue
                watermark = queued.sequence
                self._watermarks[queue] = watermark
                yield queued
                if self._is_terminal(queued):
                    return
        finally:
            self._watermarks.pop(queue, None)
            self._overflowed.discard(queue)
            task_subscribers = self._subscribers.get(task_id)
            if task_subscribers is not None:
                task_subscribers.discard(queue)
                if not task_subscribers:
                    self._subscribers.pop(task_id, None)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        subscribers = self._subscribers
        self._subscribers = {}
        for task_subscribers in subscribers.values():
            for queue in task_subscribers:
                queue.put_nowait(None)
        for queue in tuple(self._all_subscribers):
            queue.put_nowait(None)

    @staticmethod
    def _is_terminal(event: Event) -> bool:
        try:
            return TaskState(event.data.get("state")).is_terminal
        except (TypeError, ValueError):
            return False
