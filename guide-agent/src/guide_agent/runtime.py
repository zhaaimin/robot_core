"""Own a simulator or an MCP connection without importing another agent."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from guide_agent.profiles import GUIDE_PROFILE
from robot_agent_core.config import AppConfig
from robot_agent_core.configuration import validate_config
from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.ports import TaskRuntime
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.repositories.sqlite import SQLiteTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


@asynccontextmanager
async def open_runtime(config: AppConfig) -> AsyncIterator[TaskRuntime]:
    """Keep the task client alive until guide stop and monitor cleanup finish."""
    validate_config(config)
    if config.profile != GUIDE_PROFILE:
        raise AgentConfigurationError('Guide application requires its own storage profile')
    if config.backend == 'mcp':
        if config.runtime_socket is None:
            raise AgentConfigurationError('MCP backend requires a runtime socket')
        try:
            from robot_runtime_mcp.client import open_mcp_runtime
        except ImportError:
            raise AgentConfigurationError('MCP backend requires robot-agent-guide[mcp]') from None
        async with open_mcp_runtime(str(config.runtime_socket), allowed_skills=set(config.allowed_skills)) as runtime:
            missing = set(config.allowed_skills) - {definition.name for definition in await runtime.list_skills()}
            if missing:
                raise AgentConfigurationError('Guide runtime is missing required skills', details={'skills': sorted(missing)})
            yield runtime
        return
    if config.backend != 'simulator' or config.task_database is None:
        raise AgentConfigurationError('Simulator backend requires owned task storage')
    simulator = Simulator()
    runtime = RobotRuntime(
        SQLiteTaskRepository(str(config.task_database)),
        {name: simulator.skills[name] for name in config.allowed_skills},
        allowed_permissions={'navigation', 'audio'},
    )
    try:
        await runtime.start()
        yield runtime
    finally:
        await runtime.close()
