import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import SkillCall, SkillContext, TaskRequest, TaskState
from robot_agent_runtime.sdk import RemoteRuntime
from robot_agent_runtime.ports import CallbackApiResponse, CallbackRobotApi, PagedEventRepository, Skill


async def check_paged_event_repository_contract(repository: PagedEventRepository) -> None:
    from robot_agent_runtime.errors import InvalidRequest
    from robot_agent_runtime.models import Event, SkillCall, Task

    await repository.start()
    try:
        await repository.create_task(Task(id="paged", task_type="contract",
                                          state=TaskState.CREATED, steps=[SkillCall(skill="none")]))
        for sequence in (3, 1, 2):
            await repository.append_event(Event(event_id=f"page-{sequence}", task_id="paged",
                                                sequence=sequence, event_type="progress"))
        page = await repository.list_events_page("paged", 0, 2)
        assert [event.sequence for event in page] == [1, 2]
        page[0].data["changed"] = True
        assert (await repository.list_events_page("paged", 0, 1))[0].data == {}
        assert [event.sequence for event in await repository.list_events_page("paged", 2, 2)] == [3]
        assert await repository.list_events_page("absent", 0, 2) == []
        for limit in (0, -1, True):
            with pytest.raises(InvalidRequest):
                await repository.list_events_page("paged", 0, limit)
    finally:
        await repository.close()


async def test_memory_paged_event_repository_contract() -> None:
    from robot_agent_runtime.repository import InMemoryTaskRepository

    await check_paged_event_repository_contract(InMemoryTaskRepository())


async def test_sqlite_paged_event_repository_contract() -> None:
    from robot_agent_runtime.sqlite_repository import SQLiteTaskRepository

    await check_paged_event_repository_contract(SQLiteTaskRepository(":memory:"))


async def test_simulator_cancellation_acknowledges_actual_completion() -> None:
    from robot_agent_runtime.errors import TaskCancelled

    skill = Simulator().skills["wait.delay"]
    started = asyncio.Event()
    context = SkillContext("cancel", "cancel", asyncio.Event(), lambda _: started.set())
    execution = asyncio.create_task(skill.execute(context, {"delay_s": 30}))
    await asyncio.wait_for(started.wait(), 1)
    execution.cancel()
    with pytest.raises(TaskCancelled):
        await execution
    assert skill.active == 0


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SkillAdapterContract:
    """Reusable behavioral checks for future Walker and ROS 2 skill adapters."""

    def create_skills(self) -> dict[str, object]:
        raise NotImplementedError

    def successful_params(self) -> dict[str, dict]:
        raise NotImplementedError

    def test_skill_registry_and_schemas_are_valid(self) -> None:
        skills = self.create_skills()
        assert skills
        assert set(skills) == set(self.successful_params())
        for name, skill in skills.items():
            definition = skill.definition
            assert definition.name == name
            assert definition.version
            jsonschema.validators.validator_for(definition.input_schema).check_schema(
                definition.input_schema
            )
            assert definition.timeout_s > 0

    async def test_skills_execute_through_the_public_port(self) -> None:
        skills = self.create_skills()
        for name, params in self.successful_params().items():
            jsonschema.validate(params, skills[name].definition.input_schema)
            context = SkillContext(
                task_id=f"contract-{name}",
                execution_id="contract-execution",
                cancel_event=asyncio.Event(),
            )
            result = await skills[name].execute(context, params)
            assert result.success, result.error
            assert result.error is None


class TestSimulatorSkillContract(SkillAdapterContract):
    def create_skills(self) -> dict[str, object]:
        return Simulator().skills

    def successful_params(self) -> dict[str, dict]:
        return {
            "navigation.navigate_to": {"target": "contract-dock"},
            "audio.speak": {"message": "contract complete"},
            "robot.get_state": {},
            "wait.delay": {"delay_s": 0},
        }


@dataclass
class ContractApiResponse:
    is_success: bool = True
    result: Any = None
    error_code: int = 0
    error_message: str = ""


class ContractCallbackApi:
    """A structural callback port implementation without a vendor SDK."""

    def __init__(self) -> None:
        self.services: dict[str, Callable[[Any, Any], None]] = {}
        self.notifications: list[str] = []
        self.disconnect: Callable[[], None] = lambda: None

    def request(
        self, method: str, params: dict[str, Any],
        callback: Callable[[CallbackApiResponse], None],
    ) -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon(callback, ContractApiResponse(result={"workId": "contract"}))
        loop.call_soon(self.services["cc.api.work.finished"], "event", {"workId": "contract"})

    def notify(self, method: str) -> None:
        self.notifications.append(method)

    def add_service(self, name: str, callback: Callable[[Any, Any], None]) -> None:
        self.services[name] = callback

    def remove_service(self, name: str) -> None:
        self.services.pop(name)

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        self.disconnect = callback


async def check_callback_api_speech_contract(api: CallbackRobotApi) -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill

    adapter = CcApiSpeechSkill(api)
    skill: Skill = adapter
    definition = skill.definition
    assert definition.resources == {"speaker"}
    assert definition.permissions == {"audio"}
    assert definition.preconditions == set()
    assert definition.supports_cancel is True
    jsonschema.validators.validator_for(definition.input_schema).check_schema(definition.input_schema)
    await adapter.start()
    try:
        result = await asyncio.wait_for(skill.execute(
            SkillContext("contract", "contract", asyncio.Event()), {"message": "contract"},
        ), 1)
        assert result.success
        json.dumps(result.model_dump())
    finally:
        await adapter.close()


async def test_cc_api_callback_port_and_speech_skill_contract() -> None:
    api = ContractCallbackApi()
    await check_callback_api_speech_contract(api)
    assert not api.services
    assert api.notifications == ["cc.api.work.subscribe", "cc.api.work.unsubscribe"]


@pytest.mark.parametrize("example_name", ["embedded_demo.py", "remote_demo.py"])
async def test_examples_emit_ordered_success_results(example_name: str) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROJECT_ROOT / "examples" / example_name),
        cwd=PROJECT_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == 0, stderr.decode()
    payload = json.loads(stdout)
    assert payload["state"] == TaskState.SUCCEEDED.value
    assert payload["events"]
    assert payload["events"] == list(range(1, len(payload["events"]) + 1))
    assert payload["result"]["steps"][0]["output"]["target"] == "meeting_room"


async def test_cli_daemon_runs_remote_task_and_cleans_up_on_sigterm(tmp_path: Path) -> None:
    socket_path = str(tmp_path / "runtime.sock")
    database_path = str(tmp_path / "runtime.db")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "robot_agent_runtime",
        "serve",
        "--socket",
        socket_path,
        "--database",
        database_path,
        cwd=PROJECT_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    remote = RemoteRuntime(socket_path)
    try:
        async with asyncio.timeout(5):
            while True:
                try:
                    await remote.start()
                    break
                except Exception:
                    if process.returncode is not None:
                        stderr = await process.stderr.read()
                        raise AssertionError(stderr.decode())
                    await asyncio.sleep(0.02)

        task = await remote.submit_task(
            TaskRequest(
                task_type="cli-contract",
                steps=[SkillCall(skill="robot.get_state")],
                idempotency_key="cli-contract-1",
            )
        )
        events = []
        async with asyncio.timeout(2):
            async for event in remote.subscribe_task_events(task.id):
                events.append(event)
        final = await remote.get_task_status(task.id)
        assert final.state == TaskState.SUCCEEDED
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert Path(database_path).is_file()
    finally:
        await remote.close()
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)

    assert process.returncode == 0, stderr.decode()
    assert not os.path.lexists(socket_path)
