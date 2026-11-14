import asyncio
from typing import Any

from robot_agent_runtime.kernel.core import RuntimeCore
from robot_agent_runtime.models import (
    ActionOutcome,
    ErrorInfo,
    SkillCall,
    SkillDefinition,
    SkillResult,
    Task,
    TaskRequest,
    TaskState,
)
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository


class FailingSkill:
    """A skill that fails and reports what its own call established."""

    def __init__(self, action: ActionOutcome | None, *, resources: set[str] | None = None) -> None:
        self.definition = SkillDefinition(
            name="test.failing",
            version="1.0.0",
            input_schema={"type": "object"},
            resources=resources or set(),
        )
        self._action = action
        self.calls = 0

    async def execute(self, context: Any, params: dict[str, Any]) -> SkillResult:
        self.calls += 1
        return SkillResult(
            success=False,
            error=ErrorInfo(code="TEST_FAILED", message="call failed"),
            action=self._action,
        )


async def terminal(core: RuntimeCore, task_id: str) -> Task:
    async with asyncio.timeout(1):
        while True:
            task = await core.get_task_status(task_id)
            if task.state.is_terminal:
                return task
            await asyncio.sleep(0.001)


async def run_twice(skill: FailingSkill) -> tuple[Task, Task, int]:
    """Run the same step twice; the second outcome shows whether the lock was freed."""
    core = RuntimeCore(InMemoryTaskRepository(), {skill.definition.name: skill})
    await core.start()
    try:
        first = await core.submit_task(TaskRequest(
            task_type="test", steps=[SkillCall(skill=skill.definition.name)],
        ))
        settled = await terminal(core, first.id)
        second = await core.submit_task(TaskRequest(
            task_type="test", steps=[SkillCall(skill=skill.definition.name)],
        ))
        again = await terminal(core, second.id)
        return settled, again, skill.calls
    finally:
        await core.close()


def test_a_record_is_at_rest_only_when_it_proves_it() -> None:
    assert ActionOutcome(executed="none").at_rest
    assert ActionOutcome(executed="some", stopped=True).at_rest
    assert not ActionOutcome(executed="some", stopped=False).at_rest
    assert not ActionOutcome(executed="some", stopped=None).at_rest
    assert not ActionOutcome(executed="unknown").at_rest
    assert not ActionOutcome(executed="none", operator_required=True).at_rest


async def test_a_failure_that_may_still_be_moving_quarantines_resources() -> None:
    skill = FailingSkill(ActionOutcome(executed="some", stopped=False), resources={"base"})

    settled, again, calls = await run_twice(skill)

    assert settled.state == TaskState.RECONCILIATION_REQUIRED
    assert settled.error is not None and settled.error.code == "EXECUTION_UNSETTLED"
    assert again.state == TaskState.FAILED
    assert again.error is not None and again.error.code == "RESOURCE_QUARANTINED"
    assert calls == 1


async def test_a_failure_with_an_unknown_stop_quarantines_resources() -> None:
    skill = FailingSkill(ActionOutcome(executed="some"), resources={"base"})

    settled, again, calls = await run_twice(skill)

    assert settled.state == TaskState.RECONCILIATION_REQUIRED
    assert again.error is not None and again.error.code == "RESOURCE_QUARANTINED"
    assert calls == 1


async def test_a_failure_that_needs_a_person_quarantines_even_without_motion() -> None:
    skill = FailingSkill(
        ActionOutcome(executed="none", stopped=True, operator_required=True), resources={"base"},
    )

    settled, again, calls = await run_twice(skill)

    assert settled.state == TaskState.RECONCILIATION_REQUIRED
    assert again.error is not None and again.error.code == "RESOURCE_QUARANTINED"
    assert calls == 1


async def test_a_failure_that_moved_and_confirmed_stopping_releases_resources() -> None:
    skill = FailingSkill(ActionOutcome(executed="some", stopped=True), resources={"base"})

    settled, again, calls = await run_twice(skill)

    assert settled.state == TaskState.FAILED
    assert again.state == TaskState.FAILED
    assert calls == 2


async def test_a_failure_that_executed_nothing_releases_resources() -> None:
    skill = FailingSkill(ActionOutcome(executed="none"), resources={"base"})

    settled, again, calls = await run_twice(skill)

    assert settled.state == TaskState.FAILED
    assert again.state == TaskState.FAILED
    assert calls == 2


async def test_an_adapter_that_reports_no_action_keeps_the_previous_behaviour() -> None:
    skill = FailingSkill(None, resources={"base"})

    settled, again, calls = await run_twice(skill)

    assert settled.state == TaskState.FAILED
    assert again.state == TaskState.FAILED
    assert calls == 2
