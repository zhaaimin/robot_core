import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from robot_agent_runtime.errors import InvalidRequest, TaskNotFound
from robot_agent_runtime.models import ErrorInfo, Event, SkillCall, Task, TaskState
from robot_agent_runtime.ports import TaskRepository


def make_task(
    task_id: str,
    *,
    idempotency_key: str | None = None,
    state: TaskState = TaskState.CREATED,
    created_at: datetime | None = None,
) -> Task:
    timestamp = created_at or datetime(2026, 1, 1, tzinfo=UTC)
    return Task(
        id=task_id,
        task_type="navigate",
        state=state,
        steps=[SkillCall(skill="move", params={"speed": 0.2})],
        idempotency_key=idempotency_key,
        created_at=timestamp,
        updated_at=timestamp,
    )


def make_event(task_id: str, sequence: int, event_id: str | None = None) -> Event:
    return Event(
        event_id=event_id or f"event-{task_id}-{sequence}",
        task_id=task_id,
        sequence=sequence,
        event_type="task.progress",
        timestamp=datetime(2026, 1, 1, 0, 0, sequence, tzinfo=UTC),
        data={"sequence": sequence, "nested": {"ok": True}},
    )


@asynccontextmanager
async def repository(kind: str, tmp_path: Path) -> AsyncIterator[TaskRepository]:
    if kind == "memory":
        from robot_agent_runtime.repository import InMemoryTaskRepository

        repo = InMemoryTaskRepository()
    else:
        from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

        repo = SQLiteTaskRepository(":memory:")
    await repo.start()
    try:
        yield repo
    finally:
        await repo.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_task_create_get_and_update_preserve_the_complete_model(
    kind: str, tmp_path: Path
) -> None:
    timestamp = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    task = Task(
        id="task-full",
        task_type="inspect",
        state=TaskState.WAITING,
        steps=[
            SkillCall(skill="look", params={"target": "dock"}, timeout_s=3.5),
            SkillCall(skill="capture", params={"format": "png"}),
        ],
        current_step=1,
        priority=7,
        timeout_s=42.5,
        idempotency_key="request-full",
        metadata={"source": "test", "nested": {"attempt": 2}},
        result={"partial": [1, 2, 3]},
        error=ErrorInfo(
            code="WAITING_FOR_INPUT",
            message="paused",
            details={"reason": "operator"},
        ),
        cancel_requested=True,
        created_at=timestamp,
        updated_at=timestamp,
    )

    async with repository(kind, tmp_path) as repo:
        assert await repo.create_task(task) == task
        assert await repo.get_task(task.id) == task

        changed = task.model_copy(
            update={
                "state": TaskState.SUCCEEDED,
                "current_step": 2,
                "metadata": {"updated": True},
                "result": {"done": True},
                "error": None,
                "cancel_requested": False,
            }
        )
        updated = await repo.update_task(changed)

        assert updated.updated_at > task.updated_at
        assert updated.model_dump(exclude={"updated_at"}) == changed.model_dump(
            exclude={"updated_at"}
        )
        assert await repo.get_task(task.id) == updated


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_update_missing_task_raises_and_leaves_existing_storage_unchanged(
    kind: str, tmp_path: Path
) -> None:
    async with repository(kind, tmp_path) as repo:
        original = make_task("task-1", idempotency_key="original-key")
        await repo.create_task(original)

        with pytest.raises(TaskNotFound):
            await repo.update_task(
                make_task("missing", idempotency_key="original-key")
            )

        assert await repo.get_task(original.id) == original
        assert await repo.find_by_idempotency_key("original-key") == original


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_update_rejects_occupied_idempotency_key_and_allows_clearing_it(
    kind: str, tmp_path: Path
) -> None:
    async with repository(kind, tmp_path) as repo:
        task_a = make_task("task-a", idempotency_key="key-a")
        task_b = make_task("task-b", idempotency_key="key-b")
        await repo.create_task(task_a)
        await repo.create_task(task_b)

        with pytest.raises(InvalidRequest) as conflict:
            await repo.update_task(task_a.model_copy(update={"idempotency_key": "key-b"}))
        assert conflict.value.info.details["conflict"] == "idempotency_key"
        assert await repo.get_task(task_a.id) == task_a
        assert await repo.get_task(task_b.id) == task_b

        cleared = task_a.model_copy(update={"idempotency_key": None})
        updated = await repo.update_task(cleared)

        assert updated.model_dump(exclude={"updated_at"}) == cleared.model_dump(
            exclude={"updated_at"}
        )
        assert await repo.find_by_idempotency_key("key-a") is None
        assert await repo.find_by_idempotency_key("key-b") == task_b


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_missing_task_and_idempotency_key_return_none(
    kind: str, tmp_path: Path
) -> None:
    async with repository(kind, tmp_path) as repo:
        assert await repo.get_task("missing") is None
        assert await repo.find_by_idempotency_key("missing") is None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_idempotency_key_lookup_and_conflicts(
    kind: str, tmp_path: Path
) -> None:
    async with repository(kind, tmp_path) as repo:
        original = make_task("task-1", idempotency_key="same-key")
        await repo.create_task(original)
        assert await repo.find_by_idempotency_key("same-key") == original

        with pytest.raises(InvalidRequest) as duplicate_id:
            await repo.create_task(make_task("task-1", idempotency_key="other-key"))
        assert duplicate_id.value.info.details["conflict"] == "task_id"

        with pytest.raises(InvalidRequest) as duplicate_key:
            await repo.create_task(make_task("task-2", idempotency_key="same-key"))
        assert duplicate_key.value.info.details["conflict"] == "idempotency_key"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_empty_idempotency_keys_do_not_conflict(
    kind: str, tmp_path: Path
) -> None:
    async with repository(kind, tmp_path) as repo:
        await repo.create_task(make_task("task-none"))
        await repo.create_task(make_task("task-empty-1", idempotency_key=""))
        await repo.create_task(make_task("task-empty-2", idempotency_key=""))

        assert await repo.get_task("task-empty-1") is not None
        assert await repo.get_task("task-empty-2") is not None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
@pytest.mark.parametrize("conflict", ["task_id", "idempotency_key"])
async def test_concurrent_create_has_one_winner(
    kind: str, conflict: str, tmp_path: Path
) -> None:
    async with repository(kind, tmp_path) as repo:
        if conflict == "task_id":
            first = make_task("same-id", idempotency_key="key-1")
            second = make_task("same-id", idempotency_key="key-2")
        else:
            first = make_task("task-1", idempotency_key="same-key")
            second = make_task("task-2", idempotency_key="same-key")

        results = await asyncio.gather(
            repo.create_task(first), repo.create_task(second), return_exceptions=True
        )

        assert sum(isinstance(result, Task) for result in results) == 1
        errors = [result for result in results if isinstance(result, InvalidRequest)]
        assert len(errors) == 1
        assert errors[0].info.details["conflict"] == conflict


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_events_round_trip_in_sequence_order_with_after_filter(
    kind: str, tmp_path: Path
) -> None:
    async with repository(kind, tmp_path) as repo:
        events = [make_event("task-1", 2), make_event("task-1", 1), make_event("task-1", 3)]
        for event in events:
            assert await repo.append_event(event) == event

        assert await repo.list_events("task-1") == sorted(
            events, key=lambda event: event.sequence
        )
        assert await repo.list_events("task-1", after_sequence=1) == [
            events[0],
            events[2],
        ]
        assert await repo.list_events("other-task") == []


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_duplicate_event_sequence_for_one_task_conflicts(
    kind: str, tmp_path: Path
) -> None:
    async with repository(kind, tmp_path) as repo:
        await repo.append_event(make_event("task-1", 1))

        with pytest.raises(InvalidRequest) as conflict:
            await repo.append_event(make_event("task-1", 1, event_id="different-event"))
        assert conflict.value.info.details["conflict"] == "event_sequence"

        await repo.append_event(make_event("task-2", 1))


async def test_inmemory_apply_transition_failure_leaves_task_and_events_unchanged() -> None:
    from robot_agent_runtime.repository import InMemoryTaskRepository

    repo = InMemoryTaskRepository()
    await repo.start()
    original = make_task("task-1", state=TaskState.RUNNING)
    existing_event = make_event("task-1", 1)
    await repo.create_task(original)
    await repo.append_event(existing_event)
    assert hasattr(repo, "apply_transition")

    changed = original.model_copy(update={"state": TaskState.SUCCEEDED})
    with pytest.raises(InvalidRequest):
        await repo.apply_transition(
            changed, make_event("task-1", 1, event_id="duplicate-sequence")
        )

    assert await repo.get_task(original.id) == original
    assert await repo.list_events(original.id) == [existing_event]
    await repo.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_nonterminal_tasks_are_filtered_and_stably_sorted(
    kind: str, tmp_path: Path
) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    tasks = [
        make_task("late", state=TaskState.RUNNING, created_at=base + timedelta(seconds=2)),
        make_task("terminal", state=TaskState.SUCCEEDED, created_at=base - timedelta(seconds=1)),
        make_task("early", state=TaskState.QUEUED, created_at=base),
        make_task("tied-1", state=TaskState.WAITING, created_at=base + timedelta(seconds=1)),
        make_task("tied-2", state=TaskState.CANCELING, created_at=base + timedelta(seconds=1)),
    ]

    async with repository(kind, tmp_path) as repo:
        for task in tasks:
            await repo.create_task(task)

        assert [task.id for task in await repo.get_nonterminal_tasks()] == [
            "early",
            "tied-1",
            "tied-2",
            "late",
        ]


async def test_sqlite_memory_start_and_close_are_idempotent() -> None:
    from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

    repo = SQLiteTaskRepository(":memory:")
    await repo.start()
    await repo.start()
    await repo.create_task(make_task("task-1"))
    await repo.start()
    assert await repo.get_task("task-1") is not None
    await repo.close()
    await repo.close()


async def test_sqlite_same_instance_can_restart_after_repeated_close(
    tmp_path: Path,
) -> None:
    from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

    repo = SQLiteTaskRepository(str(tmp_path / "restart.db"))
    task = make_task("persistent")
    await repo.start()
    await repo.start()
    await repo.create_task(task)
    await repo.close()
    await repo.close()

    await repo.start()
    try:
        assert await repo.get_task(task.id) == task
    finally:
        await repo.close()


async def test_sqlite_start_closes_controlled_connection_when_schema_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiosqlite
    import robot_agent_runtime.sqlite_repository as sqlite_repository

    class FailingSchemaConnection:
        def __init__(self) -> None:
            self.row_factory = None
            self.close_calls = 0

        async def executescript(self, script: str) -> None:
            raise aiosqlite.OperationalError("schema initialization failed")

        async def close(self) -> None:
            self.close_calls += 1

    connection = FailingSchemaConnection()

    async def connect(database: str) -> FailingSchemaConnection:
        return connection

    monkeypatch.setattr(sqlite_repository.aiosqlite, "connect", connect)
    repo = sqlite_repository.SQLiteTaskRepository(":memory:")

    with pytest.raises(aiosqlite.OperationalError, match="schema initialization failed"):
        await repo.start()

    assert connection.close_calls == 1
    await repo.close()
    assert connection.close_calls == 1


async def test_sqlite_cancel_before_commit_rolls_back_and_allows_later_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiosqlite
    from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

    repo = SQLiteTaskRepository(":memory:")
    await repo.start()
    original_commit = aiosqlite.Connection.commit
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    armed = False

    async def controlled_commit(connection: aiosqlite.Connection) -> None:
        if armed:
            commit_started.set()
            await release_commit.wait()
        await original_commit(connection)

    monkeypatch.setattr(aiosqlite.Connection, "commit", controlled_commit)
    cancelled_create: asyncio.Task[Task] | None = None
    try:
        cancelled = make_task("cancelled")
        armed = True
        cancelled_create = asyncio.create_task(repo.create_task(cancelled))
        await commit_started.wait()
        cancelled_create.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_create

        armed = False
        replacement = make_task("replacement")
        await repo.create_task(replacement)
        assert await repo.get_task(cancelled.id) is None
        assert await repo.get_task(replacement.id) == replacement
    finally:
        armed = False
        release_commit.set()
        if cancelled_create is not None and not cancelled_create.done():
            cancelled_create.cancel()
            await asyncio.gather(cancelled_create, return_exceptions=True)
        await repo.close()


async def test_sqlite_read_waits_for_update_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiosqlite
    from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

    repo = SQLiteTaskRepository(":memory:")
    await repo.start()
    original = make_task("task-1", state=TaskState.RUNNING)
    await repo.create_task(original)
    original_commit = aiosqlite.Connection.commit
    original_execute = aiosqlite.Connection.execute
    release_commit = asyncio.Event()
    read_started = asyncio.Event()
    read_before_commit: bool | None = None
    reader: asyncio.Task[Task | None] | None = None

    async def controlled_execute(
        connection: aiosqlite.Connection, sql: str, parameters: tuple = ()
    ) -> aiosqlite.Cursor:
        if sql.lstrip().startswith("SELECT payload FROM tasks"):
            read_started.set()
        return await original_execute(connection, sql, parameters)

    async def controlled_commit(connection: aiosqlite.Connection) -> None:
        nonlocal read_before_commit, reader
        reader = asyncio.create_task(repo.get_task(original.id))
        asyncio.get_running_loop().call_soon(release_commit.set)
        await release_commit.wait()
        read_before_commit = read_started.is_set()
        if read_before_commit:
            await reader
        await original_commit(connection)

    monkeypatch.setattr(aiosqlite.Connection, "execute", controlled_execute)
    monkeypatch.setattr(aiosqlite.Connection, "commit", controlled_commit)
    try:
        changed = original.model_copy(update={"state": TaskState.SUCCEEDED})
        updated = await repo.update_task(changed)

        assert reader is not None
        assert read_before_commit is False
        assert await reader == updated
    finally:
        release_commit.set()
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await repo.close()


async def test_sqlite_can_continue_reading_and_writing_after_unique_conflict() -> None:
    from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

    repo = SQLiteTaskRepository(":memory:")
    await repo.start()
    try:
        original = make_task("task-1", idempotency_key="occupied")
        await repo.create_task(original)
        with pytest.raises(InvalidRequest):
            await repo.create_task(make_task("duplicate", idempotency_key="occupied"))

        replacement = make_task("task-2", idempotency_key="available")
        await repo.create_task(replacement)
        assert await repo.get_task(original.id) == original
        assert await repo.get_task(replacement.id) == replacement
    finally:
        await repo.close()


async def test_sqlite_concurrent_start_creates_only_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiosqlite
    from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

    original_connect = aiosqlite.connect
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()
    connect_calls = 0
    connections: list[aiosqlite.Connection] = []

    async def controlled_connect(database: str) -> aiosqlite.Connection:
        nonlocal connect_calls
        connect_calls += 1
        connect_started.set()
        await release_connect.wait()
        connection = await original_connect(database)
        connections.append(connection)
        return connection

    monkeypatch.setattr(aiosqlite, "connect", controlled_connect)
    repo = SQLiteTaskRepository(":memory:")
    first = asyncio.create_task(repo.start())
    second: asyncio.Task[None] | None = None
    try:
        await connect_started.wait()
        second_entered = asyncio.Event()

        async def start_second() -> None:
            second_entered.set()
            await repo.start()

        second = asyncio.create_task(start_second())
        await second_entered.wait()
        release_connect.set()
        await asyncio.gather(first, second)

        assert connect_calls == 1
        task = make_task("after-start")
        await repo.create_task(task)
        assert await repo.get_task(task.id) == task
    finally:
        release_connect.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await repo.close()
        for connection in connections:
            await connection.close()


async def test_sqlite_close_waits_for_concurrent_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aiosqlite
    from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

    original_connect = aiosqlite.connect
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()
    connections: list[aiosqlite.Connection] = []

    async def controlled_connect(database: str) -> aiosqlite.Connection:
        connect_started.set()
        await release_connect.wait()
        connection = await original_connect(database)
        connections.append(connection)
        return connection

    monkeypatch.setattr(aiosqlite, "connect", controlled_connect)
    repo = SQLiteTaskRepository(":memory:")
    starter = asyncio.create_task(repo.start())
    closer: asyncio.Task[None] | None = None
    try:
        await connect_started.wait()
        close_entered = asyncio.Event()

        async def close_after_start() -> None:
            close_entered.set()
            await repo.close()

        closer = asyncio.create_task(close_after_start())
        await close_entered.wait()
        release_connect.set()
        await asyncio.gather(starter, closer)

        with pytest.raises(RuntimeError, match="not started"):
            await repo.get_task("missing")
    finally:
        release_connect.set()
        await asyncio.gather(starter, closer, return_exceptions=True)
        await repo.close()
        for connection in connections:
            await connection.close()


@pytest.mark.parametrize("operation", ["task", "event"])
async def test_sqlite_preserves_unknown_integrity_errors(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aiosqlite
    from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

    repo = SQLiteTaskRepository(":memory:")
    await repo.start()
    original_execute = aiosqlite.Connection.execute
    statement = "INSERT INTO tasks" if operation == "task" else "INSERT INTO events"
    constraint = "tasks.unknown" if operation == "task" else "events.event_id"

    async def controlled_execute(
        connection: aiosqlite.Connection, sql: str, parameters: tuple = ()
    ) -> aiosqlite.Cursor:
        if statement in sql:
            raise aiosqlite.IntegrityError(f"UNIQUE constraint failed: {constraint}")
        return await original_execute(connection, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", controlled_execute)
    try:
        with pytest.raises(aiosqlite.IntegrityError) as error:
            if operation == "task":
                await repo.create_task(make_task("task-1"))
            else:
                await repo.append_event(make_event("task-1", 1))
        assert str(error.value) == f"UNIQUE constraint failed: {constraint}"
    finally:
        await repo.close()


async def test_sqlite_file_data_survives_close_and_reopen(tmp_path: Path) -> None:
    from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

    database = str(tmp_path / "runtime.db")
    task = make_task("persistent", idempotency_key="persistent-key")
    event = make_event(task.id, 1)

    first = SQLiteTaskRepository(database)
    await first.start()
    await first.create_task(task)
    await first.append_event(event)
    await first.close()

    second = SQLiteTaskRepository(database)
    await second.start()
    try:
        assert await second.get_task(task.id) == task
        assert await second.find_by_idempotency_key(task.idempotency_key) == task
        assert await second.list_events(task.id) == [event]
    finally:
        await second.close()
        await second.close()


def test_models_reject_naive_datetimes_and_nonpositive_event_sequences() -> None:
    with pytest.raises(ValidationError):
        make_task("naive", created_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        Event(event_id="invalid", task_id="task-1", sequence=0, event_type="invalid")
