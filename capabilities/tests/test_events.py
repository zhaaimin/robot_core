import asyncio
import tempfile
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path

import pytest

from robot_agent_capabilities import RobotCapabilities
from robot_agent_runtime.errors import PlatformEventCursorExpired, PlatformEventSourceUnavailable
from robot_agent_runtime.models import PlatformEvent, SkillDefinition
from robot_agent_runtime.sdk import RemoteRuntime, RemoteTransportError, RobotRuntime


class EventRuntime:
    def __init__(self, events: list[PlatformEvent], *, quiet: bool = False, error: Exception | None = None) -> None:
        self.events = events
        self.quiet = quiet
        self.error = error
        self.request: tuple[int, set[str] | None] | None = None
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()

    async def list_skills(self) -> list[SkillDefinition]:
        return []

    def subscribe_platform_events(self, *, after_cursor: int = 0, event_types: set[str] | None = None) -> AsyncIterator[PlatformEvent]:
        self.request = after_cursor, event_types
        return self._stream()

    async def _stream(self) -> AsyncIterator[PlatformEvent]:
        self.entered.set()
        try:
            for event in self.events:
                yield event
            if self.error is not None:
                raise self.error
            if self.quiet:
                await asyncio.Event().wait()
        finally:
            self.closed.set()


class FailingCloseIterator:
    def __init__(self, owner: EventRuntime) -> None:
        self._owner = owner
        self._values = iter(owner.events)

    def __aiter__(self) -> "FailingCloseIterator":
        return self

    async def __anext__(self) -> PlatformEvent:
        self._owner.entered.set()
        try:
            return next(self._values)
        except StopIteration:
            pass
        if self._owner.error is not None:
            raise self._owner.error
        if self._owner.quiet:
            await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self._owner.closed.set()
        raise RemoteTransportError("cleanup connection reset")


class FailingCloseRuntime(EventRuntime):
    def _stream(self) -> AsyncIterator[PlatformEvent]:
        return FailingCloseIterator(self)


def event(cursor: int) -> PlatformEvent:
    return PlatformEvent(event_id=f"event-{cursor}", source="other-vendor", event_type="robot.fault.changed", cursor=cursor, data={"payload": {"code": 7}})


async def test_event_facade_forwards_resume_and_filter_without_rewriting_events() -> None:
    original = event(9)
    runtime = EventRuntime([original])
    robot = await RobotCapabilities.create(runtime, allowed_skills=set())
    selected = {"robot.fault.changed"}
    async with aclosing(robot.subscribe_events(after_cursor=7, event_types=selected)) as stream:
        received = await anext(stream)
        assert received is original and received.cursor == 9 and received.task_id is None
    assert runtime.request == (7, selected)
    assert runtime.request[1] is not selected
    assert runtime.closed.is_set()


async def test_task_only_backend_keeps_invocation_and_reports_events_unsupported() -> None:
    from robot_agent_capabilities.errors import CapabilityEventsUnsupported
    from robot_agent_runtime.adapters.simulator import Simulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.sdk import RobotRuntime

    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)

    class TasksOnly:
        def __getattr__(self, name: str) -> object:
            if name == "subscribe_platform_events":
                raise AttributeError(name)
            return getattr(runtime, name)

    await runtime.start()
    try:
        robot = await RobotCapabilities.create(TasksOnly(), allowed_skills={"audio.speak"})
        observed = await robot.invoke("robot_speak", {"message": "hello"}, operation_id="legacy", wait=True)
        assert observed["state"] == "succeeded"
        with pytest.raises(CapabilityEventsUnsupported) as caught:
            await anext(robot.subscribe_events())
        assert caught.value.info.code == "CAPABILITY_EVENTS_UNSUPPORTED"
        assert await runtime.list_skills()
    finally:
        await runtime.close()


async def test_explicitly_unimplemented_events_are_reported_as_optional() -> None:
    from robot_agent_capabilities.errors import CapabilityEventsUnsupported

    runtime = EventRuntime([], error=NotImplementedError())
    robot = await RobotCapabilities.create(runtime)
    with pytest.raises(CapabilityEventsUnsupported):
        await anext(robot.subscribe_events())
    assert runtime.closed.is_set()


async def test_structured_runtime_errors_preserve_identity_and_recovery_details() -> None:
    for error in (
        PlatformEventCursorExpired("history expired", {"after_cursor": 1, "earliest_cursor": 10}),
        PlatformEventSourceUnavailable("source disconnected", {"source": "other-vendor"}),
    ):
        runtime = EventRuntime([], error=error)
        robot = await RobotCapabilities.create(runtime)
        with pytest.raises(type(error)) as caught:
            await anext(robot.subscribe_events(after_cursor=1))
        assert caught.value is error and runtime.closed.is_set()


async def test_transport_error_reports_observation_cursor_without_retrying() -> None:
    from robot_agent_capabilities.errors import CapabilityEventTransportError

    runtime = EventRuntime([event(9)], error=RemoteTransportError("connection reset"))
    robot = await RobotCapabilities.create(runtime)
    async with aclosing(robot.subscribe_events(after_cursor=7)) as stream:
        assert (await anext(stream)).cursor == 9
        with pytest.raises(CapabilityEventTransportError) as caught:
            await anext(stream)
        assert caught.value.info.code == "CAPABILITY_EVENT_TRANSPORT_FAILED"
        assert caught.value.info.details == {"requested_after_cursor": 7, "last_delivered_cursor": 9}
    assert runtime.closed.is_set()


async def test_cancelling_quiet_subscription_closes_only_the_subscription() -> None:
    runtime = EventRuntime([], quiet=True)
    robot = await RobotCapabilities.create(runtime)
    stream = robot.subscribe_events()
    reading = asyncio.create_task(anext(stream))
    await asyncio.wait_for(runtime.entered.wait(), 1)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    await stream.aclose()
    assert runtime.closed.is_set()
    assert await runtime.list_skills() == []


async def test_cleanup_failure_does_not_mask_structured_runtime_error() -> None:
    original = PlatformEventCursorExpired("expired", {"after_cursor": 1, "earliest_cursor": 10})
    runtime = FailingCloseRuntime([], error=original)
    robot = await RobotCapabilities.create(runtime)
    with pytest.raises(PlatformEventCursorExpired) as caught:
        await anext(robot.subscribe_events(after_cursor=1))
    assert caught.value is original and runtime.closed.is_set()


async def test_cleanup_failure_does_not_replace_read_cancellation() -> None:
    runtime = FailingCloseRuntime([], quiet=True)
    robot = await RobotCapabilities.create(runtime)
    stream = robot.subscribe_events()
    reading = asyncio.create_task(anext(stream))
    await asyncio.wait_for(runtime.entered.wait(), 1)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    await stream.aclose()
    assert runtime.closed.is_set()


async def test_explicit_close_maps_cleanup_transport_error_with_cursor() -> None:
    from robot_agent_capabilities.errors import CapabilityEventTransportError

    runtime = FailingCloseRuntime([event(9)])
    robot = await RobotCapabilities.create(runtime)
    stream = robot.subscribe_events(after_cursor=7)
    assert (await anext(stream)).cursor == 9
    with pytest.raises(CapabilityEventTransportError) as caught:
        await stream.aclose()
    assert caught.value.info.details == {"requested_after_cursor": 7, "last_delivered_cursor": 9}


async def test_event_parameters_fail_before_opening_a_subscription() -> None:
    from robot_agent_runtime.errors import InvalidRequest

    runtime = EventRuntime([])
    robot = await RobotCapabilities.create(runtime)
    for value in (-1, True, 1.5, "1"):
        with pytest.raises(InvalidRequest):
            await anext(robot.subscribe_events(after_cursor=value))
    for value in (["robot.fault.changed"], "robot.fault.changed", {""}, {"   "}, {7}):
        with pytest.raises(InvalidRequest):
            await anext(robot.subscribe_events(event_types=value))
    assert runtime.request is None


def test_event_protocol_is_separate_from_task_runtime() -> None:
    from robot_agent_capabilities.ports import PlatformEventRuntime, TaskRuntime

    assert hasattr(PlatformEventRuntime, "subscribe_platform_events")
    assert not hasattr(TaskRuntime, "subscribe_platform_events")


class IdleSource:
    async def watch(self) -> AsyncIterator[PlatformEvent]:
        yield PlatformEvent(event_id="initial", source="fake-vendor", event_type="robot.fault.snapshot")
        yield PlatformEvent(event_id="changed", source="fake-vendor", event_type="robot.fault.changed", data={"payload": {"code": 7}})
        await asyncio.Event().wait()


async def check_real_runtime_events(runtime: RobotRuntime | RemoteRuntime) -> None:
    robot = await RobotCapabilities.create(runtime)
    async with aclosing(robot.subscribe_events()) as stream:
        async with asyncio.timeout(1):
            initial = await anext(stream)
            changed = await anext(stream)
    assert initial.cursor == 1 and changed.cursor == 2
    assert initial.task_id is None and changed.source == "fake-vendor"
    async with aclosing(robot.subscribe_events(after_cursor=initial.cursor, event_types={"robot.fault.changed"})) as resumed:
        async with asyncio.timeout(1):
            replayed = await anext(resumed)
    assert replayed == changed
    assert await runtime.list_skills() == []


async def test_event_facade_uses_real_embedded_replay_and_live_contract() -> None:
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.sdk import RobotRuntime

    runtime = RobotRuntime(InMemoryTaskRepository(), {}, event_source=IdleSource())
    await runtime.start()
    try:
        await check_real_runtime_events(runtime)
    finally:
        await runtime.close()


async def test_event_facade_uses_real_uds_resume_contract() -> None:
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.sdk import RemoteRuntime
    from robot_agent_runtime.transport.server import RuntimeServer

    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "runtime.sock")
        server = RuntimeServer(InMemoryTaskRepository(), {}, path, event_source=IdleSource())
        runtime = RemoteRuntime(path)
        await server.start()
        await runtime.start()
        try:
            await check_real_runtime_events(runtime)
        finally:
            await runtime.close()
            await server.close()
