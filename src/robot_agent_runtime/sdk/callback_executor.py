import asyncio
import inspect
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from robot_agent_runtime.errors import (
    InvalidRequest,
    ListenerCallbackError,
    ListenerCallbackQueueFull,
    ListenerCallbackTimeout,
)


@dataclass(eq=False)
class _CallbackWork:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[Any]
    callback: Callable[..., Any] | None
    args: tuple[Any, ...]
    deadline: float
    state: Literal["queued", "running", "finished", "cancelled", "abandoned"] = "queued"
    timer: asyncio.TimerHandle | None = None
    result: Any = None
    error: BaseException | None = None


class CallbackExecutor:
    """Bound synchronous user code without joining blocked workers on exit.

    Only SDK callbacks use these daemon threads; kernel state stays on asyncio.
    Workers claim queued work under the same condition used for cancellation.
    Claimed work is in flight and cannot be interrupted, even if its caller
    cancels. It retains its worker slot until it returns; no replacement threads
    are created. Queue deadlines and capacity bound the effect on other callers.

    submit/cancel/close belong to the caller's event loop. Workers only post
    completion with call_soon_threadsafe; Future and timer operations stay on the
    loop. Closing cancels queued work and abandons in-flight results without
    waiting for user code. Threads use no executor registry or exit-time joins.
    Abandoned native coroutines are closed; errors from that cleanup are also
    abandoned so they cannot kill a worker or interrupt SDK shutdown.
    """

    def __init__(
        self, max_workers: int = 4, *, max_pending: int = 64, queue_timeout_s: float = 5.0,
    ) -> None:
        if (
            max_workers < 1 or max_pending < 1
            or not math.isfinite(queue_timeout_s) or queue_timeout_s <= 0
        ):
            raise InvalidRequest("Callback capacity and queue timeout must be positive")
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._queue_timeout_s = queue_timeout_s
        self._condition = threading.Condition()
        self._queue: deque[_CallbackWork] = deque()
        self._jobs: dict[asyncio.Future[Any], _CallbackWork] = {}
        self._started = False
        self._closed = False

    def submit(self, callback: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
        loop = asyncio.get_running_loop()
        with self._condition:
            if self._closed:
                raise InvalidRequest("Cannot submit a callback to a closed executor")
            if len(self._queue) >= self._max_pending:
                raise ListenerCallbackQueueFull("Synchronous callback queue is full")
            future: asyncio.Future[Any] = loop.create_future()
            work = _CallbackWork(
                loop, future, callback, args, time.monotonic() + self._queue_timeout_s,
            )
            self._jobs[future] = work
            self._queue.append(work)
            work.timer = loop.call_later(self._queue_timeout_s, self._expire, work)
            future.add_done_callback(self._future_done)
            if not self._started:
                self._started = True
                for index in range(self._max_workers):
                    threading.Thread(
                        target=self._worker, name=f"rar-callback-{index}", daemon=True,
                    ).start()
            self._condition.notify()
        return future

    def cancel(self, future: asyncio.Future[Any]) -> None:
        with self._condition:
            work = self._jobs.pop(future, None)
            if work is None:
                return
            if work.state == "queued":
                self._queue.remove(work)
                work.state = "cancelled"
            else:
                work.state = "abandoned"
            result, work.result = work.result, None
            work.callback, work.args, work.error = None, (), None
        def cancel_on_owner() -> None:
            if work.timer is not None:
                work.timer.cancel()
            future.cancel()

        try:
            if asyncio.get_running_loop() is work.loop:
                cancel_on_owner()
            else:
                work.loop.call_soon_threadsafe(cancel_on_owner)
        except RuntimeError:
            if not work.loop.is_closed():
                work.loop.call_soon_threadsafe(cancel_on_owner)
        self.discard_result(result)

    async def close(self) -> None:
        with self._condition:
            self._closed = True
            for future in tuple(self._jobs):
                self.cancel(future)
            self._condition.notify_all()

    def _future_done(self, future: asyncio.Future[Any]) -> None:
        if future.cancelled():
            self.cancel(future)

    def _expire(self, work: _CallbackWork) -> None:
        with self._condition:
            if work.state != "queued":
                return
            self._queue.remove(work)
            self._jobs.pop(work.future, None)
            work.state = "cancelled"
            work.callback, work.args = None, ()
        if not work.future.done():
            work.future.set_exception(ListenerCallbackTimeout("Synchronous callback expired in queue"))

    def _worker(self) -> None:
        while self._run_next():
            pass

    def _run_next(self) -> bool:
        with self._condition:
            self._condition.wait_for(lambda: self._closed or bool(self._queue))
            if self._closed:
                return False
            work = self._queue.popleft()
            callback, args = work.callback, work.args
            work.callback, work.args = None, ()
            if time.monotonic() >= work.deadline:
                work.state = "finished"
                work.error = ListenerCallbackTimeout("Synchronous callback expired in queue")
                callback = None
            else:
                work.state = "running"
        if callback is not None:
            result: Any = None
            error: BaseException | None = None
            try:
                result = callback(*args)
            except BaseException as caught:
                if (
                    isinstance(caught, (Exception, asyncio.CancelledError))
                    and not isinstance(caught, StopIteration)
                ):
                    error = caught
                else:
                    error = ListenerCallbackError(f"Task callback raised {type(caught).__name__}")
            with self._condition:
                abandoned = work.state == "abandoned"
                if not abandoned:
                    work.state, work.result, work.error = "finished", result, error
            if abandoned:
                self.discard_result(result)
                return True
        self._post(work)
        return True

    def _post(self, work: _CallbackWork) -> None:
        try:
            work.loop.call_soon_threadsafe(self._deliver, work)
        except RuntimeError:
            with self._condition:
                self._jobs.pop(work.future, None)
                work.state = "abandoned"
                result, work.result, work.error = work.result, None, None
            self.discard_result(result)

    def _deliver(self, work: _CallbackWork) -> None:
        with self._condition:
            if work.state != "finished":
                return
            self._jobs.pop(work.future, None)
            result, error = work.result, work.error
            work.result, work.error = None, None
        if work.timer is not None:
            work.timer.cancel()
        if work.future.done():
            self.discard_result(result)
        elif error is not None:
            work.future.set_exception(error)
        else:
            work.future.set_result(result)

    @staticmethod
    def discard_result(result: Any) -> None:
        if inspect.iscoroutine(result):
            try:
                result.close()
            except BaseException:
                pass
