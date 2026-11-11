"""Command-line plumbing that every agent application must behave the same way about.

Each project keeps its own flags, defaults and entry point; this module holds only
the parts that drift when duplicated: argument validation, the user state directory
convention, credential-safe error output, model release, and the exit code contract
(0 success, 130 interrupt, 1 any agent failure). Nothing here composes an
application or knows what a scenario is.
"""

import argparse
import json
import math
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from robot_agent_core.config import AppConfig
from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.models import DecisionModel, OpenAIModel
from robot_agent_runtime.errors import AgentRuntimeError, InternalError, TaskCancelled

DATA_DIR_ENVIRONMENT = 'ROBOT_AGENT_DATA_DIR'


class ApplicationParser(argparse.ArgumentParser):
    """Report usage mistakes through the application error contract.

    A bare ``ArgumentParser`` calls ``sys.exit(2)``, which would leave the CLI with
    an exit code and a message format no other failure uses.
    """

    def error(self, message: str) -> None:
        raise AgentConfigurationError(message)


def positive_timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a finite positive number') from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return result


def positive_integer(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('must be a positive integer') from None
    if result <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return result


def default_data_dir() -> Path:
    """Resolve the state directory once, from the explicit override or user state."""
    explicit = os.environ.get(DATA_DIR_ENVIRONMENT)
    if explicit:
        return Path(explicit)
    xdg_state = os.environ.get('XDG_STATE_HOME')
    if xdg_state:
        path = Path(xdg_state).expanduser()
        if path.is_absolute():
            return path / 'robot-agent'
    return Path.home() / '.local' / 'state' / 'robot-agent'


def create_model(config: AppConfig) -> DecisionModel:
    """Build the only supported decision model; credentials are a startup precondition."""
    if config.model is None or config.api_key is None:
        raise AgentConfigurationError('OpenAI configuration is incomplete')
    return OpenAIModel(
        model=config.model, api_key=config.api_key, base_url=config.base_url,
        timeout_s=config.model_timeout_s,
    )


async def close_model(model: DecisionModel) -> None:
    """Release a model's transport after the turn ends, including on failure.

    A close error still propagates: an unclosed client is a real fault, and the
    caller's finally block must not convert it into a clean exit.
    """
    close = getattr(model, 'aclose', None)
    if close is not None:
        await close()


def write_error(error: AgentRuntimeError) -> None:
    """Print one JSON error envelope with any live credential redacted.

    Adapters can echo request parameters into error details, so the secret values
    are collected from the environment at write time and removed recursively
    rather than relying on callers to avoid logging them.
    """
    secrets = sorted(
        (secret for name in ('OPENAI_API_KEY', 'CC_API_TOKEN') if (secret := os.environ.get(name))),
        key=len, reverse=True,
    )

    def redact(value: Any) -> Any:
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, '[REDACTED]')
            return value
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    print(json.dumps({'error': redact(error.to_dict())}, ensure_ascii=False), file=sys.stderr)


def run_cli(
    parse: Callable[[Sequence[str] | None], AppConfig],
    launch: Callable[[AppConfig], None],
    argv: Sequence[str] | None = None,
) -> int:
    """Run one application start under the shared exit code contract.

    ``launch`` owns asyncio and any native connection; failures never surface a
    traceback or an exception message that could carry a credential.
    """
    try:
        launch(parse(argv))
        return 0
    except KeyboardInterrupt:
        write_error(TaskCancelled('Agent application interrupted'))
        return 130
    except AgentRuntimeError as error:
        write_error(error)
        return 1
    except Exception:
        write_error(InternalError('Agent application failed'))
        return 1

