import asyncio
from pathlib import Path
from typing import Any

import pytest

import robot_agent_runtime.adapters.simulator as simulator_module
from robot_agent_runtime.core import RuntimeCore
from robot_agent_runtime.models import (
    Event,
    SkillCall,
    SkillContext,
    SkillDefinition,
    SkillResult,
    Task,
    TaskRequest,
    TaskState,
)
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository


NONTERMINAL_STATES = [
    TaskState.CREATED,
    TaskState.VALIDATING,
    TaskState.QUEUED,
    TaskState.RUNNING,
    TaskState.WAITING,
    TaskState.CANCELING,
]
AUTHORITATIVE_STATES = [
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELED,
    TaskState.UNKNOWN,
]


class ControlledSkill:
    def __init__(self) -> None:
        self.definition = SkillDefinition(
            name="controlled",
            version="1.0.0",
            input_schema={"type": "object"},
        )
        self.calls = 0

    async def execute(
        self, context: SkillContext, params: dict[str, Any]
    ) -> SkillResult:
        self.calls += 1
        return SkillResult(success=True, output={"task_id": context.task_id})


class StubReconciler:
    def __init__(
        self,
        result: TaskState | None = None,
        *,
        exception: Exception | None = None,
    ) -> None:
        self.result = result
        self.exception = exception
        self.calls: list[str] = []

    async def reconcile(self, task: Task) -> TaskState | None:
        self.calls.append(task.id)
        if self.exception is not None:
            raise self.exception
        return self.result


class BlockingReconciler(StubReconciler):
    def __init__(self, result: TaskState) -> None:
        super().__init__(result)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def reconcile(self, task: Task) -> TaskState | None:
        self.calls.append(task.id)
        self.entered.set()
        await self.release.wait()
        return self.result


class StartGateRepository(InMemoryTaskRepository):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_started = asyncio.Event()
        self.release_recovery = asyncio.Event()
        self.gate_next_recovery = True
        self.close_calls = 0

    async def get_nonterminal_tasks(self) -> list[Task]:
        if self.gate_next_recovery:
            self.gate_next_recovery = False
            self.recovery_started.set()
            await self.release_recovery.wait()
        return await super().get_nonterminal_tasks()

    async def close(self) -> None:
        self.close_calls += 1


class InjectedEventWriteFailure(BaseException):
    pass


class FailingRecoveryRepository(InMemoryTaskRepository):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def get_nonterminal_tasks(self) -> list[Task]:
        raise OSError("recovery query failed")

    async def close(self) -> None:
        self.close_calls += 1


def make_task(
    task_id: str = "recovered", state: TaskState = TaskState.RUNNING
) -> Task:
    return Task(
        id=task_id,
        task_type="recovery-test",
        state=state,
        current_step=1,
        steps=[SkillCall(skill="controlled")],
    )


async def seed_task(
    repository: InMemoryTaskRepository | SQLiteTaskRepository,
    task: Task,
    *,
    event_count: int = 0,
) -> None:
    await repository.start()
    await repository.create_task(task)
    for sequence in range(1, event_count + 1):
        await repository.append_event(
            Event(
                event_id=f"{task.id}-event-{sequence}",
                task_id=task.id,
                sequence=sequence,
                event_type="EXISTING_EVENT",
                data={"state": task.state.value, "current_step": task.current_step},
            )
        )


async def wait_for_terminal(core: RuntimeCore, task_id: str) -> Task:
    async with asyncio.timeout(1):
        while True:
            task = await core.get_task_status(task_id)
            if task.state.is_terminal:
                return task
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("state", NONTERMINAL_STATES)
async def test_start_recovers_nonterminal_without_replaying_skills(
    state: TaskState,
) -> None:
    repository = InMemoryTaskRepository()
    await seed_task(repository, make_task(state=state))
    skill = ControlledSkill()
    core = RuntimeCore(repository, {"controlled": skill})
    try:
        await core.start()

        recovered = await core.get_task_status("recovered")
        assert recovered.state == TaskState.RECONCILIATION_REQUIRED
        assert recovered.error is not None
        assert recovered.error.code == "RECONCILIATION_REQUIRED"
        assert recovered.error.details["reason"] == "no_reconciler"
        assert skill.calls == 0
        assert core._executions == {}
        events = await repository.list_events("recovered")
        assert [event.event_type for event in events] == ["TASK_RECOVERY_REQUIRED"]
    finally:
        await core.close()


@pytest.mark.parametrize("authoritative_state", AUTHORITATIVE_STATES)
async def test_reconciler_authoritative_terminal_states_are_persisted(
    authoritative_state: TaskState,
) -> None:
    repository = InMemoryTaskRepository()
    await seed_task(repository, make_task())
    skill = ControlledSkill()
    reconciler = StubReconciler(authoritative_state)
    core = RuntimeCore(repository, {"controlled": skill}, reconciler=reconciler)
    try:
        await core.start()

        recovered = await core.get_task_status("recovered")
        assert recovered.state == authoritative_state
        assert reconciler.calls == ["recovered"]
        assert skill.calls == 0
        assert core._executions == {}
        if authoritative_state == TaskState.SUCCEEDED:
            assert recovered.result is None
            assert recovered.error is None
        else:
            assert recovered.result is None
            assert recovered.error is not None
            assert recovered.error.details["authoritative"] is True
            assert recovered.error.details["reconciled"] is True
            assert recovered.error.details["authoritative_state"] == authoritative_state.value
        events = await repository.list_events("recovered")
        assert [event.event_type for event in events] == ["TASK_RECONCILED"]
        assert events[0].data["authoritative"] is True
        assert events[0].data["reconciled"] is True
        assert events[0].data["authoritative_state"] == authoritative_state.value
    finally:
        await core.close()


@pytest.mark.parametrize(
    "indeterminate_state",
    [None, *NONTERMINAL_STATES, TaskState.RECONCILIATION_REQUIRED],
)
async def test_indeterminate_reconciler_results_require_reconciliation(
    indeterminate_state: TaskState | None,
) -> None:
    repository = InMemoryTaskRepository()
    await seed_task(repository, make_task(state=TaskState.QUEUED))
    skill = ControlledSkill()
    core = RuntimeCore(
        repository,
        {"controlled": skill},
        reconciler=StubReconciler(indeterminate_state),
    )
    try:
        await core.start()

        recovered = await core.get_task_status("recovered")
        assert recovered.state == TaskState.RECONCILIATION_REQUIRED
        assert recovered.error is not None
        assert recovered.error.code == "RECONCILIATION_REQUIRED"
        assert recovered.error.details["reason"] == "indeterminate"
        assert skill.calls == 0
        assert (await repository.list_events("recovered"))[-1].event_type == (
            "TASK_RECOVERY_REQUIRED"
        )
    finally:
        await core.close()


async def test_reconciler_exception_requires_reconciliation_and_start_continues() -> None:
    repository = InMemoryTaskRepository()
    await seed_task(repository, make_task())
    skill = ControlledSkill()
    core = RuntimeCore(
        repository,
        {"controlled": skill},
        reconciler=StubReconciler(exception=TimeoutError("device timeout")),
    )
    try:
        await core.start()

        recovered = await core.get_task_status("recovered")
        assert recovered.state == TaskState.RECONCILIATION_REQUIRED
        assert recovered.error is not None
        assert recovered.error.details["exception_type"] == "TimeoutError"
        assert skill.calls == 0
        submitted = await core.submit_task(
            TaskRequest(task_type="new", steps=[SkillCall(skill="controlled")])
        )
        assert (await wait_for_terminal(core, submitted.id)).state == TaskState.SUCCEEDED
        assert skill.calls == 1
    finally:
        await core.close()


async def test_recovery_event_sequence_and_data_match_persisted_snapshot() -> None:
    repository = InMemoryTaskRepository()
    await seed_task(repository, make_task(), event_count=2)
    core = RuntimeCore(
        repository,
        {"controlled": ControlledSkill()},
        reconciler=StubReconciler(TaskState.FAILED),
    )
    try:
        await core.start()

        recovered = await core.get_task_status("recovered")
        events = await repository.list_events("recovered")
        assert [event.sequence for event in events] == [1, 2, 3]
        assert events[-1].event_type == "TASK_RECONCILED"
        assert events[-1].data["state"] == recovered.state.value
        assert events[-1].data["current_step"] == recovered.current_step
        assert recovered.error is not None
        assert events[-1].data["error"] == recovered.error.to_dict()
    finally:
        await core.close()


async def test_repeated_start_does_not_reconcile_or_emit_events_again() -> None:
    repository = InMemoryTaskRepository()
    await seed_task(repository, make_task())
    reconciler = StubReconciler(TaskState.SUCCEEDED)
    core = RuntimeCore(repository, {}, reconciler=reconciler)
    try:
        await core.start()
        events_after_first_start = await repository.list_events("recovered")

        await core.start()
        await core.start()

        assert reconciler.calls == ["recovered"]
        assert await repository.list_events("recovered") == events_after_first_start
    finally:
        await core.close()


async def test_new_task_executes_normally_after_recovery() -> None:
    repository = InMemoryTaskRepository()
    await seed_task(repository, make_task(state=TaskState.QUEUED))
    skill = ControlledSkill()
    core = RuntimeCore(repository, {"controlled": skill})
    try:
        await core.start()
        submitted = await core.submit_task(
            TaskRequest(task_type="new", steps=[SkillCall(skill="controlled")])
        )
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.SUCCEEDED
        assert final.id != "recovered"
        assert skill.calls == 1
        assert (await core.get_task_status("recovered")).state == (
            TaskState.RECONCILIATION_REQUIRED
        )
    finally:
        await core.close()


async def test_sqlite_file_reopen_recovers_task_and_preserves_event_history(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "recovery.db")
    first = SQLiteTaskRepository(database)
    await seed_task(first, make_task(), event_count=1)
    await first.close()

    second = SQLiteTaskRepository(database)
    core = RuntimeCore(second, {"controlled": ControlledSkill()})
    await core.start()
    recovered = await core.get_task_status("recovered")
    events = await second.list_events("recovered")
    await core.close()

    assert recovered.state == TaskState.RECONCILIATION_REQUIRED
    assert [event.sequence for event in events] == [1, 2]
    assert events[-1].event_type == "TASK_RECOVERY_REQUIRED"

    third = SQLiteTaskRepository(database)
    await third.start()
    try:
        assert await third.get_task("recovered") == recovered
        assert await third.list_events("recovered") == events
    finally:
        await third.close()


async def test_recovery_blocks_new_submissions_until_reconciliation_finishes() -> None:
    repository = InMemoryTaskRepository()
    await seed_task(repository, make_task())
    skill = ControlledSkill()
    reconciler = BlockingReconciler(TaskState.SUCCEEDED)
    core = RuntimeCore(repository, {"controlled": skill}, reconciler=reconciler)
    starting = asyncio.create_task(core.start())
    submitting: asyncio.Task[Task] | None = None
    try:
        await asyncio.wait_for(reconciler.entered.wait(), timeout=1)
        submitting = asyncio.create_task(
            core.submit_task(
                TaskRequest(task_type="new", steps=[SkillCall(skill="controlled")])
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert not submitting.done()
        assert skill.calls == 0

        reconciler.release.set()
        await asyncio.wait_for(starting, timeout=1)
        submitted = await asyncio.wait_for(submitting, timeout=1)
        assert (await wait_for_terminal(core, submitted.id)).state == TaskState.SUCCEEDED
        assert skill.calls == 1
    finally:
        reconciler.release.set()
        await asyncio.gather(starting, return_exceptions=True)
        if submitting is not None:
            await asyncio.gather(submitting, return_exceptions=True)
        await core.close()


async def test_repository_recovery_failure_fails_start_and_closes_repository() -> None:
    repository = FailingRecoveryRepository()
    core = RuntimeCore(repository, {})
    try:
        with pytest.raises(OSError, match="recovery query failed"):
            await core.start()

        assert repository.close_calls == 1
        with pytest.raises(RuntimeError, match="not running"):
            await core.submit_task(
                TaskRequest(task_type="rejected", steps=[SkillCall(skill="unused")])
            )
    finally:
        await core.close()


async def test_cancelled_start_cleans_up_and_can_be_retried_without_duplicate_recovery() -> None:
    repository = StartGateRepository()
    await seed_task(repository, make_task())
    core = RuntimeCore(repository, {})
    starting = asyncio.create_task(core.start())
    try:
        await asyncio.wait_for(repository.recovery_started.wait(), timeout=1)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert repository.close_calls == 1

        await core.start()
        await core.start()

        assert (await core.get_task_status("recovered")).state == (
            TaskState.RECONCILIATION_REQUIRED
        )
        assert len(await repository.list_events("recovered")) == 1
    finally:
        repository.release_recovery.set()
        await asyncio.gather(starting, return_exceptions=True)
        await core.close()


async def test_sqlite_event_write_failure_rolls_back_and_recovery_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiosqlite

    database = str(tmp_path / "atomic-recovery.db")
    first = SQLiteTaskRepository(database)
    original = make_task()
    await seed_task(first, original, event_count=1)
    original_events = await first.list_events(original.id)
    await first.close()

    second = SQLiteTaskRepository(database)
    core = RuntimeCore(second, {})
    original_execute = aiosqlite.Connection.execute
    fail_event_write = True
    update_attempted = False

    async def fail_second_write(
        connection: aiosqlite.Connection, sql: str, parameters: tuple = ()
    ) -> aiosqlite.Cursor:
        nonlocal fail_event_write, update_attempted
        statement = sql.lstrip()
        if statement.startswith("UPDATE tasks"):
            update_attempted = True
        if statement.startswith("INSERT INTO events") and fail_event_write:
            fail_event_write = False
            raise InjectedEventWriteFailure("injected event write failure")
        return await original_execute(connection, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", fail_second_write)
    try:
        with pytest.raises(InjectedEventWriteFailure):
            await core.start()
        assert update_attempted is True

        await second.start()
        snapshot_after_failure = await second.get_task(original.id)
        events_after_failure = await second.list_events(original.id)
        await second.close()

        assert snapshot_after_failure == original
        assert events_after_failure == original_events

        await core.start()
        recovered = await core.get_task_status(original.id)
        assert recovered.state == TaskState.RECONCILIATION_REQUIRED
        await core.close()

        third = SQLiteTaskRepository(database)
        await third.start()
        try:
            assert await third.get_task(original.id) == recovered
            events = await third.list_events(original.id)
            assert [event.sequence for event in events] == [1, 2]
            assert events[-1].event_type == "TASK_RECOVERY_REQUIRED"
        finally:
            await third.close()
    finally:
        await core.close()


async def test_simulator_reconciler_reads_authoritative_terminal_mapping() -> None:
    reconciler_type = getattr(simulator_module, "SimulatorReconciler", None)
    assert reconciler_type is not None
    reconciler = reconciler_type({"recovered": TaskState.CANCELED})

    assert await reconciler.reconcile(make_task()) == TaskState.CANCELED
    assert await reconciler.reconcile(make_task("untracked")) is None
