import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from robot_agent_runtime.errors import TaskCancelled
from robot_agent_runtime.events import EventBus
from robot_agent_runtime.models import ErrorInfo, Event, Task, TaskState
from robot_agent_runtime.ports import TaskRepository


class TaskStateStore:
    """Local ownership of in-flight tasks and the only writer of task state.

    Every state change in the runtime goes through :meth:`transition`, which
    holds the per-task lock while it persists snapshot and event in a single
    repository call, adopts the authoritative result onto the live ``Task``
    object and only then publishes to the event bus. The six dictionaries are
    keyed by task id and are registered and discarded as a unit, so a task is
    either locally owned in full or not at all.
    """

    def __init__(self, repository: TaskRepository, events: EventBus) -> None:
        self._repository = repository
        self._events = events
        self.executions: dict[str, asyncio.Task[None]] = {}
        self.progress_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.tasks: dict[str, Task] = {}
        self.task_locks: dict[str, asyncio.Lock] = {}
        self.sequences: dict[str, int] = {}
        self._shutting_down = False

    def register(self, task: Task) -> None:
        self.tasks[task.id] = task
        self.task_locks[task.id] = asyncio.Lock()
        self.cancel_events[task.id] = asyncio.Event()
        self.progress_tasks[task.id] = set()
        self.sequences[task.id] = 0

    def register_execution(self, task_id: str, execution: asyncio.Task[None]) -> None:
        self.executions[task_id] = execution
        execution.add_done_callback(
            lambda finished: self.execution_finished(task_id, finished)
        )

    @asynccontextmanager
    async def recovery_ownership(self, task_id: str) -> AsyncIterator[None]:
        """Own a task just long enough to write its recovery transition.

        Recovery must not leave behind local ownership: nothing is executing, so
        the only reason to hold a lock and a sequence counter is to append one
        event whose sequence continues the history written before the restart.
        """
        self.task_locks[task_id] = asyncio.Lock()
        self.sequences[task_id] = 0
        try:
            async for event in self._events.history(task_id):
                self.sequences[task_id] = event.sequence
            yield
        finally:
            self.task_locks.pop(task_id, None)
            self.sequences.pop(task_id, None)

    def cleanup(self, task_id: str) -> None:
        self.executions.pop(task_id, None)
        self.progress_tasks.pop(task_id, None)
        self.cancel_events.pop(task_id, None)
        self.tasks.pop(task_id, None)
        self.task_locks.pop(task_id, None)
        self.sequences.pop(task_id, None)

    def clear(self) -> None:
        self.executions.clear()
        self.progress_tasks.clear()
        self.cancel_events.clear()
        self.tasks.clear()
        self.task_locks.clear()
        self.sequences.clear()

    def begin_shutdown(self) -> None:
        self._shutting_down = True

    def execution_finished(self, task_id: str, execution: asyncio.Task[None]) -> None:
        self.consume_background_exception(execution)
        task = self.tasks.get(task_id)
        if (
            task is not None
            and task.state.is_terminal
            and self.executions.get(task_id) is execution
        ):
            self.cleanup(task_id)

    async def transition(
        self,
        task: Task,
        event_type: str | None,
        changes: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        guard: Callable[[Task], bool] | None = None,
    ) -> bool:
        async with self.task_locks[task.id]:
            if guard is not None and not guard(task):
                return False
            snapshot = self.snapshot(task)
            if changes:
                snapshot = snapshot.model_copy(update=changes)
            persisted = snapshot
            stored_event: Event | None = None
            if event_type is not None:
                sequence = self.sequences[task.id] + 1
                persisted, stored_event = await self._repository.apply_transition(
                    snapshot,
                    Event(
                        event_id=uuid4().hex,
                        task_id=task.id,
                        sequence=sequence,
                        event_type=event_type,
                        data={
                            "state": snapshot.state.value,
                            "current_step": snapshot.current_step,
                            **(data or {}),
                        },
                    ),
                )
                self.sequences[task.id] = sequence
            elif changes is not None:
                persisted = await self._repository.update_task(snapshot)
            self.adopt_snapshot(task, persisted)
            if stored_event is not None:
                self._events.publish(stored_event)
            return True

    async def execution_transition(
        self,
        task: Task,
        event_type: str | None,
        changes: dict[str, Any] | None,
        data: dict[str, Any] | None = None,
    ) -> None:
        transitioned = await self.transition(
            task,
            event_type,
            changes,
            data,
            guard=lambda current: not current.cancel_requested
            and not current.state.is_terminal,
        )
        if not transitioned:
            raise TaskCancelled("Cancellation won the task state transition")

    async def request_cancel(self, task: Task) -> bool:
        return await self.transition(
            task,
            "TASK_CANCEL_REQUESTED",
            {"state": TaskState.CANCELING, "cancel_requested": True},
            guard=lambda current: not current.state.is_terminal
            and not (current.state == TaskState.CANCELING and current.cancel_requested),
        )

    async def try_succeed(self, task: Task) -> bool:
        return await self.transition(
            task,
            "TASK_SUCCEEDED",
            {"state": TaskState.SUCCEEDED},
            guard=lambda current: not current.cancel_requested
            and not current.state.is_terminal,
        )

    async def fail_task(self, task: Task, error: ErrorInfo) -> None:
        await self.drain_progress(task.id)
        await self.transition(
            task,
            "TASK_FAILED",
            {"state": TaskState.FAILED, "error": error},
            {"error": error.to_dict()},
        )

    def progress_callback(self, task_id: str, step: int) -> Callable[[float], None]:
        def report(progress: float) -> None:
            if self._shutting_down:
                return
            task = self.tasks.get(task_id)
            progress_group = self.progress_tasks.get(task_id)
            if task is None or progress_group is None or task.state.is_terminal:
                return
            progress_task = asyncio.create_task(
                self.transition(
                    task,
                    "SKILL_PROGRESS",
                    data={
                        "step": step,
                        "skill": task.steps[step - 1].skill,
                        "progress": progress,
                    },
                    guard=lambda current: not current.state.is_terminal,
                )
            )
            progress_group.add(progress_task)
            progress_task.add_done_callback(
                self._progress_finished(task_id, progress_task)
            )

        return report

    def _progress_finished(
        self, task_id: str, progress_task: asyncio.Task[None]
    ) -> Callable[[asyncio.Task[None]], None]:
        def finished(_: asyncio.Task[None]) -> None:
            self.progress_tasks.get(task_id, set()).discard(progress_task)
            self.consume_background_exception(progress_task)

        return finished

    async def drain_progress(self, task_id: str) -> None:
        pending = list(self.progress_tasks.get(task_id, ()))
        if pending:
            await asyncio.gather(*pending)

    def pending_progress_tasks(self) -> list[asyncio.Task[None]]:
        return [task for group in self.progress_tasks.values() for task in group]

    @staticmethod
    def snapshot(task: Task) -> Task:
        return task.model_copy(deep=True)

    @staticmethod
    def adopt_snapshot(task: Task, snapshot: Task) -> None:
        for name in Task.model_fields:
            setattr(task, name, getattr(snapshot, name))

    @staticmethod
    def consume_background_exception(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
