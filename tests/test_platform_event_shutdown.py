import asyncio
import threading
from collections.abc import AsyncIterator
from concurrent.futures import Future

from robot_agent_runtime.models import PlatformEvent
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def test_platform_source_closes_after_cancelled_pull_finishes_late() -> None:
    class RetainedSource:
        def __init__(self) -> None:
            self.ready: Future[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = Future()
            self.cancelled: Future[None] = Future()
            self.closed: Future[int] = Future()
            self.iterator: AsyncIterator[PlatformEvent] | None = None

        def watch(self) -> AsyncIterator[PlatformEvent]:
            self.iterator = self._watch()
            return self.iterator

        async def _watch(self) -> AsyncIterator[PlatformEvent]:
            release = asyncio.Event()
            self.ready.set_result((asyncio.get_running_loop(), release))
            try:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    self.cancelled.set_result(None)
                    await release.wait()
                yield PlatformEvent(source="retained", event_type="vendor.late")
            finally:
                self.closed.set_result(threading.get_ident())

    source = RetainedSource()
    runtime = RobotRuntime(
        InMemoryTaskRepository(), {}, event_source=source,
        options=RuntimeOptions(cancel_grace_s=0.02, port_timeout_s=0.1),
    )
    await runtime.start()
    owner, release = await asyncio.wait_for(asyncio.wrap_future(source.ready), 1)
    try:
        closing = asyncio.create_task(runtime.close())
        await asyncio.wait_for(asyncio.wrap_future(source.cancelled), 1)
        await asyncio.wait_for(closing, 1)
        assert source.iterator is not None
        assert not source.closed.done()
        owner.call_soon_threadsafe(release.set)
        closed_on = await asyncio.wait_for(asyncio.wrap_future(source.closed), 1)
        assert closed_on != threading.get_ident()
    finally:
        if not owner.is_closed():
            owner.call_soon_threadsafe(release.set)
        await runtime.close()


async def test_core_stop_does_not_restart_state_pull_after_authoritative_write() -> None:
    from robot_agent_runtime.kernel.core import RuntimeCore

    entered = asyncio.Event()
    next_pull = asyncio.Event()
    release = asyncio.Event()
    committed = asyncio.Event()

    class State:
        async def unsatisfied(self) -> frozenset[str]:
            return frozenset()

        async def watch(self) -> AsyncIterator[frozenset[str]]:
            yield frozenset({"vendor.unavailable"})
            next_pull.set()
            await release.wait()

    class PersistingCore(RuntimeCore):
        async def _cancel_blocked_tasks(self, unsatisfied: frozenset[str]) -> None:
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                # A dispatched repository write returns its authoritative
                # outcome before shutdown can abandon this observation loop.
                committed.set()

    core = PersistingCore(InMemoryTaskRepository(), {}, robot_state=State())
    await core.start()
    await asyncio.wait_for(entered.wait(), 1)
    try:
        await asyncio.wait_for(core.close(), 0.2)
        assert committed.is_set() and not next_pull.is_set()
    finally:
        release.set()
        await asyncio.wait_for(core.close(), 1)


async def test_notification_close_does_not_restart_source_pull_after_saved_write() -> None:
    from robot_agent_runtime.hosting.notification_host import NotificationHost
    from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository

    entered: Future[None] = Future()
    released: Future[None] = Future()
    next_pull = threading.Event()
    source_closed = threading.Event()

    class Source:
        async def watch(self) -> AsyncIterator[PlatformEvent]:
            try:
                yield PlatformEvent(source="test", event_type="vendor.observed")
                next_pull.set()
                await asyncio.wrap_future(released)
            finally:
                source_closed.set()

    class Repository(InMemoryPlatformEventRepository):
        async def append_platform_event(self, event: PlatformEvent) -> PlatformEvent:
            entered.set_result(None)
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return await super().append_platform_event(event)

    host = NotificationHost(Repository(), Source(), RuntimeOptions(cancel_grace_s=0.02))
    await host.start()
    await asyncio.wait_for(asyncio.wrap_future(entered), 1)
    try:
        await asyncio.wait_for(host.close(), 1)
        assert source_closed.is_set() and not next_pull.is_set()
    finally:
        if not released.done():
            released.set_result(None)
        await host._collector.wait(1)
        await host.close()
