import asyncio
import io
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


def test_empty_optional_endpoint_values_keep_provider_and_local_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent.app import parse_args

    monkeypatch.setenv('OPENAI_BASE_URL', '')
    monkeypatch.setenv('CC_API_ADDRESS', '')
    config = parse_args(['--prompt', 'hello'])
    assert config.base_url is None
    assert config.address is None


def test_cli_requires_a_model_and_rejects_unbounded_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    from robot_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError

    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('ROBOT_AGENT_MODEL', raising=False)
    with pytest.raises(AgentConfigurationError):
        parse_args(['--prompt', '播报 hello'])
    monkeypatch.setenv('ROBOT_AGENT_MODEL', 'chosen-by-user')
    with pytest.raises(AgentConfigurationError):
        parse_args(['--prompt', '播报 hello'])
    monkeypatch.setenv('OPENAI_API_KEY', 'secret-key')
    config = parse_args(['--prompt', '播报 hello'])
    assert config.backend == 'simulator'
    assert config.model == 'chosen-by-user'
    assert config.max_turns == 8
    assert config.data_dir.is_absolute()
    for value in ('0', '-1', '101'):
        with pytest.raises(AgentConfigurationError):
            parse_args(['--max-turns', value])
    for option in ('--wait-timeout', '--task-timeout', '--model-timeout'):
        for value in ('0', '-1', 'nan', 'inf'):
            with pytest.raises(AgentConfigurationError):
                parse_args([option, value])


def test_resume_and_model_arguments_require_explicit_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from robot_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError

    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('ROBOT_AGENT_MODEL', raising=False)
    for argv in (
        ['--resume'], ['--resume', '--thread-id', 'one', '--prompt', 'hello'],
        ['--model', 'explicit'], ['--backend', 'cc', '--prompt', 'hello'],
        ['--thread-id', ''], ['--prompt', '   '],
    ):
        with pytest.raises(AgentConfigurationError):
            parse_args(argv)
    monkeypatch.setenv('OPENAI_API_KEY', 'secret-key')
    config = parse_args(['--model', 'explicit'])
    assert config.model == 'explicit'
    assert config.api_key == 'secret-key'
    assert 'secret-key' not in repr(config)


def test_data_directory_uses_user_state_and_explicit_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent.app import parse_args

    home = tmp_path / 'user'
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.delenv('ROBOT_AGENT_DATA_DIR', raising=False)
    monkeypatch.delenv('XDG_STATE_HOME', raising=False)
    fallback = (home / '.local' / 'state' / 'robot-agent').resolve()
    assert parse_args([]).data_dir == fallback
    for invalid_xdg in ('', 'relative/state'):
        monkeypatch.setenv('XDG_STATE_HOME', invalid_xdg)
        assert parse_args([]).data_dir == fallback
    xdg_state = tmp_path / 'xdg'
    monkeypatch.setenv('XDG_STATE_HOME', str(xdg_state))
    assert parse_args([]).data_dir == (xdg_state / 'robot-agent').resolve()
    env_state = tmp_path / 'explicit-env'
    monkeypatch.setenv('ROBOT_AGENT_DATA_DIR', str(env_state))
    assert parse_args([]).data_dir == env_state.resolve()
    cli_state = tmp_path / 'cli'
    assert parse_args(['--data-dir', str(cli_state)]).data_dir == cli_state.resolve()
    monkeypatch.setenv('ROBOT_AGENT_DATA_DIR', 'relative-data')
    assert parse_args([]).data_dir == (Path.cwd() / 'relative-data').resolve()


def test_one_shot_executes_real_simulator_and_persists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from robot_agent.app import main

    result = main(['--data-dir', str(tmp_path), '--thread-id', 'demo', '--prompt', '播报 hello'])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    output = json.loads(captured.out)
    assert output['model'] == 'test-model'
    assert output['backend'] == 'simulator'
    assert output['thread_id'] == 'demo'
    assert 'skill_catalog' not in output
    assert output['reply']
    assert output['observations'][0]['state'] == 'succeeded'
    assert (tmp_path / 'simulator-tasks.sqlite3').exists()
    assert (tmp_path / 'checkpoints.sqlite3').exists()


async def test_robot_skills_directory_exposes_catalog_before_loading_and_real_tools_after_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import parse_args, run_application
    from robot_agent_runtime.adapters.simulator import Simulator

    skill = tmp_path / 'skills' / 'robot-greeting'
    reference = skill / 'references' / 'greeting.md'
    reference.parent.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        '---\nname: robot-greeting\ndescription: Greet a visitor using robot speech.\n---\n'
        'ROBOT-GREETING-INSTRUCTIONS: Read references/greeting.md before speaking.\n', encoding='utf-8',
    )
    reference.write_text('Use the greeting supplied by the visitor.', encoding='utf-8')
    simulator = Simulator()
    monkeypatch.setattr('robot_agent.application.Simulator', lambda: simulator)

    class SelectingModel:
        def __init__(self) -> None:
            self.turn = 0

        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            self.turn += 1
            available = {schema['function']['name'] for schema in tools}
            assert 'run_skill_script' not in available
            system = '\n'.join(str(message.content) for message in messages if isinstance(message, SystemMessage))
            if self.turn == 1:
                assert available == {'load_skill', 'read_skill_resource'}
                assert 'robot-greeting' in system
                assert 'ROBOT-GREETING-INSTRUCTIONS' not in system
                name, args = 'load_skill', {'name': 'robot-greeting'}
            elif self.turn == 2:
                assert 'robot_speak' in available
                assert 'ROBOT-GREETING-INSTRUCTIONS' in system
                assert isinstance(messages[-1], ToolMessage)
                assert messages[-1].name == 'load_skill'
                name, args = 'read_skill_resource', {'name': 'robot-greeting', 'path': 'references/greeting.md'}
            elif self.turn == 3:
                assert isinstance(messages[-1], ToolMessage)
                assert json.loads(messages[-1].content)['result']['content'] == 'Use the greeting supplied by the visitor.'
                name, args = 'robot_speak', {'message': 'Welcome, visitor.'}
            else:
                assert self.turn == 4
                assert isinstance(messages[-1], ToolMessage)
                assert json.loads(messages[-1].content)['state'] == 'succeeded'
                return AIMessage(content='Greeting completed from simulator facts.')
            return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'turn-{self.turn}'}])

    config = parse_args([
        '--skills-dir', str(skill.parent), '--data-dir', str(tmp_path / 'state'),
        '--thread-id', 'custom-skills', '--prompt', 'Please greet the visitor.',
    ])
    await run_application(config, SelectingModel())
    output = json.loads(capsys.readouterr().out)
    assert output['reply'] == 'Greeting completed from simulator facts.'
    assert output['observations'][0]['tool'] == 'robot_speak'
    assert output['observations'][0]['state'] == 'succeeded'
    assert simulator.skills['audio.speak'].calls == 1


@pytest.mark.parametrize('catalog_state', ('missing', 'empty', 'invalid'))
async def test_robot_invalid_skills_directory_fails_before_runtime_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog_state: str,
) -> None:
    from robot_agent.app import parse_args, run_application
    from robot_agent_core.errors import AgentConfigurationError

    root = tmp_path / 'skills'
    if catalog_state != 'missing':
        root.mkdir()
    if catalog_state == 'invalid':
        folder = root / 'broken'
        folder.mkdir()
        (folder / 'SKILL.md').write_text('not a valid Skill document', encoding='utf-8')

    def forbidden_runtime(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('invalid Skill catalogs must fail before constructing RobotRuntime')

    class UnusedModel:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            pytest.fail('invalid Skill catalogs must fail before asking the model')

    monkeypatch.setattr('robot_agent.application.RobotRuntime', forbidden_runtime)
    state = tmp_path / 'state'
    config = parse_args(['--skills-dir', str(root), '--data-dir', str(state), '--prompt', 'hello'])
    with pytest.raises(AgentConfigurationError, match='Skill|skill'):
        await run_application(config, UnusedModel())
    assert not state.exists()


def test_interactive_runs_two_turns_and_stops_at_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import main

    monkeypatch.setattr('sys.stdin', io.StringIO('播报 hello\n播报 again\n'))
    result = main(['--data-dir', str(tmp_path), '--thread-id', 'interactive'])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    outputs = [json.loads(line) for line in captured.out.splitlines()]
    assert len(outputs) == 2
    assert all(row['thread_id'] == 'interactive' for row in outputs)
    assert all(row['observations'][0]['state'] == 'succeeded' for row in outputs)
    assert all(len(row['observations']) == 1 for row in outputs)


@pytest.mark.parametrize('bad_command', ('/skill missing', '/skill', '/skills extra'))
def test_interactive_skill_command_errors_preserve_selection_and_continue_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    bad_command: str,
) -> None:
    from robot_agent.app import main
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_runtime.adapters.simulator import Simulator

    folder = tmp_path / 'skills' / 'alpha'
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text(
        '---\nname: alpha\ndescription: Use alpha.\n---\nALPHA-BODY\n', encoding='utf-8',
    )
    simulator = Simulator()
    monkeypatch.setattr('robot_agent.application.Simulator', lambda: simulator)

    class Model:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            self.calls += 1
            system = '\n'.join(str(item.content) for item in messages if isinstance(item, SystemMessage))
            assert 'ALPHA-BODY' in system
            return AIMessage(content=f'normal turn {self.calls}')

    model = Model()
    monkeypatch.setattr('robot_agent.app.create_model', lambda config: model)
    monkeypatch.setattr('sys.stdin', io.StringIO(f'/skill alpha\nhello\n{bad_command}\nhello-again\n'))
    result = main(['--skills-dir', str(folder.parent), '--data-dir', str(tmp_path / 'state'),
                   '--thread-id', 'interactive-errors'])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    assert model.calls == 2
    outputs = [json.loads(line) for line in captured.out.splitlines()]
    assert len(outputs) == 4
    assert outputs[1]['reply'] == 'normal turn 1'
    assert outputs[3]['reply'] == 'normal turn 2'
    assert json.loads(outputs[2]['reply'])['error']['code'] == AgentConfigurationError.code
    assert outputs[2]['observations'] == []
    assert all(row['active_skill'] == 'alpha' for row in outputs)
    assert all(row['loaded_skills'] == ['alpha'] for row in outputs)
    assert outputs[2]['skill_versions'] == outputs[1]['skill_versions']
    assert simulator.skills['audio.speak'].calls == 0


def test_interactive_skill_command_without_registry_reports_error_then_executes_robot_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import main
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_runtime.adapters.simulator import Simulator

    simulator = Simulator()
    monkeypatch.setattr('robot_agent.application.Simulator', lambda: simulator)
    monkeypatch.setattr('sys.stdin', io.StringIO('/skills\n播报 still-works\n'))
    result = main(['--data-dir', str(tmp_path), '--thread-id', 'no-registry'])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    outputs = [json.loads(line) for line in captured.out.splitlines()]
    assert len(outputs) == 2
    assert json.loads(outputs[0]['reply'])['error']['code'] == AgentConfigurationError.code
    assert outputs[0]['observations'] == []
    assert outputs[1]['observations'][0]['state'] == 'succeeded'
    assert simulator.skills['audio.speak'].calls == 1


def test_interactive_selection_error_preserves_pending_checkpoint_and_allows_catalog_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from langchain_core.messages import HumanMessage

    from robot_agent.app import main, parse_args
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.graph import build_graph
    from robot_agent_core.skillkit.catalog import SkillRegistry
    from robot_agent_runtime.adapters.simulator import Simulator

    folder = tmp_path / 'skills' / 'alpha'
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text(
        '---\nname: alpha\ndescription: Use alpha.\n---\nALPHA-BODY\n', encoding='utf-8',
    )
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    argv = ['--skills-dir', str(folder.parent), '--data-dir', str(state_dir), '--thread-id', 'pending']
    config = parse_args(argv)
    document = SkillRegistry.from_directory(folder.parent).load('alpha')
    graph_config = {'configurable': {'thread_id': config.thread_id}}

    async def seed_checkpoint() -> Any:
        async with AsyncSqliteSaver.from_conn_string(str(state_dir / 'checkpoints.sqlite3')) as saver:
            graph = build_graph(checkpointer=saver)
            await graph.aupdate_state(graph_config, {
                'messages': [HumanMessage(content='durable request')], 'runtime_id': config.runtime_id,
                'active_skill': 'alpha', 'loaded_skills': ['alpha'], 'skill_documents': {'alpha': document},
                'skill_versions': {'alpha': document['fingerprint']}, 'pending': [{
                    'name': 'robot_speak', 'args': {'message': 'not started'},
                    'call_id': 'durable-call', 'operation_id': 'durable-operation',
                }],
            }, as_node='decide')
            return await graph.aget_state(graph_config)

    before = asyncio.run(seed_checkpoint())
    simulator = Simulator()
    monkeypatch.setattr('robot_agent.application.Simulator', lambda: simulator)
    monkeypatch.setattr('sys.stdin', io.StringIO('/skill alpha\n/skills\n'))
    result = main(argv)
    captured = capsys.readouterr()
    assert result == 0, captured.err
    outputs = [json.loads(line) for line in captured.out.splitlines()]
    assert len(outputs) == 2
    assert json.loads(outputs[0]['reply'])['error']['code'] == AgentConfigurationError.code
    assert 'pending' in outputs[0]['reply']
    assert all(row['active_skill'] == 'alpha' for row in outputs)
    assert all(row['loaded_skills'] == ['alpha'] for row in outputs)
    assert simulator.skills['audio.speak'].calls == 0

    async def read_checkpoint() -> Any:
        async with AsyncSqliteSaver.from_conn_string(str(state_dir / 'checkpoints.sqlite3')) as saver:
            return await build_graph(checkpointer=saver).aget_state(graph_config)

    after = asyncio.run(read_checkpoint())
    assert after.config == before.config
    assert after.values == before.values
    assert after.next == before.next == ('execute',)


def test_interactive_non_skill_errors_keep_existing_fatal_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import main
    from robot_agent_core.errors import AgentConfigurationError

    class Model:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            self.calls += 1
            raise AgentConfigurationError('ordinary input failure')

    model = Model()
    monkeypatch.setattr('robot_agent.app.create_model', lambda config: model)
    monkeypatch.setattr('sys.stdin', io.StringIO('/skillful\nhello-again\n'))
    result = main(['--data-dir', str(tmp_path), '--thread-id', 'fatal'])
    captured = capsys.readouterr()
    assert result == 1
    assert model.calls == 1
    assert captured.out == ''
    assert 'ordinary input failure' in captured.err


async def test_new_input_does_not_replace_pending_checkpoint_and_resume_uses_it(tmp_path: Path) -> None:
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent.robot_tools import RobotTools
    from robot_agent_core.shell import invoke_turn
    from robot_agent_runtime.adapters.simulator import Simulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.sdk import RobotRuntime

    class ScriptModel:
        def __init__(self) -> None:
            self.turn = 0

        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            self.turn += 1
            if self.turn == 1:
                return AIMessage(content='', tool_calls=[{
                    'name': 'robot_speak', 'args': {'message': 'once'}, 'id': 'speak',
                }])
            return AIMessage(content='done')

    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        context = AgentContext(model=ScriptModel(), tools=await RobotTools.create(runtime))
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'graph.sqlite3')) as saver:
            paused = build_graph(checkpointer=saver, interrupt_before=['execute'])
            await invoke_turn(paused, context, thread_id='paused', runtime_id='robot-one', prompt='hello')
            graph = build_graph(checkpointer=saver)
            with pytest.raises(AgentConfigurationError, match='resume'):
                await invoke_turn(graph, context, thread_id='paused', runtime_id='robot-one', prompt='overwrite')
            with pytest.raises(AgentConfigurationError, match='backend|runtime|robot'):
                await invoke_turn(graph, context, thread_id='paused', runtime_id='robot-two', prompt=None)
            result = await invoke_turn(graph, context, thread_id='paused', runtime_id='robot-one', prompt=None)
            assert result['messages'][-1].content == 'done'
            assert simulator.skills['audio.speak'].calls == 1
            with pytest.raises(AgentConfigurationError, match='pending|resume'):
                await invoke_turn(graph, context, thread_id='paused', runtime_id='robot-one', prompt=None)
            with pytest.raises(AgentConfigurationError, match='checkpoint|thread'):
                await invoke_turn(graph, context, thread_id='unknown', runtime_id='robot-one', prompt=None)
    finally:
        await runtime.close()


async def test_platform_cleanup_runs_after_runtime_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent.app import parse_args
    from robot_agent.application import open_runtime
    from robot_agent_runtime.errors import RobotConnectionError

    events: list[str] = []
    owner = asyncio.get_running_loop()

    class Platform:
        async def start(self) -> None:
            assert asyncio.get_running_loop() is owner
            events.append('platform.start')

        async def close(self) -> None:
            assert asyncio.get_running_loop() is owner
            events.append('platform.close')

    class Runtime:
        @classmethod
        def from_platform(cls, *args: Any, **kwargs: Any) -> 'Runtime':
            return cls()

        async def start(self) -> None:
            events.append('runtime.start')
            raise RobotConnectionError('startup failed')

        async def close(self) -> None:
            events.append('runtime.close')

    monkeypatch.setattr('robot_agent_runtime.adapters.cc_api.create_cc_api_platform', lambda *a, **k: Platform())
    monkeypatch.setattr('robot_agent.application.RobotRuntime', Runtime)
    config = parse_args(['--backend', 'cc', '--api-id', 'test-id', '--data-dir', str(tmp_path)])
    with pytest.raises(RobotConnectionError, match='startup failed'):
        async with open_runtime(config, api=object()):
            pytest.fail('startup should fail')
    assert events == ['platform.start', 'runtime.start', 'runtime.close', 'platform.close']


def test_configuration_errors_are_structured_and_redact_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import main
    from robot_agent_core.errors import AgentConfigurationError

    monkeypatch.setenv('OPENAI_API_KEY', 'private-key')
    result = main(['--model', ''])
    captured = capsys.readouterr()
    assert result != 0
    assert captured.out == ''
    assert json.loads(captured.err)['error']['code'] == AgentConfigurationError.code
    assert 'private-key' not in captured.err


def test_cc_connection_is_outside_asyncio_and_stopped_after_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import sys
    from types import SimpleNamespace

    from robot_agent.app import main
    from robot_agent_runtime.errors import RobotConnectionError

    events: list[str] = []

    class Api:
        def initialize(self, config: Any) -> None:
            assert config.api_id == 'test-id'
            assert config.address == 'robot:9999'
            assert config.token == 'cc-secret'
            assert config.is_reconnect is False
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            events.append('api.initialize')

        def start(self) -> None:
            events.append('api.start')
            raise RobotConnectionError('cc-secret')

        def stop(self) -> None:
            events.append('api.stop')

    monkeypatch.setenv('CC_API_TOKEN', 'cc-secret')
    monkeypatch.setitem(sys.modules, 'ubt_robot', SimpleNamespace(Config=SimpleNamespace, Api=Api))
    assert main(['--backend', 'cc', '--api-id', 'test-id', '--address', 'robot:9999',
                 '--data-dir', str(tmp_path), '--prompt', 'hello']) != 0
    captured = capsys.readouterr()
    assert events == ['api.initialize', 'api.start', 'api.stop']
    assert captured.out == ''
    assert 'cc-secret' not in captured.err
    assert json.loads(captured.err)['error']['code'] == 'ROBOT_CONNECTION_ERROR'


def test_app_owned_model_is_closed_in_its_loop_even_when_execution_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import main
    from robot_agent_runtime.errors import InvalidRequest

    events: list[str] = []

    class Model:
        async def aclose(self) -> None:
            assert asyncio.get_running_loop().is_running()
            events.append('model.close')

    async def execute(*args: Any, **kwargs: Any) -> None:
        events.append('application.run')
        raise InvalidRequest('failure')

    monkeypatch.setattr('robot_agent.app.create_model', lambda config: Model())
    monkeypatch.setattr('robot_agent.app.run_application', execute)
    assert main(['--data-dir', str(tmp_path), '--prompt', 'hello']) == 1
    assert events == ['application.run', 'model.close']
    assert json.loads(capsys.readouterr().err)['error']['code'] == InvalidRequest.code


async def test_platform_is_closed_even_when_runtime_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent.app import parse_args
    from robot_agent.application import open_runtime
    from robot_agent_runtime.errors import RobotConnectionError

    events: list[str] = []

    class Platform:
        async def start(self) -> None:
            events.append('platform.start')

        async def close(self) -> None:
            events.append('platform.close')

    class Runtime:
        @classmethod
        def from_platform(cls, *args: Any, **kwargs: Any) -> 'Runtime':
            return cls()

        async def start(self) -> None:
            events.append('runtime.start')

        async def close(self) -> None:
            events.append('runtime.close')
            raise RobotConnectionError('close failed')

    monkeypatch.setattr('robot_agent_runtime.adapters.cc_api.create_cc_api_platform', lambda *a, **k: Platform())
    monkeypatch.setattr('robot_agent.application.RobotRuntime', Runtime)
    config = parse_args(['--backend', 'cc', '--api-id', 'test-id', '--data-dir', str(tmp_path)])
    with pytest.raises(RobotConnectionError, match='close failed'):
        async with open_runtime(config, api=object()):
            events.append('application.run')
    assert events == ['platform.start', 'runtime.start', 'application.run', 'runtime.close', 'platform.close']


def test_help_does_not_create_a_model_or_vendor_connection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import main

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail('help must not construct a model or connection')

    monkeypatch.setattr('robot_agent.app.create_model', forbidden)
    monkeypatch.setattr('robot_agent.app.open_cc_connection', forbidden)
    with pytest.raises(SystemExit) as result:
        main(['--backend', 'cc', '--help'])
    assert result.value.code == 0
    assert 'LangGraph robot agent' in capsys.readouterr().out
    assert '--model-provider' not in capsys.readouterr().out


def test_cc_native_connection_outlives_async_application_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from types import SimpleNamespace

    from robot_agent.app import main

    events: list[str] = []

    class Api:
        def initialize(self, config: Any) -> None:
            events.append('api.initialize')

        def start(self) -> None:
            events.append('api.start')

        def wait_until_connected(self, timeout_ms: int) -> bool:
            assert timeout_ms == 5000
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            events.append('api.connected')
            return True

        def stop(self) -> None:
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            events.append('api.stop')

    class Model:
        async def aclose(self) -> None:
            assert asyncio.get_running_loop().is_running()
            events.append('model.close')

    async def execute(config: Any, model: Any, api: Any) -> None:
        assert isinstance(api, Api)
        assert asyncio.get_running_loop().is_running()
        events.append('application.run')

    monkeypatch.setitem(sys.modules, 'ubt_robot', SimpleNamespace(Config=SimpleNamespace, Api=Api))
    monkeypatch.setattr('robot_agent.app.create_model', lambda config: Model())
    monkeypatch.setattr('robot_agent.app.run_application', execute)
    assert main(['--backend', 'cc', '--api-id', 'test-id', '--data-dir', str(tmp_path)]) == 0
    assert events == ['api.initialize', 'api.start', 'api.connected', 'application.run', 'model.close', 'api.stop']


def test_error_redaction_handles_json_escaped_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent_core.cli import write_error as _write_error
    from robot_agent_runtime.errors import RobotConnectionError

    secret = 'secret-"token\\with\nnewline'
    monkeypatch.setenv('CC_API_TOKEN', secret)
    _write_error(RobotConnectionError(f'failed: {secret}', details={'nested': [secret]}))
    output = json.loads(capsys.readouterr().err)
    assert output['error']['message'] == 'failed: [REDACTED]'
    assert output['error']['details']['nested'] == ['[REDACTED]']


async def test_interactive_wait_keeps_application_loop_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from robot_agent.app import parse_args, run_application
    from scripted_models import ScriptedRobotModel

    @asynccontextmanager
    async def resource(*args: Any, **kwargs: Any) -> Any:
        yield object()

    async def create_tools(*args: Any, **kwargs: Any) -> Any:
        return object()

    monkeypatch.setattr('robot_agent.application.AsyncSqliteSaver', SimpleNamespace(from_conn_string=resource))
    monkeypatch.setattr('robot_agent.application.open_runtime', resource)
    monkeypatch.setattr('robot_agent.application.build_graph', lambda **kwargs: object())
    monkeypatch.setattr('robot_agent.application.RobotTools.create', create_tools)
    read_fd, write_fd = os.pipe()
    heartbeat = threading.Event()
    responsive: list[bool] = []

    def provide_input() -> None:
        try:
            responsive.append(heartbeat.wait(timeout=0.5))
            os.write(write_fd, b'quit\n')
        finally:
            os.close(write_fd)

    writer = threading.Thread(target=provide_input)
    writer.start()
    try:
        with os.fdopen(read_fd, 'r', encoding='utf-8') as stream:
            monkeypatch.setattr('sys.stdin', stream)
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await run_application(parse_args(['--data-dir', str(tmp_path)]), ScriptedRobotModel())
        assert responsive == [True], 'waiting for input blocked the platform callback loop'
    finally:
        writer.join(timeout=1)
        assert not writer.is_alive()


async def test_prompt_reader_cancel_unregisters_fd_and_restores_blocking_mode() -> None:
    from robot_agent_core.shell import AsyncPromptReader

    read_fd, write_fd = os.pipe()
    loop = asyncio.get_running_loop()
    try:
        with os.fdopen(read_fd, 'r', encoding='utf-8') as stream:
            reader = AsyncPromptReader(stream)
            task = asyncio.create_task(reader.readline())
            checkpoint = asyncio.Event()
            loop.call_soon(checkpoint.set)
            await checkpoint.wait()
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert loop.remove_reader(read_fd) is False
            assert os.get_blocking(read_fd) is True
    finally:
        os.close(write_fd)


async def test_prompt_reader_handles_batched_lines_split_unicode_and_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    from robot_agent_core.shell import AsyncPromptReader

    read_fd, write_fd = os.pipe()
    first_chunk = asyncio.Event()
    original_read = os.read

    def observe_read(fd: int, size: int) -> bytes:
        data = original_read(fd, size)
        if fd == read_fd:
            first_chunk.set()
        return data

    monkeypatch.setattr('robot_agent_core.shell.os.read', observe_read)
    try:
        with os.fdopen(read_fd, 'r', encoding='utf-8') as stream:
            reader = AsyncPromptReader(stream)
            chinese = '播报'.encode()
            os.write(write_fd, chinese[:2])
            pending = asyncio.create_task(reader.readline())
            await asyncio.wait_for(first_chunk.wait(), timeout=1)
            assert not pending.done()
            os.write(write_fd, chinese[2:] + b' hello\r\nnext\nlast')
            os.close(write_fd)
            write_fd = -1
            assert await asyncio.wait_for(pending, timeout=1) == '播报 hello'
            assert await reader.readline() == 'next'
            assert await reader.readline() == 'last'
            with pytest.raises(EOFError):
                await reader.readline()
    finally:
        if write_fd != -1:
            os.close(write_fd)


async def test_prompt_reader_accepts_regular_files_and_stringio(tmp_path: Path) -> None:
    from robot_agent_core.shell import AsyncPromptReader

    with io.StringIO('one\ntwo') as stream:
        reader = AsyncPromptReader(stream)
        assert await reader.readline() == 'one'
        assert await reader.readline() == 'two'
        with pytest.raises(EOFError):
            await reader.readline()
    path = tmp_path / 'input.txt'
    path.write_text('file input\n', encoding='utf-8')
    with path.open(encoding='utf-8') as stream:
        reader = AsyncPromptReader(stream)
        assert await reader.readline() == 'file input'
        with pytest.raises(EOFError):
            await reader.readline()


def test_cli_skill_selection_is_repeatable_and_separate_from_robot_permissions() -> None:
    from robot_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError

    defaults = parse_args([])
    assert defaults.enabled_skills is None
    selected = parse_args(['--skill', 'alpha', '--skill', 'beta', '--skill', 'alpha',
                           '--allow-skill', 'audio.speak'])
    assert selected.enabled_skills == ('alpha', 'beta')
    assert selected.allowed_skills == frozenset({'audio.speak'})
    for name in ('', '../alpha', 'Alpha', 'alpha/SKILL.md'):
        with pytest.raises(AgentConfigurationError, match='skill|Skill'):
            parse_args(['--skill', name])


async def test_selected_catalog_is_filtered_before_model_and_robot_tools_are_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import parse_args, run_application
    from robot_agent_core.skillkit.catalog import SkillRegistry
    from robot_agent_runtime.adapters.simulator import Simulator

    registry = SkillRegistry([
        {'name': name, 'description': 'Use ' + name, 'instructions': name.upper() + '-BODY',
         'fingerprint': name + '-version'} for name in ('alpha', 'beta')
    ])
    monkeypatch.setattr(SkillRegistry, 'bundled', classmethod(lambda cls, package: registry))
    simulator = Simulator()
    monkeypatch.setattr('robot_agent.application.Simulator', lambda: simulator)

    class Model:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            system = '\n'.join(str(item.content) for item in messages if isinstance(item, SystemMessage))
            assert 'Use alpha' in system
            assert 'Use beta' not in system
            assert 'ALPHA-BODY' not in system
            assert 'BETA-BODY' not in system
            return AIMessage(content='Only alpha is available')

    config = parse_args(['--skill', 'alpha', '--data-dir', str(tmp_path / 'state'), '--prompt', 'hello'])
    await run_application(config, Model())
    output = json.loads(capsys.readouterr().out)
    assert output['skill_catalog'] == [{'name': 'alpha', 'description': 'Use alpha'}]
    assert output['active_skill'] is None or output['active_skill'] == ''
    assert output['loaded_skills'] == []
    assert simulator.skills['audio.speak'].calls == 0


async def test_unknown_skill_selection_fails_before_data_or_runtime_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent.app import parse_args, run_application
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.skillkit.catalog import SkillRegistry

    registry = SkillRegistry([{
        'name': 'alpha', 'description': 'Use alpha', 'instructions': 'ALPHA-BODY', 'fingerprint': 'v1',
    }])
    monkeypatch.setattr(SkillRegistry, 'bundled', classmethod(lambda cls, package: registry))

    def forbidden_runtime(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('invalid --skill must fail before constructing RobotRuntime')

    class UnusedModel:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            pytest.fail('invalid --skill must fail before asking the model')

    monkeypatch.setattr('robot_agent.application.RobotRuntime', forbidden_runtime)
    state = tmp_path / 'state'
    config = parse_args(['--skill', 'missing', '--data-dir', str(state), '--prompt', 'hello'])
    with pytest.raises(AgentConfigurationError, match='unknown|catalog'):
        await run_application(config, UnusedModel())
    assert not state.exists()


async def test_empty_skill_selection_fails_before_data_or_runtime_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from robot_agent.app import parse_args, run_application
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.skillkit.catalog import SkillRegistry

    registry = SkillRegistry([{
        'name': 'alpha', 'description': 'Use alpha', 'instructions': 'ALPHA-BODY', 'fingerprint': 'v1',
    }])
    monkeypatch.setattr(SkillRegistry, 'bundled', classmethod(lambda cls, package: registry))

    def forbidden_runtime(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('an empty Skill catalog must fail before constructing RobotRuntime')

    class UnusedModel:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            pytest.fail('an empty Skill catalog must fail before asking the model')

    monkeypatch.setattr('robot_agent.application.RobotRuntime', forbidden_runtime)
    state = tmp_path / 'state'
    config = replace(parse_args(['--data-dir', str(state), '--prompt', 'hello']), enabled_skills=())
    with pytest.raises(AgentConfigurationError, match='nonempty'):
        await run_application(config, UnusedModel())
    assert not state.exists()


async def test_robot_explicit_skill_selection_uses_no_model_or_simulator_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import parse_args, run_application
    from robot_agent_runtime.adapters.simulator import Simulator

    root = tmp_path / 'skills'
    folder = root / 'robot-greeting'
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text(
        '---\nname: robot-greeting\ndescription: Use visitor greetings.\n---\nGREETING-BODY\n', encoding='utf-8',
    )
    simulator = Simulator()
    monkeypatch.setattr('robot_agent.application.Simulator', lambda: simulator)

    class UnusedModel:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            pytest.fail('/skill is a host command, not a model turn')

    config = parse_args(['--skills-dir', str(root), '--skill', 'robot-greeting',
                         '--data-dir', str(tmp_path / 'state'), '--thread-id', 'explicit',
                         '--prompt', '/skill robot-greeting'])
    await run_application(config, UnusedModel())
    output = json.loads(capsys.readouterr().out)
    assert output['active_skill'] == 'robot-greeting'
    assert output['loaded_skills'] == ['robot-greeting']
    assert output['observations'][0]['tool'] == 'load_skill'
    assert simulator.skills['audio.speak'].calls == 0
