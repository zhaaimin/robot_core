import asyncio
import runpy
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from robot_agent_runtime.models import SkillCall, SkillContext, Task, TaskRequest, TaskState
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


@dataclass
class Response:
    is_success: bool = True
    result: Any = None
    error_code: int = 0
    error_message: str = ""


class FakeApi:
    def __init__(self) -> None:
        self.services: dict[str, Callable[..., None]] = {}
        self.requests: asyncio.Queue[tuple[str, dict[str, Any], Callable[..., None]]] = asyncio.Queue()
        self.notifications: list[str] = []
        self.disconnect: Callable[[], None] = lambda: None

    def request(self, method: str, params: dict[str, Any], callback: Callable[..., None]) -> None:
        self.requests.put_nowait((method, params, callback))

    def notify(self, method: str) -> None:
        self.notifications.append(method)

    def add_service(self, name: str, callback: Callable[..., None]) -> None:
        self.services[name] = callback

    def remove_service(self, name: str) -> None:
        self.services.pop(name, None)

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        self.disconnect = callback

    def finish(self, work_id: str, **data: Any) -> None:
        self.services["cc.api.work.finished"]("notification", {"workId": work_id, **data})

    async def next_request(self) -> tuple[str, dict[str, Any], Callable[..., None]]:
        return await asyncio.wait_for(self.requests.get(), 1)


def make_context() -> SkillContext:
    return SkillContext("task", "execution", asyncio.Event())


async def checkpoint() -> None:
    event = asyncio.Event()
    asyncio.get_running_loop().call_soon(event.set)
    await event.wait()


@asynccontextmanager
async def running_skill(**options: Any) -> Any:
    from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill

    api = FakeApi()
    skill = CcApiSpeechSkill(api, **options)
    await skill.start()
    try:
        yield api, skill
    finally:
        await skill.close()


async def test_cc_api_waits_for_matching_finished_and_maps_message() -> None:
    async with running_skill() as (api, skill):
        execution = asyncio.create_task(skill.execute(make_context(), {"message": "你好"}))
        method, params, respond = await api.next_request()
        assert method == "cc.api.work.start"
        assert params["skillCode"] == "A000001"
        assert params["params"] == {"text": "你好", "speed": 50, "volume": 50}
        assert params["traceId"]
        respond(Response(result={"workId": "speech-1"}))
        api.finish("other")
        await checkpoint()
        assert not execution.done()
        api.finish("speech-1", type="anything")
        result = await asyncio.wait_for(execution, 1)
        assert result.success
        assert result.output["work_id"] == "speech-1"
        assert result.output["spoken"] is True
        assert result.output["message"] == "你好"
        api.finish("speech-1")
        await checkpoint()


async def test_cc_api_handles_finished_before_launch_response() -> None:
    async with running_skill() as (api, skill):
        execution = asyncio.create_task(skill.execute(make_context(), {"message": "短句"}))
        _, _, respond = await api.next_request()
        api.finish("fast")
        respond(Response(result={"workId": "fast"}))
        assert (await asyncio.wait_for(execution, 1)).success


async def test_cc_api_start_rejection_preserves_vendor_error_and_allows_retry() -> None:
    from robot_agent_runtime.errors import RobotRequestError

    async with running_skill() as (api, skill):
        execution = asyncio.create_task(skill.execute(make_context(), {"message": "first"}))
        _, _, respond = await api.next_request()
        respond(Response(False, error_code=42, error_message="speaker unavailable"))
        with pytest.raises(RobotRequestError) as caught:
            await execution
        assert caught.value.info.details["vendor_code"] == 42
        execution = asyncio.create_task(skill.execute(make_context(), {"message": "second"}))
        _, _, respond = await api.next_request()
        respond(Response(result={"workId": "retry"}))
        api.finish("retry")
        assert (await execution).success


async def test_cc_api_missing_work_id_fails_and_blocks_more_speech() -> None:
    from robot_agent_runtime.errors import RobotConnectionError, RobotStopError

    async with running_skill() as (api, skill):
        execution = asyncio.create_task(skill.execute(make_context(), {"message": "hello"}))
        _, _, respond = await api.next_request()
        respond(Response(result={}))
        with pytest.raises(RobotStopError):
            await execution
        with pytest.raises(RobotConnectionError):
            await skill.execute(make_context(), {"message": "again"})
        assert api.requests.empty()


async def test_cc_api_cancel_waits_for_finished_after_exact_stop() -> None:
    from robot_agent_runtime.errors import TaskCancelled

    async with running_skill() as (api, skill):
        context = make_context()
        execution = asyncio.create_task(skill.execute(context, {"message": "hello"}))
        _, _, respond = await api.next_request()
        respond(Response(result={"workId": "cancel-me"}))
        context.cancel_event.set()
        method, params, respond_stop = await api.next_request()
        assert method == "cc.api.work.ctrl"
        assert params == {"workId": "cancel-me", "workCtrl": "stop"}
        respond_stop(Response(result={}))
        await checkpoint()
        assert not execution.done()
        api.finish("cancel-me")
        with pytest.raises(TaskCancelled):
            await asyncio.wait_for(execution, 1)


async def test_cc_api_cancel_during_launch_stops_late_started_work() -> None:
    from robot_agent_runtime.errors import TaskCancelled

    async with running_skill() as (api, skill):
        context = make_context()
        execution = asyncio.create_task(skill.execute(context, {"message": "hello"}))
        _, _, respond = await api.next_request()
        context.cancel_event.set()
        await checkpoint()
        assert not execution.done()
        respond(Response(result={"workId": "late"}))
        _, params, respond_stop = await api.next_request()
        assert params["workId"] == "late"
        respond_stop(Response())
        api.finish("late")
        with pytest.raises(TaskCancelled):
            await asyncio.wait_for(execution, 1)


async def test_cc_api_coroutine_cancellation_stops_before_propagating() -> None:
    from robot_agent_runtime.errors import TaskCancelled
    async with running_skill() as (api, skill):
        execution = asyncio.create_task(skill.execute(make_context(), {"message": "hello"}))
        _, _, respond = await api.next_request()
        respond(Response(result={"workId": "timeout"}))
        execution.cancel()
        _, params, respond_stop = await api.next_request()
        assert params["workId"] == "timeout"
        respond_stop(Response())
        await checkpoint()
        assert not execution.done()
        api.finish("timeout")
        with pytest.raises(TaskCancelled):
            await asyncio.wait_for(execution, 1)


async def test_cc_api_unconfirmed_stop_fails_and_blocks_new_speech() -> None:
    from robot_agent_runtime.errors import RobotConnectionError, RobotStopError

    async with running_skill(stop_timeout_s=0.01) as (api, skill):
        context = make_context()
        execution = asyncio.create_task(skill.execute(context, {"message": "hello"}))
        _, _, respond = await api.next_request()
        respond(Response(result={"workId": "stuck"}))
        context.cancel_event.set()
        _, _, respond_stop = await api.next_request()
        respond_stop(Response())
        with pytest.raises(RobotStopError):
            await asyncio.wait_for(execution, 1)
        with pytest.raises(RobotConnectionError):
            await skill.execute(make_context(), {"message": "again"})


async def test_cc_api_disconnect_fails_active_speech() -> None:
    from robot_agent_runtime.errors import RobotStopError

    async with running_skill() as (api, skill):
        execution = asyncio.create_task(skill.execute(make_context(), {"message": "hello"}))
        _, _, respond = await api.next_request()
        respond(Response(result={"workId": "lost"}))
        api.disconnect()
        with pytest.raises(RobotStopError):
            await asyncio.wait_for(execution, 1)


async def test_cc_api_runtime_serializes_speaker_and_finishes_on_event() -> None:
    async with running_skill() as (api, skill):
        runtime = RobotRuntime(InMemoryTaskRepository(), {"audio.speak": skill})
        await runtime.start()
        try:
            request = TaskRequest(task_type="speech", steps=[SkillCall(skill="audio.speak", params={"message": "hello"})])
            first = await runtime.submit_task(request)
            _, first_params, respond = await api.next_request()
            respond(Response(result={"workId": "first"}))
            second = await runtime.submit_task(request)
            async with asyncio.timeout(1):
                async for event in runtime.subscribe_task_events(second.id):
                    if event.event_type == "TASK_WAITING":
                        break
            assert api.requests.empty()
            api.finish("first")
            _, second_params, respond = await api.next_request()
            assert first_params["traceId"] != second_params["traceId"]
            respond(Response(result={"workId": "second"}))
            api.finish("second")
            async with asyncio.timeout(1):
                async for _ in runtime.subscribe_task_events(second.id):
                    pass
            assert (await runtime.get_task_status(first.id)).state == TaskState.SUCCEEDED
            assert (await runtime.get_task_status(second.id)).state == TaskState.SUCCEEDED
        finally:
            await runtime.close()


async def test_cc_api_runtime_timeout_stops_work_and_reports_skill_timeout() -> None:
    async with running_skill() as (api, skill):
        runtime = RobotRuntime(InMemoryTaskRepository(), {"audio.speak": skill})
        await runtime.start()
        try:
            task = await runtime.submit_task(TaskRequest(task_type="speech", steps=[
                SkillCall(skill="audio.speak", params={"message": "hello"}, timeout_s=0.02),
            ]))
            _, _, respond = await api.next_request()
            respond(Response(result={"workId": "timed-out"}))
            _, params, respond_stop = await api.next_request()
            assert params["workId"] == "timed-out"
            respond_stop(Response())
            api.finish("timed-out")
            async with asyncio.timeout(1):
                async for _ in runtime.subscribe_task_events(task.id):
                    pass
            final = await runtime.get_task_status(task.id)
            assert final.state == TaskState.FAILED
            assert final.error.code == "SKILL_TIMEOUT"
        finally:
            await runtime.close()


async def test_cc_api_subscription_lifecycle_and_late_callbacks() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill

    api = FakeApi()
    skill = CcApiSpeechSkill(api)
    await skill.start()
    await skill.start()
    callback = api.services["cc.api.work.finished"]
    assert api.notifications == ["cc.api.work.subscribe"]
    await skill.close()
    await skill.close()
    assert api.notifications == ["cc.api.work.subscribe", "cc.api.work.unsubscribe"]
    assert not api.services
    callback("late", {"workId": "late"})
    await checkpoint()


async def test_cc_api_disconnect_before_dispatch_prevents_start_request() -> None:
    from robot_agent_runtime.errors import RobotConnectionError

    async with running_skill() as (api, skill):
        api.disconnect()
        with pytest.raises(RobotConnectionError):
            await asyncio.wait_for(
                skill.execute(make_context(), {"message": "must not dispatch"}), 1
            )
        assert api.requests.empty()


async def test_cc_api_rejected_stop_does_not_acknowledge_cancellation() -> None:
    from robot_agent_runtime.errors import RobotStopError

    async with running_skill() as (api, skill):
        context = make_context()
        execution = asyncio.create_task(skill.execute(context, {"message": "hello"}))
        _, _, respond = await api.next_request()
        respond(Response(result={"workId": "speech"}))
        context.cancel_event.set()
        _, _, respond_stop = await api.next_request()
        respond_stop(Response(False, error_code=10, error_message="stop refused"))
        with pytest.raises(RobotStopError):
            await execution


async def test_cc_api_cancellation_before_dispatch_sends_nothing() -> None:
    from robot_agent_runtime.errors import TaskCancelled

    async with running_skill() as (api, skill):
        context = make_context()
        context.cancel_event.set()
        with pytest.raises(TaskCancelled):
            await skill.execute(context, {"message": "hello"})
        assert api.requests.empty()


async def test_cc_api_close_stops_active_work_before_unsubscribing() -> None:
    async with running_skill() as (api, skill):
        execution = asyncio.create_task(skill.execute(make_context(), {"message": "hello"}))
        _, _, respond = await api.next_request()
        respond(Response(result={"workId": "closing"}))
        closing = asyncio.create_task(skill.close())
        _, _, respond_stop = await api.next_request()
        assert "cc.api.work.unsubscribe" not in api.notifications
        respond_stop(Response())
        api.finish("closing")
        await asyncio.wait_for(closing, 1)
        from robot_agent_runtime.errors import TaskCancelled
        with pytest.raises(TaskCancelled):
            await execution
        assert "cc.api.work.unsubscribe" in api.notifications


async def test_cc_api_close_propagates_unconfirmed_stop_after_detaching() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill
    from robot_agent_runtime.errors import RobotStopError

    api = FakeApi()
    skill = CcApiSpeechSkill(api, stop_timeout_s=0.01)
    await skill.start()
    execution = asyncio.create_task(skill.execute(make_context(), {"message": "hello"}))
    _, _, respond = await api.next_request()
    respond(Response(result={"workId": "uncertain-close"}))
    closing = asyncio.create_task(skill.close())
    _, _, respond_stop = await api.next_request()
    respond_stop(Response())
    with pytest.raises(RobotStopError):
        await asyncio.wait_for(closing, 1)
    with pytest.raises(RobotStopError):
        await execution
    assert "cc.api.work.unsubscribe" in api.notifications
    assert not api.services


async def test_cc_api_close_aggregates_stop_and_detach_errors() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill
    from robot_agent_runtime.errors import RobotConnectionError, RobotStopError

    class DetachFailure(FakeApi):
        def notify(self, method: str) -> None:
            if method == "cc.api.work.unsubscribe":
                raise ValueError("Transport unavailable")
            super().notify(method)

    api = DetachFailure()
    skill = CcApiSpeechSkill(api, stop_timeout_s=0.01)
    await skill.start()
    execution = asyncio.create_task(skill.execute(make_context(), {"message": "hello"}))
    _, _, respond = await api.next_request()
    respond(Response(result={"workId": "uncertain-detach"}))
    closing = asyncio.create_task(skill.close())
    _, _, respond_stop = await api.next_request()
    respond_stop(Response())
    with pytest.raises(ExceptionGroup) as caught:
        await asyncio.wait_for(closing, 1)
    assert any(isinstance(error, RobotStopError) for error in caught.value.exceptions)
    assert any(isinstance(error, RobotConnectionError) for error in caught.value.exceptions)
    with pytest.raises(RobotStopError):
        await execution
    assert not api.services


async def test_cc_api_early_event_overflow_fails_closed() -> None:
    from robot_agent_runtime.errors import RobotConnectionError, RobotStopError

    async with running_skill() as (api, skill):
        execution = asyncio.create_task(skill.execute(make_context(), {"message": "hello"}))
        await api.next_request()
        for index in range(129):
            api.finish(str(index))
        with pytest.raises(RobotStopError):
            await asyncio.wait_for(execution, 1)
        with pytest.raises(RobotConnectionError):
            await skill.execute(make_context(), {"message": "again"})


async def test_cc_api_runtime_invalid_message_does_not_dispatch() -> None:
    async with running_skill() as (api, skill):
        runtime = RobotRuntime(InMemoryTaskRepository(), {"audio.speak": skill})
        await runtime.start()
        try:
            task = await runtime.submit_task(TaskRequest(task_type="speech", steps=[
                SkillCall(skill="audio.speak", params={"message": ""}),
            ]))
            assert task.state == TaskState.FAILED
            assert task.error.code == "SCHEMA_VALIDATION_FAILED"
            assert api.requests.empty()
        finally:
            await runtime.close()


async def test_speech_demo_returns_completed_task_after_finished() -> None:
    demo = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/cc_api_speech_demo.py"))
    api = FakeApi()
    execution = asyncio.create_task(demo["speak"](api, "你好"))
    try:
        _, _, respond = await api.next_request()
        respond(Response(result={"workId": "demo-speech"}))
        await checkpoint()
        assert not execution.done()
        api.finish("demo-speech")
        final = await asyncio.wait_for(execution, 1)
        assert isinstance(final, Task)
        assert final.id
        assert final.state == TaskState.SUCCEEDED
        assert final.error is None
        assert final.result["steps"][0]["output"] == {
            "message": "你好", "spoken": True, "work_id": "demo-speech",
        }
        assert not api.services
        assert api.notifications[-1] == "cc.api.work.unsubscribe"
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


async def test_speech_demo_returns_failed_task_with_vendor_error() -> None:
    demo = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/cc_api_speech_demo.py"))
    api = FakeApi()
    execution = asyncio.create_task(demo["speak"](api, "你好"))
    try:
        _, _, respond = await api.next_request()
        respond(Response(False, error_code=10003, error_message="not_in_work_mode"))
        final = await asyncio.wait_for(execution, 1)
        assert isinstance(final, Task)
        assert final.state == TaskState.FAILED
        assert final.result is None
        assert final.error.code == "ROBOT_REQUEST_FAILED"
        assert final.error.message == "not_in_work_mode"
        assert final.error.details["vendor_code"] == 10003
        assert not api.services
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


async def test_speech_demo_returns_validation_failure_without_dispatch() -> None:
    demo = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/cc_api_speech_demo.py"))
    api = FakeApi()
    final = await asyncio.wait_for(demo["speak"](api, ""), 1)
    assert isinstance(final, Task)
    assert final.state == TaskState.FAILED
    assert final.error.code == "SCHEMA_VALIDATION_FAILED"
    assert api.requests.empty()
    assert not api.services


async def test_cc_api_disconnect_quarantines_speaker_for_another_skill() -> None:
    from robot_agent_runtime.models import SkillDefinition, SkillResult

    class OtherSpeakerSkill:
        definition = SkillDefinition(
            name="audio.other", version="1", input_schema={"type": "object"},
            resources={"speaker"}, permissions=set(), preconditions=set(),
            timeout_s=1, supports_cancel=True,
        )
        calls = 0

        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            self.calls += 1
            return SkillResult(success=True)

    async with running_skill() as (api, skill):
        other = OtherSpeakerSkill()
        runtime = RobotRuntime(InMemoryTaskRepository(), {
            "audio.speak": skill, "audio.other": other,
        })
        await runtime.start()
        try:
            first = await runtime.submit_task(TaskRequest(task_type="speech", steps=[
                SkillCall(skill="audio.speak", params={"message": "lost connection"}),
            ]))
            _, _, respond = await api.next_request()
            respond(Response(result={"workId": "still-speaking"}))
            api.disconnect()
            async with asyncio.timeout(1):
                async for _ in runtime.subscribe_task_events(first.id):
                    pass
            first_final = await runtime.get_task_status(first.id)
            assert first_final.state == TaskState.RECONCILIATION_REQUIRED
            second = await runtime.submit_task(TaskRequest(task_type="other", steps=[
                SkillCall(skill="audio.other"),
            ]))
            async with asyncio.timeout(1):
                async for _ in runtime.subscribe_task_events(second.id):
                    pass
            second_final = await runtime.get_task_status(second.id)
            assert second_final.error is not None
            assert second_final.error.code == "RESOURCE_QUARANTINED"
            assert other.calls == 0
        finally:
            await runtime.close()
