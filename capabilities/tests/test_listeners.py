import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from robot_agent_capabilities import RobotCapabilities
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import InvalidRequest, ListenerCapacityExceeded, PlatformEventSourceUnavailable, TaskWaitTimeout
from robot_agent_runtime.models import Event, PlatformEvent, SkillCall, SkillDefinition, Task, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime
from robot_agent_runtime.sdk import RemoteTransportError
from test_events import EventRuntime


@asynccontextmanager
async def running(*, wait_timeout_s: float = 5.0) -> AsyncIterator[tuple[RobotCapabilities, RobotRuntime, Simulator]]:
    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    robot = await RobotCapabilities.create(runtime, wait_timeout_s=wait_timeout_s)
    try:
        yield robot, runtime, simulator
    finally:
        if (close := getattr(robot, "close_listeners", None)) is not None:
            await close()
        await runtime.close()


async def test_task_listener_automatically_tracks_only_this_capability_client() -> None:
    async with running() as (robot, runtime, _):
        events: list[Event] = []
        done: list[Task] = []
        listener = robot.add_task_listener(on_event=events.append, on_done=done.append)
        foreign = await runtime.submit_and_wait(TaskRequest(task_type="outside", steps=[SkillCall(skill="robot.get_state")]))
        own = await robot.invoke("robot_get_state", {}, operation_id="own", wait=True)
        await asyncio.wait_for(listener.wait(), 1)
        assert [task.id for task in done] == [own["task_id"]]
        assert foreign.id not in {event.task_id for event in events}
        assert events[0].event_type == "TASK_CREATED" and events[-1].event_type == "TASK_SUCCEEDED"
        assert done[0].result == own["result"]
        await listener.close()
        assert listener.closed


async def test_task_business_failure_goes_to_done_not_listener_error() -> None:
    async with running() as (robot, _, _):
        done: list[Task] = []
        errors: list[Exception] = []
        listener = robot.add_task_listener(on_done=done.append, on_error=lambda task_id, error: errors.append(error))
        observed = await robot.invoke("robot_speak", {"message": ""}, operation_id="bad-input", wait=True)
        await asyncio.wait_for(listener.wait(), 1)
        assert done[0].id == observed["task_id"] and done[0].state.value == "failed"
        assert errors == []


async def test_explicit_task_ids_replay_but_do_not_track_future_invocations() -> None:
    async with running() as (robot, _, _):
        old = await robot.invoke("robot_get_state", {}, operation_id="old", wait=True)
        done: list[Task] = []
        listener = robot.add_task_listener(task_ids={old["task_id"]}, on_done=done.append)
        await asyncio.wait_for(listener.wait(), 1)
        await robot.invoke("robot_get_state", {}, operation_id="new", wait=True)
        await listener.wait()
        assert [task.id for task in done] == [old["task_id"]]


async def test_idempotent_invocation_is_not_notified_twice() -> None:
    async with running() as (robot, _, simulator):
        done: list[Task] = []
        listener = robot.add_task_listener(on_done=done.append)
        first = await robot.invoke("robot_speak", {"message": "hello"}, operation_id="same", wait=True)
        await robot.invoke("robot_speak", {"message": "hello"}, operation_id="same", wait=True)
        await asyncio.wait_for(listener.wait(), 1)
        assert [task.id for task in done] == [first["task_id"]]
        assert simulator.skills["audio.speak"].calls == 1


async def test_local_identity_conflict_does_not_register_an_old_task() -> None:
    async with running() as (robot, _, _):
        await robot.invoke("robot_speak", {"message": "old"}, operation_id="conflict", wait=True)
        done: list[Task] = []
        listener = robot.add_task_listener(on_done=done.append)
        conflict = await robot.invoke("robot_speak", {"message": "different"}, operation_id="conflict", wait=True)
        await listener.wait()
        assert conflict["error"]["code"] == "INVALID_REQUEST" and not done


async def test_wait_timeout_still_registers_the_submitted_task_for_later_completion() -> None:
    async with running(wait_timeout_s=0.01) as (robot, _, _):
        done: list[Task] = []
        listener = robot.add_task_listener(on_done=done.append)
        pending = await robot.invoke("robot_wait_delay", {"delay_s": 30}, operation_id="wait-timeout", wait=True)
        assert pending["error"]["code"] == "TASK_WAIT_TIMEOUT"
        await robot.invoke("robot_cancel_task", {"task_id": pending["task_id"]})
        await asyncio.wait_for(listener.wait(), 1)
        assert len(done) == 1 and done[0].id == pending["task_id"] and done[0].state.value == "canceled"


async def test_wait_timeout_identity_conflict_does_not_deliver_the_old_task() -> None:
    async with running(wait_timeout_s=0.01) as (robot, runtime, _):
        old = await robot.invoke("robot_wait_delay", {"delay_s": 30}, operation_id="timeout-conflict")
        done: list[Task] = []
        events: list[Event] = []
        errors: list[tuple[str | None, Exception]] = []
        listener = robot.add_task_listener(on_done=done.append, on_event=events.append,
                                           on_error=lambda task_id, error: errors.append((task_id, error)))
        conflict = await robot.invoke("robot_wait_delay", {"delay_s": 20}, operation_id="timeout-conflict", wait=True)
        assert conflict["error"]["code"] == "TASK_WAIT_TIMEOUT"
        await runtime.cancel_task(old["task_id"])
        await asyncio.wait_for(listener.wait(), 1)
        assert not done and not events
        assert len(errors) == 1 and errors[0][0] == old["task_id"]
        assert errors[0][1].info.code == "INVALID_REQUEST"


async def test_rejected_timeout_identity_does_not_block_a_later_matching_invocation() -> None:
    async with running(wait_timeout_s=0.01) as (robot, runtime, _):
        old = await robot.invoke("robot_wait_delay", {"delay_s": 30}, operation_id="retry-conflict")
        done: list[Task] = []
        errors: list[Exception] = []
        listener = robot.add_task_listener(on_done=done.append,
                                           on_error=lambda task_id, error: errors.append(error))
        conflict = await robot.invoke("robot_wait_delay", {"delay_s": 20},
                                      operation_id="retry-conflict", wait=True)
        assert conflict["error"]["code"] == "TASK_WAIT_TIMEOUT"
        await asyncio.wait_for(listener.wait(), 1)
        assert len(errors) == 1 and not done
        matched = await robot.invoke("robot_wait_delay", {"delay_s": 30}, operation_id="retry-conflict")
        assert matched["error"] is None and matched["task_id"] == old["task_id"]
        await runtime.cancel_task(old["task_id"])
        await asyncio.wait_for(listener.wait(), 1)
        assert [task.id for task in done] == [old["task_id"]]
        assert len(errors) == 1


async def test_timeout_listener_validates_the_submitted_snapshot_after_input_mutation() -> None:
    class TimedOutRuntime:
        snapshot: Task

        async def list_skills(self) -> list[SkillDefinition]:
            return [SkillDefinition(
                name="custom.nested", version="1", description="Observe nested input",
                input_schema={"type": "object", "properties": {"nested": {"type": "object"}}},
                resources=set(), permissions=set(), preconditions=set(), timeout_s=1,
                supports_cancel=True,
            )]

        async def submit_and_wait(self, request: TaskRequest, *, timeout_s: float) -> Task:
            self.snapshot = Task(
                id="nested", task_type=request.task_type, steps=request.steps,
                state=TaskState.SUCCEEDED, metadata=request.metadata,
            ).model_copy(deep=True)
            try:
                async with asyncio.timeout(timeout_s):
                    await asyncio.Event().wait()
            except TimeoutError:
                raise TaskWaitTimeout("Submitted task is still pending", {"task_id": self.snapshot.id})
            raise AssertionError("The wait must expire")

        async def get_task_status(self, task_id: str) -> Task:
            assert task_id == self.snapshot.id
            return self.snapshot.model_copy(deep=True)

        async def subscribe_task_events(self, task_id: str) -> AsyncIterator[Event]:
            yield Event(event_id="nested-done", task_id=task_id, sequence=1,
                        event_type="TASK_SUCCEEDED", data={"state": "succeeded"})

    runtime = TimedOutRuntime()
    robot = await RobotCapabilities.create(runtime, wait_timeout_s=0.01)
    done: list[Task] = []
    errors: list[Exception] = []
    listener = robot.add_task_listener(on_done=done.append,
                                       on_error=lambda task_id, error: errors.append(error))
    params: dict[str, Any] = {"nested": {"value": 1}}
    try:
        observed = await robot.invoke("robot_custom_nested", params, operation_id="nested", wait=True)
        assert observed["error"]["code"] == "TASK_WAIT_TIMEOUT"
        params["nested"]["value"] = 2
        await asyncio.wait_for(listener.wait(), 1)
        assert not errors and len(done) == 1
        assert done[0].steps[0].params == {"nested": {"value": 1}}
    finally:
        await robot.close_listeners()


async def _check_input_mutation_during_submission(*, wait: bool, expire_timeout: bool = False) -> None:
    class SnapshotRuntime:
        snapshot: Task

        def __init__(self) -> None:
            self.submitted = asyncio.Event()
            self.release = asyncio.Event()

        async def list_skills(self) -> list[SkillDefinition]:
            return [SkillDefinition(
                name="custom.nested", version="1", description="Observe a submitted snapshot",
                input_schema={"type": "object", "properties": {"nested": {"type": "object"}}},
                resources=set(), permissions=set(), preconditions=set(), timeout_s=1,
                supports_cancel=True,
            )]

        async def submit_task(self, request: TaskRequest) -> Task:
            self.snapshot = Task(
                id="submitted-input", task_type=request.task_type, steps=request.steps,
                state=TaskState.SUCCEEDED, metadata=request.metadata,
            ).model_copy(deep=True)
            self.submitted.set()
            await self.release.wait()
            return self.snapshot.model_copy(deep=True)

        async def submit_and_wait(self, request: TaskRequest, *, timeout_s: float | None = None) -> Task:
            task = await self.submit_task(request)
            if expire_timeout:
                try:
                    async with asyncio.timeout(timeout_s):
                        await asyncio.Event().wait()
                except TimeoutError:
                    pass
            raise TaskWaitTimeout("Reply did not arrive before the backend deadline", {"task_id": task.id})

        async def get_task_status(self, task_id: str) -> Task:
            assert task_id == self.snapshot.id
            return self.snapshot.model_copy(deep=True)

        async def subscribe_task_events(self, task_id: str) -> AsyncIterator[Event]:
            yield Event(event_id="submitted-input-done", task_id=task_id, sequence=1,
                        event_type="TASK_SUCCEEDED", data={"state": "succeeded"})

    runtime = SnapshotRuntime()
    robot = await RobotCapabilities.create(runtime, wait_timeout_s=0.01 if expire_timeout else 5)
    done: list[Task] = []
    events: list[Event] = []
    errors: list[Exception] = []
    listener = robot.add_task_listener(on_done=done.append, on_event=events.append,
                                       on_error=lambda task_id, error: errors.append(error))
    params: dict[str, Any] = {"nested": {"value": 1, "items": [{"value": 1}]}}
    invoking = asyncio.create_task(robot.invoke("robot_custom_nested", params, operation_id="snapshot", wait=wait))
    try:
        await asyncio.wait_for(runtime.submitted.wait(), 1)
        params["nested"]["value"] = 2
        params["nested"]["items"][0]["value"] = 2
        params["nested"]["items"].append({"value": 3})
        runtime.release.set()
        observed = await asyncio.wait_for(invoking, 1)
        if wait:
            assert observed["error"]["code"] == "TASK_WAIT_TIMEOUT"
        else:
            assert observed["error"] is None
        await asyncio.wait_for(listener.wait(), 1)
        assert not errors and [event.event_id for event in events] == ["submitted-input-done"]
        assert len(done) == 1 and done[0].id == observed["task_id"]
        assert done[0].steps[0].params == {"nested": {"value": 1, "items": [{"value": 1}]}}
    finally:
        runtime.release.set()
        if not invoking.done():
            invoking.cancel()
            await asyncio.gather(invoking, return_exceptions=True)
        await robot.close_listeners()


async def test_submission_identity_uses_input_snapshot_before_awaiting_the_reply() -> None:
    await _check_input_mutation_during_submission(wait=False)


async def test_timeout_listener_uses_input_snapshot_before_awaiting_submission() -> None:
    await _check_input_mutation_during_submission(wait=True, expire_timeout=True)


async def test_timeout_recovery_uses_the_same_input_snapshot_after_submission() -> None:
    await _check_input_mutation_during_submission(wait=True)


async def test_input_snapshot_preserves_runtime_rejection_of_non_json_parameters() -> None:
    async with running() as (robot, _, simulator):
        done: list[Task] = []
        listener = robot.add_task_listener(on_done=done.append)
        for index, value in enumerate((float("nan"), object())):
            observed = await robot.invoke("robot_speak", {"message": value},
                                          operation_id=f"invalid-snapshot-{index}", wait=True)
            assert observed["error"]["code"] == "INTERNAL_ERROR"
            assert observed["task_id"] is None
        await listener.wait()
        assert not done and simulator.skills["audio.speak"].calls == 0


async def test_input_snapshot_does_not_invoke_foreign_copy_hooks_before_runtime_rejection() -> None:
    copied: list[object] = []
    consumed: list[int] = []

    class ForeignValue:
        def __deepcopy__(self, memo: dict[int, Any]) -> Any:
            copied.append(self)
            raise TypeError("Foreign copying is forbidden")

    def foreign_generator() -> Any:
        consumed.append(1)
        yield 1

    values = [ForeignValue(), threading.Lock(), foreign_generator()]
    async with running() as (robot, _, simulator):
        for index, value in enumerate(values):
            observed = await robot.invoke("robot_speak", {"message": value},
                                          operation_id=f"foreign-snapshot-{index}")
            assert observed["error"]["code"] == "INTERNAL_ERROR" and observed["task_id"] is None
        assert not copied and not consumed and simulator.skills["audio.speak"].calls == 0


async def test_platform_fault_is_a_normal_event_and_listener_failure_is_separate() -> None:
    fault = PlatformEvent(source="fake", event_type="robot.fault.changed", data={"payload": {"code": 7}})
    original = PlatformEventSourceUnavailable("source disconnected")
    runtime = EventRuntime([fault], error=original)
    robot = await RobotCapabilities.create(runtime)
    events: list[PlatformEvent] = []
    errors: list[Exception] = []
    listener = robot.add_platform_listener(on_event=events.append, on_error=errors.append)
    try:
        await asyncio.wait_for(listener.wait(), 1)
        assert events == [fault] and events[0] is not fault
        assert errors == [original] and listener.closed
    finally:
        await robot.close_listeners()


async def test_platform_listener_default_error_reporting_is_not_silent() -> None:
    original = PlatformEventSourceUnavailable("offline")
    robot = await RobotCapabilities.create(EventRuntime([], error=original))
    contexts: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda owner, context: contexts.append(context))
    try:
        listener = robot.add_platform_listener(on_event=lambda event: None)
        await asyncio.wait_for(listener.wait(), 1)
        assert len(contexts) == 1 and contexts[0]["exception"] is original
    finally:
        await robot.close_listeners()
        loop.set_exception_handler(previous)


async def test_normal_platform_listener_close_does_not_report_an_error() -> None:
    runtime = EventRuntime([], quiet=True)
    robot = await RobotCapabilities.create(runtime)
    errors: list[Exception] = []
    listener = robot.add_platform_listener(on_event=lambda event: None, on_error=errors.append)
    try:
        await asyncio.wait_for(runtime.entered.wait(), 1)
        await asyncio.wait_for(listener.close(), 1)
        await listener.wait()
        assert listener.closed and runtime.closed.is_set() and errors == []
    finally:
        await robot.close_listeners()


async def test_platform_callbacks_support_async_and_synchronous_functions() -> None:
    caller = threading.get_ident()
    source = PlatformEvent(source="fake", event_type="robot.changed")
    robot = await RobotCapabilities.create(EventRuntime([source]))
    threads: list[int] = []
    try:
        sync_listener = robot.add_platform_listener(on_event=lambda event: threads.append(threading.get_ident()))
        await asyncio.wait_for(sync_listener.wait(), 1)

        async def asynchronous(event: PlatformEvent) -> None:
            threads.append(threading.get_ident())

        async_listener = robot.add_platform_listener(on_event=asynchronous)
        await asyncio.wait_for(async_listener.wait(), 1)
        assert threads[0] != caller and threads[1] == caller
    finally:
        await robot.close_listeners()


async def test_a_platform_callback_can_close_its_own_listener() -> None:
    runtime = EventRuntime([PlatformEvent(source="fake", event_type="robot.changed")], quiet=True)
    robot = await RobotCapabilities.create(runtime)
    returned = asyncio.Event()

    async def on_event(event: PlatformEvent) -> None:
        await listener.close()
        returned.set()

    listener = robot.add_platform_listener(on_event=on_event)
    try:
        await asyncio.wait_for(returned.wait(), 1)
        await asyncio.wait_for(listener.wait(), 1)
        assert listener.closed
    finally:
        await robot.close_listeners()


async def test_a_task_callback_can_close_all_capability_listeners() -> None:
    async with running() as (robot, runtime, _):
        returned = asyncio.Event()

        async def on_done(task: Task) -> None:
            await robot.close_listeners()
            returned.set()

        listener = robot.add_task_listener(on_done=on_done)
        await robot.invoke("robot_get_state", {}, operation_id="self-close", wait=True)
        await asyncio.wait_for(returned.wait(), 1)
        assert listener.closed and await runtime.list_skills()


async def test_error_handler_failure_is_reported_once_without_recursion() -> None:
    original = PlatformEventSourceUnavailable("offline")
    robot = await RobotCapabilities.create(EventRuntime([], error=original))
    calls: list[Exception] = []
    contexts: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda owner, context: contexts.append(context))

    def on_error(error: Exception) -> None:
        calls.append(error)
        raise ValueError("reporter failed")

    try:
        listener = robot.add_platform_listener(on_event=lambda event: None, on_error=on_error)
        await asyncio.wait_for(listener.wait(), 1)
        assert calls == [original] and len(contexts) == 1
        assert isinstance(contexts[0]["exception"], ValueError)
    finally:
        await robot.close_listeners()
        loop.set_exception_handler(previous)


async def test_invalid_listener_registration_is_rejected() -> None:
    async with running() as (robot, _, _):
        for add in (robot.add_task_listener, robot.add_platform_listener):
            with pytest.raises(InvalidRequest):
                add()
            with pytest.raises(InvalidRequest):
                add(on_event=object())
        with pytest.raises(InvalidRequest):
            robot.add_task_listener(on_done=lambda task: None, all_tasks=True, task_ids={"a"})
        with pytest.raises(InvalidRequest):
            robot.add_platform_listener(on_event=lambda event: None, after_cursor=-1)
        await robot.close_listeners()
        with pytest.raises(InvalidRequest):
            robot.add_task_listener(on_done=lambda task: None)
        assert (await robot.invoke("robot_get_state", {}, operation_id="after-listener-close", wait=True))["state"] == "succeeded"


async def test_listener_capacity_is_released_when_a_handle_closes() -> None:
    async with running() as (robot, _, _):
        handles = [robot.add_task_listener(on_done=lambda task: None) for _ in range(128)]
        with pytest.raises(ListenerCapacityExceeded):
            robot.add_task_listener(on_done=lambda task: None)
        await handles[0].close()
        replacement = robot.add_task_listener(on_done=lambda task: None)
        assert not replacement.closed


async def test_platform_close_before_consumer_starts_does_not_open_a_subscription() -> None:
    runtime = EventRuntime([], quiet=True)
    robot = await RobotCapabilities.create(runtime)
    listener = robot.add_platform_listener(on_event=lambda event: None)
    try:
        await listener.close()
        await listener.wait()
        assert not runtime.entered.is_set()
    finally:
        await robot.close_listeners()


async def test_global_subscription_error_is_not_attributed_to_the_previous_task() -> None:
    class GlobalRuntime(EventRuntime):
        def subscribe_all_task_events(self, event_types: set[str] | None = None) -> AsyncIterator[Event]:
            return self._stream()

    event = Event(event_id="seen", task_id="earlier", sequence=1, event_type="TASK_RUNNING", data={"state": "running"})
    robot = await RobotCapabilities.create(GlobalRuntime([event], error=RemoteTransportError("reset")))
    errors: list[tuple[str | None, Exception]] = []
    listener = robot.add_task_listener(all_tasks=True, on_event=lambda event: None,
                                       on_error=lambda task_id, error: errors.append((task_id, error)))
    try:
        await asyncio.wait_for(listener.wait(), 1)
        assert len(errors) == 1 and errors[0][0] is None
    finally:
        await robot.close_listeners()


async def test_global_task_listener_observes_other_submissions_and_fetches_snapshot() -> None:
    async with running() as (robot, runtime, _):
        completed = asyncio.Event()
        done: list[Task] = []

        async def on_done(task: Task) -> None:
            done.append(task)
            completed.set()

        listener = robot.add_task_listener(all_tasks=True, on_done=on_done)

        async def registered() -> bool:
            return bool(runtime._core._events._all_subscribers)

        async with asyncio.timeout(1):
            while not await runtime._host._loop.call(registered):
                pass
        outside = await runtime.submit_and_wait(TaskRequest(task_type="outside", steps=[SkillCall(skill="robot.get_state")]))
        await asyncio.wait_for(completed.wait(), 1)
        assert done[0].id == outside.id and done[0].result == outside.result
        await listener.close()


async def test_task_done_filter_does_not_hide_the_terminal_snapshot() -> None:
    async with running() as (robot, _, _):
        events: list[Event] = []
        done: list[Task] = []
        listener = robot.add_task_listener(on_event=events.append, on_done=done.append, event_types={"TASK_CREATED"})
        await robot.invoke("robot_get_state", {}, operation_id="filter", wait=True)
        await asyncio.wait_for(listener.wait(), 1)
        assert [event.event_type for event in events] == ["TASK_CREATED"]
        assert len(done) == 1 and done[0].state.value == "succeeded"


async def test_closing_callbacks_does_not_wait_for_running_synchronous_code() -> None:
    release = threading.Event()
    started, finished = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    robot = await RobotCapabilities.create(EventRuntime([PlatformEvent(source="fake", event_type="robot.changed")], quiet=True))

    def blocking(event: PlatformEvent) -> None:
        loop.call_soon_threadsafe(started.set)
        release.wait(2)
        loop.call_soon_threadsafe(finished.set)

    listener = robot.add_platform_listener(on_event=blocking)
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(robot.close_listeners(), 1)
        assert listener.closed and not release.is_set()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await robot.close_listeners()


async def test_cancelling_listener_wait_does_not_cancel_observation() -> None:
    runtime = EventRuntime([], quiet=True)
    robot = await RobotCapabilities.create(runtime)
    listener = robot.add_platform_listener(on_event=lambda event: None)
    waiting = asyncio.create_task(listener.wait())
    try:
        await asyncio.wait_for(runtime.entered.wait(), 1)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not listener.closed and not runtime.closed.is_set()
    finally:
        await robot.close_listeners()


async def test_platform_callback_can_close_all_listeners() -> None:
    robot = await RobotCapabilities.create(EventRuntime([PlatformEvent(source="fake", event_type="robot.changed")], quiet=True))
    completed = asyncio.Event()

    async def on_event(event: PlatformEvent) -> None:
        await robot.close_listeners()
        completed.set()

    listener = robot.add_platform_listener(on_event=on_event)
    try:
        await asyncio.wait_for(completed.wait(), 1)
        assert listener.closed
    finally:
        await robot.close_listeners()
