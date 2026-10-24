import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any
from uuid import uuid4

from robot_agent_runtime.errors import (
    AgentRuntimeError,
    InternalError,
    SkillTimeout,
    TaskCancelled,
    TaskNotFound,
)
from robot_agent_runtime.events import EventBus
from robot_agent_runtime.models import (
    ErrorInfo,
    Event,
    SkillContext,
    SkillDefinition,
    Task,
    TaskRequest,
    TaskState,
)
from robot_agent_runtime.ports import Reconciler, Skill, TaskRepository
from robot_agent_runtime.recovery import RecoveryOutcome, recover_nonterminal_tasks
from robot_agent_runtime.registry import SkillRegistry
from robot_agent_runtime.resources import ResourceManager
from robot_agent_runtime.safety import SafetyManager


class RuntimeCore:
    def __init__(
        self,
        repository: TaskRepository,
        skills: Mapping[str, Skill],
        reconciler: Reconciler | None = None,
        allowed_permissions: set[str] | None = None,
    ) -> None:
        self._repository = repository
        self._reconciler = reconciler
        self._registry = SkillRegistry(skills)
        self._safety = SafetyManager(self._registry, allowed_permissions)
        self._resources = ResourceManager()
        self._events = EventBus(repository)
        self._executions: dict[str, asyncio.Task[None]] = {}
        self._progress_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, Task] = {}
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._sequences: dict[str, int] = {}
        self._idempotency_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False
        self._closed = False

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closing or self._closed:
                raise RuntimeError("RuntimeCore is closed")
            if self._started:
                return
            try:
                await self._repository.start()
                await recover_nonterminal_tasks(
                    self._repository, self._reconciler, self._apply_recovery
                )
            except BaseException:
                await self._cleanup_failed_start()
                raise
            self._started = True

    async def _apply_recovery(self, task: Task, outcome: RecoveryOutcome) -> None:
        self._task_locks[task.id] = asyncio.Lock()
        self._sequences[task.id] = 0
        try:
            events = await self._repository.list_events(task.id)
            if events:
                self._sequences[task.id] = max(event.sequence for event in events)
            await self._transition(
                task, outcome.event_type, outcome.changes, outcome.data
            )
        finally:
            self._task_locks.pop(task.id, None)
            self._sequences.pop(task.id, None)

    async def _cleanup_failed_start(self) -> None:
        cleanup = asyncio.create_task(self._repository.close())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.gather(cleanup, return_exceptions=True)
            raise
        except Exception:
            # Preserve the exception that made startup fail.
            pass
        finally:
            self._task_locks.clear()
            self._sequences.clear()
            self._started = False

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close_impl())
            close_task = self._close_task
        # Caller cancellation must not cancel the shared shutdown. A later close
        # call awaits this same task and therefore retries the wait, not the work.
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closing = True
            executions = list(self._executions.values())
            for execution in executions:
                if not execution.done():
                    execution.cancel()

        results = await asyncio.gather(*executions, return_exceptions=True)
        remaining_progress = [
            task for group in self._progress_tasks.values() for task in group
        ]
        for task in remaining_progress:
            if not task.done():
                task.cancel()
        results.extend(await asyncio.gather(*remaining_progress, return_exceptions=True))
        errors = [result for result in results if isinstance(result, Exception)]
        self._events.close()
        try:
            await self._repository.close()
        except Exception as error:
            errors.append(error)
        finally:
            async with self._lifecycle_lock:
                self._executions.clear()
                self._progress_tasks.clear()
                self._cancel_events.clear()
                self._tasks.clear()
                self._task_locks.clear()
                self._sequences.clear()
                self._started = False
                self._closed = True
        if errors:
            raise ExceptionGroup("RuntimeCore shutdown errors", errors)

    async def submit_task(self, request: TaskRequest) -> Task:
        async with self._lifecycle_lock:
            self._require_running()
            operation = asyncio.create_task(self._submit_owned(request))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # The lifecycle lock remains held until local ownership has been
                # completely registered; only then may caller cancellation escape.
                await asyncio.gather(operation, return_exceptions=True)
                raise

    async def _submit_owned(self, request: TaskRequest) -> Task:
        async with self._idempotency_lock:
            if request.idempotency_key:
                existing = await self._repository.find_by_idempotency_key(
                    request.idempotency_key
                )
                if existing is not None:
                    return existing

            task = Task(
                id=uuid4().hex,
                task_type=request.task_type,
                state=TaskState.CREATED,
                steps=request.steps,
                priority=request.priority,
                timeout_s=request.timeout_s,
                idempotency_key=request.idempotency_key,
                metadata=request.metadata,
            )
            await self._repository.create_task(task)
            self._tasks[task.id] = task
            self._task_locks[task.id] = asyncio.Lock()
            self._cancel_events[task.id] = asyncio.Event()
            self._progress_tasks[task.id] = set()
            self._sequences[task.id] = 0

        await self._transition(task, "TASK_CREATED")
        await self._transition(task, "TASK_VALIDATING", {"state": TaskState.VALIDATING})
        try:
            for call in request.steps:
                self._safety.validate(call)
        except AgentRuntimeError as error:
            await self._fail_task(task, error.info)
            self._cleanup_local(task.id)
            return self._snapshot(task)
        except Exception as error:
            mapped = InternalError(
                f"Task validation failed: {error}",
                details={"exception_type": type(error).__name__},
            )
            await self._fail_task(task, mapped.info)
            self._cleanup_local(task.id)
            return self._snapshot(task)

        await self._transition(task, "TASK_QUEUED", {"state": TaskState.QUEUED})
        execution = asyncio.create_task(
            self._execute(task), name=f"robot-task-{task.id}"
        )
        self._executions[task.id] = execution
        execution.add_done_callback(
            lambda finished: self._execution_finished(task.id, finished)
        )
        return self._snapshot(task)

    async def get_task_status(self, task_id: str) -> Task:
        self._require_running()
        return await self._get_task(task_id)

    async def _get_task(self, task_id: str) -> Task:
        task = await self._repository.get_task(task_id)
        if task is None:
            raise TaskNotFound("Task not found", details={"task_id": task_id})
        return task

    async def list_skills(self) -> list[SkillDefinition]:
        self._require_running()
        return self._registry.list_skills()

    async def cancel_task(self, task_id: str) -> Task:
        async with self._lifecycle_lock:
            self._require_running()
            task = self._tasks.get(task_id)
            if task is None:
                persisted = await self._get_task(task_id)
                if persisted.state.is_terminal:
                    return persisted
                raise InternalError(
                    "Nonterminal task has no local execution owner",
                    details={"task_id": task_id},
                )
            if task.state.is_terminal:
                return await self._get_task(task_id)

            execution = self._executions.get(task_id)
            await self._request_cancel(task)
            if task.state.is_terminal:
                return await self._get_task(task_id)
            cancel_event = self._cancel_events[task_id]
            cancel_event.set()

        if execution is not None and execution is not asyncio.current_task():
            await asyncio.shield(execution)
        return await self._get_task(task_id)

    def subscribe_task_events(
        self, task_id: str, after_sequence: int = 0
    ) -> AsyncIterator[Event]:
        return self._events.subscribe(task_id, after_sequence)

    async def _execute(self, task: Task) -> None:
        cancel_event = self._cancel_events[task.id]
        current_step = 0
        step_results: list[dict[str, Any]] = []
        try:
            if cancel_event.is_set():
                raise TaskCancelled("Task cancelled before execution")
            await self._execution_transition(
                task, "TASK_RUNNING", {"state": TaskState.RUNNING}
            )

            for index, call in enumerate(task.steps, start=1):
                current_step = index
                if cancel_event.is_set():
                    raise TaskCancelled("Task cancelled before skill execution")
                await self._execution_transition(task, None, {"current_step": index})
                skill = self._registry.find(call.skill)
                context = SkillContext(
                    task_id=task.id,
                    execution_id=task.id,
                    cancel_event=cancel_event,
                    progress=self._progress_callback(task.id, index),
                )
                resources = skill.definition.resources
                if resources:
                    await self._execution_transition(
                        task, "TASK_WAITING", {"state": TaskState.WAITING}
                    )
                async with self._resources.acquire(resources, cancel_event):
                    if cancel_event.is_set():
                        raise TaskCancelled("Task cancelled while waiting for resources")
                    if resources:
                        await self._execution_transition(
                            task, "TASK_RUNNING", {"state": TaskState.RUNNING}
                        )
                    await self._execution_transition(
                        task,
                        "SKILL_STARTED",
                        None,
                        {"step": index, "skill": call.skill},
                    )
                    timeout_s = (
                        call.timeout_s
                        if call.timeout_s is not None
                        else skill.definition.timeout_s
                    )
                    result = await self._execute_with_timeout(
                        skill, context, call.params, index, timeout_s
                    )
                await self._drain_progress(task.id)

                if cancel_event.is_set():
                    raise TaskCancelled("Task cancelled during skill execution")
                if not result.success:
                    error = result.error or ErrorInfo(
                        code="SKILL_EXECUTION_FAILED",
                        message="Skill returned an unsuccessful result without error details",
                    )
                    await self._transition(
                        task,
                        "SKILL_FAILED",
                        data={"step": index, "skill": call.skill, "error": error.to_dict()},
                    )
                    await self._fail_task(task, error)
                    return

                await self._execution_transition(
                    task,
                    "SKILL_SUCCEEDED",
                    None,
                    {"step": index, "skill": call.skill, "output": result.output},
                )
                step_results.append(
                    {"step": index, "skill": call.skill, "output": result.output}
                )
                await self._execution_transition(
                    task,
                    "TASK_RESULT",
                    {"result": {"steps": step_results}},
                    {"step": index, "skill": call.skill, "output": result.output},
                )

            await self._drain_progress(task.id)
            if not await self._try_succeed(task):
                raise TaskCancelled("Cancellation was accepted before task success")
        except TaskCancelled as error:
            await self._drain_progress(task.id)
            await self._transition(
                task,
                "SKILL_CANCELLED",
                data={
                    "step": current_step,
                    "skill": self._skill_name(task, current_step),
                    "error": error.info.to_dict(),
                },
            )
            await self._transition(
                task,
                "TASK_CANCELED",
                {
                    "state": TaskState.CANCELED,
                    "cancel_requested": True,
                    "error": error.info,
                },
            )
        except asyncio.CancelledError:
            await self._drain_progress(task.id)
            error = TaskCancelled("Local execution stopped by runtime shutdown")
            await self._transition(
                task,
                "TASK_CANCELED",
                {"state": TaskState.CANCELED, "error": error.info},
            )
        except AgentRuntimeError as error:
            await self._fail_skill(
                task, current_step, self._skill_name(task, current_step), error.info
            )
        except Exception as error:
            mapped = InternalError(
                f"Unhandled skill exception: {error}",
                details={
                    "step": current_step,
                    "skill": self._skill_name(task, current_step),
                    "exception_type": type(error).__name__,
                },
            )
            await self._fail_skill(task, current_step, mapped.info.details["skill"], mapped.info)

    async def _execute_with_timeout(
        self,
        skill: Skill,
        context: SkillContext,
        params: dict[str, Any],
        step: int,
        timeout_s: float,
    ) -> Any:
        skill_task = asyncio.create_task(skill.execute(context, params))
        try:
            return await asyncio.wait_for(asyncio.shield(skill_task), timeout=timeout_s)
        except TimeoutError:
            if skill_task.done():
                # A skill-raised TimeoutError is an ordinary skill exception, not
                # evidence that wait_for's own deadline expired.
                return await skill_task
            skill_task.cancel()
            await asyncio.gather(skill_task, return_exceptions=True)
            raise SkillTimeout(
                f"Skill {skill.definition.name} timed out after {timeout_s} seconds",
                details={
                    "step": step,
                    "skill": skill.definition.name,
                    "timeout_s": timeout_s,
                },
            ) from None
        except asyncio.CancelledError:
            skill_task.cancel()
            await asyncio.gather(skill_task, return_exceptions=True)
            raise

    async def _fail_skill(
        self, task: Task, step: int, skill: str, error: ErrorInfo
    ) -> None:
        await self._transition(
            task,
            "SKILL_FAILED",
            data={"step": step, "skill": skill, "error": error.to_dict()},
        )
        await self._fail_task(task, error)

    async def _fail_task(self, task: Task, error: ErrorInfo) -> None:
        await self._drain_progress(task.id)
        await self._transition(
            task,
            "TASK_FAILED",
            {"state": TaskState.FAILED, "error": error},
            {"error": error.to_dict()},
        )

    async def _request_cancel(self, task: Task) -> bool:
        return await self._transition(
            task,
            "TASK_CANCEL_REQUESTED",
            {"state": TaskState.CANCELING, "cancel_requested": True},
            guard=lambda current: not current.state.is_terminal
            and not (current.state == TaskState.CANCELING and current.cancel_requested),
        )

    async def _try_succeed(self, task: Task) -> bool:
        return await self._transition(
            task,
            "TASK_SUCCEEDED",
            {"state": TaskState.SUCCEEDED},
            guard=lambda current: not current.cancel_requested
            and not current.state.is_terminal,
        )

    async def _execution_transition(
        self,
        task: Task,
        event_type: str | None,
        changes: dict[str, Any] | None,
        data: dict[str, Any] | None = None,
    ) -> None:
        transitioned = await self._transition(
            task,
            event_type,
            changes,
            data,
            guard=lambda current: not current.cancel_requested
            and not current.state.is_terminal,
        )
        if not transitioned:
            raise TaskCancelled("Cancellation won the task state transition")

    async def _transition(
        self,
        task: Task,
        event_type: str | None,
        changes: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        guard: Callable[[Task], bool] | None = None,
    ) -> bool:
        async with self._task_locks[task.id]:
            if guard is not None and not guard(task):
                return False
            snapshot = self._snapshot(task)
            if changes:
                snapshot = snapshot.model_copy(update=changes)
            persisted = snapshot
            stored_event: Event | None = None
            if event_type is not None:
                sequence = self._sequences[task.id] + 1
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
                self._sequences[task.id] = sequence
            elif changes is not None:
                persisted = await self._repository.update_task(snapshot)
            self._adopt_snapshot(task, persisted)
            if stored_event is not None:
                self._events.publish(stored_event)
            return True

    def _progress_callback(self, task_id: str, step: int) -> Callable[[float], None]:
        def report(progress: float) -> None:
            if self._closing or self._closed:
                return
            task = self._tasks.get(task_id)
            progress_group = self._progress_tasks.get(task_id)
            if task is None or progress_group is None:
                return
            progress_task = asyncio.create_task(
                self._transition(
                    task,
                    "SKILL_PROGRESS",
                    data={
                        "step": step,
                        "skill": task.steps[step - 1].skill,
                        "progress": progress,
                    },
                )
            )
            progress_group.add(progress_task)
            progress_task.add_done_callback(self._progress_finished(task_id, progress_task))

        return report

    def _progress_finished(
        self, task_id: str, progress_task: asyncio.Task[None]
    ) -> Callable[[asyncio.Task[None]], None]:
        def finished(_: asyncio.Task[None]) -> None:
            self._progress_tasks.get(task_id, set()).discard(progress_task)
            self._consume_background_exception(progress_task)

        return finished

    async def _drain_progress(self, task_id: str) -> None:
        pending = list(self._progress_tasks.get(task_id, ()))
        if pending:
            await asyncio.gather(*pending)

    def _execution_finished(self, task_id: str, execution: asyncio.Task[None]) -> None:
        self._consume_background_exception(execution)
        task = self._tasks.get(task_id)
        if (
            task is not None
            and task.state.is_terminal
            and self._executions.get(task_id) is execution
        ):
            self._cleanup_local(task_id)

    def _cleanup_local(self, task_id: str) -> None:
        self._executions.pop(task_id, None)
        self._progress_tasks.pop(task_id, None)
        self._cancel_events.pop(task_id, None)
        self._tasks.pop(task_id, None)
        self._task_locks.pop(task_id, None)
        self._sequences.pop(task_id, None)

    @staticmethod
    def _snapshot(task: Task) -> Task:
        return task.model_copy(deep=True)

    @staticmethod
    def _adopt_snapshot(task: Task, snapshot: Task) -> None:
        for name in Task.model_fields:
            setattr(task, name, getattr(snapshot, name))

    @staticmethod
    def _skill_name(task: Task, current_step: int) -> str:
        if current_step <= 0:
            return ""
        return task.steps[current_step - 1].skill

    @staticmethod
    def _consume_background_exception(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    def _require_running(self) -> None:
        if not self._started or self._closing or self._closed:
            raise RuntimeError("RuntimeCore is not running")
