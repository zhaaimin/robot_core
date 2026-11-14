import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

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


class SkillResult(BaseModel):
    success: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: ErrorInfo | None = None


@dataclass(slots=True)
class SkillContext:
    task_id: str
    execution_id: str
    cancel_event: asyncio.Event
    progress: Callable[[float], None] | None = None
