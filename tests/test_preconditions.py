import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from robot_agent_runtime.adapters.simulator import (
    SimulatedRobot,
    SimulatedRobotState,
    Simulator,
)
from robot_agent_runtime.core import RuntimeCore
from robot_agent_runtime.models import (
    SkillCall,
    SkillContext,
    SkillDefinition,
    SkillResult,
    Task,
    TaskRequest,
    TaskState,
)
from robot_agent_runtime.ports import Skill
from robot_agent_runtime.repository import InMemoryTaskRepository


async def wait_for_terminal(core: RuntimeCore, task_id: str) -> Task:
    async with asyncio.timeout(1):
        while True:
            task = await core.get_task_status(task_id)
            if task.state.is_terminal:
                return task
            await asyncio.sleep(0.001)


async def wait_until_active(skill: Any) -> None:
    async with asyncio.timeout(1):
        while skill.active != 1:
            await asyncio.sleep(0)


@asynccontextmanager
async def running_core(
    robot_state: Any = None,
    *,
    delay_s: float = 0.0,
    extra_skills: dict[str, Skill] | None = None,
) -> AsyncIterator[tuple[RuntimeCore, Simulator]]:
    sim = Simulator(delay_s=delay_s)
    core = RuntimeCore(
        InMemoryTaskRepository(),
        {**sim.skills, **(extra_skills or {})},
        robot_state=robot_state,
    )
    await core.start()
    try:
        yield core, sim
    finally:
        await core.close()


class FlipSkill:
    """Changes the robot state from inside a running task."""

    def __init__(self, robot_state: SimulatedRobotState, unsatisfied: set[str]) -> None:
        self.definition = SkillDefinition(
            name="test.flip",
            version="1.0.0",
            input_schema={"type": "object", "additionalProperties": False},
        )
        self._robot_state = robot_state
        self._unsatisfied = unsatisfied

    async def execute(
        self, context: SkillContext, params: dict[str, Any]
    ) -> SkillResult:
        self._robot_state.set_unsatisfied(self._unsatisfied)
        return SkillResult(success=True)


class RaisingWatchState:
    """A provider whose push channel is broken but whose polling works."""

    def __init__(self, unsatisfied: set[str]) -> None:
        self._unsatisfied = frozenset(unsatisfied)

    async def unsatisfied(self) -> frozenset[str]:
        return self._unsatisfied

    async def watch(self) -> AsyncIterator[frozenset[str]]:
        raise RuntimeError("broken push channel")
        yield frozenset()  # pragma: no cover - unreachable, keeps this a generator


class PollOnlyState:
    """A provider with no push channel at all."""

    def __init__(self, unsatisfied: set[str]) -> None:
        self._unsatisfied = frozenset(unsatisfied)

    async def unsatisfied(self) -> frozenset[str]:
        return self._unsatisfied

    async def watch(self) -> AsyncIterator[frozenset[str]]:
        return
        yield frozenset()  # pragma: no cover - unreachable, keeps this a generator


async def test_admission_refuses_task_whose_step_declares_a_broken_condition() -> None:
    robot_state = SimulatedRobotState({"estop.clear"})
    async with running_core(robot_state) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"}),
                    SkillCall(skill="audio.speak", params={"message": "arrived"}),
                ],
            )
        )

        assert submitted.state == TaskState.FAILED
        assert submitted.error is not None
        assert submitted.error.code == "PRECONDITION_FAILED"
        assert submitted.error.details["skill"] == "navigation.navigate_to"
        assert submitted.error.details["unsatisfied"] == ["estop.clear"]
        # All or nothing: no step of a refused task may touch the robot.
        assert submitted.current_step == 0
        assert sim.skills["navigation.navigate_to"].calls == 0
        assert sim.skills["audio.speak"].calls == 0


async def test_admission_reports_every_broken_condition_a_step_declares() -> None:
    robot_state = SimulatedRobotState({"estop.clear", "battery.sufficient", "unrelated"})
    async with running_core(robot_state) as (core, _):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"})
                ],
            )
        )

        assert submitted.error is not None
        assert submitted.error.details["unsatisfied"] == [
            "battery.sufficient",
            "estop.clear",
        ]


async def test_admission_ignores_conditions_no_step_declares() -> None:
    """An emergency stop must not block work that does not move the robot."""
    robot_state = SimulatedRobotState({"estop.clear"})
    async with running_core(robot_state) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="announcement",
                steps=[SkillCall(skill="audio.speak", params={"message": "hello"})],
            )
        )
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.SUCCEEDED
        assert sim.skills["audio.speak"].calls == 1


async def test_without_a_provider_declared_preconditions_are_not_checked() -> None:
    async with running_core(None) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"})
                ],
            )
        )
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.SUCCEEDED
        assert sim.skills["navigation.navigate_to"].calls == 1


async def test_step_gate_refuses_a_later_step_when_conditions_break_after_admission() -> None:
    robot_state = SimulatedRobotState()
    flip = FlipSkill(robot_state, {"battery.sufficient"})
    async with running_core(robot_state, extra_skills={"test.flip": flip}) as (
        core,
        sim,
    ):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="test.flip"),
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"}),
                ],
            )
        )
        assert submitted.state == TaskState.QUEUED
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.FAILED
        assert final.error is not None
        assert final.error.code == "PRECONDITION_FAILED"
        assert final.error.details["step"] == 2
        assert final.error.details["unsatisfied"] == ["battery.sufficient"]
        # The step was refused before it could start.
        assert sim.skills["navigation.navigate_to"].calls == 0


async def test_step_gate_runs_before_skill_started_is_published() -> None:
    robot_state = SimulatedRobotState()
    flip = FlipSkill(robot_state, {"estop.clear"})
    async with running_core(robot_state, extra_skills={"test.flip": flip}) as (core, _):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="test.flip"),
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"}),
                ],
            )
        )
        await wait_for_terminal(core, submitted.id)

        started = [
            event.data["step"]
            async for event in core.subscribe_task_events(submitted.id)
            if event.event_type == "SKILL_STARTED"
        ]
        assert started == [1]


async def test_push_cancels_a_running_task_that_declares_the_broken_condition() -> None:
    robot_state = SimulatedRobotState()
    async with running_core(robot_state, delay_s=5) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"})
                ],
            )
        )
        await wait_until_active(sim.skills["navigation.navigate_to"])

        robot_state.set_unsatisfied({"estop.clear"})
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.CANCELED


async def test_push_records_why_the_task_was_cancelled() -> None:
    robot_state = SimulatedRobotState()
    async with running_core(robot_state, delay_s=5) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"})
                ],
            )
        )
        await wait_until_active(sim.skills["navigation.navigate_to"])

        robot_state.set_unsatisfied({"estop.clear", "battery.sufficient"})
        await wait_for_terminal(core, submitted.id)

        reasons = [
            event.data["unsatisfied"]
            async for event in core.subscribe_task_events(submitted.id)
            if event.event_type == "TASK_PRECONDITION_LOST"
        ]
        assert reasons == [["battery.sufficient", "estop.clear"]]


async def test_push_leaves_a_running_task_that_declares_nothing() -> None:
    """The headline property: an announcement plays through an emergency stop."""
    robot_state = SimulatedRobotState()
    async with running_core(robot_state, delay_s=0.05) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="announcement",
                steps=[SkillCall(skill="audio.speak", params={"message": "hello"})],
            )
        )
        await wait_until_active(sim.skills["audio.speak"])

        robot_state.set_unsatisfied({"estop.clear"})
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.SUCCEEDED
        assert sim.skills["audio.speak"].calls == 1


async def test_push_ignores_a_condition_no_running_skill_declares() -> None:
    robot_state = SimulatedRobotState()
    async with running_core(robot_state, delay_s=0.05) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"})
                ],
            )
        )
        await wait_until_active(sim.skills["navigation.navigate_to"])

        robot_state.set_unsatisfied({"gripper.calibrated"})
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.SUCCEEDED


async def test_a_provider_without_a_push_channel_still_gates_admission() -> None:
    async with running_core(PollOnlyState({"estop.clear"})) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"})
                ],
            )
        )

        assert submitted.state == TaskState.FAILED
        assert submitted.error is not None
        assert submitted.error.code == "PRECONDITION_FAILED"
        assert sim.skills["navigation.navigate_to"].calls == 0


async def test_a_broken_push_channel_still_gates_and_surfaces_at_close() -> None:
    sim = Simulator()
    core = RuntimeCore(
        InMemoryTaskRepository(),
        sim.skills,
        robot_state=RaisingWatchState({"estop.clear"}),
    )
    await core.start()
    try:
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"})
                ],
            )
        )
        assert submitted.state == TaskState.FAILED
        assert sim.skills["navigation.navigate_to"].calls == 0
    finally:
        with pytest.raises(ExceptionGroup) as caught:
            await core.close()
    assert any(
        isinstance(error, RuntimeError) and "broken push channel" in str(error)
        for error in caught.value.exceptions
    )


async def test_a_provider_that_raises_while_polling_fails_the_submission() -> None:
    class BrokenPoll:
        async def unsatisfied(self) -> frozenset[str]:
            raise RuntimeError("cannot read robot state")

        async def watch(self) -> AsyncIterator[frozenset[str]]:
            return
            yield frozenset()  # pragma: no cover - keeps this a generator

    async with running_core(BrokenPoll()) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"})
                ],
            )
        )

        # Fail closed: an unreadable robot does not mean an available robot.
        assert submitted.state == TaskState.FAILED
        assert submitted.error is not None
        assert submitted.error.code == "INTERNAL_ERROR"
        assert sim.skills["navigation.navigate_to"].calls == 0


async def test_simulated_robot_derives_labels_from_its_opening_readings() -> None:
    robot = SimulatedRobot(voltage=20.0, estop_engaged=True)

    assert await robot.unsatisfied() == frozenset(
        {"estop.clear", "battery.sufficient"}
    )

    robot = SimulatedRobot(voltage=24.0)
    assert await robot.unsatisfied() == frozenset()


async def test_simulated_robot_holds_the_battery_label_until_recovery_voltage() -> None:
    robot = SimulatedRobot(voltage=24.0, low_voltage=22.5, recovery_voltage=23.5)

    robot.set_voltage(22.4)
    assert await robot.unsatisfied() == frozenset({"battery.sufficient"})

    # Back above the low threshold but still inside the hysteresis band: a
    # battery resting on 22.5V must not flap the label once per reading.
    robot.set_voltage(22.6)
    assert await robot.unsatisfied() == frozenset({"battery.sufficient"})

    robot.set_voltage(23.6)
    assert await robot.unsatisfied() == frozenset()


async def test_simulated_robot_pushes_only_when_the_label_set_changes() -> None:
    robot = SimulatedRobot(voltage=24.0, low_voltage=22.5, recovery_voltage=23.5)
    received: list[frozenset[str]] = []

    async def collect() -> None:
        async for unsatisfied in robot.watch():
            received.append(unsatisfied)

    watcher = asyncio.create_task(collect())
    await asyncio.sleep(0)
    try:
        robot.set_voltage(23.9)  # still sufficient
        robot.set_voltage(23.8)  # still sufficient
        robot.set_voltage(22.0)  # breaks the label
        robot.set_voltage(21.0)  # already broken
        async with asyncio.timeout(1):
            while len(received) < 1:
                await asyncio.sleep(0)
        await asyncio.sleep(0.01)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)

    assert received == [frozenset({"battery.sufficient"})]


async def test_simulated_robot_rejects_a_hysteresis_band_that_cannot_recover() -> None:
    with pytest.raises(ValueError, match="recovery_voltage"):
        SimulatedRobot(low_voltage=23.5, recovery_voltage=22.5)


async def test_a_draining_simulated_robot_interrupts_a_running_navigation() -> None:
    robot = SimulatedRobot(voltage=23.0, low_voltage=22.5, recovery_voltage=23.5)
    async with running_core(robot, delay_s=1.0) as (core, sim):
        task = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"})
                ],
            )
        )
        await wait_until_active(sim.skills["navigation.navigate_to"])

        drain = asyncio.create_task(
            robot.drain(volts_per_second=10.0, interval_s=0.005, floor=22.0)
        )
        try:
            final = await wait_for_terminal(core, task.id)
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

        assert final.state == TaskState.CANCELED
        reasons = [
            event.data["unsatisfied"]
            async for event in core.subscribe_task_events(task.id)
            if event.event_type == "TASK_PRECONDITION_LOST"
        ]
        assert reasons == [["battery.sufficient"]]
