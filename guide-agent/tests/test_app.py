import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage


class GuideModel:
    """Only tests substitute the model; production always uses its configured API."""

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, messages: Any, tools: Any) -> AIMessage:
        self.calls += 1
        available = {tool['function']['name'] for tool in tools}
        if 'guide_find_destinations' not in available:
            return AIMessage(content='', tool_calls=[{'name': 'load_skill', 'args': {'name': 'venue-guide'}, 'id': f'load-{self.calls}'}])
        if isinstance(messages[-1], ToolMessage) and messages[-1].name == 'guide_request_escort':
            return AIMessage(content='可以带您前往演示展厅，请说“确认带路”。')
        return AIMessage(content='', tool_calls=[{'name': 'guide_request_escort', 'args': {'destination_id': 'exhibition_hall'}, 'id': f'proposal-{self.calls}'}])


def tour_source(monkeypatch: pytest.MonkeyPatch) -> Any:
    from guide_agent.application import GuideApplication
    from guide_agent.ports import InteractionEvent

    presented = asyncio.Event()
    original = GuideApplication._emit

    async def emit(self: Any, event: Any, **kwargs: Any) -> None:
        await original(self, event, **kwargs)
        if event.kind == 'utterance' and event.text == '带我去展厅':
            presented.set()

    monkeypatch.setattr(GuideApplication, '_emit', emit)

    class Source:
        async def events(self) -> Any:
            yield InteractionEvent('wake', 'visitor', 'wakeup')
            yield InteractionEvent('request', 'visitor', 'utterance', '带我去展厅')
            await presented.wait()
            yield InteractionEvent('confirm', 'visitor', 'confirm')
            yield InteractionEvent('wait', 'visitor', 'wait')
            yield InteractionEvent('status', 'visitor', 'status')

    return Source()


def configuration(tmp_path: Path, *extra: str) -> Any:
    from guide_agent.app import parse_args

    return parse_args(['--model', 'test-model', '--data-dir', str(tmp_path), '--thread-id', 'visitor', *extra])


def test_configuration_is_independent_and_mcp_requires_an_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from guide_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    config = configuration(tmp_path)
    assert config.profile.name == 'guide'
    assert config.task_database.name == 'guide-simulator-tasks.sqlite3'
    assert config.business_database.name == 'guide.sqlite3'
    assert config.checkpoint_database.name == 'guide-checkpoints.sqlite3'
    with pytest.raises(AgentConfigurationError):
        parse_args(['--model', 'test-model', '--backend', 'mcp'])
    for backend in ('cc', 'unknown'):
        with pytest.raises(AgentConfigurationError):
            configuration(tmp_path, '--backend', backend)


async def test_wakeup_confirm_navigation_and_automatic_introduction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from guide_agent.application import run_application
    from guide_agent.interaction import TextInteractionSource
    from robot_agent_runtime.adapters.simulator import Simulator

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    simulator = Simulator()
    monkeypatch.setattr('guide_agent.runtime.Simulator', lambda: simulator)
    source = tour_source(monkeypatch)
    async with asyncio.timeout(10):
        await run_application(configuration(tmp_path), GuideModel(), source=source)
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]['guide']['phase'] == 'completed'
    assert simulator.skills['navigation.navigate_to'].calls == 1
    assert simulator.skills['audio.speak'].calls >= 2
    assert (tmp_path / 'guide.sqlite3').exists()
    assert (tmp_path / 'guide-checkpoints.sqlite3').exists()


async def test_stop_during_model_request_blocks_late_proposal_and_speech(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from guide_agent.application import run_application
    from guide_agent.ports import InteractionEvent
    from robot_agent_runtime.adapters.simulator import Simulator

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    entered = asyncio.Event()
    stopped = asyncio.Event()
    simulator = Simulator()
    monkeypatch.setattr('guide_agent.runtime.Simulator', lambda: simulator)

    class SlowModel:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            if 'guide_find_destinations' not in {tool['function']['name'] for tool in tools}:
                return AIMessage(content='', tool_calls=[{'name': 'load_skill', 'args': {'name': 'venue-guide'}, 'id': 'load'}])
            entered.set()
            await stopped.wait()
            return AIMessage(content='', tool_calls=[{'name': 'guide_request_escort', 'args': {'destination_id': 'exhibition_hall'}, 'id': 'late'}])

    class Source:
        async def events(self) -> Any:
            yield InteractionEvent('ask', 'visitor', 'utterance', '带我去展厅')
            await entered.wait()
            yield InteractionEvent('stop', 'visitor', 'cancel')
            stopped.set()
            yield InteractionEvent('status', 'visitor', 'status')

    async with asyncio.timeout(5):
        await run_application(configuration(tmp_path), SlowModel(), source=Source())
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert simulator.skills['navigation.navigate_to'].calls == 0
    assert simulator.skills['audio.speak'].calls == 0
    assert rows[-1]['guide']['phase'] != 'awaiting_confirmation'


async def test_partial_and_repeated_events_do_not_repeat_model_or_greeting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from guide_agent.application import run_application
    from guide_agent.interaction import JsonInteractionSource
    from robot_agent_runtime.adapters.simulator import Simulator

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    simulator = Simulator()
    monkeypatch.setattr('guide_agent.runtime.Simulator', lambda: simulator)
    source = JsonInteractionSource(io.StringIO(
        '{"event_id":"w","session_id":"visitor","kind":"wakeup"}\n'
        '{"event_id":"w","session_id":"visitor","kind":"wakeup"}\n'
        '{"event_id":"q","session_id":"visitor","kind":"utterance","text":"你好","final":false}\n'
        '{"event_id":"q","session_id":"visitor","kind":"utterance","text":"你好","final":true}\n'
        '{"event_id":"q","session_id":"visitor","kind":"utterance","text":"你好","final":true}\n',
    ))

    class ReplyModel:
        calls = 0

        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            self.calls += 1
            return AIMessage(content='您好，请问想了解哪个地点？')

    model = ReplyModel()
    await run_application(configuration(tmp_path), model, source=source)
    assert model.calls == 1
    assert simulator.skills['audio.speak'].calls == 2
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sum(row.get('duplicate', False) for row in rows) == 2


async def test_stop_discards_earlier_queued_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from guide_agent.application import run_application
    from guide_agent.interaction import TextInteractionSource
    from robot_agent_runtime.adapters.simulator import Simulator

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    simulator = Simulator()
    monkeypatch.setattr('guide_agent.runtime.Simulator', lambda: simulator)
    model = GuideModel()
    source = TextInteractionSource(io.StringIO('带我去展厅\n确认带路\n/stop\n/status\n'), session_id='visitor')
    await run_application(configuration(tmp_path), model, source=source)
    assert simulator.skills['navigation.navigate_to'].calls == 0
    assert model.calls == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]['guide']['phase'] == 'canceled'


async def test_confirmation_cannot_authorize_a_replacement_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from guide_agent.application import GuideApplication, run_application
    from guide_agent.ports import InteractionEvent
    from robot_agent_runtime.adapters.simulator import Simulator

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    simulator = Simulator()
    monkeypatch.setattr('guide_agent.runtime.Simulator', lambda: simulator)
    presented = asyncio.Event()
    original = GuideApplication._emit

    async def emit(self: Any, event: Any, **kwargs: Any) -> None:
        await original(self, event, **kwargs)
        if event.event_id == 'first':
            presented.set()

    monkeypatch.setattr(GuideApplication, '_emit', emit)

    class Source:
        async def events(self) -> Any:
            yield InteractionEvent('first', 'visitor', 'utterance', '带我去展厅')
            await presented.wait()
            yield InteractionEvent('second', 'visitor', 'utterance', '重新提出带路建议')
            yield InteractionEvent('stale-confirm', 'visitor', 'confirm')
            yield InteractionEvent('status', 'visitor', 'status')

    await run_application(configuration(tmp_path), GuideModel(), source=Source())
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert simulator.skills['navigation.navigate_to'].calls == 0
    assert rows[-1]['guide']['phase'] == 'awaiting_confirmation'
    assert any(row['event_id'] == 'stale-confirm' and row.get('error') for row in rows)


async def test_resume_does_not_upgrade_a_cancelled_pending_tools_epoch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from dataclasses import replace
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from guide_agent.application import run_application
    from guide_agent.catalog import DestinationCatalog
    from guide_agent.runtime import open_runtime
    from guide_agent.service import GuideWorkflow
    from guide_agent.tools import GuideTools
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent_core.skillkit.catalog import SkillRegistry
    from robot_agent_runtime.adapters.simulator import Simulator

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    simulator = Simulator()
    monkeypatch.setattr('guide_agent.runtime.Simulator', lambda: simulator)
    config = configuration(tmp_path)
    registry = SkillRegistry.bundled('guide_agent')
    catalog = DestinationCatalog.from_registry(registry)
    document = registry.load('venue-guide')
    async with open_runtime(config) as runtime:
        workflow = GuideWorkflow(str(config.business_database), runtime, catalog, backend='simulator')
        await workflow.start()
        try:
            epoch = await workflow.bind_turn('visitor', operation_id='original-turn')
            tools = GuideTools(workflow, catalog, 'visitor', epoch=epoch)
            context = AgentContext(model=GuideModel(), tools=tools, skills=registry, guidance=tools)
            async with AsyncSqliteSaver.from_conn_string(str(config.checkpoint_database)) as saver:
                graph = build_graph(checkpointer=saver, interrupt_before=['execute'])
                state = await graph.ainvoke({
                    'runtime_id': config.runtime_id, 'messages': [HumanMessage(content='带我去展厅')],
                    'loaded_skills': ['venue-guide'], 'active_skill': 'venue-guide',
                    'skill_documents': {'venue-guide': document},
                    'skill_versions': {'venue-guide': document['fingerprint']},
                }, {'configurable': {'thread_id': 'visitor'}}, context=context, durability='sync')
                assert state['pending'][0]['name'] == 'guide_request_escort'
            await workflow.cancel('visitor', operation_id='stop-after-decision')
        finally:
            await workflow.close()
    await run_application(replace(config, resume=True), GuideModel())
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]['guide']['phase'] == 'canceled'
    assert rows[-1]['guide']['destination_id'] is None
    assert simulator.skills['navigation.navigate_to'].calls == 0
    assert simulator.skills['audio.speak'].calls == 0


async def test_skill_catalog_commands_have_no_model_or_robot_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from guide_agent.application import run_application
    from guide_agent.interaction import TextInteractionSource
    from robot_agent_runtime.adapters.simulator import Simulator

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    simulator = Simulator()
    monkeypatch.setattr('guide_agent.runtime.Simulator', lambda: simulator)

    class UnusedModel:
        async def decide(self, messages: Any, tools: Any) -> Any:
            pytest.fail('Skill selection called a model')

    source = TextInteractionSource(io.StringIO('/skills\n/skill venue-guide\n'), session_id='visitor')
    await run_application(configuration(tmp_path), UnusedModel(), source=source)
    assert all(skill.calls == 0 for skill in simulator.skills.values())
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 2
    assert rows[-1]['guide']['phase'] == 'idle'


async def test_question_and_stop_are_processed_during_navigation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from langchain_core.messages import HumanMessage

    from guide_agent.application import GuideApplication, run_application
    from guide_agent.ports import InteractionEvent
    from robot_agent_runtime.adapters.simulator import Simulator

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    simulator = Simulator()
    simulator.skills['navigation.navigate_to'].delay_s = 30
    monkeypatch.setattr('guide_agent.runtime.Simulator', lambda: simulator)
    presented, moving, answered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = GuideApplication._emit

    async def emit(self: Any, event: Any, **kwargs: Any) -> None:
        await original(self, event, **kwargs)
        if event.event_id == 'proposal':
            presented.set()
        elif event.event_id == 'confirm':
            state = await self.workflow.status('visitor')
            async with asyncio.timeout(2):
                while (await self.workflow._runtime.get_task_status(state['navigation_task_id'])).state.value != 'running':
                    pass
            moving.set()
        elif event.event_id == 'question':
            answered.set()

    monkeypatch.setattr(GuideApplication, '_emit', emit)

    class Model(GuideModel):
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            if isinstance(messages[-1], HumanMessage) and messages[-1].content == '展厅有什么？':
                return AIMessage(content='演示展厅展示产品与发展历程，实际展陈资料尚未配置。')
            return await super().decide(messages, tools)

    class Source:
        async def events(self) -> Any:
            yield InteractionEvent('proposal', 'visitor', 'utterance', '带我去展厅')
            await presented.wait()
            yield InteractionEvent('confirm', 'visitor', 'confirm')
            await moving.wait()
            yield InteractionEvent('question', 'visitor', 'utterance', '展厅有什么？')
            await answered.wait()
            yield InteractionEvent('stop', 'visitor', 'cancel')
            yield InteractionEvent('status', 'visitor', 'status')

    async with asyncio.timeout(10):
        await run_application(configuration(tmp_path), Model(), source=Source())
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    question = next(row for row in rows if row['event_id'] == 'question')
    assert question['guide']['phase'] == 'navigating'
    assert question['speech']['state'] == 'succeeded'
    assert rows[-1]['guide']['phase'] == 'canceled'
    assert not rows[-1]['guide']['introduction_intent']
    assert simulator.skills['navigation.navigate_to'].calls == 1
    assert simulator.skills['audio.speak'].calls == 2
