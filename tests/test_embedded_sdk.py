import asyncio
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC
from typing import Any

import pytest

from robot_agent_runtime.adapters.simulator import SimulatedRobotState, Simulator, SimulatorReconciler
from robot_agent_runtime.errors import ListenerCapacityExceeded, TaskNotFound
from robot_agent_runtime.models import (
    Event, SkillCall, SkillContext, SkillDefinition, SkillResult, Task, TaskRequest, TaskState,
)
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.ports import Reconciler, RobotPlatform, RobotStateProvider, Skill
from robot_agent_runtime.thread_binding import ThreadBinding


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


async def test_from_platform_uses_skills_state_reconciler_permissions_and_options() -> None:
    class Platform:
        def __init__(self) -> None:
            self.skills = Simulator().skills
            self.robot_state = SimulatedRobotState({"estop.clear"})
            self.reconciler = SimulatorReconciler({"previous": TaskState.SUCCEEDED})

        async def start(self) -> None:
            raise AssertionError("The application owns platform startup")

        async def close(self) -> None:
            raise AssertionError("The application owns platform shutdown")

    repository = InMemoryTaskRepository()
    await repository.create_task(Task(
        id="previous", task_type="recovery", state=TaskState.RUNNING,
        steps=[SkillCall(skill="navigation.navigate_to", params={"target": "dock"})],
    ))
    platform = Platform()
    runtime = RobotRuntime.from_platform(
        repository, platform, allowed_permissions={"navigation"},
        options=RuntimeOptions(max_listener_threads=1),
    )
    try:
        await runtime.start()
        assert {skill.name for skill in await runtime.list_skills()} == set(platform.skills)
        assert (await runtime.get_task_status("previous")).state == TaskState.SUCCEEDED
        blocked = await runtime.submit_task(TaskRequest(task_type="blocked", steps=[
            SkillCall(skill="navigation.navigate_to", params={"target": "dock"}),
        ]))
        assert blocked.error is not None and blocked.error.code == "PRECONDITION_FAILED"
        denied = await runtime.submit_task(TaskRequest(task_type="denied", steps=[
            SkillCall(skill="audio.speak", params={"message": "hello"}),
        ]))
        assert denied.error is not None and denied.error.code == "PERMISSION_DENIED"
        listener = runtime.add_task_listener(on_done=lambda task: None, execution="dedicated")
        with pytest.raises(ListenerCapacityExceeded):
            runtime.add_task_listener(on_done=lambda task: None, execution="dedicated")
        await listener.close()
    finally:
        await runtime.close()


async def test_from_platform_preserves_bound_state_and_reconciler_thread_group() -> None:
    owners: dict[str, tuple[int, str]] = {}

    class State:
        async def unsatisfied(self) -> frozenset[str]:
            owners["state"] = (threading.get_ident(), threading.current_thread().name)
            return frozenset({"estop.clear"})

        async def watch(self) -> AsyncIterator[frozenset[str]]:
            if False:
                yield frozenset()

    class Authority:
        async def reconcile(self, task: Task) -> TaskState | None:
            owners["reconciler"] = (threading.get_ident(), threading.current_thread().name)
            return TaskState.SUCCEEDED

    class Platform:
        def __init__(self) -> None:
            self.skills: Mapping[str, Skill | ThreadBinding[Skill]] = {
                "navigation.navigate_to": ThreadBinding.dedicated(Simulator().skills["navigation.navigate_to"]),
            }
            self.robot_state: RobotStateProvider | ThreadBinding[RobotStateProvider] | None = (
                ThreadBinding.group(State(), "state-control")
            )
            self.reconciler: Reconciler | ThreadBinding[Reconciler] | None = (
                ThreadBinding.group(Authority(), "state-control")
            )

        async def start(self) -> None:
            pass

        async def close(self) -> None:
            pass

    repository = InMemoryTaskRepository()
    await repository.create_task(Task(
        id="previous", task_type="recovery", state=TaskState.RUNNING,
        steps=[SkillCall(skill="navigation.navigate_to", params={"target": "dock"})],
    ))
    platform: RobotPlatform = Platform()
    runtime = RobotRuntime.from_platform(
        repository, platform, options=RuntimeOptions(max_extension_threads=4),
    )
    try:
        await runtime.start()
        assert (await runtime.get_task_status("previous")).state == TaskState.SUCCEEDED
        blocked = await runtime.submit_task(TaskRequest(task_type="blocked", steps=[
            SkillCall(skill="navigation.navigate_to", params={"target": "dock"}),
        ]))
        assert blocked.error is not None and blocked.error.code == "PRECONDITION_FAILED"
        assert owners["state"] == owners["reconciler"]
        assert owners["state"][1] == "robot-extension-group-state-control"
        assert owners["state"][0] != threading.get_ident()
    finally:
        await runtime.close()


async def test_from_platform_keeps_blocking_within_the_configured_skill_group() -> None:
    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    peer_calls: list[bool] = []

    class Probe:
        def __init__(self, name: str) -> None:
            self.definition = SkillDefinition(
                name=name, version="1", input_schema={"type": "object"}, resources={name},
                permissions=set(), preconditions=set(), timeout_s=5, supports_cancel=False,
            )

        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            if self.definition.name == "block":
                caller.call_soon_threadsafe(entered.set)
                assert release.wait(3), "Test did not release the grouped skill"
            if self.definition.name == "peer":
                peer_calls.append(True)
            return SkillResult(success=True)

    class Platform:
        def __init__(self) -> None:
            self.skills: Mapping[str, Skill | ThreadBinding[Skill]] = {
                "block": ThreadBinding.group(Probe("block"), "audio"),
                "peer": ThreadBinding.group(Probe("peer"), "audio"),
                "other": ThreadBinding.dedicated(Probe("other")),
            }
            self.robot_state: RobotStateProvider | ThreadBinding[RobotStateProvider] | None = None
            self.reconciler: Reconciler | ThreadBinding[Reconciler] | None = None

        async def start(self) -> None:
            pass

        async def close(self) -> None:
            pass

    platform: RobotPlatform = Platform()
    runtime = RobotRuntime.from_platform(
        InMemoryTaskRepository(), platform,
        options=RuntimeOptions(max_extension_threads=4),
    )
    await runtime.start()
    try:
        blocking = await runtime.submit_task(TaskRequest(task_type="block", steps=[SkillCall(skill="block")]))
        await asyncio.wait_for(entered.wait(), 1)
        peer = await runtime.submit_task(TaskRequest(task_type="peer", steps=[
            SkillCall(skill="peer", timeout_s=0.03),
        ]))
        other = await runtime.submit_task(TaskRequest(task_type="other", steps=[SkillCall(skill="other")]))
        assert (await wait_for_terminal(runtime, other.id)).state == TaskState.SUCCEEDED
        peer_final = await wait_for_terminal(runtime, peer.id)
        assert peer_final.state == TaskState.FAILED
        assert peer_final.error is not None and peer_final.error.code == "SKILL_TIMEOUT"
        assert not peer_calls
        release.set()
        assert (await wait_for_terminal(runtime, blocking.id)).state == TaskState.SUCCEEDED
    finally:
        release.set()
        await runtime.close()
