import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from guide_agent.profiles import GUIDE_PROFILE
from robot_agent_core.cli import (
    ApplicationParser, close_model, create_model, default_data_dir,
    positive_integer, positive_timeout, run_cli,
)
from robot_agent_core.config import AppConfig
from robot_agent_core.configuration import resolve_storage
from robot_agent_core.errors import AgentConfigurationError


@dataclass(frozen=True)
class GuideConfig(AppConfig):
    input_mode: str = 'text'


def parse_args(argv: Sequence[str] | None = None) -> GuideConfig:
    parser = ApplicationParser(description='Conversational venue guide with Skill-configured destinations')
    parser.add_argument('--backend', choices=tuple(sorted(GUIDE_PROFILE.allowed_backends)), default='simulator')
    parser.add_argument('--runtime-socket', type=Path)
    parser.add_argument('--input', choices=('text', 'events'), default='text', help='console text or wakeup/ASR NDJSON events')
    parser.add_argument('--skills-dir', type=Path, help='directory containing venue-guide and its reference data')
    parser.add_argument('--model', default=os.environ.get('ROBOT_AGENT_MODEL'))
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--thread-id', default='visitor')
    parser.add_argument('--prompt')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--max-turns', type=positive_integer, default=8)
    parser.add_argument('--task-timeout', type=positive_timeout, default=60.0)
    parser.add_argument('--wait-timeout', type=positive_timeout, default=5.0)
    parser.add_argument('--model-timeout', type=positive_timeout, default=30.0)
    args = parser.parse_args(argv)
    key = os.environ.get('OPENAI_API_KEY')
    if not args.model or not args.model.strip() or not key or not key.strip():
        raise AgentConfigurationError('A model name and OPENAI_API_KEY are required')
    if args.max_turns > 100 or not args.thread_id.strip() or len(args.thread_id) > 128:
        raise AgentConfigurationError('Provide max-turns from 1 to 100 and a session ID of at most 128 characters')
    if args.resume and args.prompt is not None:
        raise AgentConfigurationError('--resume cannot be combined with --prompt')
    if args.input == 'events' and (args.resume or args.prompt is not None):
        raise AgentConfigurationError('Event input cannot be combined with --prompt or --resume')
    if args.prompt is not None and (not args.prompt.strip() or len(args.prompt) > 4096):
        raise AgentConfigurationError('Prompt must contain 1 to 4096 characters')
    storage = resolve_storage(
        data_dir=args.data_dir if args.data_dir is not None else default_data_dir(),
        backend=args.backend, api_id=None, address=None, profile=GUIDE_PROFILE,
        runtime_socket=args.runtime_socket,
    )
    return GuideConfig(
        backend=args.backend, model=args.model, api_key=key,
        base_url=os.environ.get('OPENAI_BASE_URL') or None,
        api_id=None, address=None, api_token='', data_dir=storage.data_dir,
        thread_id=args.thread_id, prompt=args.prompt, resume=args.resume,
        max_turns=args.max_turns, wait_timeout_s=args.wait_timeout,
        task_timeout_s=args.task_timeout, model_timeout_s=args.model_timeout,
        allowed_skills=frozenset({'navigation.navigate_to', 'audio.speak'}),
        task_database=storage.task_database, checkpoint_database=storage.checkpoint_database,
        business_database=storage.business_database, runtime_id=storage.runtime_id,
        profile=GUIDE_PROFILE, runtime_socket=storage.runtime_socket,
        skills_dir=args.skills_dir.expanduser().resolve() if args.skills_dir is not None else None,
        input_mode=args.input,
    )


async def _run(config: GuideConfig) -> None:
    from guide_agent.application import run_application

    model = create_model(config)
    try:
        await run_application(config, model)
    finally:
        await close_model(model)


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(parse_args, lambda config: asyncio.run(_run(config)), argv)
