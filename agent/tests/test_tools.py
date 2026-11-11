import asyncio
import tempfile
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime

if TYPE_CHECKING:
    from robot_agent.robot_tools import RobotTools


@asynccontextmanager
async def running(
    *, fail: bool = False, wait_timeout_s: float = 1,
) -> AsyncIterator[tuple["RobotTools", Simulator, RobotRuntime]]:
    from robot_agent.robot_tools import RobotTools

    simulator = Simulator(fail_skills={"audio.speak"} if fail else set())
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        tools = await RobotTools.create(runtime, wait_timeout_s=wait_timeout_s)
        yield tools, simulator, runtime
    finally:
        await runtime.close()


async def test_discovers_skill_schema_and_executes_a_real_runtime_task() -> None:
    async with running() as (tools, simulator, runtime):
        schemas = {tool['function']['name']: tool for tool in tools.schemas}
        assert schemas['robot_speak']['function']['parameters']['required'] == ['message']
        observed = await tools.invoke('robot_speak', {'message': 'hello'}, operation_id='op-1')
        assert observed['state'] == 'succeeded'
        assert observed['error'] is None
        assert observed['result']['steps'][0]['output']['spoken'] is True
        assert simulator.skills['audio.speak'].calls == 1
        assert (await runtime.get_task_status(observed['task_id'])).metadata['operation_id'] == 'op-1'


async def test_operation_replay_reuses_the_original_task() -> None:
    async with running() as (tools, simulator, _):
        first = await tools.invoke('robot_speak', {'message': 'hello'}, operation_id='stable')
        second = await tools.invoke('robot_speak', {'message': 'hello'}, operation_id='stable')
        assert first['task_id'] == second['task_id']
        assert simulator.skills['audio.speak'].calls == 1


async def test_bad_parameters_return_a_failed_task_without_executing() -> None:
    async with running() as (tools, simulator, _):
        observed = await tools.invoke('robot_speak', {'message': ''}, operation_id='bad')
        assert observed['state'] == 'failed'
        assert observed['error']['code'] == 'SCHEMA_VALIDATION_FAILED'
        assert observed['requires_attention'] is True
        assert simulator.skills['audio.speak'].calls == 0


async def test_skill_failures_are_observations_instead_of_success() -> None:
    async with running(fail=True) as (tools, _, _):
        observed = await tools.invoke('robot_speak', {'message': 'hello'}, operation_id='fail')
        assert observed['state'] == 'failed'
        assert observed['error']['code'] == 'SKILL_EXECUTION_FAILED'
        assert observed['requires_attention'] is True


async def test_wait_timeout_preserves_the_task_for_query_and_explicit_cancel() -> None:
    async with running(wait_timeout_s=0.01) as (tools, _, runtime):
        async with asyncio.timeout(2):
            observed = await tools.invoke('robot_wait_delay', {'delay_s': 30}, operation_id='slow')
            assert observed['error']['code'] == 'TASK_WAIT_TIMEOUT'
            task = await runtime.get_task_status(observed['task_id'])
            assert not task.cancel_requested and not task.state.is_terminal
            status = await tools.invoke('robot_task_status', {'task_id': task.id}, operation_id='query')
            assert status['task_id'] == task.id
            canceled = await tools.invoke('robot_cancel_task', {'task_id': task.id}, operation_id='cancel')
            assert canceled['state'] == 'canceled'


async def test_allowlist_controls_which_skill_tools_can_execute() -> None:
    from robot_agent.robot_tools import RobotTools

    async with running() as (_, simulator, runtime):
        tools = await RobotTools.create(runtime, allowed_skills={'audio.speak'})
        names = {tool['function']['name'] for tool in tools.schemas}
        assert 'robot_navigate_to' not in names
        observed = await tools.invoke('robot_navigate_to', {'target': 'room'}, operation_id='denied')
        assert observed['requires_attention'] is True
        assert observed['error']['code'] == 'AGENT_TOOL_NOT_ALLOWED'
        assert simulator.skills['navigation.navigate_to'].calls == 0


async def test_same_tools_work_with_remote_runtime_without_kernel_access() -> None:
    from robot_agent.robot_tools import RobotTools
    from robot_agent_runtime.sdk import RemoteRuntime
    from robot_agent_runtime.transport.server import RuntimeServer

    simulator = Simulator()
    with tempfile.TemporaryDirectory() as directory:
        socket = str(Path(directory) / 'runtime.sock')
        server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, socket)
        client = RemoteRuntime(socket)
        await server.start()
        try:
            await client.start()
            tools = await RobotTools.create(client)
            observed = await tools.invoke('robot_speak', {'message': 'remote'}, operation_id='remote')
            assert observed['state'] == 'succeeded'
            assert simulator.skills['audio.speak'].calls == 1
        finally:
            try:
                await client.close()
            finally:
                await server.close()


async def test_reconciliation_required_is_preserved_without_reexecution() -> None:
    from robot_agent.robot_tools import RobotTools
    from robot_agent_runtime.models import SkillCall, Task, TaskState

    simulator = Simulator()
    repository = InMemoryTaskRepository()
    await repository.create_task(Task(
        id='uncertain', task_type='agent_action', state=TaskState.RECONCILIATION_REQUIRED,
        steps=[SkillCall(skill='audio.speak', params={'message': 'hello'})],
        idempotency_key='agent:unknown',
    ))
    runtime = RobotRuntime(repository, simulator.skills)
    await runtime.start()
    try:
        adapted = await RobotTools.create(runtime)
        observed = await adapted.invoke('robot_speak', {'message': 'hello'}, operation_id='unknown')
        assert observed['state'] == 'reconciliation_required'
        assert observed['requires_attention'] is True
        assert simulator.skills['audio.speak'].calls == 0
    finally:
        await runtime.close()
