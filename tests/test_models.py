import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from robot_agent_runtime.errors import (
    AgentRuntimeError,
    InvalidRequest,
    PermissionDenied,
    ReconciliationRequired,
    SchemaValidationError,
    SkillExecutionError,
    SkillNotFound,
    SkillTimeout,
    TaskCancelled,
    TaskNotFound,
)
from robot_agent_runtime.models import (
    ErrorInfo,
    Event,
    SkillCall,
    SkillContext,
    SkillDefinition,
    SkillResult,
    Task,
    TaskRequest,
    TaskState,
)


def test_task_request_rejects_empty_steps() -> None:
    with pytest.raises(ValidationError):
        TaskRequest(task_type="navigate", steps=[])


@pytest.mark.parametrize("timeout_s", [0, -0.1])
def test_skill_call_rejects_non_positive_timeout(timeout_s: float) -> None:
    with pytest.raises(ValidationError):
        SkillCall(skill="move", timeout_s=timeout_s)


@pytest.mark.parametrize("timeout_s", [0, -1])
def test_task_request_rejects_non_positive_timeout(timeout_s: float) -> None:
    with pytest.raises(ValidationError):
        TaskRequest(
            task_type="navigate",
            steps=[SkillCall(skill="move")],
            timeout_s=timeout_s,
        )


def test_task_state_terminal_states() -> None:
    terminal = {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELED,
        TaskState.UNKNOWN,
        TaskState.RECONCILIATION_REQUIRED,
    }

    assert {state for state in TaskState if state.is_terminal} == terminal
    assert not TaskState.RUNNING.is_terminal


def test_error_info_serializes_to_plain_dictionary() -> None:
    info = ErrorInfo(code="BAD_INPUT", message="invalid", details={"field": "steps"})

    assert info.to_dict() == {
        "code": "BAD_INPUT",
        "message": "invalid",
        "details": {"field": "steps"},
    }


@pytest.mark.parametrize(
    ("exception_type", "code"),
    [
        (InvalidRequest, "INVALID_REQUEST"),
        (SkillNotFound, "SKILL_NOT_FOUND"),
        (SchemaValidationError, "SCHEMA_VALIDATION_FAILED"),
        (PermissionDenied, "PERMISSION_DENIED"),
        (TaskNotFound, "TASK_NOT_FOUND"),
        (SkillTimeout, "SKILL_TIMEOUT"),
        (SkillExecutionError, "SKILL_EXECUTION_FAILED"),
        (TaskCancelled, "TASK_CANCELLED"),
        (ReconciliationRequired, "RECONCILIATION_REQUIRED"),
    ],
)
def test_runtime_errors_have_stable_serializable_codes(
    exception_type: type[AgentRuntimeError], code: str
) -> None:
    error = exception_type("something failed", details={"task_id": "task-1"})

    assert isinstance(error, RuntimeError)
    assert error.info.code == code
    assert error.to_dict() == {
        "code": code,
        "message": "something failed",
        "details": {"task_id": "task-1"},
    }


def test_runtime_error_details_default_to_empty_dictionary() -> None:
    assert InvalidRequest("bad request").to_dict() == {
        "code": "INVALID_REQUEST",
        "message": "bad request",
        "details": {},
    }


def test_skill_definition_defaults_and_json_serialization() -> None:
    definition = SkillDefinition(name="move", version="1.0.0", input_schema={})

    assert definition.resources == set()
    assert definition.permissions == set()
    assert definition.timeout_s == 30
    assert definition.supports_cancel is False
    assert json.loads(definition.model_dump_json()) == {
        "name": "move",
        "version": "1.0.0",
        "input_schema": {},
        "resources": [],
        "permissions": [],
        "timeout_s": 30.0,
        "supports_cancel": False,
    }


def test_skill_definition_accepts_resource_and_permission_lists() -> None:
    definition = SkillDefinition(
        name="move",
        version="1.0.0",
        input_schema={"type": "object"},
        resources=["base"],
        permissions=["motion"],
    )

    restored = SkillDefinition.model_validate_json(definition.model_dump_json())

    assert restored == definition
    assert definition.resources == {"base"}
    assert definition.permissions == {"motion"}


def test_task_request_json_round_trip() -> None:
    request = TaskRequest(
        task_type="navigate",
        steps=[SkillCall(skill="move", params={"speed": 0.2}, timeout_s=5)],
        priority=2,
        timeout_s=20,
        idempotency_key="request-1",
        metadata={"source": "sdk"},
    )

    restored = TaskRequest.model_validate_json(request.model_dump_json())

    assert restored == request


def test_task_request_defaults() -> None:
    request = TaskRequest(task_type="navigate", steps=[SkillCall(skill="move")])

    assert request.priority == 0
    assert request.timeout_s is None
    assert request.idempotency_key is None
    assert request.metadata == {}
    assert request.steps[0].params == {}


def test_task_and_event_use_utc_and_validate_required_fields() -> None:
    task = Task(id="task-1", task_type="navigate", state=TaskState.CREATED, steps=[SkillCall(skill="move")])
    event = Event(event_id="event-1", task_id=task.id, sequence=1, event_type="task.created")

    assert task.created_at.tzinfo == UTC
    assert task.updated_at.tzinfo == UTC
    assert task.current_step == 0
    assert task.result is None
    assert task.error is None
    assert task.cancel_requested is False
    assert event.timestamp.tzinfo == UTC
    assert event.data == {}

    with pytest.raises(ValidationError):
        Event(event_id="event-2", task_id=task.id, sequence=0, event_type="invalid")
    with pytest.raises(ValidationError):
        Task(
            id="task-2",
            task_type="navigate",
            state=TaskState.CREATED,
            steps=[SkillCall(skill="move")],
            created_at=datetime(2026, 1, 1),
        )


def test_skill_result_and_context_are_directly_usable() -> None:
    cancel_event = asyncio.Event()
    progress: list[float] = []
    context = SkillContext(
        task_id="task-1",
        execution_id="execution-1",
        cancel_event=cancel_event,
        progress=progress.append,
    )
    result = SkillResult(success=True, output={"distance_m": 1.0})

    assert context.progress is not None
    context.progress(0.5)
    context.cancel_event.set()
    assert progress == [0.5]
    assert cancel_event.is_set()
    assert result.error is None
    assert result.output == {"distance_m": 1.0}


def test_skill_result_can_carry_structured_error() -> None:
    error = ErrorInfo(code="HW_ERROR", message="unavailable")
    result = SkillResult(success=False, error=error)

    assert result.output == {}
    assert result.error == error


def test_models_are_json_serializable() -> None:
    task = Task(
        id="task-1",
        task_type="navigate",
        state=TaskState.CREATED,
        steps=[SkillCall(skill="move")],
    )
    event = Event(event_id="event-1", task_id=task.id, sequence=1, event_type="task.created")
    payloads: list[dict[str, Any]] = [
        json.loads(task.model_dump_json()),
        json.loads(event.model_dump_json()),
    ]

    assert payloads[0]["state"] == "created"
    assert payloads[1]["sequence"] == 1
