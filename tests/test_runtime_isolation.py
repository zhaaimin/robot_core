import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import ExtensionTimeout, InvalidRequest
from robot_agent_runtime.models import SkillCall, SkillContext, SkillDefinition, SkillResult, Task, TaskRequest, TaskState
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


def request(skill: str) -> TaskRequest:
    return TaskRequest(task_type="isolation", steps=[SkillCall(skill=skill)])


async def terminal(runtime: RobotRuntime, task_id: str) -> Task:
    async with asyncio.timeout(2):
        async for _ in runtime.subscribe_task_events(task_id):
            pass
        return await runtime.get_task_status(task_id)


async def test_blocking_async_skill_cannot_block_kernel_or_independent_skill() -> None:
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    class BlockingSkill:
        definition = SkillDefinition(
            name="blocking", version="1", input_schema={"type": "object"},
            resources={"motor"}, permissions=set(), preconditions=set(),
            timeout_s=0.03, supports_cancel=True,
        )

        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            loop.call_soon_threadsafe(finished.set)
            return SkillResult(success=True)

    skills = Simulator().skills
    skills["blocking"] = BlockingSkill()
    runtime = RobotRuntime(InMemoryTaskRepository(), skills)
    await runtime.start()
    try:
        task = await runtime.submit_task(request("blocking"))
        await asyncio.wait_for(entered.wait(), 1)
        async with asyncio.timeout(1):
            assert (await runtime.get_task_status(task.id)).id == task.id
            healthy = await runtime.submit_task(request("robot.get_state"))
            assert (await terminal(runtime, healthy.id)).state == TaskState.SUCCEEDED
        final = await terminal(runtime, task.id)
        assert final.state == TaskState.RECONCILIATION_REQUIRED
        assert final.error is not None and final.error.code == "EXECUTION_UNRESPONSIVE"
        await asyncio.wait_for(runtime.close(), 1)
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await runtime.close()


async def test_blocking_robot_checkpoint_fails_closed_without_blocking_kernel() -> None:
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    class BlockedState:
        async def unsatisfied(self) -> frozenset[str]:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            loop.call_soon_threadsafe(finished.set)
            return frozenset()

        async def watch(self) -> AsyncIterator[frozenset[str]]:
            if False:
                yield frozenset()

    runtime = RobotRuntime(
        InMemoryTaskRepository(), Simulator().skills, robot_state=BlockedState(),
        options=RuntimeOptions(port_timeout_s=0.05, cancel_grace_s=0.02),
    )
    await runtime.start()
    try:
        submission = asyncio.create_task(runtime.submit_task(request("robot.get_state")))
        await asyncio.wait_for(entered.wait(), 1)
        assert await asyncio.wait_for(runtime.list_skills(), 1)
        task = await asyncio.wait_for(submission, 1)
        assert task.state == TaskState.FAILED
        assert task.error is not None and task.error.code == "INTERNAL_ERROR"
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await runtime.close()


async def test_blocked_repository_does_not_block_kernel_and_times_out_once() -> None:
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    class BlockedRepository(InMemoryTaskRepository):
        async def get_task(self, task_id: str) -> Task | None:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            loop.call_soon_threadsafe(finished.set)
            return await super().get_task(task_id)

    runtime = RobotRuntime(
        BlockedRepository(), Simulator().skills,
        options=RuntimeOptions(port_timeout_s=0.05, cancel_grace_s=0.02),
    )
    await runtime.start()
    try:
        query = asyncio.create_task(runtime.get_task_status("missing"))
        await asyncio.wait_for(entered.wait(), 1)
        assert await asyncio.wait_for(runtime.list_skills(), 1)
        with pytest.raises(ExtensionTimeout):
            await asyncio.wait_for(query, 1)
        with pytest.raises(ExtensionTimeout):
            await asyncio.wait_for(runtime.get_task_status("missing"), 1)
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        try:
            await asyncio.wait_for(runtime.close(), 1)
        except ExceptionGroup as errors:
            assert all(isinstance(error, ExtensionTimeout) for error in errors.exceptions)


async def test_failed_declaration_start_cannot_replace_a_blocked_extension_thread() -> None:
    release = threading.Event()
    calls: list[int] = []

    class BlockedDeclaration:
        @property
        def definition(self) -> SkillDefinition:
            calls.append(threading.get_ident())
            release.wait()
            return SkillDefinition(name="blocked", version="1", input_schema={"type": "object"})

        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            return SkillResult(success=True)

    runtime = RobotRuntime(
        InMemoryTaskRepository(), {"blocked": BlockedDeclaration()},
        options=RuntimeOptions(port_timeout_s=0.03, cancel_grace_s=0.01),
    )
    try:
        for _ in range(2):
            with pytest.raises(ExtensionTimeout):
                await asyncio.wait_for(runtime.start(), 1)
        assert len(calls) == 1
    finally:
        release.set()
        try:
            await runtime.close()
        except ExtensionTimeout:
            pass


async def test_permission_configuration_is_plain_data_before_entering_kernel() -> None:
    class Permissions(set[str]):
        def __deepcopy__(self, memo: dict[int, Any]) -> Any:
            raise AssertionError("Configuration hooks must not run on the kernel")

    runtime = RobotRuntime(
        InMemoryTaskRepository(), Simulator().skills,
        allowed_permissions=Permissions({"audio"}),
    )
    await runtime.start()
    try:
        task = await runtime.submit_task(TaskRequest(
            task_type="permissions", steps=[SkillCall(skill="audio.speak", params={"message": "hello"})],
        ))
        assert (await terminal(runtime, task.id)).state == TaskState.SUCCEEDED
    finally:
        await runtime.close()


def test_registry_keys_cannot_carry_custom_comparison_code_into_kernel() -> None:
    class CustomKey(str):
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            raise AssertionError("Registry comparison must use plain strings")

    skill = Simulator().skills["robot.get_state"]
    with pytest.raises(InvalidRequest):
        RobotRuntime(InMemoryTaskRepository(), {CustomKey("robot.get_state"): skill})
