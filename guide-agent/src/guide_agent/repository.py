import json
import sqlite3
from dataclasses import asdict
from typing import Any

from guide_agent.errors import GuideError
from guide_agent.models import GuideSession


class GuideRepository:
    """Commit intentions and robot ownership before any external side effect.

    Revisions, the lease and each session's quarantine share one transaction,
    including across workflow instances. Quarantine blocks new robot ownership
    independently of the current lease. Nothing retries an uncertain action.
    The application owns the connection on its asyncio thread.
    """

    def __init__(self, database: str) -> None:
        self._database = database
        self._connection: sqlite3.Connection | None = None

    def start(self) -> None:
        if self._connection is not None:
            return
        try:
            connection = sqlite3.connect(self._database, timeout=0, isolation_level=None)
            connection.execute('CREATE TABLE IF NOT EXISTS guide_sessions (session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS guide_robot (id INTEGER PRIMARY KEY CHECK (id=1), owner TEXT)')
            connection.execute('INSERT OR IGNORE INTO guide_robot VALUES (1, NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS guide_events (session_id TEXT, event_id TEXT, payload TEXT NOT NULL, PRIMARY KEY (session_id,event_id))')
            connection.execute('CREATE TABLE IF NOT EXISTS guide_quarantine (session_id TEXT PRIMARY KEY)')
            connection.execute('BEGIN IMMEDIATE')
            for session_id, payload in connection.execute('SELECT session_id,payload FROM guide_sessions'):
                if json.loads(payload)['phase'] == 'reconciliation_required':
                    connection.execute('INSERT OR IGNORE INTO guide_quarantine VALUES (?)', (session_id,))
            connection.execute('COMMIT')
            self._connection = connection
        except sqlite3.Error as error:
            raise GuideError('Guide database could not be opened') from error

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise GuideError('Guide repository is closed')
        return self._connection

    def get(self, session_id: str) -> GuideSession:
        row = self._db().execute('SELECT payload FROM guide_sessions WHERE session_id=?', (session_id,)).fetchone()
        if row:
            return GuideSession(**json.loads(row[0]))
        session = GuideSession(session_id)
        self.save(session)
        return session

    def all(self) -> list[GuideSession]:
        return [GuideSession(**json.loads(row[0])) for row in self._db().execute('SELECT payload FROM guide_sessions')]

    def save(self, session: GuideSession, *, lease: str | None = None) -> None:
        connection = self._db()
        try:
            connection.execute('BEGIN IMMEDIATE')
            if lease is not None:
                owner = connection.execute('SELECT owner FROM guide_robot WHERE id=1').fetchone()[0]
                quarantined = connection.execute('SELECT 1 FROM guide_quarantine LIMIT 1').fetchone() is not None
                if lease == 'claim' and (owner not in {None, session.session_id} or quarantined
                                         or session.phase == 'reconciliation_required'):
                    raise GuideError('Robot is busy or requires reconciliation')
                if lease == 'release' and owner not in {None, session.session_id}:
                    raise GuideError('Session does not own the robot')
                if lease != 'release_if_owned' or owner == session.session_id:
                    connection.execute('UPDATE guide_robot SET owner=? WHERE id=1',
                                       (session.session_id if lease == 'claim' else None,))
            payload = asdict(session)
            payload['revision'] += 1
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            if session.revision == 0:
                connection.execute('INSERT INTO guide_sessions VALUES (?,?,?)', (session.session_id, 1, encoded))
            else:
                cursor = connection.execute('UPDATE guide_sessions SET revision=?,payload=? WHERE session_id=? AND revision=?',
                                            (session.revision + 1, encoded, session.session_id, session.revision))
                if cursor.rowcount != 1:
                    raise GuideError('Guide session changed concurrently; query before retrying')
            if session.phase == 'reconciliation_required':
                connection.execute('INSERT OR IGNORE INTO guide_quarantine VALUES (?)', (session.session_id,))
            else:
                connection.execute('DELETE FROM guide_quarantine WHERE session_id=?', (session.session_id,))
            connection.execute('COMMIT')
            session.revision += 1
        except (sqlite3.Error, GuideError) as error:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            if isinstance(error, GuideError):
                raise
            raise GuideError('Guide state could not be persisted safely') from error

    def claim_event(self, session_id: str, event_id: str, payload: dict[str, Any]) -> bool:
        try:
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as error:
            raise GuideError('Event payload must be bounded JSON') from error
        if len(encoded.encode()) > 65536:
            raise GuideError('Event payload is too large')
        connection = self._db()
        try:
            connection.execute('INSERT INTO guide_events VALUES (?,?,?)', (session_id, event_id, encoded))
            return True
        except sqlite3.IntegrityError:
            previous = connection.execute('SELECT payload FROM guide_events WHERE session_id=? AND event_id=?',
                                          (session_id, event_id)).fetchone()
            if previous and previous[0] == encoded:
                return False
            raise GuideError('Event ID was already used with different content') from None
        except sqlite3.Error as error:
            raise GuideError('Guide event could not be claimed') from error

