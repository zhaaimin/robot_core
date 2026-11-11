import tempfile
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import SkillCall, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RemoteRuntime, RobotRuntime
from robot_agent_runtime.transport.server import RuntimeServer

if TYPE_CHECKING:
    from robot_agent_core.ports import TaskRuntime


async def check_task_runtime_contract(runtime: 'TaskRuntime') -> None:
    definitions = await runtime.list_skills()
    assert 'audio.speak' in {definition.name for definition in definitions}
    request = TaskRequest(
        task_type='client_contract', steps=[SkillCall(skill='audio.speak', params={'message': 'contract'})],
        idempotency_key='contract:task-runtime',
    )
    submitted = await runtime.submit_task(request)
    assert (await runtime.submit_task(request)).id == submitted.id
    final = await runtime.submit_and_wait(request, timeout_s=2)
    assert final.id == submitted.id
    assert final.state == TaskState.SUCCEEDED
    assert (await runtime.get_task_status(final.id)).state == final.state
    assert (await runtime.cancel_task(final.id)).state == final.state
    async with aclosing(runtime.subscribe_task_events(final.id)) as stream:
        events = [event async for event in stream]
    assert events
    assert all(event.task_id == final.id for event in events)
    sequences = [event.sequence for event in events]
    assert sequences == sorted(set(sequences))
    assert events[-1].data['state'] == 'succeeded'
    async with aclosing(runtime.subscribe_task_events(final.id, after_sequence=events[0].sequence)) as stream:
        replay = [event async for event in stream]
    assert [event.sequence for event in replay] == sequences[1:]


async def test_embedded_task_runtime_contract() -> None:
    from robot_agent_core.ports import TaskRuntime

    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    client: TaskRuntime = runtime
    await runtime.start()
    try:
        await check_task_runtime_contract(client)
    finally:
        await runtime.close()


async def test_remote_task_runtime_contract() -> None:
    simulator = Simulator()
    with tempfile.TemporaryDirectory() as directory:
        socket = str(Path(directory) / 'runtime.sock')
        server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, socket)
        runtime = RemoteRuntime(socket)
        await server.start()
        try:
            await runtime.start()
            await check_task_runtime_contract(runtime)
        finally:
            await runtime.close()
            await server.close()


async def test_mcp_task_runtime_contract() -> None:
    pytest.importorskip('robot_runtime_mcp')
    from robot_runtime_mcp.client import open_mcp_runtime

    simulator = Simulator()
    with tempfile.TemporaryDirectory() as directory:
        socket = str(Path(directory) / 'runtime.sock')
        server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, socket)
        await server.start()
        try:
            async with open_mcp_runtime(socket) as runtime:
                await check_task_runtime_contract(runtime)
        finally:
            await server.close()
