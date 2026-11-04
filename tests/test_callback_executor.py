import asyncio
import gc
import sys
import threading
import weakref
from collections.abc import Generator
from pathlib import Path

import pytest

from robot_agent_runtime.errors import AgentRuntimeError, InvalidRequest
from robot_agent_runtime.sdk.callback_executor import CallbackExecutor


async def test_callback_executor_blocked_worker_does_not_prevent_process_exit() -> None:
    script = """
import asyncio
import threading
from robot_agent_runtime.sdk.callback_executor import CallbackExecutor

async def main():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    executor = CallbackExecutor()
    def block():
        loop.call_soon_threadsafe(entered.set)
        threading.Event().wait()
    executor.submit(block)
    await entered.wait()
    await executor.close()

asyncio.run(main())
print('closed', flush=True)
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script,
        cwd=Path(__file__).resolve().parents[1],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0, stderr.decode()
        assert stdout.strip() == b"closed"
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


async def test_callback_executor_close_cancels_queued_work() -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    calls: list[str] = []
    executor = CallbackExecutor(max_workers=1)

    def block() -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()

    running = executor.submit(block)
    queued: asyncio.Future[object] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        queued = executor.submit(calls.append, "queued")
        await executor.close()
        release.set()
        await asyncio.gather(running, queued, return_exceptions=True)
        assert queued.cancelled()
        assert running.cancelled()
        assert not calls
    finally:
        release.set()
        await executor.close()


async def test_callback_executor_cancelling_queued_work_reclaims_capacity() -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    calls: list[str] = []
    executor = CallbackExecutor(max_workers=1, max_pending=1)

    def block() -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()

    running = executor.submit(block)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        cancelled = executor.submit(calls.append, "cancelled")
        executor.cancel(cancelled)
        replacement = executor.submit(calls.append, "replacement")
        release.set()
        await asyncio.wait_for(asyncio.gather(running, replacement), 1)
        assert cancelled.cancelled()
        assert calls == ["replacement"]
    finally:
        release.set()
        await executor.close()


async def test_callback_executor_rejects_full_queue() -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    executor = CallbackExecutor(max_workers=1, max_pending=1)

    def block() -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()

    running = executor.submit(block)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        queued = executor.submit(lambda: None)
        with pytest.raises(AgentRuntimeError) as caught:
            executor.submit(lambda: None)
        assert caught.value.code == "LISTENER_CALLBACK_QUEUE_FULL"
        release.set()
        await asyncio.wait_for(asyncio.gather(running, queued), 1)
    finally:
        release.set()
        await executor.close()


async def test_callback_executor_queue_deadline_prevents_late_delivery() -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    calls: list[str] = []
    executor = CallbackExecutor(max_workers=1, queue_timeout_s=0.02)

    def block() -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()

    running = executor.submit(block)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        queued = executor.submit(calls.append, "expired")
        with pytest.raises(AgentRuntimeError) as caught:
            await asyncio.wait_for(queued, 1)
        assert caught.value.code == "LISTENER_CALLBACK_TIMEOUT"
        release.set()
        await asyncio.wait_for(running, 1)
        await executor.submit(lambda: None)
        assert not calls
    finally:
        release.set()
        await executor.close()


async def test_callback_executor_close_discards_late_exception() -> None:
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    executor = CallbackExecutor(max_workers=1)

    def fail() -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        loop.call_soon_threadsafe(finished.set)
        raise ValueError("late failure")

    future = executor.submit(fail)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await executor.close()
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        assert future.cancelled()
    finally:
        release.set()
        await asyncio.gather(future, return_exceptions=True)
        await executor.close()


async def test_callback_executor_rejects_submissions_after_close() -> None:
    executor = CallbackExecutor()
    await executor.close()
    with pytest.raises(InvalidRequest):
        executor.submit(lambda: None)


async def test_callback_executor_idle_workers_release_callback_references() -> None:
    class Callback:
        def __call__(self) -> None:
            return None

    executor = CallbackExecutor(max_workers=1)
    callback = Callback()
    reference = weakref.ref(callback)
    try:
        await executor.submit(callback)
        del callback
        gc.collect()
        assert reference() is None
    finally:
        await executor.close()


async def test_callback_executor_cancelled_running_work_retains_worker_slot() -> None:
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    executor = CallbackExecutor(max_workers=1, queue_timeout_s=0.02)
    calls: list[str] = []

    def block() -> None:
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        loop.call_soon_threadsafe(finished.set)

    running = executor.submit(block)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        executor.cancel(running)
        queued = executor.submit(calls.append, "replacement")
        with pytest.raises(AgentRuntimeError) as caught:
            await asyncio.wait_for(queued, 1)
        assert caught.value.code == "LISTENER_CALLBACK_TIMEOUT"
        assert running.cancelled()
        assert not calls
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await executor.close()


async def test_callback_executor_abandoned_cleanup_error_does_not_kill_worker() -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    executor = CallbackExecutor(max_workers=1, queue_timeout_s=0.2)

    class Suspend:
        def __await__(self) -> Generator[None, None, None]:
            yield None

    async def cleanup() -> None:
        try:
            await Suspend()
        finally:
            raise ValueError("abandoned coroutine cleanup failed")

    def callback() -> object:
        result = cleanup()
        result.send(None)
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return result

    future = executor.submit(callback)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        executor.cancel(future)
        release.set()
        assert await asyncio.wait_for(executor.submit(lambda: "alive"), 1) == "alive"
        assert future.cancelled()
    finally:
        release.set()
        await executor.close()
