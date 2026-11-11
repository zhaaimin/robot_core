import hashlib
import json
import importlib
import io
import shutil
import tempfile
from contextlib import redirect_stderr
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest

from hotel_agent.errors import HotelWorkflowError
from robot_agent_core.ports import Observation
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_runtime.errors import AgentRuntimeError


_RECEPTION = 'frontdesk-reception'
_HANDOFF = 'guest-handoff'
_ESCORT = 'escort-assistance'
_QUERY = 'scripts/query_reception.py'
_ASSIGN = 'scripts/assign_butler.py'
_START = 'scripts/start_escort.py'
_PACKAGES = {
    _RECEPTION: {
        'references/hotel-information.md', 'references/reception-rules.md',
        'references/reception-request-schema.md', _QUERY,
    },
    _HANDOFF: {
        'references/handoff-rules.md', 'references/handoff-request-schema.md', _ASSIGN,
    },
    _ESCORT: {
        'references/escort-rules.md', 'references/escort-request-schema.md', _START,
    },
}
_ACTIONS = (
    (_RECEPTION, _QUERY, {'receipt_code': 'DEMO-001'}, 'hotel_confirm_reception'),
    (_HANDOFF, _ASSIGN, {}, 'hotel_assign_butler'),
    (_ESCORT, _START, {}, 'hotel_start_escort'),
)


class RecordingTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.observation: Observation = {
            'tool': 'hotel-observation', 'task_id': 'saved-task', 'state': 'accepted',
            'result': {'phase': 'waiting_for_guest', 'room_id': '0108'},
            'error': None, 'requires_attention': False,
        }

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return []

    async def invoke(
        self, name: str, args: dict[str, Any], *, operation_id: str,
    ) -> Observation:
        self.calls.append((name, args, operation_id))
        return self.observation


def copy_bundled_skill(root: Path, name: str) -> Path:
    source = files('hotel_agent').joinpath('skills', name)
    target = root / name
    shutil.copytree(str(source), target)
    return target


def test_bundled_catalog_contains_three_independent_business_packages() -> None:
    registry = SkillRegistry.bundled('hotel_agent')
    assert [item['name'] for item in registry.catalog()] == sorted(_PACKAGES)
    for name, paths in _PACKAGES.items():
        loaded = registry.load(name)
        assert loaded['description'].startswith('Use when')
        assert set(registry.resources(name)) == paths
        content = loaded['instructions']
        for path in paths:
            resource = registry.read_resource(name, path)
            assert resource['content'].strip()
            assert resource['fingerprint'] == hashlib.sha256(resource['content'].encode()).hexdigest()
            assert path in loaded['instructions']
            content += '\n' + resource['content']
        for other in _PACKAGES.keys() - {name}:
            assert other not in content


async def test_published_scripts_delegate_only_their_own_action_with_the_saved_operation() -> None:
    from robot_agent_core.skillkit.scripts import ScriptContext

    tools = RecordingTools()
    for name, path, arguments, delegated_tool in _ACTIONS:
        module = importlib.import_module(f'hotel_agent.skills.{name}.scripts.{Path(path).stem}')
        context = ScriptContext(tools=tools, operation_id=f'{name}-operation')
        observed = await module.run(arguments, context)
        assert observed is tools.observation
        assert tools.calls[-1] == (delegated_tool, arguments, f'{name}-operation')
    assert len(tools.calls) == 3


async def test_bundled_scripts_reject_missing_extra_and_forged_fields_before_tools() -> None:
    from robot_agent_core.skillkit.scripts import ScriptContext

    tools = RecordingTools()
    context = ScriptContext(tools=tools, operation_id='invalid-operation')
    query = importlib.import_module(f'hotel_agent.skills.{_RECEPTION}.scripts.query_reception')
    invalid_queries: list[Any] = [
        None, [], {}, {'receipt_code': None}, {'receipt_code': True},
        {'receipt_code': ''}, {'receipt_code': '  '}, {'receipt_code': 'x' * 129},
        {'receipt_code': 'DEMO-001', 'room_id': '0201'},
        {'receipt_code': 'DEMO-001', 'confirmed': True},
    ]
    for arguments in invalid_queries:
        with pytest.raises(HotelWorkflowError):
            await query.run(arguments, context)
    for name, path in ((_HANDOFF, _ASSIGN), (_ESCORT, _START)):
        module = importlib.import_module(f'hotel_agent.skills.{name}.scripts.{Path(path).stem}')
        for arguments in (
            None, [], {'action': 'assign'}, {'action': 'start'}, {'robot_id': 'other'},
            {'destination_id': 'other-room'}, {'coordinates': [1, 2]},
            {'guest_ready': True}, {'consent_confirmed': True}, {'arrival_confirmed': True},
        ):
            with pytest.raises(HotelWorkflowError):
                await module.run(arguments, context)
    assert tools.calls == []


async def test_published_bindings_execute_scripts_and_preserve_the_tool_result() -> None:
    from hotel_agent.script_bindings import build_hotel_script_runner

    registry = SkillRegistry.bundled('hotel_agent')
    tools = RecordingTools()
    runner = build_hotel_script_runner(registry, tools)
    for name, path, args, _ in _ACTIONS:
        observed = await runner.invoke(name, path, args, operation_id=f'run:{name}')
        assert observed['tool'] == 'run_skill_script'
        assert observed['result'] == tools.observation['result']
        assert observed['state'] == tools.observation['state']
        assert observed['task_id'] == tools.observation['task_id']
        assert observed['error'] == tools.observation['error']
        assert observed['requires_attention'] == tools.observation['requires_attention']
        assert observed['execution'] == {
            'skill': name, 'path': path,
            'fingerprint': registry.read_resource(name, path)['fingerprint'],
        }
    assert [call[0] for call in tools.calls] == [action[3] for action in _ACTIONS]
    assert 'execution' not in tools.observation


async def test_each_package_can_bind_alone_and_cannot_read_or_run_other_packages() -> None:
    from hotel_agent.script_bindings import build_hotel_script_runner

    for name, path, args, delegated_tool in _ACTIONS:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_bundled_skill(root, name)
            registry = SkillRegistry.from_directory(root)
            tools = RecordingTools()
            runner = build_hotel_script_runner(registry, tools)
            assert [item['name'] for item in registry.catalog()] == [name]
            observed = await runner.invoke(name, path, args, operation_id='selected-package')
            assert observed['error'] is None
            assert tools.calls == [(delegated_tool, args, 'selected-package')]
            for other, other_path, other_args, _ in _ACTIONS:
                if other == name:
                    continue
                with pytest.raises(AgentRuntimeError):
                    registry.read_resource(other, other_path)
                denied = await runner.invoke(other, other_path, other_args, operation_id='not-visible')
                assert denied['error'] is not None
            assert len(tools.calls) == 1


async def test_custom_hotel_guidance_can_change_without_replacing_published_script_code() -> None:
    from hotel_agent.script_bindings import build_hotel_script_runner

    for name, path, args, delegated_tool in _ACTIONS:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = copy_bundled_skill(root, name)
            document = skill / 'SKILL.md'
            document.write_text(document.read_text(encoding='utf-8') + '\nUse concise local wording.\n',
                                encoding='utf-8')
            tools = RecordingTools()
            runner = build_hotel_script_runner(SkillRegistry.from_directory(root), tools)
            observed = await runner.invoke(name, path, args, operation_id='custom-doc')
            assert observed['error'] is None
            assert tools.calls == [(delegated_tool, args, 'custom-doc')]


async def test_custom_hotel_script_code_stays_unbound_without_disabling_the_skill() -> None:
    from hotel_agent.script_bindings import build_hotel_script_runner

    for name, path, args, _ in _ACTIONS:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = copy_bundled_skill(root, name)
            (skill / path).write_text('raise AssertionError("unregistered code executed")\n',
                                      encoding='utf-8')
            tools = RecordingTools()
            registry = SkillRegistry.from_directory(root)
            with redirect_stderr(io.StringIO()) as warnings:
                runner = build_hotel_script_runner(registry, tools)
            assert registry.load(name)['name'] == name
            observed = await runner.invoke(name, path, args, operation_id='unapproved-script')
            assert observed['error']['code'] == 'AGENT_TOOL_NOT_ALLOWED'
            assert observed['requires_attention'] is True
            assert name in warnings.getvalue() and path in warnings.getvalue()
            assert tools.calls == []


async def test_document_only_hotel_skills_remain_loadable_without_script_bindings() -> None:
    from hotel_agent.script_bindings import build_hotel_script_runner

    for name, path, args, _ in _ACTIONS:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / name
            skill.mkdir()
            (skill / 'SKILL.md').write_text(
                f'---\nname: {name}\ndescription: Use when answering hotel questions.\n---\n'
                'Provide guidance without a script.\n', encoding='utf-8',
            )
            registry = SkillRegistry.from_directory(root)
            tools = RecordingTools()
            with redirect_stderr(io.StringIO()) as warnings:
                runner = build_hotel_script_runner(registry, tools)
            assert registry.load(name)['instructions'] == 'Provide guidance without a script.'
            assert registry.resources(name) == []
            observed = await runner.invoke(name, path, args, operation_id='document-only')
            assert observed['error']['code'] == 'AGENT_TOOL_NOT_ALLOWED'
            assert observed['requires_attention'] is True
            assert name in warnings.getvalue() and path in warnings.getvalue()
            assert tools.calls == []


async def test_unavailable_scripts_do_not_disable_other_published_hotel_scripts() -> None:
    from hotel_agent.script_bindings import build_hotel_script_runner

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        document_only = root / _RECEPTION
        document_only.mkdir()
        (document_only / 'SKILL.md').write_text(
            f'---\nname: {_RECEPTION}\ndescription: Use when answering questions.\n---\n'
            'Answer from supplied facts.\n', encoding='utf-8',
        )
        replaced = copy_bundled_skill(root, _HANDOFF)
        (replaced / _ASSIGN).write_text('raise AssertionError("unapproved code executed")\n',
                                       encoding='utf-8')
        copy_bundled_skill(root, _ESCORT)
        tools = RecordingTools()
        with redirect_stderr(io.StringIO()) as warnings:
            runner = build_hotel_script_runner(SkillRegistry.from_directory(root), tools)
        assert len(warnings.getvalue().splitlines()) == 2
        for name, path, args, _ in _ACTIONS[:2]:
            denied = await runner.invoke(name, path, args, operation_id='not-approved')
            assert denied['error']['code'] == 'AGENT_TOOL_NOT_ALLOWED'
        observed = await runner.invoke(_ESCORT, _START, {}, operation_id='published-escort')
        assert observed['error'] is None
        assert tools.calls == [('hotel_start_escort', {}, 'published-escort')]


async def test_hotel_script_changed_after_registration_is_not_executed() -> None:
    from hotel_agent.script_bindings import build_hotel_script_runner

    for name, path, args, _ in _ACTIONS:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = copy_bundled_skill(root, name)
            tools = RecordingTools()
            runner = build_hotel_script_runner(SkillRegistry.from_directory(root), tools)
            (skill / path).write_text('raise AssertionError("unregistered code executed")\n',
                                      encoding='utf-8')
            observed = await runner.invoke(name, path, args, operation_id='untrusted-script')
            assert observed['error'] is not None
            assert observed['requires_attention'] is True
            assert tools.calls == []


async def test_hotel_binding_factory_does_not_require_a_hotel_skill_in_custom_catalogs() -> None:
    from hotel_agent.script_bindings import build_hotel_script_runner

    registry = SkillRegistry([{
        'name': 'local-help', 'description': 'Use when local help is needed.',
        'instructions': 'Answer the question.', 'fingerprint': 'a' * 64,
    }])
    tools = RecordingTools()
    runner = build_hotel_script_runner(registry, tools)
    for name, path, args, _ in _ACTIONS:
        observed = await runner.invoke(name, path, args, operation_id='not-in-catalog')
        assert observed['error'] is not None
    assert tools.calls == []


async def test_single_package_uses_business_facts_prepared_without_other_skills() -> None:
    from hotel_agent.script_bindings import build_hotel_script_runner
    from hotel_agent.service import HotelWorkflow
    from hotel_agent.simulator import HotelSimulator
    from hotel_agent.tools import HotelTools
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.sdk import RobotRuntime

    simulator = HotelSimulator(delay_s=0)
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workflow = HotelWorkflow(str(root / 'hotel.sqlite3'), runtime)
        await workflow.start()
        try:
            for name, path, args, _ in _ACTIONS:
                if name != _RECEPTION:
                    await workflow.confirm_reception(name, 'DEMO-001', operation_id=f'{name}:receipt')
                    await workflow.record_guest_event(name, 'consent')
                if name == _ESCORT:
                    await workflow.assign_robot(name, operation_id=f'{name}:assign')
                    await workflow.record_guest_event(name, 'ready')
                catalog_root = root / name
                catalog_root.mkdir()
                copy_bundled_skill(catalog_root, name)
                runner = build_hotel_script_runner(
                    SkillRegistry.from_directory(catalog_root), HotelTools(workflow, name),
                )
                observed = await runner.invoke(name, path, args, operation_id=f'{name}:selected')
                assert observed['error'] is None
                assert observed['execution']['skill'] == name
                if name == _HANDOFF:
                    assert observed['result']['robot_id'] == 'butler-1'
                    assert simulator.skills['navigation.navigate_to'].calls == 0
                    await workflow.cancel(name, operation_id=f'{name}:release')
                elif name == _ESCORT:
                    settled = await workflow.wait_for_settled(name)
                    assert settled['phase'] == 'awaiting_arrival_confirmation'
                    assert settled['arrival_confirmed'] is False
        finally:
            try:
                await workflow.close()
            finally:
                await runtime.close()


async def test_registered_scripts_follow_real_workflow_guards_and_simulated_escort() -> None:
    from hotel_agent.script_bindings import build_hotel_script_runner
    from hotel_agent.service import HotelWorkflow
    from hotel_agent.simulator import HotelSimulator
    from hotel_agent.tools import HotelTools
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.sdk import RobotRuntime

    simulator = HotelSimulator(delay_s=0)
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    with tempfile.TemporaryDirectory() as directory:
        workflow = HotelWorkflow(str(Path(directory) / 'hotel.sqlite3'), runtime)
        await workflow.start()
        try:
            tools = HotelTools(workflow, 'script-guest')
            runner = build_hotel_script_runner(SkillRegistry.bundled('hotel_agent'), tools)
            invalid = await runner.invoke(
                _RECEPTION, _QUERY, {'receipt_code': 'unknown'}, operation_id='unknown-receipt',
            )
            assert invalid['error']['code'] == 'HOTEL_WORKFLOW_REJECTED'
            assert invalid['requires_attention'] is True
            reception = await runner.invoke(
                _RECEPTION, _QUERY, {'receipt_code': 'DEMO-001'}, operation_id='verify-receipt',
            )
            assert reception['tool'] == 'run_skill_script'
            assert reception['result']['phase'] == 'awaiting_consent'
            assert reception['result']['room_id'] == '0108'
            assert reception['result']['destination_id'] == 'room-0108-door'
            assert reception['execution']['path'] == _QUERY
            unconfirmed = await runner.invoke(_HANDOFF, _ASSIGN, {}, operation_id='missing-consent')
            assert unconfirmed['error']['code'] == 'HOTEL_WORKFLOW_REJECTED'
            assert (await workflow.snapshot('script-guest'))['consent_confirmed'] is False
            await workflow.record_guest_event('script-guest', 'consent')
            assignment = await runner.invoke(_HANDOFF, _ASSIGN, {}, operation_id='assign-robot')
            assert assignment['result']['robot_id'] == 'butler-1'
            assert assignment['result']['meeting_point'] == '大堂前台会合点'
            repeated = await runner.invoke(_HANDOFF, _ASSIGN, {}, operation_id='observe-existing-assignment')
            assert repeated['result']['service_id'] == assignment['result']['service_id']
            premature = await runner.invoke(_ESCORT, _START, {}, operation_id='missing-readiness')
            assert premature['error']['code'] == 'HOTEL_WORKFLOW_REJECTED'
            assert simulator.skills['navigation.navigate_to'].calls == 0
            await workflow.record_guest_event('script-guest', 'ready')
            started = await runner.invoke(_ESCORT, _START, {}, operation_id='start-escort')
            assert started['tool'] == 'run_skill_script'
            assert started['state'] == 'accepted'
            assert started['task_id']
            repeated = await runner.invoke(_ESCORT, _START, {}, operation_id='observe-existing-escort')
            assert repeated['task_id'] == started['task_id']
            settled = await workflow.wait_for_settled('script-guest')
            assert settled['phase'] == 'awaiting_arrival_confirmation'
            assert settled['arrival_confirmed'] is False
            assert simulator.skills['navigation.navigate_to'].calls == 1
            awaiting = await tools.invoke('hotel_status', {}, operation_id='await-arrival')
            assert awaiting['state'] == 'accepted'
            await workflow.record_guest_event('script-guest', 'arrived')
            completed = await tools.invoke('hotel_status', {}, operation_id='confirmed-arrival')
            assert completed['state'] == 'succeeded'
            assert completed['result']['phase'] == 'completed'
            assert completed['result']['arrival_confirmed'] is True
            returned = await workflow.wait_for_settled('script-guest')
            assert returned['robot_status'] == 'available'
            assert simulator.skills['navigation.navigate_to'].calls == 2
        finally:
            try:
                await workflow.close()
            finally:
                await runtime.close()


def test_bundled_hotel_skills_provide_metadata_and_separate_complete_instructions() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    registry = SkillRegistry.bundled('hotel_agent')
    assert [item["name"] for item in registry.catalog()] == [
        "escort-assistance", "frontdesk-reception", "guest-handoff",
    ]
    serialized_catalog = json.dumps(registry.catalog())
    assert len(serialized_catalog) < 1024
    for item in registry.catalog():
        assert set(item) == {"name", "description"}
        assert item["description"].startswith("Use when")
        loaded = registry.load(item["name"])
        assert set(loaded) == {"name", "description", "instructions", "fingerprint"}
        assert len(loaded["fingerprint"]) == 64
        assert loaded["instructions"].strip()
        assert loaded["instructions"] not in serialized_catalog
        for resource in registry.resources(item["name"]):
            assert resource in loaded["instructions"]
            assert resource in registry.resources(item["name"])
