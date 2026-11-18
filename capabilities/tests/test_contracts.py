import asyncio
import tempfile
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import Event, Task
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RemoteRuntime
from robot_agent_runtime.transport.server import RuntimeServer
from test_capabilities import running

if TYPE_CHECKING:
    from robot_agent_capabilities.ports import ListenerHandle, TaskEventRuntime, TaskRuntime


async def check_capability_contract(runtime: 'TaskRuntime') -> None:
    from robot_agent_capabilities import RobotCapabilities

    capabilities = await RobotCapabilities.create(runtime, allowed_skills={'audio.speak'})
    tools = {tool.name: tool for tool in capabilities.tools}
    assert set(tools) == {'robot_speak', 'robot_task_status', 'robot_cancel_task'}
    assert tools['robot_speak'].skill == 'audio.speak'
    assert tools['robot_speak'].input_schema['required'] == ['message']
    assert tools['robot_speak'].input_schema['properties']['message']['description'].endswith('[minLength=1]')
    done: list[Task] = []
    isolated_done: list[Task] = []
    locations: list[asyncio.AbstractEventLoop] = []

    def isolated_callback(task: Task) -> None:
        isolated_done.append(task)
        locations.append(asyncio.get_running_loop())

    listener = capabilities.add_task_listener(on_done=done.append)
    isolated = capabilities.add_task_listener(on_done=isolated_callback, execution='group', group='contract')
    try:
        first = await capabilities.invoke('robot_speak', {'message': 'contract'}, operation_id='contract', wait=True)
        replay = await capabilities.invoke('robot_speak', {'message': 'contract'}, operation_id='contract', wait=True)
        await asyncio.gather(listener.wait(), isolated.wait())
        assert [task.id for task in done] == [first['task_id']]
        assert [task.id for task in isolated_done] == [first['task_id']]
        assert locations and asyncio.get_running_loop() not in locations
        await check_listener_handle_contract(listener)
    finally:
        await capabilities.close_listeners()
    assert first['task_id'] == replay['task_id']
    assert first['state'] == 'succeeded'
    assert first['requires_attention'] is False
    assert (await runtime.get_task_status(first['task_id'])).idempotency_key == 'robot:contract'
    status = await capabilities.invoke('robot_task_status', {'task_id': first['task_id']})
    assert status['state'] == 'succeeded'
    assert status['result'] == first['result']
    async with asyncio.timeout(2):
        completed = [event async for event in capabilities.subscribe_events(
            sources={'task'}, task_ids={first['task_id']}, task_terminal_only=True,
        )]
    assert len(completed) == 1 and isinstance(completed[0], Event)
    assert completed[0].task_id == first['task_id'] and completed[0].data['state'] == 'succeeded'
    async with asyncio.timeout(2):
        resumed = [event async for event in capabilities.subscribe_events(
            sources={'task'}, task_ids={first['task_id']},
            after_sequences={first['task_id']: completed[0].sequence},
        )]
    assert resumed == []


async def check_listener_handle_contract(handle: 'ListenerHandle') -> None:
    """Close observation idempotently and wait without inferring action success."""
    assert isinstance(handle.closed, bool)
    await handle.close()
    await handle.close()
    async with asyncio.timeout(1):
        await handle.wait()
    assert handle.closed


async def test_listener_handle_contract_for_platform_observation() -> None:
    from robot_agent_capabilities import RobotCapabilities
    from test_events import EventRuntime

    runtime = EventRuntime([], quiet=True)
    capabilities = await RobotCapabilities.create(runtime)
    try:
        listener = capabilities.add_platform_listener(on_event=lambda event: None)
        await asyncio.wait_for(runtime.entered.wait(), 1)
        await check_listener_handle_contract(listener)
    finally:
        await capabilities.close_listeners()


async def check_task_event_runtime_contract(runtime: 'TaskEventRuntime', expected: list[Event]) -> None:
    """Observe supplied live facts without fabricating a global task sequence."""
    received: list[Event] = []
    positions: dict[str, int] = {}
    async with aclosing(runtime.subscribe_all_task_events()) as stream:
        async with asyncio.timeout(1):
            for _ in expected:
                event = await anext(stream)
                assert isinstance(event, Event)
                assert event.sequence > positions.get(event.task_id, 0)
                positions[event.task_id] = event.sequence
                received.append(event)
    assert {event.event_id for event in received} == {event.event_id for event in expected}


async def test_optional_task_event_runtime_contract() -> None:
    from test_task_events import Combined, Script, task_event

    events = [task_event('a', 1), task_event('b', 1), task_event('a', 2)]
    await check_task_event_runtime_contract(Combined(tasks=Script(events)), events)


async def test_embedded_capability_contract() -> None:
    async with running() as (runtime, simulator):
        await check_capability_contract(runtime)
        assert simulator.skills['audio.speak'].calls == 1


async def test_remote_capability_contract() -> None:
    simulator = Simulator()
    with tempfile.TemporaryDirectory() as directory:
        socket = str(Path(directory) / 'runtime.sock')
        server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, socket)
        client = RemoteRuntime(socket)
        await server.start()
        try:
            await client.start()
            await check_capability_contract(client)
            assert simulator.skills['audio.speak'].calls == 1
        finally:
            await client.close()
            await server.close()


async def test_mcp_task_client_uses_the_same_capability_contract() -> None:
    pytest.importorskip('robot_runtime_mcp')
    from robot_runtime_mcp.client import open_mcp_runtime

    simulator = Simulator()
    with tempfile.TemporaryDirectory() as directory:
        socket = str(Path(directory) / 'runtime.sock')
        server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, socket)
        await server.start()
        try:
            async with open_mcp_runtime(socket) as runtime:
                await check_capability_contract(runtime)
            assert simulator.skills['audio.speak'].calls == 1
        finally:
            await server.close()
