import asyncio
import inspect
import threading
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, Literal

import robot_agent_runtime.errors as runtime_errors
from robot_agent_runtime.errors import (
    AgentRuntimeError,
    ExtensionCapacityExceeded,
    InvalidRequest,
    ListenerCallbackError,
    ListenerCallbackQueueFull,
    ListenerCallbackTimeout,
    ListenerCapacityExceeded,
)
from robot_agent_runtime.hosting.boundary import bounded_snapshot
from robot_agent_runtime.hosting.thread_runtime import LoopJob, LoopThread
from robot_agent_runtime.options import RuntimeOptions


ListenerExecution = Literal["caller", "group", "dedicated"]
CALLBACK_ORIGIN: ContextVar[tuple[object, asyncio.Task[Any]] | None] = ContextVar(
    "listener_callback_origin", default=None,
)


async def on_callback_owner(factory: Callable[[], Awaitable[None]]) -> bool:
    origin = CALLBACK_ORIGIN.get()
    if origin is None or origin[1].get_loop() is asyncio.get_running_loop():
        return False
    future = asyncio.run_coroutine_threadsafe(factory(), origin[1].get_loop())
    await asyncio.wrap_future(future)
    return True


def _clean_error(error: BaseException, options: RuntimeOptions) -> Exception:
    from robot_agent_runtime.sdk.remote import RemoteRuntimeError, RemoteTransportError

    try:
        if isinstance(error, AgentRuntimeError):
            info = bounded_snapshot(error.info, max_bytes=options.max_snapshot_bytes,
                                    max_depth=options.max_snapshot_depth)
            if type(error) is RemoteRuntimeError:
                return RemoteRuntimeError(info.message, info.details, code=info.code)
            kind = vars(runtime_errors).get(type(error).__name__, AgentRuntimeError)
            if not isinstance(kind, type) or not issubclass(kind, AgentRuntimeError):
                kind = AgentRuntimeError
            cleaned = kind(info.message, info.details)
            cleaned.info = info
            cleaned.code = info.code
            return cleaned
        if type(error) in (ValueError, TypeError, TimeoutError, RuntimeError,
                           KeyError, LookupError, AttributeError, OSError, RemoteTransportError):
            message = bounded_snapshot(str(error), max_bytes=options.max_snapshot_bytes,
                                       max_depth=options.max_snapshot_depth)
            return type(error)(message)
    except BaseException:
        pass
    return ListenerCallbackError("Task callback failed", {"cause": type(error).__name__})


class CallbackLoop:
    """One callback owner, with bounded admission and revocable queued work.

    A running callback retains its thread until real completion. Queue expiry is
    driven by the submitting loop and rechecked before execution, so neither
    a blocked worker nor delayed timer delivery can start expired user code.
    A registration claim under its own lock separates revocable work from an
    in-flight callback; no user code runs while that lock is held.
    Only detached arguments and clean exceptions cross back; callback results
    are awaited and discarded on this loop. Closing never replaces a worker.
    """

    def __init__(self, name: str, options: RuntimeOptions) -> None:
        self._runner = LoopThread(name, max_pending=options.max_pending_listener_callbacks)
        self._options = options
        self._retired = False
        self.references = 0
        self._jobs: dict[LoopJob[Exception | None], asyncio.Task[Any]] = {}
        self._lock = threading.Lock()

    def stopped(self) -> bool:
        return self._runner.stopped()

    async def invoke(
        self, listener: object, callback: Callable[..., Any], args: tuple[Any, ...],
        *, can_start: Callable[[], bool] | None = None,
    ) -> None:
        origin = asyncio.current_task()
        if origin is None:
            raise InvalidRequest("A listener callback requires an observing task")
        await self._runner.start()
        if can_start is not None and not can_start():
            return
        deadline = time.monotonic() + self._options.listener_queue_timeout_s

        async def execute() -> Exception | None:
            if can_start is not None and not can_start():
                return None
            if time.monotonic() >= deadline:
                return ListenerCallbackTimeout("Listener callback expired in queue")
            token = CALLBACK_ORIGIN.set((listener, origin))
            try:
                values = tuple(_clean_error(value, self._options)
                               if isinstance(value, BaseException) else value for value in args)
                result = callback(*values)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                return ListenerCallbackError("Task callback cancelled itself")
            except BaseException as error:
                return _clean_error(error, self._options)
            finally:
                CALLBACK_ORIGIN.reset(token)
            return None

        with self._lock:
            if self._retired:
                raise InvalidRequest("Listener callback loop is closing")
            try:
                job = self._runner.submit(execute)
            except ExtensionCapacityExceeded:
                raise ListenerCallbackQueueFull("Listener callback loop is full") from None
            self._jobs[job] = origin
        expired = False

        def expire() -> None:
            nonlocal expired
            expired = job.cancel_if_queued()

        timer = asyncio.get_running_loop().call_later(
            self._options.listener_queue_timeout_s, expire,
        )
        try:
            try:
                error = await job.wait()
            except asyncio.CancelledError:
                if expired and not origin.cancelling():
                    raise ListenerCallbackTimeout("Listener callback expired in queue") from None
                job.cancel()
                raise
            if error is not None:
                raise error
        finally:
            timer.cancel()
            with self._lock:
                self._jobs.pop(job, None)

    async def close(self) -> None:
        origin = CALLBACK_ORIGIN.get()
        with self._lock:
            self._retired = True
            preserved = next((job for job, observer in self._jobs.items()
                              if origin is not None and observer is origin[1]), None)
        await self._runner.close(self._options.cancel_grace_s, preserve=preserved)


class ListenerLoops:
    """SDK-local groups and dedicated registrations have a separate thread budget.

    Retired loops count until their worker really exits. A blocked member can
    delay its group but cannot cause unbounded replacement threads on re-add.
    """

    def __init__(self, options: RuntimeOptions) -> None:
        self._options = options
        self._groups: dict[str, CallbackLoop] = {}
        self._loops: set[CallbackLoop] = set()
        self._lock = threading.Lock()
        self._closed = False

    def acquire(self, execution: ListenerExecution, group: str | None) -> CallbackLoop | None:
        if type(execution) is not str or execution not in ("caller", "group", "dedicated"):
            raise InvalidRequest("Listener execution must be caller, group or dedicated")
        if execution == "group":
            if type(group) is not str or not group.strip():
                raise InvalidRequest("Group listener execution requires a nonempty group name")
        elif group is not None:
            raise InvalidRequest("Only group listener execution accepts a group name")
        with self._lock:
            if self._closed:
                raise InvalidRequest("Listener callback loops are closing")
            if execution == "caller":
                return None
            owner = self._groups.get(group) if execution == "group" else None
            if owner is None:
                self._loops = {item for item in self._loops if not item.stopped()}
                if len(self._loops) >= self._options.max_listener_threads:
                    raise ListenerCapacityExceeded("SDK listener thread capacity is exhausted")
                name = f"rar-listener-group-{group}" if execution == "group" else "rar-listener-dedicated"
                owner = CallbackLoop(name, self._options)
                self._loops.add(owner)
                if execution == "group":
                    self._groups[group] = owner
            owner.references += 1
            return owner

    async def release(self, owner: CallbackLoop) -> None:
        with self._lock:
            owner.references -= 1
            if owner.references:
                return
            self._groups = {key: value for key, value in self._groups.items() if value is not owner}
        await owner.close()

    async def close(self) -> None:
        with self._lock:
            self._closed = True
            owners = tuple(self._loops)
            self._groups.clear()
        await asyncio.gather(*(owner.close() for owner in owners))
