import asyncio
from typing import Any

from robot_agent_runtime.errors import (
    AgentRuntimeError,
    ExecutionUnresponsive,
    ExecutionUnsettled,
    InternalError,
    PreconditionFailed,
    RobotStopError,
    SkillTimeout,
    TaskCancelled,
    TaskTimeout,
)
from robot_agent_runtime.models import ErrorInfo, SkillContext, Task, TaskState
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.ports import RobotStateProvider, Skill
from robot_agent_runtime.kernel.registry import SkillRegistry
from robot_agent_runtime.kernel.resources import ResourceLocks
from robot_agent_runtime.kernel.task_state import TaskStateStore


class WorkflowExecutor:
    """Runs the steps of an already validated task.

    The executor owns no state of its own: ownership and every state write live
    in :class:`TaskStateStore`. Its job is ordering — acquire resources, start
    the skill under a deadline, and map whatever comes back (result, timeout,
    cancellation, unexpected exception) onto exactly one terminal transition.

    A bounded cancellation grace cannot forcibly stop external code. When it
    expires, resources are quarantined and the task requires reconciliation;
    ending a software waiter cannot establish physical stopping.
    """

    def __init__(
        self,
        state: TaskStateStore,
        registry: SkillRegistry,
        resources: ResourceLocks,
        robot_state: RobotStateProvider | None = None,
        options: RuntimeOptions | None = None,
    ) -> None:
        self._state = state
        self._registry = registry
        self._resources = resources
        self._robot_state = robot_state
        self._options = options or RuntimeOptions()

    async def run(self, task: Task, *, deadline: float | None = None) -> None:
        cancel_event = self._state.cancel_events[task.id]
        current_step = 0
        step_results: list[dict[str, Any]] = []
        budget = asyncio.timeout_at(deadline)
        try:
            async with budget:
                if cancel_event.is_set():
                    raise TaskCancelled("Task cancelled before execution")
                await self._state.execution_transition(
                    task, "TASK_RUNNING", {"state": TaskState.RUNNING}
                )

                for index, call in enumerate(task.steps, start=1):
                    self._require_deadline(deadline, budget)
                    current_step = index
                    if cancel_event.is_set():
                        raise TaskCancelled("Task cancelled before skill execution")
                    await self._state.execution_transition(
                        task, None, {"current_step": index}
                    )
                    skill = self._registry.find(call.skill)
                    context = SkillContext(
                        task_id=task.id,
                        execution_id=task.id,
                        cancel_event=cancel_event,
                        progress=self._state.progress_callback(task.id, index),
                    )
                    resources = skill.definition.resources
                    if resources:
                        await self._state.execution_transition(
                            task, "TASK_WAITING", {"state": TaskState.WAITING}
                        )
                    self._require_deadline(deadline, budget)
                    async with self._resources.acquire(resources, cancel_event):
                        if cancel_event.is_set():
                            raise TaskCancelled("Task cancelled while waiting for resources")
                        # Read the robot only once the hardware this step needs is
                        # ours, and before SKILL_STARTED is published: a reading
                        # taken before the lock can already be stale, and no event
                        # may claim a step began that is about to be refused.
                        await self._require_preconditions(skill, index)
                        self._require_deadline(deadline, budget)
                        if cancel_event.is_set():
                            raise TaskCancelled("Task cancelled before skill dispatch")
                        if resources:
                            await self._state.execution_transition(
                                task, "TASK_RUNNING", {"state": TaskState.RUNNING}
                            )
                        await self._state.execution_transition(
                            task,
                            "SKILL_STARTED",
                            None,
                            {"step": index, "skill": call.skill},
                        )
                        self._require_deadline(deadline, budget)
                        timeout_s = (
                            call.timeout_s
                            if call.timeout_s is not None
                            else skill.definition.timeout_s
                        )
                        try:
                            result = await self._execute_with_timeout(
                                skill, context, call.params, index, timeout_s
                            )
                        except ExecutionUnresponsive:
                            self._resources.quarantine(resources)
                            raise
                        except RobotStopError as error:
                            self._resources.quarantine(resources)
                            raise ExecutionUnresponsive(
                                "Physical stop was not confirmed",
                                details={"skill": call.skill, "resources": sorted(resources)},
                            ) from error
                        # A failure only releases the hardware when its own record
                        # says nothing is still moving. Quarantine happens before
                        # the lock is released so no waiter can take it in between.
                        outcome = result.action
                        if not result.success and outcome is not None and not outcome.at_rest:
                            self._resources.quarantine(resources)
                            raise ExecutionUnsettled(
                                "Skill failed without establishing that the hardware is at rest",
                                details={
                                    "skill": call.skill,
                                    "resources": sorted(resources),
                                    "executed": outcome.executed,
                                    "stopped": outcome.stopped,
                                    "operator_required": outcome.operator_required,
                                },
                            )
                    await self._state.drain_progress(task.id)

                    if cancel_event.is_set():
                        raise TaskCancelled("Task cancelled during skill execution")
                    if not result.success:
                        error = result.error or ErrorInfo(
                            code="SKILL_EXECUTION_FAILED",
                            message="Skill returned an unsuccessful result without error details",
                        )
                        await self._state.transition(
                            task,
                            "SKILL_FAILED",
                            data={"step": index, "skill": call.skill, "error": error.to_dict()},
                        )
                        await self._state.fail_task(task, error)
                        return

                    await self._state.execution_transition(
                        task,
                        "SKILL_SUCCEEDED",
                        None,
                        {"step": index, "skill": call.skill, "output": result.output},
                    )
                    step_results.append(
                        {"step": index, "skill": call.skill, "output": result.output}
                    )
                    await self._state.execution_transition(
                        task,
                        "TASK_RESULT",
                        {"result": {"steps": step_results}},
                        {"step": index, "skill": call.skill, "output": result.output},
                    )

                await self._state.drain_progress(task.id)
                self._require_deadline(deadline, budget)
            if not await self._state.try_succeed(task):
                raise TaskCancelled("Cancellation was accepted before task success")
        except TimeoutError as cause:
            error = (TaskTimeout("Task exceeded its total deadline", {"timeout_s": task.timeout_s})
                     if budget.expired() else InternalError(
                         f"Unhandled skill exception: {cause}",
                         {"step": current_step, "skill": self._skill_name(task, current_step),
                          "exception_type": type(cause).__name__},
                     ))
            await self._state.drain_progress(task.id)
            await self._fail_skill(task, current_step, self._skill_name(task, current_step), error.info)
        except ExecutionUnresponsive as error:
            await self._state.drain_progress(task.id)
            await self._state.transition(
                task, "TASK_RECONCILIATION_REQUIRED",
                {"state": TaskState.RECONCILIATION_REQUIRED, "error": error.info},
                {"error": error.to_dict(), "step": current_step},
            )
        except TaskCancelled as error:
            await self._state.drain_progress(task.id)
            await self._state.transition(
                task,
                "SKILL_CANCELLED",
                data={
                    "step": current_step,
                    "skill": self._skill_name(task, current_step),
                    "error": error.info.to_dict(),
                },
            )
            await self._state.transition(
                task,
                "TASK_CANCELED",
                {
                    "state": TaskState.CANCELED,
                    "cancel_requested": True,
                    "error": error.info,
                },
            )
        except asyncio.CancelledError:
            await self._state.drain_progress(task.id)
            if budget.expired():
                error = TaskTimeout("Task exceeded its total deadline", {"timeout_s": task.timeout_s})
                await self._state.fail_task(task, error.info)
                return
            error = TaskCancelled("Local execution stopped by runtime shutdown")
            await self._state.transition(
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
            await self._fail_skill(
                task, current_step, mapped.info.details["skill"], mapped.info
            )

    async def _require_preconditions(self, skill: Skill, step: int) -> None:
        name = skill.definition.name
        declared = skill.definition.preconditions
        if self._robot_state is None or not declared:
            return
        blocked = declared & frozenset(await self._robot_state.unsatisfied())
        if blocked:
            raise PreconditionFailed(
                f"Robot preconditions not met for skill {name}",
                details={
                    "step": step,
                    "skill": name,
                    "unsatisfied": sorted(blocked),
                },
            )

    async def _execute_with_timeout(
        self,
        skill: Skill,
        context: SkillContext,
        params: dict[str, Any],
        step: int,
        timeout_s: float,
    ) -> Any:
        skill_task = asyncio.create_task(skill.execute(context, params))
        skill_task.add_done_callback(self._state.consume_background_exception)
        cancelled = asyncio.create_task(context.cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {skill_task, cancelled}, timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if skill_task in done:
                if context.cancel_event.is_set():
                    await self._stop_skill(skill_task, skill, step)
                    raise TaskCancelled("Skill stopped after cancellation")
                return await skill_task
            if cancelled in done:
                await self._stop_skill(skill_task, skill, step)
                raise TaskCancelled("Skill stopped after cancellation")
            context.cancel_event.set()
            await self._stop_skill(skill_task, skill, step)
            raise SkillTimeout(
                f"Skill {skill.definition.name} timed out after {timeout_s} seconds",
                details={
                    "step": step,
                    "skill": skill.definition.name,
                    "timeout_s": timeout_s,
                },
            ) from None
        except asyncio.CancelledError:
            context.cancel_event.set()
            await self._stop_skill(skill_task, skill, step)
            raise
        finally:
            cancelled.cancel()
            cleanup = asyncio.gather(cancelled, return_exceptions=True)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()

    async def _stop_skill(
        self, skill_task: asyncio.Task[Any], skill: Skill, step: int,
    ) -> None:
        confirmation = asyncio.create_task(self._confirm_stop(skill_task, skill, step))
        while not confirmation.done():
            try:
                await asyncio.shield(confirmation)
            except asyncio.CancelledError:
                continue
        confirmation.result()

    async def _confirm_stop(
        self, skill_task: asyncio.Task[Any], skill: Skill, step: int,
    ) -> None:
        finished, _ = await asyncio.wait(
            {skill_task}, timeout=self._options.cancel_grace_s,
        )
        if not finished and skill.definition.supports_cancel:
            skill_task.cancel()
            finished, _ = await asyncio.wait(
                {skill_task}, timeout=self._options.cancel_grace_s,
            )
        if finished and not skill_task.cancelled():
            error = skill_task.exception()
            if isinstance(error, ExecutionUnresponsive):
                raise error
            if isinstance(error, TaskCancelled):
                return
            if error is None and skill_task.result().success:
                return
        raise ExecutionUnresponsive(
            "Skill did not confirm termination within the cancellation grace period",
            details={
                "skill": skill.definition.name, "step": step,
                "resources": sorted(skill.definition.resources),
                "cancel_grace_s": self._options.cancel_grace_s,
            },
        )

    @staticmethod
    def _require_deadline(
        deadline: float | None, budget: asyncio.Timeout | None = None,
    ) -> None:
        if ((budget is not None and budget.expired())
                or (deadline is not None and asyncio.get_running_loop().time() >= deadline)):
            raise TaskTimeout("Task exceeded its total deadline")

    async def _fail_skill(
        self, task: Task, step: int, skill: str, error: ErrorInfo
    ) -> None:
        await self._state.transition(
            task,
            "SKILL_FAILED",
            data={"step": step, "skill": skill, "error": error.to_dict()},
        )
        await self._state.fail_task(task, error)

    @staticmethod
    def _skill_name(task: Task, current_step: int) -> str:
        if current_step <= 0:
            return ""
        return task.steps[current_step - 1].skill
