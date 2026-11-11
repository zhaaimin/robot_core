from collections.abc import Sequence
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


class SequenceModel:
    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def decide(
        self, messages: Sequence[BaseMessage], tools: list[dict[str, Any]],
    ) -> AIMessage:
        self.calls += 1
        if self.calls > 1:
            assert isinstance(messages[-1], ToolMessage)
        return self.responses.pop(0)


def speech_call(*, call_id: str = 'speak-1') -> AIMessage:
    return AIMessage(content='', tool_calls=[{
        'name': 'robot_speak', 'args': {'message': 'hello'}, 'id': call_id,
    }])


async def test_graph_executes_then_feeds_actual_tool_result_back_to_model() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent.robot_tools import RobotTools

    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        model = SequenceModel([speech_call(), AIMessage(content='已完成播报')])
        context = AgentContext(model=model, tools=await RobotTools.create(runtime))
        graph = build_graph(checkpointer=InMemorySaver())
        final = await graph.ainvoke(
            {'messages': [HumanMessage(content='播报 hello')], 'model_turns': 0},
            {'configurable': {'thread_id': 'success'}}, context=context,
        )
        assert final['messages'][-1].content == '已完成播报'
        assert final['observations'][0]['state'] == 'succeeded'
        assert model.calls == 2
        assert simulator.skills['audio.speak'].calls == 1
    finally:
        await runtime.close()


async def test_model_cannot_issue_an_unbounded_number_of_actions_in_one_turn() -> None:
    from robot_agent_core.errors import AgentModelError
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent.robot_tools import RobotTools

    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        message = AIMessage(content='', tool_calls=[{
            'name': 'robot_speak', 'args': {'message': str(index)}, 'id': str(index),
        } for index in range(9)])
        model = SequenceModel([message, AIMessage(content='done')])
        with pytest.raises(AgentModelError):
            await build_graph().ainvoke(
                {'messages': [HumanMessage(content='hello')], 'model_turns': 0},
                context=AgentContext(model=model, tools=await RobotTools.create(runtime)),
            )
        assert simulator.skills['audio.speak'].calls == 0
    finally:
        await runtime.close()


async def test_model_turn_limit_prevents_additional_actions() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent.robot_tools import RobotTools

    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        model = SequenceModel([speech_call()])
        final = await build_graph().ainvoke(
            {'messages': [HumanMessage(content='hello')], 'model_turns': 0},
            context=AgentContext(model=model, tools=await RobotTools.create(runtime), max_turns=1),
        )
        assert final['halted'] is True
        assert 'AGENT_STEP_LIMIT' in final['messages'][-1].content
        assert model.calls == 1
        assert simulator.skills['audio.speak'].calls == 1
    finally:
        await runtime.close()


async def test_sqlite_checkpoint_survives_graph_and_saver_reconstruction() -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent.robot_tools import RobotTools

    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        model = SequenceModel([speech_call(), AIMessage(content='done')])
        context = AgentContext(model=model, tools=await RobotTools.create(runtime))
        config = {'configurable': {'thread_id': 'durable'}}
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'checkpoints.sqlite3')
            async with AsyncSqliteSaver.from_conn_string(path) as saver:
                paused = await build_graph(checkpointer=saver, interrupt_before=['execute']).ainvoke(
                    {'messages': [HumanMessage(content='hello')], 'model_turns': 0},
                    config, context=context, durability='sync',
                )
            action = paused['pending'][0]
            first = await context.tools.invoke(action['name'], action['args'], operation_id=action['operation_id'])
            async with AsyncSqliteSaver.from_conn_string(path) as saver:
                final = await build_graph(checkpointer=saver).ainvoke(
                    None, config, context=context, durability='sync',
                )
            assert final['observations'][0]['task_id'] == first['task_id']
            assert simulator.skills['audio.speak'].calls == 1
    finally:
        await runtime.close()


async def test_graph_stops_on_failure_and_skips_later_physical_actions() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent.robot_tools import RobotTools

    simulator = Simulator(fail_skills={'audio.speak'})
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        message = speech_call()
        message.tool_calls.append({'name': 'robot_navigate_to', 'args': {'target': 'room'}, 'id': 'nav'})
        model = SequenceModel([message])
        final = await build_graph().ainvoke(
            {'messages': [HumanMessage(content='test')], 'model_turns': 0},
            context=AgentContext(model=model, tools=await RobotTools.create(runtime)),
        )
        assert final['halted'] is True
        assert final['observations'][0]['state'] == 'failed'
        assert final['observations'][1]['error']['code'] == 'AGENT_ACTION_SKIPPED'
        assert simulator.skills['navigation.navigate_to'].calls == 0
        assert model.calls == 1
    finally:
        await runtime.close()


async def test_wait_deadline_still_stops_following_actions_if_query_sees_late_success() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent.robot_tools import RobotTools
    from robot_agent_runtime.errors import TaskWaitTimeout
    from robot_agent_runtime.models import Task, TaskRequest

    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    original = runtime.submit_and_wait

    async def deadline_race(request: TaskRequest, *, timeout_s: float | None = None) -> Task:
        final = await original(request, timeout_s=timeout_s)
        raise TaskWaitTimeout('Deadline raced with completion', details={'task_id': final.id})

    try:
        message = speech_call()
        message.tool_calls.append({'name': 'robot_speak', 'args': {'message': 'second'}, 'id': 'second'})
        model = SequenceModel([message, AIMessage(content='done')])
        with patch.object(runtime, 'submit_and_wait', deadline_race):
            final = await build_graph().ainvoke(
                {'messages': [HumanMessage(content='two actions')], 'model_turns': 0},
                context=AgentContext(model=model, tools=await RobotTools.create(runtime)),
            )
        assert final['halted'] is True
        assert final['observations'][0]['state'] == 'succeeded'
        assert final['observations'][0]['error']['code'] == 'TASK_WAIT_TIMEOUT'
        assert final['observations'][1]['error']['code'] == 'AGENT_ACTION_SKIPPED'
        assert simulator.skills['audio.speak'].calls == 1
    finally:
        await runtime.close()


async def test_checkpoint_resume_preserves_the_operation_key() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent.robot_tools import RobotTools

    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        model = SequenceModel([speech_call(), AIMessage(content='done')])
        context = AgentContext(model=model, tools=await RobotTools.create(runtime))
        saver = InMemorySaver()
        config = {'configurable': {'thread_id': 'resume'}}
        graph = build_graph(checkpointer=saver, interrupt_before=['execute'])
        paused = await graph.ainvoke(
            {'messages': [HumanMessage(content='hello')], 'model_turns': 0}, config, context=context,
        )
        action = paused['pending'][0]
        first = await context.tools.invoke(action['name'], action['args'], operation_id=action['operation_id'])
        resumed = await build_graph(checkpointer=saver).ainvoke(None, config, context=context)
        assert resumed['observations'][0]['task_id'] == first['task_id']
        assert simulator.skills['audio.speak'].calls == 1
    finally:
        await runtime.close()
