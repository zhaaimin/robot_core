import hashlib
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from hotel_agent.script_bindings import build_hotel_script_runner
from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.graph import AgentContext, build_graph
from robot_agent_core.ports import Observation
from robot_agent_core.skillkit.catalog import SkillRegistry


_ACTIONS = (
    ('frontdesk-reception', 'scripts/query_reception.py',
     {'receipt_code': 'DEMO-001'}, 'hotel_confirm_reception'),
    ('guest-handoff', 'scripts/assign_butler.py', {}, 'hotel_assign_butler'),
    ('escort-assistance', 'scripts/start_escort.py', {}, 'hotel_start_escort'),
)


class RecordingTools:
    schemas: list[dict[str, Any]] = []

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def invoke(
        self, name: str, args: dict[str, Any], *, operation_id: str,
    ) -> Observation:
        self.calls.append((name, args, operation_id))
        return {'tool': name, 'task_id': None, 'state': 'succeeded',
                'result': {'phase': 'waiting_for_guest'}, 'error': None,
                'requires_attention': False}


class ScriptModel:
    def __init__(self, name: str, path: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.path = path
        self.arguments = arguments
        self.calls = 0

    async def decide(
        self, messages: Sequence[BaseMessage], tools: list[dict[str, Any]],
    ) -> AIMessage:
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content='', tool_calls=[{
                'name': 'run_skill_script', 'id': 'hotel-script',
                'args': {'name': self.name, 'path': self.path, 'args': self.arguments},
            }])
        return AIMessage(content='已按实际结果完成本轮处理')


async def pause_script(
    name: str, path: str, arguments: dict[str, Any],
    registry: SkillRegistry, tools: RecordingTools,
) -> tuple[InMemorySaver, AgentContext, dict[str, dict[str, str]]]:
    saved = registry.load(name)
    context = AgentContext(
        model=ScriptModel(name, path, arguments), tools=tools, skills=registry,
        scripts=build_hotel_script_runner(registry, tools),
    )
    saver = InMemorySaver()
    config = {'configurable': {'thread_id': name}}
    paused = await build_graph(checkpointer=saver, interrupt_before=['execute']).ainvoke(
        {'messages': [HumanMessage(content='请处理酒店服务')], 'active_skill': name,
         'loaded_skills': [name], 'skill_documents': {name: saved},
         'skill_versions': {name: saved['fingerprint']}},
        config, context=context, durability='sync',
    )
    assert paused['pending'][0]['script_fingerprint'] == registry.read_resource(name, path)['fingerprint']
    assert tools.calls == []
    return saver, context, config


async def test_old_error_import_script_checkpoint_is_rejected_before_tool_dispatch() -> None:
    registry = SkillRegistry.bundled('hotel_agent')
    for name, path, arguments, _ in _ACTIONS:
        resource = registry.read_resource(name, path)
        legacy_source = resource['content'].replace(
            'from hotel_agent.errors import HotelWorkflowError',
            'from robot_agent_core.errors import HotelWorkflowError',
        )
        legacy_fingerprint = hashlib.sha256(legacy_source.encode()).hexdigest()
        assert legacy_fingerprint != resource['fingerprint']
        tools = RecordingTools()
        saver, context, config = await pause_script(name, path, arguments, registry, tools)
        graph = build_graph(checkpointer=saver)
        paused = await graph.aget_state(config)
        legacy_action = {**paused.values['pending'][0], 'script_fingerprint': legacy_fingerprint}
        await graph.aupdate_state(config, {'pending': [legacy_action]}, as_node='decide')
        before = await graph.aget_state(config)
        assert before.next == ('execute',)
        assert before.values['skill_documents'][name] == registry.load(name)
        assert before.values['skill_versions'][name] == registry.load(name)['fingerprint']

        with pytest.raises(AgentConfigurationError, match='Pending script version changed'):
            await build_graph(checkpointer=saver).ainvoke(
                None, config, context=context, durability='sync',
            )

        after = await graph.aget_state(config)
        assert after.values['pending'] == before.values['pending']
        assert after.values['pending'][0]['script_fingerprint'] == legacy_fingerprint
        assert tools.calls == []


async def test_current_published_script_checkpoint_resumes_with_its_frozen_operation() -> None:
    registry = SkillRegistry.bundled('hotel_agent')
    for name, path, arguments, delegated_tool in _ACTIONS:
        tools = RecordingTools()
        saver, context, config = await pause_script(name, path, arguments, registry, tools)
        graph = build_graph(checkpointer=saver)
        paused = await graph.aget_state(config)
        action = paused.values['pending'][0]
        final = await graph.ainvoke(None, config, context=context, durability='sync')

        assert final['halted'] is False
        assert final['observations'][0]['error'] is None
        assert final['observations'][0]['execution'] == {
            'skill': name, 'path': path, 'fingerprint': action['script_fingerprint'],
        }
        assert tools.calls == [(delegated_tool, arguments, action['operation_id'])]
