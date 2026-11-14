from typing import Any, NotRequired, TypedDict

from robot_agent_runtime.models import ErrorInfo, Task, TaskState


class Observation(TypedDict):
    tool: str
    task_id: str | None
    state: str | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    requires_attention: bool
    execution: NotRequired[dict[str, str]]


def failed_observation(tool: str, error: ErrorInfo, *, task_id: str | None = None) -> Observation:
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
