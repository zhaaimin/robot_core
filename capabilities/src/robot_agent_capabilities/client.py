import asyncio
import json
import math
import threading
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from typing import Any

from robot_agent_capabilities.catalog import CANCEL_TOOL, STATUS_TOOL, ToolDefinition, build_catalog
from robot_agent_capabilities.errors import (
    CapabilityConfigurationError, CapabilityNotAllowed, CapabilityTransportError,
)
from robot_agent_capabilities.observations import Observation, failed_observation, task_observation
from robot_agent_capabilities.ports import ListenerHandle, TaskRuntime
from robot_agent_capabilities.listeners import (
    CapabilityListeners, PlatformErrorCallback, PlatformEventCallback,
    TaskDoneCallback, TaskErrorCallback, TaskEventCallback,
)
from robot_agent_capabilities.events import CapabilityEvent, subscribe_events
from robot_agent_runtime.errors import AgentRuntimeError, InvalidRequest, TaskWaitTimeout
from robot_agent_runtime.models import SkillCall, SkillDefinition, Task, TaskRequest
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.sdk.listener_execution import ListenerExecution
from robot_agent_runtime.sdk.remote import RemoteTransportError


def _positive_timeout(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise CapabilityConfigurationError(f"{name} must be finite and positive")
    return float(value)


def _identity_json(value: Any, active: set[int]) -> Any:
    kind = type(value)
    parents = type.__getattribute__(kind, "__mro__")

    def derives(base: type[Any]) -> bool:
        return any(parent is base for parent in parents)

    if value is None or kind is bool:
        return value
    if derives(str):
        return str.__str__(value)
    if derives(int):
        return int.__int__(value)
    if derives(float):
        return float.__float__(value)
    if not any(derives(base) for base in (dict, list, tuple)):
        raise TypeError("Operation identity requires JSON values")
    identity = id(value)
    if identity in active:
        raise ValueError("Operation identity contains a reference cycle")
    active.add(identity)
    try:
        if derives(dict):
            copied: dict[str, Any] = {}
            for key, item in dict.items(value):
                name = _identity_json(key, active)
                if type(name) is not str:
                    raise TypeError("Operation identity requires string keys")
                copied[name] = _identity_json(item, active)
            return copied
        base = list if derives(list) else tuple
        return [_identity_json(item, active) for item in base.__iter__(value)]
    finally:
        active.remove(identity)


def _request_validator(request: TaskRequest) -> Callable[[Task], None]:
    try:
        steps = [{name: getattr(step, name) for name in SkillCall.model_fields} for step in request.steps]
        expected = json.dumps(_identity_json(steps, set()), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        expected = None
    operation_id = request.metadata['operation_id']

    def validate(task: Task) -> None:
        if expected is None:
            raise InvalidRequest("Operation identity could not be captured from non-JSON parameters",
                                 {"task_id": task.id, "operation_id": operation_id})
        actual = json.dumps([step.model_dump(mode='json') for step in task.steps], sort_keys=True, allow_nan=False)
        if actual != expected:
            raise InvalidRequest("Operation ID already belongs to a task with different parameters",
                                 {"task_id": task.id, "operation_id": operation_id})

    return validate


class RobotCapabilities:
    """Expose shared robot tools and authoritative task/platform observations.

    The application owns model, hardware and runtime lifecycle; this client
    owns only its callback resources. Background submission is the default;
    bounded waiting observes the same task and never
    cancels it. Stable caller IDs survive transport changes and retries. A tool
    error or lost reply does not establish that a physical action did not run.
    Operation identity uses a private input snapshot captured before dispatch;
    capture traverses JSON containers through built-in operations, never foreign
    copying or serialization hooks. Unrepresentable inputs still reach runtime
    validation; they cannot establish observation identity if a backend accepts
    them. This snapshot grants no execution eligibility or safety decision.
    """

    def __init__(
        self, runtime: TaskRuntime, definitions: list[SkillDefinition], *,
        allowed_skills: set[str] | None = None, wait_timeout_s: float = 5.0,
        task_timeout_s: float = 60.0, idempotency_prefix: str = "robot:",
        task_type: str = "robot_capability",
        listener_options: RuntimeOptions | None = None,
    ) -> None:
        if (not isinstance(idempotency_prefix, str) or not idempotency_prefix.strip()
                or not isinstance(task_type, str) or not task_type.strip()):
            raise CapabilityConfigurationError("Task type and operation namespace must be nonempty strings")
        if listener_options is not None and not isinstance(listener_options, RuntimeOptions):
            raise CapabilityConfigurationError("listener_options must be RuntimeOptions")
        self._runtime = runtime
        self._wait_timeout_s = _positive_timeout(wait_timeout_s, "wait_timeout_s")
        self._task_timeout_s = _positive_timeout(task_timeout_s, "task_timeout_s")
        self._prefix = idempotency_prefix
        self._task_type = task_type
        self._tools, self._skills = build_catalog(definitions, allowed_skills)
        self._listeners: CapabilityListeners | None = None
        self._listener_options = listener_options
        self._listener_lock = threading.Lock()

    def _listener_manager(self) -> CapabilityListeners:
        with self._listener_lock:
            if self._listeners is None:
                self._listeners = CapabilityListeners(self._runtime, options=self._listener_options)
            return self._listeners

    @classmethod
    async def create(
        cls, runtime: TaskRuntime, *, allowed_skills: set[str] | None = None,
        wait_timeout_s: float = 5.0, task_timeout_s: float = 60.0,
        idempotency_prefix: str = "robot:", task_type: str = "robot_capability",
        listener_options: RuntimeOptions | None = None,
    ) -> "RobotCapabilities":
        definitions = await runtime.list_skills()
        return cls(runtime, definitions, allowed_skills=allowed_skills, wait_timeout_s=wait_timeout_s,
                   task_timeout_s=task_timeout_s, idempotency_prefix=idempotency_prefix, task_type=task_type,
                   listener_options=listener_options)

    @property
    def tools(self) -> list[ToolDefinition]:
        return [tool.model_copy(deep=True) for tool in self._tools]

    def add_task_listener(
        self, *, on_event: TaskEventCallback | None = None, on_done: TaskDoneCallback | None = None,
        on_error: TaskErrorCallback | None = None, task_ids: set[str] | None = None,
        all_tasks: bool = False, event_types: set[str] | None = None,
        execution: ListenerExecution = "caller", group: str | None = None,
    ) -> ListenerHandle:
        """Observe own future invokes by default; explicit IDs replay known tasks."""
        return self._listener_manager().add_task(on_event=on_event, on_done=on_done, on_error=on_error,
                                                  task_ids=task_ids, all_tasks=all_tasks, event_types=event_types,
                                                  execution=execution, group=group)

    def add_platform_listener(
        self, *, on_event: PlatformEventCallback | None = None, on_error: PlatformErrorCallback | None = None,
        after_cursor: int = 0, event_types: set[str] | None = None,
        execution: ListenerExecution = "caller", group: str | None = None,
    ) -> ListenerHandle:
        """Observe device facts, reporting observer failures separately from faults."""
        return self._listener_manager().add_platform(on_event=on_event, on_error=on_error,
                                                      after_cursor=after_cursor, event_types=event_types,
                                                      execution=execution, group=group)

    async def close_listeners(self) -> None:
        """Release this client's observer resources without closing the runtime."""
        await self._listener_manager().close()

    async def subscribe_events(
        self, *, sources: set[str] | None = None, after_cursor: int = 0,
        event_types: set[str] | None = None, task_ids: set[str] | None = None,
        after_sequences: dict[str, int] | None = None, task_terminal_only: bool = False,
    ) -> AsyncGenerator[CapabilityEvent, None]:
        """Observe original platform/task facts with independent recovery positions.

        Defaults preserve platform-only observation. Known tasks replay history;
        all-task mode is live-only. The client owns no new queue, runtime or
        robot lifecycle; closing releases only this observation's iterators.
        """
        async with aclosing(subscribe_events(
            self._runtime, sources=sources, after_cursor=after_cursor,
            event_types=event_types, task_ids=task_ids, after_sequences=after_sequences,
            task_terminal_only=task_terminal_only,
        )) as stream:
            async for event in stream:
                yield event

    async def invoke(
        self, name: str, params: dict[str, Any], *, operation_id: str | None = None, wait: bool = False,
    ) -> Observation:
        request: TaskRequest | None = None
        validate: Callable[[Task], None] | None = None
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
            validate = _request_validator(request)
            if wait:
                wait_deadline = asyncio.get_running_loop().time() + self._wait_timeout_s
            task = (await self._runtime.submit_and_wait(request, timeout_s=self._wait_timeout_s) if wait
                    else await self._runtime.submit_task(request))
            return self._submission_observation(name, task, validate)
        except TaskWaitTimeout as error:
            task_id = error.info.details["task_id"]
            if self._listeners is not None and validate is not None:
                self._listeners.track(task_id, validate=validate)
            remaining = (wait_deadline - asyncio.get_running_loop().time()) if wait_deadline is not None else 0
            if remaining > 0 and validate is not None:
                try:
                    async with asyncio.timeout(remaining):
                        task = await self._runtime.get_task_status(task_id)
                    observed = self._submission_observation(name, task, validate)
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

    def _submission_observation(self, name: str, task: Task, validate: Callable[[Task], None]) -> Observation:
        observed = task_observation(name, task)
        try:
            validate(task)
        except InvalidRequest as error:
            observed['error'] = error.to_dict()
            observed['requires_attention'] = True
        else:
            if self._listeners is not None:
                self._listeners.track(task.id)
        return observed
