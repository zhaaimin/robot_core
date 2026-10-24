import asyncio
from datetime import UTC, datetime

import aiosqlite

from robot_agent_runtime.errors import InvalidRequest, TaskNotFound
from robot_agent_runtime.models import Event, Task


class SQLiteTaskRepository:
    def __init__(self, database: str) -> None:
        self._database = database
        self._connection: aiosqlite.Connection | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            async with self._operation_lock:
                if self._connection is not None:
                    return

                connection = await aiosqlite.connect(self._database)
                connection.row_factory = aiosqlite.Row
                try:
                    await connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS tasks (
                            id TEXT PRIMARY KEY,
                            idempotency_key TEXT,
                            is_terminal INTEGER NOT NULL,
                            created_at REAL NOT NULL,
                            payload TEXT NOT NULL
                        );

                        CREATE UNIQUE INDEX IF NOT EXISTS tasks_idempotency_key_unique
                        ON tasks(idempotency_key)
                        WHERE idempotency_key IS NOT NULL AND idempotency_key <> '';

                        CREATE TABLE IF NOT EXISTS events (
                            event_id TEXT NOT NULL,
                            task_id TEXT NOT NULL,
                            sequence INTEGER NOT NULL CHECK (sequence > 0),
                            payload TEXT NOT NULL,
                            UNIQUE (task_id, sequence)
                        );
                        """
                    )
                    await connection.commit()
                except BaseException:
                    await connection.close()
                    raise
                self._connection = connection

    async def close(self) -> None:
        async with self._lifecycle_lock:
            async with self._operation_lock:
                if self._connection is None:
                    return
                connection = self._connection
                try:
                    await connection.close()
                finally:
                    self._connection = None

    async def create_task(self, task: Task) -> Task:
        async with self._operation_lock:
            connection = self._require_connection()
            cursor: aiosqlite.Cursor | None = None
            try:
                cursor = await connection.execute(
                    """
                    INSERT INTO tasks (id, idempotency_key, is_terminal, created_at, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    self._task_parameters(task),
                )
                await connection.commit()
            except BaseException as error:
                await connection.rollback()
                if isinstance(error, aiosqlite.IntegrityError):
                    conflict = self._task_conflict(task, error)
                    if conflict is not None:
                        raise conflict from error
                raise
            finally:
                if cursor is not None:
                    await cursor.close()
            return Task.model_validate_json(task.model_dump_json())

    async def get_task(self, task_id: str) -> Task | None:
        async with self._operation_lock:
            connection = self._require_connection()
            cursor: aiosqlite.Cursor | None = None
            try:
                cursor = await connection.execute(
                    "SELECT payload FROM tasks WHERE id = ?", (task_id,)
                )
                row = await cursor.fetchone()
            finally:
                if cursor is not None:
                    await cursor.close()
            return Task.model_validate_json(row["payload"]) if row is not None else None

    async def update_task(self, task: Task) -> Task:
        updated = task.model_copy(update={"updated_at": datetime.now(UTC)})
        async with self._operation_lock:
            connection = self._require_connection()
            _, idempotency_key, is_terminal, created_at, payload = self._task_parameters(updated)
            cursor: aiosqlite.Cursor | None = None
            try:
                cursor = await connection.execute(
                    """
                    UPDATE tasks
                    SET idempotency_key = ?, is_terminal = ?, created_at = ?, payload = ?
                    WHERE id = ?
                    """,
                    (idempotency_key, is_terminal, created_at, payload, task.id),
                )
                if cursor.rowcount == 0:
                    raise TaskNotFound("Task not found", details={"task_id": task.id})
                await connection.commit()
            except BaseException as error:
                await connection.rollback()
                if isinstance(error, aiosqlite.IntegrityError):
                    conflict = self._task_conflict(updated, error)
                    if conflict is not None:
                        raise conflict from error
                raise
            finally:
                if cursor is not None:
                    await cursor.close()
            return Task.model_validate_json(updated.model_dump_json())

    async def append_event(self, event: Event) -> Event:
        async with self._operation_lock:
            connection = self._require_connection()
            cursor: aiosqlite.Cursor | None = None
            try:
                cursor = await connection.execute(
                    """
                    INSERT INTO events (event_id, task_id, sequence, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.task_id,
                        event.sequence,
                        event.model_dump_json(),
                    ),
                )
                await connection.commit()
            except BaseException as error:
                await connection.rollback()
                if (
                    isinstance(error, aiosqlite.IntegrityError)
                    and "UNIQUE constraint failed: events.task_id, events.sequence"
                    in str(error)
                ):
                    raise InvalidRequest(
                        "Event sequence already exists for task",
                        details={
                            "conflict": "event_sequence",
                            "task_id": event.task_id,
                            "sequence": event.sequence,
                        },
                    ) from error
                raise
            finally:
                if cursor is not None:
                    await cursor.close()
            return Event.model_validate_json(event.model_dump_json())

    async def apply_transition(self, task: Task, event: Event) -> tuple[Task, Event]:
        if task.id != event.task_id:
            raise InvalidRequest("Task and event task ids must match")
        updated = task.model_copy(update={"updated_at": datetime.now(UTC)})
        async with self._operation_lock:
            connection = self._require_connection()
            _, idempotency_key, is_terminal, created_at, payload = self._task_parameters(
                updated
            )
            cursor: aiosqlite.Cursor | None = None
            try:
                cursor = await connection.execute("BEGIN")
                await cursor.close()
                cursor = None
                cursor = await connection.execute(
                    """
                    UPDATE tasks
                    SET idempotency_key = ?, is_terminal = ?, created_at = ?, payload = ?
                    WHERE id = ?
                    """,
                    (idempotency_key, is_terminal, created_at, payload, task.id),
                )
                if cursor.rowcount == 0:
                    raise TaskNotFound("Task not found", details={"task_id": task.id})
                await cursor.close()
                cursor = None
                cursor = await connection.execute(
                    """
                    INSERT INTO events (event_id, task_id, sequence, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.task_id,
                        event.sequence,
                        event.model_dump_json(),
                    ),
                )
                await connection.commit()
            except BaseException as error:
                await connection.rollback()
                if (
                    isinstance(error, aiosqlite.IntegrityError)
                    and "UNIQUE constraint failed: events.task_id, events.sequence"
                    in str(error)
                ):
                    raise InvalidRequest(
                        "Event sequence already exists for task",
                        details={
                            "conflict": "event_sequence",
                            "task_id": event.task_id,
                            "sequence": event.sequence,
                        },
                    ) from error
                if isinstance(error, aiosqlite.IntegrityError):
                    conflict = self._task_conflict(updated, error)
                    if conflict is not None:
                        raise conflict from error
                raise
            finally:
                if cursor is not None:
                    await cursor.close()
            return (
                Task.model_validate_json(updated.model_dump_json()),
                Event.model_validate_json(event.model_dump_json()),
            )

    async def list_events(
        self, task_id: str, after_sequence: int = 0
    ) -> list[Event]:
        async with self._operation_lock:
            connection = self._require_connection()
            cursor: aiosqlite.Cursor | None = None
            try:
                cursor = await connection.execute(
                    """
                    SELECT payload FROM events
                    WHERE task_id = ? AND sequence > ?
                    ORDER BY sequence ASC
                    """,
                    (task_id, after_sequence),
                )
                rows = await cursor.fetchall()
            finally:
                if cursor is not None:
                    await cursor.close()
            return [Event.model_validate_json(row["payload"]) for row in rows]

    async def get_nonterminal_tasks(self) -> list[Task]:
        async with self._operation_lock:
            connection = self._require_connection()
            cursor: aiosqlite.Cursor | None = None
            try:
                cursor = await connection.execute(
                    """
                    SELECT payload FROM tasks
                    WHERE is_terminal = 0
                    ORDER BY created_at ASC, rowid ASC
                    """
                )
                rows = await cursor.fetchall()
            finally:
                if cursor is not None:
                    await cursor.close()
            return [Task.model_validate_json(row["payload"]) for row in rows]

    async def find_by_idempotency_key(self, key: str) -> Task | None:
        async with self._operation_lock:
            if not key:
                return None
            connection = self._require_connection()
            cursor: aiosqlite.Cursor | None = None
            try:
                cursor = await connection.execute(
                    "SELECT payload FROM tasks WHERE idempotency_key = ?", (key,)
                )
                row = await cursor.fetchone()
            finally:
                if cursor is not None:
                    await cursor.close()
            return Task.model_validate_json(row["payload"]) if row is not None else None

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Repository is not started")
        return self._connection

    @staticmethod
    def _task_parameters(task: Task) -> tuple[str, str | None, int, float, str]:
        return (
            task.id,
            task.idempotency_key,
            int(task.state.is_terminal),
            task.created_at.timestamp(),
            task.model_dump_json(),
        )

    @staticmethod
    def _task_conflict(
        task: Task, error: aiosqlite.IntegrityError
    ) -> InvalidRequest | None:
        message = str(error)
        if "UNIQUE constraint failed: tasks.idempotency_key" in message:
            return InvalidRequest(
                "Idempotency key already exists",
                details={
                    "conflict": "idempotency_key",
                    "idempotency_key": task.idempotency_key,
                },
            )
        if "UNIQUE constraint failed: tasks.id" in message:
            return InvalidRequest(
                "Task id already exists",
                details={"conflict": "task_id", "task_id": task.id},
            )
        return None
