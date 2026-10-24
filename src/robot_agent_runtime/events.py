import asyncio
from collections.abc import AsyncIterator

from robot_agent_runtime.errors import EventStreamOverflow
from robot_agent_runtime.models import Event, TaskState
from robot_agent_runtime.ports import TaskRepository


class EventBus:
    """Persistent replay followed by live, per-task event delivery."""

    def __init__(self, repository: TaskRepository) -> None:
        self._repository = repository
        self._queue_size = 1000
        self._subscribers: dict[
            str, set[asyncio.Queue[Event | EventStreamOverflow | None]]
        ] = {}
        self._watermarks: dict[asyncio.Queue[Event | EventStreamOverflow | None], int] = {}
        self._overflowed: set[asyncio.Queue[Event | EventStreamOverflow | None]] = set()
        self._closed = False

    def publish(self, event: Event) -> None:
        if self._closed:
            return
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
                    "task_id": task_id,
                    "after_sequence": self._watermarks.get(queue, 0),
                    "queue_size": self._queue_size,
                },
            )
        )

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
            replay = await self._repository.list_events(task_id, after_sequence)
            for event in replay:
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

    @staticmethod
    def _is_terminal(event: Event) -> bool:
        try:
            return TaskState(event.data.get("state")).is_terminal
        except (TypeError, ValueError):
            return False
