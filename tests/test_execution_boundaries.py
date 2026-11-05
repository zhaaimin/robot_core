import asyncio
from typing import Any

from robot_agent_runtime.kernel.core import RuntimeCore
from robot_agent_runtime.errors import RobotStopError
from robot_agent_runtime.models import Event, SkillCall, SkillContext, SkillDefinition, SkillResult, Task, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository


class StubbornSkill:
    def __init__(self, name: str = "stubborn") -> None:
        self.definition = SkillDefinition(
            name=name, version="1", input_schema={"type": "object"},
            resources={"motor"}, permissions=set(), preconditions=set(),
            timeout_s=0.02, supports_cancel=True,
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
        self.entered.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
            if context.progress is not None:
                context.progress(0.9)
            return SkillResult(success=True)
        finally:
            self.finished.set()


class ImmediateSkill:
    def __init__(self, name: str, resources: set[str]) -> None:
        self.definition = SkillDefinition(
            name=name, version="1", input_schema={"type": "object"},
            resources=resources, permissions=set(), preconditions=set(),
            timeout_s=1, supports_cancel=True,
        )
        self.calls = 0

    async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
        self.calls += 1
        return SkillResult(success=True)


def request(name: str, *, timeout_s: float | None = None) -> TaskRequest:
    return TaskRequest(task_type="isolation", steps=[SkillCall(skill=name, timeout_s=timeout_s)])


async def terminal(core: RuntimeCore, task_id: str) -> Task:
    async with asyncio.timeout(1):
        while True:
            task = await core.get_task_status(task_id)
            if task.state.is_terminal:
                return task
            await asyncio.sleep(0)


async def test_total_deadline_expires_while_waiting_for_resources() -> None:
    blocker = StubbornSkill()
    later = ImmediateSkill("later", {"motor"})
    core = RuntimeCore(InMemoryTaskRepository(), {"stubborn": blocker, "later": later})
    await core.start()
    try:
        await core.submit_task(request("stubborn", timeout_s=30))
        await asyncio.wait_for(blocker.entered.wait(), 1)
        task = await core.submit_task(TaskRequest(
            task_type="deadline", steps=[SkillCall(skill="later")], timeout_s=0.02,
        ))
        final = await terminal(core, task.id)
        assert final.state == TaskState.FAILED
        assert final.error is not None and final.error.code == "TASK_TIMEOUT"
        assert later.calls == 0
    finally:
        blocker.release.set()
        await core.close()


async def test_total_deadline_stops_cooperative_execution() -> None:
    from robot_agent_runtime.errors import TaskCancelled

    class Cooperative(StubbornSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            self.entered.set()
            await context.cancel_event.wait()
            self.finished.set()
            raise TaskCancelled("Stopped")

    skill = Cooperative()
    core = RuntimeCore(InMemoryTaskRepository(), {"stubborn": skill})
    await core.start()
    try:
        task = await core.submit_task(TaskRequest(
            task_type="deadline", steps=[SkillCall(skill="stubborn", timeout_s=30)], timeout_s=0.02,
        ))
        final = await terminal(core, task.id)
        assert final.state == TaskState.FAILED
        assert final.error is not None and final.error.code == "TASK_TIMEOUT"
        assert skill.finished.is_set()
    finally:
        await core.close()


async def test_unsupported_cancellation_does_not_cancel_real_coroutine() -> None:
    class Unsupported(StubbornSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.finished.set()
                raise
            return SkillResult(success=True)

    skill = Unsupported()
    skill.definition.supports_cancel = False
    core = RuntimeCore(InMemoryTaskRepository(), {"stubborn": skill})
    await core.start()
    try:
        task = await core.submit_task(request("stubborn", timeout_s=30))
        await asyncio.wait_for(skill.entered.wait(), 1)
        final = await core.cancel_task(task.id)
        assert final.state == TaskState.RECONCILIATION_REQUIRED
        assert not skill.finished.is_set()
    finally:
        skill.release.set()
        await core.close()


async def test_unsuccessful_cancellation_result_keeps_resources_quarantined() -> None:
    class Uncertain(StubbornSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            self.entered.set()
            await context.cancel_event.wait()
            return SkillResult(success=False)

    skill = Uncertain()
    core = RuntimeCore(InMemoryTaskRepository(), {"stubborn": skill})
    await core.start()
    try:
        task = await core.submit_task(request("stubborn", timeout_s=30))
        await asyncio.wait_for(skill.entered.wait(), 1)
        final = await core.cancel_task(task.id)
        assert final.state == TaskState.RECONCILIATION_REQUIRED
        assert "motor" in core._resources._quarantined
    finally:
        await core.close()


async def test_deadline_expiring_during_started_write_never_dispatches_skill() -> None:
    class SlowWrite(InMemoryTaskRepository):
        async def apply_transition(self, task: Task, event: Event) -> tuple[Task, Event]:
            if event.event_type == "SKILL_STARTED":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass
            return await super().apply_transition(task, event)

    skill = ImmediateSkill("later", set())
    core = RuntimeCore(SlowWrite(), {"later": skill})
    await core.start()
    try:
        task = await core.submit_task(TaskRequest(
            task_type="deadline", steps=[SkillCall(skill="later")], timeout_s=0.02,
        ))
        final = await terminal(core, task.id)
        assert final.error is not None and final.error.code == "TASK_TIMEOUT"
        assert skill.calls == 0
    finally:
        await core.close()


async def test_shutdown_during_deadline_stop_preserves_uncertain_ownership() -> None:
    class NoticesCancellation(StubbornSkill):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_seen = asyncio.Event()

        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            self.entered.set()
            await context.cancel_event.wait()
            self.cancel_seen.set()
            return await super().execute(context, params)

    skill = NoticesCancellation()
    repo = InMemoryTaskRepository()
    core = RuntimeCore(repo, {"stubborn": skill})
    await core.start()
    try:
        task = await core.submit_task(TaskRequest(
            task_type="deadline", steps=[SkillCall(skill="stubborn", timeout_s=30)], timeout_s=0.02,
        ))
        await asyncio.wait_for(skill.cancel_seen.wait(), 1)
        await asyncio.wait_for(core.close(), 1)
        final = await repo.get_task(task.id)
        assert final is not None and final.state == TaskState.RECONCILIATION_REQUIRED
        assert "motor" in core._resources._quarantined
        assert not skill.finished.is_set()
    finally:
        skill.release.set()
        await core.close()


async def test_shutdown_during_confirmed_deadline_stop_preserves_timeout_reason() -> None:
    from robot_agent_runtime.errors import TaskCancelled

    class ConfirmsCancellation(StubbornSkill):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_seen = asyncio.Event()

        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            await context.cancel_event.wait()
            self.cancel_seen.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.finished.set()
                raise TaskCancelled("Physical stop confirmed") from None

    skill, repo = ConfirmsCancellation(), InMemoryTaskRepository()
    core = RuntimeCore(repo, {"stubborn": skill})
    await core.start()
    try:
        task = await core.submit_task(TaskRequest(
            task_type="deadline", steps=[SkillCall(skill="stubborn", timeout_s=30)], timeout_s=0.02,
        ))
        await asyncio.wait_for(skill.cancel_seen.wait(), 1)
        await asyncio.wait_for(core.close(), 1)
        final = await repo.get_task(task.id)
        assert final is not None and final.state == TaskState.FAILED
        assert final.error is not None and final.error.code == "TASK_TIMEOUT"
        assert skill.finished.is_set()
    finally:
        await core.close()


async def test_shutdown_during_local_cleanup_preserves_adapter_uncertainty() -> None:
    from robot_agent_runtime.errors import ExecutionUnresponsive

    class Uncertain(ImmediateSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            loop = asyncio.get_running_loop()
            execution = core._executions[context.task_id]
            loop.call_soon(lambda: loop.call_soon(lambda: loop.call_soon(execution.cancel)))
            raise ExecutionUnresponsive("Physical stop is unknown")

    skill = Uncertain("uncertain", {"motor"})
    core = RuntimeCore(InMemoryTaskRepository(), {"uncertain": skill})
    await core.start()
    try:
        task = await core.submit_task(request("uncertain"))
        final = await terminal(core, task.id)
        assert final.state == TaskState.RECONCILIATION_REQUIRED
        assert "motor" in core._resources._quarantined
    finally:
        await core.close()


async def test_uncooperative_timeout_quarantines_resource_and_keeps_other_work_running() -> None:
    stuck = StubbornSkill()
    waiting = ImmediateSkill("waiting", {"motor"})
    independent = ImmediateSkill("independent", {"speaker"})
    repository = InMemoryTaskRepository()
    core = RuntimeCore(repository, {"stubborn": stuck, "waiting": waiting, "independent": independent})
    await core.start()
    try:
        first = await core.submit_task(request("stubborn"))
        await asyncio.wait_for(stuck.entered.wait(), 1)
        second = await core.submit_task(request("waiting"))
        final = await terminal(core, first.id)
        assert final.state == TaskState.RECONCILIATION_REQUIRED
        assert final.error is not None and final.error.code == "EXECUTION_UNRESPONSIVE"
        blocked = await terminal(core, second.id)
        assert blocked.state == TaskState.FAILED
        assert blocked.error is not None and blocked.error.code == "RESOURCE_QUARANTINED"
        assert waiting.calls == 0
        healthy = await core.submit_task(request("independent"))
        assert (await terminal(core, healthy.id)).state == TaskState.SUCCEEDED
        before = await repository.list_events(first.id)
        stuck.release.set()
        await asyncio.wait_for(stuck.finished.wait(), 1)
        assert await repository.list_events(first.id) == before
        assert (await core.get_task_status(first.id)).state == TaskState.RECONCILIATION_REQUIRED
    finally:
        stuck.release.set()
        await core.close()


async def test_uncooperative_cancel_has_bounded_acknowledgement() -> None:
    stuck = StubbornSkill()
    core = RuntimeCore(InMemoryTaskRepository(), {"stubborn": stuck})
    await core.start()
    try:
        task = await core.submit_task(request("stubborn", timeout_s=30))
        await asyncio.wait_for(stuck.entered.wait(), 1)
        final = await asyncio.wait_for(core.cancel_task(task.id), 1)
        assert final.state == TaskState.RECONCILIATION_REQUIRED
        assert final.cancel_requested
    finally:
        stuck.release.set()
        await core.close()


async def test_shutdown_does_not_wait_forever_for_uncooperative_skill() -> None:
    stuck = StubbornSkill()
    repository = InMemoryTaskRepository()
    core = RuntimeCore(repository, {"stubborn": stuck})
    await core.start()
    try:
        task = await core.submit_task(request("stubborn", timeout_s=30))
        await asyncio.wait_for(stuck.entered.wait(), 1)
        await asyncio.wait_for(core.close(), 1)
        persisted = await repository.get_task(task.id)
        assert persisted is not None and persisted.state == TaskState.RECONCILIATION_REQUIRED
    finally:
        stuck.release.set()
        await core.close()


async def test_cooperative_stop_error_still_quarantines_physical_resources() -> None:
    class UnconfirmedStop(StubbornSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            self.entered.set()
            await context.cancel_event.wait()
            raise RobotStopError("Physical stop was not confirmed")

    first = UnconfirmedStop()
    later = ImmediateSkill("later", {"motor"})
    core = RuntimeCore(InMemoryTaskRepository(), {"stubborn": first, "later": later})
    await core.start()
    try:
        task = await core.submit_task(request("stubborn", timeout_s=30))
        await asyncio.wait_for(first.entered.wait(), 1)
        final = await asyncio.wait_for(core.cancel_task(task.id), 1)
        assert final.state == TaskState.RECONCILIATION_REQUIRED
        second = await core.submit_task(request("later"))
        assert (await terminal(core, second.id)).error.code == "RESOURCE_QUARANTINED"
        assert later.calls == 0
    finally:
        await core.close()


async def test_progress_queued_behind_terminal_write_cannot_append_after_terminal() -> None:
    class GatedRepository(InMemoryTaskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def apply_transition(self, task: Task, event: Event) -> tuple[Task, Event]:
            if event.event_type == "TASK_SUCCEEDED":
                self.entered.set()
                await self.release.wait()
            return await super().apply_transition(task, event)

    class RetainedProgress(ImmediateSkill):
        def __init__(self) -> None:
            super().__init__("retained", set())
            self.context: SkillContext | None = None

        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            self.context = context
            return SkillResult(success=True)

    repository, skill = GatedRepository(), RetainedProgress()
    core = RuntimeCore(repository, {"retained": skill})
    await core.start()
    try:
        task = await core.submit_task(request("retained"))
        await asyncio.wait_for(repository.entered.wait(), 1)
        assert skill.context is not None and skill.context.progress is not None
        skill.context.progress(0.9)
        pending_progress = tuple(core._state.progress_tasks[task.id])
        repository.release.set()
        await asyncio.gather(*pending_progress)
        assert (await terminal(core, task.id)).state == TaskState.SUCCEEDED
        assert (await repository.list_events(task.id))[-1].event_type == "TASK_SUCCEEDED"
    finally:
        repository.release.set()
        await core.close()
