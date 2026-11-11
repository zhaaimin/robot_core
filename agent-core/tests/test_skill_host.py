import copy
import tempfile
from pathlib import Path
from typing import Any

import pytest

from robot_agent_core.ports import Observation
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_core.skillkit.scripts import ApprovedScript, ScriptContext, SkillScriptRunner
from test_generic_skill_host import Backend, make_registry


def action() -> dict[str, Any]:
    return {'name': 'run_skill_script', 'args': {
        'name': 'example-task', 'path': 'scripts/action.py', 'args': {},
    }, 'operation_id': 'frozen-operation', 'call_id': 'call-one'}


def registered_runner(registry: SkillRegistry, backend: Backend) -> SkillScriptRunner:
    async def handler(args: dict[str, Any], context: ScriptContext) -> Observation:
        return await context.tools.invoke('do_action', args, operation_id=context.operation_id)

    resource = registry.read_resource('example-task', 'scripts/action.py')
    return SkillScriptRunner(registry, backend, [ApprovedScript(
        'example-task', 'scripts/action.py', resource['fingerprint'], handler,
    )])


def test_host_schemas_are_scoped_copies_and_reserve_names_only_when_enabled() -> None:
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.skillkit.host import SkillHost

    with tempfile.TemporaryDirectory() as directory:
        registry = make_registry(Path(directory))
        business = Backend().schemas
        before = copy.deepcopy(business)
        host = SkillHost(registry)
        schemas = host.schemas(business, [])
        assert [item['function']['name'] for item in schemas] == ['load_skill', 'read_skill_resource']
        schemas[0]['function']['parameters']['properties']['name']['enum'].append('not-visible')
        fresh = host.schemas(business, ['example-task'])
        assert fresh[0]['function']['parameters']['properties']['name']['enum'] == ['example-task']
        assert fresh[-1]['function']['name'] == 'do_action'
        assert business == before
        collision = [{'function': {'name': 'load_skill'}}]
        with pytest.raises(AgentConfigurationError, match='collide'):
            host.schemas(collision, [])
        assert SkillHost().schemas(collision, []) == collision
        assert SkillHost().handles('load_skill') is False


async def test_host_batch_uses_a_frozen_loading_boundary_without_mutating_input() -> None:
    from robot_agent_core.errors import AgentSkillNotLoaded
    from robot_agent_core.skillkit.host import SkillHost

    with tempfile.TemporaryDirectory() as directory:
        registry = make_registry(Path(directory))
        backend = Backend()
        host = SkillHost(registry, registered_runner(registry, backend))
        state: dict[str, Any] = {}
        batch = host.begin_batch(state)
        loaded = await batch.invoke('load_skill', {'name': 'example-task'}, operation_id='load')
        assert loaded['state'] == 'succeeded'
        assert state == {}
        assert batch.before_loaded == frozenset()
        assert 'script_fingerprint' not in host.freeze_action(action(), [])
        with pytest.raises(AgentSkillNotLoaded):
            await batch.invoke('run_skill_script', action()['args'], operation_id='not-yet')
        with pytest.raises(AgentSkillNotLoaded):
            batch.require_business_access()
        assert backend.calls == []
        saved = batch.update()
        next_batch = host.begin_batch(saved)
        request = action()
        before = copy.deepcopy(request)
        pinned = host.freeze_action(request, next_batch.before_loaded)
        assert pinned['script_fingerprint'] == registry.read_resource('example-task', 'scripts/action.py')['fingerprint']
        assert request == before and pinned is not request
        result = await next_batch.invoke('run_skill_script', pinned['args'], operation_id=pinned['operation_id'])
        assert result['error'] is None
        assert backend.calls == [('do_action', {})]


async def test_host_resource_audit_is_deduplicated_and_input_state_is_unchanged() -> None:
    from robot_agent_core.skillkit.host import SkillHost

    with tempfile.TemporaryDirectory() as directory:
        registry = make_registry(Path(directory))
        saved = registry.load('example-task')
        state = {'active_skill': 'example-task', 'loaded_skills': ['example-task'],
                 'skill_documents': {'example-task': saved}, 'skill_versions': {'example-task': saved['fingerprint']}}
        before = copy.deepcopy(state)
        batch = SkillHost(registry).begin_batch(state)
        for operation in ('read-one', 'read-two'):
            observed = await batch.invoke('read_skill_resource', {
                'name': 'example-task', 'path': 'references/policy.md',
            }, operation_id=operation)
            assert 'violet' in observed['result']['content']
        assert len(batch.update()['loaded_resources']) == 1
        assert state == before


async def test_host_parameter_errors_keep_invalid_request_semantics() -> None:
    from robot_agent_core.errors import AgentToolArgumentsError, AgentToolNotAllowed
    from robot_agent_core.skillkit.host import SkillHost

    with tempfile.TemporaryDirectory() as directory:
        batch = SkillHost(make_registry(Path(directory))).begin_batch({})
        for arguments in ({}, {'name': 1}, {'name': 'example-task', 'extra': True}):
            with pytest.raises(AgentToolArgumentsError) as caught:
                await batch.invoke('load_skill', arguments, operation_id='bad-args')
            assert caught.value.code == 'INVALID_REQUEST'
        with pytest.raises(AgentToolNotAllowed):
            await batch.invoke('unknown-host-tool', {}, operation_id='unknown')


def test_host_rejects_changed_or_missing_frozen_script_versions_before_dispatch() -> None:
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.skillkit.host import SkillHost

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = make_registry(root)
        backend = Backend()
        host = SkillHost(registry, registered_runner(registry, backend))
        saved = registry.load('example-task')
        pending = host.freeze_action(action(), ['example-task'])
        state = {'loaded_skills': ['example-task'], 'skill_documents': {'example-task': saved},
                 'skill_versions': {'example-task': saved['fingerprint']}, 'pending': [pending]}
        missing = copy.deepcopy(state)
        missing['pending'][0].pop('script_fingerprint')
        with pytest.raises(AgentConfigurationError, match='Pending script version'):
            host.begin_batch(missing)
        before = copy.deepcopy(state)
        (root / 'example-task/scripts/action.py').write_text('# New script\n')
        with pytest.raises(AgentConfigurationError, match='Pending script version'):
            host.begin_batch(state)
        assert state == before and backend.calls == []


async def test_another_loaded_skill_does_not_authorize_a_newly_loaded_script() -> None:
    from robot_agent_core.errors import AgentSkillNotLoaded
    from robot_agent_core.skillkit.host import SkillHost

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_registry(root)
        other = root / 'other-skill'
        other.mkdir()
        (other / 'SKILL.md').write_text(
            '---\nname: other-skill\ndescription: Use when helping.\n---\nProvide help.\n',
        )
        registry = SkillRegistry.from_directory(root)
        backend = Backend()
        host = SkillHost(registry, registered_runner(registry, backend))
        saved = registry.load('other-skill')
        batch = host.begin_batch({'loaded_skills': ['other-skill'], 'skill_documents': {'other-skill': saved},
                                  'skill_versions': {'other-skill': saved['fingerprint']}})
        await batch.invoke('load_skill', {'name': 'example-task'}, operation_id='select')
        with pytest.raises(AgentSkillNotLoaded):
            await batch.invoke('run_skill_script', action()['args'], operation_id='blocked')
        assert backend.calls == []


def test_host_prepares_guidance_and_failure_instruction_from_verified_saved_documents() -> None:
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.skillkit.host import SkillHost

    with tempfile.TemporaryDirectory() as directory:
        registry = make_registry(Path(directory))
        saved = registry.load('example-task')
        state = {'active_skill': 'example-task', 'loaded_skills': ['example-task'],
                 'skill_documents': {'example-task': saved}, 'skill_versions': {'example-task': saved['fingerprint']}}
        host = SkillHost(registry)
        assert saved['instructions'] in '\n'.join(host.guidance(host.snapshot(state)))
        assert saved['instructions'] in host.failure_instruction(state)
        assert SkillHost().failure_instruction({}) is None
        with pytest.raises(AgentConfigurationError, match='Active skill'):
            host.guidance(host.snapshot({'active_skill': 'example-task'}))
