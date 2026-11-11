"""Connect hotel task coordination to an independently owned runtime."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.ports import TaskRuntime


@asynccontextmanager
async def open_runtime(socket_path: Path, *, allowed_skills: set[str]) -> AsyncIterator[TaskRuntime]:
    """Keep the MCP subprocess alive through workflow stop and reconciliation.

    Only the bridge belongs to this context; the runtime daemon remains owned
    by its application. Capabilities are checked before the workflow opens its
    business storage, so an incompatible endpoint cannot accept an escort.
    """
    try:
        from robot_runtime_mcp.client import open_mcp_runtime
    except ImportError:
        raise AgentConfigurationError('MCP backend requires robot-agent-hotel[mcp]') from None

    async with open_mcp_runtime(str(socket_path), allowed_skills=allowed_skills) as runtime:
        available = {definition.name for definition in await runtime.list_skills()}
        missing = {'navigation.navigate_to', 'audio.speak'} - available
        if missing:
            raise AgentConfigurationError('Hotel MCP runtime is missing required skills',
                                          details={'skills': sorted(missing)})
        yield runtime
