from collections.abc import AsyncIterator, Mapping

from robot_agent_runtime.core import RuntimeCore
from robot_agent_runtime.models import Event, SkillDefinition, Task, TaskRequest
from robot_agent_runtime.ports import (
    Reconciler,
    RobotStateProvider,
    Skill,
    TaskRepository,
)


class RobotRuntime:
    def __init__(
        self,
        repository: TaskRepository,
        skills: Mapping[str, Skill],
        reconciler: Reconciler | None = None,
        allowed_permissions: set[str] | None = None,
        robot_state: RobotStateProvider | None = None,
    ) -> None:
        self._core = RuntimeCore(
            repository, skills, reconciler, allowed_permissions, robot_state
        )

    async def start(self) -> None:
        await self._core.start()

    async def close(self) -> None:
        await self._core.close()

    async def submit_task(self, request: TaskRequest) -> Task:
        return await self._core.submit_task(request)

    async def get_task_status(self, task_id: str) -> Task:
        return await self._core.get_task_status(task_id)

    async def cancel_task(self, task_id: str) -> Task:
        return await self._core.cancel_task(task_id)

    async def list_skills(self) -> list[SkillDefinition]:
        return await self._core.list_skills()

    async def subscribe_task_events(
        self, task_id: str, after_sequence: int = 0
    ) -> AsyncIterator[Event]:
        async for event in self._core.subscribe_task_events(task_id, after_sequence):
            yield event
