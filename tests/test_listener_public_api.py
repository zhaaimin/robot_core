import asyncio
import inspect
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from typing import Any

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import InvalidRequest, ListenerCallbackError, ListenerCapacityExceeded
from robot_agent_runtime.models import Event, SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime
from robot_agent_runtime.sdk.callback_executor import CallbackExecutor
from robot_agent_runtime.sdk.listeners import TaskListener


def _validation_listener(
    *, before_lookup: Callable[[], Awaitable[None]] | None = None,
    after_error: Callable[[str, Exception], Awaitable[None]] | None = None,
) -> tuple[TaskListener, CallbackExecutor, Task, list[str], list[tuple[str, Exception]]]:
    executor = CallbackExecutor()
    task = Task(
        id="manual", task_type="execution", state=TaskState.SUCCEEDED,
        steps=[SkillCall(skill="robot.get_state")],
    )
    order: list[str] = []
    errors: list[tuple[str, Exception]] = []

    async def get_task(task_id: str) -> Task:
        assert task_id == task.id
        order.append("lookup")
        if before_lookup is not None:
            await before_lookup()
        return task

    async def subscribe(task_id: str) -> AsyncGenerator[Event, None]:
        order.append("subscribe")
        yield Event(
            event_id="terminal", task_id=task_id, sequence=1,
            event_type="TASK_SUCCEEDED", data={"state": TaskState.SUCCEEDED.value},
        )

    async def on_event(event: Event) -> None:
        order.append("event")

    async def on_done(final: Task) -> None:
        order.append("done")

    async def on_error(task_id: str, error: Exception) -> None:
        order.append("error")
        errors.append((task_id, error))
        if after_error is not None:
            await after_error(task_id, error)

    async def detach(listener: TaskListener) -> None:
        pass

    return TaskListener(
        subscribe, get_task, detach, executor,
        on_event=on_event, on_done=on_done, on_error=on_error,
    ), executor, task, order, errors


async def test_public_listener_validator_accepts_snapshot_before_event_delivery() -> None:
    listener, executor, task, order, errors = _validation_listener()

    def validate(snapshot: Task) -> None:
        assert snapshot.id == task.id
        assert snapshot is not task
        snapshot.metadata["validator"] = True
        order.append("validate")

    try:
        listener.track(task.id, validate=validate)
        await asyncio.wait_for(listener.wait(), 1)
        assert order == ["lookup", "validate", "subscribe", "event", "lookup", "done"]
        assert "validator" not in task.metadata
        assert not errors
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_validator_failure_prevents_all_task_deliveries() -> None:
    listener, executor, task, order, errors = _validation_listener()
    failure = ValueError("The task belongs to a different operation")

    def validate(snapshot: Task) -> None:
        order.append("validate")
        raise failure

    try:
        listener.track(task.id, validate=validate)
        await asyncio.wait_for(listener.wait(), 1)
        assert order == ["lookup", "validate", "error"]
        assert errors == [(task.id, failure)]
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_rejected_validator_allows_later_valid_tracking() -> None:
    listener, executor, task, order, errors = _validation_listener()

    def reject(snapshot: Task) -> None:
        raise ValueError("Different operation")

    def accept(snapshot: Task) -> None:
        order.append("accepted")

    try:
        listener.track(task.id, validate=reject)
        await asyncio.wait_for(listener.wait(), 1)
        assert len(errors) == 1
        listener.track(task.id, validate=accept)
        await asyncio.wait_for(listener.wait(), 1)
        assert order == [
            "lookup", "error", "lookup", "accepted", "subscribe", "event", "lookup", "done",
        ]
        assert len(errors) == 1
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_failed_validation_lookup_allows_later_tracking() -> None:
    lookups = 0

    async def lookup() -> None:
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            raise ValueError("Snapshot lookup failed")

    listener, executor, task, order, errors = _validation_listener(before_lookup=lookup)

    def accept(snapshot: Task) -> None:
        order.append("accepted")

    try:
        listener.track(task.id, validate=accept)
        await asyncio.wait_for(listener.wait(), 1)
        listener.track(task.id, validate=accept)
        await asyncio.wait_for(listener.wait(), 1)
        assert len(errors) == 1
        assert order.count("accepted") == 1
        assert order[-1] == "done"
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_reentrant_unvalidated_retry_keeps_wait_and_precedence() -> None:
    replacement_lookup, release = asyncio.Event(), asyncio.Event()
    lookups = 0

    async def lookup() -> None:
        nonlocal lookups
        lookups += 1
        if lookups == 2:
            replacement_lookup.set()
            await release.wait()

    def speculative(snapshot: Task) -> None:
        raise AssertionError("Unconditional tracking must keep precedence")

    async def retry(task_id: str, error: Exception) -> None:
        listener.track(task_id, validate=speculative)
        listener.track(task_id)
        listener.track(task_id, validate=speculative)

    listener, executor, task, order, errors = _validation_listener(
        before_lookup=lookup, after_error=retry,
    )

    def reject(snapshot: Task) -> None:
        raise ValueError("Speculative operation mismatch")

    try:
        listener.track(task.id, validate=reject)
        waiter = asyncio.create_task(listener.wait())
        await asyncio.wait_for(replacement_lookup.wait(), 1)
        assert not waiter.done()
        assert order == ["lookup", "error", "subscribe", "event", "lookup"]
        release.set()
        await asyncio.wait_for(waiter, 1)
        assert order[-1] == "done"
        assert len(errors) == 1
    finally:
        release.set()
        await listener.close()
        await executor.close()


async def test_public_listener_reentrant_validator_retries_are_explicit() -> None:
    def reject(snapshot: Task) -> None:
        raise ValueError("Different operation")

    async def retry(task_id: str, error: Exception) -> None:
        if len(errors) == 1:
            listener.track(task_id, validate=reject)
            listener.track(task_id, validate=reject)
            listener.track(task_id, validate=reject)

    listener, executor, task, order, errors = _validation_listener(after_error=retry)
    try:
        listener.track(task.id, validate=reject)
        await asyncio.wait_for(listener.wait(), 1)
        assert order == ["lookup", "error"] * 4
        assert len(errors) == 4
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_preserves_pending_validators_in_both_orders() -> None:
    for valid_first in (True, False):
        entered, release = asyncio.Event(), asyncio.Event()

        async def lookup() -> None:
            entered.set()
            await release.wait()

        listener, executor, task, order, errors = _validation_listener(before_lookup=lookup)

        def reject(snapshot: Task) -> None:
            raise ValueError("Different operation")

        def accept(snapshot: Task) -> None:
            order.append("accepted")

        try:
            listener.track(task.id, validate=reject)
            await asyncio.wait_for(entered.wait(), 1)
            for validate in ((accept, reject) if valid_first else (reject, accept)):
                listener.track(task.id, validate=validate)
            release.set()
            await asyncio.wait_for(listener.wait(), 1)
            assert order[-1] == "done" and order.count("accepted") == 1
            assert order.count("subscribe") == 1
            assert len(errors) == (1 if valid_first else 2)
        finally:
            release.set()
            await listener.close()
            await executor.close()


async def test_public_listener_validation_backlog_reports_overflow_and_remains_reusable() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup() -> None:
        entered.set()
        await release.wait()

    listener, executor, task, order, errors = _validation_listener(before_lookup=lookup)

    def reject(snapshot: Task) -> None:
        raise ValueError("Different operation")

    try:
        listener.track(task.id, validate=reject)
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(100):
            listener.track(task.id, validate=reject)
        release.set()
        await asyncio.wait_for(listener.wait(), 1)
        assert len(errors) == 66
        assert isinstance(errors[-1][1], ListenerCapacityExceeded)
        assert "subscribe" not in order
        listener.track(task.id)
        await asyncio.wait_for(listener.wait(), 1)
        assert order[-1] == "done" and order.count("subscribe") == 1
    finally:
        release.set()
        await listener.close()
        await executor.close()


async def test_public_listener_close_discards_pending_validation_replacements() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup() -> None:
        entered.set()
        await release.wait()

    listener, executor, task, order, errors = _validation_listener(before_lookup=lookup)

    def accept(snapshot: Task) -> None:
        order.append("validated")

    try:
        listener.track(task.id, validate=accept)
        await asyncio.wait_for(entered.wait(), 1)
        listener.track(task.id, validate=accept)
        await listener.close()
        await asyncio.wait_for(listener.wait(), 1)
        assert listener.closed and order == ["lookup"] and not errors
    finally:
        release.set()
        await listener.close()
        await executor.close()


async def test_public_listener_overflow_does_not_compare_user_validators() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def lookup() -> None:
        entered.set()
        await release.wait()

    listener, executor, task, order, errors = _validation_listener(before_lookup=lookup)

    class Validator:
        def __call__(self, snapshot: Task) -> None:
            raise ValueError("Different operation")

        def __eq__(self, other: object) -> bool:
            raise AssertionError("Registration must not run user equality under its lock")

    try:
        listener.track(task.id, validate=Validator())
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(100):
            listener.track(task.id, validate=Validator())
        release.set()
        await asyncio.wait_for(listener.wait(), 1)
        assert len(errors) == 66 and isinstance(errors[-1][1], ListenerCapacityExceeded)
    finally:
        release.set()
        await listener.close()
        await executor.close()


async def test_public_listener_unvalidated_error_keeps_legacy_seen_policy() -> None:
    async def lookup() -> None:
        raise ValueError("Final snapshot lookup failed")

    listener, executor, task, order, errors = _validation_listener(before_lookup=lookup)
    try:
        listener.track(task.id)
        await asyncio.wait_for(listener.wait(), 1)
        previous = list(order)
        listener.track(task.id)
        await listener.wait()
        assert order == previous == ["subscribe", "event", "lookup", "error"]
        assert len(errors) == 1
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_duplicate_tracking_keeps_first_validator() -> None:
    listener, executor, task, order, errors = _validation_listener()

    def first(snapshot: Task) -> None:
        order.append("first-validator")

    def replacement(snapshot: Task) -> None:
        raise AssertionError("An observed task must keep its original validator")

    try:
        listener.track(task.id, validate=first)
        listener.track(task.id, validate=replacement)
        listener.track(task.id)
        await asyncio.wait_for(listener.wait(), 1)
        listener.track(task.id, validate=replacement)
        await listener.wait()
        assert order.count("first-validator") == 1
        assert order.count("subscribe") == 1
        assert order[-1] == "done"
        assert not errors
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_wait_includes_pending_validation_lookup() -> None:
    entered, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def block_lookup() -> None:
        entered.set()
        await release.wait()

    listener, executor, task, order, errors = _validation_listener(before_lookup=block_lookup)

    def validate(snapshot: Task) -> None:
        order.append("validate")

    async def join() -> None:
        waiting.set()
        await listener.wait()

    try:
        listener.track(task.id, validate=validate)
        await asyncio.wait_for(entered.wait(), 1)
        waiter = asyncio.create_task(join())
        await asyncio.wait_for(waiting.wait(), 1)
        assert not waiter.done()
        assert order == ["lookup"]
        release.set()
        await asyncio.wait_for(waiter, 1)
        assert order[-1] == "done"
        assert not errors
    finally:
        release.set()
        await listener.close()
        await executor.close()


async def test_public_listener_rejects_async_and_noncallable_validators() -> None:
    listener, executor, task, order, errors = _validation_listener()

    async def invalid(snapshot: Task) -> None:
        pass

    class AsyncValidator:
        async def __call__(self, snapshot: Task) -> None:
            pass

    try:
        for validate in (invalid, AsyncValidator(), False, "invalid", object()):
            with pytest.raises(InvalidRequest):
                listener.track(task.id, validate=validate)  # type: ignore[arg-type]
        await listener.wait()
        assert not order
        assert not errors
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_rejects_and_closes_validator_returned_coroutines() -> None:
    listener, executor, task, order, errors = _validation_listener()
    results: list[Any] = []

    async def returned() -> None:
        raise AssertionError("Validators cannot schedule asynchronous work")

    def validate(snapshot: Task) -> Any:
        result = returned()
        results.append(result)
        return result

    try:
        listener.track(task.id, validate=validate)
        await asyncio.wait_for(listener.wait(), 1)
        assert order == ["lookup", "error"]
        assert len(errors) == 1 and isinstance(errors[0][1], InvalidRequest)
        assert inspect.getcoroutinestate(results[0]) == inspect.CORO_CLOSED
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_requires_validator_to_return_none() -> None:
    listener, executor, task, order, errors = _validation_listener()

    def validate(snapshot: Task) -> Any:
        return True

    try:
        listener.track(task.id, validate=validate)
        await asyncio.wait_for(listener.wait(), 1)
        assert order == ["lookup", "error"]
        assert len(errors) == 1 and isinstance(errors[0][1], InvalidRequest)
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_closed_ignores_valid_validator_registration() -> None:
    listener, executor, task, order, errors = _validation_listener()

    def validate(snapshot: Task) -> None:
        raise AssertionError("A closed listener must not validate tasks")

    try:
        await listener.close()
        listener.track(task.id, validate=validate)
        await listener.wait()
        assert not order
        assert not errors
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_tracks_known_task_with_replay_and_final_snapshots() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    executor = CallbackExecutor()
    await runtime.start()
    events: list[Event] = []
    results: list[Task] = []
    detached: list[TaskListener] = []

    async def detach(listener: TaskListener) -> None:
        detached.append(listener)

    async def on_event(event: Event) -> None:
        events.append(event)
        event.data["observer"] = True

    async def on_done(task: Task) -> None:
        results.append(task)
        task.metadata["observer"] = True

    listener = TaskListener(
        runtime.subscribe_task_events, runtime.get_task_status, detach, executor,
        on_event=on_event, on_done=on_done, on_error=None,
    )
    try:
        known = await runtime.submit_task(TaskRequest(
            task_type="execution", steps=[SkillCall(skill="robot.get_state")],
        ))
        async with asyncio.timeout(1):
            async for _ in runtime.subscribe_task_events(known.id):
                pass
        listener.track(known.id)
        listener.track(known.id)
        await asyncio.wait_for(listener.wait(), 1)
        assert events[0].event_type == "TASK_CREATED"
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert TaskState(events[-1].data["state"]).is_terminal
        assert [task.id for task in results] == [known.id]
        assert results[0].state == TaskState.SUCCEEDED
        assert "observer" not in (await runtime.get_task_status(known.id)).metadata
        replay = [event async for event in runtime.subscribe_task_events(known.id)]
        assert all("observer" not in event.data for event in replay)
    finally:
        await listener.close()
        await executor.close()
        await runtime.close()
    assert detached == [listener]


async def test_public_listener_closed_is_readonly_and_late_valid_tracking_is_ignored() -> None:
    subscriptions: list[str] = []
    executor = CallbackExecutor()

    async def subscribe(task_id: str) -> Any:
        subscriptions.append(task_id)
        if False:
            yield None

    async def get_task(task_id: str) -> Task:
        raise AssertionError("A closed listener must not query a task")

    async def detach(listener: TaskListener) -> None:
        pass

    listener = TaskListener(
        subscribe, get_task, detach, executor,
        on_event=None, on_done=None, on_error=None,
    )
    try:
        assert listener.closed is False
        with pytest.raises(AttributeError):
            listener.closed = True  # type: ignore[misc]
        await listener.close()
        assert listener.closed is True
        listener.track("known-late-task")
        await listener.wait()
        assert not subscriptions
    finally:
        await listener.close()
        await executor.close()


async def test_public_listener_rejects_empty_and_non_plain_string_task_ids() -> None:
    class TaskId(str):
        pass

    executor = CallbackExecutor()

    async def subscribe(task_id: str) -> Any:
        raise AssertionError("Invalid task IDs must not open subscriptions")
        yield None

    async def get_task(task_id: str) -> Task:
        raise AssertionError("Invalid task IDs must not query a task")

    async def detach(listener: TaskListener) -> None:
        pass

    listener = TaskListener(
        subscribe, get_task, detach, executor,
        on_event=None, on_done=None, on_error=None,
    )
    try:
        for task_id in ("", " \t", None, False, 1, TaskId("task")):
            with pytest.raises(InvalidRequest):
                listener.track(task_id)  # type: ignore[arg-type]
        await listener.close()
        with pytest.raises(InvalidRequest):
            listener.track("")
    finally:
        await listener.close()
        await executor.close()


async def test_executor_invoke_async_callbacks_and_callables_on_callers_loop() -> None:
    caller = asyncio.get_running_loop()
    thread = threading.get_ident()
    observed: list[tuple[asyncio.AbstractEventLoop, int]] = []
    executor = CallbackExecutor(max_workers=1)

    async def callback(value: str) -> str:
        observed.append((asyncio.get_running_loop(), threading.get_ident()))
        return value

    class AsyncCallback:
        async def __call__(self, value: str) -> str:
            return await callback(value)

    try:
        assert await executor.invoke(callback, "function") == "function"
        assert await executor.invoke(AsyncCallback(), "callable") == "callable"
        assert observed == [(caller, thread), (caller, thread)]
    finally:
        await executor.close()


async def test_executor_invoke_sync_callbacks_use_existing_daemon_workers() -> None:
    caller = threading.get_ident()
    observed: list[threading.Thread] = []
    executor = CallbackExecutor(max_workers=1)

    def callback(value: str) -> str:
        observed.append(threading.current_thread())
        return value

    try:
        assert await executor.invoke(callback, "first") == "first"
        assert await executor.invoke(callback, "second") == "second"
        assert observed[0] is observed[1]
        assert observed[0].ident != caller
        assert observed[0].daemon
        assert observed[0].name.startswith("rar-callback-")
    finally:
        await executor.close()


async def test_executor_invoke_awaits_sync_returned_awaitables_on_callers_loop() -> None:
    caller = asyncio.get_running_loop()
    observed: list[asyncio.AbstractEventLoop] = []
    executor = CallbackExecutor(max_workers=1)

    async def result(value: str) -> str:
        observed.append(asyncio.get_running_loop())
        return value

    class CustomAwaitable:
        def __await__(self) -> Generator[Any, None, str]:
            return result("custom").__await__()

    def callback(custom: bool) -> Awaitable[str]:
        return CustomAwaitable() if custom else result("coroutine")

    try:
        assert await executor.invoke(callback, False) == "coroutine"
        assert await executor.invoke(callback, True) == "custom"
        assert observed == [caller, caller]
    finally:
        await executor.close()


async def test_executor_invoke_guard_denies_async_and_sync_callback_start() -> None:
    calls: list[str] = []
    executor = CallbackExecutor(max_workers=1)

    async def callback() -> None:
        calls.append("async")

    try:
        assert await executor.invoke(callback, can_start=lambda: False) is None
        assert await executor.invoke(calls.append, "sync", can_start=lambda: False) is None
        assert not calls
    finally:
        await executor.close()


async def test_executor_invoke_rechecks_guard_when_worker_claims_queued_callback() -> None:
    caller = asyncio.get_running_loop()
    entered, submitted = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    allowed = threading.Event()
    allowed.set()
    calls: list[str] = []

    class ObservedExecutor(CallbackExecutor):
        def submit(self, callback: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
            future = super().submit(callback, *args)
            submitted.set()
            return future

    executor = ObservedExecutor(max_workers=1)

    def block() -> None:
        caller.call_soon_threadsafe(entered.set)
        release.wait()

    running = executor.submit(block)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        submitted.clear()
        queued = asyncio.create_task(executor.invoke(
            calls.append, "revoked", can_start=allowed.is_set,
        ))
        await asyncio.wait_for(submitted.wait(), 1)
        allowed.clear()
        release.set()
        assert await asyncio.wait_for(queued, 1) is None
        await asyncio.wait_for(running, 1)
        assert not calls
    finally:
        release.set()
        await executor.close()


async def test_executor_invoke_discards_returned_coroutine_if_guard_was_revoked() -> None:
    allowed = threading.Event()
    allowed.set()
    calls: list[str] = []
    results: list[Any] = []
    executor = CallbackExecutor(max_workers=1)

    async def returned() -> None:
        calls.append("awaited")

    def callback() -> Awaitable[None]:
        allowed.clear()
        result = returned()
        results.append(result)
        return result

    try:
        assert await executor.invoke(callback, can_start=allowed.is_set) is None
        assert not calls
        assert inspect.getcoroutinestate(results[0]) == inspect.CORO_CLOSED
    finally:
        await executor.close()


async def test_executor_invoke_normalizes_callback_self_cancellation() -> None:
    executor = CallbackExecutor(max_workers=1)

    async def async_cancel() -> None:
        raise asyncio.CancelledError("callback cancelled itself")

    def sync_cancel() -> None:
        raise asyncio.CancelledError("callback cancelled itself")

    try:
        for callback in (async_cancel, sync_cancel):
            with pytest.raises(ListenerCallbackError) as caught:
                await executor.invoke(callback)
            assert isinstance(caught.value.__cause__, asyncio.CancelledError)
        assert asyncio.current_task().cancelling() == 0
    finally:
        await executor.close()


async def test_executor_invoke_propagates_actual_async_caller_cancellation() -> None:
    entered, finished = asyncio.Event(), asyncio.Event()
    executor = CallbackExecutor(max_workers=1)

    async def callback() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    try:
        invocation = asyncio.create_task(executor.invoke(callback))
        await asyncio.wait_for(entered.wait(), 1)
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
        assert finished.is_set()
    finally:
        await executor.close()


async def test_executor_invoke_normalizes_unsafe_async_base_exceptions() -> None:
    executor = CallbackExecutor(max_workers=1)

    async def callback(error: BaseException) -> None:
        raise error

    try:
        for error in (SystemExit("exit"), KeyboardInterrupt("interrupt"), StopIteration("stop")):
            with pytest.raises(ListenerCallbackError):
                await executor.invoke(callback, error)
    finally:
        await executor.close()


async def test_executor_invoke_preserves_ordinary_callback_exceptions() -> None:
    executor = CallbackExecutor(max_workers=1)
    failure = ValueError("callback failed")

    async def async_fail() -> None:
        raise failure

    def sync_fail() -> None:
        raise failure

    try:
        for callback in (async_fail, sync_fail):
            with pytest.raises(ValueError) as caught:
                await executor.invoke(callback)
            assert caught.value is failure
    finally:
        await executor.close()


async def test_executor_invoke_cancels_queued_work_and_reclaims_capacity() -> None:
    caller = asyncio.get_running_loop()
    entered, submitted = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    calls: list[str] = []

    class ObservedExecutor(CallbackExecutor):
        def submit(self, callback: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
            future = super().submit(callback, *args)
            submitted.set()
            return future

    executor = ObservedExecutor(max_workers=1, max_pending=1)

    def block() -> None:
        caller.call_soon_threadsafe(entered.set)
        release.wait()

    running = executor.submit(block)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        submitted.clear()
        queued = asyncio.create_task(executor.invoke(calls.append, "cancelled"))
        await asyncio.wait_for(submitted.wait(), 1)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        replacement = executor.submit(calls.append, "replacement")
        release.set()
        await asyncio.wait_for(asyncio.gather(running, replacement), 1)
        assert calls == ["replacement"]
    finally:
        release.set()
        await executor.close()


async def test_executor_invoke_cancellation_discards_late_running_coroutine_result() -> None:
    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    results: list[Any] = []
    executor = CallbackExecutor(max_workers=1)

    async def abandoned() -> None:
        raise AssertionError("Cancelled results must never execute")

    def callback() -> Awaitable[None]:
        result = abandoned()
        results.append(result)
        caller.call_soon_threadsafe(entered.set)
        release.wait()
        return result

    try:
        invocation = asyncio.create_task(executor.invoke(callback))
        await asyncio.wait_for(entered.wait(), 1)
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
        release.set()
        await asyncio.wait_for(executor.submit(lambda: None), 1)
        assert inspect.getcoroutinestate(results[0]) == inspect.CORO_CLOSED
    finally:
        release.set()
        await executor.close()


async def test_executor_invoke_cancellation_racing_delivery_closes_unclaimed_coroutine() -> None:
    results: list[Any] = []

    class CancellingExecutor(CallbackExecutor):
        def submit(self, callback: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
            owner = asyncio.current_task()
            assert owner is not None
            future = super().submit(callback, *args)
            future.add_done_callback(lambda finished: owner.cancel())
            return future

    executor = CancellingExecutor(max_workers=1)

    async def unclaimed() -> None:
        raise AssertionError("Unclaimed results must never execute")

    def callback() -> Awaitable[None]:
        result = unclaimed()
        results.append(result)
        return result

    try:
        invocation = asyncio.create_task(executor.invoke(callback))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(invocation, 1)
        assert inspect.getcoroutinestate(results[0]) == inspect.CORO_CLOSED
    finally:
        await executor.close()


async def test_executor_invoke_close_abandons_blocked_sync_callback_without_waiting() -> None:
    caller = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    executor = CallbackExecutor(max_workers=1)

    def callback() -> None:
        caller.call_soon_threadsafe(entered.set)
        release.wait()
        caller.call_soon_threadsafe(finished.set)

    try:
        invocation = asyncio.create_task(executor.invoke(callback))
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(executor.close(), 1)
        assert not release.is_set()
        with pytest.raises(ListenerCallbackError):
            await invocation
    finally:
        release.set()
        if entered.is_set():
            await asyncio.wait_for(finished.wait(), 1)
        await executor.close()
