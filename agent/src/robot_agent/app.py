import asyncio
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from robot_agent_core.cli import (
    DATA_DIR_ENVIRONMENT, ApplicationParser, close_model, create_model, default_data_dir,
    positive_integer, positive_timeout, run_cli,
)
from robot_agent_core.config import AppConfig
from robot_agent_core.configuration import resolve_storage, validate_config
from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.models import DecisionModel
from robot_agent_core.skillkit.catalog import valid_name
from robot_agent_runtime.errors import RobotConnectionError

from robot_agent.application import run_robot_application
from robot_agent.profiles import ROBOT_PROFILE


def parse_args(argv: Sequence[str] | None = None) -> AppConfig:
    parser = ApplicationParser(
        description='LangGraph robot agent; the decision model is always an OpenAI-compatible endpoint.',
    )
    parser.add_argument('--backend', choices=tuple(sorted(ROBOT_PROFILE.allowed_backends)), default='simulator')
    parser.add_argument('--skills-dir', type=Path, help='override the controlled Agent Skill directory')
    parser.add_argument('--skill', action='append', help='expose an Agent Skill by name; repeat to expose more')
    parser.add_argument('--model', default=os.environ.get('ROBOT_AGENT_MODEL'))
    parser.add_argument('--api-id', default=os.environ.get('CC_API_ID'))
    parser.add_argument('--address', default=os.environ.get('CC_API_ADDRESS'))
    parser.add_argument('--prompt', help='run one turn; omit for interactive input')
    parser.add_argument('--thread-id', help='persistent conversation identifier; generated when omitted')
    parser.add_argument('--resume', action='store_true', help='resume pending work without a new prompt')
    parser.add_argument('--data-dir', type=Path, help=f'override {DATA_DIR_ENVIRONMENT} or the user state directory')
    parser.add_argument('--max-turns', type=positive_integer, default=8)
    parser.add_argument('--wait-timeout', type=positive_timeout, default=5.0)
    parser.add_argument('--task-timeout', type=positive_timeout, default=60.0)
    parser.add_argument('--model-timeout', type=positive_timeout, default=30.0)
    parser.add_argument('--allow-skill', action='append', help='limit discovered skills; repeat to allow more')
    args = parser.parse_args(argv)
    ROBOT_PROFILE.validate_backend(args.backend)
    if args.max_turns > 100:
        raise AgentConfigurationError('--max-turns must be an integer from 1 to 100')
    if args.thread_id is not None and not args.thread_id.strip():
        raise AgentConfigurationError('--thread-id must not be empty')
    if args.resume and (args.thread_id is None or args.prompt is not None):
        raise AgentConfigurationError('--resume requires an explicit --thread-id and no --prompt')
    if args.prompt is not None and not args.prompt.strip():
        raise AgentConfigurationError('--prompt must not be empty')
    api_key = os.environ.get('OPENAI_API_KEY')
    if not args.model or not args.model.strip():
        raise AgentConfigurationError('A decision model is required: --model or ROBOT_AGENT_MODEL')
    if not api_key or not api_key.strip():
        raise AgentConfigurationError('A decision model is required: OPENAI_API_KEY')
    if args.backend == 'cc' and (not args.api_id or not args.api_id.strip()):
        raise AgentConfigurationError('CC mode requires --api-id or CC_API_ID')
    defaults = {'audio.speak'} if args.backend == 'cc' else {
        'audio.speak', 'navigation.navigate_to', 'robot.get_state', 'wait.delay',
    }
    allowed = frozenset(args.allow_skill if args.allow_skill is not None else defaults)
    if any(not skill.strip() for skill in allowed):
        raise AgentConfigurationError('--allow-skill must not be empty')
    if args.skill is not None and any(not valid_name(name) for name in args.skill):
        raise AgentConfigurationError('--skill must name a valid Agent Skill')
    data_dir = args.data_dir if args.data_dir is not None else default_data_dir()
    address = args.address or None
    storage = resolve_storage(
        data_dir=data_dir, backend=args.backend, api_id=args.api_id, address=address,
        profile=ROBOT_PROFILE,
    )
    return AppConfig(
        backend=args.backend, model=args.model, profile=ROBOT_PROFILE,
        api_key=api_key, base_url=os.environ.get('OPENAI_BASE_URL') or None, api_id=args.api_id,
        address=address, api_token=os.environ.get('CC_API_TOKEN', ''),
        data_dir=storage.data_dir, thread_id=args.thread_id or str(uuid4()),
        prompt=args.prompt, resume=args.resume, max_turns=args.max_turns,
        wait_timeout_s=args.wait_timeout, task_timeout_s=args.task_timeout,
        model_timeout_s=args.model_timeout, allowed_skills=allowed,
        task_database=storage.task_database, checkpoint_database=storage.checkpoint_database,
        business_database=storage.business_database, runtime_id=storage.runtime_id,
        skills_dir=args.skills_dir.expanduser().resolve() if args.skills_dir is not None else None,
        enabled_skills=tuple(dict.fromkeys(args.skill)) if args.skill is not None else None,
    )


@contextmanager
def open_cc_connection(config: AppConfig) -> Iterator[Any]:
    """The native SDK owns threads and must be started outside asyncio.run.

    The application retains the original connection until both runtime and
    platform have released their callbacks, including failed initialization.
    """
    validate_config(config)
    try:
        import ubt_robot
    except ImportError:
        raise RobotConnectionError('CC backend requires the supplied ubt_robot SDK') from None
    native_config = ubt_robot.Config()
    native_config.api_id = config.api_id
    native_config.is_reconnect = False
    if config.address:
        native_config.address = config.address
        native_config.token = config.api_token
    api = ubt_robot.Api()
    try:
        api.initialize(native_config)
        api.start()
        if not api.wait_until_connected(5000):
            raise RobotConnectionError('CC API did not connect within 5 seconds')
        yield api
    finally:
        api.stop()


async def run_application(
    config: AppConfig, model: DecisionModel, api: Any = None,
) -> None:
    """Check the snapshot this agent owns before its application opens storage."""
    validate_config(config)
    await run_robot_application(config, model, api)


async def _run_owned_application(config: AppConfig, api: Any = None) -> None:
    model = create_model(config)
    try:
        await run_application(config, model, api)
    finally:
        await close_model(model)


def _launch(config: AppConfig) -> None:
    """Own the event loop, and the native SDK connection only for the CC backend."""
    if config.backend == 'cc':
        with open_cc_connection(config) as api:
            asyncio.run(_run_owned_application(config, api))
        return
    asyncio.run(_run_owned_application(config))


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(parse_args, _launch, argv)
