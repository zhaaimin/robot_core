import asyncio
import io
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from chat_server import completion, local_chat_server


def tool_call(name: str, args: dict[str, Any], identifier: str) -> Any:
    return completion({'role': 'assistant', 'content': '', 'tool_calls': [{
        'id': identifier, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(args)},
    }]}, finish_reason='tool_calls')


def skill_directory(tmp_path: Path, *, mapped: bool) -> Path:
    from importlib.resources import files

    source = files('guide_agent').joinpath('skills')
    target = tmp_path / 'skills'
    shutil.copytree(str(source), target)
    if mapped:
        config = target / 'venue-guide/references/destinations.yaml'
        config.write_text(config.read_text().replace('map_target: null', 'map_target: verified-exhibition-hall', 1))
    return target


async def test_real_model_http_and_mcp_complete_guide_from_skill_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    pytest.importorskip('robot_runtime_mcp')
    from guide_agent.app import parse_args
    from guide_agent.application import run_application
    from guide_agent.interaction import TextInteractionSource
    from robot_agent_core.models import OpenAIModel
    from robot_agent_runtime.adapters.simulator import Simulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.sdk import RemoteRuntime
    from robot_agent_runtime.transport.server import RuntimeServer

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    simulator = Simulator()
    socket = str(tmp_path / 'runtime.sock')
    server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, socket)
    responses = [
        tool_call('load_skill', {'name': 'venue-guide'}, 'load'),
        tool_call('guide_find_destinations', {'query': '展厅'}, 'find'),
        tool_call('guide_describe_destination', {'destination_id': 'exhibition_hall'}, 'describe'),
        tool_call('guide_request_escort', {'destination_id': 'exhibition_hall'}, 'propose'),
        completion({'role': 'assistant', 'content': '演示展厅展示产品与发展历程。需要带路请说“确认带路”。'}),
    ]
    await server.start()
    try:
        async with local_chat_server(responses) as chat:
            config = parse_args([
                '--model', 'guide-test', '--backend', 'mcp', '--runtime-socket', socket,
                '--skills-dir', str(skill_directory(tmp_path, mapped=True)),
                '--data-dir', str(tmp_path / 'agent'), '--thread-id', 'visitor',
            ])
            model = OpenAIModel('guide-test', 'test-key', base_url=chat.base_url)
            try:
                from test_app import tour_source

                source = tour_source(monkeypatch)
                async with asyncio.timeout(15):
                    await run_application(config, model, source=source)
            finally:
                await model.aclose()
            assert len(chat.requests) == 5
            observations = [json.loads(message['content']) for message in chat.requests[-1].body['messages']
                            if message['role'] == 'tool']
            assert any(item['tool'] == 'guide_describe_destination' and '演示展厅' in json.dumps(item, ensure_ascii=False)
                       for item in observations)
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert rows[-1]['guide']['phase'] == 'completed'
        assert simulator.skills['navigation.navigate_to'].calls == 1
        client = RemoteRuntime(socket)
        try:
            task = await client.get_task_status(rows[-1]['guide']['navigation_task_id'])
            assert task.steps[0].params == {'target': 'verified-exhibition-hall'}
            introduction = await client.get_task_status(rows[-1]['guide']['introduction_task_id'])
            assert '演示展厅' in introduction.steps[0].params['message']
            assert introduction.state.value == 'succeeded'
        finally:
            await client.close()
        assert config.task_database is None
    finally:
        await server.close()


async def test_unmapped_mcp_place_still_answers_but_never_navigates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any,
) -> None:
    pytest.importorskip('robot_runtime_mcp')
    from guide_agent.app import parse_args
    from guide_agent.application import run_application
    from guide_agent.interaction import TextInteractionSource
    from robot_agent_runtime.adapters.simulator import Simulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.transport.server import RuntimeServer
    from test_app import GuideModel

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    simulator = Simulator()
    socket = str(tmp_path / 'runtime.sock')
    server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, socket)
    config = parse_args(['--model', 'test', '--backend', 'mcp', '--runtime-socket', socket,
                         '--data-dir', str(tmp_path / 'agent')])
    await server.start()
    try:
        from test_app import tour_source

        source = tour_source(monkeypatch)
        await run_application(config, GuideModel(), source=source)
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert simulator.skills['navigation.navigate_to'].calls == 0
        assert any(row.get('error', {}).get('code') == 'GUIDE_WORKFLOW_REJECTED' for row in rows)
        assert simulator.skills['audio.speak'].calls == 2
    finally:
        await server.close()
