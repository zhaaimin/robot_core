"""Sequential forwarding to an already connected, application-owned socket."""

import asyncio
import json
import math
from collections.abc import AsyncIterator
from typing import Protocol

from robot_agent_runtime.errors import InvalidRequest
from robot_agent_runtime.models import Event, PlatformEvent


class WebSocketConnection(Protocol):
    """The connected transport boundary; authentication belongs to the caller.

    ``wait_closed`` must finish when the connection disconnects, including while
    no events are arriving. ``send`` completion is transport progress, not an
    application acknowledgement. The caller owns socket closure and must not
    run another sender concurrently on the same connection.
    """

    async def send(self, message: str) -> None: ...

    async def wait_closed(self) -> None: ...


class PlatformEventRuntime(Protocol):
    def subscribe_platform_events(
        self, *, after_cursor: int = 0, event_types: set[str] | None = None,
    ) -> AsyncIterator[PlatformEvent]: ...


class TaskEventRuntime(Protocol):
    def subscribe_all_task_events(
        self, *, event_types: set[str] | None = None,
    ) -> AsyncIterator[Event]: ...


def _check_timeout(timeout_s: float) -> None:
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise InvalidRequest("send_timeout_s must be positive and finite")


async def forward_platform_events(
    runtime: PlatformEventRuntime, websocket: WebSocketConnection, *,
    after_cursor: int = 0, event_types: set[str] | None = None,
    send_timeout_s: float = 5.0,
) -> None:
    """Forward saved platform facts as ``{type: platform_event, event: ...}``.

    The consumer stores the cursor after application processing and supplies it
    on reconnection; a successful send must not advance that durable cursor.
    Disconnect returns normally, whereas source/send errors and send deadlines
    propagate. Only one send is in flight. Cancellation requests do not prove
    native side effects stopped: an uncooperative send may finish after return,
    with its result consumed and no replacement sender started by this call.
    Cleanup also waits only a bounded grace. A read that finishes later retains
    one continuation to close its iterator after actual completion; late cleanup
    failures are reported to the owning loop rather than silently abandoned.
    """
    _check_timeout(send_timeout_s)
    events = runtime.subscribe_platform_events(
        after_cursor=after_cursor, event_types=event_types,
    )
    await _forward(events, websocket, "platform_event", send_timeout_s)


async def forward_task_events(
    runtime: TaskEventRuntime, websocket: WebSocketConnection, *,
    event_types: set[str] | None = None, send_timeout_s: float = 5.0,
) -> None:
    """Forward the live all-task stream; historical replay remains per task.

    Envelopes use ``type: task_event`` and preserve each task's own sequence.
    Connection ownership, timeout and delivery semantics match platform events.
    """
    _check_timeout(send_timeout_s)
    events = runtime.subscribe_all_task_events(event_types=event_types)
    await _forward(events, websocket, "task_event", send_timeout_s)


def _consume_result(task: asyncio.Future[object]) -> None:
    if not task.cancelled():
        task.exception()


def _request_cancel(task: asyncio.Future[object] | None) -> None:
    if task is None:
        return
    if not task.done():
        task.cancel()
    task.add_done_callback(_consume_result)


async def _close_event_iterator(
    events: AsyncIterator[Event] | AsyncIterator[PlatformEvent],
    reading: asyncio.Future[Event | PlatformEvent] | None,
) -> None:
    if reading is not None and not reading.done():
        await asyncio.wait((reading,))
    if (close := getattr(events, "aclose", None)) is not None:
        await close()


def _report_late_cleanup(task: asyncio.Future[None]) -> None:
    if not task.cancelled() and (error := task.exception()) is not None:
        task.get_loop().call_exception_handler({
            "message": "Late WebSocket event iterator cleanup failed", "exception": error,
        })


async def _forward(
    events: AsyncIterator[Event] | AsyncIterator[PlatformEvent],
    websocket: WebSocketConnection, envelope_type: str, timeout_s: float,
) -> None:
    disconnected = asyncio.ensure_future(websocket.wait_closed())
    reading: asyncio.Future[Event | PlatformEvent] | None = None
    sending: asyncio.Future[None] | None = None
    try:
        while True:
            reading = asyncio.ensure_future(anext(events))
            done, _ = await asyncio.wait(
                (reading, disconnected), return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected in done:
                disconnected.result()
                return
            try:
                event = reading.result()
            except StopAsyncIteration:
                return
            reading = None
            message = json.dumps(
                {"type": envelope_type, "event": event.model_dump(mode="json")},
                separators=(",", ":"),
            )
            sending = asyncio.ensure_future(websocket.send(message))
            done, _ = await asyncio.wait(
                (sending, disconnected), timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected in done:
                disconnected.result()
                return
            if sending not in done:
                raise TimeoutError("WebSocket event send timed out")
            sending.result()
            sending = None
    finally:
        # Do not wait for a cancelled send to acknowledge cancellation: socket
        # wrappers can resist it. There is at most one such send per invocation.
        _request_cancel(sending)
        _request_cancel(disconnected)
        _request_cancel(reading)
        closing = asyncio.create_task(_close_event_iterator(events, reading))
        observed = False
        try:
            done, _ = await asyncio.wait((closing,), timeout=timeout_s)
            if closing in done:
                observed = True
                closing.result()
        finally:
            closing.add_done_callback(_consume_result if observed else _report_late_cleanup)
        # Give cooperative socket operations a turn to release their resources.
        cleanup = tuple(task for task in (sending, disconnected) if task is not None)
        if cleanup:
            await asyncio.wait(cleanup, timeout=0)
