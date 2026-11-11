"""Compose the shared agent with hotel tools and trusted guest input."""

import json
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph

from robot_agent_core.config import AppConfig
from robot_agent_core.configuration import validate_config
from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.graph import AgentContext, build_graph
from robot_agent_core.models import DecisionModel
from robot_agent_core.ports import TaskRuntime

from hotel_agent.profiles import HOTEL_PROFILE
from robot_agent_core.shell import error_state, invoke_turn, is_skill_command, result_envelope, run_shell
from robot_agent_runtime.errors import AgentRuntimeError
from robot_agent_runtime.repositories.sqlite import SQLiteTaskRepository
from robot_agent_runtime.sdk import RobotRuntime

if TYPE_CHECKING:
    from hotel_agent.service import HotelWorkflow


async def invoke_hotel_turn(
    graph: CompiledStateGraph, context: AgentContext, workflow: 'HotelWorkflow', *,
    session_id: str, runtime_id: str, prompt: str | None,
) -> dict[str, Any]:
    """Record guest confirmations only at the trusted input boundary.

    A pending graph must resume its durable decision before a new confirmation
    can alter the business facts underneath it. Cancellation remains available
    even then, because the workflow will reject a resumed physical action after
    cancellation. Status and explicit waiting never invoke the model.
    """
    if prompt is None:
        return await invoke_turn(
            graph, context, thread_id=session_id, runtime_id=runtime_id, prompt=None,
        )
    command = prompt.strip()
    if not command:
        raise AgentConfigurationError('Prompt must not be empty')
    checkpoint = await graph.aget_state({'configurable': {'thread_id': session_id}})
    if checkpoint.values and checkpoint.values.get('runtime_id') != runtime_id:
        raise AgentConfigurationError('Checkpoint belongs to a different hotel runtime')
    if command == '/skills':
        return await invoke_turn(
            graph, context, thread_id=session_id, runtime_id=runtime_id, prompt=prompt,
        )
    if command in {'/cancel', '/status', '/wait'}:
        if command == '/cancel':
            await workflow.cancel(session_id, operation_id=uuid4().hex)
        elif command == '/wait':
            await workflow.wait_for_settled(session_id, timeout_s=2.0)
        snapshot = await workflow.refresh(session_id)
        return {
            'messages': [AIMessage(content='应用控制事件返回事实：' + json.dumps(snapshot, ensure_ascii=False))],
            'observations': [],
            'active_skill': checkpoint.values.get('active_skill') if checkpoint.values else None,
            'loaded_skills': checkpoint.values.get('loaded_skills', []) if checkpoint.values else [],
            'loaded_resources': checkpoint.values.get('loaded_resources', []) if checkpoint.values else [],
            'skill_versions': checkpoint.values.get('skill_versions', {}) if checkpoint.values else {},
        }
    if checkpoint.next:
        raise AgentConfigurationError('Thread has pending work; use --resume before adding a prompt')
    events = {'/consent': 'consent', '/start': 'ready', '/arrived': 'arrived'}
    if command in events:
        await workflow.record_guest_event(session_id, events[command])
    if command.startswith('/guest'):
        raise AgentConfigurationError('/guest is an interactive application command')
    return await invoke_turn(
        graph, context, thread_id=session_id, runtime_id=runtime_id, prompt=prompt,
    )


async def _result(
    config: AppConfig, workflow: 'HotelWorkflow', session_id: str,
    state: dict[str, Any], catalog: list[dict[str, str]], *, include_hotel: bool = True,
) -> dict[str, Any]:
    return {
        **result_envelope(config, state, thread_id=session_id, catalog=catalog),
        'agent': 'hotel', 'session_id': session_id,
        'hotel': await workflow.refresh(session_id) if include_hotel else None,
        'active_skill': state.get('active_skill') or None,
    }


@asynccontextmanager
async def open_hotel_runtime(config: AppConfig) -> AsyncIterator[TaskRuntime]:
    """Release the selected connection after the caller closes hotel workflows."""
    if config.backend == 'mcp':
        from hotel_agent.adapters.mcp import open_runtime

        if config.runtime_socket is None:
            raise AgentConfigurationError('Hotel MCP backend requires a runtime socket')
        async with open_runtime(config.runtime_socket, allowed_skills=set(config.allowed_skills)) as runtime:
            yield runtime
        return

    from hotel_agent.simulator import HotelSimulator

    if config.backend != 'simulator' or config.task_database is None:
        raise AgentConfigurationError('Hotel runtime requires simulator storage or an MCP endpoint')
    simulator = HotelSimulator(delay_s=0.05)
    runtime = RobotRuntime(
        SQLiteTaskRepository(str(config.task_database)),
        {name: simulator.skills[name] for name in ('navigation.navigate_to', 'audio.speak')},
        allowed_permissions={'navigation', 'audio'},
    )
    try:
        await runtime.start()
        yield runtime
    finally:
        await runtime.close()


async def run_hotel_application(config: AppConfig, model: DecisionModel) -> None:
    """Own one hotel workflow while guest graph histories remain independent.

    The hotel workflow closes while the runtime is still queryable so confirmed
    shutdown outcomes are persisted before hotel storage closes. PMS integration
    and human handoff remain simulated hotel capabilities. Physical tasks use either
    the embedded simulator or an MCP connection to an application-owned runtime.
    Announcements never take the movement lease or enter a business phase.
    """
    validate_config(config)
    if config.profile.name != HOTEL_PROFILE.name:
        raise AgentConfigurationError(
            'Hotel application requires the hotel storage profile',
            details={'profile': config.profile.name},
        )
    if config.business_database is None:
        raise AgentConfigurationError('hotel agent requires a business database')
    from hotel_agent.script_bindings import build_hotel_script_runner
    from hotel_agent.service import HotelWorkflow
    from hotel_agent.tools import HotelTools
    from robot_agent_core.skillkit.catalog import SkillRegistry

    missing_capabilities = {'navigation.navigate_to', 'audio.speak'} - set(config.allowed_skills)
    if missing_capabilities:
        raise AgentConfigurationError(
            'hotel agent requires the skills its workflow advertises',
            details={'skills': sorted(missing_capabilities)},
        )
    registry = (SkillRegistry.from_directory(config.skills_dir) if config.skills_dir is not None
                else SkillRegistry.bundled('hotel_agent'))
    if config.enabled_skills is not None:
        registry = registry.select(config.enabled_skills)
    catalog = registry.catalog()
    if not catalog:
        raise AgentConfigurationError('hotel agent requires a nonempty Agent Skill catalog')
    config.data_dir.mkdir(parents=True, exist_ok=True)
    print(
        f'酒店验证场景：模拟入住系统、同楼层单机器人导航（{config.backend}）；人工交接仅记录请求。'
        ' 输入 /skills 查看能力，用 /skill NAME 选择；'
        '选择 frontdesk-reception 后输入 入住 DEMO-001，选择 guest-handoff 后 /consent，'
        '选择 escort-assistance 后 /start → /wait → /arrived → /wait；'
        '/status 查询，/cancel 停止，/guest 会话ID 切换客人；'
        '需要机器人播报已核验的信息时直接说明。',
        file=sys.stderr,
    )
    async with open_hotel_runtime(config) as runtime:
        workflow = HotelWorkflow(
            str(config.business_database), runtime,
            task_timeout_s=config.task_timeout_s, wait_timeout_s=config.wait_timeout_s,
        )
        try:
            await workflow.start()
            async with AsyncSqliteSaver.from_conn_string(str(config.checkpoint_database)) as saver:
                graph = build_graph(checkpointer=saver)
                contexts: dict[str, AgentContext] = {}

                def context_for(session_id: str) -> AgentContext:
                    if session_id not in contexts:
                        tools = HotelTools(workflow, session_id)
                        contexts[session_id] = AgentContext(
                            model=model, tools=tools, max_turns=config.max_turns,
                            skills=registry, guidance=tools, scripts=build_hotel_script_runner(registry, tools),
                        )
                    return contexts[session_id]

                session_id = config.thread_id
                last_prompt: str | None = None

                async def turn(prompt: str | None) -> dict[str, Any]:
                    nonlocal session_id, last_prompt
                    last_prompt = prompt
                    command = prompt.strip() if prompt is not None else ''
                    if command.startswith('/guest') and config.prompt is None and not config.resume:
                        guest = re.fullmatch(r'/guest\s+([^\s]{1,128})', command)
                        if guest is None:
                            raise AgentConfigurationError('Use /guest SESSION_ID with a nonempty ID of at most 128 characters')
                        session_id = guest.group(1)
                        context_for(session_id)
                        return {'messages': [AIMessage(content='已切换客人会话：' + session_id)], 'observations': []}
                    return await invoke_hotel_turn(
                        graph, context_for(session_id), workflow, session_id=session_id,
                        runtime_id=config.runtime_id, prompt=prompt,
                    )

                async def render(state: dict[str, Any]) -> dict[str, Any]:
                    return await _result(
                        config, workflow, session_id, state, catalog,
                        include_hotel=not is_skill_command(last_prompt),
                    )

                async def recover(prompt: str, error: AgentRuntimeError) -> dict[str, Any]:
                    state: dict[str, Any] = {}
                    if is_skill_command(prompt):
                        checkpoint = await graph.aget_state({'configurable': {'thread_id': session_id}})
                        state.update(checkpoint.values)
                    return error_state(error, state)

                await run_shell(
                    config, turn=turn, render=render, prompt=lambda: f'hotel-agent[{session_id}]> ',
                    recover=recover,
                )
        finally:
            await workflow.close()
