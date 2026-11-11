import copy
from types import MappingProxyType
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime

from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_core.state import AgentState


VERSION_ERROR = 'Loaded skill version changed; do not resume saved actions with new instructions'


def document(name: str = 'alpha', fingerprint: str = 'v1') -> dict[str, str]:
    return {'name': name, 'description': 'Use ' + name, 'instructions': 'Saved ' + name,
            'fingerprint': fingerprint}


def loaded_state(saved: dict[str, str]) -> AgentState:
    name = saved['name']
    return {'active_skill': name, 'loaded_skills': [name],
            'skill_documents': {name: dict(saved)}, 'skill_versions': {name: saved['fingerprint']}}


def test_empty_state_does_not_require_a_registry() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    assert verified_skill_documents({}, None) == {}


def test_unloaded_documents_are_preserved_without_a_registry() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    state: AgentState = {'loaded_skills': [], 'skill_documents': {'alpha': document()}}
    before = copy.deepcopy(state)
    verified = verified_skill_documents(state, None)
    assert verified == before['skill_documents']
    assert verified is not state['skill_documents']
    verified['beta'] = document('beta')
    assert state == before


def test_matching_versions_keep_the_saved_body_without_mutating_state() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    saved = document()
    state = loaded_state(saved)
    state['skill_documents']['alpha']['saved_metadata'] = {'source': 'checkpoint'}
    before = copy.deepcopy(state)
    registry = SkillRegistry([{**saved, 'instructions': 'Current registry copy'}])
    verified = verified_skill_documents(state, registry)
    assert verified == before['skill_documents']
    assert verified['alpha']['instructions'] == 'Saved alpha'
    assert verified is not state['skill_documents']
    assert registry.load('alpha')['instructions'] == 'Current registry copy'
    assert state == before


def test_read_only_mapping_state_is_accepted() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    state = loaded_state(document())
    assert verified_skill_documents(MappingProxyType(state), SkillRegistry([document()])) == state['skill_documents']


def test_loaded_skill_requires_its_registry() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    state = loaded_state(document())
    before = copy.deepcopy(state)
    with pytest.raises(AgentConfigurationError, match='original registry'):
        verified_skill_documents(state, None)
    assert state == before


def test_loaded_skill_must_remain_visible_in_the_registry() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    state = loaded_state(document('beta'))
    registry = SkillRegistry([document(), document('beta')]).select(['alpha'])
    before = copy.deepcopy(state)
    with pytest.raises(AgentConfigurationError, match='no longer available'):
        verified_skill_documents(state, registry)
    assert state == before


def test_loaded_skill_requires_its_saved_document() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    state = loaded_state(document())
    state['skill_documents'] = {}
    with pytest.raises(AgentConfigurationError) as caught:
        verified_skill_documents(state, SkillRegistry([document()]))
    assert str(caught.value) == VERSION_ERROR


def test_saved_document_fingerprint_must_match_the_registry() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    state = loaded_state(document())
    state['skill_documents']['alpha']['fingerprint'] = 'other'
    before = copy.deepcopy(state)
    with pytest.raises(AgentConfigurationError) as caught:
        verified_skill_documents(state, SkillRegistry([document()]))
    assert str(caught.value) == VERSION_ERROR
    assert state == before


def test_loaded_skill_requires_its_saved_version() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    state = loaded_state(document())
    state['skill_versions'] = {}
    with pytest.raises(AgentConfigurationError) as caught:
        verified_skill_documents(state, SkillRegistry([document()]))
    assert str(caught.value) == VERSION_ERROR


def test_saved_version_must_match_document_and_registry_fingerprints() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    state = loaded_state(document())
    state['skill_versions']['alpha'] = 'other'
    before = copy.deepcopy(state)
    with pytest.raises(AgentConfigurationError) as caught:
        verified_skill_documents(state, SkillRegistry([document()]))
    assert str(caught.value) == VERSION_ERROR
    assert state == before


def test_registry_version_changes_are_rejected_for_inactive_loaded_skills() -> None:
    from robot_agent_core.skillkit.versions import verified_skill_documents

    state = loaded_state(document())
    state['loaded_skills'].append('beta')
    state['skill_documents']['beta'] = document('beta')
    state['skill_versions']['beta'] = 'v1'
    before = copy.deepcopy(state)
    with pytest.raises(AgentConfigurationError) as caught:
        verified_skill_documents(state, SkillRegistry([document(), document('beta', 'v2')]))
    assert str(caught.value) == VERSION_ERROR
    assert state == before


async def test_graph_and_explicit_selection_reject_versions_with_the_same_message() -> None:
    from robot_agent_core.graph import AgentContext, build_graph, decide, execute
    from robot_agent_core.skillkit.selection import handle_skill_command

    class NoModel:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            raise AssertionError('Changed instructions must be rejected before a model runs')

    class NoTools:
        schemas: list[dict[str, Any]] = []

        async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Any:
            raise AssertionError('Changed instructions must be rejected before an action runs')

    state = loaded_state(document())
    state['runtime_id'] = 'robot'
    registry = SkillRegistry([document(fingerprint='v2')])
    runtime = Runtime(context=AgentContext(model=NoModel(), tools=NoTools(), skills=registry))
    for node in (decide, execute):
        with pytest.raises(AgentConfigurationError) as caught:
            await node(state, runtime)
        assert str(caught.value) == VERSION_ERROR
    graph = build_graph(checkpointer=InMemorySaver())
    config = {'configurable': {'thread_id': 'versioned'}}
    await graph.aupdate_state(config, state, as_node='decide')
    before = await graph.aget_state(config)
    with pytest.raises(AgentConfigurationError) as caught:
        await handle_skill_command(graph, registry, thread_id='versioned', runtime_id='robot', prompt='/skill alpha')
    assert str(caught.value) == VERSION_ERROR
    after = await graph.aget_state(config)
    assert after.values == before.values
    assert after.config == before.config
