import asyncio
import runpy
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from pathlib import Path
from unittest.mock import patch

import pytest

from robot_agent_runtime.errors import (
    EventStreamOverflow, ExtensionCapacityExceeded, RobotConnectionError, RobotRequestError,
    RobotResponseError, RobotStopError, TaskCancelled,
)
from robot_agent_runtime.models import SkillCall, SkillContext, SkillResult, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.thread_binding import ThreadBinding, unwrap_thread_binding


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
        self.disconnect: Callable[[], None] = lambda: None
        self.stops = 0

    def request(self, method: str, params: dict[str, Any], callback: Callable[..., None]) -> None:
        self.requests.put_nowait((method, params, callback))

    def notify(self, method: str) -> None:
        self.notifications.append(method)

    def add_service(self, name: str, callback: Callable[..., None]) -> None:
        self.registrations.append(name)
        self.services[name] = callback

    def remove_service(self, name: str) -> None:
        self.services.pop(name, None)

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        self.disconnect = callback

    def finish(self, work_id: str) -> None:
        self.services["cc.api.work.finished"]("event", {"workId": work_id})

    def stop(self) -> None:
        self.stops += 1

    async def next_request(self) -> tuple[str, dict[str, Any], Callable[..., None]]:
        return await asyncio.wait_for(self.requests.get(), 1)


def context() -> SkillContext:
    return SkillContext("task", "execution", asyncio.Event())


async def checkpoint() -> None:
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


async def test_cc_platform_factory_exposes_existing_runtime_ports_and_speech() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill, create_cc_api_platform
    from robot_agent_runtime.ports import RobotPlatform

    api = Api()
    platform: RobotPlatform = create_cc_api_platform(api)
    assert set(platform.skills) == {"audio.speak", "robot.status"}
    assert isinstance(unwrap_thread_binding(platform.skills["audio.speak"]), CcApiSpeechSkill)
    assert platform.robot_state is None and platform.reconciler is None
    with pytest.raises(TypeError):
        platform.skills["other"] = platform.skills["audio.speak"]
    await platform.start()
    await platform.start()
    runtime = RobotRuntime.from_platform(InMemoryTaskRepository(), platform)
    try:
        await runtime.start()
        task = await runtime.submit_task(TaskRequest(task_type="platform", steps=[
            SkillCall(skill="audio.speak", params={"message": "hello"}),
        ]))
        method, params, respond = await api.next_request()
        assert method == "cc.api.work.start" and params["params"]["text"] == "hello"
        respond(Response(result={"workId": "speech"}))
        api.finish("speech")
        async with asyncio.timeout(1):
            async for _ in runtime.subscribe_task_events(task.id):
                pass
        assert (await runtime.get_task_status(task.id)).state == TaskState.SUCCEEDED
    finally:
        try:
            await runtime.close()
        finally:
            await platform.close()
    await platform.close()
    assert api.notifications == ["cc.api.work.subscribe", "cc.api.work.unsubscribe"]
    assert api.registrations == ["cc.api.work.finished"]
    assert not api.services and api.stops == 0


async def test_cc_driver_fans_out_events_and_failure_without_overwriting_callbacks() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api)
    first: list[str] = []
    second: list[str] = []
    failures: list[Exception] = []
    detach_first = driver.subscribe_finished(first.append)
    driver.subscribe_finished(second.append)
    driver.subscribe_failure(failures.append)
    await driver.start()
    await driver.start()
    try:
        api.finish("both")
        await checkpoint()
        assert first == second == ["both"]
        detach_first()
        detach_first()
        api.finish("second")
        await checkpoint()
        assert first == ["both"] and second == ["both", "second"]
        api.disconnect()
        await checkpoint()
        assert len(failures) == 1 and isinstance(failures[0], RobotConnectionError)
        with pytest.raises(RobotConnectionError):
            await driver.request("must-not-dispatch", {})
        assert api.requests.empty()
    finally:
        await driver.close()
    assert api.registrations == ["cc.api.work.finished"]


async def test_cc_driver_correlates_reversed_responses_and_preserves_vendor_rejection() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api)
    await driver.start()
    try:
        first = asyncio.create_task(driver.request("first", {"a": 1}))
        second = asyncio.create_task(driver.request("second", {"b": 2}))
        _, _, respond_first = await api.next_request()
        _, _, respond_second = await api.next_request()
        respond_second(Response(result={"value": "second"}))
        respond_first(Response(False, error_code=42, error_message="rejected"))
        assert await second == {"value": "second"}
        with pytest.raises(RobotRequestError) as caught:
            await first
        assert caught.value.info.details["vendor_code"] == 42
        third = asyncio.create_task(driver.request("third", {}))
        _, _, respond = await api.next_request()
        respond(Response(result={"ok": True}))
        assert await third == {"ok": True}
    finally:
        await driver.close()


async def test_shared_driver_keeps_other_skill_alive_when_one_skill_closes() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver, CcApiSpeechSkill

    api = Api()
    driver = CcApiDriver(api)
    first, second = CcApiSpeechSkill(driver), CcApiSpeechSkill(driver)
    await first.start()
    await second.start()
    try:
        await first.close()
        assert api.notifications == ["cc.api.work.subscribe"]
        assert api.registrations == ["cc.api.work.finished"]
        execution = asyncio.create_task(second.execute(context(), {"message": "still connected"}))
        _, _, respond = await api.next_request()
        respond(Response(result={"workId": "second"}))
        api.finish("second")
        assert (await asyncio.wait_for(execution, 1)).success
    finally:
        await second.close()
        await driver.close()
    assert api.notifications[-1] == "cc.api.work.unsubscribe"


async def test_platform_close_stops_active_speech_before_releasing_driver() -> None:
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform

    api = Api()
    platform = create_cc_api_platform(api)
    await platform.start()
    skill = unwrap_thread_binding(platform.skills["audio.speak"])
    execution = asyncio.create_task(skill.execute(context(), {"message": "hello"}))
    _, _, respond = await api.next_request()
    respond(Response(result={"workId": "active"}))
    closing = asyncio.create_task(platform.close())
    method, params, respond_stop = await api.next_request()
    assert method == "cc.api.work.ctrl" and params["workId"] == "active"
    assert api.notifications == ["cc.api.work.subscribe"]
    respond_stop(Response())
    await checkpoint()
    assert not closing.done()
    api.finish("active")
    await asyncio.wait_for(closing, 1)
    with pytest.raises(TaskCancelled):
        await execution
    assert not api.services and api.notifications[-1] == "cc.api.work.unsubscribe"


async def test_platform_start_failure_rolls_back_driver_callbacks() -> None:
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform

    class BrokenApi(Api):
        def notify(self, method: str) -> None:
            super().notify(method)
            if method == "cc.api.work.subscribe":
                raise ValueError("Subscription failed")

    api = BrokenApi()
    platform = create_cc_api_platform(api)
    with pytest.raises(RobotConnectionError):
        await platform.start()
    await platform.close()
    assert not api.services and api.stops == 0


async def test_cc_driver_snapshots_sdk_responses_before_cross_thread_delivery() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api)
    await driver.start()
    try:
        pending = asyncio.create_task(driver.request("snapshot", {}))
        _, _, respond = await api.next_request()
        values = {"items": ["original"]}

        def producer() -> None:
            respond(Response(result=values))
            values["items"].append("mutated")

        worker = threading.Thread(target=producer)
        worker.start()
        await asyncio.to_thread(worker.join, 1)
        assert await asyncio.wait_for(pending, 1) == {"items": ["original"]}
    finally:
        await driver.close()


async def test_cc_driver_bounds_requests_and_reserves_capacity_for_stop_control() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api, options=RuntimeOptions(max_pending_calls=1, max_control_calls=1))
    await driver.start()
    first = asyncio.create_task(driver.request("normal", {}))
    _, _, respond_first = await api.next_request()
    try:
        with pytest.raises(ExtensionCapacityExceeded):
            await driver.request("rejected", {})
        stop = asyncio.create_task(driver.request("stop", {}, control=True))
        method, _, respond_stop = await api.next_request()
        assert method == "stop"
        with pytest.raises(ExtensionCapacityExceeded):
            await driver.request("rejected-control", {}, control=True)
        respond_stop(Response(result={"stopped": True}))
        assert await stop == {"stopped": True}
        respond_first(Response(result={}))
        await first
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await driver.close()


async def test_speech_stop_uses_driver_control_capacity_when_normal_requests_are_full() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver, CcApiSpeechSkill

    api = Api()
    driver = CcApiDriver(api, options=RuntimeOptions(max_pending_calls=1, max_control_calls=1))
    speech = CcApiSpeechSkill(driver)
    await speech.start()
    ctx = context()
    execution = asyncio.create_task(speech.execute(ctx, {"message": "stop under load"}))
    _, _, respond = await api.next_request()
    respond(Response(result={"workId": "active"}))
    await checkpoint()
    pending = asyncio.create_task(driver.request("unrelated", {}))
    _, _, respond_pending = await api.next_request()
    try:
        ctx.cancel_event.set()
        method, params, respond_stop = await api.next_request()
        assert method == "cc.api.work.ctrl" and params["workId"] == "active"
        respond_stop(Response())
        api.finish("active")
        with pytest.raises(TaskCancelled):
            await execution
        respond_pending(Response(result={}))
        await pending
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await speech.close()
        await driver.close()


async def test_cc_driver_response_deadline_fails_closed_without_dispatching_more_work() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api, options=RuntimeOptions(port_timeout_s=0.02))
    failures: list[Exception] = []
    driver.subscribe_failure(failures.append)
    await driver.start()
    try:
        pending = asyncio.create_task(driver.request("no-response", {}))
        _, _, late_response = await api.next_request()
        with pytest.raises(RobotConnectionError):
            await asyncio.wait_for(pending, 1)
        assert len(failures) == 1
        late_response(Response(result={"late": True}))
        with pytest.raises(RobotConnectionError):
            await driver.request("must-not-retry", {})
        assert api.requests.empty()
    finally:
        await driver.close()


async def test_cc_driver_callback_overflow_fails_closed_and_detaches() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api, options=RuntimeOptions(max_event_buffer=1))
    failures: list[Exception] = []
    driver.subscribe_failure(failures.append)
    await driver.start()
    try:
        api.finish("first")
        api.finish("overflow")
        await checkpoint()
        assert len(failures) == 1 and isinstance(failures[0], EventStreamOverflow)
        with pytest.raises(RobotConnectionError):
            await driver.request("must-not-dispatch", {})
        assert api.requests.empty()
    finally:
        await driver.close()
    assert not api.services


async def test_cc_driver_malformed_response_fails_all_pending_requests() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api)
    await driver.start()
    first = asyncio.create_task(driver.request("first", {}))
    second = asyncio.create_task(driver.request("second", {}))
    _, _, respond = await api.next_request()
    await api.next_request()
    try:
        respond(object())
        for pending in (first, second):
            with pytest.raises(RobotResponseError):
                await asyncio.wait_for(pending, 1)
    finally:
        await driver.close()


async def test_cc_driver_drops_late_callbacks_after_close() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api)
    events: list[str] = []
    driver.subscribe_finished(events.append)
    await driver.start()
    pending = asyncio.create_task(driver.request("active", {}))
    _, _, respond = await api.next_request()
    finished = api.services["cc.api.work.finished"]
    disconnected = api.disconnect
    await driver.close()
    with pytest.raises(RobotConnectionError):
        await pending
    respond(Response(result={}))
    finished("late", {"workId": "late"})
    disconnected()
    await checkpoint()
    assert not events


async def test_platform_close_reuses_stop_failure_and_still_releases_driver() -> None:
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform

    api = Api()
    platform = create_cc_api_platform(api, stop_timeout_s=0.02)
    await platform.start()
    skill = unwrap_thread_binding(platform.skills["audio.speak"])
    execution = asyncio.create_task(skill.execute(context(), {"message": "hello"}))
    _, _, respond = await api.next_request()
    respond(Response(result={"workId": "unconfirmed"}))
    closing = asyncio.create_task(platform.close())
    _, _, respond_stop = await api.next_request()
    respond_stop(Response())
    with pytest.raises(RobotStopError) as first:
        await asyncio.wait_for(closing, 1)
    with pytest.raises(RobotStopError) as second:
        await platform.close()
    assert first.value is second.value
    with pytest.raises(RobotStopError):
        await execution
    assert not api.services
    assert api.notifications == ["cc.api.work.subscribe", "cc.api.work.unsubscribe"]


async def test_cancelling_platform_start_waiter_does_not_repeat_startup() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver, create_cc_api_platform

    api = Api()
    platform = create_cc_api_platform(api)
    entered, release = asyncio.Event(), asyncio.Event()
    start = CcApiDriver.start

    async def gated_start(driver: CcApiDriver) -> None:
        entered.set()
        await release.wait()
        await start(driver)

    with patch.object(CcApiDriver, "start", gated_start):
        waiter = asyncio.create_task(platform.start())
        await asyncio.wait_for(entered.wait(), 1)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        release.set()
        await asyncio.wait_for(platform.start(), 1)
    await platform.close()
    assert api.registrations == ["cc.api.work.finished"]


async def test_cancelling_platform_close_waiter_does_not_interrupt_stop_confirmation() -> None:
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform

    api = Api()
    platform = create_cc_api_platform(api)
    await platform.start()
    skill = unwrap_thread_binding(platform.skills["audio.speak"])
    execution = asyncio.create_task(skill.execute(context(), {"message": "hello"}))
    _, _, respond = await api.next_request()
    respond(Response(result={"workId": "active"}))
    waiter = asyncio.create_task(platform.close())
    _, _, respond_stop = await api.next_request()
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert api.services
    respond_stop(Response())
    api.finish("active")
    await asyncio.wait_for(platform.close(), 1)
    with pytest.raises(TaskCancelled):
        await execution
    assert not api.services and api.requests.empty()


async def test_cancelling_dispatched_driver_request_does_not_allow_uncertain_retry() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api)
    failures: list[Exception] = []
    driver.subscribe_failure(failures.append)
    await driver.start()
    pending = asyncio.create_task(driver.request("cc.api.work.start", {}))
    _, _, late_response = await api.next_request()
    try:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        late_response(Response(result={"workId": "unknown-start"}))
        with pytest.raises(RobotConnectionError):
            await driver.request("cc.api.work.start", {})
        assert api.requests.empty()
        assert len(failures) == 1 and isinstance(failures[0], RobotConnectionError)
    finally:
        await driver.close()


async def test_finished_work_id_obeys_snapshot_limits_before_delivery() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiDriver

    api = Api()
    driver = CcApiDriver(api, options=RuntimeOptions(max_snapshot_bytes=32))
    events: list[str] = []
    failures: list[Exception] = []
    driver.subscribe_finished(events.append)
    driver.subscribe_failure(failures.append)
    await driver.start()
    try:
        api.finish("x" * 1000)
        await checkpoint()
        assert not events
        assert len(failures) == 1 and isinstance(failures[0], RobotResponseError)
        with pytest.raises(RobotConnectionError):
            await driver.request("must-not-dispatch", {})
    finally:
        await driver.close()


async def test_cc_platform_explicit_group_preserves_binding_and_starts_owned_skill() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill, create_cc_api_platform

    api = Api()
    platform = create_cc_api_platform(api, skill_thread_group="audio")
    bound = platform.skills["audio.speak"]
    assert isinstance(bound, ThreadBinding) and bound.mode == "group" and bound.group_name == "audio"
    assert isinstance(unwrap_thread_binding(bound), CcApiSpeechSkill)
    owner_names: list[str] = []
    execute = CcApiSpeechSkill.execute

    async def observe(
        skill: CcApiSpeechSkill, ctx: SkillContext, params: dict[str, Any],
    ) -> SkillResult:
        owner_names.append(threading.current_thread().name)
        return await execute(skill, ctx, params)

    await platform.start()
    runtime = RobotRuntime.from_platform(InMemoryTaskRepository(), platform)
    try:
        with patch.object(CcApiSpeechSkill, "execute", observe):
            await runtime.start()
            task = await runtime.submit_task(TaskRequest(task_type="binding", steps=[
                SkillCall(skill="audio.speak", params={"message": "same audio group"}),
            ]))
            _, _, respond = await api.next_request()
            respond(Response(result={"workId": "grouped"}))
            api.finish("grouped")
            async with asyncio.timeout(1):
                async for _ in runtime.subscribe_task_events(task.id):
                    pass
            assert (await runtime.get_task_status(task.id)).state == TaskState.SUCCEEDED
    finally:
        try:
            await runtime.close()
        finally:
            await platform.close()
    assert owner_names == ["robot-extension-group-audio"]
    assert not api.services


def test_cc_platform_rejects_invalid_group_before_sdk_startup() -> None:
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform
    from robot_agent_runtime.errors import InvalidRequest

    for name in ("", "   ", 1, True, []):
        api = Api()
        with pytest.raises(InvalidRequest):
            create_cc_api_platform(api, skill_thread_group=name)
        assert not api.registrations and not api.notifications


async def test_cc_speech_demo_keeps_explicit_audio_thread_group() -> None:
    from robot_agent_runtime.errors import InvalidRequest

    demo = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/cc_api_speech_demo.py"))
    captured: list[str | None] = []

    def capture(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs.get("skill_thread_group"))
        raise InvalidRequest("Stop before connecting SDK")

    with patch.dict(demo["speak"].__globals__, create_cc_api_platform=capture):
        with pytest.raises(InvalidRequest):
            await demo["speak"](Api(), "audio grouping")
    assert captured == ["audio"]


async def test_cc_speech_demo_returns_final_result_with_either_listener_execution() -> None:
    demo = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/cc_api_speech_demo.py"))
    for execution in ("caller", "dedicated"):
        api = Api()
        speaking = asyncio.create_task(demo["speak"](
            api, "hello demo", listener1_execution=execution, listener2_execution=execution,
        ))
        try:
            method, params, respond = await api.next_request()
            assert method == "cc.api.work.start" and params["params"]["text"] == "hello demo"
            respond(Response(result={"workId": "demo-work"}))
            api.finish("demo-work")
            final = await asyncio.wait_for(speaking, 1)
            assert final.state == TaskState.SUCCEEDED
            assert final.result is not None
            assert final.result["steps"][0]["output"]["work_id"] == "demo-work"
            assert not api.services and api.stops == 0
        finally:
            speaking.cancel()
            await asyncio.gather(speaking, return_exceptions=True)


async def test_cc_speech_demo_returns_vendor_failure_in_final_task() -> None:
    demo = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/cc_api_speech_demo.py"))
    api = Api()
    speaking = asyncio.create_task(demo["speak"](api, "rejected"))
    try:
        _, _, respond = await api.next_request()
        respond(Response(False, error_code=17, error_message="not_in_work_mode"))
        final = await asyncio.wait_for(speaking, 1)
        assert final.state == TaskState.FAILED
        assert final.error is not None and final.error.details["vendor_code"] == 17
        assert not api.services and api.stops == 0
    finally:
        speaking.cancel()
        await asyncio.gather(speaking, return_exceptions=True)


async def test_cc_platform_exposes_robot_status_skill() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiRobotInfoSkill, create_cc_api_platform

    api = Api()
    platform = create_cc_api_platform(api)
    assert set(platform.skills) == {"audio.speak", "robot.status"}
    assert isinstance(unwrap_thread_binding(platform.skills["robot.status"]), CcApiRobotInfoSkill)
    await platform.close()


async def test_robot_status_skill_discovers_ids_then_queries_and_returns_payload() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiRobotInfoSkill

    api = Api()
    skill = CcApiRobotInfoSkill(api)
    await skill.start()
    try:
        execution = asyncio.create_task(skill.execute(context(), {}))
        # First it discovers the vendor's real status items…
        method, params, respond = await api.next_request()
        assert method == "cc.api.status.list"
        respond(Response(result={"list": ["state", "work_mode"]}))
        # …then queries those discovered IDs.
        method, params, respond = await api.next_request()
        assert method == "cc.api.status.get"
        assert params["list"] == ["state", "work_mode"]
        respond(Response(result={"state": "idle", "work_mode": "stand"}))
        result = await asyncio.wait_for(execution, 1)
        assert result.success is True
        assert result.output["status"] == {"state": "idle", "work_mode": "stand"}
        assert result.output["status_ids"] == ["state", "work_mode"]
        # A read-only query reports no physical execution and is at rest.
        assert result.action is not None and result.action.executed == "none" and result.action.at_rest
    finally:
        await skill.close()


async def test_robot_status_skill_accepts_explicit_ids() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiRobotInfoSkill

    api = Api()
    skill = CcApiRobotInfoSkill(api)
    await skill.start()
    try:
        execution = asyncio.create_task(skill.execute(context(), {"status_ids": ["state", "leg_status"]}))
        _, params, respond = await api.next_request()
        assert params["list"] == ["state", "leg_status"]
        respond(Response(result={"state": "walking", "leg_status": "STAND"}))
        result = await asyncio.wait_for(execution, 1)
        assert result.success and result.output["status_ids"] == ["state", "leg_status"]
    finally:
        await skill.close()


async def test_robot_status_skill_rejects_invalid_ids() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiRobotInfoSkill
    from robot_agent_runtime.errors import InvalidRequest

    api = Api()
    skill = CcApiRobotInfoSkill(api)
    await skill.start()
    try:
        for bad in ("state", 123, ["state", ""], [1, 2]):
            with pytest.raises(InvalidRequest):
                await skill.execute(context(), {"status_ids": bad})
        assert api.requests.empty()
    finally:
        await skill.close()


async def test_robot_status_skill_propagates_vendor_rejection() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiRobotInfoSkill
    from robot_agent_runtime.errors import RobotRequestError

    api = Api()
    skill = CcApiRobotInfoSkill(api)
    await skill.start()
    try:
        execution = asyncio.create_task(skill.execute(context(), {}))
        _, _, respond = await api.next_request()
        respond(Response(False, error_code=5, error_message="status unavailable"))
        with pytest.raises(RobotRequestError) as caught:
            await asyncio.wait_for(execution, 1)
        assert caught.value.info.details["vendor_code"] == 5
    finally:
        await skill.close()


async def test_robot_status_skill_unavailable_before_start_and_after_close() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiRobotInfoSkill
    from robot_agent_runtime.errors import RobotConnectionError

    api = Api()
    skill = CcApiRobotInfoSkill(api)
    with pytest.raises(RobotConnectionError):
        await skill.execute(context(), {})
    await skill.start()
    await skill.close()
    with pytest.raises(RobotConnectionError):
        await skill.execute(context(), {})


async def test_platform_end_to_end_robot_status_via_runtime() -> None:
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform

    api = Api()
    platform = create_cc_api_platform(api)
    await platform.start()
    runtime = RobotRuntime.from_platform(InMemoryTaskRepository(), platform)
    try:
        await runtime.start()
        task = await runtime.submit_task(TaskRequest(task_type="status", steps=[
            SkillCall(skill="robot.status", params={"status_ids": ["state"]}),
        ]))
        method, params, respond = await api.next_request()
        assert method == "cc.api.status.get" and params["list"] == ["state"]
        respond(Response(result={"state": "idle"}))
        async with asyncio.timeout(1):
            async for _ in runtime.subscribe_task_events(task.id):
                pass
        assert (await runtime.get_task_status(task.id)).state == TaskState.SUCCEEDED
    finally:
        try:
            await runtime.close()
        finally:
            await platform.close()
