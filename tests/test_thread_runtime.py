import asyncio
import concurrent.futures
import gc
import math
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from robot_agent_runtime import errors


async def test_control_capacity_is_reserved_and_bounded() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    owner = LoopThread("reserved-control", max_pending=1, control_capacity=1)
    state: dict[str, Any] = {}
    entered = asyncio.Event()
    caller = asyncio.get_running_loop()

    async def blocked() -> None:
        state["loop"] = asyncio.get_running_loop()
        state["release"] = asyncio.Event()
        caller.call_soon_threadsafe(entered.set)
        await state["release"].wait()

    async def control() -> int:
        return 7

    await owner.start()
    job = owner.submit(blocked)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert job.started()
        with pytest.raises(errors.ExtensionCapacityExceeded):
            owner.submit(control)
        assert await owner.submit(control, control=True).wait(1) == 7
        reserved = owner.submit(blocked, control=True)
        with pytest.raises(errors.ExtensionCapacityExceeded):
            owner.submit(control, control=True)
        reserved.cancel()
    finally:
        job.cancel()
        await owner.close()


async def test_close_does_not_force_cancel_protected_started_work() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    owner = LoopThread("protected-work")
    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    state: dict[str, Any] = {}

    async def work() -> int:
        state["loop"] = asyncio.get_running_loop()
        state["release"] = asyncio.Event()
        caller.call_soon_threadsafe(entered.set)
        await state["release"].wait()
        return 9

    await owner.start()
    job = owner.submit(work, cancellable=False)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await owner.close(grace_s=0.01)
        assert not job.done()
        state["loop"].call_soon_threadsafe(state["release"].set)
        assert await job.wait(1) == 9
    finally:
        if state and not job.done():
            state["loop"].call_soon_threadsafe(state["release"].set)
        await owner.close()


async def test_close_waits_for_actual_thread_exit_after_loop_stop_acknowledgement() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    tail_entered = asyncio.Event()
    release = threading.Event()

    class TailOwner(LoopThread):
        def _run(self) -> None:
            try:
                super()._run()
            finally:
                caller.call_soon_threadsafe(tail_entered.set)
                release.wait(2)

    owner = TailOwner("actual-exit")
    await owner.start()
    try:
        await owner.close(grace_s=0)
        await asyncio.wait_for(tail_entered.wait(), 1)
        started = caller.time()
        await owner.close(grace_s=0.05)
        assert caller.time() - started >= 0.04
        assert owner._thread.is_alive()
        release.set()
        await owner.close(grace_s=1)
        assert not owner._thread.is_alive()
    finally:
        release.set()
        await owner.close()


def test_thread_isolation_errors_have_structured_codes() -> None:
    for name, code in (
        ("ExtensionCapacityExceeded", "EXTENSION_CAPACITY_EXCEEDED"),
        ("ExtensionTimeout", "EXTENSION_TIMEOUT"),
        ("ExecutionUnresponsive", "EXECUTION_UNRESPONSIVE"),
        ("ResourceQuarantined", "RESOURCE_QUARANTINED"),
        ("TaskTimeout", "TASK_TIMEOUT"),
        ("SnapshotTooLarge", "SNAPSHOT_TOO_LARGE"),
    ):
        error_type = getattr(errors, name, None)
        assert error_type is not None, name
        error = error_type("unavailable", {"extension": "test"})
        assert isinstance(error, errors.AgentRuntimeError)
        assert error.to_dict() == {
            "code": code, "message": "unavailable", "details": {"extension": "test"},
        }


def test_runtime_options_have_frozen_validated_defaults() -> None:
    from robot_agent_runtime.options import RuntimeOptions

    options = RuntimeOptions()
    assert options.port_timeout_s == 5.0
    assert options.cancel_grace_s == 0.2
    assert options.max_extension_threads == 32
    assert options.max_pending_calls == 256
    assert options.max_control_calls == 16
    assert options.max_subscriptions == 256
    assert options.max_snapshot_bytes == 1_048_576
    assert options.max_snapshot_depth == 64
    assert options.event_page_size == 128
    assert options.max_event_buffer == 1000
    with pytest.raises(FrozenInstanceError):
        options.port_timeout_s = 1.0
    for name in ("port_timeout_s", "cancel_grace_s"):
        for value in (0, -1, math.inf, -math.inf, math.nan, True, "1"):
            with pytest.raises(errors.InvalidRequest):
                RuntimeOptions(**{name: value})
    for name in (
        "max_extension_threads", "max_pending_calls", "max_control_calls", "max_subscriptions",
        "max_snapshot_bytes", "max_snapshot_depth", "event_page_size", "max_event_buffer",
    ):
        for value in (0, -1, 1.5, math.inf, math.nan, True, "1"):
            with pytest.raises(errors.InvalidRequest):
                RuntimeOptions(**{name: value})


async def test_loop_thread_calls_factory_and_awaitable_in_daemon_thread() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    owner = LoopThread("test-loop-thread")
    caller_thread = threading.get_ident()
    factory_threads: list[int] = []

    async def work() -> tuple[int, bool]:
        return threading.get_ident(), threading.current_thread().daemon

    def factory() -> Awaitable[tuple[int, bool]]:
        factory_threads.append(threading.get_ident())
        return work()

    await owner.start()
    try:
        thread_id, daemon = await owner.call(factory)
        assert thread_id != caller_thread
        assert factory_threads == [thread_id]
        assert daemon
        await owner.start()
        assert (await owner.call(factory))[0] == thread_id
    finally:
        await owner.close()


async def wait_until(predicate: Callable[[], bool]) -> None:
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(1):
        while not predicate():
            checkpoint = loop.create_future()
            loop.call_soon(checkpoint.set_result, None)
            await checkpoint


async def test_blocking_factory_does_not_block_caller_or_other_loop() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    owner, other = LoopThread("blocked-factory"), LoopThread("other-loop")

    async def result() -> int:
        return 17

    def factory() -> Awaitable[int]:
        caller.call_soon_threadsafe(entered.set)
        release.wait()
        return result()

    await asyncio.gather(owner.start(), other.start())
    job = owner.submit(factory)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(TimeoutError):
            await job.wait(0.01)
        assert not job.done()
        assert await asyncio.wait_for(other.call(result), 1) == 17
        release.set()
        assert await job.wait(1) == 17
        assert job.result() == 17
    finally:
        release.set()
        await asyncio.gather(owner.close(), other.close())


async def test_cancel_request_does_not_complete_job_that_swallows_cancellation() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    entered, cancelled = asyncio.Event(), asyncio.Event()
    state: dict[str, Any] = {}
    owner = LoopThread("swallowed-cancel")

    async def work() -> str:
        state["loop"] = asyncio.get_running_loop()
        state["release"] = asyncio.Event()
        caller.call_soon_threadsafe(entered.set)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            caller.call_soon_threadsafe(cancelled.set)
            await state["release"].wait()
        return "actually-finished"

    await owner.start()
    job = owner.submit(work)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        job.cancel()
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not job.done()
        with pytest.raises(TimeoutError):
            await job.wait(0.01)
        assert not job.done()
        state["loop"].call_soon_threadsafe(state["release"].set)
        assert await job.wait(1) == "actually-finished"
    finally:
        if state:
            state["loop"].call_soon_threadsafe(state["release"].set)
        await owner.close()


async def test_cancelling_waiter_does_not_cancel_actual_job() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    state: dict[str, Any] = {}
    owner = LoopThread("cancel-waiter")

    async def work() -> int:
        state["loop"] = asyncio.get_running_loop()
        state["release"] = asyncio.Event()
        caller.call_soon_threadsafe(entered.set)
        await state["release"].wait()
        return 5

    await owner.start()
    job = owner.submit(work)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        waiter = asyncio.create_task(job.wait())
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not job.done()
        state["loop"].call_soon_threadsafe(state["release"].set)
        assert await job.wait(1) == 5
    finally:
        if state:
            state["loop"].call_soon_threadsafe(state["release"].set)
        await owner.close()


async def test_call_timeout_requests_actual_cancellation() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    cancelled = asyncio.Event()
    owner = LoopThread("call-timeout")

    async def work() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            caller.call_soon_threadsafe(cancelled.set)

    await owner.start()
    try:
        with pytest.raises(TimeoutError):
            await owner.call(work, 0.02)
        await asyncio.wait_for(cancelled.wait(), 1)
    finally:
        await owner.close()


async def test_queued_cancel_prevents_factory_and_reclaims_registered_capacity() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    calls: list[str] = []
    owner = LoopThread("cancel-queued", max_pending=2)

    async def result() -> int:
        return 1

    def block() -> Awaitable[int]:
        caller.call_soon_threadsafe(entered.set)
        release.wait()
        return result()

    def queued() -> Awaitable[int]:
        calls.append("queued")
        return result()

    await owner.start()
    running = owner.submit(block)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        pending = owner.submit(queued)
        with pytest.raises(errors.ExtensionCapacityExceeded):
            owner.submit(result)
        pending.cancel()
        assert pending.done()
        with pytest.raises(concurrent.futures.CancelledError):
            pending.result()
        replacement = owner.submit(result)
        release.set()
        assert await running.wait(1) == 1
        assert await replacement.wait(1) == 1
        assert calls == []
    finally:
        release.set()
        await owner.close()


async def test_close_is_bounded_with_blocked_factory_and_does_not_replace_thread() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    worker: list[threading.Thread] = []
    owner = LoopThread("bounded-close")

    async def result() -> None:
        return None

    def block() -> Awaitable[None]:
        worker.append(threading.current_thread())
        caller.call_soon_threadsafe(entered.set)
        release.wait()
        return result()

    await owner.start()
    job = owner.submit(block)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(owner.close(0.01), 0.5)
        assert not job.done()
        assert worker[0].is_alive()
        assert worker[0].daemon
        with pytest.raises(errors.InvalidRequest):
            owner.submit(result)
        with pytest.raises(errors.InvalidRequest):
            await owner.start()
        await owner.close(0.01)
    finally:
        release.set()
        await wait_until(lambda: not worker[0].is_alive())


async def test_loop_thread_catches_base_exception_and_keeps_running() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    owner = LoopThread("base-exception")

    async def fail() -> None:
        raise SystemExit("external exit")

    async def recover() -> int:
        return 7

    await owner.start()
    try:
        with pytest.raises(errors.InternalError) as caught:
            await owner.call(fail)
        assert caught.value.info.details["exception"] == "SystemExit"
        assert await owner.call(recover) == 7
    finally:
        await owner.close()


async def test_factory_stop_iteration_is_mapped_before_cross_loop_delivery() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    owner = LoopThread("factory-stop-iteration")
    caller = asyncio.get_running_loop()
    contexts: list[dict[str, Any]] = []
    previous_handler = caller.get_exception_handler()
    caller.set_exception_handler(lambda loop, context: contexts.append(context))

    def exhausted() -> Awaitable[None]:
        raise StopIteration("factory exhausted")

    await owner.start()
    try:
        with pytest.raises(errors.InternalError) as caught:
            await owner.call(exhausted, 0.1)
        assert caught.value.info.details["exception"] == "StopIteration"
        assert not contexts
    finally:
        await owner.close()
        caller.set_exception_handler(previous_handler)


async def test_normal_close_exits_thread_and_is_idempotent() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    owner = LoopThread("normal-close")

    async def identify() -> threading.Thread:
        return threading.current_thread()

    await owner.start()
    worker = await owner.call(identify)
    await asyncio.gather(owner.close(), owner.close())
    await wait_until(lambda: not worker.is_alive())
    await owner.close()


async def test_close_is_idempotent_while_loop_close_has_not_returned() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    loop_closed = asyncio.Event()
    release = threading.Event()
    owner = LoopThread("close-finalization-race")

    async def widen_finalization_window() -> threading.Thread:
        target = asyncio.get_running_loop()
        original_close = target.close

        def close_loop() -> None:
            original_close()
            caller.call_soon_threadsafe(loop_closed.set)
            release.wait()

        target.close = close_loop
        return threading.current_thread()

    await owner.start()
    worker = await owner.call(widen_finalization_window)
    try:
        await owner.close(0.01)
        await asyncio.wait_for(loop_closed.wait(), 1)
        await asyncio.wait_for(owner.close(0.01), 0.5)
    finally:
        release.set()
        await wait_until(lambda: not worker.is_alive())


async def test_start_close_race_does_not_leave_loop_thread() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    for index in range(20):
        name = f"start-close-race-{index}"
        owner = LoopThread(name)
        results = await asyncio.wait_for(
            asyncio.gather(owner.start(), owner.start(), owner.close(), return_exceptions=True), 1,
        )
        assert all(result is None or isinstance(result, errors.InvalidRequest) for result in results)
        await wait_until(lambda: all(thread.name != name for thread in threading.enumerate()))


async def test_submit_requires_start_and_close_before_start_is_final() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    owner = LoopThread("never-started")

    async def work() -> None:
        return None

    with pytest.raises(errors.InvalidRequest):
        owner.submit(work)
    await owner.close()
    with pytest.raises(errors.InvalidRequest):
        await owner.start()
    with pytest.raises(errors.InvalidRequest):
        owner.submit(work)


async def test_timed_out_late_exception_is_consumed_before_loop_exit() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    contexts: list[dict[str, Any]] = []
    workers: list[threading.Thread] = []
    owner = LoopThread("late-exception")
    previous_handler = caller.get_exception_handler()
    caller.set_exception_handler(lambda loop, context: contexts.append(context))

    async def fail() -> None:
        raise ValueError("late failure")

    def factory() -> Awaitable[None]:
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: contexts.append(context))
        workers.append(threading.current_thread())
        caller.call_soon_threadsafe(entered.set)
        release.wait()
        return fail()

    await owner.start()
    try:
        job = owner.submit(factory)
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(TimeoutError):
            await job.wait(0.01)
        await owner.close(0.01)
        release.set()
        await wait_until(lambda: not workers[0].is_alive())
        del job
        gc.collect()
        checkpoint = asyncio.Event()
        caller.call_soon(checkpoint.set)
        await checkpoint.wait()
        assert not contexts
    finally:
        release.set()
        await owner.close()
        caller.set_exception_handler(previous_handler)


async def test_close_keeps_cancellation_resistant_job_until_actual_completion() -> None:
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    entered, cancelled = asyncio.Event(), asyncio.Event()
    state: dict[str, Any] = {}
    owner = LoopThread("close-resistant-job")

    async def work() -> str:
        state["loop"] = asyncio.get_running_loop()
        state["release"] = asyncio.Event()
        state["worker"] = threading.current_thread()
        caller.call_soon_threadsafe(entered.set)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            caller.call_soon_threadsafe(cancelled.set)
            await state["release"].wait()
        return "finished-after-close"

    await owner.start()
    job = owner.submit(work)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(owner.close(0.01), 0.5)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not job.done()
        assert state["worker"].is_alive()
        await owner.close(0.01)
        assert not job.done()
        state["loop"].call_soon_threadsafe(state["release"].set)
        assert await job.wait(1) == "finished-after-close"
        await wait_until(lambda: not state["worker"].is_alive())
    finally:
        if state["worker"].is_alive():
            state["loop"].call_soon_threadsafe(state["release"].set)
        await owner.close()


async def test_blocked_loop_thread_does_not_prevent_process_exit() -> None:
    script = """
import asyncio
import threading
from robot_agent_runtime.hosting.thread_runtime import LoopThread

async def main():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    owner = LoopThread('permanently-blocked')
    def factory():
        loop.call_soon_threadsafe(entered.set)
        threading.Event().wait()
    await owner.start()
    owner.submit(factory)
    await entered.wait()
    await owner.close(0.01)

asyncio.run(main())
print('closed', flush=True)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-X", "pycache_prefix=/tmp/robot-core-loop-subprocess-tests",
        "-W", "error", "-c", script,
        cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0, stderr.decode()
        assert stdout.strip() == b"closed"
        assert stderr == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def test_fatal_job_completion_cannot_exit_the_callers_event_loop() -> None:
    script = """
import asyncio
from robot_agent_runtime.errors import InternalError
from robot_agent_runtime.hosting.thread_runtime import LoopThread

async def main():
    owner = LoopThread('fatal-completion')
    await owner.start()
    try:
        for exception in (SystemExit(7), KeyboardInterrupt(), GeneratorExit(), BaseException()):
            async def fail():
                raise exception
            job = owner.submit(fail)
            results = await asyncio.gather(asyncio.create_task(job.wait()), return_exceptions=True)
            assert isinstance(results[0], InternalError)
            assert results[0].info.details['exception'] == type(exception).__name__
    finally:
        await owner.close()

asyncio.run(main())
print('caller-survived', flush=True)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-X", "pycache_prefix=/tmp/robot-core-loop-subprocess-tests",
        "-W", "error", "-c", script,
        cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0, stderr.decode()
        assert stdout.strip() == b"caller-survived"
        assert stderr == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()

