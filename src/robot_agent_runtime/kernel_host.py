import asyncio
import threading
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from robot_agent_runtime.boundary import bounded_snapshot
from robot_agent_runtime.core import RuntimeCore
from robot_agent_runtime.errors import (
    EventStreamOverflow,
    ExtensionCapacityExceeded,
    ExtensionTimeout,
    InvalidRequest,
)
from robot_agent_runtime.models import Event, TaskRequest
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.thread_runtime import LoopJob, LoopThread


_EMPTY = object()
_END = object()


class _EventStream:
    """Keep generator ownership on the kernel and bound its shutdown backlog.

    Buffered events remain replayable after the kernel stops. Overflow reports
    the last event actually delivered to this consumer, so replay can recover
    every omitted event even when shutdown finished before the consumer resumed.
    """

    def __init__(
        self, source: Callable[[], AsyncGenerator[Event, None]], task_id: str,
        after_sequence: int, options: RuntimeOptions,
    ) -> None:
        self._source_factory = source
        self._source: AsyncGenerator[Event, None] | None = None
        self._task_id = task_id
        self._options = options
        self._pull_lock = asyncio.Lock()
        self._buffer_lock = threading.Lock()
        self._buffer: deque[Event] = deque()
        self._error: BaseException | None = None
        self._overflowed = False
        self._incomplete = False
        self._delivered = after_sequence
        self._offline = False
        self._ended = False

    def _snapshot(self, event: Event) -> Event:
        return bounded_snapshot(
            event, max_bytes=self._options.max_snapshot_bytes,
            max_depth=self._options.max_snapshot_depth,
        )

    def delivered(self, event: Event) -> None:
        with self._buffer_lock:
            self._delivered = event.sequence

    def abort_close(self) -> None:
        with self._buffer_lock:
            if not self._offline:
                self._incomplete = True
                self._offline = True

    def offline_next(self) -> Event | object:
        with self._buffer_lock:
            if not self._offline:
                return _EMPTY
            if self._buffer:
                return self._buffer.popleft()
            if self._overflowed:
                self._overflowed = False
                raise EventStreamOverflow(
                    "Shutdown event buffer overflow; resubscribe to replay missed events",
                    details={
                        "task_id": self._task_id, "after_sequence": self._delivered,
                        "queue_size": self._options.max_event_buffer,
                    },
                )
            if self._incomplete:
                self._incomplete = False
                raise EventStreamOverflow(
                    "Event stream shutdown was incomplete; resubscribe to replay missed events",
                    details={
                        "task_id": self._task_id, "after_sequence": self._delivered,
                        "reason": "shutdown_incomplete",
                    },
                )
            if self._error is not None:
                error, self._error = self._error, None
                raise error
            return _END

    async def pull(self) -> Event | object:
        async with self._pull_lock:
            buffered = self.offline_next()
            if buffered is not _EMPTY:
                return buffered
            if self._ended:
                return _END
            if self._source is None:
                self._source = self._source_factory()
            try:
                return self._snapshot(await anext(self._source))
            except StopAsyncIteration:
                self._ended = True
                return _END

    async def drain(self) -> None:
        async with self._pull_lock:
            try:
                with self._buffer_lock:
                    offline = self._offline
                if not self._ended and not offline and self._source is not None:
                    async for event in self._source:
                        with self._buffer_lock:
                            if self._offline:
                                break
                            if len(self._buffer) >= self._options.max_event_buffer:
                                self._overflowed = True
                                break
                            self._buffer.append(self._snapshot(event))
            except Exception as error:
                with self._buffer_lock:
                    if not self._offline:
                        self._error = error
            finally:
                if self._source is not None:
                    await self._source.aclose()
                with self._buffer_lock:
                    self._offline = True
                self._ended = True

    async def close(self) -> None:
        async with self._pull_lock:
            if self._source is not None:
                await self._source.aclose()
            self._ended = True
            with self._buffer_lock:
                self._buffer.clear()
                self._error = None
                self._overflowed = False
                self._incomplete = False
                self._offline = True


class KernelHost:
    """Own core lifecycle and asyncio state on one dedicated daemon loop.

    Concurrent callers share actual lifecycle completion, independent of their
    event loops or cancellation. Admission gives requests, subscriptions and
    controls distinct budgets; lifecycle work has reserved slots so shutdown
    remains available when clients have filled all three budgets.
    """

    def __init__(self, core: RuntimeCore, options: RuntimeOptions | None = None) -> None:
        self._core = core
        self._options = options or RuntimeOptions()
        self._loop = LoopThread(
            "robot-kernel",
            self._options.max_pending_calls + self._options.max_subscriptions,
            control_capacity=self._options.max_control_calls + 3,
        )
        self._admission_lock = threading.Lock()
        self._request_jobs: set[LoopJob[Any]] = set()
        self._control_jobs: set[LoopJob[Any]] = set()
        self._lifecycle_lock: asyncio.Lock | None = None
        self._start_job: LoopJob[None] | None = None
        self._close_job: LoopJob[None] | None = None
        self._streams: set[_EventStream] = set()
        self._started = False
        self._closing = False
        self._closed = False
        self._loop_available = False

    def _kernel_lifecycle_lock(self) -> asyncio.Lock:
        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        return self._lifecycle_lock

    async def start(self) -> None:
        with self._admission_lock:
            if self._closing or self._closed:
                raise InvalidRequest("Kernel host is closed")
            if self._started:
                return
        await self._loop.start()
        with self._admission_lock:
            if self._closing or self._closed:
                raise InvalidRequest("Kernel host is closed")
            self._loop_available = True
            if self._start_job is None or self._start_job.done():
                if self._started:
                    return
                self._start_job = self._loop.submit(
                    self._start_core, control=True, cancellable=False,
                )
            job = self._start_job
        await job.wait()

    async def _start_core(self) -> None:
        async with self._kernel_lifecycle_lock():
            await self._core.isolate(self._options)
            await self._core.start()
            with self._admission_lock:
                self._started = True

    async def _settle(self, job: LoopJob[Any]) -> None:
        try:
            await job.wait(self._options.port_timeout_s + self._options.cancel_grace_s)
        except (Exception, asyncio.CancelledError):
            pass

    async def _begin_close(self) -> LoopJob[None]:
        with self._admission_lock:
            self._closing = True
            if self._close_job is not None:
                return self._close_job
        interrupted = False
        while True:
            try:
                await self._loop.start()
                break
            except asyncio.CancelledError:
                interrupted = True
            except InvalidRequest:
                with self._admission_lock:
                    job = self._close_job
                if job is None:
                    raise
                if interrupted:
                    raise asyncio.CancelledError
                return job
        with self._admission_lock:
            if self._close_job is None:
                self._loop_available = True
                self._close_job = self._loop.submit(
                    self._shutdown, control=True, cancellable=False,
                )
            job = self._close_job
        if interrupted:
            raise asyncio.CancelledError
        return job

    async def close(self) -> None:
        job = await self._begin_close()
        try:
            await job.wait()
        finally:
            if job.done():
                await self._loop.close(self._options.cancel_grace_s)

    async def _shutdown(self) -> None:
        try:
            async with self._kernel_lifecycle_lock():
                await self._core.isolate(self._options)
                self._core._event_drainer = self._drain_streams
                try:
                    await self._core.close()
                finally:
                    self._core._event_drainer = None
        finally:
            with self._admission_lock:
                self._loop_available = False
                self._started = False
                self._closed = True
            await self._loop.close(0)

    async def _drain_streams(self) -> None:
        with self._admission_lock:
            streams = tuple(self._streams)

        async def drain() -> None:
            for stream in streams:
                await stream.drain()
            with self._admission_lock:
                self._streams.clear()

        job: LoopJob[None] | None = None
        try:
            job = self._loop.submit(drain, control=True)
            await job.wait(self._options.port_timeout_s + self._options.cancel_grace_s)
        except BaseException as error:
            for stream in streams:
                stream.abort_close()
            if job is not None:
                job.cancel()
            if isinstance(error, TimeoutError):
                raise ExtensionTimeout(
                    "Kernel event subscriptions did not finish closing",
                ) from error
            raise

    def _submit_request(
        self, factory: Callable[[], Awaitable[Any]], *, control: bool = False,
    ) -> LoopJob[Any]:
        with self._admission_lock:
            if not self._started or self._closing or self._closed:
                raise InvalidRequest("Kernel host is not running")
            jobs = self._control_jobs if control else self._request_jobs
            jobs.difference_update(job for job in tuple(jobs) if job.done())
            limit = self._options.max_control_calls if control else self._options.max_pending_calls
            if len(jobs) >= limit:
                raise ExtensionCapacityExceeded("Kernel host has no invocation capacity")
            job = self._loop.submit(factory, control=control)
            jobs.add(job)
            return job

    def _snapshot(self, value: Any) -> Any:
        return bounded_snapshot(
            value, max_bytes=self._options.max_snapshot_bytes,
            max_depth=self._options.max_snapshot_depth,
        )

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "submit_task":
            source = args[0] if args else kwargs.get("request")
            request = self._snapshot(source)
            if type(request) is not TaskRequest:
                raise InvalidRequest("Task request must be a TaskRequest")
            copied_args, copied_kwargs = (request,), {}
        elif method in {"get_task_status", "cancel_task"}:
            task_id = args[0] if args else kwargs.get("task_id")
            if not isinstance(task_id, str):
                raise InvalidRequest("task_id must be a string")
            copied_args, copied_kwargs = (self._snapshot(task_id),), {}
        elif method == "list_skills":
            copied_args, copied_kwargs = (), {}
        else:
            raise InvalidRequest("Unknown kernel method", details={"method": method})

        async def invoke() -> Any:
            return self._snapshot(await getattr(self._core, method)(*copied_args, **copied_kwargs))

        job = self._submit_request(
            invoke, control=method in {"get_task_status", "cancel_task"},
        )
        try:
            return await job.wait()
        except asyncio.CancelledError:
            job.cancel()
            raise

    async def subscribe_task_events(
        self, task_id: str, after_sequence: int = 0
    ) -> AsyncGenerator[Event, None]:
        if not isinstance(task_id, str):
            raise InvalidRequest("task_id must be a string")
        if type(after_sequence) is not int or after_sequence < 0:
            raise InvalidRequest("after_sequence must be a non-negative integer")
        task_id = self._snapshot(task_id)
        stream = _EventStream(
            lambda: self._core.subscribe_task_events(task_id, after_sequence),
            task_id, after_sequence, self._options,
        )
        with self._admission_lock:
            if not self._started or self._closing or self._closed:
                raise InvalidRequest("Kernel host is not running")
            if len(self._streams) >= self._options.max_subscriptions:
                raise ExtensionCapacityExceeded("Kernel host has no subscription capacity")
            self._streams.add(stream)
        pending: LoopJob[Any] | None = None
        try:
            while True:
                event = stream.offline_next()
                if event is _EMPTY:
                    with self._admission_lock:
                        available = self._loop_available
                        if available:
                            try:
                                pending = self._loop.submit(stream.pull)
                            except InvalidRequest:
                                available = False
                    if not available:
                        event = stream.offline_next()
                        if event is _EMPTY:
                            stream.abort_close()
                            event = stream.offline_next()
                    else:
                        event = await pending.wait()
                        pending = None
                if event is _END:
                    return
                stream.delivered(event)
                yield event
        finally:
            if pending is not None:
                pending.cancel()
                await self._settle(pending)
            try:
                if self._loop_available and not self._closing:
                    await self._loop.call(stream.close)
            except InvalidRequest:
                if not self._closing:
                    raise
            finally:
                with self._admission_lock:
                    self._streams.discard(stream)
