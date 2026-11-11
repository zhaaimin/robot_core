import asyncio
import os
from collections.abc import Sequence
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

from hotel_agent.application import run_hotel_application
from hotel_agent.profiles import HOTEL_PROFILE


def parse_args(argv: Sequence[str] | None = None) -> AppConfig:
    parser = ApplicationParser(
        description='Hotel reception, handoff and escort agent; the decision model is always an OpenAI-compatible endpoint.',
    )
    parser.add_argument('--backend', choices=tuple(sorted(HOTEL_PROFILE.allowed_backends)), default='simulator')
    parser.add_argument('--runtime-socket', type=Path, help='existing runtime Unix socket for the MCP backend')
    parser.add_argument('--skills-dir', type=Path, help='override the controlled Agent Skill directory')
    parser.add_argument('--skill', action='append', help='expose an Agent Skill by name; repeat to expose more')
    parser.add_argument('--model', default=os.environ.get('ROBOT_AGENT_MODEL'))
    parser.add_argument('--prompt', help='run one turn; omit for interactive input')
    parser.add_argument('--thread-id', help='guest session identifier; generated when omitted')
    parser.add_argument('--resume', action='store_true', help='resume pending work without a new prompt')
    parser.add_argument('--data-dir', type=Path, help=f'override {DATA_DIR_ENVIRONMENT} or the user state directory')
    parser.add_argument('--max-turns', type=positive_integer, default=8)
    parser.add_argument('--wait-timeout', type=positive_timeout, default=5.0)
    parser.add_argument('--task-timeout', type=positive_timeout, default=60.0)
    parser.add_argument('--model-timeout', type=positive_timeout, default=30.0)
    parser.add_argument('--allow-skill', action='append', help='limit discovered skills; repeat to allow more')
    args = parser.parse_args(argv)
    HOTEL_PROFILE.validate_backend(args.backend)
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
    defaults = {'audio.speak', 'navigation.navigate_to'} if args.backend == 'mcp' else {
        'audio.speak', 'navigation.navigate_to', 'robot.get_state', 'wait.delay',
    }
    allowed = frozenset(args.allow_skill if args.allow_skill is not None else defaults)
    if any(not skill.strip() for skill in allowed):
        raise AgentConfigurationError('--allow-skill must not be empty')
    if args.skill is not None and any(not valid_name(name) for name in args.skill):
        raise AgentConfigurationError('--skill must name a valid Agent Skill')
    data_dir = args.data_dir if args.data_dir is not None else default_data_dir()
    storage = resolve_storage(
        data_dir=data_dir, backend=args.backend, api_id=None, address=None,
        profile=HOTEL_PROFILE, runtime_socket=args.runtime_socket,
    )
    return AppConfig(
        backend=args.backend, model=args.model, profile=HOTEL_PROFILE,
        api_key=api_key, base_url=os.environ.get('OPENAI_BASE_URL') or None,
        api_id=None, address=None, api_token='',
        data_dir=storage.data_dir, thread_id=args.thread_id or str(uuid4()),
        prompt=args.prompt, resume=args.resume, max_turns=args.max_turns,
        wait_timeout_s=args.wait_timeout, task_timeout_s=args.task_timeout,
        model_timeout_s=args.model_timeout, allowed_skills=allowed,
        task_database=storage.task_database, checkpoint_database=storage.checkpoint_database,
        business_database=storage.business_database, runtime_id=storage.runtime_id,
        skills_dir=args.skills_dir.expanduser().resolve() if args.skills_dir is not None else None,
        enabled_skills=tuple(dict.fromkeys(args.skill)) if args.skill is not None else None,
        runtime_socket=storage.runtime_socket,
    )


async def run_application(
    config: AppConfig, model: DecisionModel, api: Any = None,
) -> None:
    """Check the snapshot this agent owns, then run one hotel workflow loop."""
    validate_config(config)
    await run_hotel_application(config, model)


async def _run_owned_application(config: AppConfig) -> None:
    model = create_model(config)
    try:
        await run_application(config, model)
    finally:
        await close_model(model)


def _launch(config: AppConfig) -> None:
    asyncio.run(_run_owned_application(config))


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(parse_args, _launch, argv)
