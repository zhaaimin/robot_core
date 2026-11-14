"""Expose allowlisted runtime skills as model tools over the public SDK.

This is the robot-scene implementation of `robot_agent_core.ports.ToolSet`. The
application owns the runtime client and its lifecycle; the tool set only
translates calls, so no execution policy moves into the agent.
"""

import copy
import re
from typing import Any

from robot_agent_core.errors import AgentConfigurationError, AgentToolNotAllowed, AgentTransportError
from robot_agent_core.ports import Observation, failed_observation, task_observation
from robot_agent_core.validation import positive_timeout
from robot_agent_runtime.errors import AgentRuntimeError, InvalidRequest, TaskWaitTimeout
from robot_agent_runtime.models import SkillCall, SkillDefinition, TaskRequest
from robot_agent_runtime.schemas import describe_bounds
from robot_agent_runtime.sdk import RemoteRuntime, RemoteTransportError, RobotRuntime


_NAMES = {
    "audio.speak": ("robot_speak", "让机器人播报 message，成功表示播报技能已完成。"),
    "navigation.navigate_to": ("robot_navigate_to", "让机器人导航到 target，返回真实任务状态。"),
    "robot.get_state": ("robot_get_state", "查询机器人状态，返回技能提供的状态快照。"),
}
_STATUS = "robot_task_status"
_CANCEL = "robot_cancel_task"


class RobotTools:
    """Translate public SDK calls without moving execution policy into the agent.

    Each action gets its stable operation ID from the preceding graph node.
    Replaying an execution node therefore reuses the runtime task. A local wait
    deadline returns the task for observation; it never implies physical stop.
    The application owns this client and its lifecycle, including pending tasks.
    """

    def __init__(
        self, runtime: RobotRuntime | RemoteRuntime, definitions: list[SkillDefinition],
        *, wait_timeout_s: float, task_timeout_s: float,
    ) -> None:
        self._runtime = runtime
        self._wait_timeout_s = positive_timeout(wait_timeout_s, "wait_timeout_s")
        self._task_timeout_s = positive_timeout(task_timeout_s, "task_timeout_s")
        self._skills: dict[str, SkillDefinition] = {}
        self._schemas: list[dict[str, Any]] = []
        for definition in definitions:
            name, fallback = _NAMES.get(definition.name, (
                "robot_" + re.sub(r"[^A-Za-z0-9_-]", "_", definition.name),
                f"执行机器人技能 {definition.name}，返回 task_id、state、result 和 error。",
            ))
            description = definition.description or fallback
            if name in self._skills or name in {_STATUS, _CANCEL} or len(name) > 64:
                raise AgentConfigurationError("Skill tool names collide or exceed 64 characters")
            self._skills[name] = definition
            self._schemas.append({"type": "function", "function": {
                "name": name, "description": description,
                "parameters": describe_bounds(definition.input_schema),
            }})
        for name, description in (
            (_STATUS, "按 task_id 查询原任务；执行中不等于完成。"),
            (_CANCEL, "按 task_id 显式请求取消；仅根据返回状态判断是否确认停止。"),
        ):
            self._schemas.append({"type": "function", "function": {
                "name": name, "description": description,
                "parameters": {
                    "type": "object", "properties": {"task_id": {"type": "string", "minLength": 1}},
                    "required": ["task_id"], "additionalProperties": False,
                },
            }})

    @classmethod
    async def create(
        cls, runtime: RobotRuntime | RemoteRuntime, *, allowed_skills: set[str] | None = None,
        wait_timeout_s: float = 5.0, task_timeout_s: float = 60.0,
    ) -> "RobotTools":
        definitions = await runtime.list_skills()
        if allowed_skills is not None:
            missing = allowed_skills - {definition.name for definition in definitions}
            if missing:
                raise AgentConfigurationError("Unknown allowed skills", details={"skills": sorted(missing)})
            definitions = [definition for definition in definitions if definition.name in allowed_skills]
        return cls(runtime, definitions, wait_timeout_s=wait_timeout_s, task_timeout_s=task_timeout_s)

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._schemas)

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Observation:
        try:
            if not operation_id or not isinstance(args, dict):
                raise InvalidRequest("A stable operation_id and object arguments are required")
            if name in {_STATUS, _CANCEL}:
                if set(args) != {"task_id"} or not isinstance(args["task_id"], str) or not args["task_id"]:
                    raise InvalidRequest("Provide exactly one nonempty string task_id")
                task = (await self._runtime.get_task_status(args["task_id"]) if name == _STATUS
                        else await self._runtime.cancel_task(args["task_id"]))
                return task_observation(name, task)
            definition = self._skills.get(name)
            if definition is None:
                raise AgentToolNotAllowed("Tool is not in the application allowlist", details={"tool": name})
            request = TaskRequest(
                task_type="agent_action", steps=[SkillCall(skill=definition.name, params=args)],
                timeout_s=self._task_timeout_s, idempotency_key=f"agent:{operation_id}",
                metadata={"operation_id": operation_id, "tool": name},
            )
            task = await self._runtime.submit_and_wait(request, timeout_s=self._wait_timeout_s)
            return task_observation(name, task)
        except TaskWaitTimeout as error:
            task_id = error.info.details["task_id"]
            try:
                task = await self._runtime.get_task_status(task_id)
            except (AgentRuntimeError, RemoteTransportError):
                return failed_observation(name, error.info, task_id=task_id)
            observed = task_observation(name, task)
            observed["error"] = error.to_dict()
            observed["requires_attention"] = True
            return observed
        except AgentRuntimeError as error:
            return failed_observation(name, error.info)
        except RemoteTransportError:
            error = AgentTransportError(
                "Transport failed; query or resubmit the saved operation before issuing a new action",
                details={"operation_id": operation_id},
            )
            return failed_observation(name, error.info)
