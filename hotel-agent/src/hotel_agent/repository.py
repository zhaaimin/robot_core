import json
import sqlite3
from dataclasses import asdict

from hotel_agent.errors import HotelWorkflowError
from hotel_agent.models import HotelService


class HotelRepository:
    """Persist business state and the single robot lease in one SQLite commit.

    Revision checks reject another workflow's stale write. Lease acquisition
    shares that transaction, so separate sessions cannot both allocate a robot
    even when applications share the same database. No physical call occurs in
    a database transaction; its intent must be committed first.
    """

    def __init__(self, database: str, robot_id: str) -> None:
        self._database = database
        self._robot_id = robot_id
        self._connection: sqlite3.Connection | None = None

    def start(self) -> None:
        if self._connection is not None:
            return
        try:
            connection = sqlite3.connect(self._database, timeout=0, isolation_level=None)
            connection.execute('CREATE TABLE IF NOT EXISTS hotel_services ('
                               'session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS hotel_robots ('
                               'robot_id TEXT PRIMARY KEY, service_id TEXT, status TEXT NOT NULL)')
            connection.execute('INSERT OR IGNORE INTO hotel_robots VALUES (?, NULL, ?)',
                               (self._robot_id, 'available'))
            self._connection = connection
        except sqlite3.Error as error:
            raise HotelWorkflowError('Hotel database could not be opened') from error

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise HotelWorkflowError('Hotel workflow has not been started')
        return self._connection

    def get(self, session_id: str) -> HotelService:
        row = self._db().execute('SELECT payload FROM hotel_services WHERE session_id = ?',
                                 (session_id,)).fetchone()
        if row is not None:
            return HotelService(**json.loads(row[0]))
        service = HotelService(session_id=session_id)
        self.save(service)
        return service

    def all(self) -> list[HotelService]:
        return [HotelService(**json.loads(row[0])) for row in
                self._db().execute('SELECT payload FROM hotel_services').fetchall()]

    def save(self, service: HotelService, *, robot: str | None = None) -> None:
        connection = self._db()
        try:
            connection.execute('BEGIN IMMEDIATE')
            if robot is not None:
                row = connection.execute('SELECT service_id, status FROM hotel_robots WHERE robot_id = ?',
                                         (self._robot_id,)).fetchone()
                if row is None:
                    raise HotelWorkflowError('Hotel robot is not registered')
                owner, status = row
                if robot == 'claim':
                    if status != 'available' or owner is not None:
                        raise HotelWorkflowError('Robot is busy or quarantined')
                    owner, status = service.service_id, 'allocated'
                elif owner != service.service_id:
                    raise HotelWorkflowError('Service no longer owns its robot allocation')
                elif robot == 'available':
                    owner, status = None, 'available'
                else:
                    status = robot
                connection.execute('UPDATE hotel_robots SET service_id = ?, status = ? WHERE robot_id = ?',
                                   (owner, status, self._robot_id))
            previous = service.revision
            payload = asdict(service)
            payload['revision'] = previous + 1
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            if previous == 0:
                connection.execute('INSERT INTO hotel_services VALUES (?, ?, ?)',
                                   (service.session_id, 1, encoded))
            else:
                cursor = connection.execute(
                    'UPDATE hotel_services SET revision = ?, payload = ? WHERE session_id = ? AND revision = ?',
                    (previous + 1, encoded, service.session_id, previous),
                )
                if cursor.rowcount != 1:
                    raise HotelWorkflowError('Hotel service changed concurrently; refresh before retrying')
            connection.execute('COMMIT')
            service.revision = previous + 1
        except (sqlite3.Error, HotelWorkflowError) as error:
            if connection.in_transaction:
                connection.execute('ROLLBACK')
            if isinstance(error, HotelWorkflowError):
                raise
            raise HotelWorkflowError('Hotel state could not be persisted safely') from error
