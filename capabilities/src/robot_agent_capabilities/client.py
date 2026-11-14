import asyncio
import json
import math
from typing import Any

from robot_agent_capabilities.catalog import CANCEL_TOOL, STATUS_TOOL, ToolDefinition, build_catalog
from robot_agent_capabilities.errors import (
    CapabilityConfigurationError, CapabilityNotAllowed, CapabilityTransportError,
)
from robot_agent_capabilities.observations import Observation, failed_observation, task_observation
from robot_agent_capabilities.ports import TaskRuntime
from robot_agent_runtime.errors import AgentRuntimeError, InvalidRequest, TaskWaitTimeout
from robot_agent_runtime.models import SkillCall, SkillDefinition, Task, TaskRequest
from robot_agent_runtime.sdk.remote import RemoteTransportError


def _positive_timeout(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise CapabilityConfigurationError(f"{name} must be finite and positive")
    return float(value)


class RobotCapabilities:
    """Translate a shared tool catalog into authoritative runtime task calls.

    This client owns no model, loop, hardware or runtime lifecycle. Background
    submission is the default; bounded waiting observes the same task and never
    cancels it. Stable caller IDs survive transport changes and retries. A tool
    error or lost reply does not establish that a physical action did not run.
    """

    def __init__(
        self, runtime: TaskRuntime, definitions: list[SkillDefinition], *,
        allowed_skills: set[str] | None = None, wait_timeout_s: float = 5.0,
        task_timeout_s: float = 60.0, idempotency_prefix: str = "robot:",
        task_type: str = "robot_capability",
    ) -> None:
        if (not isinstance(idempotency_prefix, str) or not idempotency_prefix.strip()
                or not isinstance(task_type, str) or not task_type.strip()):
            raise CapabilityConfigurationError("Task type and operation namespace must be nonempty strings")
        self._runtime = runtime
        self._wait_timeout_s = _positive_timeout(wait_timeout_s, "wait_timeout_s")
        self._task_timeout_s = _positive_timeout(task_timeout_s, "task_timeout_s")
        self._prefix = idempotency_prefix
        self._task_type = task_type
        self._tools, self._skills = build_catalog(definitions, allowed_skills)

    @classmethod
    async def create(
        cls, runtime: TaskRuntime, *, allowed_skills: set[str] | None = None,
        wait_timeout_s: float = 5.0, task_timeout_s: float = 60.0,
        idempotency_prefix: str = "robot:", task_type: str = "robot_capability",
    ) -> "RobotCapabilities":
        definitions = await runtime.list_skills()
        return cls(runtime, definitions, allowed_skills=allowed_skills, wait_timeout_s=wait_timeout_s,
                   task_timeout_s=task_timeout_s, idempotency_prefix=idempotency_prefix, task_type=task_type)

    @property
    def tools(self) -> list[ToolDefinition]:
        return [tool.model_copy(deep=True) for tool in self._tools]

    async def invoke(
        self, name: str, params: dict[str, Any], *, operation_id: str | None = None, wait: bool = False,
    ) -> Observation:
        request: TaskRequest | None = None
        wait_deadline: float | None = None
        try:
            if not isinstance(params, dict) or type(wait) is not bool:
                raise InvalidRequest("Tool parameters must be an object and wait must be boolean")
            if name in {STATUS_TOOL, CANCEL_TOOL}:
                if (set(params) != {"task_id"} or not isinstance(params["task_id"], str)
                        or not params["task_id"].strip()):
                    raise InvalidRequest("Provide exactly one nonempty string task_id")
                task = (await self._runtime.get_task_status(params["task_id"]) if name == STATUS_TOOL
                        else await self._runtime.cancel_task(params["task_id"]))
                return task_observation(name, task)
            definition = self._skills.get(name)
            if definition is None:
                raise CapabilityNotAllowed("Tool is not in the capability allowlist", {"tool": name})
            if not isinstance(operation_id, str) or not operation_id.strip():
                raise InvalidRequest("A stable nonempty operation_id is required")
            request = TaskRequest(
                task_type=self._task_type, steps=[SkillCall(skill=definition.name, params=params)],
                timeout_s=self._task_timeout_s, idempotency_key=self._prefix + operation_id,
                metadata={"operation_id": operation_id, "tool": name},
            )
            if wait:
                wait_deadline = asyncio.get_running_loop().time() + self._wait_timeout_s
            task = (await self._runtime.submit_and_wait(request, timeout_s=self._wait_timeout_s) if wait
                    else await self._runtime.submit_task(request))
            return self._submission_observation(name, task, request)
        except TaskWaitTimeout as error:
            task_id = error.info.details["task_id"]
            remaining = (wait_deadline - asyncio.get_running_loop().time()) if wait_deadline is not None else 0
            if remaining > 0 and request is not None:
                try:
                    async with asyncio.timeout(remaining):
                        task = await self._runtime.get_task_status(task_id)
                    observed = self._submission_observation(name, task, request)
                    if observed['error'] is None:
                        observed['error'] = error.to_dict()
                    observed['requires_attention'] = True
                    return observed
                except (TimeoutError, AgentRuntimeError, RemoteTransportError):
                    pass
            return failed_observation(name, error.info, task_id=task_id)
        except AgentRuntimeError as error:
            return failed_observation(name, error.info)
        except RemoteTransportError:
            error = CapabilityTransportError(
                "Transport failed; query or resubmit the saved operation before issuing a new action",
                {"operation_id": operation_id, "outcome_uncertain": True},
            )
            return failed_observation(name, error.info)

    @staticmethod
    def _submission_observation(name: str, task: Task, request: TaskRequest) -> Observation:
        observed = task_observation(name, task)
        actual = json.dumps([step.model_dump(mode='json') for step in task.steps], sort_keys=True, allow_nan=False)
        expected = json.dumps([step.model_dump(mode='json') for step in request.steps], sort_keys=True, allow_nan=False)
        if actual != expected:
            error = InvalidRequest("Operation ID already belongs to a task with different parameters",
                                   {"task_id": task.id, "operation_id": request.metadata['operation_id']})
            observed['error'] = error.to_dict()
            observed['requires_attention'] = True
        return observed
