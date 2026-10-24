import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.core import RuntimeCore
from robot_agent_runtime.errors import AgentRuntimeError
from robot_agent_runtime.events import EventBus
from robot_agent_runtime.models import (
    Event,
    SkillCall,
    SkillDefinition,
    SkillResult,
    Task,
    TaskRequest,
    TaskState,
)
from robot_agent_runtime.ports import TaskRepository
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository


async def wait_for_event(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


async def wait_for_terminal(core: RuntimeCore, task_id: str) -> Task:
    async with asyncio.timeout(1):
        while True:
            task = await core.get_task_status(task_id)
            if task.state.is_terminal:
                return task
            await asyncio.sleep(0.001)


async def collect_events(core: RuntimeCore, task_id: str) -> list[Event]:
    received: list[Event] = []
    async for event in core.subscribe_task_events(task_id):
        received.append(event)
    return received


@asynccontextmanager
async def running_core(
    repository: TaskRepository | None = None,
    *,
    simulator: Simulator | None = None,
    allowed_permissions: set[str] | None = None,
) -> AsyncIterator[tuple[RuntimeCore, Simulator]]:
    sim = simulator or Simulator()
    core = RuntimeCore(
        repository or InMemoryTaskRepository(),
        sim.skills,
        allowed_permissions=allowed_permissions,
    )
    await core.start()
    try:
        yield core, sim
    finally:
        await core.close()


@pytest.mark.parametrize("repository_kind", ["memory", "sqlite"])
async def test_multi_step_success_persists_results_and_ordered_events(
    repository_kind: str, tmp_path: Path
) -> None:
    repository: TaskRepository
    if repository_kind == "sqlite":
        repository = SQLiteTaskRepository(str(tmp_path / "core.db"))
    else:
        repository = InMemoryTaskRepository()

    async with running_core(repository) as (core, sim):
        definitions = await core.list_skills()
        assert {definition.name for definition in definitions} >= {
            "navigation.navigate_to",
            "audio.speak",
            "robot.get_state",
            "wait.delay",
        }
        submitted = await core.submit_task(
            TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"}),
                    SkillCall(skill="audio.speak", params={"message": "arrived"}),
                    SkillCall(skill="robot.get_state"),
                ],
            )
        )
        assert submitted.id
        assert submitted.state in {TaskState.CREATED, TaskState.VALIDATING, TaskState.QUEUED}

        final = await wait_for_terminal(core, submitted.id)
        assert final.state == TaskState.SUCCEEDED
        assert final.current_step == 3
        assert final.result is not None
        assert final.result["steps"][0]["output"]["target"] == "dock"
        assert final.result["steps"][2]["output"]["state"]["position"] == "dock"
        assert sim.skills["navigation.navigate_to"].calls == 1
        assert sim.skills["audio.speak"].calls == 1

        events = await repository.list_events(submitted.id)
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert len({event.event_id for event in events}) == len(events)
        assert [event.event_type for event in events[:4]] == [
            "TASK_CREATED",
            "TASK_VALIDATING",
            "TASK_QUEUED",
            "TASK_RUNNING",
        ]
        assert events[-1].event_type == "TASK_SUCCEEDED"
        assert "TASK_RESULT" in {event.event_type for event in events}
        assert sum(event.event_type == "SKILL_STARTED" for event in events) == 3
        assert sum(event.event_type == "SKILL_SUCCEEDED" for event in events) == 3
        assert {event.event_type for event in events} >= {"SKILL_PROGRESS"}


async def test_idempotent_submission_returns_original_without_reexecution() -> None:
    async with running_core() as (core, sim):
        request = TaskRequest(
            task_type="speak",
            idempotency_key="same-request",
            steps=[SkillCall(skill="audio.speak", params={"message": "once"})],
        )
        first = await core.submit_task(request)
        second = await core.submit_task(request)

        assert second.id == first.id
        await wait_for_terminal(core, first.id)
        assert sim.skills["audio.speak"].calls == 1
        events = await collect_events(core, first.id)
        assert sum(event.event_type == "SKILL_STARTED" for event in events) == 1


@pytest.mark.parametrize(
    ("step", "allowed", "error_code"),
    [
        (SkillCall(skill="missing.skill"), None, "SKILL_NOT_FOUND"),
        (
            SkillCall(skill="navigation.navigate_to", params={"target": 3}),
            None,
            "SCHEMA_VALIDATION_FAILED",
        ),
        (
            SkillCall(skill="navigation.navigate_to", params={"target": "dock"}),
            set(),
            "PERMISSION_DENIED",
        ),
    ],
)
async def test_validation_rejects_before_any_skill_execution(
    step: SkillCall, allowed: set[str] | None, error_code: str
) -> None:
    async with running_core(allowed_permissions=allowed) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="invalid",
                steps=[SkillCall(skill="audio.speak", params={"message": "must not run"}), step],
            )
        )
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.FAILED
        assert final.error is not None
        assert final.error.code == error_code
        assert final.current_step == 0
        assert sim.skills["audio.speak"].calls == 0
        assert sim.skills["navigation.navigate_to"].calls == 0
        events = await collect_events(core, submitted.id)
        assert [event.event_type for event in events[:2]] == ["TASK_CREATED", "TASK_VALIDATING"]
        assert "TASK_QUEUED" not in {event.event_type for event in events}
        assert events[-1].event_type == "TASK_FAILED"


class PausedReplayRepository(InMemoryTaskRepository):
    def __init__(self) -> None:
        super().__init__()
        self.replay_started = asyncio.Event()
        self.release_replay = asyncio.Event()
        self.pause_next_replay = True

    async def list_events(self, task_id: str, after_sequence: int = 0) -> list[Event]:
        if self.pause_next_replay:
            self.pause_next_replay = False
            self.replay_started.set()
            await self.release_replay.wait()
        return await super().list_events(task_id, after_sequence)


async def test_event_subscription_replays_then_streams_without_gaps_or_duplicates() -> None:
    simulator = Simulator()
    repository = PausedReplayRepository()
    async with running_core(repository, simulator=simulator) as (core, sim):
        navigation = sim.skills["navigation.navigate_to"]
        navigation.delay_s = 0.05
        submitted = await core.submit_task(
            TaskRequest(
                task_type="navigate",
                steps=[SkillCall(skill="navigation.navigate_to", params={"target": "dock"})],
            )
        )
        subscription = core.subscribe_task_events(submitted.id, after_sequence=1)
        first = asyncio.create_task(anext(subscription))
        await wait_for_event(repository.replay_started)
        async with asyncio.timeout(1):
            while navigation.calls == 0:
                await asyncio.sleep(0.001)
        repository.release_replay.set()

        received = [await first]
        await wait_for_terminal(core, submitted.id)
        async for event in subscription:
            received.append(event)

        sequences = [event.sequence for event in received]
        assert sequences == sorted(set(sequences))
        assert sequences[0] == 2
        assert sequences == list(range(2, sequences[-1] + 1))
        assert received[-1].event_type == "TASK_SUCCEEDED"


@pytest.mark.parametrize(
    ("state", "event_type"),
    [
        (TaskState.SUCCEEDED, "TASK_SUCCEEDED"),
        (TaskState.FAILED, "TASK_FAILED"),
        (TaskState.CANCELED, "TASK_CANCELED"),
        (TaskState.UNKNOWN, "TASK_RECONCILED"),
        (TaskState.RECONCILIATION_REQUIRED, "TASK_RECOVERY_REQUIRED"),
    ],
)
async def test_live_terminal_event_is_yielded_before_subscription_ends(
    state: TaskState, event_type: str
) -> None:
    bus = EventBus(InMemoryTaskRepository())
    subscription = bus.subscribe("task")
    first = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)
    terminal = Event(
        event_id="terminal",
        task_id="task",
        sequence=1,
        event_type=event_type,
        data={"state": state.value},
    )
    bus.publish(terminal)

    assert await asyncio.wait_for(first, timeout=1) == terminal
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(subscription), timeout=1)


async def test_slow_subscriber_overflow_is_explicit_and_supports_replay_recovery() -> None:
    repository = InMemoryTaskRepository()
    bus = EventBus(repository)
    bus._queue_size = 1
    subscription = bus.subscribe("task")
    pending = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)
    first = Event(
        event_id="first",
        task_id="task",
        sequence=1,
        event_type="TASK_RUNNING",
        data={"state": TaskState.RUNNING.value},
    )
    terminal = Event(
        event_id="terminal-overflow",
        task_id="task",
        sequence=2,
        event_type="TASK_SUCCEEDED",
        data={"state": TaskState.SUCCEEDED.value},
    )
    bus.publish(first)
    bus.publish(terminal)

    with pytest.raises(AgentRuntimeError) as caught:
        await asyncio.wait_for(pending, timeout=1)
    assert type(caught.value).__name__ == "EventStreamOverflow"
    assert caught.value.info.code == "EVENT_STREAM_OVERFLOW"
    assert caught.value.info.details["after_sequence"] == 0

    await repository.append_event(first)
    await repository.append_event(terminal)
    replayed: list[Event] = []
    async with asyncio.timeout(1):
        async for event in bus.subscribe(
            "task", caught.value.info.details["after_sequence"]
        ):
            replayed.append(event)
    assert [event.event_id for event in replayed] == ["first", "terminal-overflow"]


async def test_live_successful_task_subscription_ends_naturally() -> None:
    class ControlledSkill:
        def __init__(self) -> None:
            self.definition = SkillDefinition(
                name="test.controlled",
                version="1.0.0",
                input_schema={"type": "object"},
            )
            self.release = asyncio.Event()

        async def execute(self, context: Any, params: dict[str, Any]) -> SkillResult:
            await self.release.wait()
            return SkillResult(success=True)

    skill = ControlledSkill()
    core = RuntimeCore(InMemoryTaskRepository(), {skill.definition.name: skill})
    await core.start()
    try:
        submitted = await core.submit_task(
            TaskRequest(
                task_type="controlled",
                steps=[SkillCall(skill=skill.definition.name)],
            )
        )
        events: list[Event] = []
        async with asyncio.timeout(1):
            async for event in core.subscribe_task_events(submitted.id):
                events.append(event)
                if event.event_type == "SKILL_STARTED":
                    skill.release.set()
        assert events[-1].event_type == "TASK_SUCCEEDED"
    finally:
        skill.release.set()
        await core.close()


async def test_close_finishes_subscribers_and_cleans_repository() -> None:
    class TrackingRepository(InMemoryTaskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    repository = TrackingRepository()
    simulator = Simulator()
    simulator.skills["wait.delay"].delay_s = 30
    core = RuntimeCore(repository, simulator.skills)
    await core.start()
    submitted = await core.submit_task(
        TaskRequest(task_type="wait", steps=[SkillCall(skill="wait.delay", params={"delay_s": 30})])
    )
    subscription = core.subscribe_task_events(submitted.id)
    assert (await anext(subscription)).event_type == "TASK_CREATED"
    pending = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)

    await core.close()

    assert pending.done()
    with pytest.raises(StopAsyncIteration):
        while True:
            await asyncio.wait_for(pending, timeout=1)
            pending = asyncio.create_task(anext(subscription))
    assert repository.close_calls == 1
    await subscription.aclose()


async def test_simulator_reports_authoritative_state_and_tracks_concurrency() -> None:
    simulator = Simulator()
    state = await simulator.skills["robot.get_state"].execute(
        context=make_context(), params={}
    )
    assert state.success
    assert state.output["state"]["position"] == "home"
    assert simulator.skills["navigation.navigate_to"].max_active == 0


def make_context() -> Any:
    from robot_agent_runtime.models import SkillContext

    return SkillContext(task_id="test", execution_id="test", cancel_event=asyncio.Event())


async def test_delay_skill_concurrent_calls_do_not_mutate_shared_delay() -> None:
    simulator = Simulator(delay_s=7)
    skill = simulator.skills["wait.delay"]
    first = asyncio.create_task(
        skill.execute(make_context(), {"delay_s": 30})
    )
    second = asyncio.create_task(
        skill.execute(make_context(), {"delay_s": 40})
    )
    try:
        async with asyncio.timeout(1):
            while skill.active != 2:
                await asyncio.sleep(0)

        assert skill.max_active == 2
        assert skill.delay_s == 7
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
    assert skill.delay_s == 7


async def test_non_cancellable_get_state_finishes_before_core_cancels_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulator = Simulator(delay_s=0.05)
    get_state = simulator.skills["robot.get_state"]
    performed = asyncio.Event()
    outputs: list[dict[str, Any]] = []
    original_perform = get_state._perform

    async def observed_perform(context: Any, params: dict[str, Any]) -> dict[str, Any]:
        output = await original_perform(context, params)
        outputs.append(output)
        performed.set()
        return output

    monkeypatch.setattr(get_state, "_perform", observed_perform)
    async with running_core(simulator=simulator) as (core, sim):
        submitted = await core.submit_task(
            TaskRequest(
                task_type="inspect_then_speak",
                steps=[
                    SkillCall(skill="robot.get_state"),
                    SkillCall(skill="audio.speak", params={"message": "later"}),
                ],
            )
        )
        async with asyncio.timeout(1):
            while get_state.active != 1:
                await asyncio.sleep(0)

        final = await asyncio.wait_for(core.cancel_task(submitted.id), timeout=1)

        assert performed.is_set()
        assert outputs == [
            {
                "state": {
                    "position": "home",
                    "last_message": None,
                    "navigation_count": 0,
                    "speech_count": 0,
                }
            }
        ]
        assert get_state.calls == 1
        assert get_state.active == 0
        assert final.state == TaskState.CANCELED
        assert sim.skills["audio.speak"].calls == 0
