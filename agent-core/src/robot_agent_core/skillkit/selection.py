import json
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph

from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_core.skillkit.host import SkillHost


async def handle_skill_command(
    graph: CompiledStateGraph, registry: SkillRegistry, *, thread_id: str,
    runtime_id: str, prompt: str,
) -> dict[str, Any] | None:
    """Apply explicit instruction choices without turning them into business facts.

    A load uses the graph's existing document/version state and tool transcript,
    but ends at the decision boundary without invoking a model. Pending work is
    never replaced. Catalog inspection does not write a checkpoint or read a
    body, so it remains available while durable work awaits resumption.
    """
    parts = prompt.strip().split()
    if not parts or parts[0] not in {'/skill', '/skills'}:
        return None
    command = parts[0]
    if command == '/skills' and len(parts) != 1:
        raise AgentConfigurationError('Use /skills without arguments')
    if command == '/skill' and len(parts) != 2:
        raise AgentConfigurationError('Use /skill NAME with one catalog name')
    config = {'configurable': {'thread_id': thread_id}}
    snapshot = await graph.aget_state(config)
    state = snapshot.values
    if state and state.get('runtime_id') != runtime_id:
        raise AgentConfigurationError('Checkpoint belongs to a different robot runtime or backend')
    if command == '/skills':
        catalog = registry.catalog()
        metadata = {'skill_catalog': catalog, 'active_skill': state.get('active_skill'),
                    'loaded_skills': list(state.get('loaded_skills', []))}
        return {**state, **metadata, 'observations': [],
                'messages': [AIMessage(content='可用技能与当前选择：' + json.dumps(metadata, ensure_ascii=False))]}
    if snapshot.next or state.get('pending'):
        raise AgentConfigurationError('Thread has pending work; use --resume before selecting a skill')
    registry.select([parts[1]])
    batch = SkillHost(registry).begin_batch(state)
    name = parts[1]
    call_id = uuid4().hex
    observed = await batch.invoke('load_skill', {'name': name}, operation_id=call_id)
    await graph.aupdate_state(config, {
        'messages': [
            HumanMessage(content=prompt),
            AIMessage(content='', tool_calls=[{'name': 'load_skill', 'args': {'name': name}, 'id': call_id}]),
            ToolMessage(content=json.dumps(observed, ensure_ascii=False), tool_call_id=call_id, name='load_skill'),
            AIMessage(content=f'已加载并选择技能：{name}。'),
        ],
        **batch.update(), 'observations': [observed], 'pending': [],
        'model_turns': 0, 'halted': False, 'runtime_id': runtime_id,
    }, as_node='decide')
    updated = await graph.aget_state(config)
    return dict(updated.values)
