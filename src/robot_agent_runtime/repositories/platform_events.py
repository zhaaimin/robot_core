import asyncio
from collections import OrderedDict

import aiosqlite

from robot_agent_runtime.errors import InvalidRequest, PlatformEventCursorExpired
from robot_agent_runtime.models import PlatformEvent


def _positive(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise InvalidRequest(f"{name} must be a positive integer")


def _check_cursor(after: int, earliest: int, latest: int) -> None:
    if type(after) is not int or after < 0 or after > latest:
        raise InvalidRequest("Platform event cursor is outside this journal", {"after_cursor": after, "latest_cursor": latest})
    if after < earliest - 1:
        raise PlatformEventCursorExpired(
            "Platform event history has expired; synchronize current platform state",
            {"after_cursor": after, "earliest_cursor": earliest, "latest_cursor": latest},
        )


def _input(event: PlatformEvent) -> PlatformEvent:
    if type(event) is not PlatformEvent or event.cursor != 0:
        raise InvalidRequest("Platform event sources must submit cursor zero")
    return PlatformEvent.model_validate_json(event.model_dump_json())


def _same(existing: PlatformEvent, incoming: PlatformEvent) -> None:
    if existing.model_copy(update={"cursor": 0}) != incoming:
        raise InvalidRequest("Platform event ID already identifies a different fact", {"event_id": incoming.event_id})


class InMemoryPlatformEventRepository:
    """Bound retained history while keeping the cursor monotonic across pruning.

    Like the task memory repository this is process-local, not crash durable.
    Event-ID deduplication is guaranteed within the retained history window.
    """

    def __init__(self, *, max_events: int = 10000) -> None:
        _positive(max_events, "max_events")
        self._max_events = max_events
        self._events: OrderedDict[int, PlatformEvent] = OrderedDict()
        self._ids: dict[str, int] = {}
        self._latest = 0
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def close(self) -> None:
        self._started = False

    def _require_started(self) -> None:
        if not self._started:
            raise InvalidRequest("Platform event journal is not started")

    async def append_platform_event(self, event: PlatformEvent) -> PlatformEvent:
        self._require_started()
        value = _input(event)
        prior = self._ids.get(value.event_id)
        if prior is not None:
            _same(self._events[prior], value)
            return self._events[prior].model_copy(deep=True)
        self._latest += 1
        stored = value.model_copy(update={"cursor": self._latest})
        self._events[stored.cursor] = stored
        self._ids[stored.event_id] = stored.cursor
        while len(self._events) > self._max_events:
            _, removed = self._events.popitem(last=False)
            self._ids.pop(removed.event_id, None)
        return stored.model_copy(deep=True)

    async def platform_event_bounds(self) -> tuple[int, int]:
        self._require_started()
        return next(iter(self._events), self._latest + 1), self._latest

    async def list_platform_events(self, after_cursor: int = 0, limit: int = 128) -> list[PlatformEvent]:
        _positive(limit, "limit")
        earliest, latest = await self.platform_event_bounds()
        _check_cursor(after_cursor, earliest, latest)
        result: list[PlatformEvent] = []
        for cursor, value in self._events.items():
            if cursor > after_cursor:
                result.append(value.model_copy(deep=True))
                if len(result) == limit:
                    break
        return result


class SQLitePlatformEventRepository:
    """Own an independent connection and transaction for the device journal.

    Task transitions keep their original transaction. AUTOINCREMENT preserves
    resume positions across retention and process restart; bounded retention
    deliberately limits deduplication and replay to the same documented window.
    """

    def __init__(self, database: str, *, max_events: int = 10000) -> None:
        _positive(max_events, "max_events")
        self._database = database
        self._max_events = max_events
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._connection is not None:
                return
            connection = await aiosqlite.connect(self._database)
            try:
                await connection.execute("""CREATE TABLE IF NOT EXISTS platform_events (
                    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                )""")
                await connection.commit()
            except BaseException:
                await connection.close()
                raise
            self._connection = connection

    def _connection_or_raise(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise InvalidRequest("Platform event journal is not started")
        return self._connection

    async def close(self) -> None:
        async with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                await connection.close()

    async def append_platform_event(self, event: PlatformEvent) -> PlatformEvent:
        value = _input(event)
        async with self._lock:
            connection = self._connection_or_raise()
            try:
                async with connection.execute("BEGIN IMMEDIATE"):
                    pass
                async with connection.execute("SELECT cursor, payload FROM platform_events WHERE event_id=?", (value.event_id,)) as query:
                    prior = await query.fetchone()
                if prior is not None:
                    stored = PlatformEvent.model_validate_json(prior[1]).model_copy(update={"cursor": prior[0]})
                    _same(stored, value)
                else:
                    async with connection.execute("INSERT INTO platform_events(event_id,payload) VALUES (?,?)", (value.event_id, value.model_dump_json())) as insert:
                        stored = value.model_copy(update={"cursor": insert.lastrowid})
                    async with connection.execute("""DELETE FROM platform_events WHERE cursor < COALESCE(
                        (SELECT cursor FROM platform_events ORDER BY cursor DESC LIMIT 1 OFFSET ?), 0)""", (self._max_events - 1,)):
                        pass
                await connection.commit()
                return stored
            except BaseException:
                await connection.rollback()
                raise

    async def _bounds(self, connection: aiosqlite.Connection) -> tuple[int, int]:
        async with connection.execute("SELECT seq FROM sqlite_sequence WHERE name='platform_events'") as query:
            row = await query.fetchone()
        latest = row[0] if row else 0
        async with connection.execute("SELECT MIN(cursor) FROM platform_events") as query:
            first = await query.fetchone()
        earliest = first[0] if first is not None and first[0] is not None else latest + 1
        return earliest, latest

    async def platform_event_bounds(self) -> tuple[int, int]:
        async with self._lock:
            return await self._bounds(self._connection_or_raise())

    async def list_platform_events(self, after_cursor: int = 0, limit: int = 128) -> list[PlatformEvent]:
        _positive(limit, "limit")
        async with self._lock:
            connection = self._connection_or_raise()
            earliest, latest = await self._bounds(connection)
            _check_cursor(after_cursor, earliest, latest)
            async with connection.execute("SELECT cursor,payload FROM platform_events WHERE cursor>? ORDER BY cursor LIMIT ?", (after_cursor, limit)) as query:
                rows = await query.fetchall()
            return [PlatformEvent.model_validate_json(row[1]).model_copy(update={"cursor": row[0]}) for row in rows]
