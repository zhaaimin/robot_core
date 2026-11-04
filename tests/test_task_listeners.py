import asyncio
import tempfile
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine, Generator
from concurrent.futures import Future
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.core import RuntimeCore
from robot_agent_runtime.errors import (
    EventStreamOverflow,
    InvalidRequest,
    ListenerCallbackError,
    ListenerCallbackQueueFull,
    ListenerCallbackTimeout,
    TaskEventStreamEnded,
)
from robot_agent_runtime.models import Event, SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sdk import RemoteRuntime, RobotRuntime
from robot_agent_runtime.sdk.callback_executor import CallbackExecutor
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.server import RuntimeServer


def request(*, key: str | None = None, skill: str = "robot.get_state") -> TaskRequest:
    return TaskRequest(task_type="listener", idempotency_key=key, steps=[SkillCall(skill=skill)])


def long_request() -> TaskRequest:
    return TaskRequest(task_type="listener", steps=[
        SkillCall(skill="wait.delay", params={"delay_s": 30}),
    ])


async def wait_for_terminal(runtime: RobotRuntime, task_id: str) -> Task:
    async with asyncio.timeout(1):
        while True:
            task = await runtime.get_task_status(task_id)
            if task.state.is_terminal:
                return task
            await asyncio.sleep(0)


@asynccontextmanager
async def embedded() -> AsyncIterator[RobotRuntime]:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.close()


@asynccontextmanager
async def remote_pair() -> AsyncIterator[tuple[RemoteRuntime, RemoteRuntime]]:
    with tempfile.TemporaryDirectory(prefix="rar-listener-") as directory:
        socket_path = str(Path(directory) / "s.sock")
        server = RuntimeServer(RuntimeCore(InMemoryTaskRepository(), Simulator().skills), socket_path)
        await server.start()
        first, second = RemoteRuntime(socket_path), RemoteRuntime(socket_path)
        try:
            await first.start()
            await second.start()
            yield first, second
        finally:
            await first.close()
            await second.close()
            await server.close()


async def test_listener_delivers_ordered_events_then_results_for_multiple_tasks() -> None:
    async with embedded() as runtime:
        events: dict[str, list[Event]] = {}
        results: dict[str, Task] = {}

        def on_event(event: Event) -> None:
            events.setdefault(event.task_id, []).append(event)

        def on_done(task: Task) -> None:
            assert TaskState(events[task.id][-1].data["state"]).is_terminal
            results[task.id] = task

        listener = runtime.add_task_listener(on_event=on_event, on_done=on_done)
        first = await runtime.submit_task(request())
        second = await runtime.submit_task(request())
        await asyncio.wait_for(listener.wait(), 1)
        assert set(results) == {first.id, second.id}
        for task_id, final in results.items():
            assert final.state == TaskState.SUCCEEDED
            assert final.result is not None
            assert events[task_id][0].event_type == "TASK_CREATED"
            assert [event.sequence for event in events[task_id]] == list(range(1, len(events[task_id]) + 1))


async def test_listener_reports_task_failure_via_done_not_error() -> None:
    async with embedded() as runtime:
        results: list[Task] = []
        errors: list[Exception] = []
        listener = runtime.add_task_listener(
            on_done=results.append, on_error=lambda task_id, error: errors.append(error),
        )
        task = await runtime.submit_task(request(skill="missing"))
        await asyncio.wait_for(listener.wait(), 1)
        assert results[0].id == task.id
        assert results[0].state == TaskState.FAILED
        assert results[0].error.code == "SKILL_NOT_FOUND"
        assert not errors


async def test_listener_deduplicates_repeated_idempotent_submissions() -> None:
    async with embedded() as runtime:
        results: list[Task] = []
        listener = runtime.add_task_listener(on_done=results.append)
        first = await runtime.submit_task(request(key="same"))
        await runtime.submit_task(request(key="same"))
        await asyncio.wait_for(listener.wait(), 1)
        await runtime.submit_task(request(key="same"))
        await asyncio.wait_for(listener.wait(), 1)
        assert [task.id for task in results] == [first.id]


async def test_listener_late_registration_does_not_scan_old_tasks() -> None:
    async with embedded() as runtime:
        old = await runtime.submit_task(request())
        async with asyncio.timeout(1):
            async for _ in runtime.subscribe_task_events(old.id):
                pass
        results: list[Task] = []
        listener = runtime.add_task_listener(on_done=results.append)
        await listener.wait()
        assert not results
        new = await runtime.submit_task(request())
        await asyncio.wait_for(listener.wait(), 1)
        assert [task.id for task in results] == [new.id]


async def test_listener_async_callback_does_not_delay_other_tasks_or_submit() -> None:
    async with embedded() as runtime:
        entered, release, second_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        first_id: str | None = None
        results: list[str] = []

        async def on_event(event: Event) -> None:
            nonlocal first_id
            if first_id is None:
                first_id = event.task_id
            if event.task_id == first_id and not release.is_set():
                entered.set()
                await release.wait()

        async def on_done(task: Task) -> None:
            results.append(task.id)
            if task.id != first_id:
                second_done.set()

        listener = runtime.add_task_listener(on_event=on_event, on_done=on_done)
        first = await runtime.submit_task(request())
        await asyncio.wait_for(entered.wait(), 1)
        second = await runtime.submit_task(request())
        await asyncio.wait_for(second_done.wait(), 1)
        assert results == [second.id]
        release.set()
        await asyncio.wait_for(listener.wait(), 1)
        assert results == [second.id, first.id]


async def test_listener_callback_failure_isolated_from_task_and_other_listeners() -> None:
    async with embedded() as runtime:
        errors: list[tuple[str, Exception]] = []
        results: list[Task] = []

        def broken(event: Event) -> None:
            raise ValueError("display failed")

        async def on_error(task_id: str, error: Exception) -> None:
            errors.append((task_id, error))

        bad = runtime.add_task_listener(on_event=broken, on_error=on_error)
        good = runtime.add_task_listener(on_done=results.append)
        task = await runtime.submit_task(request())
        await asyncio.wait_for(asyncio.gather(bad.wait(), good.wait()), 1)
        assert len(errors) == 1
        assert errors[0][0] == task.id
        assert isinstance(errors[0][1], ValueError)
        assert results[0].state == TaskState.SUCCEEDED


async def test_listener_close_only_stops_notifications_not_execution() -> None:
    async with embedded() as runtime:
        loop = asyncio.get_running_loop()
        running = asyncio.Event()
        events: list[Event] = []
        results: list[Task] = []

        def on_event(event: Event) -> None:
            events.append(event)
            if event.event_type == "SKILL_STARTED":
                loop.call_soon_threadsafe(running.set)

        listener = runtime.add_task_listener(on_event=on_event, on_done=results.append)
        task = await runtime.submit_task(long_request())
        await asyncio.wait_for(running.wait(), 1)
        await asyncio.wait_for(listener.close(), 1)
        count = len(events)
        assert (await runtime.get_task_status(task.id)).state == TaskState.RUNNING
        await runtime.cancel_task(task.id)
        await runtime.submit_task(request())
        await listener.wait()
        await listener.close()
        assert len(events) == count
        assert not results
        assert not runtime._core._events._subscribers


async def test_listener_receives_cancelled_task_result() -> None:
    async with embedded() as runtime:
        results: list[Task] = []
        listener = runtime.add_task_listener(on_done=results.append)
        task = await runtime.submit_task(long_request())
        await runtime.cancel_task(task.id)
        await asyncio.wait_for(listener.wait(), 1)
        assert results[0].state == TaskState.CANCELED


async def test_listener_handles_can_close_from_their_own_callback() -> None:
    async with embedded() as runtime:
        called = asyncio.Event()

        async def on_event(event: Event) -> None:
            await listener.close()
            called.set()

        listener = runtime.add_task_listener(on_event=on_event)
        await runtime.submit_task(request())
        await asyncio.wait_for(called.wait(), 1)
        await asyncio.wait_for(listener.wait(), 1)
        assert not runtime._core._events._subscribers


async def test_listener_sdk_close_cleans_blocked_callback() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    entered, finished = asyncio.Event(), asyncio.Event()

    async def on_event(event: Event) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    listener = runtime.add_task_listener(on_event=on_event)
    await runtime.submit_task(request())
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(runtime.close(), 1)
    assert finished.is_set()
    await asyncio.wait_for(listener.wait(), 1)
    await runtime.close()


async def test_remote_listener_observes_only_this_clients_submissions() -> None:
    async with remote_pair() as (first, second):
        results: list[Task] = []
        events: list[Event] = []
        listener = first.add_task_listener(on_event=events.append, on_done=results.append)
        await second.submit_task(request())
        owned = await first.submit_task(request())
        await asyncio.wait_for(listener.wait(), 1)
        assert [task.id for task in results] == [owned.id]
        assert {event.task_id for event in events} == {owned.id}
        assert events[0].event_type == "TASK_CREATED"
        assert events[-1].event_type == "TASK_SUCCEEDED"


async def test_remote_listener_close_does_not_cancel_server_task() -> None:
    async with remote_pair() as (first, second):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def on_event(event: Event) -> None:
            if event.event_type == "SKILL_STARTED":
                loop.call_soon_threadsafe(entered.set)

        listener = first.add_task_listener(on_event=on_event)
        task = await first.submit_task(long_request())
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(first.close(), 1)
        await asyncio.wait_for(listener.wait(), 1)
        assert not first._writers
        assert (await second.get_task_status(task.id)).state == TaskState.RUNNING
        await second.cancel_task(task.id)


async def test_listener_registration_rejects_no_callbacks() -> None:
    async with embedded() as runtime:
        with pytest.raises(InvalidRequest):
            runtime.add_task_listener()


async def test_listener_wait_cancellation_preserves_background_subscription() -> None:
    async with embedded() as runtime:
        entered, release = asyncio.Event(), asyncio.Event()
        results: list[Task] = []

        async def on_event(event: Event) -> None:
            entered.set()
            await release.wait()

        listener = runtime.add_task_listener(on_event=on_event, on_done=results.append)
        task = await runtime.submit_task(request())
        await asyncio.wait_for(entered.wait(), 1)
        waiter = asyncio.create_task(listener.wait())
        started_wait = asyncio.Event()
        asyncio.get_running_loop().call_soon(started_wait.set)
        await started_wait.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await asyncio.wait_for(listener.wait(), 1)
        assert [final.id for final in results] == [task.id]


async def test_listener_one_tasks_callback_failure_does_not_stop_other_tasks() -> None:
    async with embedded() as runtime:
        failed: list[str] = []
        completed: list[str] = []
        first_id: str | None = None

        def on_event(event: Event) -> None:
            nonlocal first_id
            if first_id is None:
                first_id = event.task_id
            if event.task_id == first_id:
                raise ValueError("only the first task display fails")

        listener = runtime.add_task_listener(
            on_event=on_event, on_done=lambda task: completed.append(task.id),
            on_error=lambda task_id, error: failed.append(task_id),
        )
        first = await runtime.submit_task(request())
        second = await runtime.submit_task(request())
        await asyncio.wait_for(listener.wait(), 1)
        assert failed == [first.id]
        assert completed == [second.id]
        assert (await runtime.get_task_status(first.id)).state == TaskState.SUCCEEDED


async def test_listener_event_mutation_does_not_change_other_observers() -> None:
    async with embedded() as runtime:
        events: list[Event] = []
        results: list[Task] = []

        def mutate(event: Event) -> None:
            event.data["state"] = "corrupted"
            event.event_type = "corrupted"

        first = runtime.add_task_listener(on_event=mutate)
        second = runtime.add_task_listener(on_event=events.append, on_done=results.append)
        task = await runtime.submit_task(request())
        await asyncio.wait_for(asyncio.gather(first.wait(), second.wait()), 1)
        assert all(event.event_type != "corrupted" for event in events)
        assert results[0].id == task.id
        assert results[0].state == TaskState.SUCCEEDED


async def test_listener_early_stream_end_reports_error_instead_of_completion() -> None:
    class TruncatedRuntime(RobotRuntime):
        async def subscribe_task_events(
            self, task_id: str, after_sequence: int = 0
        ) -> AsyncGenerator[Event, None]:
            stream = super().subscribe_task_events(task_id, after_sequence)
            try:
                yield await anext(stream)
            finally:
                await stream.aclose()

    runtime = TruncatedRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    try:
        errors: list[tuple[str, Exception]] = []
        results: list[Task] = []
        listener = runtime.add_task_listener(
            on_done=results.append, on_error=lambda task_id, error: errors.append((task_id, error)),
        )
        task = await runtime.submit_task(request())
        await asyncio.wait_for(listener.wait(), 1)
        assert not results
        assert len(errors) == 1
        assert errors[0][0] == task.id
        assert isinstance(errors[0][1], TaskEventStreamEnded)
    finally:
        await runtime.close()


async def test_listener_subscription_error_is_delivered_with_task_id() -> None:
    class OverflowRuntime(RobotRuntime):
        async def subscribe_task_events(
            self, task_id: str, after_sequence: int = 0
        ) -> AsyncGenerator[Event, None]:
            stream = super().subscribe_task_events(task_id, after_sequence)
            try:
                yield await anext(stream)
                raise EventStreamOverflow("injected overflow")
            finally:
                await stream.aclose()

    runtime = OverflowRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    try:
        errors: list[tuple[str, Exception]] = []
        listener = runtime.add_task_listener(on_error=lambda task_id, error: errors.append((task_id, error)))
        task = await runtime.submit_task(request())
        await asyncio.wait_for(listener.wait(), 1)
        assert errors[0][0] == task.id
        assert isinstance(errors[0][1], EventStreamOverflow)
        assert not runtime._core._events._subscribers
    finally:
        await runtime.close()


async def test_listener_error_handler_failure_is_reported_to_event_loop() -> None:
    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()
    reported: list[dict[str, object]] = []
    loop.set_exception_handler(lambda loop, context: reported.append(context))
    try:
        async with embedded() as runtime:
            def on_event(event: Event) -> None:
                raise ValueError("event callback failed")

            def on_error(task_id: str, error: Exception) -> None:
                raise LookupError("error callback failed")

            listener = runtime.add_task_listener(on_event=on_event, on_error=on_error)
            task = await runtime.submit_task(request())
            await asyncio.wait_for(listener.wait(), 1)
            assert len(reported) == 1
            assert reported[0]["task_id"] == task.id
            assert isinstance(reported[0]["exception"], LookupError)
    finally:
        loop.set_exception_handler(original)


async def test_listener_callback_can_close_sdk_without_deadlock() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    entered = asyncio.Event()

    async def on_event(event: Event) -> None:
        entered.set()
        await runtime.close()

    runtime.add_task_listener(on_event=on_event)
    await runtime.submit_task(request())
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(runtime.close(), 1)
    assert not runtime._core._events._subscribers
    with pytest.raises(InvalidRequest):
        runtime.add_task_listener(on_event=on_event)


async def test_listener_wait_inside_own_callback_is_rejected() -> None:
    async with embedded() as runtime:
        rejected = asyncio.Event()
        errors: list[tuple[str, Exception]] = []

        async def on_event(event: Event) -> None:
            with pytest.raises(InvalidRequest):
                await listener.wait()
            rejected.set()

        listener = runtime.add_task_listener(
            on_event=on_event,
            on_error=lambda task_id, error: errors.append((task_id, error)),
        )
        task = await runtime.submit_task(request())
        await asyncio.wait_for(rejected.wait(), 1)
        await asyncio.wait_for(listener.wait(), 1)
        assert not errors
        assert (await runtime.get_task_status(task.id)).state == TaskState.SUCCEEDED


async def test_listener_error_without_handler_is_reported_to_event_loop() -> None:
    loop = asyncio.get_running_loop()
    original = loop.get_exception_handler()
    reported: list[dict[str, object]] = []
    loop.set_exception_handler(lambda loop, context: reported.append(context))
    try:
        async with embedded() as runtime:
            def on_event(event: Event) -> None:
                raise ValueError("no error handler registered")

            listener = runtime.add_task_listener(on_event=on_event)
            task = await runtime.submit_task(request())
            await asyncio.wait_for(listener.wait(), 1)
            assert len(reported) == 1
            assert reported[0]["task_id"] == task.id
            assert isinstance(reported[0]["exception"], ValueError)
    finally:
        loop.set_exception_handler(original)


async def test_listener_sync_blocking_callback_does_not_stall_kernel() -> None:
    async with embedded() as runtime:
        loop = asyncio.get_running_loop()
        entered, second_done = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        first_id: str | None = None

        def blocking(event: Event) -> None:
            if event.task_id == first_id and event.sequence == 1:
                loop.call_soon_threadsafe(entered.set)
                release.wait()

        async def on_done(task: Task) -> None:
            if task.id != first_id:
                second_done.set()

        listener = runtime.add_task_listener(on_event=blocking, on_done=on_done)
        first_id = (await runtime.submit_task(request())).id
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = await runtime.submit_task(request())
            await asyncio.wait_for(second_done.wait(), 1)
            assert (await runtime.get_task_status(second.id)).state == TaskState.SUCCEEDED
        finally:
            release.set()
        await asyncio.wait_for(listener.wait(), 1)


async def test_listener_close_does_not_wait_for_running_sync_callback() -> None:
    async with embedded() as runtime:
        loop = asyncio.get_running_loop()
        entered, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()

        def blocking(event: Event) -> None:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            loop.call_soon_threadsafe(finished.set)

        runtime.add_task_listener(on_event=blocking)
        await runtime.submit_task(request())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(runtime.close(), 1)
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 1)


async def test_listener_async_callable_runs_on_owning_loop() -> None:
    loop = asyncio.get_running_loop()
    observed: list[Event | Task] = []

    class Receiver:
        async def __call__(self, value: Event | Task) -> None:
            assert asyncio.get_running_loop() is loop
            observed.append(value)

    async with embedded() as runtime:
        listener = runtime.add_task_listener(on_event=Receiver(), on_done=Receiver())
        task = await runtime.submit_task(request())
        await asyncio.wait_for(listener.wait(), 1)
        assert isinstance(observed[0], Event)
        assert isinstance(observed[-1], Task)
        assert observed[-1].id == task.id


async def test_listener_sync_wrapper_awaits_returned_awaitable_on_loop() -> None:
    loop = asyncio.get_running_loop()
    for custom in (False, True):
        observed: list[str] = []

        async def consume(event: Event) -> None:
            assert asyncio.get_running_loop() is loop
            observed.append(event.event_type)

        class CustomAwaitable:
            def __init__(self, event: Event) -> None:
                self.event = event

            def __await__(self) -> Generator[Any, None, None]:
                return consume(self.event).__await__()

        def callback(event: Event) -> Awaitable[None]:
            return CustomAwaitable(event) if custom else consume(event)

        async with embedded() as runtime:
            listener = runtime.add_task_listener(on_event=callback)
            await runtime.submit_task(request())
            await asyncio.wait_for(listener.wait(), 1)
            assert observed[0] == "TASK_CREATED"
            assert observed[-1] == "TASK_SUCCEEDED"


async def test_listener_callback_self_cancellation_reports_structured_error() -> None:
    def sync_cancel(event: Event) -> None:
        raise asyncio.CancelledError("callback cancelled itself")

    async def async_cancel(event: Event) -> None:
        raise asyncio.CancelledError("callback cancelled itself")

    for callback in (sync_cancel, async_cancel):
        async with embedded() as runtime:
            errors: list[Exception] = []

            async def on_error(task_id: str, error: Exception) -> None:
                errors.append(error)

            listener = runtime.add_task_listener(on_event=callback, on_error=on_error)
            task = await runtime.submit_task(request())
            await asyncio.wait_for(listener.wait(), 1)
            assert len(errors) == 1
            assert isinstance(errors[0], ListenerCallbackError)
            assert (await wait_for_terminal(runtime, task.id)).state == TaskState.SUCCEEDED


async def test_listener_close_cancels_callback_waiting_for_a_worker() -> None:
    loop = asyncio.get_running_loop()
    entered, submitted = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    progressed = threading.Event()
    events: list[Event] = []

    class ObservedExecutor(CallbackExecutor):
        def submit(self, callback: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
            future = super().submit(callback, *args)
            submitted.set()
            return future

    class ReleasingTask(asyncio.Task[None]):
        def cancel(self, msg: str | None = None) -> bool:
            cancelled = super().cancel(msg)
            release.set()
            assert progressed.wait(1)
            return cancelled

    def task_factory(
        loop: asyncio.AbstractEventLoop, coroutine: Coroutine[Any, Any, None], **kwargs: Any,
    ) -> asyncio.Task[None]:
        if getattr(coroutine, "__name__", "") == "_observe":
            return ReleasingTask(coroutine, loop=loop, **kwargs)
        return asyncio.Task(coroutine, loop=loop, **kwargs)

    executor = ObservedExecutor(max_workers=1)

    def block() -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()

    running = executor.submit(block)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        submitted.clear()
        async with embedded() as runtime:
            runtime._listeners._executor = executor
            listener = runtime.add_task_listener(on_event=events.append)
            original_factory = loop.get_task_factory()
            loop.set_task_factory(task_factory)
            try:
                await runtime.submit_task(request())
            finally:
                loop.set_task_factory(original_factory)
            await asyncio.wait_for(submitted.wait(), 1)
            marker = executor.submit(progressed.set)
            await asyncio.wait_for(listener.close(), 1)
            await asyncio.wait_for(asyncio.gather(running, marker), 1)
            assert not events
    finally:
        release.set()
        await executor.close()


async def test_listener_overload_reports_without_cancelling_business_task() -> None:
    loop = asyncio.get_running_loop()
    for sync_error_handler in (False, True):
        entered = asyncio.Event()
        release = threading.Event()
        errors: list[Exception] = []
        reported: list[dict[str, Any]] = []
        executor = CallbackExecutor(max_workers=1, max_pending=1)

        def block() -> None:
            loop.call_soon_threadsafe(entered.set)
            release.wait()

        running = executor.submit(block)
        original_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: reported.append(context))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            filler = executor.submit(lambda: None)

            def on_sync_error(task_id: str, error: Exception) -> None:
                errors.append(error)

            async def on_async_error(task_id: str, error: Exception) -> None:
                errors.append(error)

            async with embedded() as runtime:
                runtime._listeners._executor = executor
                listener = runtime.add_task_listener(
                    on_event=lambda event: None,
                    on_error=on_sync_error if sync_error_handler else on_async_error,
                )
                task = await runtime.submit_task(request())
                await asyncio.wait_for(listener.wait(), 1)
                assert (await wait_for_terminal(runtime, task.id)).state == TaskState.SUCCEEDED
                if sync_error_handler:
                    assert not errors
                    assert len(reported) == 1
                    assert isinstance(reported[0]["exception"], ListenerCallbackQueueFull)
                else:
                    assert len(errors) == 1
                    assert isinstance(errors[0], ListenerCallbackQueueFull)
                    assert not reported
                release.set()
                await asyncio.wait_for(asyncio.gather(running, filler), 1)
        finally:
            release.set()
            await executor.close()
            loop.set_exception_handler(original_handler)


async def test_listener_queue_timeout_reports_without_late_notification() -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    executor = CallbackExecutor(max_workers=1, queue_timeout_s=0.02)
    events: list[Event] = []
    errors: list[Exception] = []

    def block() -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()

    async def on_error(task_id: str, error: Exception) -> None:
        errors.append(error)

    running = executor.submit(block)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with embedded() as runtime:
            runtime._listeners._executor = executor
            listener = runtime.add_task_listener(on_event=events.append, on_error=on_error)
            await runtime.submit_task(request())
            await asyncio.wait_for(listener.wait(), 1)
            assert len(errors) == 1
            assert isinstance(errors[0], ListenerCallbackTimeout)
            release.set()
            await asyncio.wait_for(running, 1)
            await executor.submit(lambda: None)
            assert not events
    finally:
        release.set()
        await executor.close()


async def test_listener_cancellation_after_delivery_closes_unclaimed_coroutine() -> None:
    returned: list[Coroutine[Any, Any, None]] = []
    errors: list[Exception] = []

    class Suspend:
        def __await__(self) -> Generator[None, None, None]:
            yield None

    class CancellingExecutor(CallbackExecutor):
        def submit(self, callback: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
            owner = asyncio.current_task()
            assert owner is not None
            future = super().submit(callback, *args)
            future.add_done_callback(lambda done: owner.cancel())
            return future

    async def consume(event: Event) -> None:
        try:
            await Suspend()
            raise AssertionError("Cancelled listener must not resume the coroutine")
        finally:
            raise ValueError("unclaimed coroutine cleanup failed")

    def callback(event: Event) -> Awaitable[None]:
        result = consume(event)
        result.send(None)
        returned.append(result)
        return result

    async def on_error(task_id: str, error: Exception) -> None:
        errors.append(error)

    try:
        async with embedded() as runtime:
            runtime._listeners._executor = CancellingExecutor(max_workers=1)
            listener = runtime.add_task_listener(on_event=callback, on_error=on_error)
            await runtime.submit_task(request())
            await asyncio.wait_for(listener.wait(), 1)
            assert len(returned) == 1
            assert returned[0].cr_frame is None
            assert not errors
    finally:
        for result in returned:
            result.close()


async def test_remote_listener_failure_and_idempotency_match_embedded() -> None:
    async with remote_pair() as (runtime, _):
        results: list[Task] = []
        errors: list[Exception] = []
        listener = runtime.add_task_listener(
            on_done=results.append, on_error=lambda task_id, error: errors.append(error),
        )
        task = await runtime.submit_task(request(skill="missing", key="same"))
        await asyncio.wait_for(listener.wait(), 1)
        await runtime.submit_task(request(skill="missing", key="same"))
        await asyncio.wait_for(listener.wait(), 1)
        assert [final.id for final in results] == [task.id]
        assert results[0].state == TaskState.FAILED
        assert results[0].error.code == "SKILL_NOT_FOUND"
        assert not errors


async def test_sdk_close_stops_kernel_before_a_callback_confirms_cancellation() -> None:
    runtime = RobotRuntime(
        InMemoryTaskRepository(), Simulator().skills,
        options=RuntimeOptions(cancel_grace_s=0.02),
    )
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delivered: list[int] = []

    async def stubborn(event: Event) -> None:
        delivered.append(event.sequence)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()

    await runtime.start()
    listener = runtime.add_task_listener(on_event=stubborn)
    await runtime.submit_task(long_request())
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await asyncio.wait_for(runtime.close(), 0.5)
        assert runtime._core._closed
        assert cancelled.is_set()
        assert len(delivered) == 1
    finally:
        release.set()
        await asyncio.wait_for(listener.wait(), 1)
        await runtime.close()


async def test_sdk_can_close_from_another_loop_with_an_active_listener() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    closed: Future[None] = Future()

    async def on_event(event: Event) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    await runtime.start()
    listener = runtime.add_task_listener(on_event=on_event)
    await runtime.submit_task(long_request())
    await asyncio.wait_for(entered.wait(), 1)

    def close_elsewhere() -> None:
        try:
            asyncio.run(runtime.close())
        except BaseException as error:
            closed.set_exception(error)
        else:
            closed.set_result(None)

    threading.Thread(target=close_elsewhere, daemon=True).start()
    await asyncio.wait_for(asyncio.wrap_future(closed), 1)
    assert cancelled.is_set()
    await listener.wait()
    await runtime.close()


async def test_remote_close_bounds_a_callback_that_suppresses_cancellation() -> None:
    async with remote_pair() as (runtime, _):
        entered, release = asyncio.Event(), asyncio.Event()

        async def stubborn(event: Event) -> None:
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        listener = runtime.add_task_listener(on_event=stubborn)
        await runtime.submit_task(long_request())
        await asyncio.wait_for(entered.wait(), 1)
        try:
            await asyncio.wait_for(runtime.close(), 0.5)
            assert runtime._closed
            assert not runtime._writers
        finally:
            release.set()
            await asyncio.wait_for(listener.wait(), 1)


async def test_sdk_close_from_its_callback_can_return_to_that_callback() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    finished = asyncio.Event()

    async def on_event(event: Event) -> None:
        await runtime.close()
        finished.set()

    await runtime.start()
    runtime.add_task_listener(on_event=on_event)
    await runtime.submit_task(long_request())
    try:
        await asyncio.wait_for(finished.wait(), 1)
        assert runtime._core._closed
    finally:
        await runtime.close()


async def test_cross_loop_close_abandons_a_running_synchronous_callback() -> None:
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    closed: Future[None] = Future()
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)

    def callback(event: Event) -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        loop.call_soon_threadsafe(finished.set)

    await runtime.start()
    runtime.add_task_listener(on_event=callback)
    await runtime.submit_task(long_request())
    await asyncio.wait_for(entered.wait(), 1)

    def close_elsewhere() -> None:
        try:
            asyncio.run(runtime.close())
        except BaseException as error:
            closed.set_exception(error)
        else:
            closed.set_result(None)

    threading.Thread(target=close_elsewhere, daemon=True).start()
    try:
        await asyncio.wait_for(asyncio.wrap_future(closed), 1)
        assert runtime._core._closed
        assert not finished.is_set()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await runtime.close()
