import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from robot_agent_core.errors import AgentConfigurationError, AgentToolNotAllowed
from robot_agent_core.graph import AgentContext, build_graph
from robot_agent_core.skillkit.catalog import SkillRegistry


class NoPhysicalTools:
    schemas: list[dict[str, Any]] = []

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Any:
        raise AssertionError('Selecting a skill must not call a robot or hotel interface')


class NoModel:
    async def decide(self, messages: Any, tools: Any) -> AIMessage:
        raise AssertionError('Skill commands must not ask a model to choose or load instructions')


def write_skill(root: Path, name: str, body: str) -> Path:
    folder = root / name
    reference = folder / 'references' / 'rules.md'
    reference.parent.mkdir(parents=True, exist_ok=True)
    (folder / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: Use when {name} applies.\n---\n{body}\n', encoding='utf-8',
    )
    reference.write_text(f'{name} scoped reference', encoding='utf-8')
    return folder


def test_registry_selection_preserves_snapshots_and_scoped_resources() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        alpha = write_skill(root, 'alpha', 'ALPHA-BODY')
        write_skill(root, 'beta', 'BETA-BODY')
        original = SkillRegistry.from_directory(root)
        alpha_document = original.load('alpha')
        (alpha / 'SKILL.md').write_text('replaced after validation', encoding='utf-8')
        selected = original.select(['alpha', 'alpha'])
        assert selected.catalog() == [{'name': 'alpha', 'description': 'Use when alpha applies.'}]
        assert selected.load('alpha') == alpha_document
        assert selected.resources('alpha') == ['references/rules.md']
        assert selected.read_resource('alpha', 'references/rules.md')['content'] == 'alpha scoped reference'
        selected.load('alpha')['instructions'] = 'mutated copy'
        assert original.load('alpha') == alpha_document
        assert [item['name'] for item in original.catalog()] == ['alpha', 'beta']
        for access in (selected.load, selected.resources):
            with pytest.raises(AgentToolNotAllowed):
                access('beta')
        with pytest.raises(AgentToolNotAllowed):
            selected.read_resource('beta', 'references/rules.md')
        outside = root / 'outside.md'
        outside.write_text('private', encoding='utf-8')
        (alpha / 'references' / 'rules.md').unlink()
        (alpha / 'references' / 'rules.md').symlink_to(outside)
        with pytest.raises(AgentToolNotAllowed):
            selected.read_resource('alpha', 'references/rules.md')
        assert original.select([]).catalog() == []
        for names in (['missing'], ['../alpha'], [''], [1], 'alpha'):
            with pytest.raises(AgentConfigurationError):
                original.select(names)  # type: ignore[arg-type]


async def test_explicit_selection_loads_one_body_with_audit_and_without_model_calls() -> None:
    from robot_agent_core.shell import invoke_turn

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_skill(root, 'alpha', 'ALPHA-BODY')
        write_skill(root, 'beta', 'BETA-BODY')
        registry = SkillRegistry.from_directory(root)
        graph = build_graph(checkpointer=InMemorySaver())
        context = AgentContext(model=NoModel(), tools=NoPhysicalTools(), skills=registry)
        result = await invoke_turn(
            graph, context, thread_id='one', runtime_id='runtime-one', prompt='/skill alpha',
        )
        assert result['active_skill'] == 'alpha'
        assert result['loaded_skills'] == ['alpha']
        assert set(result['skill_documents']) == {'alpha'}
        assert result['skill_documents']['alpha'] == registry.load('alpha')
        assert result['skill_versions'] == {'alpha': registry.load('alpha')['fingerprint']}
        assert result['model_turns'] == 0
        assert result['halted'] is False
        assert result['runtime_id'] == 'runtime-one'
        assert result['pending'] == []
        assert isinstance(result['messages'][0], HumanMessage)
        assert result['messages'][0].content == '/skill alpha'
        call, observed, reply = result['messages'][1:]
        assert isinstance(call, AIMessage)
        assert call.tool_calls == [{'name': 'load_skill', 'args': {'name': 'alpha'},
                                    'id': call.tool_calls[0]['id'], 'type': 'tool_call'}]
        assert call.tool_calls[0]['id']
        assert isinstance(observed, ToolMessage)
        assert observed.name == 'load_skill'
        assert observed.tool_call_id == call.tool_calls[0]['id']
        observation = json.loads(str(observed.content))
        assert observation == result['observations'][0]
        assert observation == {
            'tool': 'load_skill', 'task_id': None, 'state': 'succeeded',
            'result': {**registry.load('alpha'), 'resources': registry.resources('alpha')},
            'error': None, 'requires_attention': False,
        }
        assert isinstance(reply, AIMessage) and 'alpha' in str(reply.content)
        assert not reply.tool_calls
        checkpoint = await graph.aget_state({'configurable': {'thread_id': 'one'}})
        assert checkpoint.next == ()
        assert checkpoint.values == result
        assert not {'consent', 'ready', 'arrival'} & result.keys()


async def test_switching_active_skill_keeps_only_current_body_in_model_guidance() -> None:
    from robot_agent_core.shell import invoke_turn

    class Model:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            system = '\n'.join(str(item.content) for item in messages if isinstance(item, SystemMessage))
            assert 'BETA-BODY' in system
            assert 'ALPHA-BODY' not in system
            return AIMessage(content='beta selected')

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_skill(root, 'alpha', 'ALPHA-BODY')
        write_skill(root, 'beta', 'BETA-BODY')
        registry = SkillRegistry.from_directory(root)
        graph = build_graph(checkpointer=InMemorySaver())
        context = AgentContext(model=Model(), tools=NoPhysicalTools(), skills=registry)
        for name in ('alpha', 'beta'):
            selected = await invoke_turn(
                graph, context, thread_id='one', runtime_id='robot', prompt='/skill ' + name,
            )
        assert selected['loaded_skills'] == ['alpha', 'beta']
        assert selected['active_skill'] == 'beta'
        final = await invoke_turn(graph, context, thread_id='one', runtime_id='robot', prompt='help')
        assert final['messages'][-1].content == 'beta selected'


async def test_catalog_listing_is_read_only_and_does_not_load_bodies_even_with_pending_work() -> None:
    from robot_agent_core.shell import invoke_turn
    from robot_agent_core.skillkit.selection import handle_skill_command

    class CatalogOnlyRegistry(SkillRegistry):
        def load(self, name: str) -> dict[str, str]:
            raise AssertionError('Listing a catalog must not load any body')

    registry = CatalogOnlyRegistry([{
        'name': 'alpha', 'description': 'Use alpha.', 'instructions': 'PRIVATE-BODY', 'fingerprint': 'v1',
    }])
    graph = build_graph(checkpointer=InMemorySaver())
    config = {'configurable': {'thread_id': 'one'}}
    await graph.aupdate_state(config, {
        'messages': [HumanMessage(content='durable request')], 'runtime_id': 'robot',
        'active_skill': 'alpha', 'loaded_skills': ['alpha'], 'pending': [{
            'name': 'robot_speak', 'args': {'message': 'once'}, 'call_id': 'c1', 'operation_id': 'op1',
        }],
    }, as_node='decide')
    before = await graph.aget_state(config)
    listed = await invoke_turn(
        graph, AgentContext(model=NoModel(), tools=NoPhysicalTools(), skills=registry),
        thread_id='one', runtime_id='robot', prompt='/skills',
    )
    assert listed is not None
    assert listed['skill_catalog'] == [{'name': 'alpha', 'description': 'Use alpha.'}]
    assert listed['active_skill'] == 'alpha'
    assert listed['loaded_skills'] == ['alpha']
    assert 'PRIVATE-BODY' not in str(listed['messages'][-1].content)
    after = await graph.aget_state(config)
    assert after.config == before.config
    assert after.values == before.values
    assert after.next == before.next == ('execute',)
    with pytest.raises(AgentConfigurationError, match='pending|resume'):
        await handle_skill_command(graph, registry, thread_id='one', runtime_id='robot', prompt='/skill alpha')
    assert (await graph.aget_state(config)).values == before.values


async def test_catalog_listing_does_not_create_an_empty_thread_checkpoint() -> None:
    from robot_agent_core.skillkit.selection import handle_skill_command

    registry = SkillRegistry([{
        'name': 'alpha', 'description': 'Use alpha.', 'instructions': 'PRIVATE-BODY', 'fingerprint': 'v1',
    }])
    graph = build_graph(checkpointer=InMemorySaver())
    config = {'configurable': {'thread_id': 'new'}}
    listed = await handle_skill_command(graph, registry, thread_id='new', runtime_id='robot', prompt='/skills')
    assert listed is not None
    assert listed['loaded_skills'] == []
    assert listed['active_skill'] is None
    assert listed['skill_catalog'] == registry.catalog()
    assert not (await graph.aget_state(config)).values


async def test_selection_rejects_hidden_malformed_runtime_and_version_changes_without_writing() -> None:
    from robot_agent_core.skillkit.selection import handle_skill_command

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_skill(root, 'alpha', 'ALPHA-BODY')
        write_skill(root, 'beta', 'BETA-BODY')
        registry = SkillRegistry.from_directory(root)
        selected = registry.select(['alpha'])
        graph = build_graph(checkpointer=InMemorySaver())
        config = {'configurable': {'thread_id': 'one'}}
        assert await handle_skill_command(
            graph, selected, thread_id='one', runtime_id='robot', prompt='/skillful normal request',
        ) is None
        for prompt in ('/skill', '/skill alpha beta', '/skills alpha', '/skill ../alpha', '/skill beta'):
            with pytest.raises(AgentConfigurationError):
                await handle_skill_command(graph, selected, thread_id='one', runtime_id='robot', prompt=prompt)
            assert not (await graph.aget_state(config)).values
        await handle_skill_command(graph, selected, thread_id='one', runtime_id='robot', prompt='/skill alpha')
        before = await graph.aget_state(config)
        with pytest.raises(AgentConfigurationError, match='runtime|backend|robot'):
            await handle_skill_command(graph, selected, thread_id='one', runtime_id='other', prompt='/skill alpha')
        assert (await graph.aget_state(config)).config == before.config
        write_skill(root, 'alpha', 'ALPHA-CHANGED')
        changed = SkillRegistry.from_directory(root)
        with pytest.raises(AgentConfigurationError, match='version|new conversation'):
            await handle_skill_command(graph, changed, thread_id='one', runtime_id='robot', prompt='/skill beta')
        assert (await graph.aget_state(config)).config == before.config


async def test_shared_invoke_turn_rejects_commands_without_a_configured_registry() -> None:
    from robot_agent_core.shell import invoke_turn

    graph = build_graph(checkpointer=InMemorySaver())
    context = AgentContext(model=NoModel(), tools=NoPhysicalTools())
    for prompt in ('/skill alpha', '/skills'):
        with pytest.raises(AgentConfigurationError, match='Skill|skill'):
            await invoke_turn(graph, context, thread_id='one', runtime_id='robot', prompt=prompt)
