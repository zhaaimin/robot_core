import asyncio
import tempfile
import threading
from concurrent.futures import Future
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import PlatformEventSourceUnavailable
from robot_agent_runtime.models import PlatformEvent, SkillCall, TaskRequest
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository
from robot_agent_runtime.sdk import RemoteRuntime, RobotRuntime
from robot_agent_runtime.transport.server import RuntimeServer


class Source:
    def __init__(self) -> None:
        self.ready: Future = Future()
        self.owner = 0
        self.closed = threading.Event()

    async def watch(self):
        self.owner = threading.get_ident()
        queue: asyncio.Queue[PlatformEvent] = asyncio.Queue()
        self.ready.set_result((asyncio.get_running_loop(), queue))
        try:
            yield PlatformEvent(event_id="initial", source="other-vendor", event_type="robot.fault.snapshot", data={"faults": []})
            while True:
                yield await queue.get()
        finally:
            self.closed.set()

    async def emit(self, event: PlatformEvent) -> None:
        loop, queue = await asyncio.wrap_future(self.ready)
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(queue.put(event), loop))


async def test_unexpected_source_cancellation_ends_existing_and_new_platform_subscriptions() -> None:
    ready: Future[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = Future()

    class CancelledSource:
        async def watch(self) -> AsyncIterator[PlatformEvent]:
            cancelled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            ready.set_result((asyncio.get_running_loop(), cancelled))
            yield PlatformEvent(event_id="before-cancellation", source="vendor", event_type="vendor.snapshot")
            await cancelled

    runtime = RobotRuntime(InMemoryTaskRepository(), {}, event_source=CancelledSource())
    await runtime.start()
    try:
        async with aclosing(runtime.subscribe_platform_events()) as existing:
            saved = await asyncio.wait_for(anext(existing), 1)
            owner, cancelled = await asyncio.wait_for(asyncio.wrap_future(ready), 1)
            owner.call_soon_threadsafe(cancelled.cancel)
            with pytest.raises(PlatformEventSourceUnavailable) as failure:
                await asyncio.wait_for(anext(existing), 1)
            assert failure.value.info.code == "PLATFORM_EVENT_SOURCE_UNAVAILABLE"
        async with aclosing(runtime.subscribe_platform_events()) as resumed:
            assert (await asyncio.wait_for(anext(resumed), 1)).event_id == saved.event_id
            with pytest.raises(PlatformEventSourceUnavailable):
                await asyncio.wait_for(anext(resumed), 1)
    finally:
        await runtime.close()


async def test_runtime_shutdown_finishes_platform_subscription_without_source_failure() -> None:
    source = Source()
    runtime = RobotRuntime(InMemoryTaskRepository(), {}, event_source=source)
    await runtime.start()
    try:
        async with aclosing(runtime.subscribe_platform_events()) as stream:
            await asyncio.wait_for(anext(stream), 1)
            await asyncio.wait_for(runtime.close(), 1)
            async with asyncio.timeout(1):
                assert [event async for event in stream] == []
        assert source.closed.is_set()
    finally:
        await runtime.close()


async def test_embedded_idle_platform_events_replay_and_owner_isolation() -> None:
    source = Source()
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills, event_source=source)
    await runtime.start()
    try:
        async with aclosing(runtime.subscribe_platform_events()) as stream:
            first = await asyncio.wait_for(anext(stream), 1)
            assert first.cursor == 1 and first.task_id is None
            await source.emit(PlatformEvent(source="other-vendor", event_type="robot.fault.changed", data={"faults": ["motor"]}))
            assert (await asyncio.wait_for(anext(stream), 1)).cursor == 2
        async with aclosing(runtime.subscribe_platform_events(after_cursor=1)) as resumed:
            assert (await asyncio.wait_for(anext(resumed), 1)).data["faults"] == ["motor"]
        assert source.owner != threading.get_ident()
    finally:
        await runtime.close()
    assert source.closed.is_set()


async def test_remote_platform_events_reconnect_and_filter() -> None:
    with tempfile.TemporaryDirectory() as directory:
        source = Source()
        server = RuntimeServer(InMemoryTaskRepository(), Simulator().skills, str(Path(directory) / "runtime.sock"), event_source=source)
        await server.start()
        remote = RemoteRuntime(str(Path(directory) / "runtime.sock"))
        await remote.start()
        try:
            async with aclosing(remote.subscribe_platform_events()) as initial:
                assert (await asyncio.wait_for(anext(initial), 1)).cursor == 1
            await source.emit(PlatformEvent(source="other-vendor", event_type="robot.fault.changed", data={"faults": ["motor"]}))
            async with aclosing(remote.subscribe_platform_events(after_cursor=1, event_types={"robot.fault.changed"})) as resumed:
                value = await asyncio.wait_for(anext(resumed), 1)
                assert value.cursor == 2 and value.source == "other-vendor"
        finally:
            await remote.close()
            await server.close()


async def test_blocked_event_source_does_not_block_kernel_or_close() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingSource:
        async def watch(self):
            entered.set()
            release.wait(3)
            yield PlatformEvent(source="blocked", event_type="vendor.event")

    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills, event_source=BlockingSource(), options=RuntimeOptions(cancel_grace_s=0.05, port_timeout_s=0.2))
    try:
        await runtime.start()
        task = await asyncio.wait_for(runtime.submit_task(TaskRequest(task_type="still-alive", steps=[SkillCall(skill="robot.get_state")])), 1)
        assert task.id
        await asyncio.wait_for(runtime.close(), 1)
    finally:
        release.set()


async def test_disabled_platform_events_fail_explicitly() -> None:
    from robot_agent_runtime.errors import InvalidRequest

    runtime = RobotRuntime(InMemoryTaskRepository(), {})
    await runtime.start()
    try:
        with pytest.raises(InvalidRequest):
            await anext(runtime.subscribe_platform_events())
    finally:
        await runtime.close()


async def test_task_stream_close_unregisters_without_waiting_for_garbage_collection() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    try:
        task = await runtime.submit_task(TaskRequest(task_type="cleanup", steps=[SkillCall(skill="robot.get_state")]))
        stream = runtime.subscribe_task_events(task.id)
        await anext(stream)
        await stream.aclose()
        assert not runtime._host._streams
    finally:
        await runtime.close()


async def test_platform_journal_failed_preparation_still_releases_connection() -> None:
    from robot_agent_runtime.errors import InvalidRequest

    class BrokenJournal(InMemoryPlatformEventRepository):
        closed = False

        async def platform_event_bounds(self):
            raise InvalidRequest("unreadable journal")

        async def close(self):
            self.closed = True
            await super().close()

    journal = BrokenJournal()
    runtime = RobotRuntime(InMemoryTaskRepository(), {}, event_repository=journal)
    try:
        with pytest.raises(InvalidRequest):
            await runtime.start()
    finally:
        await runtime.close()
    assert journal.closed


async def test_replay_pages_fit_snapshot_budget_for_individually_valid_events() -> None:
    journal = InMemoryPlatformEventRepository()
    await journal.start()
    for number in (1, 2):
        await journal.append_platform_event(PlatformEvent(source="large", event_type="large.event", data={"value": "x" * 1800}))
    await journal.close()
    runtime = RobotRuntime(InMemoryTaskRepository(), {}, event_repository=journal, options=RuntimeOptions(max_snapshot_bytes=3000))
    await runtime.start()
    try:
        async with aclosing(runtime.subscribe_platform_events()) as stream:
            assert [(await anext(stream)).cursor, (await anext(stream)).cursor] == [1, 2]
    finally:
        await runtime.close()


async def test_cc_platform_idle_fault_reaches_embedded_sdk_and_disconnect_is_explicit() -> None:
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform
    from robot_agent_runtime.errors import PlatformEventSourceUnavailable

    class Api:
        def __init__(self) -> None:
            self.services: dict[str, Any] = {}

        def request(self, method: str, params: dict[str, Any], callback: Any) -> None:
            assert method == "cc.api.fault.current.get"
            callback(SimpleNamespace(is_success=True, result={"faults": []}))

        def notify(self, method: str) -> None:
            pass

        def add_service(self, name: str, callback: Any) -> None:
            self.services[name] = callback

        def remove_service(self, name: str) -> None:
            self.services.pop(name, None)

        def set_disconnect_callback(self, callback: Any) -> None:
            self.disconnect = callback

    api = Api()
    platform = create_cc_api_platform(api, enable_events=True)
    runtime = RobotRuntime.from_platform(InMemoryTaskRepository(), platform)
    await platform.start()
    await runtime.start()
    try:
        async with aclosing(runtime.subscribe_platform_events()) as stream:
            initial = await asyncio.wait_for(anext(stream), 1)
            assert initial.event_type == "robot.fault.snapshot" and initial.cursor == 1
            worker = threading.Thread(target=lambda: api.services["cc.api.fault.current.update"]("native", {"faults": [7]}))
            worker.start()
            worker.join(1)
            changed = await asyncio.wait_for(anext(stream), 1)
            assert changed.data["payload"] == {"faults": [7]} and changed.task_id is None
            api.disconnect()
            assert (await asyncio.wait_for(anext(stream), 1)).event_type == "robot.connection.lost"
            with pytest.raises(PlatformEventSourceUnavailable):
                await asyncio.wait_for(anext(stream), 1)
    finally:
        try:
            await runtime.close()
        finally:
            await platform.close()


async def test_serve_cc_enables_fault_source_and_persistent_event_journal() -> None:
    import sys
    import robot_agent_runtime.__main__ as cli
    from robot_agent_runtime.repositories.platform_events import SQLitePlatformEventRepository

    captured: dict[str, Any] = {}

    class Api:
        def initialize(self, config: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def wait_until_connected(self, timeout: int) -> bool:
            return True

        def stop(self) -> None:
            captured["stopped"] = True

    class Platform:
        skills: dict = {}
        event_source = object()

        async def start(self) -> None:
            captured["started"] = True

        async def close(self) -> None:
            captured["platform_closed"] = True

    def platform_factory(api: Any, **kwargs: Any) -> Platform:
        captured["platform_kwargs"] = kwargs
        return Platform()

    def server_factory(*args: Any, **kwargs: Any) -> object:
        captured["server_kwargs"] = kwargs
        return object()

    async def run_server(server: Any, socket_path: str) -> None:
        assert captured["started"]

    with tempfile.TemporaryDirectory() as directory:
        with patch.dict(sys.modules, ubt_robot=SimpleNamespace(Api=Api, Config=SimpleNamespace)):
            with patch("robot_agent_runtime.adapters.cc_api.create_cc_api_platform", platform_factory):
                with patch.object(cli, "RuntimeServer", server_factory), patch.object(cli, "_run_server", run_server):
                    await cli.serve_cc("unused.sock", str(Path(directory) / "runtime.db"), "test", None, "")
    assert captured["platform_kwargs"].get("enable_events") is True
    assert captured["server_kwargs"].get("event_source") is Platform.event_source
    assert isinstance(captured["server_kwargs"].get("event_repository"), SQLitePlatformEventRepository)
    assert captured["platform_closed"] and captured["stopped"]


async def test_platform_consumer_uses_shutdown_buffer_when_loop_closes_before_pull() -> None:
    from robot_agent_runtime.hosting.event_stream import HostedEventStream, _EMPTY
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    runtime = RobotRuntime(InMemoryTaskRepository(), {}, event_source=Source())
    consumer = LoopThread("platform-race-consumer")
    initial: Future[int] = Future()
    proceed: Future[None] = Future()
    paused: Future[None] = Future()
    released = threading.Event()
    original = HostedEventStream.offline_next
    consumer_thread: list[int] = []

    async def read() -> PlatformEvent:
        consumer_thread.append(threading.get_ident())
        async with aclosing(runtime.subscribe_platform_events()) as stream:
            initial.set_result((await anext(stream)).cursor)
            await asyncio.wrap_future(proceed)
            return await anext(stream)

    def pause_between_read_and_submit(stream: HostedEventStream):
        value = original(stream)
        if value is _EMPTY and consumer_thread and threading.get_ident() == consumer_thread[0] and not paused.done():
            paused.set_result(None)
            released.wait(3)
        return value

    await runtime.start()
    await consumer.start()
    job = consumer.submit(read)
    try:
        assert await asyncio.wait_for(asyncio.wrap_future(initial), 1) == 1
        await runtime._host._event_source.emit(PlatformEvent(source="other-vendor", event_type="robot.fault.changed"))

        async def latest() -> int:
            return runtime._host._notifications._service._latest

        async with asyncio.timeout(1):
            while await runtime._host._notifications._loop.call(latest) < 2:
                pass
        with patch.object(HostedEventStream, "offline_next", pause_between_read_and_submit):
            proceed.set_result(None)
            await asyncio.wait_for(asyncio.wrap_future(paused), 1)
            await runtime.close()
            released.set()
            assert (await job.wait(1)).cursor == 2
    finally:
        released.set()
        await runtime.close()
        await consumer.close()


async def test_remote_expired_platform_cursor_preserves_recovery_error() -> None:
    from robot_agent_runtime.sdk import RemoteRuntimeError

    source = Source()
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "runtime.sock")
        server = RuntimeServer(InMemoryTaskRepository(), {}, path, event_source=source, event_repository=InMemoryPlatformEventRepository(max_events=1))
        remote = RemoteRuntime(path)
        await server.start()
        await remote.start()
        try:
            async with aclosing(remote.subscribe_platform_events()) as stream:
                assert (await asyncio.wait_for(anext(stream), 1)).cursor == 1
                await source.emit(PlatformEvent(source="other-vendor", event_type="robot.fault.changed"))
                assert (await asyncio.wait_for(anext(stream), 1)).cursor == 2
            async with aclosing(remote.subscribe_platform_events()) as expired:
                with pytest.raises(RemoteRuntimeError) as caught:
                    await asyncio.wait_for(anext(expired), 1)
                assert caught.value.info.code == "PLATFORM_EVENT_CURSOR_EXPIRED"
                assert caught.value.info.details["earliest_cursor"] == 2
        finally:
            await remote.close()
            await server.close()


async def test_remote_all_task_events_observe_other_sdk_submissions_live() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "runtime.sock")
        server = RuntimeServer(InMemoryTaskRepository(), Simulator().skills, path)
        observer = RemoteRuntime(path)
        submitter = RemoteRuntime(path)
        await server.start()
        await observer.start()
        await submitter.start()
        try:
            async with aclosing(observer.subscribe_all_task_events({"TASK_CREATED"})) as stream:
                pending = asyncio.create_task(anext(stream))

                async def registered() -> bool:
                    return bool(server._core._events._all_subscribers)

                async with asyncio.timeout(1):
                    while not await server._host._loop.call(registered):
                        pass
                first = await submitter.submit_task(TaskRequest(task_type="one", steps=[SkillCall(skill="robot.get_state")]))
                assert (await asyncio.wait_for(pending, 1)).task_id == first.id
                second = await submitter.submit_task(TaskRequest(task_type="two", steps=[SkillCall(skill="robot.get_state")]))
                value = await asyncio.wait_for(anext(stream), 1)
                assert value.task_id == second.id and value.sequence == 1
        finally:
            await observer.close()
            await submitter.close()
            await server.close()
