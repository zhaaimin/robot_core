import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from robot_agent_runtime.kernel.core import RuntimeCore
from robot_agent_runtime.errors import SkillExecutionError, TaskCancelled, TaskNotFound
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
from robot_agent_runtime.ports import Skill
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository


async def wait_for_event(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


async def wait_for_terminal(core: RuntimeCore, task_id: str) -> Task:
    async with asyncio.timeout(1):
        while True:
            task = await core.get_task_status(task_id)
            if task.state.is_terminal:
                return task
            await asyncio.sleep(0.001)


async def collect_events(core: RuntimeCore, task_id: str) -> list[Event]:
    received: list[Event] = []
    async for event in core.subscribe_task_events(task_id):
        received.append(event)
    return received


@asynccontextmanager
async def running_core(
    skills: dict[str, Skill], repository: InMemoryTaskRepository | None = None
) -> AsyncIterator[RuntimeCore]:
    core = RuntimeCore(repository or InMemoryTaskRepository(), skills)
    await core.start()
    try:
        yield core
    finally:
        await core.close()


def definition(name: str, *, resources: set[str] | None = None) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        version="1.0.0",
        input_schema={"type": "object"},
        resources=resources or set(),
        supports_cancel=True,
        timeout_s=10,
    )


class ControlledSkill:
    def __init__(self, name: str, *, resources: set[str] | None = None) -> None:
        self.definition = definition(name, resources=resources)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.cancel_observed = asyncio.Event()

    async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        release_waiter = asyncio.create_task(self.release.wait())
        cancel_waiter = asyncio.create_task(context.cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {release_waiter, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_waiter in done and context.cancel_event.is_set():
                self.cancel_observed.set()
                raise TaskCancelled("Skill acknowledged cancellation")
            return SkillResult(success=True, output={"skill": self.definition.name})
        finally:
            for waiter in (release_waiter, cancel_waiter):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(release_waiter, cancel_waiter, return_exceptions=True)
            self.active -= 1


class ImmediateSkill:
    def __init__(
        self,
        name: str,
        *,
        result: SkillResult | None = None,
        exception: Exception | None = None,
        report_progress: bool = False,
    ) -> None:
        self.definition = definition(name)
        self.result = result or SkillResult(success=True, output={"ok": True})
        self.exception = exception
        self.report_progress = report_progress
        self.calls = 0

    async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
        self.calls += 1
        if self.report_progress and context.progress is not None:
            context.progress(0.5)
        if self.exception is not None:
            raise self.exception
        return self.result


class ProgressOutcomeSkill:
    def __init__(self, outcome: str) -> None:
        self.definition = definition(outcome)
        self.entered = asyncio.Event()

    async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
        self.entered.set()
        if self.definition.name == "success":
            assert context.progress is not None
            context.progress(0.5)
            return SkillResult(success=True)
        if self.definition.name == "error":
            assert context.progress is not None
            context.progress(0.5)
            raise ValueError("progress then failure")
        if self.definition.name == "timeout":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise TaskCancelled("Synthetic work stopped") from None
            finally:
                assert context.progress is not None
                context.progress(0.5)
        else:
            await context.cancel_event.wait()
            assert context.progress is not None
            context.progress(0.5)
            raise TaskCancelled("cancel after progress")
        raise AssertionError("unreachable")


class NonCancellableSkill:
    def __init__(self) -> None:
        self.definition = definition("non_cancellable").model_copy(
            update={"supports_cancel": False}
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return SkillResult(success=True)


class ResultGateRepository(InMemoryTaskRepository):
    def __init__(self) -> None:
        super().__init__()
        self.result_update_started = asyncio.Event()
        self.release_result_update = asyncio.Event()

    async def update_task(self, task: Task) -> Task:
        if task.result is not None and task.state == TaskState.RUNNING:
            self.result_update_started.set()
            await self.release_result_update.wait()
        return await super().update_task(task)


class CreateGateRepository(InMemoryTaskRepository):
    def __init__(self) -> None:
        super().__init__()
        self.create_started = asyncio.Event()
        self.release_create = asyncio.Event()

    async def create_task(self, task: Task) -> Task:
        created = await super().create_task(task)
        self.create_started.set()
        await self.release_create.wait()
        return created


class CloseGateRepository(InMemoryTaskRepository):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_calls = 0
        self.close_completed = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.release_close.wait()
        self.close_completed += 1


class TransitionGateRepository(InMemoryTaskRepository):
    def __init__(self) -> None:
        super().__init__()
        self.waiting_append_started = asyncio.Event()
        self.release_waiting_append = asyncio.Event()
        self.cancel_append_started = asyncio.Event()
        self.release_cancel_append = asyncio.Event()

    async def append_event(self, event: Event) -> Event:
        if event.event_type == "TASK_WAITING":
            self.waiting_append_started.set()
            await self.release_waiting_append.wait()
        if event.event_type == "TASK_CANCEL_REQUESTED":
            self.cancel_append_started.set()
            await self.release_cancel_append.wait()
        return await super().append_event(event)


class OneShotEventTimeoutRepository(InMemoryTaskRepository):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def append_event(self, event: Event) -> Event:
        if event.event_type == "SKILL_STARTED" and not self.failed:
            self.failed = True
            raise TimeoutError("repository unavailable")
        return await super().append_event(event)


async def test_cancel_waits_for_acknowledgement_and_is_idempotent_after_terminal() -> None:
    skill = ControlledSkill("long")
    async with running_core({"long": skill}) as core:
        submitted = await core.submit_task(
            TaskRequest(task_type="long", steps=[SkillCall(skill="long")])
        )
        await wait_for_event(skill.entered)

        cancel_call = asyncio.create_task(core.cancel_task(submitted.id))
        for _ in range(100):
            current = await core.get_task_status(submitted.id)
            if current.state == TaskState.CANCELING:
                break
            await asyncio.sleep(0)
        assert current.cancel_requested is True
        final = await asyncio.wait_for(cancel_call, timeout=1)

        assert final.state == TaskState.CANCELED
        assert final.cancel_requested is True
        assert skill.cancel_observed.is_set()
        events_before = await collect_events(core, submitted.id)
        event_types = [event.event_type for event in events_before]
        assert "TASK_CANCEL_REQUESTED" in event_types
        assert "SKILL_CANCELLED" in event_types
        assert event_types.index("TASK_CANCEL_REQUESTED") < event_types.index("SKILL_CANCELLED")
        assert event_types[-1] == "TASK_CANCELED"

        repeated = await core.cancel_task(submitted.id)
        events_after = await collect_events(core, submitted.id)
        assert repeated == final
        assert events_after == events_before
        assert event_types.count("TASK_CANCEL_REQUESTED") == 1


async def test_skill_result_failure_stops_later_steps_and_preserves_error() -> None:
    failed = ImmediateSkill(
        "fail",
        result=SkillResult(
            success=False,
            error=ErrorInfo(code="HW_ERROR", message="hardware unavailable", details={"port": 2}),
        ),
    )
    later = ImmediateSkill("later")
    async with running_core({"fail": failed, "later": later}) as core:
        submitted = await core.submit_task(
            TaskRequest(
                task_type="failure",
                steps=[SkillCall(skill="fail"), SkillCall(skill="later")],
            )
        )
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.FAILED
        assert final.current_step == 1
        assert final.error == failed.result.error
        assert later.calls == 0
        events = await collect_events(core, submitted.id)
        assert events[-1].event_type == "TASK_FAILED"
        assert "SKILL_FAILED" in {event.event_type for event in events}


@pytest.mark.parametrize(
    ("exception", "expected_code"),
    [
        (ValueError("bad actuator"), "INTERNAL_ERROR"),
        (TimeoutError("device failure"), "INTERNAL_ERROR"),
        (SkillExecutionError("explicit failure", {"skill": "boom"}), "SKILL_EXECUTION_FAILED"),
    ],
)
async def test_unhandled_skill_exceptions_become_structured_task_errors(
    exception: Exception, expected_code: str
) -> None:
    skill = ImmediateSkill("boom", exception=exception)
    async with running_core({"boom": skill}) as core:
        submitted = await core.submit_task(
            TaskRequest(task_type="boom", steps=[SkillCall(skill="boom")])
        )
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.FAILED
        assert final.error is not None
        assert final.error.code == expected_code
        assert final.error.message
        if isinstance(exception, (ValueError, TimeoutError)):
            assert final.error.details["step"] == 1
            assert final.error.details["skill"] == "boom"
            assert final.error.details["exception_type"] == type(exception).__name__
            assert "timeout_s" not in final.error.details
        events = await collect_events(core, submitted.id)
        assert "SKILL_FAILED" in {event.event_type for event in events}
        assert events[-1].event_type == "TASK_FAILED"


async def test_step_timeout_uses_call_override_and_fails_task() -> None:
    skill = ControlledSkill("slow")
    later = ImmediateSkill("later")
    async with running_core({"slow": skill, "later": later}) as core:
        submitted = await core.submit_task(
            TaskRequest(
                task_type="timeout",
                steps=[
                    SkillCall(skill="slow", timeout_s=0.01),
                    SkillCall(skill="later"),
                ],
            )
        )
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.FAILED
        assert final.error is not None
        assert final.error.code == "SKILL_TIMEOUT"
        assert final.error.details["step"] == 1
        assert final.error.details["timeout_s"] == 0.01
        assert later.calls == 0
        events = await collect_events(core, submitted.id)
        assert events[-1].event_type == "TASK_FAILED"


async def test_progress_callback_emits_persistent_progress_event() -> None:
    skill = ImmediateSkill("progress", report_progress=True)
    async with running_core({"progress": skill}) as core:
        submitted = await core.submit_task(
            TaskRequest(task_type="progress", steps=[SkillCall(skill="progress")])
        )
        await wait_for_terminal(core, submitted.id)
        events = await collect_events(core, submitted.id)

        progress = [event for event in events if event.event_type == "SKILL_PROGRESS"]
        assert len(progress) == 1
        assert progress[0].data["progress"] == 0.5
        assert progress[0].data["step"] == 1


async def test_same_resource_is_mutually_exclusive_and_different_resources_run_in_parallel() -> None:
    first = ControlledSkill("first", resources={"base"})
    second = ControlledSkill("second", resources={"base"})
    independent = ControlledSkill("independent", resources={"speaker"})
    async with running_core(
        {"first": first, "second": second, "independent": independent}
    ) as core:
        first_task = await core.submit_task(
            TaskRequest(task_type="one", steps=[SkillCall(skill="first")])
        )
        await wait_for_event(first.entered)
        second_task = await core.submit_task(
            TaskRequest(task_type="two", steps=[SkillCall(skill="second")])
        )
        independent_task = await core.submit_task(
            TaskRequest(task_type="three", steps=[SkillCall(skill="independent")])
        )
        await wait_for_event(independent.entered)
        for _ in range(20):
            await asyncio.sleep(0)
        assert not second.entered.is_set()

        independent.release.set()
        first.release.set()
        await wait_for_event(second.entered)
        assert first.max_active == 1
        assert second.max_active == 1
        second.release.set()

        assert (await wait_for_terminal(core, first_task.id)).state == TaskState.SUCCEEDED
        assert (await wait_for_terminal(core, second_task.id)).state == TaskState.SUCCEEDED
        assert (await wait_for_terminal(core, independent_task.id)).state == TaskState.SUCCEEDED


async def test_missing_task_status_and_cancellation_raise_task_not_found() -> None:
    async with running_core({}) as core:
        with pytest.raises(TaskNotFound):
            await core.get_task_status("missing")
        with pytest.raises(TaskNotFound):
            await core.cancel_task("missing")


async def test_cancel_accepted_during_final_result_persistence_wins_over_success() -> None:
    repository = ResultGateRepository()
    skill = ImmediateSkill("last")
    async with running_core({"last": skill}, repository) as core:
        submitted = await core.submit_task(
            TaskRequest(task_type="race", steps=[SkillCall(skill="last")])
        )
        await wait_for_event(repository.result_update_started)
        cancel_call = asyncio.create_task(core.cancel_task(submitted.id))
        await asyncio.sleep(0)
        repository.release_result_update.set()

        final = await asyncio.wait_for(cancel_call, timeout=1)
        events = await repository.list_events(submitted.id)
        event_types = [event.event_type for event in events]
        assert final.state == TaskState.CANCELED
        assert event_types[-1] == "TASK_CANCELED"
        assert "TASK_SUCCEEDED" not in event_types
        assert event_types.index("TASK_RESULT") < event_types.index(
            "TASK_CANCEL_REQUESTED"
        )


async def test_success_already_terminal_makes_later_cancel_idempotent() -> None:
    skill = ImmediateSkill("last")
    repository = InMemoryTaskRepository()
    async with running_core({"last": skill}, repository) as core:
        submitted = await core.submit_task(
            TaskRequest(task_type="success", steps=[SkillCall(skill="last")])
        )
        succeeded = await wait_for_terminal(core, submitted.id)
        events_before = await repository.list_events(submitted.id)

        cancelled = await core.cancel_task(submitted.id)

        assert succeeded.state == TaskState.SUCCEEDED
        assert cancelled == succeeded
        assert await repository.list_events(submitted.id) == events_before


async def test_resource_wait_uses_waiting_state_and_starts_skill_only_after_lock() -> None:
    holder = ControlledSkill("holder", resources={"base"})
    waiter = ControlledSkill("waiter", resources={"base"})
    repository = InMemoryTaskRepository()
    async with running_core({"holder": holder, "waiter": waiter}, repository) as core:
        holder_task = await core.submit_task(
            TaskRequest(task_type="holder", steps=[SkillCall(skill="holder")])
        )
        await wait_for_event(holder.entered)
        waiter_task = await core.submit_task(
            TaskRequest(task_type="waiter", steps=[SkillCall(skill="waiter")])
        )
        async with asyncio.timeout(1):
            while (await core.get_task_status(waiter_task.id)).current_step != 1:
                await asyncio.sleep(0)
        for _ in range(5):
            await asyncio.sleep(0)

        waiting = await core.get_task_status(waiter_task.id)
        events_while_waiting = await repository.list_events(waiter_task.id)
        assert waiting.state == TaskState.WAITING
        assert "SKILL_STARTED" not in {
            event.event_type for event in events_while_waiting
        }
        assert waiter.calls == 0

        holder.release.set()
        await wait_for_event(waiter.entered)
        running_events = await repository.list_events(waiter_task.id)
        event_types = [event.event_type for event in running_events]
        waiting_index = event_types.index("TASK_WAITING")
        running_index = event_types.index("TASK_RUNNING", waiting_index + 1)
        started_index = event_types.index("SKILL_STARTED")
        assert waiting_index < running_index < started_index

        waiter.release.set()
        assert (await wait_for_terminal(core, holder_task.id)).state == TaskState.SUCCEEDED
        assert (await wait_for_terminal(core, waiter_task.id)).state == TaskState.SUCCEEDED


async def test_cancel_while_waiting_for_resource_does_not_wait_for_holder() -> None:
    holder = ControlledSkill("holder", resources={"base"})
    waiter = ControlledSkill("waiter", resources={"base"})
    async with running_core({"holder": holder, "waiter": waiter}, repository=None) as core:
        holder_task = await core.submit_task(
            TaskRequest(task_type="holder", steps=[SkillCall(skill="holder")])
        )
        await wait_for_event(holder.entered)
        waiter_task = await core.submit_task(
            TaskRequest(task_type="waiter", steps=[SkillCall(skill="waiter")])
        )
        async with asyncio.timeout(1):
            while (await core.get_task_status(waiter_task.id)).current_step != 1:
                await asyncio.sleep(0)

        final = await asyncio.wait_for(core.cancel_task(waiter_task.id), timeout=0.2)

        assert final.state == TaskState.CANCELED
        assert waiter.calls == 0
        assert not waiter.entered.is_set()
        holder.release.set()
        assert (await wait_for_terminal(core, holder_task.id)).state == TaskState.SUCCEEDED


@pytest.mark.parametrize("outcome", ["success", "error", "timeout", "cancel"])
async def test_terminal_event_is_emitted_only_after_progress_tasks_settle(
    outcome: str,
) -> None:
    repository = InMemoryTaskRepository()
    skill = ProgressOutcomeSkill(outcome)
    async with running_core({outcome: skill}, repository) as core:
        call = SkillCall(skill=outcome, timeout_s=0.01 if outcome == "timeout" else None)
        submitted = await core.submit_task(
            TaskRequest(task_type=outcome, steps=[call])
        )
        if outcome == "cancel":
            await wait_for_event(skill.entered)
            await core.cancel_task(submitted.id)
        final = await wait_for_terminal(core, submitted.id)
        for _ in range(5):
            await asyncio.sleep(0)
        events = await repository.list_events(submitted.id)

        expected_terminal = {
            "success": "TASK_SUCCEEDED",
            "error": "TASK_FAILED",
            "timeout": "TASK_FAILED",
            "cancel": "TASK_CANCELED",
        }[outcome]
        assert events[-1].event_type == expected_terminal
        assert final.state.is_terminal
        terminal_index = events.index(events[-1])
        assert all(
            event.event_type != "SKILL_PROGRESS"
            for event in events[terminal_index + 1 :]
        )


async def test_close_delivers_execution_terminal_event_before_live_stream_ends() -> None:
    repository = InMemoryTaskRepository()
    skill = ControlledSkill("long")
    core = RuntimeCore(repository, {"long": skill})
    await core.start()
    submitted = await core.submit_task(
        TaskRequest(task_type="close", steps=[SkillCall(skill="long")])
    )
    await wait_for_event(skill.entered)
    subscription = core.subscribe_task_events(submitted.id)
    live_events = [await anext(subscription)]

    async def collect_remaining() -> None:
        async for event in subscription:
            live_events.append(event)

    collector = asyncio.create_task(collect_remaining())
    await asyncio.sleep(0)
    await core.close()
    await asyncio.wait_for(collector, timeout=1)

    persisted = await repository.list_events(submitted.id)
    assert [event.event_id for event in live_events] == [
        event.event_id for event in persisted
    ]
    assert live_events[-1].event_type == "TASK_CANCELED"
    assert persisted[-1].event_type == "TASK_CANCELED"
    await subscription.aclose()


async def test_non_cancellable_skill_completion_does_not_continue_later_steps() -> None:
    skill = NonCancellableSkill()
    later = ImmediateSkill("later")
    async with running_core({"non_cancellable": skill, "later": later}) as core:
        submitted = await core.submit_task(
            TaskRequest(
                task_type="cancel",
                steps=[SkillCall(skill="non_cancellable"), SkillCall(skill="later")],
            )
        )
        await wait_for_event(skill.entered)
        cancel_call = asyncio.create_task(core.cancel_task(submitted.id))
        async with asyncio.timeout(1):
            while (await core.get_task_status(submitted.id)).state != TaskState.CANCELING:
                await asyncio.sleep(0)
        skill.release.set()

        final = await asyncio.wait_for(cancel_call, timeout=1)
        assert final.state == TaskState.CANCELED
        assert later.calls == 0


async def test_multiple_steps_execute_strictly_in_order() -> None:
    first = ControlledSkill("first")
    second = ControlledSkill("second")
    async with running_core({"first": first, "second": second}) as core:
        submitted = await core.submit_task(
            TaskRequest(
                task_type="sequence",
                steps=[SkillCall(skill="first"), SkillCall(skill="second")],
            )
        )
        await wait_for_event(first.entered)
        for _ in range(5):
            await asyncio.sleep(0)
        assert second.calls == 0

        first.release.set()
        await wait_for_event(second.entered)
        assert first.active == 0
        second.release.set()
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.SUCCEEDED
        assert final.result is not None
        assert [step["skill"] for step in final.result["steps"]] == ["first", "second"]


async def test_submit_cancellation_completes_ownership_handoff_before_propagating() -> None:
    repository = CreateGateRepository()
    skill = ImmediateSkill("owned")
    async with running_core({"owned": skill}, repository) as core:
        request = TaskRequest(
            task_type="owned",
            idempotency_key="owned-key",
            steps=[SkillCall(skill="owned")],
        )
        submission = asyncio.create_task(core.submit_task(request))
        await wait_for_event(repository.create_started)
        submission.cancel()
        repository.release_create.set()
        with pytest.raises(asyncio.CancelledError):
            await submission

        retried = await core.submit_task(request)
        for _ in range(10):
            await asyncio.sleep(0)
        assert skill.calls == 1
        final = await wait_for_terminal(core, retried.id)
        assert final.state == TaskState.SUCCEEDED
        assert await repository.find_by_idempotency_key("owned-key") == final


async def test_close_waits_for_submit_handoff_and_no_execution_starts_after_return() -> None:
    repository = CreateGateRepository()
    skill = ImmediateSkill("probe")
    close_returned = asyncio.Event()
    calls_after_close = 0
    original_execute = skill.execute

    async def observed_execute(context: SkillContext, params: dict[str, Any]) -> SkillResult:
        nonlocal calls_after_close
        if close_returned.is_set():
            calls_after_close += 1
        return await original_execute(context, params)

    skill.execute = observed_execute  # type: ignore[method-assign]
    core = RuntimeCore(repository, {"probe": skill})
    await core.start()
    request = TaskRequest(
        task_type="probe", idempotency_key="probe-key", steps=[SkillCall(skill="probe")]
    )
    submission = asyncio.create_task(core.submit_task(request))
    await wait_for_event(repository.create_started)

    async def close_and_mark() -> None:
        await core.close()
        close_returned.set()

    closing = asyncio.create_task(close_and_mark())
    await asyncio.sleep(0)
    close_waited_for_submit = not closing.done()
    repository.release_create.set()
    await asyncio.gather(submission, closing)
    persisted = await repository.find_by_idempotency_key("probe-key")

    assert close_waited_for_submit
    assert calls_after_close == 0
    assert persisted is not None
    assert persisted.state.is_terminal


async def test_cancelled_close_can_be_retried_to_close_repository() -> None:
    repository = CloseGateRepository()
    core = RuntimeCore(repository, {})
    await core.start()
    first = asyncio.create_task(core.close())
    await wait_for_event(repository.close_started)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(core.close())
    await asyncio.sleep(0)
    second_waited = not second.done()
    repository.release_close.set()
    await asyncio.wait_for(second, timeout=1)

    assert second_waited
    assert repository.close_calls == 1
    assert repository.close_completed == 1


async def test_concurrent_close_calls_wait_for_the_same_shutdown() -> None:
    repository = CloseGateRepository()
    core = RuntimeCore(repository, {})
    await core.start()
    first = asyncio.create_task(core.close())
    await wait_for_event(repository.close_started)
    second = asyncio.create_task(core.close())
    await asyncio.sleep(0)

    assert not second.done()
    repository.release_close.set()
    await asyncio.gather(first, second)
    assert repository.close_calls == 1
    assert repository.close_completed == 1


async def test_cancel_transition_cannot_be_overwritten_by_stale_running_snapshot() -> None:
    repository = TransitionGateRepository()
    skill = ControlledSkill("resource", resources={"base"})
    async with running_core({"resource": skill}, repository) as core:
        submitted = await core.submit_task(
            TaskRequest(task_type="atomic", steps=[SkillCall(skill="resource")])
        )
        await wait_for_event(repository.waiting_append_started)
        cancellation = asyncio.create_task(core.cancel_task(submitted.id))
        await asyncio.sleep(0)
        repository.release_waiting_append.set()
        await wait_for_event(repository.cancel_append_started)
        for _ in range(5):
            await asyncio.sleep(0)
        state_during_cancel_event = (await repository.get_task(submitted.id)).state
        repository.release_cancel_append.set()
        final = await asyncio.wait_for(cancellation, timeout=1)
        events = await repository.list_events(submitted.id)

        assert state_during_cancel_event == TaskState.CANCELING
        assert final.state == TaskState.CANCELED
        event_types = [event.event_type for event in events]
        cancel_index = event_types.index("TASK_CANCEL_REQUESTED")
        assert "TASK_RUNNING" not in event_types[cancel_index + 1 :]
        expected_states = {
            "TASK_WAITING": "waiting",
            "TASK_CANCEL_REQUESTED": "canceling",
            "TASK_CANCELED": "canceled",
        }
        assert all(
            event.data["state"] == expected_states[event.event_type]
            for event in events
            if event.event_type in expected_states
        )


async def test_concurrent_cancellation_emits_one_request_and_both_wait_for_final() -> None:
    skill = ControlledSkill("long")
    repository = InMemoryTaskRepository()
    async with running_core({"long": skill}, repository) as core:
        submitted = await core.submit_task(
            TaskRequest(task_type="long", steps=[SkillCall(skill="long")])
        )
        await wait_for_event(skill.entered)
        first = asyncio.create_task(core.cancel_task(submitted.id))
        second = asyncio.create_task(core.cancel_task(submitted.id))
        first_final, second_final = await asyncio.gather(first, second)

        assert first_final.state == TaskState.CANCELED
        assert second_final == first_final
        events = await repository.list_events(submitted.id)
        assert sum(
            event.event_type == "TASK_CANCEL_REQUESTED" for event in events
        ) == 1


async def test_terminal_execution_cleans_runtime_metadata_but_preserves_history() -> None:
    repository = InMemoryTaskRepository()
    skill = ImmediateSkill("clean")
    async with running_core({"clean": skill}, repository) as core:
        submitted = await core.submit_task(
            TaskRequest(task_type="clean", steps=[SkillCall(skill="clean")])
        )
        final = await wait_for_terminal(core, submitted.id)
        for _ in range(5):
            await asyncio.sleep(0)

        runtime_dictionaries = [
            core._executions,
            core._progress_tasks,
            core._cancel_events,
            core._tasks,
            core._task_locks,
            core._sequences,
        ]
        assert all(submitted.id not in values for values in runtime_dictionaries)
        events_before = await repository.list_events(submitted.id)
        assert await core.cancel_task(submitted.id) == final
        assert await repository.list_events(submitted.id) == events_before
        replayed = await collect_events(core, submitted.id)
        assert replayed == events_before


async def test_repository_timeout_is_internal_error_not_skill_timeout() -> None:
    repository = OneShotEventTimeoutRepository()
    skill = ImmediateSkill("work")
    async with running_core({"work": skill}, repository) as core:
        submitted = await core.submit_task(
            TaskRequest(task_type="repository", steps=[SkillCall(skill="work")])
        )
        final = await wait_for_terminal(core, submitted.id)

        assert final.state == TaskState.FAILED
        assert final.error is not None
        assert final.error.code == "INTERNAL_ERROR"
        assert final.error.details["exception_type"] == "TimeoutError"
        assert "timeout_s" not in final.error.details


async def test_invalid_json_schema_fails_task_instead_of_leaving_validating() -> None:
    repository = InMemoryTaskRepository()
    skill = ImmediateSkill("invalid_schema")
    skill.definition = skill.definition.model_copy(
        update={"input_schema": {"type": "not-a-json-schema-type"}}
    )
    async with running_core({"invalid_schema": skill}, repository) as core:
        submitted = await core.submit_task(
            TaskRequest(
                task_type="schema", steps=[SkillCall(skill="invalid_schema")]
            )
        )

        assert submitted.state == TaskState.FAILED
        assert submitted.error is not None
        assert submitted.error.code == "INTERNAL_ERROR"
        assert submitted.error.details["skill"] == "invalid_schema"
        assert skill.calls == 0
        assert await repository.get_nonterminal_tasks() == []
        events = await repository.list_events(submitted.id)
        assert events[-1].event_type == "TASK_FAILED"
