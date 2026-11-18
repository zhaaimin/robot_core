"""Forward already connected sockets without owning a network server."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import pytest

from robot_agent_runtime.errors import InvalidRequest
from robot_agent_runtime.models import Event, PlatformEvent


def _websocket_module():
    from robot_agent_runtime.transport import websocket

    return websocket


class EventRuntime:
    def __init__(self, events: list[Event | PlatformEvent], *, quiet: bool = False) -> None:
        self.events = events
        self.quiet = quiet
        self.request: dict[str, object] = {}
        self.started = asyncio.Event()
        self.source_closed = asyncio.Event()
        self.error: Exception | None = None

    async def _stream(self):
        self.started.set()
        try:
            for event in self.events:
                yield event
            if self.error is not None:
                raise self.error
            if self.quiet:
                await asyncio.Event().wait()
        finally:
            self.source_closed.set()

    def subscribe_platform_events(self, *, after_cursor=0, event_types=None):
        self.request = {"after_cursor": after_cursor, "event_types": event_types}
        return self._stream()

    def subscribe_all_task_events(self, *, event_types=None):
        self.request = {"event_types": event_types}
        return self._stream()


class Socket:
    def __init__(self, *, block_send: bool = False) -> None:
        self.messages: list[dict[str, object]] = []
        self.closed = asyncio.Event()
        self.watcher_exited = asyncio.Event()
        self.send_entered = asyncio.Event()
        self.send_exited = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0
        if not block_send:
            self.release.set()

    async def send(self, message: str) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.messages.append(json.loads(message))
        self.send_entered.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
            self.send_exited.set()

    async def wait_closed(self) -> None:
        try:
            await self.closed.wait()
        finally:
            self.watcher_exited.set()


def _platform_event(cursor: int) -> PlatformEvent:
    return PlatformEvent(
        event_id=f"platform-{cursor}", source="vendor", event_type="device.fault",
        cursor=cursor, data={"fault": cursor},
    )


async def test_platform_events_are_serial_json_envelopes_with_resume_arguments() -> None:
    module = _websocket_module()
    events = [_platform_event(8), _platform_event(10)]
    runtime = EventRuntime(events)
    socket = Socket(block_send=True)
    forwarding = asyncio.create_task(module.forward_platform_events(
        runtime, socket, after_cursor=7, event_types={"device.fault"},
    ))
    await asyncio.wait_for(socket.send_entered.wait(), 1)
    try:
        assert len(socket.messages) == 1
        socket.release.set()
        await asyncio.wait_for(forwarding, 1)
        assert socket.max_active == 1
        assert runtime.request == {"after_cursor": 7, "event_types": {"device.fault"}}
        assert socket.messages == [
            {"type": "platform_event", "event": value.model_dump(mode="json")}
            for value in events
        ]
        assert runtime.source_closed.is_set() and socket.watcher_exited.is_set()
    finally:
        socket.release.set()
        if not forwarding.done():
            forwarding.cancel()
            await asyncio.gather(forwarding, return_exceptions=True)


async def test_task_events_use_distinct_envelopes_and_forward_type_filter() -> None:
    module = _websocket_module()
    event = Event(event_id="t-1", task_id="t", sequence=1, event_type="task.running")
    runtime = EventRuntime([event])
    socket = Socket()
    await module.forward_task_events(runtime, socket, event_types={"task.running"})
    assert runtime.request == {"event_types": {"task.running"}}
    assert socket.messages == [{"type": "task_event", "event": event.model_dump(mode="json")}]
    assert runtime.source_closed.is_set() and socket.watcher_exited.is_set()


async def test_send_failure_closes_source_and_disconnect_watcher() -> None:
    module = _websocket_module()

    class BrokenSocket(Socket):
        async def send(self, message: str) -> None:
            raise OSError("socket disconnected")

    runtime = EventRuntime([_platform_event(1)], quiet=True)
    socket = BrokenSocket()
    with pytest.raises(OSError, match="socket disconnected"):
        await module.forward_platform_events(runtime, socket)
    assert runtime.source_closed.is_set() and socket.watcher_exited.is_set()


async def test_quiet_disconnect_cancels_pending_source_read_and_closes_source() -> None:
    module = _websocket_module()
    runtime = EventRuntime([], quiet=True)
    socket = Socket()
    forwarding = asyncio.create_task(module.forward_platform_events(runtime, socket))
    await asyncio.wait_for(runtime.started.wait(), 1)
    socket.closed.set()
    async with asyncio.timeout(1):
        await forwarding
    assert runtime.source_closed.is_set() and socket.watcher_exited.is_set()
    assert socket.messages == []


async def test_send_timeout_is_bounded_when_send_ignores_cancellation() -> None:
    module = _websocket_module()

    class ResistantSocket(Socket):
        async def send(self, message: str) -> None:
            self.send_entered.set()
            try:
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
            finally:
                self.send_exited.set()

    runtime = EventRuntime([_platform_event(1)], quiet=True)
    socket = ResistantSocket(block_send=True)
    try:
        async with asyncio.timeout(0.5):
            with pytest.raises(TimeoutError):
                await module.forward_platform_events(runtime, socket, send_timeout_s=0.01)
        assert runtime.source_closed.is_set() and socket.watcher_exited.is_set()
        assert not socket.send_exited.is_set()
    finally:
        socket.release.set()
        await asyncio.wait_for(socket.send_exited.wait(), 1)


async def test_caller_cancellation_cleans_up_send_source_and_disconnect_watcher() -> None:
    module = _websocket_module()
    runtime = EventRuntime([_platform_event(1)], quiet=True)
    socket = Socket(block_send=True)
    forwarding = asyncio.create_task(module.forward_platform_events(runtime, socket))
    await asyncio.wait_for(socket.send_entered.wait(), 1)
    forwarding.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(forwarding, 1)
    assert runtime.source_closed.is_set()
    assert socket.send_exited.is_set() and socket.watcher_exited.is_set()


async def test_source_failure_is_propagated_and_disconnect_watcher_is_reclaimed() -> None:
    module = _websocket_module()
    runtime = EventRuntime([])
    runtime.error = OSError("platform source unavailable")
    socket = Socket()
    with pytest.raises(OSError, match="platform source unavailable"):
        await module.forward_platform_events(runtime, socket)
    assert runtime.source_closed.is_set() and socket.watcher_exited.is_set()


async def test_invalid_send_timeout_is_rejected_without_opening_subscription() -> None:
    module = _websocket_module()
    runtime = EventRuntime([])
    socket = Socket()
    with pytest.raises(InvalidRequest):
        await module.forward_platform_events(runtime, socket, send_timeout_s=0)
    assert not runtime.started.is_set()


class RetainedIterator:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.read_finished = asyncio.Event()
        self.closed = asyncio.Event()
        self.read_calls = 0
        self.close_calls = 0
        self.close_error = close_error

    def __aiter__(self) -> "RetainedIterator":
        return self

    async def __anext__(self) -> PlatformEvent:
        self.read_calls += 1
        self.entered.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
            return _platform_event(1)
        finally:
            self.read_finished.set()

    async def aclose(self) -> None:
        assert self.read_finished.is_set(), "aclose must not race the active read"
        self.close_calls += 1
        self.closed.set()
        if self.close_error is not None:
            raise self.close_error


class RetainedRuntime:
    def __init__(self, iterator: RetainedIterator) -> None:
        self.iterator = iterator

    def subscribe_platform_events(self, *, after_cursor: int = 0,
                                  event_types: set[str] | None = None) -> AsyncIterator[PlatformEvent]:
        return self.iterator

    def subscribe_all_task_events(self, *, event_types: set[str] | None = None) -> AsyncIterator[PlatformEvent]:
        return self.iterator


async def _check_late_read_cleanup(*, cancel_caller: bool) -> None:
    module = _websocket_module()
    for forward in (module.forward_platform_events, module.forward_task_events):
        iterator = RetainedIterator()
        socket = Socket()
        forwarding = asyncio.create_task(forward(RetainedRuntime(iterator), socket, send_timeout_s=0.01))
        try:
            await asyncio.wait_for(iterator.entered.wait(), 1)
            if cancel_caller:
                forwarding.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(forwarding, 0.5)
            else:
                socket.closed.set()
                await asyncio.wait_for(forwarding, 0.5)
            assert iterator.cancelled.is_set() and not iterator.closed.is_set()
            iterator.release.set()
            await asyncio.wait_for(iterator.closed.wait(), 1)
            assert iterator.close_calls == 1 and iterator.read_calls == 1
            assert socket.messages == [] and socket.watcher_exited.is_set()
        finally:
            iterator.release.set()
            if not forwarding.done():
                forwarding.cancel()
                with suppress(asyncio.CancelledError):
                    await forwarding
            await asyncio.wait_for(iterator.read_finished.wait(), 1)
            if not iterator.closed.is_set():
                await iterator.aclose()


async def test_disconnect_closes_retained_iterator_after_abandoned_read_finishes() -> None:
    await _check_late_read_cleanup(cancel_caller=False)


async def test_caller_cancellation_closes_retained_iterator_after_abandoned_read_finishes() -> None:
    await _check_late_read_cleanup(cancel_caller=True)


async def test_late_iterator_cleanup_failure_is_reported_to_the_event_loop() -> None:
    module = _websocket_module()
    failure = OSError("late subscription close failed")
    iterator = RetainedIterator(close_error=failure)
    socket = Socket()
    reported = asyncio.Event()
    contexts: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def report(owner: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        contexts.append(context)
        reported.set()

    loop.set_exception_handler(report)
    forwarding = asyncio.create_task(module.forward_platform_events(
        RetainedRuntime(iterator), socket, send_timeout_s=0.01,
    ))
    try:
        await asyncio.wait_for(iterator.entered.wait(), 1)
        socket.closed.set()
        await asyncio.wait_for(forwarding, 0.5)
        iterator.release.set()
        await asyncio.wait_for(reported.wait(), 1)
        assert iterator.closed.is_set() and iterator.close_calls == 1
        assert len(contexts) == 1 and contexts[0]["exception"] is failure
    finally:
        iterator.release.set()
        if not forwarding.done():
            forwarding.cancel()
            with suppress(asyncio.CancelledError):
                await forwarding
        await asyncio.wait_for(iterator.read_finished.wait(), 1)
        if not iterator.closed.is_set():
            with suppress(OSError):
                await iterator.aclose()
        loop.set_exception_handler(previous)
