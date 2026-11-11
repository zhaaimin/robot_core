import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from robot_agent_core.errors import (
    AgentConfigurationError, AgentSkillScriptError, AgentSkillScriptTimeout, AgentToolNotAllowed,
)
from robot_agent_core.ports import Observation, ToolSet, failed_observation
from robot_agent_core.skillkit.catalog import SkillRegistry, valid_name, valid_resource_path
from robot_agent_core.validation import positive_timeout
from robot_agent_runtime.errors import AgentRuntimeError, InvalidRequest


@dataclass(frozen=True)
class ScriptContext:
    tools: ToolSet
    operation_id: str


@dataclass(frozen=True)
class ApprovedScript:
    skill: str
    path: str
    fingerprint: str
    handler: Callable[[dict[str, Any], ScriptContext], Awaitable[Observation]]


def script_path(path: object) -> bool:
    return (valid_resource_path(path) and isinstance(path, str)
            and path.startswith("scripts/") and path.endswith(".py"))


def valid_observation(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("tool", "task_id", "state", "result", "error", "requires_attention"):
        if key not in value:
            return False
    return (isinstance(value["tool"], str)
            and (value["task_id"] is None or isinstance(value["task_id"], str))
            and (value["state"] is None or isinstance(value["state"], str))
            and (value["result"] is None or isinstance(value["result"], dict))
            and (value["error"] is None or isinstance(value["error"], dict))
            and isinstance(value["requires_attention"], bool))


class SkillScriptRunner:
    """Execute only application-approved async handlers tied to published source.

    Resource reads grant no execution authority. The application explicitly binds
    trusted Python functions; this runner never imports a model-selected path or
    launches a shell. The timeout bounds cooperative async handlers, not arbitrary
    untrusted Python, and cannot establish that a physical side effect stopped.
    """

    def __init__(
        self, registry: SkillRegistry, tools: ToolSet, bindings: list[ApprovedScript], *,
        timeout_s: float = 5.0,
    ) -> None:
        self._registry = registry
        self._tools = tools
        self._timeout_s = positive_timeout(timeout_s, "script timeout_s")
        self._bindings: dict[tuple[str, str], ApprovedScript] = {}
        for binding in bindings:
            if (not isinstance(binding, ApprovedScript) or not valid_name(binding.skill)
                    or not script_path(binding.path)
                    or not isinstance(binding.fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", binding.fingerprint) is None
                    or not inspect.iscoroutinefunction(binding.handler)):
                raise AgentConfigurationError("Script bindings require an approved async Python entry point")
            key = (binding.skill, binding.path)
            if key in self._bindings:
                raise AgentConfigurationError("Script bindings must be unique")
            try:
                resource = registry.read_resource(binding.skill, binding.path)
            except AgentRuntimeError as error:
                raise AgentConfigurationError("Script binding is not in the configured skill inventory") from error
            if resource["fingerprint"] != binding.fingerprint:
                raise AgentConfigurationError("Script binding fingerprint does not match published source")
            self._bindings[key] = binding

    async def invoke(
        self, name: str, path: str, args: dict[str, Any], *, operation_id: str,
    ) -> Observation:
        execution: dict[str, str] | None = None
        try:
            if not isinstance(operation_id, str) or not operation_id or not isinstance(args, dict):
                raise InvalidRequest("A stable operation_id and object arguments are required")
            if not isinstance(name, str) or not script_path(path):
                raise AgentToolNotAllowed("Script is not an application-approved entry point")
            binding = self._bindings.get((name, path))
            if binding is None:
                raise AgentToolNotAllowed("Script is not an application-approved entry point")
            execution = {"skill": name, "path": path, "fingerprint": binding.fingerprint}
            resource = self._registry.read_resource(name, path)
            if resource["fingerprint"] != binding.fingerprint:
                raise AgentToolNotAllowed("Script source changed after application approval")
        except AgentRuntimeError as error:
            observed = failed_observation("run_skill_script", error.info)
            if execution is not None:
                observed["execution"] = execution
            return observed

        deadline = asyncio.timeout(self._timeout_s)
        try:
            async with deadline:
                observed = await binding.handler(args, ScriptContext(self._tools, operation_id))
            if not valid_observation(observed):
                raise AgentSkillScriptError("Skill script did not return a valid observation")
        except TimeoutError:
            error = (AgentSkillScriptTimeout(
                "Skill script exceeded its deadline; query business state before another action",
            ) if deadline.expired() else AgentSkillScriptError(
                "Skill script failed; query business state before another action",
            ))
            observed = failed_observation("run_skill_script", error.info)
        except Exception:
            error = AgentSkillScriptError(
                "Skill script failed; query business state before another action",
            )
            observed = failed_observation("run_skill_script", error.info)
        return {**observed, "tool": "run_skill_script", "execution": execution}
