import asyncio
import concurrent.futures
import math
import threading
from collections.abc import Awaitable, Callable
from contextvars import Context
from typing import Any, Generic, TypeVar

from robot_agent_runtime.errors import ExtensionCapacityExceeded, InternalError, InvalidRequest
from robot_agent_runtime.hosting.execution_context import CONTROL_OPERATION


T = TypeVar("T")


def _consume_future(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


async def _wait_future(
    future: concurrent.futures.Future[T], timeout_s: float | None = None,
) -> T:
    wrapped = asyncio.wrap_future(future)
    wrapped.add_done_callback(_consume_future)
    protected = asyncio.shield(wrapped)
    if timeout_s is None:
        return await protected
    return await asyncio.wait_for(protected, timeout_s)


class LoopJob(Generic[T]):
    """Separate a cancellation request from proof that external work has stopped.

    The completion future belongs to the actual invocation. Waiting, timing out,
    or cancelling a waiter never changes it. A queued invocation can finish as
    cancelled immediately because its factory has not acquired the thread yet.
    cancel_if_queued atomically revokes only unstarted work. A caller can then
    disable an uncertain port before requesting cancellation of started work.
    """

    def __init__(
        self, owner: "LoopThread", factory: Callable[[], Awaitable[T]], *,
        control: bool = False, cancellable: bool = True,
    ) -> None:
        self._owner = owner
        self._factory: Callable[[], Awaitable[T]] | None = factory
        self._completion: concurrent.futures.Future[T] = concurrent.futures.Future()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._cancel_requested = False
        self._control = control
        self._cancellable = cancellable

    async def wait(self, timeout_s: float | None = None) -> T:
        return await _wait_future(self._completion, timeout_s)

    def cancel(self) -> None:
        self._owner._cancel(self)

    def cancel_if_queued(self) -> bool:
        return self._owner._cancel(self, only_if_queued=True)

    def done(self) -> bool:
        return self._completion.done()

    def started(self) -> bool:
        with self._owner._lock:
            return self._started

    def result(self) -> T:
        return self._completion.result()


class LoopThread:
    """Keep external invocations on one stable daemon event-loop thread.

    Admission counts every registered invocation, including queued factories.
    A blocked or cancellation-resistant invocation retains its thread and slot;
    close requests cancellation and waits only for a bounded grace period. The
    loop stays alive until real completion, and no replacement thread is made.
    Self-closing callbacks may preserve their own job; the loop still stops
    automatically after that job finishes, without needing its caller loop.
    """

    def __init__(self, name: str, max_pending: int = 256, control_capacity: int = 0) -> None:
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending <= 0:
            raise InvalidRequest("max_pending must be a positive integer")
        if type(control_capacity) is not int or control_capacity < 0:
            raise InvalidRequest("control_capacity must be a nonnegative integer")
        self._name = name
        self._max_pending = max_pending
        self._control_capacity = control_capacity
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._stopped: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._jobs: set[LoopJob[Any]] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def stopped(self) -> bool:
        return self._stopped.done()

    async def start(self) -> None:
        with self._lock:
            if self._closed:
                raise InvalidRequest("loop thread is closed")
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
        await _wait_future(self._ready)
        with self._lock:
            if self._closed:
                raise InvalidRequest("loop thread is closed")

    def submit(
        self, factory: Callable[[], Awaitable[T]], *,
        control: bool = False, cancellable: bool = True,
    ) -> LoopJob[T]:
        with self._lock:
            if self._closed:
                raise InvalidRequest("loop thread is closed")
            if self._loop is None or not self._ready.done():
                raise InvalidRequest("loop thread must be started before submitting work")
            capacity = self._control_capacity if control else self._max_pending
            if sum(job._control == control for job in self._jobs) >= capacity:
                raise ExtensionCapacityExceeded(
                    "loop thread has no invocation capacity", {"max_pending": self._max_pending},
                )
            job = LoopJob(self, factory, control=control, cancellable=cancellable)
            self._jobs.add(job)
            self._loop.call_soon_threadsafe(self._launch, job, context=Context())
            return job

    async def call(
        self, factory: Callable[[], Awaitable[T]], timeout_s: float | None = None,
        *, control: bool = False,
    ) -> T:
        job = self.submit(factory, control=control)
        try:
            return await job.wait(timeout_s)
        except BaseException:
            job.cancel()
            raise

    async def close(
        self, grace_s: float = 0.2, *, preserve: LoopJob[Any] | None = None,
    ) -> None:
        if (
            isinstance(grace_s, bool)
            or not isinstance(grace_s, (int, float))
            or not math.isfinite(grace_s)
            or grace_s < 0
        ):
            raise InvalidRequest("grace_s must be finite and nonnegative")
        with self._lock:
            self._closed = True
            jobs = tuple(self._jobs)
            if self._thread is None and not self._stopped.done():
                self._stopped.set_result(None)
        for job in jobs:
            if job is not preserve:
                job.cancel()
        with self._lock:
            self._stop_when_idle()
        if preserve is not None:
            return
        try:
            await _wait_future(self._stopped, grace_s)
        except TimeoutError:
            pass

    def _run(self) -> None:
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with self._lock:
                self._loop = loop
                self._ready.set_result(None)
                self._stop_when_idle()
            loop.run_forever()
        except BaseException as exc:
            with self._lock:
                self._closed = True
                error = InternalError("loop thread failed", {"exception": type(exc).__name__})
                if not self._ready.done():
                    self._ready.set_exception(error)
                for job in tuple(self._jobs):
                    if not job.done():
                        job._completion.set_exception(error)
                    job._factory = None
                self._jobs.clear()
        finally:
            with self._lock:
                self._loop = None
            if loop is not None:
                loop.close()
            with self._lock:
                if not self._stopped.done():
                    self._stopped.set_result(None)

    def _launch(self, job: LoopJob[Any]) -> None:
        with self._lock:
            if job.done():
                return
            task = asyncio.create_task(self._execute(job))
            job._task = task
            self._tasks.add(task)
            task.add_done_callback(lambda finished: self._task_done(job, finished))

    async def _execute(self, job: LoopJob[Any]) -> None:
        CONTROL_OPERATION.set(job._control)
        with self._lock:
            if job.done():
                return
            job._started = True
            factory = job._factory
        try:
            if factory is None:
                raise InternalError("invocation factory was lost before execution")
            result = await factory()
        except asyncio.CancelledError:
            self._finish(job, cancelled=True)
        except BaseException as exc:
            self._finish(job, exception=exc)
        else:
            self._finish(job, result=result)

    def _finish(
        self, job: LoopJob[Any], result: Any = None,
        exception: BaseException | None = None, cancelled: bool = False,
    ) -> None:
        if isinstance(exception, StopIteration) or (
            exception is not None
            and not isinstance(exception, (Exception, asyncio.CancelledError))
        ):
            exception = InternalError(
                "Invocation factory raised an unsafe completion exception",
                {"exception": type(exception).__name__},
            )
        with self._lock:
            if not job.done():
                if cancelled:
                    job._completion.cancel()
                elif exception is not None:
                    job._completion.set_exception(exception)
                else:
                    job._completion.set_result(result)
            job._factory = None
            self._jobs.discard(job)

    def _task_done(self, job: LoopJob[Any], task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            self._finish(job, cancelled=True)
        except BaseException as exc:
            self._finish(job, exception=exc)
        with self._lock:
            self._tasks.discard(task)
            job._task = None
            self._stop_when_idle()

    def _cancel(self, job: LoopJob[Any], *, only_if_queued: bool = False) -> bool:
        with self._lock:
            if only_if_queued and job._started:
                return False
            if job.done() or job._cancel_requested:
                return not job._started
            if job._started and not job._cancellable:
                return False
            job._cancel_requested = True
            if not job._started:
                job._completion.cancel()
                job._factory = None
                self._jobs.discard(job)
            if self._loop is not None and job._task is not None:
                self._loop.call_soon_threadsafe(job._task.cancel)
            self._stop_when_idle()
            return not job._started

    def _stop_when_idle(self) -> None:
        if self._closed and not self._jobs and not self._tasks and self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
