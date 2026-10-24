from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from robot_agent_runtime.models import ErrorInfo, Task, TaskState
from robot_agent_runtime.ports import Reconciler, TaskRepository


_AUTHORITATIVE_STATES = {
    TaskState.SUCCEEDED,
    TaskState.FAILED,
    TaskState.CANCELED,
    TaskState.UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    event_type: str
    changes: dict[str, Any]
    data: dict[str, Any]


async def recover_nonterminal_tasks(
    repository: TaskRepository,
    reconciler: Reconciler | None,
    apply_outcome: Callable[[Task, RecoveryOutcome], Awaitable[None]],
) -> None:
    for task in await repository.get_nonterminal_tasks():
        outcome = await _reconcile(task, reconciler)
        await apply_outcome(task, outcome)


async def _reconcile(
    task: Task, reconciler: Reconciler | None
) -> RecoveryOutcome:
    if reconciler is None:
        return _recovery_required("no_reconciler")
    try:
        state = await reconciler.reconcile(task)
    except Exception as error:
        return _recovery_required(
            "reconciler_exception", exception_type=type(error).__name__
        )
    if not isinstance(state, TaskState) or state not in _AUTHORITATIVE_STATES:
        return _recovery_required("indeterminate")
    return _reconciled(state)


def _reconciled(state: TaskState) -> RecoveryOutcome:
    details = {
        "authoritative": True,
        "reconciled": True,
        "authoritative_state": state.value,
    }
    error: ErrorInfo | None = None
    if state != TaskState.SUCCEEDED:
        error = ErrorInfo(
            code="TASK_RECONCILED",
            message=f"Task reconciled to authoritative {state.value} state",
            details=details,
        )
    changes: dict[str, Any] = {"state": state, "result": None, "error": error}
    data: dict[str, Any] = {**details}
    if error is not None:
        data["error"] = error.to_dict()
    return RecoveryOutcome("TASK_RECONCILED", changes, data)


def _recovery_required(reason: str, **details: Any) -> RecoveryOutcome:
    error = ErrorInfo(
        code="RECONCILIATION_REQUIRED",
        message="Task requires reconciliation after runtime restart",
        details={"reason": reason, "reconciled": False, **details},
    )
    return RecoveryOutcome(
        "TASK_RECOVERY_REQUIRED",
        {
            "state": TaskState.RECONCILIATION_REQUIRED,
            "result": None,
            "error": error,
        },
        {"reconciled": False, "error": error.to_dict()},
    )
