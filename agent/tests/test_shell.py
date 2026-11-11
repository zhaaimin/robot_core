import io
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage


async def test_shared_shell_preserves_interactive_input_and_error_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.shell import error_state, result_envelope, run_shell
    from robot_agent_runtime.errors import AgentRuntimeError

    config = parse_args(['--data-dir', str(tmp_path), '--thread-id', 'one'])
    monkeypatch.setattr('sys.stdin', io.StringIO('\nhello\nbad\n/exit\nignored\n'))
    received: list[str | None] = []

    async def turn(prompt: str | None) -> dict[str, Any]:
        received.append(prompt)
        if prompt == 'bad':
            raise AgentConfigurationError('choose a valid skill')
        return result_envelope(config, {'messages': [AIMessage(content='hello back')]})

    async def recover(prompt: str, error: AgentRuntimeError) -> dict[str, Any]:
        assert prompt == 'bad'
        state = error_state(error, {'active_skill': 'local-help', 'loaded_skills': ['local-help']})
        return result_envelope(config, state, catalog=[{'name': 'local-help', 'description': 'Help.'}])

    await run_shell(config, turn=turn, prompt=lambda: 'test> ', recover=recover)
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert received == ['hello', 'bad']
    assert outputs[0]['reply'] == 'hello back'
    assert outputs[1]['thread_id'] == 'one'
    assert outputs[1]['active_skill'] == 'local-help'
    assert json.loads(outputs[1]['reply'])['error']['code'] == 'AGENT_CONFIGURATION_INVALID'


async def test_shared_shell_one_shot_does_not_read_interactive_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from robot_agent.app import parse_args
    from robot_agent_core.shell import run_shell

    config = parse_args(['--data-dir', str(tmp_path), '--prompt', 'one shot'])

    class UnexpectedInput:
        def fileno(self) -> int:
            pytest.fail('One-shot execution touched stdin')

    monkeypatch.setattr('sys.stdin', UnexpectedInput())
    received: list[str | None] = []

    async def turn(prompt: str | None) -> dict[str, Any]:
        received.append(prompt)
        return {'reply': 'finished'}

    await run_shell(config, turn=turn, prompt=lambda: 'unused> ')
    assert received == ['one shot']
    assert json.loads(capsys.readouterr().out) == {'reply': 'finished'}


async def test_shared_shell_does_not_retry_business_rendering_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.shell import run_shell
    from robot_agent_runtime.errors import AgentRuntimeError

    config = parse_args(['--data-dir', str(tmp_path)])
    monkeypatch.setattr('sys.stdin', io.StringIO('/status\n/exit\n'))
    renders: list[dict[str, Any]] = []

    async def turn(prompt: str | None) -> dict[str, Any]:
        return {'reply': 'state'}

    async def render(state: dict[str, Any]) -> dict[str, Any]:
        renders.append(state)
        raise AgentConfigurationError('business state unavailable')

    async def recover(prompt: str, error: AgentRuntimeError) -> dict[str, Any]:
        pytest.fail('Rendering errors must not retry a business snapshot')

    with pytest.raises(AgentConfigurationError, match='business state unavailable'):
        await run_shell(config, turn=turn, render=render, prompt=lambda: 'test> ', recover=recover)
    assert renders == [{'reply': 'state'}]
