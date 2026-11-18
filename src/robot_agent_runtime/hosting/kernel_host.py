import asyncio
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from typing import Any

from robot_agent_runtime.hosting.boundary import bounded_snapshot
from robot_agent_runtime.kernel.core import RuntimeCore
from robot_agent_runtime.errors import (
    ExtensionCapacityExceeded,
    ExtensionTimeout,
    InvalidRequest,
)
from robot_agent_runtime.models import Event, PlatformEvent, TaskRequest
from robot_agent_runtime.hosting.notification_host import NotificationHost
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.hosting.isolation import (
    IsolatedAdmissionValidator, IsolatedPorts, isolate_ports,
)
from robot_agent_runtime.ports import PlatformEventRepository, PlatformEventSource, Reconciler, RobotStateProvider, Skill, TaskRepository
from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository
from robot_agent_runtime.thread_binding import ThreadBinding
from robot_agent_runtime.hosting.thread_runtime import LoopJob, LoopThread

from robot_agent_runtime.hosting.event_stream import HostedEventStream, _EMPTY, _END


class KernelHost:
    """Own core lifecycle and asyncio state on one dedicated daemon loop.

    Concurrent callers share actual lifecycle completion, independent of their
    event loops or cancellation. Admission gives requests, subscriptions and
    controls distinct budgets; lifecycle work has reserved slots so shutdown
    remains available when clients have filled all three budgets.
    """

    def __init__(
        self,
        repository: TaskRepository | ThreadBinding[TaskRepository],
        skills: Mapping[str, Skill | ThreadBinding[Skill]],
        reconciler: Reconciler | ThreadBinding[Reconciler] | None = None,
        allowed_permissions: set[str] | None = None,
        robot_state: RobotStateProvider | ThreadBinding[RobotStateProvider] | None = None,
        *,
        options: RuntimeOptions | None = None,
        event_source: PlatformEventSource | ThreadBinding[PlatformEventSource] | None = None,
        event_repository: PlatformEventRepository | ThreadBinding[PlatformEventRepository] | None = None,
    ) -> None:
        self._skills = dict(skills)
        if any(type(name) is not str for name in self._skills):
            raise InvalidRequest("Skill registry names must be plain strings")
        self._permissions = set(allowed_permissions) if allowed_permissions is not None else None
        if self._permissions is not None and any(type(value) is not str for value in self._permissions):
            raise InvalidRequest("Permissions must contain plain strings")
        self._inputs = (repository, reconciler, robot_state)
        self._event_source = event_source
        self._event_repository = (event_repository if event_repository is not None else
                                  InMemoryPlatformEventRepository() if event_source is not None else None)
        self._notifications: NotificationHost | None = None
        self._core: RuntimeCore | None = None
        self._ports: IsolatedPorts | None = None
        self._prepare_failure: Exception | None = None
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
        self._streams: set[HostedEventStream] = set()
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
            core = await self._prepare_core()
            await core.start()
            if self._notifications is not None:
                await self._notifications.start()
            with self._admission_lock:
                self._started = True

    def _create_core(self, ports: IsolatedPorts) -> RuntimeCore:
        return RuntimeCore(
            ports.repository, ports.skills, ports.reconciler, self._permissions, ports.robot_state,
            options=self._options, validator=IsolatedAdmissionValidator(ports, self._permissions),
        )

    async def _prepare_core(self) -> RuntimeCore:
        if self._prepare_failure is not None:
            raise self._prepare_failure
        if self._core is not None:
            return self._core
        repository, reconciler, robot_state = self._inputs
        try:
            self._ports = isolate_ports(repository, self._skills, reconciler, robot_state, self._options,
                                        event_source=self._event_source, event_repository=self._event_repository)
            await self._ports.prepare()
            self._core = self._create_core(self._ports)
            if self._ports.event_repository is not None:
                self._notifications = NotificationHost(self._ports.event_repository, self._ports.event_source, self._options)
        except BaseException as error:
            self._prepare_failure = (error if isinstance(error, Exception) else
                                     InvalidRequest("Host preparation interrupted; workers cannot be replaced"))
            if self._ports is not None:
                await self._ports.close()
            raise
        return self._core

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

    async def begin_close(self) -> None:
        await self._begin_close()

    async def _shutdown(self) -> None:
        errors: list[Exception] = []
        try:
            async with self._kernel_lifecycle_lock():
                core = await self._prepare_core()
                try:
                    await core.stop()
                except Exception:
                    # close() shares and reports the same stop result once.
                    pass
                try:
                    await self._drain_streams()
                except Exception as error:
                    errors.append(error)
                if self._notifications is not None:
                    try:
                        await self._notifications.close()
                    except Exception as error:
                        errors.append(error)
                try:
                    await core.close()
                except Exception as error:
                    if isinstance(error, ExceptionGroup):
                        errors.extend(error.exceptions)
                    else:
                        errors.append(error)
                finally:
                    if self._ports is not None:
                        await self._ports.close()
                if errors:
                    raise ExceptionGroup("Kernel host shutdown errors", errors)
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
        async with aclosing(self._subscribe_task_stream(
            lambda: self._core.subscribe_task_events(task_id, after_sequence), task_id, after_sequence,
        )) as stream:
            async for value in stream:
                yield value

    async def subscribe_all_task_events(self, event_types: set[str] | None = None) -> AsyncGenerator[Event, None]:
        if event_types is not None and (type(event_types) not in (set, frozenset) or any(type(value) is not str or not value.strip() for value in event_types)):
            raise InvalidRequest("event_types must be a set of nonempty strings")
        selected = self._snapshot(event_types)
        async with aclosing(self._subscribe_task_stream(lambda: self._core.subscribe_all_task_events(selected), "*", 0)) as stream:
            async for value in stream:
                yield value

    async def _subscribe_task_stream(self, factory: Callable[[], AsyncGenerator[Event, None]], task_id: str, after_sequence: int) -> AsyncGenerator[Event, None]:
        stream = HostedEventStream(factory, task_id, after_sequence, self._options)
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

    async def subscribe_platform_events(
        self, after_cursor: int = 0, event_types: set[str] | None = None,
    ) -> AsyncGenerator[PlatformEvent, None]:
        with self._admission_lock:
            if not self._started or self._closing or self._notifications is None:
                raise InvalidRequest("Platform events are not configured or the runtime is not running")
            notifications = self._notifications
        stream = notifications.subscribe(after_cursor, event_types)
        try:
            async for value in stream:
                yield value
        finally:
            await stream.aclose()
