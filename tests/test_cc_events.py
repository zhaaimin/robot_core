import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from robot_agent_runtime.errors import (
    EventStreamOverflow, ExtensionCapacityExceeded, RobotConnectionError,
)
from robot_agent_runtime.options import RuntimeOptions


@dataclass
class Response:
    is_success: bool = True
    result: Any = None
    error_code: int = 0
    error_message: str = ""


class Api:
    def __init__(self) -> None:
        self.services: dict[str, Callable[..., None]] = {}
        self.requests: asyncio.Queue[tuple[str, dict[str, Any], Callable[..., None]]] = asyncio.Queue()
        self.notifications: list[str] = []
        self.registrations: list[str] = []
        self.removals: list[str] = []
        self.disconnect: Callable[[], None] = lambda: None

    def request(self, method: str, params: dict[str, Any], callback: Callable[..., None]) -> None:
        self.requests.put_nowait((method, params, callback))

    def notify(self, method: str) -> None:
        self.notifications.append(method)

    def add_service(self, name: str, callback: Callable[..., None]) -> None:
        self.registrations.append(name)
        self.services[name] = callback

    def remove_service(self, name: str) -> None:
        self.removals.append(name)
        self.services.pop(name, None)

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        self.disconnect = callback

    def fault(self, payload: Any) -> None:
        self.services["cc.api.fault.current.update"]("event", payload)

    async def next_request(self) -> tuple[str, dict[str, Any], Callable[..., None]]:
        return await asyncio.wait_for(self.requests.get(), 1)


async def checkpoint() -> None:
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


async def test_cc_fault_events_remain_disabled_by_default() -> None:
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform

    api = Api()
    platform = create_cc_api_platform(api)
    assert getattr(platform, "event_source", "missing") is None
    await platform.start()
    await platform.close()
    assert api.registrations == ["cc.api.work.finished"]
    assert api.notifications == ["cc.api.work.subscribe", "cc.api.work.unsubscribe"]


async def test_cc_fault_source_starts_before_host_and_queries_only_when_watched() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiEventSource, create_cc_api_platform

    api = Api()
    platform = create_cc_api_platform(api, enable_events=True)
    await platform.start()
    assert isinstance(platform.event_source, CcApiEventSource)
    assert api.requests.empty()
    assert api.registrations == ["cc.api.work.finished", "cc.api.fault.current.update"]
    assert api.notifications == ["cc.api.work.subscribe", "cc.api.fault.subscribe"]
    stream = platform.event_source.watch()
    first = asyncio.create_task(anext(stream))
    try:
        method, params, respond = await api.next_request()
        assert method == "cc.api.fault.current.get" and params == {}
        payload = {"vendorVersion": "unknown", "faults": [{"code": 7}]}
        respond(Response(result=payload))
        snapshot = await asyncio.wait_for(first, 1)
        assert snapshot.source == "cc_api" and snapshot.event_type == "robot.fault.snapshot"
        assert snapshot.cursor == 0 and snapshot.task_id is None
        assert snapshot.data["payload"] == payload
        assert snapshot.data["sync"]["initial_snapshot_order"] == "unverified"
        payload["faults"].clear()
        assert snapshot.data["payload"]["faults"] == [{"code": 7}]
        api.fault({"faults": [{"code": 8}]})
        changed = await asyncio.wait_for(anext(stream), 1)
        assert changed.event_type == "robot.fault.changed"
        assert changed.data["payload"] == {"faults": [{"code": 8}]}
        assert api.requests.empty()
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await stream.aclose()
        await platform.close()
    assert "cc.api.fault.unsubscribe" not in api.notifications
    assert api.removals == ["cc.api.fault.current.update", "cc.api.work.finished"]


async def test_cc_fault_source_preserves_buffered_updates_without_claiming_snapshot_order() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver, CcApiEventSource

    api = Api()
    driver = CcApiDriver(api)
    source = CcApiEventSource(driver)
    await source.start()
    api.fault({"code": "first"})
    api.fault({"code": "second"})
    await checkpoint()
    stream = source.watch()
    first = asyncio.create_task(anext(stream))
    try:
        _, _, respond = await api.next_request()
        respond(Response(result={"code": "second"}))
        assert (await first).event_type == "robot.fault.snapshot"
        updates = [await anext(stream), await anext(stream)]
        assert [event.data["payload"] for event in updates] == [
            {"code": "first"}, {"code": "second"},
        ]
        assert all(event.data["sync"]["initial_snapshot_order"] == "unverified" for event in updates)
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await stream.aclose()
        await source.close()
        await driver.close()


async def test_cc_fault_source_merges_identical_consecutive_payloads_and_keeps_changes() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver, CcApiEventSource

    api = Api()
    driver = CcApiDriver(api)
    source = CcApiEventSource(driver)
    await source.start()
    stream = source.watch()
    first = asyncio.create_task(anext(stream))
    try:
        _, _, respond = await api.next_request()
        respond(Response(result={"a": 1, "b": 2}))
        await first
        api.fault({"b": 2, "a": 1})
        api.fault({"a": 2, "b": 2})
        api.fault({"b": 2, "a": 2})
        api.fault({"a": 1, "b": 2})
        assert (await anext(stream)).data["payload"] == {"a": 2, "b": 2}
        assert (await anext(stream)).data["payload"] == {"a": 1, "b": 2}
        pending = asyncio.create_task(anext(stream))
        await checkpoint()
        assert not pending.done()
        await source.close()
        with pytest.raises(StopAsyncIteration):
            await pending
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await stream.aclose()
        await source.close()
        await driver.close()


async def test_cc_fault_native_thread_callback_is_detached_and_does_not_poison_driver() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver, CcApiEventSource

    api = Api()
    driver = CcApiDriver(api)
    source = CcApiEventSource(driver)
    await source.start()
    payload = {"faults": [{"code": "motor"}]}

    def native() -> None:
        api.fault(payload)
        payload["faults"].clear()

    thread = threading.Thread(target=native)
    thread.start()
    thread.join()
    stream = source.watch()
    first = asyncio.create_task(anext(stream))
    try:
        _, _, respond = await api.next_request()
        respond(Response(result={"faults": []}))
        await first
        assert (await anext(stream)).data["payload"] == {"faults": [{"code": "motor"}]}
        query = asyncio.create_task(driver.request("read-only", {}))
        _, _, respond = await api.next_request()
        respond(Response(result={"ok": True}))
        assert await query == {"ok": True}
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await stream.aclose()
        await source.close()
        await driver.close()


async def test_cc_fault_source_close_detaches_only_its_own_driver_subscriptions() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver, CcApiEventSource

    api = Api()
    driver = CcApiDriver(api)
    first, second = CcApiEventSource(driver), CcApiEventSource(driver)
    await first.start()
    await second.start()
    await first.close()
    stream = second.watch()
    initial = asyncio.create_task(anext(stream))
    try:
        _, _, respond = await api.next_request()
        respond(Response(result=[]))
        await initial
        api.fault([{"code": 1}])
        assert (await anext(stream)).data["payload"] == [{"code": 1}]
        assert api.registrations.count("cc.api.fault.current.update") == 1
        assert api.notifications.count("cc.api.fault.subscribe") == 1
        assert not api.removals
    finally:
        initial.cancel()
        await asyncio.gather(initial, return_exceptions=True)
        await stream.aclose()
        await second.close()
        await driver.close()


async def test_cc_fault_disconnect_emits_connection_lost_then_fails_without_empty_faults() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver, CcApiEventSource
    from robot_agent_runtime.errors import PlatformEventSourceUnavailable

    api = Api()
    driver = CcApiDriver(api)
    source = CcApiEventSource(driver)
    await source.start()
    stream = source.watch()
    first = asyncio.create_task(anext(stream))
    try:
        _, _, respond = await api.next_request()
        respond(Response(result={"faults": [{"code": 1}]}))
        await first
        api.disconnect()
        lost = await asyncio.wait_for(anext(stream), 1)
        assert lost.event_type == "robot.connection.lost"
        assert lost.data["error"]["code"] == RobotConnectionError.code
        assert "payload" not in lost.data
        with pytest.raises(PlatformEventSourceUnavailable):
            await anext(stream)
        assert api.requests.empty()
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await stream.aclose()
        await source.close()
        await driver.close()


async def test_cc_fault_source_buffer_overflow_is_explicit_after_preserved_update() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver, CcApiEventSource

    api = Api()
    driver = CcApiDriver(api)
    source = CcApiEventSource(driver, options=RuntimeOptions(max_event_buffer=1))
    await source.start()
    api.fault({"code": 1})
    await checkpoint()
    api.fault({"code": 2})
    await checkpoint()
    stream = source.watch()
    try:
        assert (await anext(stream)).data["payload"] == {"code": 1}
        with pytest.raises(EventStreamOverflow):
            await anext(stream)
        assert api.requests.empty()
        query = asyncio.create_task(driver.request("read-only", {}))
        _, _, respond = await api.next_request()
        respond(Response(result={}))
        assert await query == {}
    finally:
        await stream.aclose()
        await source.close()
        await driver.close()


async def test_cc_fault_driver_subscriptions_share_capacity_and_detach_individually() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api, options=RuntimeOptions(max_subscriptions=2))
    seen: list[Any] = []
    detach = driver.subscribe_fault(seen.append)
    driver.subscribe_finished(lambda _: None)
    with pytest.raises(ExtensionCapacityExceeded):
        driver.subscribe_failure(lambda _: None)
    detach()
    detach()
    driver.subscribe_failure(lambda _: None)
    await driver.start()
    assert api.registrations == ["cc.api.work.finished"]
    await driver.close()


async def test_cc_fault_watch_runs_from_foreign_loop_and_cancels_its_owner_waiter() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver, CcApiEventSource

    api = Api()
    driver = CcApiDriver(api)
    source = CcApiEventSource(driver)
    await source.start()
    owner = asyncio.get_running_loop()
    completed = owner.create_future()

    def consume() -> None:
        async def run() -> Any:
            stream = source.watch()
            try:
                snapshot = await anext(stream)
                update = await anext(stream)
                waiting = asyncio.create_task(anext(stream))
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                return snapshot, update
            finally:
                await stream.aclose()

        try:
            result = asyncio.run(run())
        except BaseException as error:
            owner.call_soon_threadsafe(completed.set_exception, error)
        else:
            owner.call_soon_threadsafe(completed.set_result, result)

    thread = threading.Thread(target=consume)
    thread.start()
    try:
        method, _, respond = await api.next_request()
        assert method == "cc.api.fault.current.get"
        respond(Response(result={"faults": []}))
        api.fault({"faults": ["foreign"]})
        snapshot, update = await asyncio.wait_for(completed, 1)
        assert snapshot.event_type == "robot.fault.snapshot"
        assert update.data["payload"] == {"faults": ["foreign"]}
    finally:
        await source.close()
        await driver.close()
        thread.join(timeout=1)
    assert not thread.is_alive()
