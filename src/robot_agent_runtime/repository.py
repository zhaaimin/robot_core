from datetime import UTC, datetime
from heapq import nsmallest

from robot_agent_runtime.errors import InvalidRequest, TaskNotFound
from robot_agent_runtime.models import Event, Task


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._events: dict[str, dict[int, Event]] = {}

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def create_task(self, task: Task) -> Task:
        if task.id in self._tasks:
            raise InvalidRequest(
                "Task id already exists",
                details={"conflict": "task_id", "task_id": task.id},
            )
        self._check_idempotency_key(task)

        stored = self._copy_task(task)
        self._tasks[task.id] = stored
        return self._copy_task(stored)

    async def get_task(self, task_id: str) -> Task | None:
        task = self._tasks.get(task_id)
        return self._copy_task(task) if task is not None else None

    async def update_task(self, task: Task) -> Task:
        if task.id not in self._tasks:
            raise TaskNotFound("Task not found", details={"task_id": task.id})
        self._check_idempotency_key(task)

        updated = task.model_copy(update={"updated_at": datetime.now(UTC)})
        stored = self._copy_task(updated)
        self._tasks[task.id] = stored
        return self._copy_task(stored)

    async def append_event(self, event: Event) -> Event:
        task_events = self._events.setdefault(event.task_id, {})
        if event.sequence in task_events:
            raise InvalidRequest(
                "Event sequence already exists for task",
                details={
                    "conflict": "event_sequence",
                    "task_id": event.task_id,
                    "sequence": event.sequence,
                },
            )

        stored = Event.model_validate_json(event.model_dump_json())
        task_events[event.sequence] = stored
        return Event.model_validate_json(stored.model_dump_json())

    async def apply_transition(self, task: Task, event: Event) -> tuple[Task, Event]:
        if task.id != event.task_id:
            raise InvalidRequest("Task and event task ids must match")
        original_task = (
            self._copy_task(self._tasks[task.id]) if task.id in self._tasks else None
        )
        original_events = self._events.get(event.task_id)
        if original_events is not None:
            original_events = dict(original_events)
        try:
            persisted = await self.update_task(task)
            stored_event = await self.append_event(event)
        except BaseException:
            if original_task is None:
                self._tasks.pop(task.id, None)
            else:
                self._tasks[task.id] = original_task
            if original_events is None:
                self._events.pop(event.task_id, None)
            else:
                self._events[event.task_id] = original_events
            raise
        return persisted, stored_event

    async def list_events(
        self, task_id: str, after_sequence: int = 0
    ) -> list[Event]:
        task_events = self._events.get(task_id, {})
        return [
            Event.model_validate_json(task_events[sequence].model_dump_json())
            for sequence in sorted(task_events)
            if sequence > after_sequence
        ]

    async def get_nonterminal_tasks(self) -> list[Task]:
        tasks = sorted(
            (task for task in self._tasks.values() if not task.state.is_terminal),
            key=lambda task: task.created_at,
        )
        return [self._copy_task(task) for task in tasks]

    async def list_events_page(
        self, task_id: str, after_sequence: int = 0, limit: int = 128,
    ) -> list[Event]:
        if type(limit) is not int or limit <= 0:
            raise InvalidRequest("Event page limit must be a positive integer")
        events = self._events.get(task_id, {})
        sequences = nsmallest(limit, (sequence for sequence in events if sequence > after_sequence))
        return [Event.model_validate_json(events[sequence].model_dump_json()) for sequence in sequences]

    async def find_by_idempotency_key(self, key: str) -> Task | None:
        if not key:
            return None
        for task in self._tasks.values():
            if task.idempotency_key == key:
                return self._copy_task(task)
        return None

    def _check_idempotency_key(self, task: Task) -> None:
        if not task.idempotency_key:
            return
        for existing in self._tasks.values():
            if existing.id != task.id and existing.idempotency_key == task.idempotency_key:
                raise InvalidRequest(
                    "Idempotency key already exists",
                    details={
                        "conflict": "idempotency_key",
                        "idempotency_key": task.idempotency_key,
                    },
                )

    @staticmethod
    def _copy_task(task: Task) -> Task:
        return Task.model_validate_json(task.model_dump_json())
