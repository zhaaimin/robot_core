import asyncio
import inspect
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from concurrent.futures import Future
from contextlib import aclosing
from typing import Any

from robot_agent_runtime.errors import (
    InvalidRequest,
    ListenerCallbackError,
    TaskEventStreamEnded,
)
from robot_agent_runtime.models import Event, Task, TaskState
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.sdk.callback_executor import CallbackExecutor
from robot_agent_runtime.sdk.listener_execution import (
    CALLBACK_ORIGIN,
    CallbackLoop,
    ListenerExecution,
    ListenerLoops,
    on_callback_owner,
)


EventCallback = Callable[[Event], Awaitable[None] | None]
DoneCallback = Callable[[Task], Awaitable[None] | None]
ErrorCallback = Callable[[str, Exception], Awaitable[None] | None]
EventSubscription = Callable[[str], AsyncGenerator[Event, None]]
TaskLookup = Callable[[str], Awaitable[Task]]


def _consume(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


class TaskListener:
    """One registration watching future submissions through its owning SDK.

    Events are replayed before live delivery, then the terminal snapshot is
    delivered once. Each task has its own coroutine, so callbacks that yield
    allow other tasks to progress. Caller execution keeps async callbacks on the
    submitting loop and sync callbacks on daemon workers. Group/dedicated
    execution owns a stable callback loop for both kinds. Callback/transport failures end
    only that task's subscription; they never write business state or cancel
    robot execution.

    wait() joins the subscriptions already registered when called. close()
    unregisters, stops further deliveries, and cancels watchers without waiting
    for robot actions or an in-flight synchronous callback to finish — a sync callback still
    running at close() is abandoned (its result is dropped). Callers who must see
    final callbacks must wait() before closing the SDK.
    Observed IDs are retained until close to deduplicate idempotent submits.
    """

    def __init__(
        self,
        subscribe: EventSubscription,
        get_task: TaskLookup,
        detach: Callable[["TaskListener"], Awaitable[None]],
        executor: CallbackExecutor,
        *,
        on_event: EventCallback | None,
        on_done: DoneCallback | None,
        on_error: ErrorCallback | None,
        cancel_grace_s: float = 0.2,
        callback_loop: CallbackLoop | None = None,
    ) -> None:
        self._subscribe = subscribe
        self._get_task = get_task
        self._detach = detach
        self._executor = executor
        self._callback_loop = callback_loop
        self._on_event = on_event
        self._on_done = on_done
        self._on_error = on_error
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._completions: dict[str, Future[None]] = {}
        self._lock = threading.Lock()
        self._cancel_grace_s = cancel_grace_s
        self._pending_callbacks: set[asyncio.Future[Any]] = set()
        self._seen: set[str] = set()
        self._closed = False

    def _track(self, task_id: str) -> None:
        with self._lock:
            if self._closed or task_id in self._seen:
                return
            self._seen.add(task_id)
            watcher = asyncio.create_task(self._observe(task_id), name=f"task-listener-{task_id}")
            self._tasks[task_id] = watcher
            completion: Future[None] = Future()
            self._completions[task_id] = completion
        watcher.add_done_callback(lambda finished: self._finished(task_id, finished))

    async def wait(self) -> None:
        with self._lock:
            origin = CALLBACK_ORIGIN.get()
            if (origin is not None and origin[0] is self
                    or asyncio.current_task() in self._tasks.values()):
                raise InvalidRequest("A task callback cannot wait for its own listener")
            pending = tuple(self._completions.values())
        await asyncio.gather(
            *(asyncio.shield(asyncio.wrap_future(done)) for done in pending),
            return_exceptions=True,
        )

    async def close(self) -> None:
        if await on_callback_owner(self.close):
            return
        await self._close(asyncio.current_task())

    async def _close(self, origin: asyncio.Task[Any] | None) -> None:
        callback_origin = CALLBACK_ORIGIN.get()
        if callback_origin is not None and callback_origin[0] is self:
            origin = callback_origin[1]
        with self._lock:
            self._closed = True
            pending = [
                (task, self._completions[task_id])
                for task_id, task in self._tasks.items() if task is not origin
            ]
            callbacks = tuple(self._pending_callbacks)
        for future in callbacks:
            self._executor.cancel(future)
        waiters: list[asyncio.Future[Any]] = []
        for task, completion in pending:
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
            wrapped = asyncio.wrap_future(completion)
            wrapped.add_done_callback(_consume)
            waiters.append(asyncio.shield(wrapped))
        try:
            if waiters:
                await asyncio.wait(waiters, timeout=self._cancel_grace_s)
        finally:
            for waiter in waiters:
                waiter.cancel()
            with self._lock:
                self._seen.clear()
            await self._detach(self)

    async def _observe(self, task_id: str) -> None:
        try:
            terminal = False
            async with aclosing(self._subscribe(task_id)) as stream:
                async for event in stream:
                    if self._closed:
                        return
                    try:
                        terminal = TaskState(event.data.get("state")).is_terminal
                    except (ValueError, TypeError):
                        terminal = False
                    if self._on_event is not None:
                        await self._invoke(self._on_event, event.model_copy(deep=True))
                    if self._closed:
                        return
                    if terminal:
                        break
            if self._closed:
                return
            if not terminal:
                raise TaskEventStreamEnded(
                    "Task event stream ended before a terminal event",
                    details={"task_id": task_id},
                )
            final = await self._get_task(task_id)
            if not final.state.is_terminal:
                raise TaskEventStreamEnded(
                    "Terminal event was not followed by a terminal task snapshot",
                    details={"task_id": task_id},
                )
            if not self._closed and self._on_done is not None:
                await self._invoke(self._on_done, final.model_copy(deep=True))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._closed:
                await self._report_error(task_id, error)

    async def _invoke(self, callback: Callable[..., Awaitable[None] | None], *args: Any) -> None:
        if self._closed:
            return
        try:
            if self._callback_loop is not None:
                await self._callback_loop.invoke(self, callback, args, can_start=self._claim_callback)
                return
            if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
                getattr(callback, "__call__", None)
            ):
                result = callback(*args)
            else:
                future = self._executor.submit(callback, *args)
                with self._lock:
                    self._pending_callbacks.add(future)
                try:
                    result = await future
                except asyncio.CancelledError:
                    self._executor.cancel(future)
                    if future.done() and not future.cancelled() and future.exception() is None:
                        unclaimed = future.result()
                        self._executor.discard_result(unclaimed)
                    raise
                finally:
                    with self._lock:
                        self._pending_callbacks.discard(future)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise ListenerCallbackError("Task callback cancelled itself") from error

    def _claim_callback(self) -> bool:
        with self._lock:
            return not self._closed

    async def _report_error(self, task_id: str, error: Exception) -> None:
        if self._on_error is not None:
            try:
                await self._invoke(self._on_error, task_id, error)
                return
            except Exception as callback_error:
                error = callback_error
        asyncio.get_running_loop().call_exception_handler({
            "message": "Task listener error", "exception": error,
            "task_id": task_id, "listener": self,
        })

    def _finished(self, task_id: str, watcher: asyncio.Task[None]) -> None:
        with self._lock:
            if self._tasks.get(task_id) is watcher:
                self._tasks.pop(task_id, None)
                completion = self._completions.pop(task_id)
                completion.set_result(None)
        if not watcher.cancelled():
            error = watcher.exception()
            if error is not None and not self._closed:
                watcher.get_loop().call_exception_handler({
                    "message": "Unhandled task listener error", "exception": error,
                    "task_id": task_id, "listener": self,
                })


class TaskListeners:
    """SDK-owned registrations; no subscriptions exist until submit returns.

    This is client-local bookkeeping, not a global server event feed. Closing
    the collection prevents late submissions from leaving orphan listeners.
    """

    def __init__(
        self, subscribe: EventSubscription, get_task: TaskLookup, *, cancel_grace_s: float = 0.2,
        options: RuntimeOptions | None = None,
    ) -> None:
        self._subscribe = subscribe
        self._get_task = get_task
        self._executor = CallbackExecutor()
        self._loops = ListenerLoops(options or RuntimeOptions(cancel_grace_s=cancel_grace_s))
        self._listeners: set[TaskListener] = set()
        self._lock = threading.Lock()
        self._cancel_grace_s = cancel_grace_s
        self._closed = False

    def add(
        self,
        *,
        on_event: EventCallback | None = None,
        on_done: DoneCallback | None = None,
        on_error: ErrorCallback | None = None,
        execution: ListenerExecution = "caller",
        group: str | None = None,
    ) -> TaskListener:
        if self._closed:
            raise InvalidRequest("Cannot add a task listener to a closing SDK")
        callbacks = (on_event, on_done, on_error)
        if all(callback is None for callback in callbacks):
            raise InvalidRequest("Provide at least one task callback")
        if any(callback is not None and not callable(callback) for callback in callbacks):
            raise InvalidRequest("Task callbacks must be callable")
        with self._lock:
            if self._closed:
                raise InvalidRequest("Cannot add a task listener to a closing SDK")
            owner = self._loops.acquire(execution, group)
            listener = TaskListener(
                self._subscribe, self._get_task, self._detach, self._executor,
                on_event=on_event, on_done=on_done, on_error=on_error,
                cancel_grace_s=self._cancel_grace_s, callback_loop=owner,
            )
            self._listeners.add(listener)
        return listener

    async def _detach(self, listener: TaskListener) -> None:
        with self._lock:
            if listener not in self._listeners:
                return
            self._listeners.remove(listener)
        if listener._callback_loop is not None:
            await self._loops.release(listener._callback_loop)

    def track(self, task: Task) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener._track(task.id)

    async def close(self) -> None:
        with self._lock:
            self._closed = True
            listeners = tuple(self._listeners)
        current = asyncio.current_task()
        await asyncio.gather(*(listener._close(current) for listener in listeners))
        await self._executor.close()
        await self._loops.close()
