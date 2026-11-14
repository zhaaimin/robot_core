import asyncio
import json
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import SkillCall, SkillDefinition, Task, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RemoteRuntime, RobotRuntime
from robot_agent_runtime.transport.server import RuntimeServer

@asynccontextmanager
async def running() -> AsyncIterator[tuple[RobotRuntime, Simulator]]:
    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        yield runtime, simulator
    finally:
        await runtime.close()


async def test_background_submission_returns_a_handle_and_explicit_cancel_confirms_stop() -> None:
    from robot_agent_capabilities import RobotCapabilities

    async with running() as (runtime, _):
        capabilities = await RobotCapabilities.create(runtime)
        pending = await capabilities.invoke('robot_wait_delay', {'delay_s': 30}, operation_id='background')
        task_id = pending['task_id']
        assert task_id and pending['state'] not in {'succeeded', 'failed', 'canceled'}
        assert pending['requires_attention'] is True
        assert not (await runtime.get_task_status(task_id)).cancel_requested
        stopped = await capabilities.invoke('robot_cancel_task', {'task_id': task_id})
        assert stopped['state'] == 'canceled'


async def test_local_wait_timeout_preserves_queryable_task_without_cancel_or_retry() -> None:
    from robot_agent_capabilities import RobotCapabilities

    async with running() as (runtime, simulator):
        capabilities = await RobotCapabilities.create(runtime, wait_timeout_s=0.01)
        pending = await capabilities.invoke('robot_wait_delay', {'delay_s': 30}, operation_id='waiting', wait=True)
        assert pending['error']['code'] == 'TASK_WAIT_TIMEOUT'
        assert not (await runtime.get_task_status(pending['task_id'])).cancel_requested
        assert simulator.skills['wait.delay'].calls == 1
        await capabilities.invoke('robot_cancel_task', {'task_id': pending['task_id']})


async def test_allowlist_applies_to_catalog_and_invocation() -> None:
    from robot_agent_capabilities import RobotCapabilities

    async with running() as (runtime, simulator):
        capabilities = await RobotCapabilities.create(runtime, allowed_skills={'audio.speak'})
        denied = await capabilities.invoke('robot_navigate_to', {'target': 'room'}, operation_id='denied')
        assert denied['error']['code'] == 'CAPABILITY_NOT_ALLOWED'
        assert simulator.skills['navigation.navigate_to'].calls == 0
        empty = await RobotCapabilities.create(runtime, allowed_skills=set())
        assert {tool.name for tool in empty.tools} == {'robot_task_status', 'robot_cancel_task'}


async def test_invalid_parameters_are_rejected_by_runtime_before_hardware() -> None:
    from robot_agent_capabilities import RobotCapabilities

    async with running() as (runtime, simulator):
        capabilities = await RobotCapabilities.create(runtime)
        observed = await capabilities.invoke('robot_speak', {'message': ''}, operation_id='invalid', wait=True)
        assert observed['state'] == 'failed'
        assert observed['error']['code'] == 'SCHEMA_VALIDATION_FAILED'
        assert simulator.skills['audio.speak'].calls == 0


async def test_missing_stable_operation_id_never_submits() -> None:
    from robot_agent_capabilities import RobotCapabilities

    async with running() as (runtime, simulator):
        capabilities = await RobotCapabilities.create(runtime)
        for invalid in (None, '', '  ', 1, True):
            observed = await capabilities.invoke('robot_speak', {'message': 'hello'}, operation_id=invalid)
            assert observed['error']['code'] == 'INVALID_REQUEST'
        assert simulator.skills['audio.speak'].calls == 0


async def test_operation_id_reuse_with_different_parameters_reports_the_original_task() -> None:
    from robot_agent_capabilities import RobotCapabilities

    async with running() as (runtime, simulator):
        capabilities = await RobotCapabilities.create(runtime)
        first = await capabilities.invoke('robot_speak', {'message': 'first'}, operation_id='identity', wait=True)
        conflict = await capabilities.invoke('robot_speak', {'message': 'second'}, operation_id='identity', wait=True)
        assert conflict['task_id'] == first['task_id']
        assert conflict['error']['code'] == 'INVALID_REQUEST'
        assert conflict['requires_attention'] is True
        assert simulator.skills['audio.speak'].calls == 1


async def test_catalog_and_source_declarations_are_independent_snapshots() -> None:
    from robot_agent_capabilities import RobotCapabilities

    async with running() as (runtime, _):
        definition = SkillDefinition(name='custom.perform', version='1', description='original',
                                     input_schema={'type': 'object', 'properties': {'value': {'type': 'string'}}})
        capabilities = RobotCapabilities(runtime, [definition])
        definition.description = 'changed'
        returned = capabilities.tools
        returned[0].input_schema['properties']['value']['type'] = 'integer'
        assert capabilities.tools[0].description == 'original'
        assert capabilities.tools[0].input_schema['properties']['value']['type'] == 'string'


async def test_invalid_catalog_and_configuration_fail_before_execution() -> None:
    from robot_agent_capabilities import RobotCapabilities
    from robot_agent_capabilities.errors import CapabilityConfigurationError

    async with running() as (runtime, simulator):
        with pytest.raises(CapabilityConfigurationError):
            await RobotCapabilities.create(runtime, allowed_skills={'missing'})
        for invalid in (0, -1, True, float('nan'), float('inf')):
            with pytest.raises(CapabilityConfigurationError):
                await RobotCapabilities.create(runtime, wait_timeout_s=invalid)
        for names in (['a.b', 'a_b'], ['task_status'], ['x' * 65], ['é']):
            definitions = [SkillDefinition(name=name, version='1', input_schema={'type': 'object'}) for name in names]
            with pytest.raises(CapabilityConfigurationError):
                RobotCapabilities(runtime, definitions)
        assert simulator.skills['audio.speak'].calls == 0


async def test_uncertain_recovered_task_is_observed_without_reexecution() -> None:
    from robot_agent_capabilities import RobotCapabilities

    simulator = Simulator()
    repository = InMemoryTaskRepository()
    await repository.create_task(Task(
        id='uncertain', task_type='robot_capability', state=TaskState.RUNNING,
        steps=[SkillCall(skill='audio.speak', params={'message': 'hello'})], idempotency_key='robot:recovered',
    ))
    runtime = RobotRuntime(repository, simulator.skills)
    await runtime.start()
    try:
        capabilities = await RobotCapabilities.create(runtime)
        observed = await capabilities.invoke('robot_speak', {'message': 'hello'}, operation_id='recovered', wait=True)
        assert observed['state'] == 'reconciliation_required'
        assert observed['requires_attention'] is True
        assert simulator.skills['audio.speak'].calls == 0
    finally:
        await runtime.close()


async def test_transport_uncertainty_keeps_the_original_operation_identity() -> None:
    from robot_agent_capabilities import RobotCapabilities

    simulator = Simulator()
    with tempfile.TemporaryDirectory() as directory:
        socket = str(Path(directory) / 'runtime.sock')
        server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, socket)
        runtime = RemoteRuntime(socket)
        await server.start()
        try:
            capabilities = await RobotCapabilities.create(runtime)
            await server.close()
            observed = await capabilities.invoke('robot_speak', {'message': 'hello'}, operation_id='uncertain')
            assert observed['error']['code'] == 'CAPABILITY_TRANSPORT_UNCERTAIN'
            assert observed['error']['details']['operation_id'] == 'uncertain'
            assert observed['error']['details']['outcome_uncertain'] is True
            assert simulator.skills['audio.speak'].calls == 0
        finally:
            await runtime.close()
            await server.close()


async def test_wait_timeout_does_not_start_an_unbounded_followup_query() -> None:
    from robot_agent_capabilities import RobotCapabilities

    async with running() as (runtime, _):
        class UnavailableStatus:
            def __getattr__(self, name: str) -> object:
                return getattr(runtime, name)

            async def get_task_status(self, task_id: str) -> Task:
                await asyncio.Event().wait()
                raise AssertionError('unreachable')

        capabilities = await RobotCapabilities.create(UnavailableStatus(), wait_timeout_s=0.01)
        async with asyncio.timeout(0.2):
            observed = await capabilities.invoke('robot_wait_delay', {'delay_s': 30},
                                                  operation_id='bounded', wait=True)
        assert observed['task_id'] and observed['state'] is None
        assert observed['error']['code'] == 'TASK_WAIT_TIMEOUT'
        await runtime.cancel_task(observed['task_id'])


async def test_operation_identity_distinguishes_json_boolean_from_number() -> None:
    from typing import Any

    from robot_agent_capabilities import RobotCapabilities
    from robot_agent_runtime.adapters.simulator import SimulatedSkill
    from robot_agent_runtime.models import SkillContext

    class ValueSkill(SimulatedSkill):
        async def _perform(self, context: SkillContext, params: dict[str, Any]) -> dict[str, Any]:
            return dict(params)

    skill = ValueSkill(SkillDefinition(
        name='custom.value', version='1', description='Return a JSON scalar',
        input_schema={'type': 'object', 'properties': {'value': {'type': ['boolean', 'number']}},
                      'required': ['value'], 'additionalProperties': False},
        resources=set(), permissions=set(), preconditions=set(), timeout_s=1, supports_cancel=True,
    ), delay_s=0, failures=set())
    runtime = RobotRuntime(InMemoryTaskRepository(), {'custom.value': skill})
    await runtime.start()
    try:
        capabilities = await RobotCapabilities.create(runtime)
        first = await capabilities.invoke('robot_custom_value', {'value': True}, operation_id='typed', wait=True)
        changed = await capabilities.invoke('robot_custom_value', {'value': 1}, operation_id='typed', wait=True)
        assert first['error'] is None
        assert changed['task_id'] == first['task_id']
        assert changed['error']['code'] == 'INVALID_REQUEST'
        assert skill.calls == 1
    finally:
        await runtime.close()


async def test_early_wait_timeout_only_spends_the_remaining_budget_on_recovery() -> None:
    from robot_agent_capabilities import RobotCapabilities
    from robot_agent_runtime.errors import TaskWaitTimeout
    from robot_agent_runtime.models import TaskRequest

    async with running() as (runtime, _):
        original = await RobotCapabilities.create(runtime)
        pending = await original.invoke('robot_wait_delay', {'delay_s': 30}, operation_id='early-deadline')
        query_started, query_canceled = asyncio.Event(), asyncio.Event()

        class EarlyTimeout:
            def __getattr__(self, name: str) -> object:
                return getattr(runtime, name)

            async def submit_and_wait(self, request: TaskRequest, *, timeout_s: float | None = None) -> Task:
                raise TaskWaitTimeout('Remote wait ended early', {'task_id': pending['task_id']})

            async def get_task_status(self, task_id: str) -> Task:
                query_started.set()
                try:
                    await asyncio.Event().wait()
                    raise AssertionError('unreachable')
                finally:
                    query_canceled.set()

        capabilities = await RobotCapabilities.create(EarlyTimeout(), wait_timeout_s=0.03)
        async with asyncio.timeout(0.5):
            observed = await capabilities.invoke('robot_wait_delay', {'delay_s': 30},
                                                  operation_id='early-deadline', wait=True)
        assert observed['task_id'] == pending['task_id']
        assert observed['error']['code'] == 'TASK_WAIT_TIMEOUT'
        assert query_started.is_set() and query_canceled.is_set()
        await runtime.cancel_task(pending['task_id'])


def test_capabilities_import_without_an_agent_model_or_mcp_framework() -> None:
    script = '''
import importlib.abc, json, sys
class DenyFrameworks(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'robot_agent_core', 'robot_agent', 'hotel_agent', 'guide_agent', 'langgraph', 'langchain_core', 'langchain_openai', 'mcp'}:
            raise AssertionError('Unexpected framework import: ' + fullname)
sys.meta_path.insert(0, DenyFrameworks())
from robot_agent_capabilities import RobotCapabilities
from robot_agent_capabilities.ports import TaskRuntime
print(json.dumps({'client': RobotCapabilities.__name__, 'port': TaskRuntime.__name__}))
'''
    process = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=5)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {'client': 'RobotCapabilities', 'port': 'TaskRuntime'}
