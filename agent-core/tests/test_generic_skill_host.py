import hashlib
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from robot_agent_core.ports import Observation


class Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [{'type': 'function', 'function': {
            'name': 'do_action', 'description': 'Test action',
            'parameters': {'type': 'object', 'properties': {}},
        }}]

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Observation:
        self.calls.append((name, args))
        return {'tool': name, 'task_id': None, 'state': 'succeeded',
                'result': {'value': args.get('value')}, 'error': None, 'requires_attention': False}


class ScriptedModel:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[Sequence[BaseMessage], list[dict[str, Any]]]] = []

    async def decide(self, messages: Sequence[BaseMessage], tools: list[dict[str, Any]]) -> AIMessage:
        self.requests.append((list(messages), tools))
        if not self.responses:
            assert tools == []
            return AIMessage(content='执行已暂停，请核对后继续。')
        return self.responses.pop(0)


def call(name: str, args: dict[str, Any], number: int) -> AIMessage:
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': str(number)}])


def make_registry(root: Path) -> Any:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    folder = root / 'example-task'
    folder.mkdir()
    (folder / 'references').mkdir()
    (folder / 'scripts').mkdir()
    (folder / 'SKILL.md').write_text(
        '---\nname: example-task\ndescription: Use when this sample task applies.\n---\n'
        'Read references/policy.md, then run scripts/action.py.\n',
    )
    (folder / 'references/policy.md').write_text('Policy value: violet\n')
    (folder / 'scripts/action.py').write_text('# Approved test entry\n')
    return SkillRegistry.from_directory(root)


async def test_model_selects_reads_and_runs_a_registered_script_through_generic_tools() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent_core.skillkit.scripts import ApprovedScript, ScriptContext, SkillScriptRunner

    with tempfile.TemporaryDirectory() as directory:
        registry = make_registry(Path(directory))
        backend = Backend()

        async def handler(args: dict[str, Any], context: ScriptContext) -> Observation:
            return await context.tools.invoke('do_action', args, operation_id=context.operation_id)

        script = registry.read_resource('example-task', 'scripts/action.py')
        runner = SkillScriptRunner(registry, backend, [ApprovedScript(
            'example-task', 'scripts/action.py', script['fingerprint'], handler,
        )])
        model = ScriptedModel([
            call('load_skill', {'name': 'example-task'}, 1),
            call('read_skill_resource', {'name': 'example-task', 'path': 'references/policy.md'}, 2),
            call('run_skill_script', {'name': 'example-task', 'path': 'scripts/action.py', 'args': {'value': 'violet'}}, 3),
            AIMessage(content='The approved task returned violet'),
        ])
        final = await build_graph().ainvoke(
            {'messages': [HumanMessage(content='Run the sample task')]},
            context=AgentContext(model=model, tools=backend, skills=registry, scripts=runner),
        )
        assert backend.calls == [('do_action', {'value': 'violet'})]
        assert final['active_skill'] == 'example-task'
        assert final['loaded_skills'] == ['example-task']
        assert final['observations'][0]['execution']['path'] == 'scripts/action.py'
        initial_tools = {item['function']['name'] for item in model.requests[0][1]}
        assert 'do_action' not in initial_tools
        assert 'load_skill' in initial_tools
        load_schema = next(item for item in model.requests[0][1] if item['function']['name'] == 'load_skill')
        assert load_schema['function']['parameters']['properties']['name']['enum'] == ['example-task']
        text = '\n'.join(str(item.content) for item in model.requests[0][0] if isinstance(item, SystemMessage))
        assert 'Use when this sample task applies.' in text
        assert 'Read references/policy.md' not in text
        assert 'do_action' in {item['function']['name'] for item in model.requests[1][1]}
        assert any('Policy value: violet' in str(item.content) for item in model.requests[2][0])


async def test_business_action_cannot_skip_loading_or_follow_a_load_in_the_same_batch() -> None:
    from robot_agent_core.graph import AgentContext, build_graph

    with tempfile.TemporaryDirectory() as directory:
        registry = make_registry(Path(directory))
        for include_load in (False, True):
            backend = Backend()
            message = call('do_action', {}, 2)
            if include_load:
                message.tool_calls.insert(0, {'name': 'load_skill', 'args': {'name': 'example-task'}, 'id': '1'})
            final = await build_graph().ainvoke(
                {'messages': [HumanMessage(content='Act now')]},
                context=AgentContext(model=ScriptedModel([message]), tools=backend, skills=registry),
            )
            assert backend.calls == []
            assert final['halted'] is True
            assert final['observations'][-1]['error']['code'] == 'AGENT_SKILL_NOT_LOADED'


async def test_same_batch_load_cannot_run_a_script_or_read_resources_and_stops_later_actions() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent_core.skillkit.scripts import ApprovedScript, ScriptContext, SkillScriptRunner

    with tempfile.TemporaryDirectory() as directory:
        registry = make_registry(Path(directory))
        for tool, arguments in (
            ('run_skill_script', {'name': 'example-task', 'path': 'scripts/action.py', 'args': {}}),
            ('read_skill_resource', {'name': 'example-task', 'path': 'references/policy.md'}),
        ):
            backend = Backend()
            script_calls: list[str] = []

            async def handler(args: dict[str, Any], context: ScriptContext) -> Observation:
                script_calls.append(context.operation_id)
                return await context.tools.invoke('do_action', args, operation_id=context.operation_id)

            source = registry.read_resource('example-task', 'scripts/action.py')
            runner = SkillScriptRunner(registry, backend, [ApprovedScript(
                'example-task', 'scripts/action.py', source['fingerprint'], handler,
            )])
            message = AIMessage(content='', tool_calls=[
                {'name': 'load_skill', 'args': {'name': 'example-task'}, 'id': 'load'},
                {'name': tool, 'args': arguments, 'id': 'use'},
                {'name': 'do_action', 'args': {}, 'id': 'later'},
            ])
            final = await build_graph().ainvoke(
                {'messages': [HumanMessage(content='Load then act')]},
                context=AgentContext(model=ScriptedModel([message]), tools=backend,
                                     skills=registry, scripts=runner),
            )
            assert final['observations'][0]['state'] == 'succeeded'
            assert final['observations'][1]['error']['code'] == 'AGENT_SKILL_NOT_LOADED'
            assert final['observations'][2]['error']['code'] == 'AGENT_ACTION_SKIPPED'
            assert final['loaded_skills'] == ['example-task']
            assert final['halted'] is True
            assert backend.calls == [] and script_calls == []


async def test_reloading_a_previously_loaded_skill_does_not_block_its_script_in_the_same_batch() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent_core.skillkit.scripts import ApprovedScript, ScriptContext, SkillScriptRunner

    with tempfile.TemporaryDirectory() as directory:
        registry = make_registry(Path(directory))
        backend = Backend()

        async def handler(args: dict[str, Any], context: ScriptContext) -> Observation:
            return await context.tools.invoke('do_action', args, operation_id=context.operation_id)

        source = registry.read_resource('example-task', 'scripts/action.py')
        runner = SkillScriptRunner(registry, backend, [ApprovedScript(
            'example-task', 'scripts/action.py', source['fingerprint'], handler,
        )])
        saved = registry.load('example-task')
        message = AIMessage(content='', tool_calls=[
            {'name': 'load_skill', 'args': {'name': 'example-task'}, 'id': 'reload'},
            {'name': 'run_skill_script', 'args': {
                'name': 'example-task', 'path': 'scripts/action.py', 'args': {'value': 'violet'},
            }, 'id': 'run'},
        ])
        final = await build_graph().ainvoke({
            'messages': [HumanMessage(content='Reload then act')], 'active_skill': 'example-task',
            'loaded_skills': ['example-task'], 'skill_documents': {'example-task': saved},
            'skill_versions': {'example-task': saved['fingerprint']},
        }, context=AgentContext(model=ScriptedModel([message, AIMessage(content='done')]),
                                tools=backend, skills=registry, scripts=runner))
        assert final['halted'] is False
        assert backend.calls == [('do_action', {'value': 'violet'})]


async def test_resource_cannot_be_read_without_loading_its_skill() -> None:
    from robot_agent_core.graph import AgentContext, build_graph

    with tempfile.TemporaryDirectory() as directory:
        registry = make_registry(Path(directory))
        final = await build_graph().ainvoke(
            {'messages': [HumanMessage(content='Read the reference')]},
            context=AgentContext(model=ScriptedModel([
                call('read_skill_resource', {'name': 'example-task', 'path': 'references/policy.md'}, 1),
            ]), tools=Backend(), skills=registry),
        )
        assert final['halted'] is True
        assert final['observations'][0]['error']['code'] == 'AGENT_SKILL_NOT_LOADED'


async def test_changed_skill_is_not_silently_used_when_resuming_a_saved_action() -> None:
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent_core.skillkit.catalog import SkillRegistry

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = make_registry(root)
        backend = Backend()
        model = ScriptedModel([
            call('load_skill', {'name': 'example-task'}, 1), AIMessage(content='Loaded'),
            call('do_action', {}, 2),
        ])
        context = AgentContext(model=model, tools=backend, skills=registry)
        saver = InMemorySaver()
        config = {'configurable': {'thread_id': 'versioned'}}
        await build_graph(checkpointer=saver).ainvoke(
            {'messages': [HumanMessage(content='Load the skill')]}, config, context=context, durability='sync',
        )
        await build_graph(checkpointer=saver, interrupt_before=['execute']).ainvoke(
            {'messages': [HumanMessage(content='Run action')], 'model_turns': 0},
            config, context=context, durability='sync',
        )
        path = root / 'example-task/SKILL.md'
        path.write_text(path.read_text() + '\nChanged instructions\n')
        changed = AgentContext(model=model, tools=backend, skills=SkillRegistry.from_directory(root))
        with pytest.raises(AgentConfigurationError, match='changed|version'):
            await build_graph(checkpointer=saver).ainvoke(None, config, context=changed, durability='sync')
        assert backend.calls == []


async def test_saved_script_action_refuses_a_new_published_script_or_a_missing_pin() -> None:
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent_core.skillkit.catalog import SkillRegistry
    from robot_agent_core.skillkit.scripts import ApprovedScript, ScriptContext, SkillScriptRunner

    for mode in ('changed-source', 'missing-pin'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = make_registry(root)
            backend = Backend()

            async def handler(args: dict[str, Any], context: ScriptContext) -> Observation:
                return await context.tools.invoke('do_action', args, operation_id=context.operation_id)

            def runner(source: Any) -> SkillScriptRunner:
                fingerprint = source.read_resource('example-task', 'scripts/action.py')['fingerprint']
                return SkillScriptRunner(source, backend, [ApprovedScript(
                    'example-task', 'scripts/action.py', fingerprint, handler,
                )])

            model = ScriptedModel([
                call('load_skill', {'name': 'example-task'}, 1), AIMessage(content='Loaded'),
                call('run_skill_script', {'name': 'example-task', 'path': 'scripts/action.py', 'args': {}}, 2),
                AIMessage(content='done'),
            ])
            context = AgentContext(model=model, tools=backend, skills=registry, scripts=runner(registry))
            saver = InMemorySaver()
            config = {'configurable': {'thread_id': mode}}
            graph = build_graph(checkpointer=saver)
            await graph.ainvoke({'messages': [HumanMessage(content='load')]}, config, context=context, durability='sync')
            await build_graph(checkpointer=saver, interrupt_before=['execute']).ainvoke(
                {'messages': [HumanMessage(content='execute')], 'model_turns': 0}, config, context=context, durability='sync',
            )
            if mode == 'changed-source':
                (root / 'example-task/scripts/action.py').write_text('# New published script version\n')
            else:
                snapshot = await graph.aget_state(config)
                pending = [dict(action) for action in snapshot.values['pending']]
                pending[0].pop('script_fingerprint', None)
                await graph.aupdate_state(config, {'pending': pending}, as_node='decide')
            new_registry = SkillRegistry.from_directory(root)
            changed = AgentContext(model=model, tools=backend, skills=new_registry, scripts=runner(new_registry))
            with pytest.raises(AgentConfigurationError, match='script|version'):
                await graph.ainvoke(None, config, context=changed, durability='sync')
            assert backend.calls == []


async def test_failed_action_gets_one_skill_guided_response_without_any_execution_tools() -> None:
    from robot_agent_core.graph import AgentContext, build_graph

    class FailedBackend(Backend):
        async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Observation:
            self.calls.append((name, args))
            return {'tool': name, 'task_id': None, 'state': 'failed', 'result': None,
                    'error': {'code': 'INVALID_RECEIPT', 'message': 'Receipt not found', 'details': {}},
                    'requires_attention': True}

    with tempfile.TemporaryDirectory() as directory:
        registry = make_registry(Path(directory))
        for response in (AIMessage(content='凭据没有核验成功，请检查编号。'), call('do_action', {}, 3)):
            backend = FailedBackend()
            model = ScriptedModel([
                call('load_skill', {'name': 'example-task'}, 1), call('do_action', {}, 2), response,
            ])
            final = await build_graph().ainvoke(
                {'messages': [HumanMessage(content='Verify this receipt')]},
                context=AgentContext(model=model, tools=backend, skills=registry),
            )
            assert len(model.requests) == 3
            assert model.requests[-1][1] == []
            assert len(backend.calls) == 1
            assert final['halted'] is True
            assert final['pending'] == []
            assert not final['messages'][-1].tool_calls
            if not response.tool_calls:
                assert final['messages'][-1].content == response.content
