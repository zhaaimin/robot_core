import ast
import asyncio
import tempfile
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import InvalidRequest
from robot_agent_runtime.kernel.core import RuntimeCore
from robot_agent_runtime.models import SkillCall, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RemoteRuntime, RobotRuntime, RuntimeOptions, ThreadBinding
from robot_agent_runtime.transport.server import RuntimeServer


def request(name: str = "robot.get_state") -> TaskRequest:
    params = {"delay_s": 30} if name == "wait.delay" else {}
    return TaskRequest(task_type="ownership", steps=[SkillCall(skill=name, params=params)])


async def finish(runtime: Any, task_id: str) -> Any:
    async with asyncio.timeout(2):
        async for _ in runtime.subscribe_task_events(task_id):
            pass
        return await runtime.get_task_status(task_id)


def test_kernel_does_not_depend_on_hosting_or_thread_binding() -> None:
    folder = Path(__file__).resolve().parents[1] / "src/robot_agent_runtime/kernel"
    forbidden: list[tuple[str, str]] = []
    for path in folder.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = ([node.module] if isinstance(node, ast.ImportFrom) else
                       [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            for name in modules:
                if name and (name.startswith("robot_agent_runtime.hosting")
                             or name == "robot_agent_runtime.thread_binding"):
                    forbidden.append((path.name, name))
    assert forbidden == []


async def test_core_is_created_on_kernel_loop_with_final_dependencies_once() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills,
                           options=RuntimeOptions(event_page_size=3, max_event_buffer=7))
    assert runtime._host._core is None
    caller = threading.get_ident()
    constructed: list[int] = []
    original = RuntimeCore.__init__

    def create(core: RuntimeCore, *args: Any, **kwargs: Any) -> None:
        constructed.append(threading.get_ident())
        original(core, *args, **kwargs)

    with patch.object(RuntimeCore, "__init__", create):
        await runtime.start()
        try:
            core = runtime._host._core
            assert core is not None
            repository = core._repository
            assert core._state._repository is repository
            assert core._events._repository is repository
            assert core._events._page_size == 3
            assert core._events._queue_size == 7
            task = await runtime.submit_task(request())
            assert (await finish(runtime, task.id)).state == TaskState.SUCCEEDED
            await runtime.start()
            assert core._repository is repository
        finally:
            await runtime.close()
    assert len(constructed) == 1 and constructed[0] != caller


async def test_core_stop_preserves_repository_for_replay_until_close() -> None:
    class Repository(InMemoryTaskRepository):
        closes = 0

        async def close(self) -> None:
            self.closes += 1

    repository = Repository()
    core = RuntimeCore(repository, Simulator().skills)
    await core.start()
    task = await core.submit_task(request("wait.delay"))
    stream = core.subscribe_task_events(task.id)
    first = await anext(stream)
    try:
        await core.stop()
        await core.stop()
        assert repository.closes == 0
        saved = await repository.get_task(task.id)
        assert saved is not None and saved.state.is_terminal
        events = [first, *[event async for event in stream]]
        assert TaskState(events[-1].data["state"]).is_terminal
        with pytest.raises(InvalidRequest):
            await core.submit_task(request())
    finally:
        await core.close()
        await core.close()
    assert repository.closes == 1


async def test_stop_failure_does_not_skip_repository_close_or_repeat_stop() -> None:
    class Repository(InMemoryTaskRepository):
        closes = 0

        async def close(self) -> None:
            self.closes += 1

    repository = Repository()
    core = RuntimeCore(repository, Simulator().skills)
    await core.start()
    attempts = 0

    async def fail_stop() -> None:
        nonlocal attempts
        attempts += 1
        raise InvalidRequest("injected stop failure")

    core._stop_impl = fail_stop
    with pytest.raises(InvalidRequest):
        await core.stop()
    for _ in range(2):
        with pytest.raises(ExceptionGroup) as caught:
            await core.close()
        assert len(caught.value.exceptions) == 1
    assert repository.closes == attempts == 1


async def test_public_begin_close_starts_shutdown_without_waiting_for_completion() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    entered: Future[None] = Future()
    release: Future[None] = Future()
    original = runtime._host._shutdown

    async def paused() -> None:
        entered.set_result(None)
        await asyncio.wrap_future(release)
        await original()

    runtime._host._shutdown = paused
    try:
        assert await runtime._host.begin_close() is None
        assert await runtime._host.begin_close() is None
        await asyncio.wait_for(asyncio.wrap_future(entered), 1)
        with pytest.raises(InvalidRequest):
            await runtime.list_skills()
    finally:
        release.set_result(None)
        await runtime.close()


async def test_server_accepts_raw_bound_ports_and_constructs_its_own_core() -> None:
    simulator = Simulator()
    with tempfile.TemporaryDirectory(prefix="rar-owner-") as directory:
        path = str(Path(directory) / "s.sock")
        server = RuntimeServer(InMemoryTaskRepository(), {
            name: ThreadBinding.shared(skill) for name, skill in simulator.skills.items()
        }, path)
        remote = RemoteRuntime(path)
        assert server._host._core is None
        await server.start()
        try:
            await remote.start()
            task = await remote.submit_task(request())
            assert (await finish(remote, task.id)).state == TaskState.SUCCEEDED
        finally:
            await remote.close()
            await server.close()


async def test_cancelled_stop_waiter_does_not_repeat_or_skip_real_shutdown() -> None:
    core = RuntimeCore(InMemoryTaskRepository(), Simulator().skills)
    await core.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    original = core._stop_impl

    async def paused_stop() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await original()

    core._stop_impl = paused_stop
    stopping = asyncio.create_task(core.stop())
    await asyncio.wait_for(entered.wait(), 1)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    try:
        release.set()
        await core.stop()
        await core.close()
        assert calls == 1
    finally:
        release.set()
        await core.close()


async def test_host_orders_stop_replay_repository_close_and_loop_release() -> None:
    timeline: list[str] = []

    class Repository(InMemoryTaskRepository):
        async def close(self) -> None:
            timeline.append("repository.close")

        async def list_events_page(self, *args: Any, **kwargs: Any) -> Any:
            timeline.append("replay")
            return await super().list_events_page(*args, **kwargs)

    runtime = RobotRuntime(Repository(), Simulator().skills, options=RuntimeOptions(event_page_size=1))
    await runtime.start()
    core = runtime._core
    stopped = core._stop_impl

    async def stop() -> None:
        await stopped()
        timeline.append("core.stopped")

    core._stop_impl = stop
    task = await runtime.submit_task(request("wait.delay"))
    stream = runtime.subscribe_task_events(task.id)
    await anext(stream)
    timeline.clear()
    await runtime.close()
    assert timeline[0] == "core.stopped"
    assert "replay" in timeline
    assert timeline[-1] == "repository.close"
    assert runtime._host._ports is not None
    assert all(not runner._thread.is_alive() for runner in runtime._host._ports._runners)
    await stream.aclose()


async def test_failed_drain_still_closes_repository_and_all_external_loops() -> None:
    closes: list[str] = []

    class Repository(InMemoryTaskRepository):
        async def close(self) -> None:
            closes.append("closed")

    runtime = RobotRuntime(Repository(), Simulator().skills)
    await runtime.start()

    async def fail_drain() -> None:
        raise InvalidRequest("injected drain failure")

    runtime._host._drain_streams = fail_drain
    for _ in range(2):
        with pytest.raises(ExceptionGroup) as caught:
            await runtime.close()
        assert len(caught.value.exceptions) == 1
    assert closes == ["closed"]
    assert runtime._host._ports is not None
    assert all(not runner._thread.is_alive() for runner in runtime._host._ports._runners)


async def test_failed_preparation_does_not_construct_core_or_replace_workers() -> None:
    reads = 0

    class InvalidDeclaration:
        @property
        def definition(self) -> Any:
            nonlocal reads
            reads += 1
            raise InvalidRequest("injected declaration failure")

    runtime = RobotRuntime(InMemoryTaskRepository(), {"invalid": InvalidDeclaration()})
    for _ in range(2):
        with pytest.raises(InvalidRequest, match="injected declaration failure"):
            await runtime.start()
    assert runtime._host._core is None
    assert reads == 1
    with pytest.raises(InvalidRequest):
        await runtime.close()


async def test_schema_validation_remains_outside_kernel_with_injected_validator() -> None:
    entered: Future[None] = Future()
    release: Future[None] = Future()
    validation_threads: list[int] = []
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    from robot_agent_runtime.hosting.isolation import PolicyEnforcer

    original = PolicyEnforcer.validate

    def validate(policy: Any, *args: Any, **kwargs: Any) -> Any:
        validation_threads.append(threading.get_ident())
        entered.set_result(None)
        release.result(2)
        return original(policy, *args, **kwargs)

    with patch.object(PolicyEnforcer, "validate", validate):
        submission = asyncio.create_task(runtime.submit_task(request()))
        try:
            await asyncio.wait_for(asyncio.wrap_future(entered), 1)
            assert await asyncio.wait_for(runtime.list_skills(), 1)
            assert validation_threads[0] != runtime._host._loop._thread.ident
            release.set_result(None)
            task = await submission
            assert (await finish(runtime, task.id)).state == TaskState.SUCCEEDED
        finally:
            if not release.done():
                release.set_result(None)
            await runtime.close()
