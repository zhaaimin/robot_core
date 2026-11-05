import asyncio
import inspect
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import suppress
from typing import Any

import robot_agent_runtime.errors as runtime_errors
from robot_agent_runtime.hosting.boundary import bounded_snapshot
from robot_agent_runtime.errors import (
    AgentRuntimeError,
    ExecutionUnresponsive,
    ExtensionCapacityExceeded,
    ExtensionTimeout,
    InternalError,
    InvalidRequest,
    SnapshotTooLarge,
    TaskCancelled,
)
from robot_agent_runtime.models import (
    Event,
    ErrorInfo,
    SkillContext,
    SkillCall,
    SkillDefinition,
    SkillResult,
    Task,
    TaskState,
)
from robot_agent_runtime.hosting.execution_context import CONTROL_OPERATION
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.ports import Reconciler, RobotStateProvider, Skill, TaskRepository
from robot_agent_runtime.kernel.policy import PolicyEnforcer
from robot_agent_runtime.kernel.registry import SkillRegistry
from robot_agent_runtime.hosting.thread_runtime import LoopJob, LoopThread
from robot_agent_runtime.thread_binding import ThreadBinding, unwrap_thread_binding


def _snapshot(value: Any, options: RuntimeOptions) -> Any:
    return bounded_snapshot(value, max_bytes=options.max_snapshot_bytes,
                            max_depth=options.max_snapshot_depth)


class _Failure:
    """Carry a clean exception without its external traceback or exception chain."""

    __slots__ = ("error",)

    def __init__(self, error: BaseException) -> None:
        self.error = error


def _unwrap(value: Any) -> Any:
    if type(value) is _Failure:
        raise value.error from None
    return value


def _normalize_error(error: BaseException, options: RuntimeOptions) -> BaseException:
    try:
        if isinstance(error, AgentRuntimeError):
            info = _snapshot(object.__getattribute__(error, "info"), options)
            if type(info) is not ErrorInfo:
                return InternalError("Extension returned an invalid error snapshot")
            name = type.__getattribute__(type(error), "__name__")
            error_type = vars(runtime_errors).get(name, AgentRuntimeError)
            if not isinstance(error_type, type) or not issubclass(error_type, AgentRuntimeError):
                error_type = AgentRuntimeError
            canonical = error_type(info.message, info.details)
            canonical.info = info
            return canonical
        if not isinstance(error, Exception):
            return InternalError("Extension raised an unsafe exception")
        message = _snapshot(str(error), options)
        if type(message) is not str:
            return InternalError("Extension returned an invalid exception message")
        if type(error) in (ValueError, TypeError, TimeoutError, RuntimeError,
                           KeyError, LookupError, AttributeError, OSError):
            return type(error)(message)
        details = _snapshot({"cause": type.__getattribute__(type(error), "__name__"),
                             "message": message}, options)
        return InternalError("Extension operation failed", details=details)
    except SnapshotTooLarge:
        return SnapshotTooLarge("Extension error snapshot exceeds its configured limit")
    except BaseException:
        return InternalError("Extension returned an invalid error snapshot")


async def _invoke(
    factory: Callable[[], Any], options: RuntimeOptions, *, allow_cancel: bool = False,
) -> Any:
    try:
        value = factory()
        if inspect.isawaitable(value):
            value = await value
    except asyncio.CancelledError:
        error = (asyncio.CancelledError() if allow_cancel
                 else InternalError("Extension operation was cancelled"))
        return _Failure(error)
    except BaseException as error:
        return _Failure(_normalize_error(error, options))
    try:
        return _snapshot(value, options)
    except SnapshotTooLarge:
        return _Failure(SnapshotTooLarge("Extension snapshot exceeds its configured limit"))
    except BaseException:
        return _Failure(InternalError("Extension returned an invalid snapshot"))


class _Port:
    """Each external object keeps its own faults on a fixed, possibly shared loop.

    A repository deadline after dispatch leaves the commit outcome unknown, so
    subsequent operations are rejected rather than allowed to overlap it.
    Cancelling a queued job under the runner lock proves it cannot start; only
    then can the started flag safely distinguish queue delay from uncertain work.
    Other ports retain their loop after a deadline; threads are never replaced.
    """

    def __init__(self, target: Any, runner: LoopThread, options: RuntimeOptions) -> None:
        self.target = target
        self.runner = runner
        self.options = options
        self.poisoned = False

    async def call(
        self, method: str, *args: Any, poison: bool = False, write: bool = False,
        factory: Callable[..., Any] | None = None,
    ) -> Any:
        if self.poisoned:
            raise ExtensionTimeout("Extension is unavailable after an uncertain operation")
        copied = _snapshot(args, self.options)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.options.port_timeout_s

        def invoke() -> Any:
            if self.poisoned:
                raise ExtensionTimeout("Extension is unavailable after an uncertain operation")
            if factory is not None:
                return factory(self.target, *copied)
            return getattr(self.target, method)(*copied)

        job = self.runner.submit(lambda: _invoke(invoke, self.options),
                                 control=CONTROL_OPERATION.get())
        try:
            try:
                return _unwrap(await job.wait(timeout_s=self.options.port_timeout_s))
            except asyncio.CancelledError:
                if not write:
                    job.cancel()
                    raise
                remaining = max(0.0, deadline - loop.time())
                authority = asyncio.create_task(job.wait(timeout_s=remaining))
                while not authority.done():
                    try:
                        await asyncio.shield(authority)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                return _unwrap(authority.result())
        except TimeoutError as error:
            queued_cancelled = job.cancel_if_queued()
            if poison and not queued_cancelled:
                self.poisoned = True
            if not queued_cancelled:
                job.cancel()
            raise ExtensionTimeout(
                "Extension operation exceeded its deadline", details={"operation": method}
            ) from error


class _Repository:
    def __init__(self, port: _Port) -> None:
        self._port = port

    async def start(self) -> None:
        await self._port.call("start", poison=True, write=True)

    async def close(self) -> None:
        if not self._port.poisoned:
            await self._port.call("close", poison=True, write=True)

    async def create_task(self, task: Task) -> Task:
        return await self._port.call("create_task", task, poison=True, write=True)

    async def get_task(self, task_id: str) -> Task | None:
        return await self._port.call("get_task", task_id, poison=True)

    async def update_task(self, task: Task) -> Task:
        return await self._port.call("update_task", task, poison=True, write=True)

    async def append_event(self, event: Event) -> Event:
        return await self._port.call("append_event", event, poison=True, write=True)

    async def apply_transition(self, task: Task, event: Event) -> tuple[Task, Event]:
        return await self._port.call("apply_transition", task, event, poison=True, write=True)

    async def list_events(self, task_id: str, after_sequence: int = 0) -> list[Event]:
        return await self._port.call("list_events", task_id, after_sequence, poison=True)

    async def list_events_page(
        self, task_id: str, after_sequence: int = 0, limit: int = 128,
    ) -> list[Event]:
        async def page(target: Any, identity: str, after: int, size: int) -> list[Event]:
            native = getattr(target, "list_events_page", None)
            if native is not None:
                result = native(identity, after, size)
            else:
                result = target.list_events(identity, after)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, list):
                raise InternalError("Repository event pages require a list")
            return list.__getitem__(result, slice(0, size))

        return await self._port.call("list_events_page", task_id, after_sequence, limit,
                                     poison=True, factory=page)

    async def get_nonterminal_tasks(self) -> list[Task]:
        return await self._port.call("get_nonterminal_tasks", poison=True)

    async def find_by_idempotency_key(self, key: str) -> Task | None:
        return await self._port.call("find_by_idempotency_key", key, poison=True)


class _Reconciler:
    def __init__(self, port: _Port) -> None:
        self._port = port

    async def reconcile(self, task: Task) -> TaskState | None:
        return await self._port.call("reconcile", task)


class _RobotState:
    """Pull one snapshot at a time without a deadline on a quiet push source.

    The outstanding pull is cancelled when its consumer closes. Loop shutdown
    remains bounded even if foreign iterator cleanup ignores that cancellation.
    """

    def __init__(self, port: _Port) -> None:
        self._port = port

    async def unsatisfied(self) -> frozenset[str]:
        try:
            return frozenset(await self._port.call("unsatisfied"))
        except Exception as error:
            raise InternalError("Could not read robot state",
                                details={"cause": type(error).__name__}) from error

    async def watch(self) -> AsyncIterator[frozenset[str]]:
        iterator: AsyncIterator[frozenset[str]] | None = None

        async def next_value() -> tuple[bool, frozenset[str]]:
            nonlocal iterator
            if iterator is None:
                iterator = self._port.target.watch().__aiter__()
            try:
                return True, frozenset(await anext(iterator))
            except StopAsyncIteration:
                return False, frozenset()

        async def close_iterator() -> None:
            if iterator is not None:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    await close()

        try:
            while True:
                job = self._port.runner.submit(
                    lambda: _invoke(next_value, self._port.options, allow_cancel=True),
                    control=CONTROL_OPERATION.get(),
                )
                try:
                    available, value = _unwrap(await job.wait())
                except asyncio.CancelledError:
                    job.cancel()
                    raise
                if not available:
                    return
                yield value
        finally:
            with suppress(Exception, asyncio.CancelledError):
                await self._port.runner.call(
                    lambda: _invoke(close_iterator, self._port.options),
                    timeout_s=self._port.options.cancel_grace_s, control=True,
                )


class _CancelBridge:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def bind(self) -> asyncio.Event:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._event = asyncio.Event()
            if self._requested:
                self._event.set()
            return self._event

    def signal(self, cancel: Callable[[], None] | None = None) -> None:
        with self._lock:
            self._requested = True
            if self._loop is not None and self._event is not None:
                loop, event = self._loop, self._event

                def deliver() -> None:
                    event.set()
                    if cancel is not None:
                        loop.call_soon(cancel)

                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(deliver)


class _ProgressBridge:
    """Preserve ordinary progress order and coalesce only a full bounded queue."""

    def __init__(self, callback: Callable[[float], None] | None) -> None:
        self._loop = asyncio.get_running_loop()
        self._callback = callback
        self._lock = threading.Lock()
        self._pending = False
        self._closed = False
        self._values: deque[float] = deque()

    def send(self, value: float) -> None:
        plain_value = float(value)
        with self._lock:
            if self._closed or self._callback is None:
                return
            if len(self._values) == 64:
                self._values[-1] = plain_value
            else:
                self._values.append(plain_value)
            if self._pending:
                return
            self._pending = True
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._deliver)

    def _deliver(self) -> None:
        with self._lock:
            if not self._pending:
                return
            self._pending = False
            if self._closed:
                return
            values = list(self._values)
            self._values.clear()
        if self._callback is not None:
            for value in values:
                self._callback(value)

    def flush(self) -> None:
        self._deliver()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._callback = None
            self._values.clear()


class _Skill:
    """Only copied declarations and results enter the kernel.

    Cancelling a proxy requests cancellation of the real worker task and waits
    a bounded interval for its actual completion. An uncertain stop poisons
    this instance permanently, so later calls cannot repeat physical work.
    """

    def __init__(self, port: _Port) -> None:
        self._port = port
        self._definition: SkillDefinition | None = None
        self._poisoned = False

    @property
    def definition(self) -> SkillDefinition:
        if self._definition is None:
            raise InternalError("Isolated skill declarations have not been prepared")
        return _snapshot(self._definition, self._port.options)

    async def prepare(self) -> None:
        try:
            self._definition = _unwrap(await self._port.runner.call(
                lambda: _invoke(lambda: self._port.target.definition, self._port.options),
                timeout_s=self._port.options.port_timeout_s,
                control=CONTROL_OPERATION.get(),
            ))
        except TimeoutError as error:
            raise ExtensionTimeout("Skill declaration exceeded its deadline") from error

    async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
        if self._poisoned:
            raise ExecutionUnresponsive("Skill is unavailable after an unconfirmed stop")
        if context.cancel_event.is_set():
            raise TaskCancelled("Skill cancelled before dispatch")
        cancellation = _CancelBridge()
        progress = _ProgressBridge(context.progress)
        copied_params = _snapshot(params, self._port.options)
        task_id, execution_id = _snapshot((context.task_id, context.execution_id),
                                          self._port.options)
        has_progress = context.progress is not None
        async def invoke() -> SkillResult:
            local_cancel = cancellation.bind()
            if local_cancel.is_set():
                return _Failure(TaskCancelled("Skill cancelled before dispatch"))
            local_context = SkillContext(
                task_id, execution_id, local_cancel,
                progress.send if has_progress else None,
            )
            return await _invoke(
                lambda: self._port.target.execute(local_context, copied_params),
                self._port.options, allow_cancel=True,
            )

        job = self._port.runner.submit(
            invoke, control=CONTROL_OPERATION.get(),
            cancellable=self.definition.supports_cancel,
        )
        waiter = asyncio.create_task(job.wait())
        requested = asyncio.create_task(context.cancel_event.wait())
        cancelling = False
        try:
            done, _ = await asyncio.wait({waiter, requested}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done and requested not in done and not context.cancel_event.is_set():
                result = _unwrap(await waiter)
                progress.flush()
                return result
            cancellation.signal(job.cancel if self.definition.supports_cancel else None)
            cancelling = True
            return await self._cancel(job)
        except asyncio.CancelledError:
            if cancelling:
                raise
            cancellation.signal(job.cancel if self.definition.supports_cancel else None)
            return await self._cancel(job)
        finally:
            progress.close()
            waiter.cancel()
            requested.cancel()
            cleanup = asyncio.gather(waiter, requested, return_exceptions=True)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue

    async def _cancel(self, job: LoopJob) -> SkillResult:
        if not job.started():
            job.cancel()
        stopped = asyncio.create_task(job.wait(timeout_s=self._port.options.cancel_grace_s))
        while not stopped.done():
            try:
                await asyncio.shield(stopped)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            result = _unwrap(stopped.result())
        except (asyncio.CancelledError, FutureCancelledError):
            if not job.started():
                raise TaskCancelled("Skill cancelled before dispatch") from None
            self._poisoned = True
            raise ExecutionUnresponsive("Skill cancellation did not confirm a physical stop") from None
        except TaskCancelled:
            raise
        except BaseException as error:
            self._poisoned = True
            raise ExecutionUnresponsive(
                "Could not confirm that skill execution stopped",
                details={"cause": type(error).__name__},
            ) from error
        if type(result) is not SkillResult or result.success is not True:
            self._poisoned = True
            raise ExecutionUnresponsive("Skill returned an uncertain cancellation result")
        raise TaskCancelled("Skill completed before cancellation was acknowledged")


class IsolatedPorts:
    """A bounded collection of stable external loop owners.

    Construction never calls user implementations. Preparation starts the
    owners and reads skill declarations there before kernel startup. Shutdown
    never waits indefinitely for external code or creates replacement threads.
    Three control slots beyond the host's public quota keep lifecycle and
    iterator cleanup calls admissible while public queries await external work.
    """

    def __init__(
        self,
        repository: TaskRepository | ThreadBinding[TaskRepository],
        skills: Mapping[str, Skill | ThreadBinding[Skill]],
        reconciler: Reconciler | ThreadBinding[Reconciler] | None,
        robot_state: RobotStateProvider | ThreadBinding[RobotStateProvider] | None,
        options: RuntimeOptions,
    ) -> None:
        originals = [repository, *skills.values()]
        originals.extend(value for value in (reconciler, robot_state) if value is not None)
        unique: dict[int, Any] = {}
        assignments: dict[int, tuple[str, str | int | None]] = {}
        for value in originals:
            binding = value if type(value) is ThreadBinding else ThreadBinding.dedicated(value)
            identity = id(binding.target)
            key = (binding.mode, identity if binding.mode == "dedicated" else binding.group_name)
            if identity in assignments and assignments[identity] != key:
                raise InvalidRequest("The same external object has conflicting thread bindings")
            unique[identity] = binding.target
            assignments[identity] = key
        keys = dict.fromkeys(assignments.values())
        required = len(keys) + 1
        if required > options.max_extension_threads:
            raise ExtensionCapacityExceeded(
                "External thread bindings exceed the configured thread capacity",
                details={"required": required, "maximum": options.max_extension_threads},
            )
        self._options = options
        self._validation = LoopThread("robot-validation", max_pending=options.max_pending_calls,
                                      control_capacity=options.max_control_calls + 3)
        runners: dict[tuple[str, str | int | None], LoopThread] = {}
        for index, key in enumerate(keys):
            label = (f"dedicated-{index}" if key[0] == "dedicated" else
                     f"group-{key[1]}" if key[0] == "group" else "shared")
            runners[key] = LoopThread(f"robot-extension-{label}",
                                      max_pending=options.max_pending_calls,
                                      control_capacity=options.max_control_calls + 3)
        self._runners = list(runners.values())
        self._ports = {
            identity: _Port(value, runners[assignments[identity]], options)
            for identity, value in unique.items()
        }
        self.repository = _Repository(self._ports[id(unwrap_thread_binding(repository))])
        proxies: dict[int, _Skill] = {}
        self.skills: dict[str, Skill] = {}
        for name, skill in skills.items():
            identity = id(unwrap_thread_binding(skill))
            proxy = proxies.setdefault(identity, _Skill(self._ports[identity]))
            self.skills[name] = proxy
        self._skills = list(proxies.values())
        self.reconciler = (_Reconciler(self._ports[id(unwrap_thread_binding(reconciler))])
                           if reconciler is not None else None)
        self.robot_state = (_RobotState(self._ports[id(unwrap_thread_binding(robot_state))])
                           if robot_state is not None else None)

    async def prepare(self) -> None:
        async def prepare_group(skills: list[_Skill]) -> None:
            for skill in skills:
                await skill.prepare()

        try:
            await asyncio.gather(*(runner.start() for runner in self._runners))
            await self._validation.start()
            declarations: dict[LoopThread, list[_Skill]] = {}
            for skill in self._skills:
                declarations.setdefault(skill._port.runner, []).append(skill)
            await asyncio.gather(*(prepare_group(skills) for skills in declarations.values()))
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        await asyncio.gather(
            self._validation.close(grace_s=self._options.cancel_grace_s),
            *(runner.close(grace_s=self._options.cancel_grace_s)
              for runner in self._runners), return_exceptions=True,
        )

    async def validate(
        self, call: SkillCall, unsatisfied: frozenset[str],
        allowed_permissions: set[str] | None,
    ) -> None:
        copied_call, copied_unsatisfied, copied_permissions = _snapshot(
            (call, unsatisfied, allowed_permissions), self._options,
        )

        def validate() -> None:
            PolicyEnforcer(SkillRegistry(self.skills), copied_permissions).validate(
                copied_call, copied_unsatisfied
            )

        try:
            _unwrap(await self._validation.call(
                lambda: _invoke(validate, self._options),
                timeout_s=self._options.port_timeout_s, control=CONTROL_OPERATION.get(),
            ))
        except TimeoutError as error:
            raise ExtensionTimeout("Admission validation exceeded its deadline") from error


def isolate_ports(
    repository: TaskRepository | ThreadBinding[TaskRepository],
    skills: Mapping[str, Skill | ThreadBinding[Skill]],
    reconciler: Reconciler | ThreadBinding[Reconciler] | None,
    robot_state: RobotStateProvider | ThreadBinding[RobotStateProvider] | None,
    options: RuntimeOptions,
) -> IsolatedPorts:
    return IsolatedPorts(repository, skills, reconciler, robot_state, options)


class IsolatedAdmissionValidator:
    """Schedule kernel policy rules on the existing bounded validation owner.

    The port collection owns its lifecycle. The core sees only an admission
    contract and never has to select an execution thread or manage its workers.
    """

    def __init__(self, ports: IsolatedPorts, allowed_permissions: set[str] | None) -> None:
        self._ports = ports
        self._permissions = set(allowed_permissions) if allowed_permissions is not None else None

    async def validate(self, call: SkillCall, unsatisfied: frozenset[str]) -> None:
        await self._ports.validate(call, unsatisfied, self._permissions)
