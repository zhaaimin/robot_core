"""Adapt shared robot capabilities to the built-in agent's tool contract."""

from typing import Any

from robot_agent_capabilities import RobotCapabilities
from robot_agent_capabilities.errors import (
    CapabilityConfigurationError, CapabilityNotAllowed, CapabilityTransportError,
)
from robot_agent_capabilities.observations import Observation
from robot_agent_capabilities.ports import TaskRuntime
from robot_agent_core.errors import AgentConfigurationError, AgentToolNotAllowed, AgentTransportError
from robot_agent_runtime.models import SkillDefinition


_APPLICATION_ERRORS = {
    CapabilityNotAllowed.code: AgentToolNotAllowed,
    CapabilityTransportError.code: AgentTransportError,
}


class RobotTools:
    """Keep graph formatting and historical operation identity in the application.

    The shared client owns discovery, submission and factual results. Keeping
    the agent: namespace and existing error contract means an old pending graph
    still addresses its original physical task after this extraction.
    """

    def __init__(
        self, runtime: TaskRuntime, definitions: list[SkillDefinition],
        *, wait_timeout_s: float, task_timeout_s: float, allowed_skills: set[str] | None = None,
    ) -> None:
        try:
            self._capabilities = RobotCapabilities(
                runtime, definitions, allowed_skills=allowed_skills, wait_timeout_s=wait_timeout_s,
                task_timeout_s=task_timeout_s, idempotency_prefix="agent:", task_type="agent_action",
            )
        except CapabilityConfigurationError as error:
            raise AgentConfigurationError(error.info.message, error.info.details) from error

    @classmethod
    async def create(
        cls, runtime: TaskRuntime, *, allowed_skills: set[str] | None = None,
        wait_timeout_s: float = 5.0, task_timeout_s: float = 60.0,
    ) -> "RobotTools":
        return cls(runtime, await runtime.list_skills(), allowed_skills=allowed_skills,
                   wait_timeout_s=wait_timeout_s, task_timeout_s=task_timeout_s)

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {
            "name": tool.name, "description": tool.description, "parameters": tool.input_schema,
        }} for tool in self._capabilities.tools]

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Observation:
        observed = await self._capabilities.invoke(name, args, operation_id=operation_id, wait=True)
        error = observed['error']
        if error is not None and error['code'] in _APPLICATION_ERRORS:
            observed['error'] = _APPLICATION_ERRORS[error['code']](error['message'], error['details']).to_dict()
        return observed
