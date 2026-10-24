from typing import Any, Protocol

from robot_agent_runtime.models import (
    Event,
    SkillContext,
    SkillDefinition,
    SkillResult,
    Task,
    TaskState,
)


class Skill(Protocol):
    @property
    def definition(self) -> SkillDefinition: ...

    async def execute(
        self, context: SkillContext, params: dict[str, Any]
    ) -> SkillResult: ...


class TaskRepository(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def create_task(self, task: Task) -> Task: ...

    async def get_task(self, task_id: str) -> Task | None: ...

    async def update_task(self, task: Task) -> Task: ...

    async def append_event(self, event: Event) -> Event: ...

    async def apply_transition(self, task: Task, event: Event) -> tuple[Task, Event]: ...

    async def list_events(
        self, task_id: str, after_sequence: int = 0
    ) -> list[Event]: ...

    async def get_nonterminal_tasks(self) -> list[Task]: ...

    async def find_by_idempotency_key(self, key: str) -> Task | None: ...


class Reconciler(Protocol):
    async def reconcile(self, task: Task) -> TaskState | None: ...
