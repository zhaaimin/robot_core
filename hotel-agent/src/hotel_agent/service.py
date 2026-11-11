import asyncio
import math
from typing import Any

from hotel_agent.backend import HotelBackend, SimulatedHotel, VerifiedStay
from hotel_agent.errors import HotelWorkflowError
from hotel_agent.models import HotelService, ServicePhase
from hotel_agent.repository import HotelRepository
from hotel_agent.task_coordination import (
    ESCORT, RETURN, HotelTaskCoordinator, Navigation, NavigationRules,
)
from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.ports import Observation, TaskRuntime, failed_observation, task_observation
from robot_agent_runtime.errors import AgentRuntimeError, TaskWaitTimeout
from robot_agent_runtime.models import SkillCall, Task, TaskRequest, TaskState


_MAX_ANNOUNCE_CHARS = 200


class HotelWorkflow:
    """Guard hotel stages independently from the runtime's physical task state.

    Guest confirmations are trusted application events, never model arguments.
    An intent is durable before submitting motion. Missing submit outcomes and
    unconfirmed stops quarantine the robot instead of replaying an action or
    advertising it to another guest. Arrival completes guest service; the return
    task has its own outcome and cannot rewrite that completion.

    Asking the runtime anything is the task coordinator's job: it owns the
    timeouts, the queries and the watchers, and reports either a confirmed task
    or the fact that nothing is confirmed. This class only says what those
    answers mean for a guest, so phase rules, the consent gate and the robot
    lease stay together here.

    The caller starts the runtime first and closes this workflow before closing
    it. Closing requests bounded stops; it does not claim physical effects have
    ended when the runtime cannot confirm that fact. A confirmed stop away from
    a successful return still holds the allocation as needs_reposition: stopping
    does not prove the robot is at the next guest's meeting point.
    """

    def __init__(
        self, database: str, runtime: TaskRuntime, *,
        hotel: HotelBackend | None = None, task_timeout_s: float = 60.0,
        wait_timeout_s: float = 5.0,
    ) -> None:
        if (isinstance(task_timeout_s, bool) or not isinstance(task_timeout_s, (int, float))
                or not math.isfinite(task_timeout_s) or task_timeout_s <= 0):
            raise AgentConfigurationError('task_timeout_s must be finite and positive')
        if (isinstance(wait_timeout_s, bool) or not isinstance(wait_timeout_s, (int, float))
                or not math.isfinite(wait_timeout_s) or wait_timeout_s <= 0):
            raise AgentConfigurationError('wait_timeout_s must be finite and positive')
        self._runtime = runtime
        self._hotel = hotel if hotel is not None else SimulatedHotel()
        self._task_timeout_s = float(task_timeout_s)
        self._wait_timeout_s = float(wait_timeout_s)
        self._repository = HotelRepository(database, self._hotel.robot_id)
        self._lock = asyncio.Lock()
        self._started = False
        self._closing = False
        self._coordinator = HotelTaskCoordinator(
            runtime=runtime, repository=self._repository, lock=self._lock,
            timeout_s=self._task_timeout_s,
            rules=NavigationRules(
                request=self._navigation_request,
                is_active=self._navigation_active,
                ended=self._navigation_ended,
                uncertain=self._uncertain,
                is_closing=lambda: self._closing,
            ),
        )

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            if self._closing:
                raise HotelWorkflowError('A closed hotel workflow cannot be restarted')
            self._repository.start()
            self._started = True
            for service in self._repository.all():
                if service.escort_intent and not service.runtime_task_id:
                    self._uncertain(service, ESCORT, 'Saved navigation intent has no confirmed runtime task ID')
                if service.return_intent and not service.return_task_id:
                    self._uncertain(service, RETURN, 'Saved return intent has no confirmed runtime task ID')
                await self._coordinator.refresh(service)

    async def close(self) -> None:
        async with self._lock:
            if not self._started or self._closing:
                return
            self._closing = True
            for service in self._repository.all():
                if service.phase == ServicePhase.ESCORTING.value and service.runtime_task_id:
                    await self._stop_locked(service, ServicePhase.CANCELED.value)
                if service.robot_status == 'returning' and service.return_task_id:
                    await self._stop_locked(service, ServicePhase.COMPLETED.value, navigation=RETURN)
        await self._coordinator.cancel_monitors()
        async with self._lock:
            self._repository.close()
            self._started = False

    def _check(self, session_id: str) -> None:
        if not self._started or self._closing:
            raise HotelWorkflowError('Hotel workflow is not running')
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
            raise HotelWorkflowError('A nonempty bounded session_id is required')

    def _operation(self, operation_id: str) -> None:
        if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 256:
            raise HotelWorkflowError('A stable bounded operation_id is required')

    def _phase(self, service: HotelService, *allowed: ServicePhase) -> None:
        if service.phase not in {phase.value for phase in allowed}:
            raise HotelWorkflowError('Action is not allowed in the current hotel service phase',
                                     details={'phase': service.phase})

    async def snapshot(self, session_id: str) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            return self._repository.get(session_id).snapshot()

    async def refresh(self, session_id: str) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            service = self._repository.get(session_id)
            await self._coordinator.refresh(service)
            return service.snapshot()

    async def confirm_reception(
        self, session_id: str, receipt_code: str, *, operation_id: str,
    ) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            self._operation(operation_id)
            service = self._repository.get(session_id)
            if not isinstance(receipt_code, str) or not receipt_code or len(receipt_code) > 256:
                raise HotelWorkflowError('Provide a valid reception receipt code')
            if service.receipt_code is not None:
                if service.receipt_code != receipt_code:
                    raise HotelWorkflowError('A confirmed service cannot change its reception receipt')
                return service.snapshot()
            self._phase(service, ServicePhase.RECEPTION)
            try:
                async with asyncio.timeout(min(self._task_timeout_s, 2.0)):
                    stay = await self._hotel.verify_receipt(receipt_code)
            except HotelWorkflowError:
                raise
            except Exception as error:
                raise HotelWorkflowError('Hotel system could not verify the reception receipt') from error
            self._validate_stay(stay, receipt_code)
            service.receipt_code = stay.receipt_code
            service.room_id = stay.room_id
            service.destination_id = stay.destination_id
            service.meeting_point_id = getattr(self._hotel, 'meeting_point_id', None)
            service.meeting_point = getattr(self._hotel, 'meeting_point', None)
            service.floor = stay.floor
            service.phase = ServicePhase.AWAITING_CONSENT.value
            self._repository.save(service)
            return service.snapshot()

    def _validate_stay(self, stay: VerifiedStay, receipt_code: str) -> None:
        if not isinstance(stay, VerifiedStay):
            raise HotelWorkflowError('Hotel system did not return a verified stay')
        for value in (stay.receipt_code, stay.room_id, stay.destination_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise HotelWorkflowError('Hotel stay contains an invalid receipt, room or destination')
        if stay.receipt_code != receipt_code:
            raise HotelWorkflowError('Hotel stay does not match the verified receipt')
        supported_floor = getattr(self._hotel, 'supported_floor', None)
        if (type(supported_floor) is not int or type(stay.floor) is not int
                or stay.floor != supported_floor):
            raise HotelWorkflowError('Hotel escort supports only the configured same floor',
                                     details={'supported_floor': supported_floor})

    async def record_guest_event(self, session_id: str, event: str) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            service = self._repository.get(session_id)
            if event == 'consent':
                if service.consent_confirmed:
                    return service.snapshot()
                self._phase(service, ServicePhase.AWAITING_CONSENT)
                service.consent_confirmed = True
                service.phase = ServicePhase.WAITING_FOR_ROBOT.value
            elif event == 'ready':
                if service.guest_ready:
                    return service.snapshot()
                self._phase(service, ServicePhase.WAITING_FOR_GUEST)
                service.guest_ready = True
            elif event == 'arrived':
                if service.arrival_confirmed:
                    return service.snapshot()
                await self._coordinator.refresh(service)
                self._phase(service, ServicePhase.AWAITING_ARRIVAL_CONFIRMATION)
                service.arrival_confirmed = True
                service.phase = ServicePhase.COMPLETED.value
                service.return_intent = True
                service.robot_status = 'returning'
                self._repository.save(service, robot='returning')
                await self._coordinator.submit(service, RETURN)
                return service.snapshot()
            else:
                raise HotelWorkflowError('Unknown guest event; expected consent, ready or arrived')
            self._repository.save(service)
            return service.snapshot()

    async def assign_robot(self, session_id: str, *, operation_id: str) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            self._operation(operation_id)
            service = self._repository.get(session_id)
            if service.robot_id is not None:
                self._phase(service, ServicePhase.WAITING_FOR_GUEST, ServicePhase.ESCORTING,
                            ServicePhase.AWAITING_ARRIVAL_CONFIRMATION, ServicePhase.COMPLETED)
                return service.snapshot()
            self._phase(service, ServicePhase.WAITING_FOR_ROBOT)
            if not service.consent_confirmed:
                raise HotelWorkflowError('Guest consent is required before assigning a robot')
            if not service.meeting_point_id or not service.meeting_point:
                raise HotelWorkflowError('Hotel meeting point is missing; request human assistance')
            service.robot_id = self._hotel.robot_id
            service.robot_status = 'allocated'
            service.phase = ServicePhase.WAITING_FOR_GUEST.value
            self._repository.save(service, robot='claim')
            return service.snapshot()

    async def start_escort(self, session_id: str, *, operation_id: str) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            self._operation(operation_id)
            service = self._repository.get(session_id)
            if service.escort_intent:
                return service.snapshot()
            self._phase(service, ServicePhase.WAITING_FOR_GUEST)
            if not service.consent_confirmed or not service.guest_ready or not service.robot_id:
                raise HotelWorkflowError('Consent, robot assignment and guest readiness are all required')
            service.escort_intent = True
            service.phase = ServicePhase.ESCORTING.value
            service.robot_status = 'escorting'
            self._repository.save(service, robot='escorting')
            await self._coordinator.submit(service, ESCORT)
            return service.snapshot()

    async def announce(
        self, session_id: str, message: str, *, operation_id: str,
    ) -> Observation:
        """Speak an already-known hotel fact without entering the phase machine.

        Speech needs the speaker, not the movement lease, so it stays outside the
        robot allocation and cannot be blocked by another guest's escort. Its
        outcome is never a business phase: nothing here records arrival or
        completion. A local wait deadline returns the saved task for observation
        instead of quarantining the robot or replaying the sentence, because an
        unverified wait says nothing about whether the speaker finished.
        """
        self._check(session_id)
        self._operation(operation_id)
        if (not isinstance(message, str) or not message.strip()
                or len(message) > _MAX_ANNOUNCE_CHARS):
            raise HotelWorkflowError(
                f'An announcement of 1 to {_MAX_ANNOUNCE_CHARS} characters is required')
        async with self._lock:
            service = self._repository.get(session_id)
            request = TaskRequest(
                task_type='hotel_announce',
                steps=[SkillCall(skill='audio.speak', params={'message': message})],
                timeout_s=self._task_timeout_s,
                idempotency_key=f'hotel:{service.service_id}:announce:{operation_id}',
                metadata={'service_id': service.service_id, 'session_id': session_id,
                          'hotel_action': 'announce', 'operation_id': operation_id},
            )
        try:
            task = await self._runtime.submit_and_wait(request, timeout_s=self._wait_timeout_s)
        except TaskWaitTimeout as error:
            task_id = str(error.info.details.get('task_id', ''))
            try:
                observed = task_observation('hotel_announce', await self._runtime.get_task_status(task_id))
            except AgentRuntimeError:
                return failed_observation('hotel_announce', error.info, task_id=task_id)
            observed['error'] = error.to_dict()
            observed['requires_attention'] = True
            return observed
        except AgentRuntimeError as error:
            return failed_observation('hotel_announce', error.info)
        return task_observation('hotel_announce', task)

    async def request_human(
        self, session_id: str, reason: str, *, operation_id: str,
    ) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            self._operation(operation_id)
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                raise HotelWorkflowError('A bounded human assistance reason is required')
            service = self._repository.get(session_id)
            if service.phase in {ServicePhase.COMPLETED.value, ServicePhase.CANCELED.value}:
                service.human_reason = reason
                self._repository.save(service)
                return service.snapshot()
            self._phase(service, ServicePhase.RECEPTION, ServicePhase.AWAITING_CONSENT,
                        ServicePhase.WAITING_FOR_ROBOT, ServicePhase.WAITING_FOR_GUEST,
                        ServicePhase.ESCORTING, ServicePhase.AWAITING_ARRIVAL_CONFIRMATION,
                        ServicePhase.HUMAN_ASSISTANCE, ServicePhase.RECONCILIATION_REQUIRED)
            service.human_reason = reason
            await self._stop_locked(service, ServicePhase.HUMAN_ASSISTANCE.value)
            return service.snapshot()

    async def cancel(self, session_id: str, *, operation_id: str) -> dict[str, Any]:
        async with self._lock:
            self._check(session_id)
            self._operation(operation_id)
            service = self._repository.get(session_id)
            if service.phase == ServicePhase.CANCELED.value:
                return service.snapshot()
            self._phase(service, ServicePhase.RECEPTION, ServicePhase.AWAITING_CONSENT,
                        ServicePhase.WAITING_FOR_ROBOT, ServicePhase.WAITING_FOR_GUEST,
                        ServicePhase.ESCORTING, ServicePhase.AWAITING_ARRIVAL_CONFIRMATION,
                        ServicePhase.HUMAN_ASSISTANCE, ServicePhase.RECONCILIATION_REQUIRED)
            await self._stop_locked(service, ServicePhase.CANCELED.value)
            return service.snapshot()

    async def wait_for_settled(self, session_id: str, *, timeout_s: float = 2.0) -> dict[str, Any]:
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise HotelWorkflowError('timeout_s must be finite and positive')
        await self.refresh(session_id)
        async with self._lock:
            self._check(session_id)
            service = self._repository.get(session_id)
            tasks = self._coordinator.pending_monitors(service.service_id)
        if tasks:
            await asyncio.wait(tasks, timeout=timeout_s)
        return await self.refresh(session_id)

    def _navigation_request(self, service: HotelService, navigation: Navigation) -> TaskRequest:
        """Build the physical request for one navigation kind.

        The destination and the task type are hotel facts; the key is the
        durable identity that lets a replayed intent reuse the running task, so
        it stays tied to the service rather than to whichever node asked.
        """
        destination = (service.destination_id if navigation.kind == 'escort'
                       else self._hotel.return_destination_id)
        return TaskRequest(
            task_type=f'hotel_{navigation.kind}',
            steps=[SkillCall(skill='navigation.navigate_to', params={'target': destination})],
            timeout_s=self._task_timeout_s,
            idempotency_key=f'hotel:{service.service_id}:{navigation.kind}',
            metadata={'service_id': service.service_id, 'session_id': service.session_id,
                      'hotel_action': navigation.kind},
        )

    def _navigation_active(self, service: HotelService, navigation: Navigation) -> bool:
        """Say whether a saved navigation task still speaks for the robot."""
        if navigation.kind == 'escort':
            return service.phase == ServicePhase.ESCORTING.value
        return service.robot_status == 'returning'

    def _navigation_ended(self, service: HotelService, navigation: Navigation, task: Task) -> None:
        """Map a confirmed terminal task onto guest service state.

        Only the escort phase is rewritten by navigation results, and a return
        result never touches that phase: arrival already completed the guest.
        """
        if not task.state.is_terminal:
            return
        error = task.error.to_dict() if task.error else None
        if navigation.kind == 'return':
            if task.state == TaskState.SUCCEEDED:
                service.robot_status = 'available'
                service.return_error = None
                self._repository.save(service, robot='available')
            else:
                service.robot_status = ('needs_reposition' if task.state in {
                    TaskState.CANCELED, TaskState.FAILED,
                } else 'quarantined')
                service.return_error = error or HotelWorkflowError(
                    'Robot return did not complete successfully', details={'state': task.state.value},
                ).to_dict()
                self._repository.save(service, robot=service.robot_status)
            return
        if service.phase != ServicePhase.ESCORTING.value:
            return
        service.error = error
        if task.state == TaskState.SUCCEEDED:
            service.phase = ServicePhase.AWAITING_ARRIVAL_CONFIRMATION.value
            service.robot_status = 'at_room'
            self._repository.save(service, robot='at_room')
        elif task.state == TaskState.CANCELED:
            service.phase = ServicePhase.CANCELED.value
            service.robot_status = 'needs_reposition'
            self._repository.save(service, robot='needs_reposition')
        else:
            service.phase = (ServicePhase.HUMAN_ASSISTANCE.value if task.state == TaskState.FAILED
                             else ServicePhase.RECONCILIATION_REQUIRED.value)
            service.error = error or HotelWorkflowError(
                'Navigation ended without confirmed success', details={'state': task.state.value},
            ).to_dict()
            service.robot_status = ('needs_reposition' if task.state == TaskState.FAILED
                                    else 'quarantined')
            self._repository.save(service, robot=service.robot_status)

    def _uncertain(self, service: HotelService, navigation: Navigation, message: str) -> None:
        """Quarantine a navigation whose real-world outcome cannot be confirmed.

        An unconfirmed escort is never re-sent, and the robot is not offered to
        another guest, because the physical effect may still be in progress.
        """
        if navigation.kind == 'escort' and service.phase not in {
            ServicePhase.ESCORTING.value, ServicePhase.RECONCILIATION_REQUIRED.value,
        }:
            return
        if navigation.kind == 'return' and service.robot_status not in {'returning', 'quarantined'}:
            return
        error = HotelWorkflowError(message).to_dict()
        if navigation.kind == 'escort':
            service.phase = ServicePhase.RECONCILIATION_REQUIRED.value
            service.error = error
        else:
            service.return_error = error
        service.robot_status = 'quarantined'
        self._repository.save(service, robot='quarantined')

    async def _stop_locked(
        self, service: HotelService, target: str, *, navigation: Navigation = ESCORT,
    ) -> None:
        """Move a service to `target`, but only as far as the runtime confirms.

        The phase is the hotel's decision; where the robot physically is, is not.
        So a confirmed stop away from a meeting point keeps the allocation as
        needs_reposition, and a stop the runtime cannot confirm quarantines
        instead of claiming the guest's route was cancelled.
        """
        if service.robot_status == 'quarantined':
            if navigation.kind == 'escort':
                service.phase = ServicePhase.RECONCILIATION_REQUIRED.value
            self._repository.save(service)
            return
        if navigation.kind == 'escort' and service.escort_intent and service.runtime_task_id is None:
            self._uncertain(service, navigation, 'Navigation submission has no confirmed task to stop')
            return
        robot_status = 'needs_reposition' if service.escort_intent else 'available'
        stopped = await self._coordinator.stop(service, navigation)
        if stopped.requested and stopped.task is None:
            return
        if stopped.task is not None:
            task = stopped.task
            if navigation.kind == 'return':
                service.phase = ServicePhase.COMPLETED.value
                if task.state == TaskState.SUCCEEDED:
                    robot_status = 'available'
                    service.return_error = None
                else:
                    service.return_error = task.error.to_dict() if task.error else HotelWorkflowError(
                        'Robot return stopped before reaching its meeting point',
                        details={'state': task.state.value},
                    ).to_dict()
            else:
                service.phase = target
                service.error = task.error.to_dict() if task.error else None
        else:
            service.phase = target
        if service.robot_id is not None and service.robot_status != 'available':
            service.robot_status = robot_status
            self._repository.save(service, robot=robot_status)
        else:
            self._repository.save(service)
