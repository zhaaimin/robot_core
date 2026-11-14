from typing import Any, ClassVar

from robot_agent_runtime.models import ErrorInfo


class AgentRuntimeError(RuntimeError):
    code: ClassVar[str] = "RUNTIME_ERROR"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.info = ErrorInfo(code=self.code, message=message, details=details or {})

    def to_dict(self) -> dict[str, Any]:
        return self.info.to_dict()


class InvalidRequest(AgentRuntimeError):
    code = "INVALID_REQUEST"


class SkillNotFound(AgentRuntimeError):
    code = "SKILL_NOT_FOUND"


class SchemaValidationError(AgentRuntimeError):
    code = "SCHEMA_VALIDATION_FAILED"


class PermissionDenied(AgentRuntimeError):
    code = "PERMISSION_DENIED"


class PreconditionFailed(AgentRuntimeError):
    code = "PRECONDITION_FAILED"


class TaskNotFound(AgentRuntimeError):
    code = "TASK_NOT_FOUND"


class EventStreamOverflow(AgentRuntimeError):
    code = "EVENT_STREAM_OVERFLOW"


class SkillTimeout(AgentRuntimeError):
    code = "SKILL_TIMEOUT"


class TaskTimeout(AgentRuntimeError):
    code = "TASK_TIMEOUT"


class TaskWaitTimeout(AgentRuntimeError):
    code = "TASK_WAIT_TIMEOUT"


class SnapshotTooLarge(AgentRuntimeError):
    code = "SNAPSHOT_TOO_LARGE"


class SkillExecutionError(AgentRuntimeError):
    code = "SKILL_EXECUTION_FAILED"


class InternalError(AgentRuntimeError):
    code = "INTERNAL_ERROR"


class TaskCancelled(AgentRuntimeError):
    code = "TASK_CANCELLED"


class ReconciliationRequired(AgentRuntimeError):
    code = "RECONCILIATION_REQUIRED"


class RobotConnectionError(AgentRuntimeError):
    code = "ROBOT_CONNECTION_ERROR"


class RobotRequestError(AgentRuntimeError):
    code = "ROBOT_REQUEST_FAILED"


class RobotResponseError(AgentRuntimeError):
    code = "ROBOT_RESPONSE_INVALID"


class RobotStopError(AgentRuntimeError):
    code = "ROBOT_STOP_UNCONFIRMED"


class TaskEventStreamEnded(AgentRuntimeError):
    code = "TASK_EVENT_STREAM_ENDED"


class ListenerCallbackError(AgentRuntimeError):
    code = "LISTENER_CALLBACK_FAILED"


class ListenerCallbackQueueFull(AgentRuntimeError):
    code = "LISTENER_CALLBACK_QUEUE_FULL"


class ListenerCallbackTimeout(AgentRuntimeError):
    code = "LISTENER_CALLBACK_TIMEOUT"


class ListenerCapacityExceeded(AgentRuntimeError):
    code = "LISTENER_CAPACITY_EXCEEDED"


class ExtensionCapacityExceeded(AgentRuntimeError):
    code = "EXTENSION_CAPACITY_EXCEEDED"


class ExtensionTimeout(AgentRuntimeError):
    code = "EXTENSION_TIMEOUT"


class ExecutionUnresponsive(AgentRuntimeError):
    code = "EXECUTION_UNRESPONSIVE"


class ExecutionUnsettled(ExecutionUnresponsive):
    """A failed call whose own record cannot establish that the hardware is at rest.

    Shares the unresponsive consequence — quarantine and require reconciliation —
    because a waiter that cannot prove stopping must not release physical
    ownership. The distinct code keeps the cause visible in events and errors.
    """

    code = "EXECUTION_UNSETTLED"


class ResourceQuarantined(AgentRuntimeError):
    code = "RESOURCE_QUARANTINED"
