import asyncio
from typing import Any

from robot_agent_runtime.errors import (
    AgentRuntimeError,
    InternalError,
    PreconditionFailed,
    SkillTimeout,
    TaskCancelled,
)
from robot_agent_runtime.models import ErrorInfo, SkillContext, Task, TaskState
from robot_agent_runtime.ports import RobotStateProvider, Skill
from robot_agent_runtime.registry import SkillRegistry
from robot_agent_runtime.resources import ResourceLocks
from robot_agent_runtime.task_state import TaskStateStore


class WorkflowExecutor:
    """Runs the steps of an already validated task.

    The executor owns no state of its own: ownership and every state write live
    in :class:`TaskStateStore`. Its job is ordering — acquire resources, start
    the skill under a deadline, and map whatever comes back (result, timeout,
    cancellation, unexpected exception) onto exactly one terminal transition.
    """

    def __init__(
        self,
        state: TaskStateStore,
        registry: SkillRegistry,
        resources: ResourceLocks,
        robot_state: RobotStateProvider | None = None,
    ) -> None:
        self._state = state
        self._registry = registry
        self._resources = resources
        self._robot_state = robot_state

    async def run(self, task: Task) -> None:
        cancel_event = self._state.cancel_events[task.id]
        current_step = 0
        step_results: list[dict[str, Any]] = []
        try:
            if cancel_event.is_set():
                raise TaskCancelled("Task cancelled before execution")
            await self._state.execution_transition(
                task, "TASK_RUNNING", {"state": TaskState.RUNNING}
            )

            for index, call in enumerate(task.steps, start=1):
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
                async with self._resources.acquire(resources, cancel_event):
                    if cancel_event.is_set():
                        raise TaskCancelled("Task cancelled while waiting for resources")
                    # Read the robot only once the hardware this step needs is
                    # ours, and before SKILL_STARTED is published: a reading
                    # taken before the lock can already be stale, and no event
                    # may claim a step began that is about to be refused.
                    await self._require_preconditions(skill, index)
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
                    timeout_s = (
                        call.timeout_s
                        if call.timeout_s is not None
                        else skill.definition.timeout_s
                    )
                    result = await self._execute_with_timeout(
                        skill, context, call.params, index, timeout_s
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
            if not await self._state.try_succeed(task):
                raise TaskCancelled("Cancellation was accepted before task success")
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
