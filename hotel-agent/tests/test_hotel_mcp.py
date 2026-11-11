import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest


def test_hotel_mcp_configuration_owns_only_local_business_and_checkpoints(tmp_path: Path) -> None:
    from hotel_agent.app import parse_args
    from robot_agent_core.configuration import rebuild_config, validate_config

    socket = tmp_path / 'remote' / '..' / 'runtime.sock'
    config = parse_args([
        '--backend', 'mcp', '--runtime-socket', str(socket),
        '--data-dir', str(tmp_path / 'data'),
    ])
    assert config.runtime_socket == tmp_path / 'runtime.sock'
    assert config.task_database is None
    assert config.business_database.parent == config.data_dir
    assert config.checkpoint_database.parent == config.data_dir
    assert 'mcp' in config.business_database.name
    assert 'mcp' in config.checkpoint_database.name
    simulator = parse_args(['--data-dir', str(config.data_dir)])
    assert config.business_database != simulator.business_database
    assert config.checkpoint_database != simulator.checkpoint_database
    assert config.runtime_id != simulator.runtime_id
    changed = rebuild_config(config, runtime_socket=tmp_path / 'another.sock')
    assert changed.runtime_id != config.runtime_id
    assert changed.business_database != config.business_database
    assert changed.checkpoint_database != config.checkpoint_database
    validate_config(changed)


def test_mcp_endpoint_is_required_and_exclusive_to_the_hotel_mcp_backend(tmp_path: Path) -> None:
    from hotel_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError

    invalid = (
        ['--backend', 'mcp'],
        ['--backend', 'cc', '--runtime-socket', '/runtime.sock'],
        ['--runtime-socket', '/runtime.sock'],
    )
    for arguments in invalid:
        with pytest.raises(AgentConfigurationError):
            parse_args([*arguments, '--data-dir', str(tmp_path)])
    assert not (tmp_path / 'hotel.sqlite3').exists()


def test_stale_mcp_endpoint_is_rejected_before_startup(tmp_path: Path) -> None:
    from hotel_agent.app import parse_args
    from robot_agent_core.configuration import validate_config
    from robot_agent_core.errors import AgentConfigurationError

    config = parse_args([
        '--backend', 'mcp', '--runtime-socket', str(tmp_path / 'one.sock'),
        '--data-dir', str(tmp_path),
    ])
    with pytest.raises(AgentConfigurationError, match='rebuild_config'):
        validate_config(replace(config, runtime_socket=tmp_path / 'two.sock'))


async def test_hotel_agent_completes_the_existing_skill_journey_over_real_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip('robot_runtime_mcp')
    from hotel_agent.app import parse_args, run_application
    from hotel_agent.simulator import HotelSimulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.sdk import RemoteRuntime
    from robot_agent_runtime.transport.server import RuntimeServer
    from scripted_models import scripted_model

    socket = str(tmp_path / 'runtime.sock')
    simulator = HotelSimulator(delay_s=0.02)
    server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, socket)
    config = parse_args([
        '--backend', 'mcp', '--runtime-socket', socket,
        '--data-dir', str(tmp_path / 'agent'), '--thread-id', 'guest-mcp',
    ])

    def unexpected_local_runtime(*args: Any, **kwargs: Any) -> None:
        pytest.fail('MCP hotel created an embedded runtime')

    monkeypatch.setattr('hotel_agent.application.RobotRuntime', unexpected_local_runtime)
    monkeypatch.setattr('sys.stdin', io.StringIO(
        '/skill frontdesk-reception\n入住 DEMO-001\n'
        '/skill guest-handoff\n/consent\n'
        '/skill escort-assistance\n/start\n/wait\n/arrived\n/wait\n/status\n',
    ))
    await server.start()
    try:
        async with asyncio.timeout(20):
            await run_application(config, scripted_model())
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert rows[-1]['backend'] == 'mcp'
        assert rows[-1]['hotel']['phase'] == 'completed'
        assert rows[-1]['hotel']['arrival_confirmed'] is True
        assert rows[-1]['hotel']['robot_status'] == 'available'
        assert simulator.skills['navigation.navigate_to'].calls == 2
        assert not (config.data_dir / 'hotel-simulator-tasks.sqlite3').exists()
        assert config.business_database.exists()
        assert config.checkpoint_database.exists()
        client = RemoteRuntime(socket)
        try:
            await client.start()
            escort = await client.get_task_status(rows[-1]['hotel']['runtime_task_id'])
            assert escort.metadata['hotel_action'] == 'escort'
        finally:
            await client.close()
    finally:
        await server.close()


async def test_hotel_mcp_cancel_keeps_the_existing_reposition_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip('robot_runtime_mcp')
    from hotel_agent.app import parse_args, run_application
    from hotel_agent.simulator import HotelSimulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.transport.server import RuntimeServer
    from scripted_models import scripted_model

    socket = str(tmp_path / 'runtime.sock')
    simulator = HotelSimulator(delay_s=30)
    server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, socket)
    config = parse_args([
        '--backend', 'mcp', '--runtime-socket', socket,
        '--data-dir', str(tmp_path / 'agent'), '--thread-id', 'guest-mcp',
    ])
    monkeypatch.setattr('sys.stdin', io.StringIO(
        '/skill frontdesk-reception\n入住 DEMO-001\n'
        '/skill guest-handoff\n/consent\n'
        '/skill escort-assistance\n/start\n/cancel\n/status\n',
    ))
    await server.start()
    try:
        async with asyncio.timeout(20):
            await run_application(config, scripted_model())
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert rows[-1]['hotel']['phase'] == 'canceled'
        assert rows[-1]['hotel']['robot_status'] == 'needs_reposition'
        assert simulator.skills['navigation.navigate_to'].calls == 1
    finally:
        await server.close()


async def test_hotel_mcp_refuses_a_runtime_without_a_speaker_before_business_storage(
    tmp_path: Path,
) -> None:
    pytest.importorskip('robot_runtime_mcp')
    from hotel_agent.app import parse_args, run_application
    from hotel_agent.simulator import HotelSimulator
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.transport.server import RuntimeServer
    from scripted_models import scripted_model

    simulator = HotelSimulator()
    socket = str(tmp_path / 'runtime.sock')
    server = RuntimeServer(InMemoryTaskRepository(),
                           {'navigation.navigate_to': simulator.skills['navigation.navigate_to']}, socket)
    config = parse_args([
        '--backend', 'mcp', '--runtime-socket', socket,
        '--data-dir', str(tmp_path / 'agent'),
    ])
    await server.start()
    try:
        with pytest.raises(AgentConfigurationError, match='missing required skills'):
            await run_application(config, scripted_model())
        assert not config.business_database.exists()
        assert not config.checkpoint_database.exists()
        assert simulator.skills['navigation.navigate_to'].calls == 0
    finally:
        await server.close()


async def test_hotel_mcp_disconnect_quarantines_active_escort_without_replay(tmp_path: Path) -> None:
    pytest.importorskip('robot_runtime_mcp')
    from hotel_agent.adapters.mcp import open_runtime
    from hotel_agent.service import HotelWorkflow
    from hotel_agent.simulator import HotelSimulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.transport.server import RuntimeServer

    socket = tmp_path / 'runtime.sock'
    simulator = HotelSimulator(delay_s=30)
    server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, str(socket))
    await server.start()
    try:
        async with open_runtime(socket, allowed_skills={'audio.speak', 'navigation.navigate_to'}) as runtime:
            workflow = HotelWorkflow(str(tmp_path / 'hotel.sqlite3'), runtime)
            await workflow.start()
            try:
                await workflow.confirm_reception('guest', 'DEMO-001', operation_id='receipt')
                await workflow.record_guest_event('guest', 'consent')
                await workflow.assign_robot('guest', operation_id='assign')
                await workflow.record_guest_event('guest', 'ready')
                submitted = await workflow.start_escort('guest', operation_id='start')
                assert submitted['runtime_task_id']
                await server.close()
                snapshot = await workflow.refresh('guest')
                assert snapshot['phase'] == 'reconciliation_required'
                assert snapshot['robot_status'] == 'quarantined'
                assert simulator.skills['navigation.navigate_to'].calls <= 1
            finally:
                await workflow.close()
    finally:
        await server.close()


async def test_hotel_mcp_reconnect_preserves_announcement_idempotency_and_business_identity(tmp_path: Path) -> None:
    pytest.importorskip('robot_runtime_mcp')
    from hotel_agent.adapters.mcp import open_runtime
    from hotel_agent.service import HotelWorkflow
    from hotel_agent.simulator import HotelSimulator
    from robot_agent_runtime.repositories.sqlite import SQLiteTaskRepository
    from robot_agent_runtime.transport.server import RuntimeServer

    socket = tmp_path / 'runtime.sock'
    simulator = HotelSimulator(delay_s=0)
    server = RuntimeServer(SQLiteTaskRepository(str(tmp_path / 'runtime.sqlite3')), simulator.skills, str(socket))
    await server.start()
    ids: list[str] = []
    try:
        for _ in range(2):
            async with open_runtime(socket, allowed_skills={'audio.speak', 'navigation.navigate_to'}) as runtime:
                workflow = HotelWorkflow(str(tmp_path / 'hotel.sqlite3'), runtime)
                await workflow.start()
                try:
                    announced = await workflow.announce('guest', 'Welcome', operation_id='same-announcement')
                    assert announced['state'] == 'succeeded'
                    ids.append(announced['task_id'])
                finally:
                    await workflow.close()
        assert ids[0] == ids[1]
        assert simulator.skills['audio.speak'].calls == 1
    finally:
        await server.close()


async def test_hotel_mcp_restart_recovers_saved_navigation_without_replay(tmp_path: Path) -> None:
    pytest.importorskip('robot_runtime_mcp')
    from hotel_agent.adapters.mcp import open_runtime
    from hotel_agent.errors import HotelWorkflowError
    from hotel_agent.service import HotelWorkflow
    from hotel_agent.simulator import HotelSimulator
    from robot_agent_runtime.repositories.sqlite import SQLiteTaskRepository
    from robot_agent_runtime.transport.server import RuntimeServer

    socket = tmp_path / 'runtime.sock'
    database = tmp_path / 'runtime.sqlite3'
    business = tmp_path / 'hotel.sqlite3'
    simulator = HotelSimulator(delay_s=0)
    server = RuntimeServer(SQLiteTaskRepository(str(database)), simulator.skills, str(socket))
    await server.start()
    try:
        async with open_runtime(socket, allowed_skills={'audio.speak', 'navigation.navigate_to'}) as runtime:
            workflow = HotelWorkflow(str(business), runtime)
            await workflow.start()
            try:
                await workflow.confirm_reception('guest', 'DEMO-001', operation_id='receipt')
                await workflow.record_guest_event('guest', 'consent')
                await workflow.assign_robot('guest', operation_id='assign')
                await workflow.record_guest_event('guest', 'ready')
                submitted = await workflow.start_escort('guest', operation_id='escort')
                settled = await workflow.wait_for_settled('guest', timeout_s=2)
                assert settled['phase'] == 'awaiting_arrival_confirmation'
                task_id = submitted['runtime_task_id']
            finally:
                await workflow.close()
    finally:
        await server.close()
    assert simulator.skills['navigation.navigate_to'].calls == 1

    restarted_simulator = HotelSimulator(delay_s=0)
    restarted_server = RuntimeServer(SQLiteTaskRepository(str(database)), restarted_simulator.skills, str(socket))
    await restarted_server.start()
    try:
        async with open_runtime(socket, allowed_skills={'audio.speak', 'navigation.navigate_to'}) as runtime:
            resumed = HotelWorkflow(str(business), runtime)
            await resumed.start()
            try:
                state = await resumed.refresh('guest')
                assert state['runtime_task_id'] == task_id
                assert state['phase'] == 'awaiting_arrival_confirmation'
                assert state['robot_status'] == 'at_room'
                assert not state['arrival_confirmed']
                await resumed.confirm_reception('other', 'DEMO-002', operation_id='other-receipt')
                await resumed.record_guest_event('other', 'consent')
                with pytest.raises(HotelWorkflowError):
                    await resumed.assign_robot('other', operation_id='other-assign')
                assert restarted_simulator.skills['navigation.navigate_to'].calls == 0
                assert (await runtime.get_task_status(task_id)).state.value == 'succeeded'
            finally:
                await resumed.close()
    finally:
        await restarted_server.close()
