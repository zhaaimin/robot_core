import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest

from robot_agent_runtime.adapters.simulator import SimulatedRobotState
from robot_agent_runtime.errors import ExtensionTimeout, InvalidRequest
from robot_agent_runtime.hosting.isolation import isolate_ports
from robot_agent_runtime.models import (
    SkillCall, SkillContext, SkillDefinition, SkillResult, Task, TaskRequest, TaskState,
)
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime, ThreadBinding


class ProbeSkill:
    def __init__(self, name: str) -> None:
        self.definition = SkillDefinition(
            name=name, version="1", input_schema={"type": "object"},
            resources={name}, permissions=set(), preconditions=set(),
            timeout_s=5, supports_cancel=True,
        )
        self.calls = 0

    async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
        self.calls += 1
        return SkillResult(success=True, output={"thread_name": threading.current_thread().name})


def context() -> SkillContext:
    return SkillContext("task", "execution", asyncio.Event())


def request(name: str, *, timeout_s: float | None = None) -> TaskRequest:
    return TaskRequest(task_type="boundary", steps=[SkillCall(skill=name)], timeout_s=timeout_s)


async def terminal(runtime: RobotRuntime, task_id: str) -> Task:
    async with asyncio.timeout(2):
        async for _ in runtime.subscribe_task_events(task_id):
            pass
        return await runtime.get_task_status(task_id)


def test_none_target_is_rejected_by_all_binding_constructors() -> None:
    for construct in (
        lambda: ThreadBinding(None),
        lambda: ThreadBinding.shared(None),
        lambda: ThreadBinding.group(None, "robot"),
        lambda: ThreadBinding.dedicated(None),
    ):
        with pytest.raises(InvalidRequest, match="target cannot be None"):
            construct()


async def test_repository_queued_timeouts_do_not_disable_an_object_that_never_started() -> None:
    for operation in ("get_task", "create_task", "start", "close"):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        calls: list[str] = []

        class Blocking(ProbeSkill):
            async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(3), "Test did not release its blocking skill"
                return await super().execute(context, params)

        class Repository(InMemoryTaskRepository):
            async def start(self) -> None:
                calls.append("start")

            async def close(self) -> None:
                calls.append("close")

            async def get_task(self, task_id: str) -> Task | None:
                calls.append("get_task")
                return await super().get_task(task_id)

            async def create_task(self, task: Task) -> Task:
                calls.append("create_task")
                return await super().create_task(task)

        ports = isolate_ports(ThreadBinding.group(Repository(), "robot"),
            {"block": ThreadBinding.group(Blocking("block"), "robot")}, None, None,
            RuntimeOptions(port_timeout_s=0.03, cancel_grace_s=0.02))
        await ports.prepare()
        execution = asyncio.create_task(ports.skills["block"].execute(context(), {}))
        saved = Task(id="saved", task_type="test", state=TaskState.CREATED,
                     steps=[SkillCall(skill="block")])
        args: tuple[Any, ...] = (("saved",) if operation == "get_task" else
                                 (saved,) if operation == "create_task" else ())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with pytest.raises(ExtensionTimeout):
                await getattr(ports.repository, operation)(*args)
            assert calls == []
            release.set()
            await asyncio.wait_for(execution, 1)
            await ports.repository.start()
            await ports.repository.create_task(saved)
            assert (await ports.repository.get_task("saved")) is not None
            await ports.repository.close()
            assert calls == ["start", "create_task", "get_task", "close"]
        finally:
            release.set()
            await asyncio.gather(execution, return_exceptions=True)
            await ports.close()


async def test_queued_skill_deadlines_do_not_execute_or_poison_the_skill() -> None:
    for total in (False, True):
        for supports_cancel in (False, True):
            loop = asyncio.get_running_loop()
            entered = asyncio.Event()
            release = threading.Event()

            class Blocking(ProbeSkill):
                async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(3), "Test did not release its blocking skill"
                    return await super().execute(context, params)

            peer = ProbeSkill("peer")
            peer.definition.supports_cancel = supports_cancel
            peer.definition.timeout_s = 5 if total else 0.03
            runtime = RobotRuntime(InMemoryTaskRepository(), {
                "block": ThreadBinding.group(Blocking("block"), "robot"),
                "peer": ThreadBinding.group(peer, "robot"),
            }, options=RuntimeOptions(cancel_grace_s=0.03))
            await runtime.start()
            try:
                blocked = await runtime.submit_task(request("block"))
                await asyncio.wait_for(entered.wait(), 1)
                queued = await runtime.submit_task(request("peer", timeout_s=0.06 if total else None))
                final = await terminal(runtime, queued.id)
                assert final.state == TaskState.FAILED
                assert final.error is not None
                assert final.error.code == ("TASK_TIMEOUT" if total else "SKILL_TIMEOUT")
                assert peer.calls == 0
                release.set()
                assert (await terminal(runtime, blocked.id)).state == TaskState.SUCCEEDED
                retry = await runtime.submit_task(request("peer"))
                assert (await terminal(runtime, retry.id)).state == TaskState.SUCCEEDED
                assert peer.calls == 1
            finally:
                release.set()
                await runtime.close()


async def observe_state_while_skill_blocks(grouped: bool) -> None:
    loop = asyncio.get_running_loop()
    registered = asyncio.Event()
    entered = asyncio.Event()
    delivered = asyncio.Event()
    release = threading.Event()

    class State(SimulatedRobotState):
        """Report readiness after the owner loop attached its subscription."""

        async def watch(self) -> AsyncIterator[frozenset[str]]:
            asyncio.get_running_loop().call_soon(loop.call_soon_threadsafe, registered.set)
            async for labels in super().watch():
                yield labels

    class Blocking(ProbeSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3), "Test did not release its blocking skill"
            return await super().execute(context, params)

    state = State()
    skill = Blocking("nav")
    skill.definition.preconditions = {"estop.clear"}
    runtime = RobotRuntime(InMemoryTaskRepository(),
        {"nav": ThreadBinding.group(skill, "navigation")},
        robot_state=ThreadBinding.group(state, "navigation") if grouped else state,
        options=RuntimeOptions(cancel_grace_s=0.03))
    await runtime.start()
    watcher: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(registered.wait(), 1)
        task = await runtime.submit_task(request("nav"))
        await asyncio.wait_for(entered.wait(), 1)

        async def observe() -> None:
            async for event in runtime.subscribe_task_events(task.id):
                if event.event_type == "TASK_PRECONDITION_LOST":
                    delivered.set()

        watcher = asyncio.create_task(observe())
        state.set_unsatisfied({"estop.clear"})
        if grouped:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(delivered.wait(), 0.1)
            assert (await runtime.get_task_status(task.id)).state == TaskState.RUNNING
        else:
            await asyncio.wait_for(delivered.wait(), 1)
            final = await terminal(runtime, task.id)
            assert final.state == TaskState.RECONCILIATION_REQUIRED
            assert final.error is not None and final.error.code == "EXECUTION_UNRESPONSIVE"
        assert not release.is_set()
    finally:
        release.set()
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await runtime.close()


async def test_dedicated_state_provider_delivers_precondition_loss_while_skill_blocks() -> None:
    await observe_state_while_skill_blocks(grouped=False)


async def test_state_provider_sharing_a_blocked_skill_loop_cannot_deliver_precondition_loss() -> None:
    await observe_state_while_skill_blocks(grouped=True)


async def test_thread_names_identify_shared_group_and_dedicated_bindings() -> None:
    ports = isolate_ports(InMemoryTaskRepository(), {
        "shared": ThreadBinding.shared(ProbeSkill("shared")),
        "group": ThreadBinding.group(ProbeSkill("group"), "navigation"),
        "dedicated": ThreadBinding.dedicated(ProbeSkill("dedicated")),
    }, None, None, RuntimeOptions())
    await ports.prepare()
    try:
        names = {name: (await skill.execute(context(), {})).output["thread_name"]
                 for name, skill in ports.skills.items()}
        assert names["shared"] == "robot-extension-shared"
        assert names["group"] == "robot-extension-group-navigation"
        assert names["dedicated"].startswith("robot-extension-dedicated-")
        assert len(set(names.values())) == 3
    finally:
        await ports.close()


async def test_started_repository_is_disabled_before_cancellation_can_admit_a_queued_write() -> None:
    from robot_agent_runtime.hosting.isolation import _Port, _Repository
    from robot_agent_runtime.hosting.thread_runtime import LoopThread

    caller = asyncio.get_running_loop()
    release = threading.Event()
    queue_drained = threading.Event()
    disabled_at_cancel: list[bool] = []
    entered = asyncio.Event()
    late_write_ran = threading.Event()

    class Repository(InMemoryTaskRepository):
        async def get_task(self, task_id: str) -> Task | None:
            caller.call_soon_threadsafe(entered.set)
            assert release.wait(3), "Test did not release its repository"
            return None

        async def create_task(self, task: Task) -> Task:
            late_write_ran.set()
            return await super().create_task(task)

    owner = LoopThread("repository-cancel-order")
    await owner.start()
    port = _Port(Repository(), owner, RuntimeOptions(port_timeout_s=0.1))
    repository = _Repository(port)
    read = asyncio.create_task(repository.get_task("missing"))
    write: asyncio.Task[Task] | None = None
    saved = Task(id="saved", task_type="test", state=TaskState.CREATED,
                 steps=[SkillCall(skill="test")])
    try:
        await asyncio.wait_for(entered.wait(), 1)
        active = next(iter(owner._jobs))
        original_cancel = active.cancel

        def cancel_and_allow_the_queue_to_run() -> None:
            disabled_at_cancel.append(port.poisoned)
            release.set()
            assert queue_drained.wait(1), "Repository queue did not resume"
            original_cancel()

        active.cancel = cancel_and_allow_the_queue_to_run
        write = asyncio.create_task(repository.create_task(saved))
        queued = asyncio.Event()
        caller.call_soon(queued.set)
        await queued.wait()

        async def sentinel() -> None:
            queue_drained.set()

        barrier = owner.submit(sentinel)
        with pytest.raises(ExtensionTimeout):
            await read
        assert disabled_at_cancel == [True]
        with pytest.raises(ExtensionTimeout):
            await write
        await barrier.wait(1)
        assert not late_write_ran.is_set()
        assert port.poisoned
    finally:
        release.set()
        await asyncio.gather(read, return_exceptions=True)
        if write is not None:
            await asyncio.gather(write, return_exceptions=True)
        await owner.close()
