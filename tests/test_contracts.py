import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.adapters.cc_api.contracts import CcApiClient, CcApiResponse
from robot_agent_runtime.models import SkillCall, SkillContext, TaskRequest, TaskState
from robot_agent_runtime.sdk import RemoteRuntime
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.thread_binding import ThreadBinding, unwrap_thread_binding
from robot_agent_runtime.ports import (
    AdmissionValidator, PagedEventRepository, PlatformEventRepository, PlatformEventSource, RobotPlatform, Skill,
)


async def check_platform_event_repository_contract(repository: PlatformEventRepository) -> None:
    """Reusable journal ownership, immutable identity and paging contract."""
    from robot_agent_runtime.models import PlatformEvent

    first = PlatformEvent(event_id="contract-first", source="vendor-neutral", event_type="device.changed", data={"values": [1]})
    await repository.start()
    try:
        saved = await repository.append_platform_event(first)
        assert saved.cursor == 1 and first.cursor == 0
        assert (await repository.append_platform_event(first)).cursor == 1
        saved.data["values"].append(99)
        page = await repository.list_platform_events(0, 1)
        assert len(page) == 1 and page[0].data == {"values": [1]}
        second = PlatformEvent(event_id="contract-second", source="other-vendor", event_type="vendor.detail")
        assert (await repository.append_platform_event(second)).cursor == 2
        assert [event.cursor for event in await repository.list_platform_events(1, 10)] == [2]
        assert await repository.platform_event_bounds() == (1, 2)
    finally:
        await repository.close()
        await repository.close()


async def check_platform_event_source_contract(source: PlatformEventSource, count: int) -> None:
    """Sources expose facts without task IDs, cursors or transport assumptions."""
    from contextlib import aclosing
    from robot_agent_runtime.models import PlatformEvent

    seen: set[str] = set()
    async with aclosing(source.watch()) as stream:
        async with asyncio.timeout(1):
            for _ in range(count):
                event = await anext(stream)
                assert type(event) is PlatformEvent and event.cursor == 0
                assert event.source and event.event_type and event.event_id not in seen
                json.loads(event.model_dump_json())
                seen.add(event.event_id)


async def test_platform_event_memory_repository_reusable_contract() -> None:
    from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository

    await check_platform_event_repository_contract(InMemoryPlatformEventRepository())


async def test_non_callback_platform_event_sources_share_the_same_contract() -> None:
    from robot_agent_runtime.models import PlatformEvent

    class Source:
        def __init__(self, name: str) -> None:
            self._name = name

        async def watch(self):
            for number in (1, 2):
                yield PlatformEvent(source=self._name, event_type="vendor.changed", data={"value": number})

    await check_platform_event_source_contract(Source("other-sdk"), 2)
    await check_platform_event_source_contract(Source("http-adapter"), 2)


async def check_robot_platform_contract(
    platform: RobotPlatform, successful_params: dict[str, dict[str, Any]],
    *, options: RuntimeOptions | None = None,
) -> None:
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.sdk import RobotRuntime

    skills = platform.skills
    assert set(successful_params) <= set(skills)
    await platform.start()
    runtime = RobotRuntime.from_platform(InMemoryTaskRepository(), platform, options=options)
    try:
        await platform.start()
        assert all(platform.skills[name] is skill for name, skill in skills.items())
        await runtime.start()
        for name, params in successful_params.items():
            skill = unwrap_thread_binding(skills[name])
            assert skill.definition.name == name
            jsonschema.validate(params, skill.definition.input_schema)
            task = await runtime.submit_task(TaskRequest(
                task_type="platform-contract", steps=[SkillCall(skill=name, params=params)],
            ))
            async with asyncio.timeout(1):
                async for _ in runtime.subscribe_task_events(task.id):
                    pass
            final = await runtime.get_task_status(task.id)
            assert final.state == TaskState.SUCCEEDED, final.error
    finally:
        try:
            await runtime.close()
        finally:
            await platform.close()
            await platform.close()


async def test_non_callback_vendor_platform_uses_the_same_runtime_contract() -> None:
    from types import MappingProxyType
    from robot_agent_runtime.models import SkillDefinition, SkillResult
    from robot_agent_runtime.ports import Reconciler, RobotStateProvider

    class Client:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def say(self, text: str) -> dict[str, str]:
            self.messages.append(text)
            return {"said": text}

    class Speech:
        definition = SkillDefinition(
            name="audio.speak", version="1", input_schema={"type": "object"},
            resources={"speaker"}, permissions=set(), preconditions=set(),
            timeout_s=1, supports_cancel=False,
        )

        def __init__(self, client: Client) -> None:
            self._client = client

        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            return SkillResult(success=True, output=await self._client.say(params["message"]))

    class Platform:
        def __init__(self) -> None:
            self.client = Client()
            self._skills: Mapping[str, Skill] = MappingProxyType({"audio.speak": Speech(self.client)})

        @property
        def skills(self) -> Mapping[str, Skill]:
            return self._skills

        @property
        def robot_state(self) -> RobotStateProvider | None:
            return None

        @property
        def reconciler(self) -> Reconciler | None:
            return None

        async def start(self) -> None:
            pass

        async def close(self) -> None:
            pass

    platform = Platform()
    await check_robot_platform_contract(platform, {"audio.speak": {"message": "different SDK"}})
    assert platform.client.messages == ["different SDK"]


async def test_platform_contract_preserves_shared_group_dedicated_and_plain_skills() -> None:
    from types import MappingProxyType
    from robot_agent_runtime.models import SkillDefinition, SkillResult
    from robot_agent_runtime.ports import Reconciler, RobotStateProvider

    owners: dict[str, int] = {}

    class Probe:
        def __init__(self, name: str) -> None:
            self.definition = SkillDefinition(
                name=name, version="1", input_schema={"type": "object"}, resources=set(),
                permissions=set(), preconditions=set(), timeout_s=1, supports_cancel=False,
            )

        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            owners[self.definition.name] = threading.get_ident()
            return SkillResult(success=True)

    class Platform:
        def __init__(self) -> None:
            self._skills: Mapping[str, Skill | ThreadBinding[Skill]] = MappingProxyType({
                "group-a": ThreadBinding.group(Probe("group-a"), "audio"),
                "group-b": ThreadBinding.group(Probe("group-b"), "audio"),
                "shared-a": ThreadBinding.shared(Probe("shared-a")),
                "shared-b": ThreadBinding.shared(Probe("shared-b")),
                "dedicated": ThreadBinding.dedicated(Probe("dedicated")),
                "plain": Probe("plain"),
            })

        @property
        def skills(self) -> Mapping[str, Skill | ThreadBinding[Skill]]:
            return self._skills

        @property
        def robot_state(self) -> RobotStateProvider | None:
            return None

        @property
        def reconciler(self) -> Reconciler | None:
            return None

        async def start(self) -> None:
            pass

        async def close(self) -> None:
            pass

    platform: RobotPlatform = Platform()
    await check_robot_platform_contract(
        platform, {name: {} for name in platform.skills},
        options=RuntimeOptions(max_extension_threads=6),
    )
    assert owners["group-a"] == owners["group-b"]
    assert owners["shared-a"] == owners["shared-b"]
    assert len({owners[name] for name in ("group-a", "shared-a", "dedicated", "plain")}) == 4
    assert threading.get_ident() not in owners.values()


async def test_cc_grouped_platform_uses_the_same_contract() -> None:
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform

    api = ContractCallbackApi()
    await check_robot_platform_contract(
        create_cc_api_platform(api, skill_thread_group="audio"),
        {"audio.speak": {"message": "grouped CC"}},
    )
    assert not api.services


async def check_admission_validator_contract(validator: AdmissionValidator) -> None:
    from robot_agent_runtime.errors import PermissionDenied, PreconditionFailed, SchemaValidationError

    valid = SkillCall(skill="navigation.navigate_to", params={"target": "dock"})
    await validator.validate(valid, frozenset())
    with pytest.raises(SchemaValidationError):
        await validator.validate(SkillCall(skill="navigation.navigate_to", params={}), frozenset())
    with pytest.raises(PreconditionFailed):
        await validator.validate(valid, frozenset({"estop.clear"}))
    with pytest.raises(PermissionDenied):
        await validator.validate(SkillCall(skill="audio.speak", params={"message": "hello"}), frozenset())


async def test_local_admission_validator_contract() -> None:
    from robot_agent_runtime.kernel.policy import LocalAdmissionValidator, PolicyEnforcer
    from robot_agent_runtime.kernel.registry import SkillRegistry

    await check_admission_validator_contract(LocalAdmissionValidator(
        PolicyEnforcer(SkillRegistry(Simulator().skills), {"navigation"}),
    ))


async def test_isolated_admission_validator_contract() -> None:
    from robot_agent_runtime.hosting.isolation import IsolatedAdmissionValidator, isolate_ports
    from robot_agent_runtime.options import RuntimeOptions
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository

    ports = isolate_ports(InMemoryTaskRepository(), Simulator().skills, None, None, RuntimeOptions())
    await ports.prepare()
    try:
        await check_admission_validator_contract(IsolatedAdmissionValidator(ports, {"navigation"}))
    finally:
        await ports.close()


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
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository

    await check_paged_event_repository_contract(InMemoryTaskRepository())


async def test_sqlite_paged_event_repository_contract() -> None:
    from robot_agent_runtime.repositories.sqlite import SQLiteTaskRepository

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
        callback: Callable[[CcApiResponse], None],
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


async def check_cc_api_speech_contract(api: CcApiClient) -> None:
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
    await check_cc_api_speech_contract(api)
    assert not api.services
    assert api.notifications == ["cc.api.work.subscribe", "cc.api.work.unsubscribe"]


async def test_cc_vendor_platform_contract() -> None:
    from robot_agent_runtime.adapters.cc_api import create_cc_api_platform

    api = ContractCallbackApi()
    await check_robot_platform_contract(create_cc_api_platform(api), {
        "audio.speak": {"message": "CC platform contract"},
    })
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
