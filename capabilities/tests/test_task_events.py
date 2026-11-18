import asyncio
import tempfile
from collections import deque
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest

from robot_agent_capabilities import RobotCapabilities
from robot_agent_runtime.errors import InvalidRequest, TaskNotFound
from robot_agent_runtime.models import Event, PlatformEvent, SkillDefinition
from robot_agent_runtime.sdk import RemoteRuntime, RemoteTransportError, RobotRuntime


ObservedEvent = Event | PlatformEvent


def task_event(task_id: str, sequence: int, state: str = "succeeded") -> Event:
    return Event(event_id=f"{task_id}-{sequence}", task_id=task_id, sequence=sequence,
                 event_type=f"TASK_{state.upper()}", data={"state": state})


def platform_event(cursor: int = 9) -> PlatformEvent:
    return PlatformEvent(event_id=f"platform-{cursor}", source="vendor", event_type="robot.fault.changed", cursor=cursor)


class Script:
    def __init__(self, events: list[ObservedEvent] | None = None, *, quiet: bool = False,
                 error: Exception | None = None, close_error: Exception | None = None) -> None:
        self.events = deque(events or ())
        self.queue: asyncio.Queue[ObservedEvent] | None = asyncio.Queue() if quiet else None
        self.error = error
        self.close_error = close_error
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()
        self.active = 0
        self.max_active = 0

    def __aiter__(self) -> "Script":
        return self

    async def __anext__(self) -> ObservedEvent:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if self.events:
                return self.events.popleft()
            if self.error is not None:
                raise self.error
            if self.queue is not None:
                return await self.queue.get()
            raise StopAsyncIteration
        finally:
            self.active -= 1

    async def aclose(self) -> None:
        self.closed.set()
        if self.close_error is not None:
            raise self.close_error


class TasksOnly:
    def __init__(self, histories: dict[str, list[Event]]) -> None:
        self.histories = histories
        self.task_requests: list[tuple[str, int]] = []
        self.opened: list[Script] = []

    async def list_skills(self) -> list[SkillDefinition]:
        return []

    def subscribe_task_events(self, task_id: str, after_sequence: int = 0) -> AsyncIterator[Event]:
        self.task_requests.append((task_id, after_sequence))
        stream = Script([event for event in self.histories[task_id] if event.sequence > after_sequence])
        self.opened.append(stream)
        return stream


class Combined(TasksOnly):
    def __init__(self, histories: dict[str, list[Event]] | None = None,
                 platform: Script | None = None, tasks: Script | None = None) -> None:
        super().__init__(histories or {})
        self.platform = platform or Script()
        self.tasks = tasks or Script()
        self.platform_request: tuple[int, set[str] | None] | None = None
        self.global_request: set[str] | None = None

    def subscribe_platform_events(self, *, after_cursor: int = 0,
                                  event_types: set[str] | None = None) -> AsyncIterator[PlatformEvent]:
        self.platform_request = after_cursor, event_types
        self.opened.append(self.platform)
        return self.platform

    def subscribe_all_task_events(self, event_types: set[str] | None = None) -> AsyncIterator[Event]:
        self.global_request = event_types
        self.opened.append(self.tasks)
        return self.tasks


async def test_known_tasks_replay_without_platform_or_global_backend() -> None:
    first, second = task_event("a", 2), task_event("b", 1, "canceled")
    runtime = TasksOnly({"a": [task_event("a", 1, "running"), first], "b": [second]})
    robot = await RobotCapabilities.create(runtime)
    values = [event async for event in robot.subscribe_events(
        sources={"task"}, task_ids={"a", "b"}, after_sequences={"a": 1},
    )]
    assert {event.event_id for event in values} == {first.event_id, second.event_id}
    assert any(event is first for event in values) and any(event is second for event in values)
    assert sorted(runtime.task_requests) == [("a", 0), ("b", 0)]
    assert all(stream.closed.is_set() for stream in runtime.opened)


async def test_task_and_platform_sources_keep_original_models_and_positions() -> None:
    completed, fault = task_event("a", 6), platform_event()
    runtime = Combined({"a": [task_event("a", 5, "running"), completed]}, Script([fault]))
    robot = await RobotCapabilities.create(runtime)
    selected = {"TASK_SUCCEEDED", "robot.fault.changed"}
    values = [event async for event in robot.subscribe_events(
        sources={"task", "platform"}, task_ids={"a"}, after_sequences={"a": 4},
        after_cursor=7, event_types=selected,
    )]
    assert {event.event_id for event in values} == {completed.event_id, fault.event_id}
    assert any(event is completed for event in values) and any(event is fault for event in values)
    assert runtime.platform_request == (7, selected)
    assert runtime.platform_request[1] is not selected


async def test_quiet_platform_does_not_block_completion_or_end_after_task_finishes() -> None:
    runtime = Combined({"a": [task_event("a", 1)]}, Script(quiet=True))
    robot = await RobotCapabilities.create(runtime)
    async with aclosing(robot.subscribe_events(sources={"task", "platform"}, task_ids={"a"})) as stream:
        assert (await asyncio.wait_for(anext(stream), 1)).task_id == "a"
        fault = platform_event()
        await runtime.platform.queue.put(fault)
        assert await asyncio.wait_for(anext(stream), 1) is fault
    assert all(source.closed.is_set() for source in runtime.opened)


async def test_global_task_mode_delegates_to_optional_live_stream() -> None:
    first, second = task_event("a", 1), task_event("b", 1)
    runtime = Combined(tasks=Script([first, second]))
    robot = await RobotCapabilities.create(runtime)
    values = [event async for event in robot.subscribe_events(sources={"task"}, event_types={"TASK_SUCCEEDED"})]
    assert values == [first, second] and runtime.global_request == {"TASK_SUCCEEDED"}
    assert not runtime.task_requests and runtime.platform_request is None


async def test_terminal_filter_includes_failure_cancel_unknown_and_reconciliation() -> None:
    states = ("succeeded", "failed", "canceled", "unknown", "reconciliation_required")
    histories = {state: [task_event(state, 1, "running"), task_event(state, 2, state)] for state in states}
    fault = platform_event()
    runtime = Combined(histories, Script([fault]))
    robot = await RobotCapabilities.create(runtime)
    values = [event async for event in robot.subscribe_events(
        sources={"task", "platform"}, task_ids=set(states), task_terminal_only=True,
    )]
    assert len(values) == 6 and any(event is fault for event in values)
    assert {event.data["state"] for event in values if isinstance(event, Event)} == set(states)


async def test_missing_requested_source_fails_before_any_subscription_opens() -> None:
    from robot_agent_capabilities.errors import CapabilityEventsUnsupported

    runtime = TasksOnly({"a": [task_event("a", 1)]})
    robot = await RobotCapabilities.create(runtime)
    for arguments in ({"sources": {"task"}}, {"sources": {"task", "platform"}, "task_ids": {"a"}}):
        with pytest.raises(CapabilityEventsUnsupported):
            await anext(robot.subscribe_events(**arguments))
    assert not runtime.opened


async def test_merge_rejects_ambiguous_positions_and_invalid_selection() -> None:
    runtime = Combined()
    robot = await RobotCapabilities.create(runtime)
    invalid = (
        {"sources": set()}, {"sources": {"unknown"}}, {"sources": ["task"]},
        {"task_ids": {"a"}}, {"sources": {"task"}, "task_ids": set()},
        {"sources": {"task"}, "task_ids": {" "}},
        {"sources": {"task"}, "after_cursor": 1},
        {"sources": {"task"}, "after_sequences": {"a": 0}},
        {"sources": {"task"}, "task_ids": {"a"}, "after_sequences": {"b": 0}},
        {"sources": {"task"}, "task_ids": {"a"}, "after_sequences": {"a": True}},
        {"sources": {"task"}, "task_ids": {"a"}, "after_sequences": {"a": -1}},
        {"task_terminal_only": True}, {"sources": {"task"}, "task_terminal_only": 1},
    )
    for arguments in invalid:
        with pytest.raises(InvalidRequest):
            await anext(robot.subscribe_events(**arguments))
    assert not runtime.opened


async def test_source_failure_closes_peer_and_preserves_original_error() -> None:
    original = TaskNotFound("missing", {"task_id": "a"})
    runtime = Combined(platform=Script(quiet=True), tasks=Script(error=original))
    robot = await RobotCapabilities.create(runtime)
    with pytest.raises(TaskNotFound) as caught:
        await anext(robot.subscribe_events(sources={"task", "platform"}))
    assert caught.value is original
    assert all(source.closed.is_set() for source in runtime.opened)


async def test_task_transport_error_reports_per_task_positions_not_platform_cursor() -> None:
    from robot_agent_capabilities.errors import CapabilityEventTransportError

    runtime = Combined(tasks=Script([task_event("a", 6)], error=RemoteTransportError("reset")))
    robot = await RobotCapabilities.create(runtime)
    async with aclosing(robot.subscribe_events(sources={"task"})) as stream:
        assert (await anext(stream)).sequence == 6
        with pytest.raises(CapabilityEventTransportError) as caught:
            await anext(stream)
    details = caught.value.info.details
    assert details["source"] == "task"
    assert details["last_delivered_task"] == {"task_id": "a", "sequence": 6}
    assert "last_delivered_cursor" not in details


async def test_cancelling_merge_releases_both_readers_without_runtime_shutdown() -> None:
    runtime = Combined(platform=Script(quiet=True), tasks=Script(quiet=True))
    robot = await RobotCapabilities.create(runtime)
    stream = robot.subscribe_events(sources={"task", "platform"})
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(asyncio.gather(runtime.platform.entered.wait(), runtime.tasks.entered.wait()), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await stream.aclose()
    assert all(source.closed.is_set() and source.max_active == 1 for source in runtime.opened)


async def test_task_event_protocol_does_not_expand_required_task_runtime() -> None:
    from robot_agent_capabilities.ports import TaskEventRuntime, TaskRuntime

    assert hasattr(TaskEventRuntime, "subscribe_all_task_events")
    assert not hasattr(TaskRuntime, "subscribe_all_task_events")


async def test_merge_does_not_prefetch_unbounded_events_while_consumer_is_paused() -> None:
    runtime = Combined(platform=Script([platform_event(number) for number in range(1, 101)]),
                       tasks=Script([task_event("a", number) for number in range(1, 101)]))
    robot = await RobotCapabilities.create(runtime)
    stream = robot.subscribe_events(sources={"task", "platform"})
    try:
        await asyncio.wait_for(anext(stream), 1)
        assert len(runtime.platform.events) >= 99 and len(runtime.tasks.events) >= 99
    finally:
        await stream.aclose()
    assert all(source.closed.is_set() for source in runtime.opened)


async def test_setup_failure_closes_iterator_even_when_its_reader_never_started() -> None:
    from robot_agent_capabilities.errors import CapabilityEventsUnsupported

    class BrokenTasks(Combined):
        def subscribe_all_task_events(self, event_types: set[str] | None = None) -> AsyncIterator[Event]:
            raise NotImplementedError("global tasks unavailable")

    runtime = BrokenTasks(platform=Script(quiet=True))
    robot = await RobotCapabilities.create(runtime)
    with pytest.raises(CapabilityEventsUnsupported):
        await anext(robot.subscribe_events(sources={"task", "platform"}))
    assert runtime.platform.closed.is_set()


async def test_close_is_bounded_and_late_pull_cleans_up_on_actual_completion() -> None:
    from robot_agent_capabilities.errors import CapabilityEventCleanupTimeout

    release = asyncio.Event()

    class Resistant(Script):
        async def __anext__(self) -> ObservedEvent:
            self.entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return platform_event()

    resistant = Resistant()
    runtime = Combined({"a": [task_event("a", 1)]}, resistant)
    robot = await RobotCapabilities.create(runtime)
    stream = robot.subscribe_events(sources={"task", "platform"}, task_ids={"a"})
    try:
        assert (await asyncio.wait_for(anext(stream), 1)).task_id == "a"
        await asyncio.wait_for(resistant.entered.wait(), 1)
        async with asyncio.timeout(1):
            with pytest.raises(CapabilityEventCleanupTimeout):
                await stream.aclose()
        assert not resistant.closed.is_set()
        release.set()
        await asyncio.wait_for(resistant.closed.wait(), 1)
    finally:
        release.set()
        await stream.aclose()


class IdlePlatform:
    async def watch(self) -> AsyncIterator[PlatformEvent]:
        yield PlatformEvent(source="fake-vendor", event_type="robot.fault.snapshot")
        await asyncio.Event().wait()


async def check_completed_task_merge(runtime: RobotRuntime | RemoteRuntime) -> None:
    robot = await RobotCapabilities.create(runtime, allowed_skills={"audio.speak"})
    observed = await robot.invoke("robot_speak", {"message": "already finished"}, operation_id="merge-finished", wait=True)
    assert observed["state"] == "succeeded"
    task_id = observed["task_id"]
    received: list[ObservedEvent] = []
    async with aclosing(robot.subscribe_events(
        sources={"task", "platform"}, task_ids={task_id}, task_terminal_only=True,
    )) as stream:
        async with asyncio.timeout(1):
            while not any(isinstance(event, Event) for event in received) or not any(isinstance(event, PlatformEvent) for event in received):
                received.append(await anext(stream))
    terminal = next(event for event in received if isinstance(event, Event))
    assert terminal.task_id == task_id and terminal.data["state"] == "succeeded"
    resumed = [event async for event in robot.subscribe_events(
        sources={"task"}, task_ids={task_id}, after_sequences={task_id: terminal.sequence - 1},
        task_terminal_only=True,
    )]
    assert resumed == [terminal]
    assert await runtime.list_skills()


async def test_completed_task_and_platform_merge_use_real_embedded_history() -> None:
    from robot_agent_runtime.adapters.simulator import Simulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository

    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills, event_source=IdlePlatform())
    await runtime.start()
    try:
        await check_completed_task_merge(runtime)
    finally:
        await runtime.close()


async def test_completed_task_and_platform_merge_use_real_uds_history() -> None:
    from robot_agent_runtime.adapters.simulator import Simulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.transport.server import RuntimeServer

    with tempfile.TemporaryDirectory() as directory:
        socket = str(Path(directory) / "runtime.sock")
        server = RuntimeServer(InMemoryTaskRepository(), Simulator().skills, socket, event_source=IdlePlatform())
        runtime = RemoteRuntime(socket)
        await server.start()
        await runtime.start()
        try:
            await check_completed_task_merge(runtime)
        finally:
            await runtime.close()
            await server.close()


async def test_all_task_mode_observes_another_real_sdk_without_platform_source() -> None:
    from robot_agent_runtime.adapters.simulator import Simulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.models import SkillCall, TaskRequest
    from robot_agent_runtime.transport.server import RuntimeServer

    with tempfile.TemporaryDirectory() as directory:
        socket = str(Path(directory) / "runtime.sock")
        server = RuntimeServer(InMemoryTaskRepository(), Simulator().skills, socket)
        observer, submitter = RemoteRuntime(socket), RemoteRuntime(socket)
        await server.start()
        await observer.start()
        await submitter.start()
        robot = await RobotCapabilities.create(observer)
        try:
            async with aclosing(robot.subscribe_events(sources={"task"}, task_terminal_only=True)) as stream:
                reading = asyncio.create_task(anext(stream))

                async def registered() -> bool:
                    return bool(server._core._events._all_subscribers)

                async with asyncio.timeout(1):
                    while not await server._host._loop.call(registered):
                        pass
                finished = await submitter.submit_and_wait(TaskRequest(task_type="other-client", steps=[SkillCall(skill="robot.get_state")]))
                received = await asyncio.wait_for(reading, 1)
                assert received.task_id == finished.id and received.data["state"] == "succeeded"
        finally:
            await observer.close()
            await submitter.close()
            await server.close()


async def test_late_source_error_is_reported_after_cleanup_timeout() -> None:
    from robot_agent_capabilities.errors import CapabilityEventCleanupTimeout

    release, reported = asyncio.Event(), asyncio.Event()
    original = ValueError("late read failure")
    contexts: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def report(owner: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        contexts.append(context)
        reported.set()

    class Resistant(Script):
        async def __anext__(self) -> ObservedEvent:
            self.entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            raise original

    resistant = Resistant()
    runtime = Combined({"a": [task_event("a", 1)]}, resistant)
    robot = await RobotCapabilities.create(runtime)
    stream = robot.subscribe_events(sources={"task", "platform"}, task_ids={"a"})
    loop.set_exception_handler(report)
    try:
        await asyncio.wait_for(anext(stream), 1)
        await asyncio.wait_for(resistant.entered.wait(), 1)
        with pytest.raises(CapabilityEventCleanupTimeout):
            await stream.aclose()
        release.set()
        await asyncio.wait_for(resistant.closed.wait(), 1)
        await asyncio.wait_for(reported.wait(), 1)
        assert contexts[0]["exception"] is original and contexts[0]["source"] == "platform"
    finally:
        release.set()
        await stream.aclose()
        loop.set_exception_handler(previous)


async def test_additional_cleanup_timeout_is_visible_after_transport_close_error() -> None:
    from robot_agent_capabilities.errors import CapabilityEventTransportError

    release = asyncio.Event()

    class Resistant(Script):
        async def __anext__(self) -> ObservedEvent:
            self.entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return task_event("a", 1)

    resistant = Resistant()
    runtime = Combined(platform=Script([platform_event()], close_error=RemoteTransportError("close reset")), tasks=resistant)
    robot = await RobotCapabilities.create(runtime)
    stream = robot.subscribe_events(sources={"task", "platform"})
    try:
        await asyncio.wait_for(anext(stream), 1)
        await asyncio.wait_for(resistant.entered.wait(), 1)
        with pytest.raises(CapabilityEventTransportError) as caught:
            await stream.aclose()
        assert any("CapabilityEventCleanupTimeout" in note and "task" in note for note in getattr(caught.value, "__notes__", ()))
    finally:
        release.set()
        await asyncio.wait_for(resistant.closed.wait(), 1)
        await stream.aclose()


async def test_resuming_after_processed_terminal_is_empty_without_waiting() -> None:
    from robot_agent_runtime.adapters.simulator import Simulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository

    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    robot = await RobotCapabilities.create(runtime)
    try:
        observed = await robot.invoke("robot_speak", {"message": "done"}, operation_id="terminal-cursor", wait=True)
        task_id = observed["task_id"]
        history = [event async for event in robot.subscribe_events(sources={"task"}, task_ids={task_id})]
        async with asyncio.timeout(1):
            resumed = [event async for event in robot.subscribe_events(
                sources={"task"}, task_ids={task_id}, after_sequences={task_id: history[-1].sequence},
            )]
        assert resumed == []
    finally:
        await runtime.close()
