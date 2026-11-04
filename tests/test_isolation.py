import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from robot_agent_runtime.errors import (
    ExecutionUnresponsive,
    ExtensionCapacityExceeded,
    ExtensionTimeout,
    InternalError,
    SnapshotTooLarge,
    TaskCancelled,
)
from robot_agent_runtime.models import (
    SkillCall,
    SkillContext,
    SkillDefinition,
    SkillResult,
    Task,
    TaskState,
)
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.repository import InMemoryTaskRepository


def definition(name: str = "test.skill") -> SkillDefinition:
    return SkillDefinition(
        name=name,
        version="1",
        input_schema={"type": "object", "properties": {}},
        resources={"device"},
        permissions=set(),
        preconditions=set(),
        timeout_s=1,
        supports_cancel=True,
    )


def context(progress: Callable[[float], None] | None = None) -> SkillContext:
    return SkillContext("task", "execution", asyncio.Event(), progress)


def task() -> Task:
    return Task(id="task", task_type="test", state=TaskState.CREATED,
                steps=[SkillCall(skill="test.skill")], metadata={"nested": []})


async def checkpoint() -> None:
    ready = asyncio.Event()
    asyncio.get_running_loop().call_soon(ready.set)
    await ready.wait()


async def test_skill_definition_and_blocking_execution_keep_stable_worker_affinity() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    kernel_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()

    class BlockingSkill:
        definition_thread = 0
        execution_thread = 0

        @property
        def definition(self) -> SkillDefinition:
            self.definition_thread = threading.get_ident()
            return definition()

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            self.execution_thread = threading.get_ident()
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return SkillResult(success=True)

    original = BlockingSkill()
    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": original}, None, None,
                          RuntimeOptions())
    await ports.prepare()
    execution = asyncio.create_task(ports.skills["test.skill"].execute(context(), {}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(checkpoint(), 0.2)
        assert not execution.done()
        assert original.definition_thread == original.execution_thread != kernel_thread
    finally:
        release.set()
        await asyncio.wait_for(execution, 1)
        await ports.close()


async def test_skill_params_results_and_declarations_are_independent_snapshots() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    class SnapshotSkill:
        definition = definition()
        returned = SkillResult(success=True, output={"nested": []})
        saved: dict[str, Any] = {}

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            params["nested"].append("worker")
            self.saved = params
            return self.returned

    original = SnapshotSkill()
    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": original}, None, None,
                          RuntimeOptions())
    await ports.prepare()
    try:
        proxy = ports.skills["test.skill"]
        original.definition.resources.add("late")
        proxy.definition.resources.add("caller")
        assert proxy.definition.resources == {"device"}
        params: dict[str, Any] = {"nested": []}
        result = await proxy.execute(context(), params)
        assert params == {"nested": []}
        result.output["nested"].append("caller")
        assert original.returned.output == {"nested": []}
        original.saved["nested"].append("late")
        assert params == {"nested": []}
    finally:
        await ports.close()


async def test_blocking_skill_cancel_is_bounded_and_poisons_instance() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    class BlockingSkill:
        definition = definition()
        calls = 0

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            self.calls += 1
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return SkillResult(success=True)

    original = BlockingSkill()
    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": original}, None, None,
                          RuntimeOptions(cancel_grace_s=0.03))
    await ports.prepare()
    proxy = ports.skills["test.skill"]
    execution = asyncio.create_task(proxy.execute(context(), {}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        execution.cancel()
        with pytest.raises(ExecutionUnresponsive):
            await asyncio.wait_for(execution, 0.3)
        with pytest.raises(ExecutionUnresponsive):
            await proxy.execute(context(), {})
        assert original.calls == 1
    finally:
        release.set()
        await ports.close()


async def test_skill_cancellation_forwards_local_event_and_confirms_real_completion() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    stopped = asyncio.Event()

    class CooperativeSkill:
        definition = definition()

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            loop.call_soon_threadsafe(entered.set)
            try:
                await ctx.cancel_event.wait()
                raise TaskCancelled("stopped")
            finally:
                loop.call_soon_threadsafe(stopped.set)

    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": CooperativeSkill()},
                          None, None, RuntimeOptions())
    await ports.prepare()
    ctx = context()
    execution = asyncio.create_task(ports.skills["test.skill"].execute(ctx, {}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        ctx.cancel_event.set()
        with pytest.raises((TaskCancelled, asyncio.CancelledError)):
            await asyncio.wait_for(execution, 1)
        assert stopped.is_set()
    finally:
        await ports.close()


async def test_repository_timeout_poisons_later_writes_and_never_blocks_kernel() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    release = threading.Event()

    class BlockingRepository(InMemoryTaskRepository):
        writes = 0

        async def get_task(self, task_id: str) -> Task | None:
            release.wait()
            return None

        async def create_task(self, value: Task) -> Task:
            self.writes += 1
            return await super().create_task(value)

    original = BlockingRepository()
    ports = isolate_ports(original, {}, None, None, RuntimeOptions(port_timeout_s=0.03))
    await ports.prepare()
    try:
        with pytest.raises(ExtensionTimeout):
            await asyncio.wait_for(ports.repository.get_task("task"), 0.3)
        with pytest.raises(ExtensionTimeout):
            await ports.repository.create_task(task())
        assert original.writes == 0
    finally:
        release.set()
        await ports.close()


async def test_repository_results_are_snapshots_even_if_adapter_returns_live_models() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    class LiveRepository(InMemoryTaskRepository):
        value = task()

        async def get_task(self, task_id: str) -> Task | None:
            return self.value

    original = LiveRepository()
    ports = isolate_ports(original, {}, None, None, RuntimeOptions())
    await ports.prepare()
    try:
        value = await ports.repository.get_task("task")
        assert value is not None
        value.metadata["nested"].append("caller")
        assert original.value.metadata == {"nested": []}
    finally:
        await ports.close()


async def test_state_and_reconciler_deadlines_are_bounded() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    release = threading.Event()

    class BlockingState:
        async def unsatisfied(self) -> frozenset[str]:
            release.wait()
            return frozenset()

        async def watch(self) -> AsyncIterator[frozenset[str]]:
            yield frozenset()

    class BlockingReconciler:
        async def reconcile(self, value: Task) -> TaskState | None:
            release.wait()
            return TaskState.SUCCEEDED

    ports = isolate_ports(InMemoryTaskRepository(), {}, BlockingReconciler(),
                          BlockingState(), RuntimeOptions(port_timeout_s=0.03))
    await ports.prepare()
    try:
        assert ports.robot_state is not None and ports.reconciler is not None
        with pytest.raises(InternalError):
            await asyncio.wait_for(ports.robot_state.unsatisfied(), 0.3)
        with pytest.raises(ExtensionTimeout):
            await asyncio.wait_for(ports.reconciler.reconcile(task()), 0.3)
    finally:
        release.set()
        await ports.close()


async def test_extension_base_exception_becomes_internal_error() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    class BrokenState:
        async def unsatisfied(self) -> frozenset[str]:
            raise SystemExit("external exit")

        async def watch(self) -> AsyncIterator[frozenset[str]]:
            return
            yield frozenset()

    ports = isolate_ports(InMemoryTaskRepository(), {}, None, BrokenState(), RuntimeOptions())
    await ports.prepare()
    try:
        assert ports.robot_state is not None
        with pytest.raises(InternalError):
            await ports.robot_state.unsatisfied()
        await checkpoint()
    finally:
        await ports.close()


async def test_distinct_extension_objects_obey_fixed_thread_capacity() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    class Skill:
        definition = definition()

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            return SkillResult(success=True)

    with pytest.raises(ExtensionCapacityExceeded):
        isolate_ports(InMemoryTaskRepository(), {"test.skill": Skill()}, None, None,
                      RuntimeOptions(max_extension_threads=1))
    skill = Skill()
    ports = isolate_ports(InMemoryTaskRepository(), {"a": skill, "b": skill}, None, None,
                          RuntimeOptions(max_extension_threads=3))
    await ports.prepare()
    try:
        assert ports.skills["a"] is ports.skills["b"]
    finally:
        await ports.close()


async def test_simulator_push_crosses_thread_owner_loop_safely() -> None:
    from robot_agent_runtime.adapters.simulator import SimulatedRobotState
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    watching = asyncio.Event()

    class State(SimulatedRobotState):
        async def watch(self) -> AsyncIterator[frozenset[str]]:
            loop.call_soon_threadsafe(watching.set)
            async for value in super().watch():
                yield value

    original = State()
    ports = isolate_ports(InMemoryTaskRepository(), {}, None, original, RuntimeOptions())
    await ports.prepare()
    assert ports.robot_state is not None
    iterator = ports.robot_state.watch()
    pending = asyncio.create_task(anext(iterator))
    try:
        await asyncio.wait_for(watching.wait(), 1)
        original.set_unsatisfied({"estop.clear"})
        assert await asyncio.wait_for(pending, 1) == frozenset({"estop.clear"})
    finally:
        await iterator.aclose()
        await ports.close()


@dataclass
class Response:
    is_success: bool = True
    result: Any = None
    error_code: int = 0
    error_message: str = ""


async def test_cc_api_execute_uses_adapter_owner_loop_and_real_stop_completion() -> None:
    from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill
    from robot_agent_runtime.isolation import isolate_ports

    owner_thread = threading.get_ident()

    class Api:
        def __init__(self) -> None:
            self.requests: asyncio.Queue[tuple[str, dict[str, Any], Callable[..., None]]] = asyncio.Queue()
            self.services: dict[str, Callable[..., None]] = {}

        def request(self, method: str, params: dict[str, Any], callback: Callable[..., None]) -> None:
            assert threading.get_ident() == owner_thread
            self.requests.put_nowait((method, params, callback))

        def notify(self, method: str) -> None:
            assert threading.get_ident() == owner_thread

        def add_service(self, name: str, callback: Callable[..., None]) -> None:
            self.services[name] = callback

        def remove_service(self, name: str) -> None:
            self.services.pop(name, None)

        def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
            pass

    api = Api()
    skill = CcApiSpeechSkill(api)
    await skill.start()
    ports = isolate_ports(InMemoryTaskRepository(), {"audio.speak": skill}, None, None,
                          RuntimeOptions(cancel_grace_s=0.2))
    await ports.prepare()
    ctx = context()
    execution = asyncio.create_task(ports.skills["audio.speak"].execute(ctx, {"message": "hello"}))
    try:
        method, _, respond = await asyncio.wait_for(api.requests.get(), 1)
        assert method == "cc.api.work.start"
        respond(Response(result={"workId": "speech"}))
        ctx.cancel_event.set()
        method, params, respond = await asyncio.wait_for(api.requests.get(), 1)
        assert method == "cc.api.work.ctrl" and params["workId"] == "speech"
        respond(Response(result={}))
        await checkpoint()
        assert not execution.done()
        api.services["cc.api.work.finished"]("event", {"workId": "speech"})
        with pytest.raises((TaskCancelled, asyncio.CancelledError)):
            await asyncio.wait_for(execution, 1)
    finally:
        await ports.close()
        await skill.close()


async def test_cancelled_repository_read_does_not_poison_close_or_later_writes() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()

    class WaitingRepository(InMemoryTaskRepository):
        async def get_task(self, task_id: str) -> Task | None:
            loop.call_soon_threadsafe(entered.set)
            await asyncio.Event().wait()
            return None

    ports = isolate_ports(WaitingRepository(), {}, None, None, RuntimeOptions())
    await ports.prepare()
    read = asyncio.create_task(ports.repository.get_task("task"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
        assert (await ports.repository.create_task(task())).id == "task"
        await ports.repository.close()
    finally:
        await ports.close()


async def test_cancelled_repository_write_returns_authoritative_completion() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    class SlowWriteRepository(InMemoryTaskRepository):
        async def create_task(self, value: Task) -> Task:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return await super().create_task(value)

    ports = isolate_ports(SlowWriteRepository(), {}, None, None, RuntimeOptions())
    await ports.prepare()
    write = asyncio.create_task(ports.repository.create_task(task()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        write.cancel()
        await checkpoint()
        release.set()
        assert (await asyncio.wait_for(write, 1)).id == "task"
        assert (await ports.repository.get_task("task")).id == "task"
    finally:
        release.set()
        await ports.close()


async def test_progress_is_delivered_before_normal_completion_and_late_calls_are_ignored() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    class ProgressSkill:
        definition = definition()
        saved: Callable[[float], None] | None = None

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            assert ctx.progress is not None
            self.saved = ctx.progress
            for index in range(1000):
                ctx.progress(index / 999)
            return SkillResult(success=True)

    original = ProgressSkill()
    values: list[float] = []
    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": original}, None, None,
                          RuntimeOptions())
    await ports.prepare()
    try:
        await ports.skills["test.skill"].execute(context(values.append), {})
        assert values and values[-1] == 1.0
        before = list(values)
        assert original.saved is not None
        original.saved(0.5)
        await checkpoint()
        assert values == before
    finally:
        await ports.close()


async def test_swallowed_cancellation_is_unresponsive_until_real_task_stops() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    cancellation_seen = asyncio.Event()
    owner: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    class ResistantSkill:
        definition = definition()

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            release = asyncio.Event()
            owner.append((asyncio.get_running_loop(), release))
            loop.call_soon_threadsafe(entered.set)
            try:
                await release.wait()
            except asyncio.CancelledError:
                loop.call_soon_threadsafe(cancellation_seen.set)
                await release.wait()
            return SkillResult(success=True)

    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": ResistantSkill()}, None,
                          None, RuntimeOptions(cancel_grace_s=0.03))
    await ports.prepare()
    execution = asyncio.create_task(ports.skills["test.skill"].execute(context(), {}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        execution.cancel()
        with pytest.raises(ExecutionUnresponsive):
            await asyncio.wait_for(execution, 0.3)
        assert cancellation_seen.is_set()
    finally:
        owner[0][0].call_soon_threadsafe(owner[0][1].set)
        await ports.close()


async def test_queued_repository_write_cannot_start_after_another_call_times_out() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    class Repository(InMemoryTaskRepository):
        writes = 0

        async def get_task(self, task_id: str) -> Task | None:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return None

        async def create_task(self, value: Task) -> Task:
            self.writes += 1
            return await super().create_task(value)

    original = Repository()
    ports = isolate_ports(original, {}, None, None, RuntimeOptions(port_timeout_s=0.03))
    await ports.prepare()
    read = asyncio.create_task(ports.repository.get_task("task"))
    pending: asyncio.Task[Task] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        pending = asyncio.create_task(ports.repository.create_task(task()))
        with pytest.raises(ExtensionTimeout):
            await read
        release.set()
        with pytest.raises(ExtensionTimeout):
            await pending
        assert original.writes == 0
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await ports.close()


async def test_non_json_payload_and_custom_exception_never_cross_into_kernel() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    class UnsafeError(Exception):
        def __str__(self) -> str:
            return "external message"

    class UnsafeValue:
        def __deepcopy__(self, memo: dict[int, Any]) -> Any:
            return self

    class BrokenError(Exception):
        def __str__(self) -> str:
            raise SystemExit("exception formatter failed")

    class UnsafeSkill:
        definition = definition()

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            if params.get("broken_error"):
                raise BrokenError()
            if params.get("error"):
                raise UnsafeError()
            return SkillResult(success=True, output={"unsafe": UnsafeValue()})

    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": UnsafeSkill()}, None, None,
                          RuntimeOptions())
    await ports.prepare()
    try:
        for params in ({}, {"error": True}, {"broken_error": True}):
            with pytest.raises(InternalError) as caught:
                await ports.skills["test.skill"].execute(context(), params)
            assert type(caught.value) is InternalError
    finally:
        await ports.close()


async def test_validation_runs_on_fixed_bounded_worker_and_keeps_policy_rules() -> None:
    from unittest.mock import patch

    from robot_agent_runtime.errors import SchemaValidationError
    from robot_agent_runtime.isolation import isolate_ports
    from robot_agent_runtime.policy import PolicyEnforcer

    class Skill:
        definition = definition()
        definition.input_schema = {
            "type": "object", "properties": {"field": {"type": "string"}},
            "required": ["field"],
        }

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            return SkillResult(success=True)

    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": Skill()}, None, None,
                          RuntimeOptions(port_timeout_s=0.03))
    await ports.prepare()
    release = threading.Event()
    worker_ids: list[int] = []
    original = PolicyEnforcer.validate

    def blocking_validate(self: PolicyEnforcer, call: SkillCall,
                          unsatisfied: frozenset[str]) -> Any:
        worker_ids.append(threading.get_ident())
        release.wait()
        return original(self, call, unsatisfied)

    try:
        with patch.object(PolicyEnforcer, "validate", blocking_validate):
            with pytest.raises(ExtensionTimeout):
                await asyncio.wait_for(
                    ports.validate(SkillCall(skill="test.skill", params={"field": "ok"}),
                                   frozenset(), None), 0.3
                )
        assert worker_ids and worker_ids[0] != threading.get_ident()
        release.set()
        await ports.validate(SkillCall(skill="test.skill", params={"field": "ok"}),
                             frozenset(), None)
        with pytest.raises(SchemaValidationError):
            await ports.validate(SkillCall(skill="test.skill", params={"field": 1}),
                                 frozenset(), None)
    finally:
        release.set()
        await ports.close()


async def test_blocked_state_watch_close_is_bounded_until_real_pull_finishes() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    class BlockingWatch:
        async def unsatisfied(self) -> frozenset[str]:
            return frozenset()

        async def watch(self) -> AsyncIterator[frozenset[str]]:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            yield frozenset()

    ports = isolate_ports(InMemoryTaskRepository(), {}, None, BlockingWatch(),
                          RuntimeOptions(cancel_grace_s=0.03))
    await ports.prepare()
    assert ports.robot_state is not None
    iterator = ports.robot_state.watch()
    pending = asyncio.create_task(anext(iterator))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await ports.repository.get_task("task") is None
        await asyncio.wait_for(ports.close(), 0.3)
        assert not pending.done()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 1)
        await iterator.aclose()
        await ports.close()


async def test_precancelled_skill_never_dispatches_external_execute() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    class Action:
        definition = definition()
        calls = 0

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            self.calls += 1
            return SkillResult(success=True)

    original = Action()
    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": original}, None, None,
                          RuntimeOptions())
    await ports.prepare()
    try:
        ctx = context()
        ctx.cancel_event.set()
        for _ in range(50):
            with pytest.raises((TaskCancelled, asyncio.CancelledError)):
                await ports.skills["test.skill"].execute(ctx, {})
        assert original.calls == 0
    finally:
        await ports.close()


async def test_progress_conversion_cannot_hold_a_lock_needed_by_kernel_close() -> None:
    from unittest.mock import patch

    from robot_agent_runtime.isolation import _ProgressBridge, isolate_ports

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    monitor_finished = asyncio.Event()
    release = threading.Event()
    close_entered = threading.Event()
    responsive: list[bool] = []
    original_close = _ProgressBridge.close

    class BlockingFloat(float):
        def __float__(self) -> float:
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return 0.5

    class Skill:
        definition = definition()

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            assert ctx.progress is not None
            ctx.progress(BlockingFloat(0.5))
            return SkillResult(success=True)

    def observed_close(self: _ProgressBridge) -> None:
        close_entered.set()
        original_close(self)

    def monitor() -> None:
        ready = threading.Event()
        try:
            if close_entered.wait(1):
                loop.call_soon_threadsafe(ready.set)
                responsive.append(ready.wait(0.2))
            else:
                responsive.append(False)
        finally:
            release.set()
            loop.call_soon_threadsafe(monitor_finished.set)

    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": Skill()}, None, None,
                          RuntimeOptions(cancel_grace_s=0.03))
    await ports.prepare()
    observer = threading.Thread(target=monitor, daemon=True)
    try:
        with patch.object(_ProgressBridge, "close", observed_close):
            execution = asyncio.create_task(ports.skills["test.skill"].execute(
                context(lambda value: None), {}
            ))
            await asyncio.wait_for(entered.wait(), 1)
            observer.start()
            execution.cancel()
            with pytest.raises(ExecutionUnresponsive):
                await asyncio.wait_for(execution, 1)
            await asyncio.wait_for(monitor_finished.wait(), 1)
            assert responsive == [True]
    finally:
        release.set()
        await ports.close()


def test_bounded_snapshot_rejects_size_depth_and_cycles_before_copying() -> None:
    from robot_agent_runtime.boundary import bounded_snapshot

    for value, max_bytes, max_depth in (
        ({"large": "x" * 1000}, 100, 64),
        ([[["nested"]]], 1000, 2),
    ):
        with pytest.raises(SnapshotTooLarge):
            bounded_snapshot(value, max_bytes=max_bytes, max_depth=max_depth)
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pytest.raises(InternalError):
        bounded_snapshot(cyclic, max_bytes=1000, max_depth=64)


def test_bounded_snapshot_uses_plain_types_without_external_copy_or_dump_hooks() -> None:
    from robot_agent_runtime.boundary import bounded_snapshot

    class HostileString(str):
        def __str__(self) -> str:
            raise AssertionError("string hook must not execute")

    class HostileList(list[Any]):
        def __iter__(self) -> Any:
            raise AssertionError("iterator hook must not execute")

        def __deepcopy__(self, memo: dict[int, Any]) -> Any:
            raise AssertionError("copy hook must not execute")

    class HostileResult(SkillResult):
        def model_dump_json(self, **kwargs: Any) -> str:
            raise AssertionError("serializer hook must not execute")

    original = HostileResult(success=True)
    original.output["values"] = HostileList([HostileString("safe")])
    copied = bounded_snapshot(original, max_bytes=1000, max_depth=64)
    assert type(copied) is SkillResult
    assert type(copied.output["values"]) is list
    assert type(copied.output["values"][0]) is str
    copied.output["values"].append("new")
    assert len(original.output["values"]) == 1


async def test_all_extension_exception_branches_are_bounded_and_have_no_external_chain() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    class UnsafeException(Exception):
        pass

    class BrokenSkill:
        definition = definition()

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            kind = params["kind"]
            if kind == "builtin":
                raise ValueError("x" * 4000)
            if kind == "runtime":
                raise TaskCancelled("stopped", {"large": "x" * 4000})
            if kind == "result":
                return SkillResult(success=True, output={"large": "x" * 4000})
            raise UnsafeException("external")

    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": BrokenSkill()}, None,
                          None, RuntimeOptions(max_snapshot_bytes=1000))
    await ports.prepare()
    try:
        for kind in ("builtin", "runtime", "result"):
            with pytest.raises(SnapshotTooLarge) as caught:
                await ports.skills["test.skill"].execute(context(), {"kind": kind})
            assert caught.value.__context__ is None
            assert caught.value.__cause__ is None
        with pytest.raises(InternalError) as caught:
            await ports.skills["test.skill"].execute(context(), {"kind": "custom"})
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None
    finally:
        await ports.close()


async def test_noncancellable_skill_waits_for_natural_completion_without_injecting_cancel() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    entered, cancellation_seen = asyncio.Event(), asyncio.Event()
    worker: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    class PhysicalAction:
        definition = definition()
        definition.supports_cancel = False

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            release = asyncio.Event()
            worker.append((asyncio.get_running_loop(), release))
            loop.call_soon_threadsafe(entered.set)
            try:
                await release.wait()
            except asyncio.CancelledError:
                loop.call_soon_threadsafe(cancellation_seen.set)
                raise
            return SkillResult(success=True)

    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": PhysicalAction()},
                          None, None, RuntimeOptions(cancel_grace_s=0.03))
    await ports.prepare()
    execution = asyncio.create_task(ports.skills["test.skill"].execute(context(), {}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        execution.cancel()
        with pytest.raises(ExecutionUnresponsive):
            await asyncio.wait_for(execution, 0.3)
        assert not cancellation_seen.is_set()
        await ports.close()
        assert not cancellation_seen.is_set()
    finally:
        worker[0][0].call_soon_threadsafe(worker[0][1].set)
        await ports.close()


async def test_started_skill_cancelled_error_is_an_unconfirmed_physical_stop() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()

    class UnconfirmedAction:
        definition = definition()

        async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> SkillResult:
            loop.call_soon_threadsafe(entered.set)
            await asyncio.Event().wait()
            return SkillResult(success=True)

    ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": UnconfirmedAction()},
                          None, None, RuntimeOptions(cancel_grace_s=0.03))
    await ports.prepare()
    execution = asyncio.create_task(ports.skills["test.skill"].execute(context(), {}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        execution.cancel()
        with pytest.raises(ExecutionUnresponsive):
            await asyncio.wait_for(execution, 0.3)
        with pytest.raises(ExecutionUnresponsive):
            await ports.skills["test.skill"].execute(context(), {})
    finally:
        await ports.close()


async def test_cancelled_repository_lifecycle_cannot_overlap_retry_or_cleanup() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    for operation in ("start", "close"):
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()

        class Repository(InMemoryTaskRepository):
            starts = 0
            closes = 0

            async def start(self) -> None:
                self.starts += 1
                if operation == "start":
                    loop.call_soon_threadsafe(entered.set)
                    release.wait()

            async def close(self) -> None:
                self.closes += 1
                if operation == "close":
                    loop.call_soon_threadsafe(entered.set)
                    release.wait()

        original = Repository()
        ports = isolate_ports(original, {}, None, None, RuntimeOptions(port_timeout_s=0.03))
        await ports.prepare()
        pending = asyncio.create_task(getattr(ports.repository, operation)())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            pending.cancel()
            with pytest.raises(ExtensionTimeout):
                await asyncio.wait_for(pending, 0.3)
            with pytest.raises(ExtensionTimeout):
                await ports.repository.start()
            await ports.repository.close()
            assert original.starts == (1 if operation == "start" else 0)
            assert original.closes == (1 if operation == "close" else 0)
        finally:
            release.set()
            await ports.close()


async def test_legacy_repository_event_page_is_sliced_in_worker_before_snapshot() -> None:
    from robot_agent_runtime.isolation import isolate_ports
    from robot_agent_runtime.models import Event

    class LegacyRepository:
        async def list_events(self, task_id: str, after_sequence: int = 0) -> list[Event]:
            return [Event(event_id=str(i), task_id=task_id, sequence=i, event_type="step",
                          data={"large": "x" * 400}) for i in range(after_sequence + 1, 11)]

    ports = isolate_ports(LegacyRepository(), {}, None, None,
                          RuntimeOptions(max_snapshot_bytes=2000))
    await ports.prepare()
    try:
        page = await ports.repository.list_events_page("task", 2, 2)
        assert [event.sequence for event in page] == [3, 4]
    finally:
        await ports.close()


def test_bounded_snapshot_rejects_foreign_objects_without_reading_their_class_hook() -> None:
    from robot_agent_runtime.boundary import bounded_snapshot

    class UnsafeValue:
        @property
        def __class__(self) -> type[Any]:
            raise AssertionError("foreign class hook must not execute")

    with pytest.raises(InternalError):
        bounded_snapshot(UnsafeValue(), max_bytes=1000, max_depth=64)


async def test_control_port_call_has_capacity_and_worker_context_when_normal_slot_is_full() -> None:
    from robot_agent_runtime.execution_context import CONTROL_OPERATION
    from robot_agent_runtime.isolation import isolate_ports

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    worker: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []

    class Repository(InMemoryTaskRepository):
        async def get_task(self, task_id: str) -> Task | None:
            release = asyncio.Event()
            worker.append((asyncio.get_running_loop(), release))
            loop.call_soon_threadsafe(entered.set)
            await release.wait()
            return None

        async def list_events_page(self, task_id: str, after_sequence: int = 0,
                                   limit: int = 128) -> list[Any]:
            assert CONTROL_OPERATION.get()
            assert limit == 2
            return []

        async def list_events(self, task_id: str, after_sequence: int = 0) -> list[Any]:
            raise AssertionError("native paging must be preferred")

    ports = isolate_ports(Repository(), {}, None, None,
                          RuntimeOptions(max_pending_calls=1, max_control_calls=1))
    await ports.prepare()
    read = asyncio.create_task(ports.repository.get_task("task"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        token = CONTROL_OPERATION.set(True)
        try:
            assert await ports.repository.list_events_page("task", limit=2) == []
        finally:
            CONTROL_OPERATION.reset(token)
    finally:
        worker[0][0].call_soon_threadsafe(worker[0][1].set)
        await read
        await ports.close()


async def test_invalid_cancellation_results_poison_skill_before_another_dispatch() -> None:
    from robot_agent_runtime.isolation import isolate_ports

    for returned in ({}, [], None, SkillResult(success=False)):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        class Action:
            definition = definition()
            calls = 0

            async def execute(self, ctx: SkillContext, params: dict[str, Any]) -> Any:
                self.calls += 1
                if self.calls > 1:
                    return SkillResult(success=True)
                loop.call_soon_threadsafe(entered.set)
                await ctx.cancel_event.wait()
                return returned

        original = Action()
        ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": original}, None,
                              None, RuntimeOptions(cancel_grace_s=0.03))
        await ports.prepare()
        ctx = context()
        execution = asyncio.create_task(ports.skills["test.skill"].execute(ctx, {}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            ctx.cancel_event.set()
            with pytest.raises(ExecutionUnresponsive):
                await asyncio.wait_for(execution, 0.3)
            with pytest.raises(ExecutionUnresponsive):
                await ports.skills["test.skill"].execute(context(), {})
            assert original.calls == 1
        finally:
            await ports.close()


async def test_simultaneous_cancellation_and_completion_requires_confirmed_stop() -> None:
    from unittest.mock import patch

    from robot_agent_runtime.isolation import isolate_ports

    original_wait = asyncio.wait

    async def both_ready(tasks: Any, **kwargs: Any) -> Any:
        return await original_wait(tasks, return_when=asyncio.ALL_COMPLETED)

    for returned in ({}, SkillResult(success=False), SkillResult(success=True)):
        loop = asyncio.get_running_loop()
        ctx = context()

        class Action:
            definition = definition()
            calls = 0

            async def execute(self, local: SkillContext, params: dict[str, Any]) -> Any:
                self.calls += 1
                loop.call_soon_threadsafe(ctx.cancel_event.set)
                return returned

        original = Action()
        ports = isolate_ports(InMemoryTaskRepository(), {"test.skill": original}, None,
                              None, RuntimeOptions(cancel_grace_s=0.03))
        await ports.prepare()
        confirmed = type(returned) is SkillResult and returned.success
        try:
            with patch("robot_agent_runtime.isolation.asyncio.wait", both_ready):
                expected = TaskCancelled if confirmed else ExecutionUnresponsive
                with pytest.raises(expected):
                    await asyncio.wait_for(ports.skills["test.skill"].execute(ctx, {}), 0.3)
            if not confirmed:
                with pytest.raises(ExecutionUnresponsive):
                    await ports.skills["test.skill"].execute(context(), {})
                assert original.calls == 1
        finally:
            await ports.close()


async def test_host_control_query_cannot_consume_repository_lifecycle_capacity() -> None:
    from contextlib import suppress

    from robot_agent_runtime.sdk import RobotRuntime

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()

    class Repository(InMemoryTaskRepository):
        close_count = 0
        read_count = 0

        async def get_task(self, task_id: str) -> Task | None:
            self.read_count += 1
            loop.call_soon_threadsafe(entered.set)
            await asyncio.Future()
            return None

        async def close(self) -> None:
            self.close_count += 1

    original = Repository()
    runtime = RobotRuntime(original, {}, options=RuntimeOptions(max_control_calls=1,
                                                               cancel_grace_s=0.03))
    await runtime.start()
    query = asyncio.create_task(runtime.get_task_status("hold"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(ExtensionCapacityExceeded):
            await runtime.get_task_status("another")
        assert original.read_count == 1
        await asyncio.wait_for(runtime.close(), 1)
        assert original.close_count == 1
    finally:
        query.cancel()
        await asyncio.gather(query, return_exceptions=True)
        with suppress(Exception):
            await runtime.close()
