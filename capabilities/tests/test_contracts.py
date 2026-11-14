import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RemoteRuntime
from robot_agent_runtime.transport.server import RuntimeServer
from test_capabilities import running

if TYPE_CHECKING:
    from robot_agent_capabilities.ports import TaskRuntime


async def check_capability_contract(runtime: 'TaskRuntime') -> None:
    from robot_agent_capabilities import RobotCapabilities

    capabilities = await RobotCapabilities.create(runtime, allowed_skills={'audio.speak'})
    tools = {tool.name: tool for tool in capabilities.tools}
    assert set(tools) == {'robot_speak', 'robot_task_status', 'robot_cancel_task'}
    assert tools['robot_speak'].skill == 'audio.speak'
    assert tools['robot_speak'].input_schema['required'] == ['message']
    assert tools['robot_speak'].input_schema['properties']['message']['description'].endswith('[minLength=1]')
    first = await capabilities.invoke('robot_speak', {'message': 'contract'}, operation_id='contract', wait=True)
    replay = await capabilities.invoke('robot_speak', {'message': 'contract'}, operation_id='contract', wait=True)
    assert first['task_id'] == replay['task_id']
    assert first['state'] == 'succeeded'
    assert first['requires_attention'] is False
    assert (await runtime.get_task_status(first['task_id'])).idempotency_key == 'robot:contract'
    status = await capabilities.invoke('robot_task_status', {'task_id': first['task_id']})
    assert status['state'] == 'succeeded'
    assert status['result'] == first['result']


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
