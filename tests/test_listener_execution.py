import asyncio
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import (
    InvalidRequest, ListenerCallbackError, ListenerCallbackQueueFull,
    ListenerCallbackTimeout, ListenerCapacityExceeded,
)
from robot_agent_runtime.models import Event, SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RemoteRuntime, RemoteRuntimeError, RemoteTransportError, RobotRuntime
from robot_agent_runtime.sdk.listener_execution import CallbackLoop
from robot_agent_runtime.sdk.listeners import TaskListeners
from robot_agent_runtime.hosting.thread_runtime import LoopThread
from robot_agent_runtime.transport.server import RuntimeServer


def request() -> TaskRequest:
    return TaskRequest(task_type="execution", steps=[SkillCall(skill="robot.get_state")])


@asynccontextmanager
async def embedded(options: RuntimeOptions | None = None) -> AsyncIterator[RobotRuntime]:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills, options=options)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.close()


@asynccontextmanager
async def remote() -> AsyncIterator[RemoteRuntime]:
    with tempfile.TemporaryDirectory(prefix="listener-execution-") as directory:
        server = RuntimeServer(
            InMemoryTaskRepository(), Simulator().skills, str(Path(directory) / "s.sock"),
        )
        await server.start()
        runtime = RemoteRuntime(str(Path(directory) / "s.sock"))
        try:
            await runtime.start()
            yield runtime
        finally:
            await runtime.close()
            await server.close()


async def test_explicit_caller_keeps_async_callbacks_on_submitters_loop() -> None:
    caller = asyncio.get_running_loop()
    observed: list[asyncio.AbstractEventLoop] = []

    async def on_event(event: Event) -> None:
        observed.append(asyncio.get_running_loop())

    async with embedded() as runtime:
        listener = runtime.add_task_listener(on_event=on_event, execution="caller")
        await runtime.submit_task(request())
        await asyncio.wait_for(listener.wait(), 1)
    assert observed and all(loop is caller for loop in observed)


async def test_group_shares_one_stable_loop_for_all_callback_kinds() -> None:
    caller = asyncio.get_running_loop()
    loops: list[asyncio.AbstractEventLoop] = []
    threads: list[int] = []
    events: list[int] = []
    finals: list[Task] = []

    async def on_event(event: Event) -> None:
        loops.append(asyncio.get_running_loop())
        threads.append(threading.get_ident())
        events.append(event.sequence)

    def on_done(task: Task) -> None:
        loops.append(asyncio.get_running_loop())
        threads.append(threading.get_ident())
        finals.append(task)

    async with embedded() as runtime:
        first = runtime.add_task_listener(on_event=on_event, execution="group", group="ui")
        second = runtime.add_task_listener(on_done=on_done, execution="group", group="ui")
        await runtime.submit_task(request())
        await asyncio.wait_for(asyncio.gather(first.wait(), second.wait()), 2)
    assert loops and len(set(loops)) == 1 and caller not in loops
    assert len(set(threads)) == 1 and threading.get_ident() not in threads
    assert events == list(range(1, len(events) + 1))
    assert finals[0].state == TaskState.SUCCEEDED


async def test_dedicated_and_different_groups_have_distinct_owner_loops() -> None:
    observed: dict[str, asyncio.AbstractEventLoop] = {}

    def receiver(name: str) -> Any:
        async def on_done(task: Task) -> None:
            observed[name] = asyncio.get_running_loop()
        return on_done

    async with embedded() as runtime:
        listeners = [
            runtime.add_task_listener(on_done=receiver("a"), execution="dedicated"),
            runtime.add_task_listener(on_done=receiver("b"), execution="dedicated"),
            runtime.add_task_listener(on_done=receiver("c"), execution="group", group="c"),
            runtime.add_task_listener(on_done=receiver("d"), execution="group", group="d"),
        ]
        await runtime.submit_task(request())
        await asyncio.wait_for(asyncio.gather(*(item.wait() for item in listeners)), 2)
    assert len(set(observed.values())) == 4
    assert asyncio.get_running_loop() not in observed.values()


async def test_blocking_async_dedicated_callback_does_not_block_other_listeners() -> None:
    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    observed: list[Task] = []

    async def blocking(event: Event) -> None:
        caller.call_soon_threadsafe(entered.set)
        assert release.wait(3), "Test did not release the isolated callback"

    async with embedded() as runtime:
        first = runtime.add_task_listener(on_event=blocking, execution="dedicated")
        second = runtime.add_task_listener(on_done=observed.append, execution="dedicated")
        third = runtime.add_task_listener(on_done=observed.append)
        try:
            await runtime.submit_task(request())
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(asyncio.gather(second.wait(), third.wait()), 1)
            assert len(observed) == 2
        finally:
            release.set()
        await asyncio.wait_for(first.wait(), 1)


async def test_blocking_group_affects_same_group_but_not_a_different_group() -> None:
    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    same_seen = threading.Event()

    def blocking(event: Event) -> None:
        caller.call_soon_threadsafe(entered.set)
        assert release.wait(3), "Test did not release the group"

    async with embedded() as runtime:
        first = runtime.add_task_listener(on_event=blocking, execution="group", group="slow")
        try:
            await runtime.submit_task(request())
            await asyncio.wait_for(entered.wait(), 1)
            same = runtime.add_task_listener(
                on_done=lambda task: same_seen.set(), execution="group", group="slow",
            )
            other = runtime.add_task_listener(
                on_done=lambda task: None, execution="group", group="fast",
            )
            await runtime.submit_task(request())
            await asyncio.wait_for(other.wait(), 1)
            assert not same_seen.is_set()
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(first.wait(), same.wait()), 1)
        assert same_seen.is_set()


async def test_isolated_sync_returned_awaitable_runs_on_its_owner_loop() -> None:
    locations: list[asyncio.AbstractEventLoop] = []

    async def finish() -> None:
        locations.append(asyncio.get_running_loop())

    def on_done(task: Task) -> Awaitable[None]:
        locations.append(asyncio.get_running_loop())
        return finish()

    async with embedded() as runtime:
        listener = runtime.add_task_listener(on_done=on_done, execution="dedicated")
        await runtime.submit_task(request())
        await asyncio.wait_for(listener.wait(), 1)
    assert len(locations) == 2 and locations[0] is locations[1]
    assert locations[0] is not asyncio.get_running_loop()


async def test_isolated_error_handler_uses_same_loop_and_other_listener_succeeds() -> None:
    locations: list[asyncio.AbstractEventLoop] = []
    errors: list[Exception] = []
    finals: list[Task] = []

    async def broken(event: Event) -> None:
        locations.append(asyncio.get_running_loop())
        raise ValueError("callback failed")

    def on_error(task_id: str, error: Exception) -> None:
        locations.append(asyncio.get_running_loop())
        errors.append(error)

    async with embedded() as runtime:
        bad = runtime.add_task_listener(
            on_event=broken, on_error=on_error, execution="dedicated",
        )
        good = runtime.add_task_listener(on_done=finals.append, execution="dedicated")
        await runtime.submit_task(request())
        await asyncio.wait_for(asyncio.gather(bad.wait(), good.wait()), 1)
    assert len(errors) == 1 and isinstance(errors[0], ValueError)
    assert locations[0] is locations[1]
    assert finals[0].state == TaskState.SUCCEEDED


async def test_remote_sdk_supports_group_and_dedicated_callbacks() -> None:
    caller = asyncio.get_running_loop()
    observed: list[asyncio.AbstractEventLoop] = []

    async def on_done(task: Task) -> None:
        observed.append(asyncio.get_running_loop())
        assert task.state == TaskState.SUCCEEDED

    async with remote() as runtime:
        first = runtime.add_task_listener(on_done=on_done, execution="group", group="remote")
        second = runtime.add_task_listener(on_done=on_done, execution="dedicated")
        await runtime.submit_task(request())
        await asyncio.wait_for(asyncio.gather(first.wait(), second.wait()), 1)
    assert len(observed) == 2 and len(set(observed)) == 2
    assert caller not in observed


async def test_execution_configuration_is_validated_at_registration() -> None:
    invalid: list[dict[str, Any]] = [
        {"execution": "unknown"}, {"execution": None}, {"execution": []},
        {"execution": "group"}, {"execution": "group", "group": ""},
        {"execution": "group", "group": "   "}, {"execution": "group", "group": 7},
        {"execution": "caller", "group": "ui"},
        {"execution": "dedicated", "group": "ui"},
    ]
    async with embedded() as runtime:
        for config in invalid:
            with pytest.raises(InvalidRequest):
                runtime.add_task_listener(on_done=lambda task: None, **config)


def test_listener_execution_limits_are_validated() -> None:
    for name in ("max_listener_threads", "max_pending_listener_callbacks"):
        for value in (0, -1, True, 1.5):
            with pytest.raises(InvalidRequest):
                RuntimeOptions(**{name: value})
    for value in (0, -1, True, float("inf"), float("nan")):
        with pytest.raises(InvalidRequest):
            RuntimeOptions(listener_queue_timeout_s=value)


async def test_listener_thread_budget_counts_groups_once_and_releases_closed_owners() -> None:
    async with embedded(RuntimeOptions(max_listener_threads=1)) as runtime:
        first = runtime.add_task_listener(on_done=lambda task: None, execution="group", group="ui")
        second = runtime.add_task_listener(on_done=lambda task: None, execution="group", group="ui")
        caller = runtime.add_task_listener(on_done=lambda task: None)
        with pytest.raises(ListenerCapacityExceeded):
            runtime.add_task_listener(on_done=lambda task: None, execution="dedicated")
        await first.close()
        with pytest.raises(ListenerCapacityExceeded):
            runtime.add_task_listener(on_done=lambda task: None, execution="group", group="other")
        await runtime.submit_task(request())
        await asyncio.wait_for(asyncio.gather(second.wait(), caller.wait()), 1)
        await second.close()
        replacement = runtime.add_task_listener(on_done=lambda task: None, execution="dedicated")
        await replacement.close()


async def test_closed_blocked_listener_retains_thread_capacity_until_real_exit() -> None:
    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    workers: list[threading.Thread] = []

    async def blocking(event: Event) -> None:
        workers.append(threading.current_thread())
        caller.call_soon_threadsafe(entered.set)
        assert release.wait(3)

    async with embedded(RuntimeOptions(max_listener_threads=1, cancel_grace_s=0.02)) as runtime:
        listener = runtime.add_task_listener(on_event=blocking, execution="dedicated")
        try:
            await runtime.submit_task(request())
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(listener.close(), 0.5)
            await listener.close()
            with pytest.raises(ListenerCapacityExceeded):
                runtime.add_task_listener(on_done=lambda task: None, execution="dedicated")
        finally:
            release.set()
        await asyncio.to_thread(workers[0].join, 1)
        assert not workers[0].is_alive()
        replacement = runtime.add_task_listener(on_done=lambda task: None, execution="dedicated")
        await runtime.submit_task(request())
        await asyncio.wait_for(replacement.wait(), 1)


async def test_isolated_queue_limits_end_listener_without_late_callback_or_business_failure() -> None:
    caller = asyncio.get_running_loop()
    for capacity, expected_error in ((1, ListenerCallbackQueueFull), (2, ListenerCallbackTimeout)):
        entered = asyncio.Event()
        release = threading.Event()
        deliveries: list[Task] = []
        reported: list[dict[str, Any]] = []
        previous = caller.get_exception_handler()
        caller.set_exception_handler(lambda loop, context: reported.append(context))

        def blocking(event: Event) -> None:
            caller.call_soon_threadsafe(entered.set)
            assert release.wait(3)

        try:
            async with embedded(RuntimeOptions(
                max_pending_listener_callbacks=capacity, listener_queue_timeout_s=0.02,
            )) as runtime:
                first = runtime.add_task_listener(on_event=blocking, execution="group", group="busy")
                try:
                    await runtime.submit_task(request())
                    await asyncio.wait_for(entered.wait(), 1)
                    second = runtime.add_task_listener(
                        on_done=deliveries.append, execution="group", group="busy",
                        on_error=lambda task_id, error: None,
                    )
                    await asyncio.wait_for(first.close(), 0.5)
                    task = await runtime.submit_task(request())
                    await asyncio.wait_for(second.wait(), 1)
                    assert (await runtime.get_task_status(task.id)).state == TaskState.SUCCEEDED
                    assert len(reported) == 1
                    assert isinstance(reported[0]["exception"], expected_error)
                finally:
                    release.set()
                await second.close()
                assert not deliveries
        finally:
            release.set()
            caller.set_exception_handler(previous)


async def test_isolated_callback_cannot_wait_for_its_own_listener() -> None:
    rejected: list[bool] = []

    async with embedded() as runtime:
        async def on_event(event: Event) -> None:
            with pytest.raises(InvalidRequest):
                await listener.wait()
            rejected.append(True)

        listener = runtime.add_task_listener(on_event=on_event, execution="dedicated")
        await runtime.submit_task(request())
        await asyncio.wait_for(listener.wait(), 1)
        assert rejected


async def test_isolated_callback_can_close_its_listener_and_return() -> None:
    caller = asyncio.get_running_loop()
    finished = asyncio.Event()
    deliveries: list[int] = []
    async with embedded() as runtime:
        async def on_event(event: Event) -> None:
            deliveries.append(event.sequence)
            await listener.close()
            caller.call_soon_threadsafe(finished.set)

        listener = runtime.add_task_listener(on_event=on_event, execution="dedicated")
        await runtime.submit_task(request())
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.wait_for(listener.wait(), 1)
        assert deliveries == [1]


async def test_isolated_callback_can_close_either_sdk_and_return() -> None:
    caller = asyncio.get_running_loop()
    for factory in (embedded, remote):
        finished = asyncio.Event()
        async with factory() as runtime:
            async def on_event(event: Event) -> None:
                await runtime.close()
                caller.call_soon_threadsafe(finished.set)

            listener = runtime.add_task_listener(on_event=on_event, execution="dedicated")
            await runtime.submit_task(request())
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.wait_for(listener.wait(), 1)


async def test_sdk_close_bounds_blocked_isolated_callback_and_drops_late_deliveries() -> None:
    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    deliveries: list[int] = []
    workers: list[threading.Thread] = []

    async def blocking(event: Event) -> None:
        deliveries.append(event.sequence)
        workers.append(threading.current_thread())
        caller.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        raise ValueError("late failure")

    async with embedded(RuntimeOptions(cancel_grace_s=0.02)) as runtime:
        listener = runtime.add_task_listener(on_event=blocking, execution="dedicated")
        try:
            await runtime.submit_task(request())
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(runtime.close(), 0.5)
            assert runtime._core._closed
            assert deliveries == [1]
        finally:
            release.set()
        await asyncio.to_thread(workers[0].join, 1)
        await asyncio.wait_for(listener.wait(), 1)
        assert deliveries == [1]


async def test_isolated_fatal_errors_and_self_cancellation_are_listener_failures() -> None:
    for error in (SystemExit(7), KeyboardInterrupt(), asyncio.CancelledError()):
        failures: list[Exception] = []

        async def broken(event: Event) -> None:
            raise error

        async with embedded() as runtime:
            listener = runtime.add_task_listener(
                on_event=broken, on_error=lambda task_id, exc: failures.append(exc),
                execution="dedicated",
            )
            await runtime.submit_task(request())
            await asyncio.wait_for(listener.wait(), 1)
        assert len(failures) == 1 and isinstance(failures[0], ListenerCallbackError)


async def test_callback_self_close_releases_worker_after_its_submitter_loop_exits() -> None:
    callback_closed: Future[threading.Thread] = Future()
    caller_closed: Future[None] = Future()
    release = threading.Event()

    async def application() -> None:
        runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
        await runtime.start()

        async def on_event(event: Event) -> None:
            await runtime.close()
            callback_closed.set_result(threading.current_thread())
            assert release.wait(3)

        runtime.add_task_listener(on_event=on_event, execution="dedicated")
        await runtime.submit_task(request())
        await asyncio.wrap_future(callback_closed)

    def caller() -> None:
        try:
            asyncio.run(application())
        except BaseException as error:
            caller_closed.set_exception(error)
        else:
            caller_closed.set_result(None)

    thread = threading.Thread(target=caller, daemon=True)
    thread.start()
    try:
        worker = await asyncio.wait_for(asyncio.wrap_future(callback_closed), 2)
        await asyncio.wait_for(asyncio.wrap_future(caller_closed), 2)
    finally:
        release.set()
    await asyncio.to_thread(worker.join, 1)
    assert not worker.is_alive(), "Callback worker depended on a shutdown task in a closed caller loop"


async def test_expired_callback_is_rejected_even_when_submitters_loop_is_blocked() -> None:
    caller = asyncio.get_running_loop()
    entered, queued = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    deliveries: list[bool] = []
    owner = CallbackLoop("deadline-recheck", RuntimeOptions(listener_queue_timeout_s=0.02))

    def block() -> None:
        caller.call_soon_threadsafe(entered.set)
        assert release.wait(3)

    running = asyncio.create_task(owner.invoke(object(), block, ()))
    await asyncio.wait_for(entered.wait(), 1)
    submit = LoopThread.submit

    def observed_submit(self: LoopThread, *args: Any, **kwargs: Any) -> Any:
        result = submit(self, *args, **kwargs)
        queued.set()
        return result

    with patch.object(LoopThread, "submit", observed_submit):
        pending = asyncio.create_task(owner.invoke(object(), lambda: deliveries.append(True), ()))
        await asyncio.wait_for(queued.wait(), 1)
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        time.sleep(0.1)  # Deliberately stall deadline delivery on the submitting loop.
        await asyncio.wait_for(running, 1)
        with pytest.raises(ListenerCallbackTimeout):
            await pending
        assert not deliveries
    finally:
        release.set()
        timer.cancel()
        await owner.close()


async def test_cancelling_listener_close_does_not_leak_its_thread_registration() -> None:
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def subscribe(task_id: str) -> AsyncIterator[Event]:
        try:
            entered.set()
            await asyncio.Event().wait()
            yield Event(task_id=task_id, sequence=1, event_type="TASK_CREATED")
        finally:
            cleaning.set()
            await release.wait()

    async def lookup(task_id: str) -> Task:
        raise AssertionError("No terminal event was delivered")

    listeners = TaskListeners(subscribe, lookup, options=RuntimeOptions(max_listener_threads=1))
    listener = listeners.add(on_done=lambda task: None, execution="dedicated")
    listener._track("task")
    await asyncio.wait_for(entered.wait(), 1)
    closing = asyncio.create_task(listener.close())
    await asyncio.wait_for(cleaning.wait(), 1)
    closing.cancel()
    await asyncio.gather(closing, return_exceptions=True)
    release.set()
    await asyncio.wait_for(listener.wait(), 1)
    try:
        replacement = listeners.add(on_done=lambda task: None, execution="dedicated")
        await replacement.close()
    finally:
        await listeners.close()


async def test_isolated_error_callback_preserves_remote_business_and_transport_errors() -> None:
    original_errors = (
        RemoteRuntimeError("Task was deleted", {"task_id": "task"}, code="TASK_NOT_FOUND"),
        RemoteTransportError("Connection closed"),
    )
    for original in original_errors:
        received: list[Exception] = []

        async def subscribe(task_id: str) -> AsyncIterator[Event]:
            raise original
            yield

        async def lookup(task_id: str) -> Task:
            raise AssertionError("No terminal event was delivered")

        listeners = TaskListeners(subscribe, lookup)
        listener = listeners.add(
            on_error=lambda task_id, error: received.append(error), execution="dedicated",
        )
        try:
            listener._track("task")
            await asyncio.wait_for(listener.wait(), 1)
            assert len(received) == 1 and type(received[0]) is type(original)
            assert str(received[0]) == str(original)
            assert received[0] is not original and received[0].__traceback__ is None
            if isinstance(original, RemoteRuntimeError):
                assert received[0].to_dict() == original.to_dict()
                assert received[0].code == original.code
        finally:
            await listeners.close()


async def test_registration_closed_while_starting_shared_loop_cannot_begin_callback() -> None:
    deliveries: list[bool] = []

    async def subscribe(task_id: str) -> AsyncIterator[Event]:
        yield Event(event_id="event", task_id=task_id, sequence=1, event_type="TASK_CREATED")

    async def lookup(task_id: str) -> Task:
        raise AssertionError("Closed listener must not query a final snapshot")

    listeners = TaskListeners(subscribe, lookup)
    listener = listeners.add(
        on_event=lambda event: deliveries.append(True), execution="group", group="ui",
    )
    listeners.add(on_done=lambda task: None, execution="group", group="ui")
    start = LoopThread.start

    async def start_then_close(owner: LoopThread) -> None:
        await start(owner)
        await listener.close()

    try:
        with patch.object(LoopThread, "start", start_then_close):
            listener._track("task")
            await asyncio.wait_for(listener.wait(), 1)
        assert not deliveries
    finally:
        await listeners.close()


async def test_worker_rechecks_registration_revoked_after_submission() -> None:
    active = threading.Event()
    active.set()
    deliveries: list[bool] = []
    owner = CallbackLoop("registration-recheck", RuntimeOptions())
    submit = LoopThread.submit

    def revoke_before_execute(self: LoopThread, factory: Any, **kwargs: Any) -> Any:
        async def execute() -> Any:
            active.clear()
            return await factory()
        return submit(self, execute, **kwargs)

    try:
        with patch.object(LoopThread, "submit", revoke_before_execute):
            await asyncio.wait_for(owner.invoke(
                object(), lambda: deliveries.append(True), (), can_start=active.is_set,
            ), 1)
        assert not deliveries
    finally:
        await owner.close()
