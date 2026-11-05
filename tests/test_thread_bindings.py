import asyncio
import threading
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from robot_agent_runtime.errors import (
    ExecutionUnresponsive, ExtensionCapacityExceeded, InvalidRequest, TaskCancelled,
)
from robot_agent_runtime.hosting.isolation import isolate_ports
from robot_agent_runtime.models import (
    SkillCall, SkillContext, SkillDefinition, SkillResult, Task, TaskRequest, TaskState,
)
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository


class RecordingSkill:
    def __init__(self, name: str) -> None:
        self.name = name
        self.created_thread = threading.get_ident()
        self.declaration_thread = 0

    @property
    def definition(self) -> SkillDefinition:
        self.declaration_thread = threading.get_ident()
        return SkillDefinition(
            name=self.name, version="1", input_schema={"type": "object"},
            resources=set(), permissions=set(), preconditions=set(),
            timeout_s=2, supports_cancel=True,
        )

    async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
        return SkillResult(success=True, output={"thread": threading.get_ident()})


def context() -> SkillContext:
    return SkillContext("task", "execution", asyncio.Event())


async def test_shared_group_and_dedicated_bindings_have_stable_thread_owners() -> None:
    from robot_agent_runtime.sdk import ThreadBinding

    originals = {name: RecordingSkill(name) for name in (
        "shared_a", "shared_b", "group_a", "group_b", "other_group", "dedicated", "plain",
    )}
    ports = isolate_ports(InMemoryTaskRepository(), {
        "shared_a": ThreadBinding.shared(originals["shared_a"]),
        "shared_b": ThreadBinding.shared(originals["shared_b"]),
        "group_a": ThreadBinding.group(originals["group_a"], "shared"),
        "group_b": ThreadBinding.group(originals["group_b"], "shared"),
        "other_group": ThreadBinding.group(originals["other_group"], "navigation"),
        "dedicated": ThreadBinding.dedicated(originals["dedicated"]),
        "plain": originals["plain"],
    }, None, None, RuntimeOptions())
    await ports.prepare()
    try:
        threads: dict[str, int] = {}
        for name, proxy in ports.skills.items():
            first = await proxy.execute(context(), {})
            second = await proxy.execute(context(), {})
            threads[name] = first.output["thread"]
            assert first.output == second.output
            assert threads[name] == originals[name].declaration_thread
            assert threads[name] != originals[name].created_thread
        assert threads["shared_a"] == threads["shared_b"]
        assert threads["group_a"] == threads["group_b"]
        assert len({threads[name] for name in (
            "shared_a", "group_a", "other_group", "dedicated", "plain",
        )}) == 5
    finally:
        await ports.close()


async def test_shared_binding_accepts_more_objects_than_thread_capacity() -> None:
    from robot_agent_runtime.sdk import ThreadBinding

    skills = {str(index): ThreadBinding.shared(RecordingSkill(str(index)))
              for index in range(40)}
    ports = isolate_ports(InMemoryTaskRepository(), skills, None, None,
                          RuntimeOptions(max_extension_threads=3))
    await ports.prepare()
    try:
        owners = {(await proxy.execute(context(), {})).output["thread"]
                  for proxy in ports.skills.values()}
        assert len(owners) == 1
    finally:
        await ports.close()
    with pytest.raises(ExtensionCapacityExceeded):
        isolate_ports(InMemoryTaskRepository(), skills, None, None,
                      RuntimeOptions(max_extension_threads=2))


async def test_same_object_aliases_share_proxy_and_reject_conflicting_bindings() -> None:
    from robot_agent_runtime.sdk import ThreadBinding

    original = RecordingSkill("skill")
    for first, second in (
        (original, ThreadBinding.dedicated(original)),
        (ThreadBinding.shared(original), ThreadBinding.shared(original)),
        (ThreadBinding.group(original, "robot"), ThreadBinding.group(original, "robot")),
    ):
        ports = isolate_ports(InMemoryTaskRepository(), {"a": first, "b": second},
                              None, None, RuntimeOptions())
        await ports.prepare()
        try:
            assert ports.skills["a"] is ports.skills["b"]
        finally:
            await ports.close()
    for first, second in (
        (original, ThreadBinding.shared(original)),
        (ThreadBinding.shared(original), ThreadBinding.group(original, "shared")),
        (ThreadBinding.group(original, "a"), ThreadBinding.group(original, "b")),
    ):
        with pytest.raises(InvalidRequest, match="conflicting thread bindings"):
            isolate_ports(InMemoryTaskRepository(), {"a": first, "b": second},
                          None, None, RuntimeOptions())


async def test_all_external_roles_can_share_named_group_without_owning_kernel_loop() -> None:
    from robot_agent_runtime.sdk import ThreadBinding

    owners: list[int] = []

    class Repository(InMemoryTaskRepository):
        async def start(self) -> None:
            owners.append(threading.get_ident())

        async def close(self) -> None:
            owners.append(threading.get_ident())

    class State:
        async def unsatisfied(self) -> frozenset[str]:
            owners.append(threading.get_ident())
            return frozenset()

        async def watch(self) -> AsyncIterator[frozenset[str]]:
            owners.append(threading.get_ident())
            yield frozenset()

    class Reconcile:
        async def reconcile(self, task: Any) -> None:
            owners.append(threading.get_ident())

    skill = RecordingSkill("skill")
    ports = isolate_ports(
        ThreadBinding.group(Repository(), "robot"),
        {"skill": ThreadBinding.group(skill, "robot")},
        ThreadBinding.group(Reconcile(), "robot"),
        ThreadBinding.group(State(), "robot"), RuntimeOptions(max_extension_threads=2),
    )
    await ports.prepare()
    try:
        await ports.repository.start()
        assert ports.robot_state is not None and ports.reconciler is not None
        await ports.robot_state.unsatisfied()
        stream = ports.robot_state.watch()
        assert await anext(stream) == frozenset()
        await stream.aclose()
        await ports.reconciler.reconcile(None)
        result = await ports.skills["skill"].execute(context(), {})
        await ports.repository.close()
        assert len(owners) == 5
        assert set(owners) == {result.output["thread"], skill.declaration_thread}
        assert owners[0] != threading.get_ident()
    finally:
        await ports.close()


def test_binding_rejects_invalid_modes_groups_and_nested_bindings() -> None:
    from robot_agent_runtime.sdk import ThreadBinding

    skill = RecordingSkill("skill")
    for mode, group in (("invalid", None), ("group", None), ("group", ""),
                        ("group", "  "), ("group", 1), ("shared", "x"),
                        ("dedicated", "x")):
        with pytest.raises(InvalidRequest):
            ThreadBinding(skill, mode=mode, group_name=group)
    with pytest.raises(InvalidRequest):
        ThreadBinding.shared(ThreadBinding.shared(skill))


async def terminal(runtime: Any, task_id: str) -> Task:
    async with asyncio.timeout(2):
        async for _ in runtime.subscribe_task_events(task_id):
            pass
        return await runtime.get_task_status(task_id)


async def test_embedded_sdk_preserves_bindings_and_defaults_repository_to_dedicated() -> None:
    from robot_agent_runtime.sdk import RobotRuntime, ThreadBinding

    class Repository(InMemoryTaskRepository):
        owner = 0

        async def start(self) -> None:
            self.owner = threading.get_ident()

    repository = Repository()
    runtime = RobotRuntime(repository, {
        name: ThreadBinding.shared(RecordingSkill(name)) for name in ("a", "b")
    }, options=RuntimeOptions(max_extension_threads=3))
    await runtime.start()
    try:
        owners: list[int] = []
        for name in ("a", "b"):
            task = await runtime.submit_task(TaskRequest(task_type="test", steps=[SkillCall(skill=name)]))
            final = await terminal(runtime, task.id)
            assert final.state == TaskState.SUCCEEDED
            assert final.result is not None
            owners.append(final.result["steps"][0]["output"]["thread"])
        assert owners[0] == owners[1] != repository.owner
    finally:
        await runtime.close()


async def test_server_applies_bindings_and_remote_protocol_stays_compatible() -> None:
    from robot_agent_runtime.sdk import RemoteRuntime, ThreadBinding
    from robot_agent_runtime.transport.server import RuntimeServer

    with tempfile.TemporaryDirectory(prefix="rar-bind-") as directory:
        path = str(Path(directory) / "s.sock")
        server = RuntimeServer(
            InMemoryTaskRepository(),
            {name: ThreadBinding.group(RecordingSkill(name), "robot") for name in ("a", "b")},
            path, options=RuntimeOptions(max_extension_threads=3))
        remote = RemoteRuntime(path)
        await server.start()
        try:
            await remote.start()
            owners: list[int] = []
            for name in ("a", "b"):
                task = await remote.submit_task(TaskRequest(task_type="test", steps=[SkillCall(skill=name)]))
                final = await terminal(remote, task.id)
                assert final.state == TaskState.SUCCEEDED
                assert final.result is not None
                owners.append(final.result["steps"][0]["output"]["thread"])
            assert owners[0] == owners[1] != threading.get_ident()
        finally:
            await remote.close()
            await server.close()


async def test_shared_skills_can_interleave_cooperatively_on_the_same_loop() -> None:
    from robot_agent_runtime.sdk import ThreadBinding

    ready: asyncio.Event | None = None

    class Waiting(RecordingSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            nonlocal ready
            ready = asyncio.Event()
            await ready.wait()
            return await super().execute(context, params)

    class Releasing(RecordingSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            assert ready is not None
            ready.set()
            return await super().execute(context, params)

    ports = isolate_ports(InMemoryTaskRepository(), {
        "wait": ThreadBinding.shared(Waiting("wait")),
        "release": ThreadBinding.shared(Releasing("release")),
    }, None, None, RuntimeOptions())
    await ports.prepare()
    try:
        first, second = await asyncio.wait_for(asyncio.gather(
            ports.skills["wait"].execute(context(), {}),
            ports.skills["release"].execute(context(), {}),
        ), 1)
        assert first.output == second.output
    finally:
        await ports.close()


async def test_blocked_shared_group_stalls_peers_but_not_dedicated_skill_or_kernel() -> None:
    from robot_agent_runtime.sdk import RobotRuntime, ThreadBinding

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    peer_entered = threading.Event()

    class Blocking(RecordingSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3), "Blocking test did not release its worker"
            return await super().execute(context, params)

    class Peer(RecordingSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            peer_entered.set()
            return await super().execute(context, params)

    runtime = RobotRuntime(InMemoryTaskRepository(), {
        "block": ThreadBinding.shared(Blocking("block")),
        "peer": ThreadBinding.shared(Peer("peer")),
        "dedicated": ThreadBinding.dedicated(RecordingSkill("dedicated")),
    })
    await runtime.start()
    try:
        block = await runtime.submit_task(TaskRequest(task_type="test", steps=[SkillCall(skill="block")]))
        await asyncio.wait_for(entered.wait(), 1)
        peer = await runtime.submit_task(TaskRequest(task_type="test", steps=[SkillCall(skill="peer")]))
        independent = await runtime.submit_task(TaskRequest(task_type="test", steps=[SkillCall(skill="dedicated")]))
        assert (await terminal(runtime, independent.id)).state == TaskState.SUCCEEDED
        assert len(await asyncio.wait_for(runtime.list_skills(), 1)) == 3
        assert not peer_entered.is_set()
        release.set()
        assert (await terminal(runtime, block.id)).state == TaskState.SUCCEEDED
        assert (await terminal(runtime, peer.id)).state == TaskState.SUCCEEDED
    finally:
        release.set()
        await runtime.close()


async def test_unconfirmed_shared_skill_does_not_poison_other_objects_or_loop() -> None:
    from robot_agent_runtime.sdk import ThreadBinding

    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    class Unconfirmed(RecordingSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            loop.call_soon_threadsafe(entered.set)
            await context.cancel_event.wait()
            raise asyncio.CancelledError

    ports = isolate_ports(InMemoryTaskRepository(), {
        "bad": ThreadBinding.shared(Unconfirmed("bad")),
        "good": ThreadBinding.shared(RecordingSkill("good")),
    }, None, None, RuntimeOptions(cancel_grace_s=0.03))
    await ports.prepare()
    ctx = context()
    execution = asyncio.create_task(ports.skills["bad"].execute(ctx, {}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        ctx.cancel_event.set()
        with pytest.raises(ExecutionUnresponsive):
            await asyncio.wait_for(execution, 1)
        with pytest.raises(ExecutionUnresponsive):
            await ports.skills["bad"].execute(context(), {})
        assert (await ports.skills["good"].execute(context(), {})).success
    finally:
        await ports.close()


async def test_cancelling_one_shared_skill_does_not_cancel_its_peer() -> None:
    from robot_agent_runtime.sdk import ThreadBinding

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()

    class Cancelled(RecordingSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            loop.call_soon_threadsafe(entered.set)
            await context.cancel_event.wait()
            raise TaskCancelled("Stopped")

    ports = isolate_ports(InMemoryTaskRepository(), {
        "cancel": ThreadBinding.group(Cancelled("cancel"), "robot"),
        "peer": ThreadBinding.group(RecordingSkill("peer"), "robot"),
    }, None, None, RuntimeOptions())
    await ports.prepare()
    ctx = context()
    execution = asyncio.create_task(ports.skills["cancel"].execute(ctx, {}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        ctx.cancel_event.set()
        with pytest.raises(TaskCancelled):
            await asyncio.wait_for(execution, 1)
        assert (await ports.skills["peer"].execute(context(), {})).success
    finally:
        await ports.close()


async def test_low_level_core_uses_plain_ports_on_the_calling_loop() -> None:
    from robot_agent_runtime.kernel.core import RuntimeCore

    skill = RecordingSkill("skill")
    core = RuntimeCore(InMemoryTaskRepository(), {"skill": skill})
    await core.start()
    try:
        task = await core.submit_task(TaskRequest(task_type="test", steps=[SkillCall(skill="skill")]))
        final = await terminal(core, task.id)
        assert final.state == TaskState.SUCCEEDED
        assert final.result is not None
        assert final.result["steps"][0]["output"]["thread"] == threading.get_ident()
    finally:
        await core.close()


async def test_shared_threads_are_local_to_each_runtime_and_stopped_at_close() -> None:
    from robot_agent_runtime.sdk import ThreadBinding

    left = isolate_ports(InMemoryTaskRepository(),
                         {"a": ThreadBinding.shared(RecordingSkill("a"))},
                         None, None, RuntimeOptions())
    right = isolate_ports(InMemoryTaskRepository(),
                          {"b": ThreadBinding.shared(RecordingSkill("b"))},
                          None, None, RuntimeOptions())
    await left.prepare()
    await right.prepare()
    try:
        first = await left.skills["a"].execute(context(), {})
        second = await right.skills["b"].execute(context(), {})
        owners = {first.output["thread"], second.output["thread"]}
        assert len(owners) == 2
        await left.close()
        assert (await right.skills["b"].execute(context(), {})).success
    finally:
        await left.close()
        await right.close()
    assert not owners.intersection(thread.ident for thread in threading.enumerate())


async def test_shared_startup_prepares_many_declarations_with_small_call_limits() -> None:
    from robot_agent_runtime.sdk import RobotRuntime, ThreadBinding

    runtime = RobotRuntime(InMemoryTaskRepository(), {
        str(index): ThreadBinding.shared(RecordingSkill(str(index))) for index in range(40)
    }, options=RuntimeOptions(max_extension_threads=3, max_pending_calls=1, max_control_calls=1))
    try:
        await runtime.start()
        assert len(await runtime.list_skills()) == 40
    finally:
        await runtime.close()


async def test_shared_objects_use_one_call_budget_and_preserve_control_capacity() -> None:
    from robot_agent_runtime.hosting.execution_context import CONTROL_OPERATION
    from robot_agent_runtime.sdk import ThreadBinding

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()

    class Waiting(RecordingSkill):
        async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
            loop.call_soon_threadsafe(entered.set)
            await context.cancel_event.wait()
            raise TaskCancelled("Stopped")

    ports = isolate_ports(InMemoryTaskRepository(), {
        "waiting": ThreadBinding.shared(Waiting("waiting")),
        "peer": ThreadBinding.shared(RecordingSkill("peer")),
    }, None, None, RuntimeOptions(max_pending_calls=1, max_control_calls=1))
    await ports.prepare()
    ctx = context()
    execution = asyncio.create_task(ports.skills["waiting"].execute(ctx, {}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(ExtensionCapacityExceeded):
            await ports.skills["peer"].execute(context(), {})
        token = CONTROL_OPERATION.set(True)
        try:
            assert (await ports.skills["peer"].execute(context(), {})).success
        finally:
            CONTROL_OPERATION.reset(token)
        ctx.cancel_event.set()
        with pytest.raises(TaskCancelled):
            await asyncio.wait_for(execution, 1)
        assert (await ports.skills["peer"].execute(context(), {})).success
    finally:
        ctx.cancel_event.set()
        await ports.close()
