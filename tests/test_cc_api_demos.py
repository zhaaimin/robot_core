import asyncio
import io
import json
import os
import runpy
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import RobotConnectionError, SkillExecutionError
from robot_agent_runtime.models import ErrorInfo, SkillCall, Task, TaskState
from robot_agent_runtime.sdk import RobotRuntime


NEW_DEMOS = ("cc_api_speech_minimal_demo.py", "cc_api_speech_callback_demo.py")
LEGACY_DEMO = "cc_api_speech_demo.py"


def load_demo(name: str) -> dict[str, Any]:
    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples" / name))


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
        self.disconnect: Callable[[], None] = lambda: None
        self.stops = 0

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

    def finish(self, work_id: str) -> None:
        self.services["cc.api.work.finished"]("event", {"workId": work_id})

    def stop(self) -> None:
        self.stops += 1

    async def next_request(self) -> tuple[str, dict[str, Any], Callable[..., None]]:
        return await asyncio.wait_for(self.requests.get(), 1)


async def run_speech(name: str, *, rejection: bool = False) -> tuple[Task, str]:
    demo = load_demo(name)
    api = Api()
    output = io.StringIO()
    with redirect_stdout(output):
        speaking = asyncio.create_task(demo["speak"](api, "hello"))
        try:
            method, params, respond = await api.next_request()
            assert method == "cc.api.work.start" and params["params"]["text"] == "hello"
            if rejection:
                respond(Response(False, error_code=17, error_message="not_in_work_mode"))
            else:
                respond(Response(result={"workId": "speech"}))
                checkpoint = asyncio.Event()
                asyncio.get_running_loop().call_soon(checkpoint.set)
                await checkpoint.wait()
                assert not speaking.done()
                api.finish("speech")
            final = await asyncio.wait_for(speaking, 1)
            assert not api.services and api.notifications[-1] == "cc.api.work.unsubscribe"
            assert api.stops == 0
            return final, output.getvalue()
        finally:
            speaking.cancel()
            await asyncio.gather(speaking, return_exceptions=True)


async def test_minimal_demo_waits_for_finished_and_returns_task_without_callback_output() -> None:
    final, output = await run_speech(NEW_DEMOS[0])
    assert final.state == TaskState.SUCCEEDED and final.result is not None
    assert final.result["steps"][0]["output"] == {
        "message": "hello", "spoken": True, "work_id": "speech",
    }
    assert output == ""


async def test_callback_demo_delivers_ordered_events_and_final_callback_before_return() -> None:
    final, output = await run_speech(NEW_DEMOS[1])
    events = [json.loads(line.removeprefix("[EVENT] "))
              for line in output.splitlines() if line.startswith("[EVENT] ")]
    completed = [json.loads(line.removeprefix("[DONE] "))
                 for line in output.splitlines() if line.startswith("[DONE] ")]
    assert final.state == TaskState.SUCCEEDED
    assert events[0]["event_type"] == "TASK_CREATED"
    assert events[-1]["event_type"] == "TASK_SUCCEEDED"
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert len(completed) == 1 and completed[0]["id"] == final.id
    assert completed[0]["state"] == TaskState.SUCCEEDED.value
    assert "[LISTENER ERROR]" not in output


async def test_new_speech_demos_return_vendor_rejection_as_a_task() -> None:
    for name in NEW_DEMOS:
        final, output = await run_speech(name, rejection=True)
        assert final.state == TaskState.FAILED and final.error is not None
        assert final.error.code == "ROBOT_REQUEST_FAILED"
        assert final.error.details["vendor_code"] == 17
        if name == NEW_DEMOS[1]:
            assert '"event_type":"TASK_FAILED"' in output
            assert '"state":"failed"' in output and "[DONE]" in output


async def test_new_speech_demos_return_validation_failure_without_dispatch() -> None:
    for name in NEW_DEMOS:
        demo = load_demo(name)
        api = Api()
        output = io.StringIO()
        with redirect_stdout(output):
            final = await asyncio.wait_for(demo["speak"](api, ""), 1)
        assert final.state == TaskState.FAILED and final.error is not None
        assert final.error.code == "SCHEMA_VALIDATION_FAILED"
        assert api.requests.empty() and not api.services and api.stops == 0
        if name == NEW_DEMOS[1]:
            assert "[EVENT]" in output.getvalue() and "[DONE]" in output.getvalue()


async def test_callback_demo_raises_listener_failure_after_releasing_platform() -> None:
    demo = load_demo(NEW_DEMOS[1])
    api = Api()
    error = ValueError("display failed")
    original_add = RobotRuntime.add_task_listener

    async def fail_event(event: Any) -> None:
        raise error

    def add_failing_listener(runtime: RobotRuntime, **callbacks: Any) -> Any:
        callbacks["on_event"] = fail_event
        return original_add(runtime, **callbacks)

    output = io.StringIO()
    with patch.object(RobotRuntime, "add_task_listener", add_failing_listener):
        with redirect_stdout(output):
            with pytest.raises(ValueError) as caught:
                await asyncio.wait_for(demo["speak"](api, ""), 1)
    assert caught.value is error
    assert "[LISTENER ERROR]" in output.getvalue()
    assert not api.services and api.stops == 0


async def test_new_speech_demos_close_platform_when_startup_fails() -> None:
    for name in NEW_DEMOS:
        demo = load_demo(name)
        error = RobotConnectionError("startup failed")
        closed: list[str] = []

        class Platform:
            skills = Simulator().skills
            robot_state = None
            reconciler = None

            async def start(self) -> None:
                raise error

            async def close(self) -> None:
                closed.append("platform")

        def create(api: Any, *, skill_thread_group: str) -> Platform:
            assert skill_thread_group == "audio"
            return Platform()

        with patch.dict(demo["speak"].__globals__, create_cc_api_platform=create):
            with pytest.raises(RobotConnectionError) as caught:
                await demo["speak"](Api(), "hello")
        assert caught.value is error and closed == ["platform"]


class NativeApi:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.calls: list[str] = []
        self.config: Any = None

    def initialize(self, config: Any) -> None:
        self.config = config
        self.calls.append("initialize")

    def start(self) -> None:
        self.calls.append("start")

    def wait_until_connected(self, timeout_ms: int) -> bool:
        assert timeout_ms == 5000
        self.calls.append("connect")
        return self.connected

    def stop(self) -> None:
        self.calls.append("stop")


def cli_task(*, failed: bool = False) -> Task:
    return Task(
        id="cli-task", task_type="announcement",
        state=TaskState.FAILED if failed else TaskState.SUCCEEDED,
        steps=[SkillCall(skill="audio.speak", params={"message": "hello"})],
        result=None if failed else {"spoken": True},
        error=ErrorInfo(code="ROBOT_REQUEST_FAILED", message="rejected") if failed else None,
    )


def test_new_speech_demo_clis_configure_and_release_application_connection() -> None:
    for name in NEW_DEMOS:
        demo = load_demo(name)
        api = NativeApi()

        async def speak(client: Any, message: str) -> Task:
            assert client is api and message == "hello"
            api.calls.append("speak")
            return cli_task()

        vendor = SimpleNamespace(Config=SimpleNamespace, Api=lambda: api)
        output = io.StringIO()
        with patch.dict(sys.modules, ubt_robot=vendor):
            with patch.dict(os.environ, CC_API_TOKEN="example-token"):
                with patch.dict(demo["main"].__globals__, speak=speak):
                    with redirect_stdout(output):
                        demo["main"]([
                            "--api-id", "example", "--address", "robot:48159", "--message", "hello",
                        ])
        assert api.calls == ["initialize", "start", "connect", "speak", "stop"]
        assert api.config.api_id == "example" and api.config.address == "robot:48159"
        assert api.config.token == "example-token" and not api.config.is_reconnect
        assert "[RESULT]" in output.getvalue()


def test_new_speech_demo_clis_stop_connection_on_task_failure_or_exception() -> None:
    for name in NEW_DEMOS:
        for exception in (False, True):
            demo = load_demo(name)
            api = NativeApi()
            error = RobotConnectionError("disconnected")

            async def speak(client: Any, message: str) -> Task:
                if exception:
                    raise error
                return cli_task(failed=True)

            vendor = SimpleNamespace(Config=SimpleNamespace, Api=lambda: api)
            with patch.dict(sys.modules, ubt_robot=vendor):
                with patch.dict(demo["main"].__globals__, speak=speak):
                    if exception:
                        with pytest.raises(RobotConnectionError) as caught:
                            demo["main"](["--api-id", "example", "--message", "hello"])
                        assert caught.value is error
                    else:
                        with pytest.raises(SkillExecutionError) as caught:
                            demo["main"](["--api-id", "example", "--message", "hello"])
                        assert caught.value.info.details["task_id"] == "cli-task"
            assert api.calls == ["initialize", "start", "connect", "stop"]


def test_new_speech_demo_clis_stop_connection_when_connecting_fails() -> None:
    for name in NEW_DEMOS:
        demo = load_demo(name)
        api = NativeApi(connected=False)
        vendor = SimpleNamespace(Config=SimpleNamespace, Api=lambda: api)
        with patch.dict(sys.modules, ubt_robot=vendor):
            with pytest.raises(RobotConnectionError):
                demo["main"](["--api-id", "example", "--message", "hello"])
        assert api.calls == ["initialize", "start", "connect", "stop"]


def test_speech_demo_help_separates_basic_examples_from_thread_experiment() -> None:
    for name in (*NEW_DEMOS, LEGACY_DEMO):
        with patch.dict(sys.modules, ubt_robot=None):
            demo = load_demo(name)
            output = io.StringIO()
            with redirect_stdout(output):
                with pytest.raises(SystemExit) as caught:
                    demo["main"](["--help"])
        assert caught.value.code == 0
        help_text = output.getvalue()
        assert "--api-id" in help_text and "--message" in help_text
        assert ("--listener-block" in help_text) == (name == LEGACY_DEMO)
