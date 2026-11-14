from collections.abc import AsyncGenerator
from typing import Protocol

from robot_agent_runtime.models import Event, SkillDefinition, Task, TaskRequest


class TaskRuntime(Protocol):
    """Use authoritative tasks and ordered events independently of transport.

    The application owns the client's lifetime. A wait timeout or subscription
    cancellation releases observation only; physical stopping requires an
    explicit cancel_task and confirmed outcome. Implementations preserve stable
    submission keys and the runtime's original event sequence numbers.
    """

    async def list_skills(self) -> list[SkillDefinition]: ...

    async def submit_task(self, request: TaskRequest) -> Task: ...

    async def submit_and_wait(self, request: TaskRequest, *, timeout_s: float | None = None) -> Task: ...

    async def get_task_status(self, task_id: str) -> Task: ...

    async def cancel_task(self, task_id: str) -> Task: ...

    def subscribe_task_events(self, task_id: str, after_sequence: int = 0) -> AsyncGenerator[Event, None]: ...
