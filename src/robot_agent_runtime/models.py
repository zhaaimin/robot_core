import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, Field


class TaskState(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    CANCELING = "canceling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELED,
            TaskState.UNKNOWN,
            TaskState.RECONCILIATION_REQUIRED,
        }


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class SkillCall(BaseModel):
    skill: str
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float | None = Field(default=None, gt=0)


class TaskRequest(BaseModel):
    task_type: str
    steps: list[SkillCall] = Field(min_length=1)
    priority: int = 0
    timeout_s: float | None = Field(default=None, gt=0)
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillDefinition(BaseModel):
    name: str
    version: str
    description: str = ""
    input_schema: dict[str, Any]
    resources: set[str] = Field(default_factory=set)
    permissions: set[str] = Field(default_factory=set)
    preconditions: set[str] = Field(default_factory=set)
    timeout_s: float = Field(default=30, gt=0)
    supports_cancel: bool = False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware UTC")
    return value.astimezone(UTC)


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]


class Task(BaseModel):
    id: str
    task_type: str
    state: TaskState
    steps: list[SkillCall] = Field(min_length=1)
    current_step: int = Field(default=0, ge=0)
    priority: int = 0
    timeout_s: float | None = Field(default=None, gt=0)
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: ErrorInfo | None = None
    cancel_requested: bool = False
    created_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))


class Event(BaseModel):
    event_id: str
    task_id: str
    sequence: int = Field(gt=0)
    event_type: str
    timestamp: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = Field(default_factory=dict)


class PlatformEvent(BaseModel):
    """Opaque platform facts, independent of tasks and their sequence counters.

    Sources submit cursor zero; the event journal assigns the authoritative
    resume position. A retained event ID identifies the same immutable fact.
    Runtime code never interprets vendor payloads or infers physical safety.
    """

    event_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, pattern=r"\S")
    source: str = Field(min_length=1, pattern=r"\S")
    event_type: str = Field(min_length=1, pattern=r"\S")
    timestamp: UTCDateTime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    cursor: int = Field(default=0, ge=0, strict=True)


class ActionOutcome(BaseModel):
    """What one call established about the hardware, apart from its goal.

    A failed call may have moved an arm; a successful call may still be moving.
    Only these facts let the kernel decide whether the declared resources may be
    handed to other work, so ``SkillResult.success`` cannot answer them.

    ``stopped`` is deliberately tri-state: ``True`` only when the adapter
    confirmed the actuators are at rest, ``False`` when it knows they are not,
    and ``None`` when it cannot tell. ``operator_required`` keeps a recovery
    that needs a human out of every automatic path.

    ``SkillResult.action`` being ``None`` means the adapter does not report this
    yet; the kernel then keeps its previous behaviour instead of assuming rest.
    """

    executed: Literal["none", "some", "unknown"] = "unknown"
    stopped: bool | None = None
    operator_required: bool = False

    @property
    def at_rest(self) -> bool:
        """Whether this record establishes that nothing is still moving.

        A recovery that needs a person is never at rest, even when the call
        itself executed nothing: handing the hardware to the next task would
        skip the recovery the environment asked for.
        """
        return not self.operator_required and (
            self.executed == "none" or self.stopped is True
        )


class SkillResult(BaseModel):
    success: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: ErrorInfo | None = None
    action: ActionOutcome | None = None


@dataclass(slots=True)
class SkillContext:
    task_id: str
    execution_id: str
    cancel_event: asyncio.Event
    progress: Callable[[float], None] | None = None
