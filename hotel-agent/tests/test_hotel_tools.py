import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from hotel_agent.service import HotelWorkflow
from hotel_agent.tools import HotelTools
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def test_hotel_tools_cannot_forge_consent_or_choose_a_destination() -> None:
    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    with tempfile.TemporaryDirectory() as directory:
        workflow = HotelWorkflow(str(Path(directory) / 'hotel.sqlite3'), runtime)
        await workflow.start()
        try:
            tools = HotelTools(workflow, 'guest-one')
            names = {item['function']['name'] for item in tools.schemas}
            assert 'robot_navigate_to' not in names
            received = await tools.invoke('hotel_confirm_reception', {'receipt_code': 'DEMO-001'}, operation_id='receipt')
            assert received['result']['room_id'] == '0108'
            forged = await tools.invoke('hotel_assign_butler', {'consent_confirmed': True}, operation_id='forged')
            assert forged['requires_attention'] is True
            premature = await tools.invoke('hotel_start_escort', {'destination_id': 'another-room'}, operation_id='wrong')
            assert premature['requires_attention'] is True
            missing = await tools.invoke('hotel_assign_butler', {}, operation_id='missing')
            assert missing['error']['code'] == 'HOTEL_WORKFLOW_REJECTED'
            assert simulator.skills['navigation.navigate_to'].calls == 0
        finally:
            try:
                await workflow.close()
            finally:
                await runtime.close()


async def test_hotel_tools_supply_facts_without_selecting_skills_and_preserve_arrival_confirmation() -> None:
    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    with tempfile.TemporaryDirectory() as directory:
        workflow = HotelWorkflow(str(Path(directory) / 'hotel.sqlite3'), runtime)
        await workflow.start()
        try:
            tools = HotelTools(workflow, 'guest-one')
            described = await tools.describe()
            assert 'required_skill' not in described
            assert described['facts']['phase'] == 'reception'
            assert '/consent' in described['confirmation_policy']
            await tools.invoke('hotel_confirm_reception', {'receipt_code': 'DEMO-001'}, operation_id='receipt')
            await workflow.record_guest_event('guest-one', 'consent')
            assigned = await tools.invoke('hotel_assign_butler', {}, operation_id='assign')
            assert assigned['result']['phase'] == 'waiting_for_guest'
            described = await tools.describe()
            assert 'required_skill' not in described
            assert described['facts']['phase'] == 'waiting_for_guest'
            await workflow.record_guest_event('guest-one', 'ready')
            await tools.invoke('hotel_start_escort', {}, operation_id='start')
            settled = await workflow.wait_for_settled('guest-one')
            assert settled['phase'] == 'awaiting_arrival_confirmation'
            described = await tools.describe()
            assert 'required_skill' not in described
            assert described['facts']['phase'] == 'awaiting_arrival_confirmation'
            assert not settled['arrival_confirmed']
            await workflow.record_guest_event('guest-one', 'arrived')
            final = await tools.invoke('hotel_status', {}, operation_id='status')
            assert final['result']['phase'] == 'completed'
            assert final['state'] == 'succeeded'
        finally:
            try:
                await workflow.close()
            finally:
                await runtime.close()


@asynccontextmanager
async def live_tools(*, fail_speak: bool = False) -> AsyncIterator[tuple[HotelTools, Simulator, HotelWorkflow]]:
    """Reuse the runtime's real speaker skill instead of a stubbed tool boundary."""
    simulator = Simulator(fail_skills={'audio.speak'} if fail_speak else set())
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    with tempfile.TemporaryDirectory() as directory:
        workflow = HotelWorkflow(str(Path(directory) / 'hotel.sqlite3'), runtime)
        await workflow.start()
        try:
            yield HotelTools(workflow, 'guest-one'), simulator, workflow
        finally:
            try:
                await workflow.close()
            finally:
                await runtime.close()


async def test_announce_tool_speaks_without_laundering_speech_into_a_business_snapshot() -> None:
    """The announcement result must stay a task outcome, not a phase payload.

    Returning the service snapshot here would let the model read a spoken
    sentence as evidence that reception, consent or arrival advanced.
    """
    async with live_tools() as (tools, simulator, workflow):
        names = {item['function']['name'] for item in tools.schemas}
        assert 'hotel_announce' in names
        before = await workflow.snapshot('guest-one')
        observed = await tools.invoke(
            'hotel_announce', {'message': '机器人已经在大堂前台会合点等您'}, operation_id='speak-1',
        )
        assert observed['tool'] == 'hotel_announce'
        assert observed['state'] == 'succeeded'
        assert observed['requires_attention'] is False
        assert observed['result']['steps'][0]['output'] == {
            'message': '机器人已经在大堂前台会合点等您', 'spoken': True,
        }
        assert 'phase' not in observed['result']
        assert simulator.skills['audio.speak'].calls == 1
        assert (await workflow.snapshot('guest-one'))['phase'] == before['phase']


async def test_announce_tool_rejects_missing_or_unbounded_message_arguments() -> None:
    async with live_tools() as (tools, simulator, _):
        for args, operation in (
            ({}, 'missing'), ({'message': ''}, 'blank'), ({'message': '要' * 201}, 'oversized'),
            ({'message': '请到大堂', 'destination_id': 'room-0108-door'}, 'smuggled'),
        ):
            observed = await tools.invoke('hotel_announce', args, operation_id=operation)
            assert observed['error'] is not None, args
            assert observed['requires_attention'] is True
        assert simulator.skills['audio.speak'].calls == 0


async def test_announce_tool_reports_a_failed_speech_instead_of_claiming_it_was_spoken() -> None:
    async with live_tools(fail_speak=True) as (tools, simulator, _):
        observed = await tools.invoke('hotel_announce', {'message': '大堂前台会合点在入口右侧'}, operation_id='broken')
        assert observed['state'] == 'failed'
        assert observed['requires_attention'] is True
        assert observed['error'] is not None
        assert simulator.skills['audio.speak'].calls == 1
