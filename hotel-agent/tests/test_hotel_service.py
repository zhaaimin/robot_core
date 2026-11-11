import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TYPE_CHECKING

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import SkillCall, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime

if TYPE_CHECKING:
    from hotel_agent.service import HotelWorkflow


class RecordingRuntime:
    def __init__(self, runtime: RobotRuntime) -> None:
        self.runtime = runtime
        self.requests: list[TaskRequest] = []

    async def submit_task(self, request: TaskRequest) -> Any:
        self.requests.append(request)
        return await self.runtime.submit_task(request)

    async def submit_and_wait(self, request: TaskRequest, *, timeout_s: float | None = None) -> Any:
        self.requests.append(request)
        return await self.runtime.submit_and_wait(request, timeout_s=timeout_s)

    async def get_task_status(self, task_id: str) -> Any:
        return await self.runtime.get_task_status(task_id)

    def subscribe_task_events(self, task_id: str) -> Any:
        return self.runtime.subscribe_task_events(task_id)

    async def cancel_task(self, task_id: str) -> Any:
        return await self.runtime.cancel_task(task_id)


@asynccontextmanager
async def running(
    *, delay_s: float = 0.0, fail: bool = False, wait_timeout_s: float = 5.0,
) -> AsyncIterator[tuple['HotelWorkflow', Simulator, RecordingRuntime, str]]:
    from hotel_agent.service import HotelWorkflow

    simulator = Simulator(delay_s=delay_s, fail_skills={'navigation.navigate_to'} if fail else set())
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    recorded = RecordingRuntime(runtime)
    with tempfile.TemporaryDirectory() as directory:
        database = str(Path(directory) / 'hotel.sqlite')
        workflow = HotelWorkflow(database, recorded, wait_timeout_s=wait_timeout_s)
        await workflow.start()
        try:
            yield workflow, simulator, recorded, database
        finally:
            await workflow.close()
            await runtime.close()


async def ready(workflow: 'HotelWorkflow', session: str = 'guest-1') -> dict[str, Any]:
    await workflow.confirm_reception(session, 'DEMO-001', operation_id='reception')
    await workflow.record_guest_event(session, 'consent')
    await workflow.assign_robot(session, operation_id='assign')
    return await workflow.record_guest_event(session, 'ready')


async def robot_state(runtime: RecordingRuntime) -> dict[str, Any]:
    task = await runtime.runtime.submit_and_wait(TaskRequest(
        task_type='hotel_test_state', steps=[SkillCall(skill='robot.get_state')],
    ), timeout_s=1)
    assert task.state == TaskState.SUCCEEDED
    return task.result['steps'][0]['output']['state']


async def test_reception_uses_verified_hotel_receipt_and_rejects_arbitrary_rooms() -> None:
    from hotel_agent.errors import HotelWorkflowError

    async with running() as (workflow, _, _, _):
        initial = await workflow.snapshot('guest-1')
        assert initial['phase'] == 'reception'
        with pytest.raises(HotelWorkflowError):
            await workflow.confirm_reception('guest-1', 'room-9999', operation_id='bad')
        assert (await workflow.snapshot('guest-1'))['phase'] == 'reception'
        confirmed = await workflow.confirm_reception('guest-1', 'DEMO-001', operation_id='verified')
        assert confirmed['phase'] == 'awaiting_consent'
        assert confirmed['room_id'] == '0108'
        assert confirmed['destination_id'] == 'room-0108-door'
        assert confirmed['meeting_point_id'] == 'hotel-lobby'
        assert confirmed['meeting_point'] == '大堂前台会合点'
        assert confirmed['consent_confirmed'] is False
        assert json.loads(json.dumps(confirmed)) == confirmed
        with pytest.raises(HotelWorkflowError):
            await workflow.confirm_reception('guest-1', 'DEMO-002', operation_id='different-room')


async def test_consent_and_ready_are_required_before_physical_submission() -> None:
    from hotel_agent.errors import HotelWorkflowError

    async with running() as (workflow, simulator, _, _):
        await workflow.confirm_reception('guest-1', 'DEMO-001', operation_id='receipt')
        with pytest.raises(HotelWorkflowError):
            await workflow.assign_robot('guest-1', operation_id='early')
        with pytest.raises(HotelWorkflowError):
            await workflow.record_guest_event('guest-1', 'arrived')
        await workflow.record_guest_event('guest-1', 'consent')
        assert (await workflow.assign_robot('guest-1', operation_id='assign'))['phase'] == 'waiting_for_guest'
        with pytest.raises(HotelWorkflowError):
            await workflow.start_escort('guest-1', operation_id='too-soon')
        assert simulator.skills['navigation.navigate_to'].calls == 0
        await workflow.record_guest_event('guest-1', 'ready')
        assert (await workflow.start_escort('guest-1', operation_id='start'))['runtime_task_id']


async def test_navigation_success_waits_for_guest_and_return_is_separate() -> None:
    async with running() as (workflow, simulator, runtime, _):
        prepared = await ready(workflow)
        assert prepared['guest_ready'] is True
        started = await workflow.start_escort('guest-1', operation_id='start')
        settled = await workflow.wait_for_settled('guest-1')
        assert settled['phase'] == 'awaiting_arrival_confirmation'
        assert settled['arrival_confirmed'] is False
        assert runtime.requests[0].steps[0].params == {'target': 'room-0108-door'}
        assert runtime.requests[0].idempotency_key == f"hotel:{settled['service_id']}:escort"
        completed = await workflow.record_guest_event('guest-1', 'arrived')
        assert completed['phase'] == 'completed'
        assert completed['arrival_confirmed'] is True
        returned = await workflow.wait_for_settled('guest-1')
        assert returned['phase'] == 'completed'
        assert returned['return_task_id'] != started['runtime_task_id']
        assert returned['robot_status'] == 'available'
        assert simulator.skills['navigation.navigate_to'].calls == 2
        assert runtime.requests[-1].steps[0].params == {'target': 'hotel-lobby'}
        # Both keys are durable identities: moving where they are built must not
        # change them, or already-verified restart data would replay navigation.
        assert runtime.requests[-1].idempotency_key == f"hotel:{returned['service_id']}:return"


async def test_navigation_tasks_carry_their_own_identity_and_service_metadata() -> None:
    """Navigation is pinned to the service, not to the node that asked for it.

    The idempotency key and metadata are what let a replayed start reuse the
    running task, so moving the request builder to another module must keep the
    exact strings the runtime already persisted.
    """
    async with running(delay_s=30) as (workflow, _, runtime, _):
        await ready(workflow)
        started = await workflow.start_escort('guest-1', operation_id='start')
        request = runtime.requests[0]
        assert request.task_type == 'hotel_escort'
        assert request.idempotency_key == f"hotel:{started['service_id']}:escort"
        assert request.metadata == {
            'service_id': started['service_id'], 'session_id': 'guest-1', 'hotel_action': 'escort',
        }
        assert [call.skill for call in request.steps] == ['navigation.navigate_to']
        replay = await workflow.start_escort('guest-1', operation_id='another-node')
        assert replay['runtime_task_id'] == started['runtime_task_id']
        assert len(runtime.requests) == 1


async def test_replayed_start_reuses_the_persisted_task_with_new_operation_ids() -> None:
    async with running() as (workflow, simulator, runtime, _):
        await ready(workflow)
        first = await workflow.start_escort('guest-1', operation_id='start')
        await workflow.wait_for_settled('guest-1')
        replay = await workflow.start_escort('guest-1', operation_id='retry-node')
        assert replay['runtime_task_id'] == first['runtime_task_id']
        assert len(runtime.requests) == 1
        assert simulator.skills['navigation.navigate_to'].calls == 1


async def test_robot_cannot_be_assigned_to_two_concurrent_guest_sessions() -> None:
    from hotel_agent.errors import HotelWorkflowError

    async with running() as (workflow, _, _, _):
        for session, receipt in [('guest-1', 'DEMO-001'), ('guest-2', 'DEMO-002')]:
            await workflow.confirm_reception(session, receipt, operation_id=f'receipt-{session}')
            await workflow.record_guest_event(session, 'consent')
        results = await asyncio.gather(
            workflow.assign_robot('guest-1', operation_id='assign-1'),
            workflow.assign_robot('guest-2', operation_id='assign-2'),
            return_exceptions=True,
        )
        assert sum(isinstance(item, dict) for item in results) == 1
        assert sum(isinstance(item, HotelWorkflowError) for item in results) == 1


async def test_navigation_failure_escalates_without_claiming_guest_arrival() -> None:
    async with running(fail=True) as (workflow, _, _, _):
        await ready(workflow)
        await workflow.start_escort('guest-1', operation_id='start')
        settled = await workflow.wait_for_settled('guest-1')
        assert settled['phase'] == 'human_assistance'
        assert settled['error']['code'] == 'SKILL_EXECUTION_FAILED'
        assert settled['robot_status'] == 'needs_reposition'
        assert settled['arrival_confirmed'] is False


async def test_return_failure_cannot_retroactively_fail_completed_guest_service() -> None:
    async with running() as (workflow, simulator, _, _):
        await ready(workflow)
        await workflow.start_escort('guest-1', operation_id='start')
        await workflow.wait_for_settled('guest-1')
        simulator.skills['navigation.navigate_to']._failures.add('navigation.navigate_to')
        await workflow.record_guest_event('guest-1', 'arrived')
        settled = await workflow.wait_for_settled('guest-1')
        assert settled['phase'] == 'completed'
        assert settled['error'] is None
        assert settled['return_error']['code'] == 'SKILL_EXECUTION_FAILED'
        assert settled['robot_status'] == 'needs_reposition'


async def test_confirmed_cancel_stops_navigation_and_repeated_cancel_is_harmless() -> None:
    async with running(delay_s=30) as (workflow, _, _, _):
        await ready(workflow)
        await workflow.start_escort('guest-1', operation_id='start')
        canceled = await workflow.cancel('guest-1', operation_id='cancel')
        assert canceled['phase'] == 'canceled'
        assert canceled['robot_status'] == 'needs_reposition'
        assert (await workflow.cancel('guest-1', operation_id='again'))['phase'] == 'canceled'


async def test_uncertain_cancel_retains_robot_allocation_for_reconciliation() -> None:
    from hotel_agent.errors import HotelWorkflowError

    async with running(delay_s=30) as (workflow, _, runtime, _):
        await ready(workflow)
        await workflow.start_escort('guest-1', operation_id='start')
        async def unknown_cancel(task_id: str) -> Any:
            task = await runtime.runtime.get_task_status(task_id)
            return task.model_copy(update={'state': TaskState.RECONCILIATION_REQUIRED})
        runtime.cancel_task = unknown_cancel
        canceled = await workflow.cancel('guest-1', operation_id='cancel')
        assert canceled['phase'] == 'reconciliation_required'
        assert canceled['robot_status'] == 'quarantined'
        await workflow.confirm_reception('guest-2', 'DEMO-002', operation_id='receipt-2')
        await workflow.record_guest_event('guest-2', 'consent')
        with pytest.raises(HotelWorkflowError):
            await workflow.assign_robot('guest-2', operation_id='assign-2')


async def test_reload_queries_known_task_without_replaying_physical_action() -> None:
    from hotel_agent.service import HotelWorkflow

    async with running(delay_s=0.01) as (workflow, simulator, runtime, database):
        await ready(workflow)
        first = await workflow.start_escort('guest-1', operation_id='start')
        original_query = runtime.get_task_status
        both_observers = asyncio.Event()
        terminal_queries = 0
        async def query_together(task_id: str) -> Any:
            nonlocal terminal_queries
            task = await original_query(task_id)
            if task.state.is_terminal:
                terminal_queries += 1
                if terminal_queries >= 2:
                    both_observers.set()
                async with asyncio.timeout(2):
                    await both_observers.wait()
            return task
        runtime.get_task_status = query_together
        recovered = HotelWorkflow(database, runtime)
        await recovered.start()
        try:
            settled = await recovered.wait_for_settled('guest-1')
            assert settled['runtime_task_id'] == first['runtime_task_id']
            assert settled['phase'] == 'awaiting_arrival_confirmation', settled
            assert len(runtime.requests) == 1
            assert simulator.skills['navigation.navigate_to'].calls == 1
        finally:
            await recovered.close()


async def test_missing_authoritative_meeting_point_rejects_assignment() -> None:
    from hotel_agent.backend import SimulatedHotel
    from hotel_agent.errors import HotelWorkflowError
    from hotel_agent.service import HotelWorkflow

    async with running() as (_, simulator, runtime, database):
        hotel = SimulatedHotel()
        hotel.meeting_point_id = ''
        hotel.meeting_point = ''
        workflow = HotelWorkflow(database, runtime, hotel=hotel)
        await workflow.start()
        try:
            await workflow.confirm_reception('guest-1', 'DEMO-001', operation_id='receipt')
            await workflow.record_guest_event('guest-1', 'consent')
            with pytest.raises(HotelWorkflowError):
                await workflow.assign_robot('guest-1', operation_id='assign')
            assert simulator.skills['navigation.navigate_to'].calls == 0
        finally:
            await workflow.close()


async def test_submit_outcome_unknown_is_persisted_and_never_automatically_replayed() -> None:
    from hotel_agent.service import HotelWorkflow

    async with running() as (workflow, _, runtime, database):
        await ready(workflow)
        async def uncertain_submit(request: TaskRequest) -> Any:
            runtime.requests.append(request)
            raise ConnectionError('response lost')
        runtime.submit_task = uncertain_submit
        uncertain = await workflow.start_escort('guest-1', operation_id='start')
        assert uncertain['phase'] == 'reconciliation_required'
        assert uncertain['runtime_task_id'] is None
        assert uncertain['robot_status'] == 'quarantined'
        await workflow.close()
        recovered = HotelWorkflow(database, runtime)
        await recovered.start()
        try:
            assert (await recovered.snapshot('guest-1'))['phase'] == 'reconciliation_required'
            assert len(runtime.requests) == 1
        finally:
            await recovered.close()


async def test_human_request_before_motion_releases_assignment_and_preserves_reason() -> None:
    async with running() as (workflow, simulator, _, _):
        await ready(workflow)
        human = await workflow.request_human('guest-1', '需要前台人工协助', operation_id='human')
        assert human['phase'] == 'human_assistance'
        assert human['human_reason'] == '需要前台人工协助'
        assert human['robot_status'] == 'available'
        assert simulator.skills['navigation.navigate_to'].calls == 0


async def test_close_confirms_stop_before_closing_the_hotel_database() -> None:
    from hotel_agent.service import HotelWorkflow

    async with running(delay_s=30) as (workflow, _, runtime, database):
        await ready(workflow)
        started = await workflow.start_escort('guest-1', operation_id='start')
        await workflow.close()
        task = await runtime.get_task_status(started['runtime_task_id'])
        assert task.state == TaskState.CANCELED
        recovered = HotelWorkflow(database, runtime)
        await recovered.start()
        try:
            assert (await recovered.snapshot('guest-1'))['phase'] == 'canceled'
        finally:
            await recovered.close()


async def test_hotel_backend_failure_is_a_domain_rejection_without_confirming_reception() -> None:
    from hotel_agent.backend import SimulatedHotel, VerifiedStay
    from hotel_agent.errors import HotelWorkflowError
    from hotel_agent.service import HotelWorkflow

    class UnavailableHotel(SimulatedHotel):
        async def verify_receipt(self, receipt_code: str) -> VerifiedStay:
            raise ConnectionError('PMS unavailable')

    async with running() as (_, _, runtime, database):
        workflow = HotelWorkflow(database, runtime, hotel=UnavailableHotel())
        await workflow.start()
        try:
            with pytest.raises(HotelWorkflowError):
                await workflow.confirm_reception('guest-1', 'DEMO-001', operation_id='receipt')
            assert (await workflow.snapshot('guest-1'))['phase'] == 'reception'
        finally:
            await workflow.close()


async def test_two_workflows_share_the_persisted_single_robot_lease() -> None:
    from hotel_agent.errors import HotelWorkflowError
    from hotel_agent.service import HotelWorkflow

    async with running() as (first, _, runtime, database):
        second = HotelWorkflow(database, runtime)
        await second.start()
        try:
            await first.confirm_reception('guest-1', 'DEMO-001', operation_id='receipt-1')
            await first.record_guest_event('guest-1', 'consent')
            await first.assign_robot('guest-1', operation_id='assign-1')
            await second.confirm_reception('guest-2', 'DEMO-002', operation_id='receipt-2')
            await second.record_guest_event('guest-2', 'consent')
            with pytest.raises(HotelWorkflowError):
                await second.assign_robot('guest-2', operation_id='assign-2')
            assert (await second.snapshot('guest-2'))['robot_id'] is None
        finally:
            await second.close()


async def test_close_during_return_preserves_completed_guest_service() -> None:
    from hotel_agent.service import HotelWorkflow

    async with running() as (workflow, simulator, runtime, database):
        await ready(workflow)
        await workflow.start_escort('guest-1', operation_id='start')
        await workflow.wait_for_settled('guest-1')
        simulator.skills['navigation.navigate_to'].delay_s = 30
        assert (await robot_state(runtime))['position'] == 'room-0108-door'
        completed = await workflow.record_guest_event('guest-1', 'arrived')
        await workflow.close()
        assert (await runtime.get_task_status(completed['return_task_id'])).state == TaskState.CANCELED
        recovered = HotelWorkflow(database, runtime)
        await recovered.start()
        try:
            assert (await recovered.snapshot('guest-1'))['phase'] == 'completed'
            assert (await recovered.snapshot('guest-1'))['arrival_confirmed'] is True
            assert (await recovered.snapshot('guest-1'))['robot_status'] == 'needs_reposition'
            assert (await robot_state(runtime))['position'] == 'room-0108-door'
            await recovered.confirm_reception('guest-2', 'DEMO-002', operation_id='receipt-2')
            await recovered.record_guest_event('guest-2', 'consent')
            from hotel_agent.errors import HotelWorkflowError
            with pytest.raises(HotelWorkflowError):
                await recovered.assign_robot('guest-2', operation_id='assign-2')
            assert len(runtime.requests) == 2
        finally:
            await recovered.close()


async def test_cancel_at_room_keeps_the_lease_without_automatically_moving_robot() -> None:
    from hotel_agent.errors import HotelWorkflowError

    async with running() as (workflow, _, runtime, _):
        await ready(workflow)
        await workflow.start_escort('guest-1', operation_id='start')
        await workflow.wait_for_settled('guest-1')
        assert (await robot_state(runtime))['position'] == 'room-0108-door'
        canceled = await workflow.cancel('guest-1', operation_id='cancel-at-room')
        assert canceled['phase'] == 'canceled'
        assert canceled['robot_status'] == 'needs_reposition'
        assert (await robot_state(runtime))['position'] == 'room-0108-door'
        await workflow.confirm_reception('guest-2', 'DEMO-002', operation_id='receipt-2')
        await workflow.record_guest_event('guest-2', 'consent')
        with pytest.raises(HotelWorkflowError):
            await workflow.assign_robot('guest-2', operation_id='assign-2')
        assert len(runtime.requests) == 1


async def test_invalid_or_unsupported_hotel_stay_never_commits_reception() -> None:
    from hotel_agent.backend import SimulatedHotel, VerifiedStay
    from hotel_agent.errors import HotelWorkflowError
    from hotel_agent.service import HotelWorkflow

    class InvalidStayHotel(SimulatedHotel):
        def __init__(self, stay: VerifiedStay) -> None:
            self.stay = stay

        async def verify_receipt(self, receipt_code: str) -> VerifiedStay:
            return self.stay

    invalid = [
        VerifiedStay('DEMO-001', '0208', 2, 'room-0208-door'),
        VerifiedStay('DEMO-001', '0108', True, 'room-0108-door'),
        VerifiedStay('DEMO-001', '', 1, 'room-0108-door'),
        VerifiedStay('DEMO-001', '   ', 1, 'room-0108-door'),
        VerifiedStay('DEMO-OTHER', '0108', 1, 'room-0108-door'),
        VerifiedStay('DEMO-001', '0108', 1, ''),
    ]
    async with running() as (_, simulator, runtime, database):
        for index, stay in enumerate(invalid):
            workflow = HotelWorkflow(database, runtime, hotel=InvalidStayHotel(stay))
            await workflow.start()
            session_id = f'invalid-{index}'
            try:
                with pytest.raises(HotelWorkflowError):
                    await workflow.confirm_reception(session_id, 'DEMO-001', operation_id='receipt')
                snapshot = await workflow.snapshot(session_id)
                assert snapshot['phase'] == 'reception'
                assert snapshot['room_id'] is None
                assert snapshot['destination_id'] is None
                assert simulator.skills['navigation.navigate_to'].calls == 0
            finally:
                await workflow.close()


async def test_human_request_after_cancel_records_reason_without_releasing_held_robot() -> None:
    from hotel_agent.errors import HotelWorkflowError

    async with running() as (workflow, _, runtime, _):
        await ready(workflow)
        started = await workflow.start_escort('guest-1', operation_id='start')
        await workflow.wait_for_settled('guest-1')
        await workflow.cancel('guest-1', operation_id='cancel-at-room')
        human = await workflow.request_human(
            'guest-1', '取消后请人工确认回位', operation_id='human-after-cancel',
        )
        assert human['phase'] == 'canceled'
        assert human['robot_status'] == 'needs_reposition'
        assert human['runtime_task_id'] == started['runtime_task_id']
        assert human['human_reason'] == '取消后请人工确认回位'
        assert (await workflow.snapshot('guest-1'))['human_reason'] == human['human_reason']
        assert (await robot_state(runtime))['position'] == 'room-0108-door'
        await workflow.confirm_reception('guest-2', 'DEMO-002', operation_id='receipt-2')
        await workflow.record_guest_event('guest-2', 'consent')
        with pytest.raises(HotelWorkflowError):
            await workflow.assign_robot('guest-2', operation_id='assign-2')
        assert len(runtime.requests) == 1


async def test_human_request_after_return_failure_preserves_completed_guest_service() -> None:
    from hotel_agent.errors import HotelWorkflowError

    async with running() as (workflow, simulator, runtime, _):
        await ready(workflow)
        await workflow.start_escort('guest-1', operation_id='start')
        await workflow.wait_for_settled('guest-1')
        simulator.skills['navigation.navigate_to']._failures.add('navigation.navigate_to')
        await workflow.record_guest_event('guest-1', 'arrived')
        settled = await workflow.wait_for_settled('guest-1')
        assert (await runtime.get_task_status(settled['return_task_id'])).state == TaskState.FAILED
        human = await workflow.request_human(
            'guest-1', '返程失败，请人工处理机器人', operation_id='human-after-return-failure',
        )
        assert human['phase'] == 'completed'
        assert human['arrival_confirmed'] is True
        assert human['robot_status'] == 'needs_reposition'
        assert human['return_task_id'] == settled['return_task_id']
        assert human['return_error'] == settled['return_error']
        assert human['human_reason'] == '返程失败，请人工处理机器人'
        assert (await workflow.snapshot('guest-1'))['human_reason'] == human['human_reason']
        assert (await robot_state(runtime))['position'] == 'room-0108-door'
        await workflow.confirm_reception('guest-2', 'DEMO-002', operation_id='receipt-2')
        await workflow.record_guest_event('guest-2', 'consent')
        with pytest.raises(HotelWorkflowError):
            await workflow.assign_robot('guest-2', operation_id='assign-2')
        assert len(runtime.requests) == 2


async def test_announce_speaks_without_changing_the_service_phase_or_robot_allocation() -> None:
    """Announcement is speech output, not a service step: it must not move phase or lease.

    A guest can be mid-escort while staff ask the robot to state breakfast hours;
    rewriting business state from a speech would make the escort machine depend on
    dialogue volume instead of on confirmed physical progress.
    """
    async with running() as (workflow, simulator, runtime, _):
        before = await ready(workflow)
        observed = await workflow.announce(
            'guest-1', '早餐时间是七点到十点', operation_id='announce-1',
        )
        assert observed['tool'] == 'hotel_announce'
        assert observed['state'] == 'succeeded'
        assert observed['requires_attention'] is False
        assert observed['result']['steps'][0]['output'] == {
            'message': '早餐时间是七点到十点', 'spoken': True,
        }
        assert simulator.skills['audio.speak'].calls == 1
        after = await workflow.snapshot('guest-1')
        assert after['phase'] == before['phase']
        assert after['robot_status'] == before['robot_status']
        assert after['runtime_task_id'] == before['runtime_task_id']
        request = runtime.requests[-1]
        assert request.task_type == 'hotel_announce'
        assert [call.skill for call in request.steps] == ['audio.speak']
        service_id = after['service_id']
        assert request.idempotency_key == f'hotel:{service_id}:announce:announce-1'
        assert (await robot_state(runtime))['last_message'] == '早餐时间是七点到十点'


async def test_announce_rejects_blank_or_oversized_messages_without_speaking() -> None:
    from hotel_agent.errors import HotelWorkflowError

    async with running() as (workflow, simulator, runtime, _):
        await ready(workflow)
        for message, operation in (('', 'blank'), ('要' * 201, 'oversized'), (None, 'missing')):
            with pytest.raises(HotelWorkflowError):
                await workflow.announce('guest-1', message, operation_id=operation)
        assert simulator.skills['audio.speak'].calls == 0
        assert len(runtime.requests) == 0


async def test_announce_works_while_another_guest_holds_the_robot_lease() -> None:
    """Speech needs the speaker, not the robot's movement allocation.

    The lease serialises escorts. Blocking a spoken hotel fact behind it would
    let one guest's route stall another guest's question without any physical
    reason, so announcement is deliberately outside the lease.
    """
    async with running(delay_s=0.05) as (workflow, simulator, _, _):
        await ready(workflow)
        await workflow.start_escort('guest-1', operation_id='start')
        assert (await workflow.snapshot('guest-1'))['phase'] == 'escorting'
        await workflow.confirm_reception('guest-2', 'DEMO-002', operation_id='receipt-2')
        observed = await workflow.announce('guest-2', '退房时间是中午十二点', operation_id='announce-2')
        assert observed['state'] == 'succeeded'
        assert simulator.skills['audio.speak'].calls == 1
        assert (await workflow.snapshot('guest-2'))['phase'] == 'awaiting_consent'
        assert (await workflow.snapshot('guest-2'))['robot_id'] is None


async def test_repeated_announce_operation_id_reuses_the_saved_task_without_speaking_twice() -> None:
    async with running() as (workflow, simulator, runtime, _):
        await ready(workflow)
        first = await workflow.announce('guest-1', '电梯在东侧', operation_id='same-key')
        second = await workflow.announce('guest-1', '电梯在东侧', operation_id='same-key')
        assert second['task_id'] == first['task_id']
        assert second['state'] == 'succeeded'
        assert simulator.skills['audio.speak'].calls == 1
        assert len(runtime.requests) == 2
        assert runtime.requests[0].idempotency_key == runtime.requests[1].idempotency_key


async def test_announce_wait_timeout_keeps_the_task_observable_without_quarantining() -> None:
    """A local observation deadline says nothing about the speaker, so record nothing.

    The speech may still finish. Quarantining the robot or replaying the sentence
    would turn an unverifiable wait into a claimed physical fact.
    """
    async with running(delay_s=0.2, wait_timeout_s=0.02) as (workflow, simulator, _, _):
        before = await ready(workflow)
        observed = await workflow.announce('guest-1', '泳池在三层', operation_id='slow-announce')
        assert observed['state'] not in {None, 'succeeded'}
        assert observed['task_id']
        assert observed['error'] is not None
        assert observed['requires_attention'] is True
        after = await workflow.snapshot('guest-1')
        assert after['phase'] == before['phase']
        assert after['robot_status'] == before['robot_status']
        assert simulator.skills['audio.speak'].calls == 1
