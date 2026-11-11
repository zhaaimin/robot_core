"""Describe the tool boundary the graph orchestrates without naming any adapter.

`ToolSet` is the only contract the decision loop needs: a schema list plus one
invoke call that returns an `Observation`. Robot and business adapters are
structural implementations of it, so adding a scene never changes the graph.
An `Observation` is a factual record of what a call produced; it is not a new
instruction and callers must not reinterpret `accepted` as completion.
"""

from collections.abc import AsyncGenerator
from typing import Any, NotRequired, Protocol, TypedDict

from robot_agent_runtime.models import ErrorInfo, Event, SkillDefinition, Task, TaskRequest, TaskState


class TaskRuntime(Protocol):
    """Use authoritative task snapshots and ordered events across transports.

    The application owns connection lifetime. A wait timeout or event stream
    cancellation releases observation only; stopping physical work requires
    cancel_task and a confirmed terminal result. Stable request keys survive
    retries, and transports must preserve original event sequence numbers.
    """

    async def list_skills(self) -> list[SkillDefinition]: ...

    async def submit_task(self, request: TaskRequest) -> Task: ...

    async def submit_and_wait(self, request: TaskRequest, *, timeout_s: float | None = None) -> Task: ...

    async def get_task_status(self, task_id: str) -> Task: ...

    async def cancel_task(self, task_id: str) -> Task: ...

    def subscribe_task_events(self, task_id: str, after_sequence: int = 0) -> AsyncGenerator[Event, None]: ...


class Observation(TypedDict):
    tool: str
    task_id: str | None
    state: str | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    requires_attention: bool
    execution: NotRequired[dict[str, str]]


class ToolSet(Protocol):
    """Keep graph orchestration independent of robot and business adapters."""

    @property
    def schemas(self) -> list[dict[str, Any]]: ...

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Observation: ...


def failed_observation(
    tool: str, error: ErrorInfo, *, task_id: str | None = None,
) -> Observation:
    return {
        "tool": tool, "task_id": task_id, "state": None, "result": None,
        "error": error.to_dict(), "requires_attention": True,
    }


def task_observation(tool: str, task: Task) -> Observation:
    return {
        "tool": tool, "task_id": task.id, "state": task.state.value,
        "result": task.result, "error": task.error.to_dict() if task.error else None,
        "requires_attention": task.state != TaskState.SUCCEEDED,
    }
