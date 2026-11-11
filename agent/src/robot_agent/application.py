"""Compose robot SDK capabilities without depending on the command line."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from robot_agent_core.config import AppConfig
from robot_agent_core.configuration import validate_config
from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.graph import AgentContext, build_graph
from robot_agent_core.models import DecisionModel
from robot_agent_core.shell import error_state, invoke_turn, is_skill_command, result_envelope, run_shell
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent.profiles import ROBOT_PROFILE
from robot_agent.robot_tools import RobotTools
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import AgentRuntimeError
from robot_agent_runtime.repositories.sqlite import SQLiteTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


def _require_own_profile(config: AppConfig) -> None:
    """Refuse a snapshot assembled by a different agent before any storage opens.

    Validation alone cannot tell the two agents apart: a hotel snapshot is
    internally consistent with the hotel profile it carries. The name is the
    only thing that says which durable histories belong to this application, so
    it is checked here rather than trusted from the command line.
    """
    validate_config(config)
    if config.profile.name != ROBOT_PROFILE.name:
        raise AgentConfigurationError(
            'Robot application requires the robot storage profile',
            details={'profile': config.profile.name},
        )


@asynccontextmanager
async def open_runtime(config: AppConfig, api: Any = None) -> AsyncIterator[RobotRuntime]:
    _require_own_profile(config)
    platform: Any = None
    runtime: RobotRuntime | None = None
    repository = SQLiteTaskRepository(str(config.task_database))
    try:
        if config.backend == 'cc':
            if api is None:
                raise AgentConfigurationError('CC runtime requires an owned native connection')
            from robot_agent_runtime.adapters.cc_api import create_cc_api_platform

            platform = create_cc_api_platform(api, skill_thread_group='audio')
            runtime = RobotRuntime.from_platform(repository, platform, allowed_permissions={'audio'})
            await platform.start()
        else:
            simulator = Simulator()
            runtime = RobotRuntime(repository, simulator.skills, allowed_permissions={'audio', 'navigation'})
        await runtime.start()
        yield runtime
    finally:
        try:
            if runtime is not None:
                await runtime.close()
        finally:
            if platform is not None:
                await platform.close()


async def run_robot_application(
    config: AppConfig, model: DecisionModel, api: Any = None,
) -> None:
    """Run the decision loop against the robot tools this profile selects.

    Agent Skills are opt-in: without a directory or an explicit selection the
    graph runs with the plain robot tools, and this application ships no skill
    content of its own.
    """
    _require_own_profile(config)
    registry: SkillRegistry | None = None
    if config.skills_dir is not None or config.enabled_skills is not None:
        registry = (SkillRegistry.from_directory(config.skills_dir)
                    if config.skills_dir is not None else SkillRegistry.bundled('robot_agent'))
        if config.enabled_skills is not None:
            registry = registry.select(config.enabled_skills)
        if not registry.catalog():
            raise AgentConfigurationError('Configured Skill directory must contain a nonempty catalog')
    config.data_dir.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(config.checkpoint_database)) as saver:
        graph = build_graph(checkpointer=saver)
        async with open_runtime(config, api) as runtime:
            tools = await RobotTools.create(
                runtime, allowed_skills=set(config.allowed_skills),
                wait_timeout_s=config.wait_timeout_s, task_timeout_s=config.task_timeout_s,
            )
            context = AgentContext(model=model, tools=tools, max_turns=config.max_turns, skills=registry)
            catalog = registry.catalog() if registry is not None else None

            async def turn(prompt: str | None) -> dict[str, Any]:
                return await invoke_turn(
                    graph, context, thread_id=config.thread_id, runtime_id=config.runtime_id, prompt=prompt,
                )

            async def render(state: dict[str, Any]) -> dict[str, Any]:
                return result_envelope(config, state, catalog=catalog)

            async def recover(prompt: str, error: AgentRuntimeError) -> dict[str, Any]:
                if not is_skill_command(prompt):
                    raise error
                checkpoint = await graph.aget_state({'configurable': {'thread_id': config.thread_id}})
                return error_state(error, checkpoint.values)

            await run_shell(config, turn=turn, render=render, prompt=lambda: 'robot-agent> ', recover=recover)
