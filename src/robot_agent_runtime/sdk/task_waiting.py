import asyncio
import math
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing

from robot_agent_runtime.errors import InvalidRequest, TaskEventStreamEnded, TaskWaitTimeout
from robot_agent_runtime.models import Event, Task, TaskRequest, TaskState


async def submit_and_wait(
    request: TaskRequest,
    *,
    submit: Callable[[TaskRequest], Awaitable[Task]],
    subscribe: Callable[[str], AsyncGenerator[Event, None]],
    get_task: Callable[[str], Awaitable[Task]],
    timeout_s: float | None,
) -> Task:
    """Observe one submitted task without taking ownership of its execution.

    Replay closes the submit/subscribe race. Only the wait has a local deadline;
    timing out or cancelling the caller closes its stream without stopping the
    robot. Existing listeners retain their independent delivery and lifetime.
    """
    if timeout_s is not None and (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise InvalidRequest("timeout_s must be finite and positive, or None")

    submitted = await submit(request)
    if submitted.state.is_terminal:
        return submitted

    budget = asyncio.timeout(timeout_s)
    try:
        async with budget:
            terminal = False
            async with aclosing(subscribe(submitted.id)) as events:
                async for event in events:
                    try:
                        terminal = TaskState(event.data.get("state")).is_terminal
                    except (TypeError, ValueError):
                        terminal = False
                    if terminal:
                        break
            if not terminal:
                raise TaskEventStreamEnded(
                    "Task event stream ended before a terminal event",
                    details={"task_id": submitted.id},
                )
            final = await get_task(submitted.id)
            if not final.state.is_terminal:
                raise TaskEventStreamEnded(
                    "Terminal event was not followed by a terminal task snapshot",
                    details={"task_id": submitted.id},
                )
            return final
    except TimeoutError as error:
        if not budget.expired():
            raise
        raise TaskWaitTimeout(
            "Waiting for the task exceeded its deadline; the task was not cancelled",
            details={"task_id": submitted.id, "timeout_s": timeout_s},
        ) from error
