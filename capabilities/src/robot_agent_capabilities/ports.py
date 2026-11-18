from collections.abc import AsyncGenerator, AsyncIterator
from typing import Protocol

from robot_agent_runtime.models import Event, PlatformEvent, SkillDefinition, Task, TaskRequest


class ListenerHandle(Protocol):
    """Own observation only; completion never establishes robot action success.

    Task wait joins the tasks already registered when called. Continuous
    platform/all-task wait joins the observer itself. close is idempotent and
    does not own the shared runtime or cancel robot work.
    """

    @property
    def closed(self) -> bool: ...

    async def wait(self) -> None: ...

    async def close(self) -> None: ...


class PlatformEventRuntime(Protocol):
    """Optional observation independent of task support and client lifecycle.

    The backend owns journal cursors, filtering and replay. Ending observation
    releases only this iterator, never the runtime or physical work. Consumers
    retain their processed cursor; delivery alone does not acknowledge it.
    """

    def subscribe_platform_events(
        self, *, after_cursor: int = 0, event_types: set[str] | None = None,
    ) -> AsyncIterator[PlatformEvent]: ...


class TaskEventRuntime(Protocol):
    """Optional live all-task observation without a cross-task replay cursor.

    Known task histories use TaskRuntime.subscribe_task_events instead. This
    optional port never implies that events preceding registration can replay.
    """

    def subscribe_all_task_events(
        self, event_types: set[str] | None = None,
    ) -> AsyncIterator[Event]: ...


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
