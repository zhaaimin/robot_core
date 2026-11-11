import asyncio
import math
import uuid
from contextlib import aclosing
from typing import Any

from guide_agent.catalog import DestinationCatalog
from guide_agent.errors import GuideError
from guide_agent.models import GuideSession
from guide_agent.repository import GuideRepository
from robot_agent_core.ports import Observation, TaskRuntime, failed_observation, task_observation
from robot_agent_runtime.models import SkillCall, Task, TaskRequest, TaskState


_KNOWN_END = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED}
_ACTIVE = {'navigating', 'introducing', 'reconciliation_required'}
_GREETING = '您好，我可以为您介绍地点，也可以带您前往。请问您想了解哪里？'


def _positive(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise GuideError('Timeout must be finite and positive')
    return float(value)


def _identity(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise GuideError('A nonempty bounded stable identifier is required')


def _missing_task_id(session: GuideSession) -> bool:
    return ((session.navigation_intent and not session.navigation_task_id)
            or (session.introduction_intent and not session.introduction_task_id)
            or any(op.get('intent') and not op.get('task_id')
                   for op in session.operations.values() if op['kind'] == 'speech'))


def _unresolved_intent(session: GuideSession) -> bool:
    return _missing_task_id(session) or any(op.get('uncertain') for op in session.operations.values()
                                           if op['kind'] == 'speech')


class GuideWorkflow:
    """Keep consent, durable action intent and physical completion independent.

    Only the trusted application confirms a proposal. Runtime snapshots decide
    arrival and speech completion; a lost submission or unconfirmed stop holds
    the single robot lease without replaying work. Catalog facts are frozen at
    proposal time. Any unresolved action intent blocks automatic follow-up work.
    The application starts and closes the externally owned runtime.
    """

    def __init__(self, database: str, runtime: TaskRuntime, catalog: DestinationCatalog, *,
                 backend: str, task_timeout_s: float = 60, wait_timeout_s: float = 5) -> None:
        if backend not in {'simulator', 'mcp'}:
            raise GuideError('Unknown guide backend')
        self._runtime = runtime
        self._catalog = catalog
        self._backend = backend
        self._task_timeout_s = _positive(task_timeout_s)
        self._wait_timeout_s = _positive(wait_timeout_s)
        self._repository = GuideRepository(database)
        self._lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._changed = asyncio.Event()

    async def start(self) -> None:
        async with self._lock:
            if self._closing:
                raise GuideError('A closed guide workflow cannot restart')
            if self._started:
                return
            self._repository.start()
            self._started = True
            for session in self._repository.all():
                if _unresolved_intent(session):
                    self._uncertain(session, 'Saved action intent has no authoritative physical outcome')
                else:
                    await self._refresh_locked(session)

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._close_task)

    async def _finish_close(self) -> None:
        async with self._lock:
            if not self._started:
                return
            for session in self._repository.all():
                await self._stop_locked(session)
            monitors = list(self._monitors.values())
            for monitor in monitors:
                monitor.cancel()
        if monitors:
            await asyncio.wait(monitors, timeout=self._wait_timeout_s)
        async with self._lock:
            self._repository.close()
            self._started = False

    def _check(self, session_id: str) -> None:
        if not self._started or self._closing:
            raise GuideError('Guide workflow is not running')
        _identity(session_id)

    def _epoch(self, session: GuideSession, expected_epoch: int | None) -> None:
        if expected_epoch is not None and (type(expected_epoch) is not int or expected_epoch != session.epoch):
            raise GuideError('Action belongs to a stale conversation epoch')

    def _version(self, session: GuideSession) -> None:
        if session.fingerprint is not None and session.fingerprint != self._catalog.fingerprint:
            raise GuideError('Guide resources changed; propose a new tour before confirming')

    def _operation(self, session: GuideSession, operation_id: str, kind: str, payload: dict[str, Any]) -> bool:
        _identity(operation_id)
        previous = session.operations.get(operation_id)
        if previous is not None:
            if previous['kind'] != kind or previous['payload'] != payload:
                raise GuideError('Operation ID was already used for a different action')
            return False
        session.operations[operation_id] = {'kind': kind, 'payload': payload}
        return True

    def _save(self, session: GuideSession, *, lease: str | None = None) -> None:
        self._repository.save(session, lease=lease)
        self._changed.set()

    async def status(self, session_id: str) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            session = self._repository.get(session_id)
            await self._refresh_locked(session)
            return session.snapshot()

    async def bind_turn(self, session_id: str, *, operation_id: str,
                        expected_epoch: int | None = None) -> int:
        async with self._lock:
            self._check(session_id)
            _identity(operation_id)
            session = self._repository.get(session_id)
            previous = session.operations.get(operation_id)
            if previous is None:
                self._epoch(session, expected_epoch)
                epoch = session.epoch
            else:
                epoch = previous['payload'].get('epoch')
                if previous['kind'] != 'turn' or type(epoch) is not int:
                    raise GuideError('Operation ID was already used for a different action')
                if expected_epoch is not None and (type(expected_epoch) is not int or expected_epoch != epoch):
                    raise GuideError('Action belongs to a stale conversation epoch')
            self._operation(session, operation_id, 'turn', {'epoch': epoch})
            session.dialogue_operation_id = operation_id
            self._save(session)
            return epoch

    async def saved_turn_epoch(self, session_id: str) -> int:
        async with self._lock:
            self._check(session_id)
            session = self._repository.get(session_id)
            operation = (session.operations.get(session.dialogue_operation_id)
                         if session.dialogue_operation_id is not None else None)
            if operation is None or operation['kind'] != 'turn' or type(operation['payload'].get('epoch')) is not int:
                raise GuideError('No saved conversation turn is available to resume')
            return operation['payload']['epoch']

    async def claim_event(self, session_id: str, event_id: str, payload: dict[str, Any]) -> bool:
        async with self._lock:
            self._check(session_id)
            _identity(event_id)
            if not isinstance(payload, dict):
                raise GuideError('Event payload must be a JSON object')
            return self._repository.claim_event(session_id, event_id, payload)

    async def request_escort(self, session_id: str, destination_id: str, *, operation_id: str,
                             expected_epoch: int | None = None) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            session = self._repository.get(session_id)
            self._epoch(session, expected_epoch)
            if not self._operation(session, operation_id, 'propose', {'destination_id': destination_id}):
                return session.snapshot()
            if session.phase in _ACTIVE:
                raise GuideError('Finish or cancel the current tour before proposing another')
            destination = self._catalog.get(destination_id)
            session.tour_id = str(uuid.uuid4())
            session.destination_id = destination.id
            session.fingerprint = self._catalog.fingerprint
            session.introduction = destination.introduction
            session.target = None
            session.navigation_intent = session.introduction_intent = False
            session.navigation_task_id = session.introduction_task_id = None
            session.stopping = False
            session.error = None
            session.phase = 'awaiting_confirmation'
            self._save(session)
            return session.snapshot()

    async def confirm_escort(self, session_id: str, *, operation_id: str,
                             expected_tour_id: str | None = None) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            session = self._repository.get(session_id)
            if not self._operation(session, operation_id, 'confirm', {'expected_tour_id': expected_tour_id}):
                return session.snapshot()
            if expected_tour_id is None:
                raise GuideError('Confirmation requires the expected destination proposal')
            _identity(expected_tour_id)
            if expected_tour_id != session.tour_id:
                raise GuideError('Confirmation belongs to another destination proposal')
            if session.phase != 'awaiting_confirmation' or not session.destination_id:
                raise GuideError('A current destination proposal must be confirmed first')
            self._version(session)
            session.target = self._catalog.navigation_target(session.destination_id, self._backend)
            session.navigation_intent = True
            session.phase = session.robot_status = 'navigating'
            self._save(session, lease='claim')
            await self._submit_locked(session, 'navigation')
            return session.snapshot()

    def _request(self, session: GuideSession, action: str, *, message: str | None = None,
                 operation_id: str | None = None) -> TaskRequest:
        is_navigation = action == 'navigation'
        return TaskRequest(
            task_type='guide_' + action,
            steps=[SkillCall(skill='navigation.navigate_to' if is_navigation else 'audio.speak',
                             params={'target': session.target} if is_navigation else {'message': message or session.introduction})],
            timeout_s=self._task_timeout_s,
            idempotency_key=f'guide:{session.session_id}:{session.tour_id}:{action}:{operation_id or "automatic"}',
            metadata={'session_id': session.session_id, 'tour_id': session.tour_id, 'guide_action': action,
                      'operation_id': operation_id, 'epoch': session.epoch},
        )

    async def _submit_locked(self, session: GuideSession, action: str) -> None:
        try:
            async with asyncio.timeout(self._wait_timeout_s):
                task = await self._runtime.submit_task(self._request(session, action))
            if not isinstance(task, Task) or not task.id:
                raise GuideError('Runtime did not return an authoritative task')
        except asyncio.CancelledError:
            self._uncertain(session, 'Action submission was interrupted before its task ID was saved')
            raise
        except Exception:
            self._uncertain(session, 'Action submission outcome is unknown; do not replay it')
            return
        setattr(session, action + '_task_id', task.id)
        self._save(session)
        await self._apply_locked(session, action, task)
        self._watch(session)

    def _uncertain(self, session: GuideSession, reason: str) -> None:
        session.phase = 'reconciliation_required'
        session.robot_status = 'quarantined'
        session.error = GuideError(reason).to_dict()
        self._save(session)

    async def _apply_locked(self, session: GuideSession, action: str, task: Task) -> None:
        if not task.state.is_terminal:
            return
        if task.state not in _KNOWN_END:
            self._uncertain(session, 'Runtime reports an unresolved physical outcome')
            return
        if session.stopping:
            return
        if task.state != TaskState.SUCCEEDED:
            session.phase = 'canceled' if task.state == TaskState.CANCELED else 'failed'
            session.robot_status = 'available'
            session.error = task.error.to_dict() if task.error else None
            self._save(session, lease='release')
        elif action == 'navigation':
            if session.introduction_intent:
                return
            if session.fingerprint != self._catalog.fingerprint or self._closing:
                self._uncertain(session, 'New introduction requires the confirmed catalog version')
                return
            session.introduction_intent = True
            session.phase = session.robot_status = 'introducing'
            self._save(session)
            await self._submit_locked(session, 'introduction')
        else:
            session.phase = 'completed'
            session.robot_status = 'available'
            session.error = None
            self._save(session, lease='release')

    async def _refresh_locked(self, session: GuideSession) -> None:
        if _unresolved_intent(session):
            return
        if session.phase not in _ACTIVE:
            return
        action = 'introduction' if session.introduction_intent else 'navigation'
        task_id = getattr(session, action + '_task_id')
        if not task_id:
            return
        try:
            async with asyncio.timeout(self._wait_timeout_s):
                task = await self._runtime.get_task_status(task_id)
            if task.id != task_id:
                raise GuideError('Runtime returned another task')
        except Exception:
            self._uncertain(session, 'Runtime task observation failed')
            return
        current = self._repository.get(session.session_id)
        if current.revision != session.revision:
            session.__dict__.update(current.__dict__)
        if _unresolved_intent(session):
            return
        current_action = 'introduction' if session.introduction_intent else 'navigation'
        if session.phase not in _ACTIVE or getattr(session, current_action + '_task_id') != task_id:
            self._watch(session)
            return
        if session.stopping and task.state in _KNOWN_END:
            await self._stop_locked(session)
            return
        await self._apply_locked(session, action, task)
        self._watch(session)

    def _watch(self, session: GuideSession) -> None:
        if self._closing or session.phase not in {'navigating', 'introducing'}:
            return
        action = 'introduction' if session.introduction_intent else 'navigation'
        task_id = getattr(session, action + '_task_id')
        if not task_id or (task_id in self._monitors and not self._monitors[task_id].done()):
            return
        self._monitors[task_id] = asyncio.create_task(self._follow(session.session_id, task_id))

    async def _follow(self, session_id: str, task_id: str) -> None:
        try:
            async with asyncio.timeout(self._task_timeout_s + self._wait_timeout_s):
                async with aclosing(self._runtime.subscribe_task_events(task_id)) as events:
                    async for event in events:
                        if event.task_id != task_id:
                            continue
                        try:
                            terminal = TaskState(event.data.get('state')).is_terminal
                        except (TypeError, ValueError):
                            terminal = False
                        if terminal:
                            async with self._lock:
                                if self._closing:
                                    return
                                session = self._repository.get(session_id)
                                if session.phase in {'navigating', 'introducing'} and task_id in {
                                        session.navigation_task_id, session.introduction_task_id}:
                                    await self._refresh_locked(session)
                            return
            reason = 'Runtime event stream ended without a confirmed task result'
        except asyncio.CancelledError:
            raise
        except Exception:
            reason = 'Runtime task observation was interrupted'
        async with self._lock:
            if not self._closing:
                session = self._repository.get(session_id)
                action = 'introduction' if session.introduction_intent else 'navigation'
                if session.phase in {'navigating', 'introducing'} and getattr(session, action + '_task_id') == task_id:
                    self._uncertain(session, reason)

    async def cancel(self, session_id: str, *, operation_id: str) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            session = self._repository.get(session_id)
            if not self._operation(session, operation_id, 'cancel', {}):
                return session.snapshot()
            session.epoch += 1
            session.stopping = True
            self._save(session)
            await self._stop_locked(session)
            return session.snapshot()

    async def _stop_locked(self, session: GuideSession) -> None:
        active = session.phase in _ACTIVE
        if not active and not any(op.get('task_id') and (op.get('uncertain') or not op.get('settled'))
                                  for op in session.operations.values()):
            if session.phase == 'awaiting_confirmation' or session.stopping:
                session.phase = 'canceled'
                session.destination_id = None
                session.robot_status = 'available'
                self._save(session)
            return
        session.stopping = True
        self._save(session)
        unknown = _missing_task_id(session)
        task_ids = {value for value in (session.navigation_task_id, session.introduction_task_id) if value} if active else set()
        task_ids.update(op['task_id'] for op in session.operations.values()
                        if op.get('task_id') and (op.get('uncertain') or not op.get('settled')))
        for task_id in task_ids:
            try:
                async with asyncio.timeout(self._wait_timeout_s):
                    task = await self._runtime.cancel_task(task_id)
                    if not task.state.is_terminal:
                        task = await self._wait_task(task_id)
                if task.id != task_id or task.state not in _KNOWN_END:
                    unknown = True
                else:
                    for op in session.operations.values():
                        if op.get('task_id') == task_id:
                            op['settled'] = True
                            op.pop('uncertain', None)
            except Exception:
                unknown = True
        if unknown:
            self._uncertain(session, 'Runtime has not confirmed every physical action stopped')
        else:
            session.phase = 'canceled'
            session.destination_id = None
            session.robot_status = 'available'
            session.error = None
            self._save(session, lease='release_if_owned' if active else None)

    async def _speech_locked(self, session: GuideSession, message: str, operation_id: str) -> str | None:
        op = session.operations[operation_id]
        if op.get('intent'):
            return op.get('task_id')
        op['intent'] = True
        self._save(session)
        try:
            async with asyncio.timeout(self._wait_timeout_s):
                task = await self._runtime.submit_task(self._request(session, 'speech', message=message, operation_id=operation_id))
            if not isinstance(task, Task) or not task.id:
                raise GuideError('Runtime returned no speech task')
        except asyncio.CancelledError:
            self._uncertain(session, 'Speech submission was interrupted before saving its task ID')
            raise
        except Exception:
            self._uncertain(session, 'Speech submission outcome is unknown; do not replay it')
            return None
        op['task_id'] = task.id
        op['settled'] = task.state in _KNOWN_END
        if task.state.is_terminal and task.state not in _KNOWN_END:
            op['uncertain'] = True
            self._uncertain(session, 'Runtime reports an unresolved speech outcome')
        else:
            self._save(session)
        return task.id

    async def speak(self, session_id: str, message: str, *, operation_id: str,
                    expected_epoch: int | None = None) -> Observation:
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise GuideError('Speech requires 1 to 4000 characters')
        async with self._lock:
            self._check(session_id)
            session = self._repository.get(session_id)
            self._epoch(session, expected_epoch)
            self._version(session)
            self._operation(session, operation_id, 'speech', {'message': message})
            task_id = await self._speech_locked(session, message, operation_id)
        return await self._observe_speech(session_id, operation_id, task_id)

    async def _wait_task(self, task_id: str) -> Task:
        task = await self._runtime.get_task_status(task_id)
        if task.state.is_terminal:
            return task
        async with aclosing(self._runtime.subscribe_task_events(task_id)) as events:
            async for event in events:
                try:
                    terminal = event.task_id == task_id and TaskState(event.data.get('state')).is_terminal
                except (TypeError, ValueError):
                    terminal = False
                if terminal:
                    return await self._runtime.get_task_status(task_id)
        raise GuideError('Runtime task event stream ended before completion')

    async def _observe_speech(self, session_id: str, operation_id: str, task_id: str | None) -> Observation:
        if not task_id:
            return failed_observation('guide_speak', GuideError('Speech submission has no confirmed task ID').info)
        try:
            async with asyncio.timeout(self._wait_timeout_s):
                task = await self._wait_task(task_id)
            observed = task_observation('guide_speak', task)
            async with self._lock:
                if self._started and not self._closing:
                    session = self._repository.get(session_id)
                    op = session.operations[operation_id]
                    op['settled'] = task.state in _KNOWN_END
                    if task.state.is_terminal and task.state not in _KNOWN_END:
                        op['uncertain'] = True
                        self._uncertain(session, 'Runtime reports an unresolved speech outcome')
                    else:
                        self._save(session)
            return observed
        except Exception:
            return failed_observation('guide_speak', GuideError('Speech was submitted; completion is not yet confirmed').info, task_id=task_id)

    async def wake(self, session_id: str, *, operation_id: str) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            session = self._repository.get(session_id)
            if not self._operation(session, operation_id, 'wake', {}):
                return session.snapshot()
            if session.awake:
                self._save(session)
                return session.snapshot()
            session.awake = True
            greeting_operation = 'greeting:' + operation_id
            self._operation(session, greeting_operation, 'speech', {'message': _GREETING})
            task_id = await self._speech_locked(session, _GREETING, greeting_operation)
        await self._observe_speech(session_id, greeting_operation, task_id)
        return await self.status(session_id)

    async def wait_for_settled(self, session_id: str, *, timeout_s: float = 2) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + _positive(timeout_s)
        while True:
            state = await self.status(session_id)
            if state['phase'] not in {'navigating', 'introducing'}:
                return state
            self._changed.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return state
            try:
                async with asyncio.timeout(remaining):
                    await self._changed.wait()
            except TimeoutError:
                return await self.status(session_id)

