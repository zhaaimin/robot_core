import asyncio
from collections.abc import AsyncIterator, Mapping
from uuid import uuid4

from robot_agent_runtime.errors import (
    AgentRuntimeError,
    InternalError,
    SkillNotFound,
    TaskNotFound,
)
from robot_agent_runtime.events import EventBus
from robot_agent_runtime.executor import WorkflowExecutor
from robot_agent_runtime.models import (
    Event,
    SkillDefinition,
    Task,
    TaskRequest,
    TaskState,
)
from robot_agent_runtime.policy import PolicyEnforcer
from robot_agent_runtime.ports import (
    Reconciler,
    RobotStateProvider,
    Skill,
    TaskRepository,
)
from robot_agent_runtime.recovery import RecoveryOutcome, recover_nonterminal_tasks
from robot_agent_runtime.registry import SkillRegistry
from robot_agent_runtime.resources import ResourceLocks
from robot_agent_runtime.task_state import TaskStateStore


class RuntimeCore:
    """Runtime lifecycle and the public task API.

    Startup reconciles whatever the previous process left behind, submission
    validates and hands a task to :class:`WorkflowExecutor`, and shutdown
    withdraws local ownership. State writes themselves belong to
    :class:`TaskStateStore`.
    """

    def __init__(
        self,
        repository: TaskRepository,
        skills: Mapping[str, Skill],
        reconciler: Reconciler | None = None,
        allowed_permissions: set[str] | None = None,
        robot_state: RobotStateProvider | None = None,
    ) -> None:
        self._repository = repository
        self._reconciler = reconciler
        self._robot_state = robot_state
        self._registry = SkillRegistry(skills)
        self._policy = PolicyEnforcer(self._registry, allowed_permissions, robot_state)
        self._resources = ResourceLocks()
        self._events = EventBus(repository)
        self._state = TaskStateStore(repository, self._events)
        self._executor = WorkflowExecutor(
            self._state, self._registry, self._resources, robot_state
        )
        self._idempotency_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._robot_watcher: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False
        self._closed = False

    # Local ownership lives in the state store. These read-only views keep the
    # historical attribute names available for inspection and assertions.
    @property
    def _executions(self) -> dict[str, asyncio.Task[None]]:
        return self._state.executions

    @property
    def _progress_tasks(self) -> dict[str, set[asyncio.Task[None]]]:
        return self._state.progress_tasks

    @property
    def _cancel_events(self) -> dict[str, asyncio.Event]:
        return self._state.cancel_events

    @property
    def _tasks(self) -> dict[str, Task]:
        return self._state.tasks

    @property
    def _task_locks(self) -> dict[str, asyncio.Lock]:
        return self._state.task_locks

    @property
    def _sequences(self) -> dict[str, int]:
        return self._state.sequences

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
                if self._robot_state is not None:
                    self._robot_watcher = asyncio.create_task(
                        self._watch_robot_state(self._robot_state),
                        name="robot-state-watch",
                    )
            except BaseException:
                await self._cleanup_failed_start()
                raise
            self._started = True

    async def _watch_robot_state(self, provider: RobotStateProvider) -> None:
        """Interrupt in-flight work whose preconditions stopped holding.

        This is the only push path in the runtime, and it is deliberately not
        specific to any one condition: an emergency stop and a battery falling
        below its reserve arrive as the same set change and are answered the
        same way. A task is cancelled only if the skill it is running declares
        one of the now-broken labels, so an announcement keeps playing through
        an emergency stop that halts the base.

        If the provider's iterator raises, enforcement degrades to the
        per-step checkpoint in :class:`WorkflowExecutor`; nothing can start
        work the robot cannot do, but work already running is no longer
        interrupted. The exception surfaces from :meth:`close`.
        """
        async for unsatisfied in provider.watch():
            await self._cancel_blocked_tasks(frozenset(unsatisfied))

    async def _cancel_blocked_tasks(self, unsatisfied: frozenset[str]) -> None:
        if not unsatisfied:
            return
        affected: list[tuple[Task, list[str]]] = []
        for task_id, task in list(self._state.tasks.items()):
            skill = self._current_skill(task)
            if skill is None:
                continue
            blocked = sorted(skill.definition.preconditions & unsatisfied)
            if not blocked:
                continue
            cancel_event = self._state.cancel_events.get(task_id)
            if cancel_event is not None:
                # Signal first, record second: stopping the robot must not wait
                # on a database write.
                cancel_event.set()
                affected.append((task, blocked))
        for task, blocked in affected:
            try:
                await self._state.transition(
                    task,
                    "TASK_PRECONDITION_LOST",
                    data={"unsatisfied": blocked},
                    guard=lambda current: not current.state.is_terminal,
                )
            except KeyError:
                # The execution finished and dropped local ownership before the
                # reason could be appended. The cancellation itself is recorded
                # by the executor either way.
                continue

    def _current_skill(self, task: Task) -> Skill | None:
        if task.state.is_terminal or not 1 <= task.current_step <= len(task.steps):
            return None
        try:
            return self._registry.find(task.steps[task.current_step - 1].skill)
        except SkillNotFound:
            return None

    async def _apply_recovery(self, task: Task, outcome: RecoveryOutcome) -> None:
        async with self._state.recovery_ownership(task.id):
            await self._state.transition(
                task, outcome.event_type, outcome.changes, outcome.data
            )

    async def _cleanup_failed_start(self) -> None:
        watcher = self._robot_watcher
        self._robot_watcher = None
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
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
            self._state.task_locks.clear()
            self._state.sequences.clear()
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
            self._state.begin_shutdown()
            watcher = self._robot_watcher
            self._robot_watcher = None
            if watcher is not None and not watcher.done():
                watcher.cancel()
            executions = list(self._state.executions.values())
            for execution in executions:
                if not execution.done():
                    execution.cancel()

        results = await asyncio.gather(*executions, return_exceptions=True)
        if watcher is not None:
            results.extend(await asyncio.gather(watcher, return_exceptions=True))
        remaining_progress = self._state.pending_progress_tasks()
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
                self._state.clear()
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
            self._state.register(task)

        await self._state.transition(task, "TASK_CREATED")
        await self._state.transition(
            task, "TASK_VALIDATING", {"state": TaskState.VALIDATING}
        )
        try:
            # One reading for the whole task, so every step is judged against
            # the same robot snapshot. A provider that raises fails the
            # submission rather than admitting work blindly.
            unsatisfied = await self._policy.unsatisfied()
            for call in request.steps:
                self._policy.validate(call, unsatisfied)
        except AgentRuntimeError as error:
            await self._state.fail_task(task, error.info)
            self._state.cleanup(task.id)
            return self._state.snapshot(task)
        except Exception as error:
            mapped = InternalError(
                f"Task validation failed: {error}",
                details={"exception_type": type(error).__name__},
            )
            await self._state.fail_task(task, mapped.info)
            self._state.cleanup(task.id)
            return self._state.snapshot(task)

        await self._state.transition(task, "TASK_QUEUED", {"state": TaskState.QUEUED})
        execution = asyncio.create_task(
            self._executor.run(task), name=f"robot-task-{task.id}"
        )
        self._state.register_execution(task.id, execution)
        return self._state.snapshot(task)

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
            task = self._state.tasks.get(task_id)
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

            execution = self._state.executions.get(task_id)
            await self._state.request_cancel(task)
            if task.state.is_terminal:
                return await self._get_task(task_id)
            cancel_event = self._state.cancel_events[task_id]
            cancel_event.set()

        if execution is not None and execution is not asyncio.current_task():
            await asyncio.shield(execution)
        return await self._get_task(task_id)

    def subscribe_task_events(
        self, task_id: str, after_sequence: int = 0
    ) -> AsyncIterator[Event]:
        return self._events.subscribe(task_id, after_sequence)

    def _require_running(self) -> None:
        if not self._started or self._closing or self._closed:
            raise RuntimeError("RuntimeCore is not running")
