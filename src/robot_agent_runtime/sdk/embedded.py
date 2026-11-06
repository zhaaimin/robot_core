from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from typing import Self

from robot_agent_runtime.kernel.core import RuntimeCore
from robot_agent_runtime.errors import InvalidRequest
from robot_agent_runtime.hosting.kernel_host import KernelHost
from robot_agent_runtime.models import Event, SkillDefinition, Task, TaskRequest
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.ports import (
    Reconciler,
    RobotPlatform,
    RobotStateProvider,
    Skill,
    TaskRepository,
)
from robot_agent_runtime.sdk.listeners import (
    DoneCallback,
    ErrorCallback,
    EventCallback,
    TaskListener,
    TaskListeners,
)
from robot_agent_runtime.sdk.listener_execution import ListenerExecution, on_callback_owner
from robot_agent_runtime.thread_binding import ThreadBinding


class RobotRuntime:
    def __init__(
        self,
        repository: TaskRepository | ThreadBinding[TaskRepository],
        skills: Mapping[str, Skill | ThreadBinding[Skill]],
        reconciler: Reconciler | ThreadBinding[Reconciler] | None = None,
        allowed_permissions: set[str] | None = None,
        robot_state: RobotStateProvider | ThreadBinding[RobotStateProvider] | None = None,
        *,
        options: RuntimeOptions | None = None,
    ) -> None:
        self._options = options or RuntimeOptions()
        self._host = KernelHost(repository, skills, reconciler, allowed_permissions, robot_state,
                                options=self._options)
        self._listeners = TaskListeners(
            self.subscribe_task_events, self.get_task_status,
            cancel_grace_s=self._options.cancel_grace_s,
            options=self._options,
        )
        self._closing = False

    @classmethod
    def from_platform(
        cls,
        repository: TaskRepository | ThreadBinding[TaskRepository],
        platform: RobotPlatform,
        *,
        allowed_permissions: set[str] | None = None,
        options: RuntimeOptions | None = None,
    ) -> Self:
        """Preserve platform port bindings; the application owns its lifecycle."""
        return cls(
            repository, platform.skills, reconciler=platform.reconciler,
            allowed_permissions=allowed_permissions, robot_state=platform.robot_state,
            options=options,
        )

    @property
    def _core(self) -> RuntimeCore:
        core = self._host._core
        if core is None:
            raise InvalidRequest("Runtime core has not been prepared")
        return core

    def add_task_listener(
        self,
        *,
        on_event: EventCallback | None = None,
        on_done: DoneCallback | None = None,
        on_error: ErrorCallback | None = None,
        execution: ListenerExecution = "caller",
        group: str | None = None,
    ) -> TaskListener:
        return self._listeners.add(on_event=on_event, on_done=on_done, on_error=on_error,
                                   execution=execution, group=group)

    async def start(self) -> None:
        if self._closing:
            raise InvalidRequest("Cannot start a closing SDK")
        await self._host.start()

    async def close(self) -> None:
        if await on_callback_owner(self.close):
            return
        self._closing = True
        await self._host.begin_close()
        try:
            await self._listeners.close()
        finally:
            await self._host.close()

    async def submit_task(self, request: TaskRequest) -> Task:
        if self._closing:
            raise InvalidRequest("Cannot submit a task to a closing SDK")
        task = await self._host.call("submit_task", request)
        self._listeners.track(task)
        return task

    async def get_task_status(self, task_id: str) -> Task:
        return await self._host.call("get_task_status", task_id)

    async def cancel_task(self, task_id: str) -> Task:
        return await self._host.call("cancel_task", task_id)

    async def list_skills(self) -> list[SkillDefinition]:
        return await self._host.call("list_skills")

    async def subscribe_task_events(
        self, task_id: str, after_sequence: int = 0
    ) -> AsyncGenerator[Event, None]:
        async with aclosing(self._host.subscribe_task_events(task_id, after_sequence)) as stream:
            async for event in stream:
                yield event
