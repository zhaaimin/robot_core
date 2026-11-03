import asyncio
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from typing import Any

from robot_agent_runtime.errors import (
    InvalidRequest,
    ListenerCallbackError,
    TaskEventStreamEnded,
)
from robot_agent_runtime.models import Event, Task, TaskState


EventCallback = Callable[[Event], Awaitable[None] | None]
DoneCallback = Callable[[Task], Awaitable[None] | None]
ErrorCallback = Callable[[str, Exception], Awaitable[None] | None]
EventSubscription = Callable[[str], AsyncGenerator[Event, None]]
TaskLookup = Callable[[str], Awaitable[Task]]


class TaskListener:
    """One registration watching future submissions through its owning SDK.

    Events are replayed before live delivery, then the terminal snapshot is
    delivered once. Each task has its own coroutine, so awaiting an async
    callback does not hold up other tasks. Synchronous callbacks must return
    promptly. Callback/transport failures end only that task's subscription;
    they never write business state or cancel robot execution.

    wait() joins the subscriptions already registered when called. close()
    unregisters and cancels subscriptions, without waiting for robot actions.
    SDK shutdown closes listeners before shutting down its own resources;
    callers wanting final callbacks must wait() before closing the SDK.
    Observed IDs are retained until close to deduplicate idempotent submits.
    """

    def __init__(
        self,
        subscribe: EventSubscription,
        get_task: TaskLookup,
        detach: Callable[["TaskListener"], None],
        *,
        on_event: EventCallback | None,
        on_done: DoneCallback | None,
        on_error: ErrorCallback | None,
    ) -> None:
        self._subscribe = subscribe
        self._get_task = get_task
        self._detach = detach
        self._on_event = on_event
        self._on_done = on_done
        self._on_error = on_error
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._seen: set[str] = set()
        self._closed = False

    def _track(self, task_id: str) -> None:
        if self._closed or task_id in self._seen:
            return
        self._seen.add(task_id)
        watcher = asyncio.create_task(self._observe(task_id), name=f"task-listener-{task_id}")
        self._tasks[task_id] = watcher
        watcher.add_done_callback(lambda finished: self._finished(task_id, finished))

    async def wait(self) -> None:
        pending = tuple(self._tasks.values())
        if asyncio.current_task() in pending:
            raise InvalidRequest("A task callback cannot wait for its own listener")
        await asyncio.gather(
            *(asyncio.shield(task) for task in pending), return_exceptions=True
        )

    async def close(self) -> None:
        self._closed = True
        self._detach(self)
        current = asyncio.current_task()
        pending = [task for task in self._tasks.values() if task is not current]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._seen.clear()

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

    @staticmethod
    async def _invoke(callback: Callable[..., Awaitable[None] | None], *args: Any) -> None:
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise ListenerCallbackError("Task callback cancelled itself") from error

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
        if self._tasks.get(task_id) is watcher:
            self._tasks.pop(task_id, None)
        if not watcher.cancelled():
            error = watcher.exception()
            if error is not None:
                watcher.get_loop().call_exception_handler({
                    "message": "Unhandled task listener error", "exception": error,
                    "task_id": task_id, "listener": self,
                })


class TaskListeners:
    """SDK-owned registrations; no subscriptions exist until submit returns.

    This is client-local bookkeeping, not a global server event feed. Closing
    the collection prevents late submissions from leaving orphan listeners.
    """

    def __init__(self, subscribe: EventSubscription, get_task: TaskLookup) -> None:
        self._subscribe = subscribe
        self._get_task = get_task
        self._listeners: set[TaskListener] = set()
        self._closed = False

    def add(
        self,
        *,
        on_event: EventCallback | None = None,
        on_done: DoneCallback | None = None,
        on_error: ErrorCallback | None = None,
    ) -> TaskListener:
        if self._closed:
            raise InvalidRequest("Cannot add a task listener to a closing SDK")
        callbacks = (on_event, on_done, on_error)
        if all(callback is None for callback in callbacks):
            raise InvalidRequest("Provide at least one task callback")
        if any(callback is not None and not callable(callback) for callback in callbacks):
            raise InvalidRequest("Task callbacks must be callable")
        listener = TaskListener(
            self._subscribe, self._get_task, self._listeners.discard,
            on_event=on_event, on_done=on_done, on_error=on_error,
        )
        self._listeners.add(listener)
        return listener

    def track(self, task: Task) -> None:
        for listener in tuple(self._listeners):
            listener._track(task.id)

    async def close(self) -> None:
        self._closed = True
        listeners = tuple(self._listeners)
        await asyncio.gather(*(listener.close() for listener in listeners))
