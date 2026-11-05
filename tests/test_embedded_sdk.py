import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import TaskNotFound
from robot_agent_runtime.models import Event, SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def wait_for_terminal(runtime: RobotRuntime, task_id: str) -> Task:
    async with asyncio.timeout(1):
        while True:
            task = await runtime.get_task_status(task_id)
            if task.state.is_terminal:
                return task
            await asyncio.sleep(0)


async def collect_through_terminal(runtime: RobotRuntime, task_id: str) -> list[Event]:
    events: list[Event] = []
    async with asyncio.timeout(1):
        async for event in runtime.subscribe_task_events(task_id):
            events.append(event)
    return events


@asynccontextmanager
async def running_runtime() -> AsyncIterator[tuple[RobotRuntime, Simulator]]:
    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        yield runtime, simulator
    finally:
        await runtime.close()


async def test_embedded_full_api_uses_injected_core_components() -> None:
    async with running_runtime() as (runtime, simulator):
        skills = await runtime.list_skills()
        assert {skill.name for skill in skills} == set(simulator.skills)

        submitted = await runtime.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"}),
                    SkillCall(skill="audio.speak", params={"message": "arrived"}),
                ],
            )
        )
        final = await wait_for_terminal(runtime, submitted.id)
        assert final.state == TaskState.SUCCEEDED
        assert final.created_at.tzinfo == UTC
        assert final.result is not None
        assert final.result["steps"][0]["output"]["target"] == "dock"

        events = await collect_through_terminal(runtime, submitted.id)
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert events[0].event_type == "TASK_CREATED"
        assert events[-1].event_type == "TASK_SUCCEEDED"


async def test_embedded_completed_task_replay_ends_naturally_after_terminal_event() -> None:
    async with running_runtime() as (runtime, _):
        submitted = await runtime.submit_task(
            TaskRequest(task_type="state", steps=[SkillCall(skill="robot.get_state")])
        )
        await wait_for_terminal(runtime, submitted.id)

        events: list[Event] = []
        async with asyncio.timeout(1):
            async for event in runtime.subscribe_task_events(submitted.id):
                events.append(event)

        assert events[-1].event_type == "TASK_SUCCEEDED"
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))


async def test_embedded_replay_honors_after_sequence() -> None:
    async with running_runtime() as (runtime, _):
        submitted = await runtime.submit_task(
            TaskRequest(task_type="state", steps=[SkillCall(skill="robot.get_state")])
        )
        await wait_for_terminal(runtime, submitted.id)
        subscription = runtime.subscribe_task_events(submitted.id, after_sequence=3)
        first = await asyncio.wait_for(anext(subscription), timeout=1)
        assert first.sequence == 4
        await subscription.aclose()


async def test_embedded_cancel_and_not_found_match_core() -> None:
    async with running_runtime() as (runtime, _):
        submitted = await runtime.submit_task(
            TaskRequest(
                task_type="wait",
                steps=[SkillCall(skill="wait.delay", params={"delay_s": 30})],
            )
        )
        cancelled = await runtime.cancel_task(submitted.id)
        assert cancelled.state == TaskState.CANCELED
        assert cancelled.cancel_requested

        with pytest.raises(TaskNotFound) as caught:
            await runtime.get_task_status("missing")
        assert caught.value.info.code == "TASK_NOT_FOUND"
        assert caught.value.info.details == {"task_id": "missing"}


async def test_embedded_lifecycle_is_idempotent_and_delegates_close_once() -> None:
    class TrackingRepository(InMemoryTaskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.starts = 0
            self.closes = 0

        async def start(self) -> None:
            self.starts += 1

        async def close(self) -> None:
            self.closes += 1

    repository = TrackingRepository()
    runtime = RobotRuntime(repository, Simulator().skills)
    await runtime.start()
    await runtime.start()
    await runtime.close()
    await runtime.close()
    assert (repository.starts, repository.closes) == (1, 1)


async def test_embedded_subscription_close_finishes_pending_iterator() -> None:
    async with running_runtime() as (runtime, _):
        submitted = await runtime.submit_task(
            TaskRequest(
                task_type="wait",
                steps=[SkillCall(skill="wait.delay", params={"delay_s": 30})],
            )
        )
        subscription = runtime.subscribe_task_events(submitted.id)
        assert (await anext(subscription)).event_type == "TASK_CREATED"
        await runtime.close()
        async with asyncio.timeout(1):
            with pytest.raises(StopAsyncIteration):
                while True:
                    await anext(subscription)
