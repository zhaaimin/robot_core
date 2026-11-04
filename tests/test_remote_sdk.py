import asyncio
import json
import os
import stat
import tempfile
from collections.abc import AsyncIterator
from concurrent.futures import Future
from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import robot_agent_runtime.wire as wire_module
import robot_agent_runtime.sdk as sdk_module
import robot_agent_runtime.server as server_module
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import InvalidRequest
from robot_agent_runtime.core import RuntimeCore
from robot_agent_runtime.kernel_host import KernelHost
from robot_agent_runtime.models import Event, SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sdk import RemoteRuntime, RemoteRuntimeError, RemoteTransportError, RobotRuntime
from robot_agent_runtime.server import RuntimeServer


class HostedCoreClient:
    """Keep integration tests on the server's actual kernel owner loop."""

    def __init__(self, host: KernelHost) -> None:
        self._host = host

    async def submit_task(self, value: TaskRequest) -> Task:
        return await self._host.call("submit_task", value)

    async def get_task_status(self, task_id: str) -> Task:
        return await self._host.call("get_task_status", task_id)

    def subscribe_task_events(self, task_id: str) -> AsyncIterator[Event]:
        return self._host.subscribe_task_events(task_id)


def request(task_type: str = "state", skill: str = "robot.get_state") -> TaskRequest:
    params = {"delay_s": 30} if skill == "wait.delay" else {}
    return TaskRequest(task_type=task_type, steps=[SkillCall(skill=skill, params=params)])


async def wait_for_terminal(
    runtime: RobotRuntime | RemoteRuntime | HostedCoreClient, task_id: str
) -> Task:
    async with asyncio.timeout(1):
        while True:
            task = await runtime.get_task_status(task_id)
            if task.state.is_terminal:
                return task
            await asyncio.sleep(0)


async def collect_through_terminal(
    runtime: RobotRuntime | RemoteRuntime, task_id: str, after_sequence: int = 0
) -> list[Event]:
    events: list[Event] = []
    async with asyncio.timeout(1):
        async for event in runtime.subscribe_task_events(task_id, after_sequence):
            events.append(event)
    return events


@asynccontextmanager
async def running_server(
    repository: InMemoryTaskRepository | None = None,
) -> AsyncIterator[tuple[RuntimeServer, HostedCoreClient, str, Simulator]]:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "s.sock")
        simulator = Simulator()
        core = RuntimeCore(repository or InMemoryTaskRepository(), simulator.skills)
        server = RuntimeServer(core, socket_path)
        await server.start()
        try:
            yield server, HostedCoreClient(server._host), socket_path, simulator
        finally:
            await server.close()


async def remote_request(socket_path: str, payload: bytes) -> list[dict[str, Any]]:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(payload)
    await writer.drain()
    writer.write_eof()
    messages = []
    async with asyncio.timeout(1):
        while line := await reader.readline():
            messages.append(json.loads(line))
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()
    return messages


async def test_remote_full_api_and_health() -> None:
    async with running_server() as (_, _, socket_path, simulator):
        remote = RemoteRuntime(socket_path)
        await remote.start()
        try:
            skills = await remote.list_skills()
            assert {skill.name for skill in skills} == set(simulator.skills)
            submitted = await remote.submit_task(request())
            final = await wait_for_terminal(remote, submitted.id)
            assert final.state == TaskState.SUCCEEDED
            assert final.created_at.tzinfo == UTC
            assert final.updated_at.tzinfo == UTC
            events = await collect_through_terminal(remote, submitted.id)
            assert events[0].event_type == "TASK_CREATED"
            assert events[-1].event_type == "TASK_SUCCEEDED"
            assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        finally:
            await remote.close()


async def test_remote_start_health_failure_is_a_transport_error() -> None:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        remote = RemoteRuntime(str(Path(directory) / "missing.sock"))
        with pytest.raises(RemoteTransportError):
            await remote.start()
        await remote.close()


@pytest.mark.parametrize("response", [b"not-json\n", b""])
async def test_remote_rejects_malformed_response_and_eof(response: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "s.sock")

        async def handle(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(response)
            await writer.drain()
            writer.close()

        fake = await asyncio.start_unix_server(handle, path=socket_path)
        try:
            with pytest.raises(RemoteTransportError):
                await RemoteRuntime(socket_path).start()
        finally:
            fake.close()
            await fake.wait_closed()


async def test_remote_business_errors_preserve_code_message_details() -> None:
    async with running_server() as (_, _, socket_path, _):
        remote = RemoteRuntime(socket_path)
        await remote.start()
        try:
            with pytest.raises(RemoteRuntimeError) as caught:
                await remote.get_task_status("missing")
            assert caught.value.info.code == "TASK_NOT_FOUND"
            assert caught.value.info.message == "Task not found"
            assert caught.value.info.details == {"task_id": "missing"}

            with pytest.raises(RemoteRuntimeError) as invalid:
                await remote.subscribe_task_events("task", -1).__anext__()
            assert invalid.value.info.code == "INVALID_REQUEST"
        finally:
            await remote.close()


async def test_remote_cancellation_matches_embedded() -> None:
    async with running_server() as (_, _, socket_path, _):
        remote = RemoteRuntime(socket_path)
        await remote.start()
        try:
            submitted = await remote.submit_task(request("wait", "wait.delay"))
            cancelled = await remote.cancel_task(submitted.id)
            assert cancelled.state == TaskState.CANCELED
            assert cancelled.cancel_requested
            events = await collect_through_terminal(remote, submitted.id)
            assert events[-1].event_type == "TASK_CANCELED"
        finally:
            await remote.close()


async def test_local_remote_multistep_final_state_and_event_types_match() -> None:
    async with running_server() as (_, _, socket_path, _):
        local = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
        remote = RemoteRuntime(socket_path)
        await local.start()
        await remote.start()
        try:
            multi = TaskRequest(
                task_type="patrol",
                steps=[
                    SkillCall(skill="navigation.navigate_to", params={"target": "dock"}),
                    SkillCall(skill="audio.speak", params={"message": "arrived"}),
                ],
            )
            local_task, remote_task = await asyncio.gather(
                local.submit_task(multi), remote.submit_task(multi)
            )
            local_final, remote_final = await asyncio.gather(
                wait_for_terminal(local, local_task.id),
                wait_for_terminal(remote, remote_task.id),
            )
            local_events, remote_events = await asyncio.gather(
                collect_through_terminal(local, local_task.id),
                collect_through_terminal(remote, remote_task.id),
            )
            assert local_final.state == remote_final.state == TaskState.SUCCEEDED
            assert local_final.result == remote_final.result
            assert [event.event_type for event in local_events] == [
                event.event_type for event in remote_events
            ]
        finally:
            await remote.close()
            await local.close()


async def test_remote_completed_replay_ends_naturally_and_wire_stream_sends_done() -> None:
    async with running_server() as (_, core, socket_path, _):
        submitted = await core.submit_task(request())
        await wait_for_terminal(core, submitted.id)
        remote = RemoteRuntime(socket_path)
        await remote.start()
        try:
            events: list[Event] = []
            async with asyncio.timeout(1):
                async for event in remote.subscribe_task_events(submitted.id):
                    events.append(event)
            assert events[-1].event_type == "TASK_SUCCEEDED"
        finally:
            await remote.close()

        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(
            json.dumps(
                {
                    "id": "replay",
                    "method": "subscribe_task_events",
                    "params": {"task_id": submitted.id},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        messages: list[dict[str, Any]] = []
        async with asyncio.timeout(1):
            while line := await reader.readline():
                messages.append(json.loads(line))
        writer.close()
        await writer.wait_closed()
        assert messages[-1] == {"id": "replay", "ok": True, "type": "done"}
        assert messages[-2]["event"]["event_type"] == "TASK_SUCCEEDED"


async def test_remote_live_subscription_ends_after_cancel_terminal_event() -> None:
    async with running_server() as (_, _, socket_path, _):
        remote = RemoteRuntime(socket_path)
        await remote.start()
        try:
            submitted = await remote.submit_task(request("wait", "wait.delay"))
            subscription = remote.subscribe_task_events(submitted.id)
            events: list[Event] = []
            async with asyncio.timeout(1):
                async for event in subscription:
                    events.append(event)
                    if event.event_type == "SKILL_STARTED":
                        await remote.cancel_task(submitted.id)
            assert events[-1].event_type == "TASK_CANCELED"
        finally:
            await remote.close()


@pytest.mark.parametrize(
    ("state", "event_type"),
    [
        (TaskState.SUCCEEDED, "TASK_SUCCEEDED"),
        (TaskState.FAILED, "TASK_FAILED"),
        (TaskState.CANCELED, "TASK_CANCELED"),
        (TaskState.UNKNOWN, "TASK_RECONCILED"),
        (TaskState.RECONCILIATION_REQUIRED, "TASK_RECOVERY_REQUIRED"),
    ],
)
async def test_terminal_replay_semantics_match_for_local_and_remote(
    state: TaskState, event_type: str
) -> None:
    repository = InMemoryTaskRepository()
    task = Task(
        id="terminal",
        task_type="terminal",
        state=state,
        steps=[SkillCall(skill="robot.get_state")],
    )
    await repository.create_task(task)
    await repository.append_event(
        Event(
            event_id=f"event-{state.value}",
            task_id=task.id,
            sequence=1,
            event_type=event_type,
            data={"state": state.value},
        )
    )
    async with running_server(repository) as (_, core, socket_path, _):
        local_events: list[Event] = []
        async with asyncio.timeout(1):
            async for event in core.subscribe_task_events(task.id):
                local_events.append(event)

        remote = RemoteRuntime(socket_path)
        await remote.start()
        try:
            remote_events: list[Event] = []
            async with asyncio.timeout(1):
                async for event in remote.subscribe_task_events(task.id):
                    remote_events.append(event)
        finally:
            await remote.close()

        assert [event.event_type for event in local_events] == [event_type]
        assert [event.event_type for event in remote_events] == [event_type]
        assert local_events[-1].data["state"] == state.value
        assert remote_events[-1].data["state"] == state.value


async def test_remote_replays_after_sequence_without_duplicates() -> None:
    async with running_server() as (_, _, socket_path, _):
        remote = RemoteRuntime(socket_path)
        await remote.start()
        try:
            submitted = await remote.submit_task(request())
            await wait_for_terminal(remote, submitted.id)
            events = await collect_through_terminal(remote, submitted.id, after_sequence=3)
            assert events[0].sequence == 4
            assert [event.sequence for event in events] == list(range(4, events[-1].sequence + 1))
        finally:
            await remote.close()


async def test_ndjson_health_empty_lines_and_socket_permissions() -> None:
    async with running_server() as (_, _, socket_path, _):
        assert stat.S_IMODE(os.stat(socket_path).st_mode) == 0o600
        response = await remote_request(
            socket_path,
            b'\n\n{"id":"h1","method":"health","params":{}}\n',
        )
        assert response == [
            {
                "id": "h1",
                "ok": True,
                "result": {
                    "status": "ok",
                    "service": "robot-agent-runtime",
                    "version": "0.1.0",
                },
            }
        ]


@pytest.mark.parametrize(
    ("payload", "request_id"),
    [
        (b"not-json\n", None),
        (b'{"id":"u","method":"unknown","params":{}}\n', "u"),
        (b'{"id":"p","method":"get_task_status","params":{}}\n', "p"),
        (b'{"id":"v","method":"submit_task","params":{"request":{"task_type":"x","steps":[]}}}\n', "v"),
        (b'{"id":"a","method":"subscribe_task_events","params":{"task_id":"x","after_sequence":-1}}\n', "a"),
    ],
)
async def test_protocol_returns_structured_errors(payload: bytes, request_id: str | None) -> None:
    async with running_server() as (_, _, socket_path, _):
        response = await remote_request(socket_path, payload)
        assert len(response) == 1
        assert response[0]["id"] == request_id
        assert response[0]["ok"] is False
        assert response[0]["error"]["code"] == "INVALID_REQUEST"
        assert response[0]["error"]["message"]
        assert isinstance(response[0]["error"]["details"], dict)


async def test_protocol_maps_json_recursion_error_to_invalid_request() -> None:
    def raise_recursion(_: bytes) -> None:
        raise RecursionError("injected recursion")

    original_json = wire_module.json
    wire_module.json = SimpleNamespace(
        loads=raise_recursion,
        dumps=json.dumps,
        JSONDecodeError=json.JSONDecodeError,
    )
    try:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"id":"deep","method":"health"}\n')
        reader.feed_eof()
        try:
            await wire_module.read_message(reader)
        except InvalidRequest as error:
            mapped = error.info.code == "INVALID_REQUEST"
        except RecursionError:
            mapped = False
        assert mapped, "RecursionError was not mapped to InvalidRequest"
    finally:
        wire_module.json = original_json


async def test_protocol_rejects_oversized_message() -> None:
    async with running_server() as (_, _, socket_path, _):
        response = await remote_request(socket_path, b"x" * (1024 * 1024 + 1) + b"\n")
        assert response[0]["ok"] is False
        assert response[0]["error"]["code"] == "INVALID_REQUEST"
        assert "1 MiB" in response[0]["error"]["message"]


async def test_subscription_wire_format_sends_events_then_done_on_client_disconnect() -> None:
    async with running_server() as (_, core, socket_path, _):
        submitted = await core.submit_task(request())
        await wait_for_terminal(core, submitted.id)
        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(
            json.dumps(
                {
                    "id": "s1",
                    "method": "subscribe_task_events",
                    "params": {"task_id": submitted.id},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        first = json.loads(await asyncio.wait_for(reader.readline(), timeout=1))
        assert first["id"] == "s1"
        assert first["type"] == "event"
        assert first["event"]["event_type"] == "TASK_CREATED"
        writer.close()
        await writer.wait_closed()


async def test_concurrent_remote_clients_share_one_core() -> None:
    async with running_server() as (_, core, socket_path, simulator):
        remotes = [RemoteRuntime(socket_path) for _ in range(4)]
        await asyncio.gather(*(remote.start() for remote in remotes))
        try:
            tasks = await asyncio.gather(
                *(remote.submit_task(request()) for remote in remotes for _ in range(2))
            )
            finals = await asyncio.gather(
                *(wait_for_terminal(remotes[index % 4], task.id) for index, task in enumerate(tasks))
            )
            assert len({task.id for task in tasks}) == 8
            assert all(task.state == TaskState.SUCCEEDED for task in finals)
            assert simulator.skills["robot.get_state"].calls == 8
            assert core is not None
        finally:
            await asyncio.gather(*(remote.close() for remote in remotes))


async def test_client_close_does_not_stop_server_or_other_clients() -> None:
    async with running_server() as (_, core, socket_path, _):
        first = RemoteRuntime(socket_path)
        second = RemoteRuntime(socket_path)
        await first.start()
        await second.start()
        submitted = await core.submit_task(request("wait", "wait.delay"))
        subscription = first.subscribe_task_events(submitted.id)
        assert (await anext(subscription)).event_type == "TASK_CREATED"
        async def drain_client() -> None:
            async for _ in subscription:
                pass

        pending = asyncio.create_task(drain_client())
        await asyncio.sleep(0)
        await first.close()
        await asyncio.wait_for(pending, timeout=1)

        await second.start()
        other = await second.submit_task(request())
        assert (await wait_for_terminal(second, other.id)).state == TaskState.SUCCEEDED
        await second.close()


async def test_server_close_cleans_active_subscriptions_and_socket_idempotently() -> None:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "s.sock")
        core = RuntimeCore(InMemoryTaskRepository(), Simulator().skills)
        server = RuntimeServer(core, socket_path)
        await server.start()
        remote = RemoteRuntime(socket_path)
        await remote.start()
        submitted = await remote.submit_task(request("wait", "wait.delay"))
        subscription = remote.subscribe_task_events(submitted.id)
        assert (await anext(subscription)).event_type == "TASK_CREATED"
        async def drain_server() -> None:
            async for _ in subscription:
                pass

        pending = asyncio.create_task(drain_server())
        await asyncio.sleep(0)
        await server.close()
        await server.close()
        assert not os.path.exists(socket_path)
        with pytest.raises(RemoteTransportError):
            await asyncio.wait_for(pending, timeout=1)
        await remote.close()
        with pytest.raises(RuntimeError):
            await core.get_task_status(submitted.id)


async def test_server_close_is_cancellation_safe() -> None:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "s.sock")
        core = RuntimeCore(InMemoryTaskRepository(), Simulator().skills)
        server = RuntimeServer(core, socket_path)
        await server.start()
        closing = asyncio.create_task(server.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)
        await server.close()
        assert not os.path.exists(socket_path)
        with pytest.raises(RuntimeError):
            await core.list_skills()


async def test_server_never_removes_preexisting_regular_file() -> None:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        path = Path(directory) / "s.sock"
        path.write_text("keep")
        core = RuntimeCore(InMemoryTaskRepository(), Simulator().skills)
        server = RuntimeServer(core, str(path))
        with pytest.raises(RuntimeError, match="not a socket"):
            await server.start()
        await server.close()
        assert path.read_text() == "keep"
        await core.close()


async def test_failed_start_does_not_remove_someone_elses_socket() -> None:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "s.sock")
        existing = await asyncio.start_unix_server(lambda r, w: None, path=socket_path)
        core = RuntimeCore(InMemoryTaskRepository(), Simulator().skills)
        server = RuntimeServer(core, socket_path)
        try:
            with pytest.raises(OSError):
                await server.start()
            await server.close()
            assert stat.S_ISSOCK(os.stat(socket_path).st_mode)
        finally:
            existing.close()
            await existing.wait_closed()
            await core.close()


async def test_subscription_extra_input_is_bounded_and_terminates_without_eof() -> None:
    async with running_server() as (_, core, socket_path, _):
        submitted = await core.submit_task(request("wait", "wait.delay"))
        reader, writer = await asyncio.open_unix_connection(socket_path)
        writer.write(
            json.dumps(
                {
                    "id": "bounded",
                    "method": "subscribe_task_events",
                    "params": {"task_id": submitted.id},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        first = json.loads(await asyncio.wait_for(reader.readline(), timeout=1))
        assert first["type"] == "event"

        writer.write(b"x" * (2 * 1024 * 1024 + 1))
        try:
            await asyncio.wait_for(writer.drain(), timeout=1)
        except (ConnectionError, OSError):
            pass
        try:
            assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        except (ConnectionError, OSError):
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def test_remote_close_racing_paused_connect_closes_late_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with running_server() as (_, _, socket_path, _):
        remote = RemoteRuntime(socket_path)
        connected = asyncio.Event()
        release = asyncio.Event()
        real_open = sdk_module.asyncio.open_unix_connection

        async def paused_open(path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            reader, writer = await real_open(path)
            connected.set()
            await release.wait()
            return reader, writer

        monkeypatch.setattr(sdk_module.asyncio, "open_unix_connection", paused_open)
        starting = asyncio.create_task(remote.start())
        await asyncio.wait_for(connected.wait(), timeout=1)
        closing = [asyncio.create_task(remote.close()) for _ in range(2)]
        await asyncio.sleep(0)
        release.set()
        try:
            with pytest.raises(RemoteTransportError):
                await asyncio.wait_for(starting, timeout=1)
            await asyncio.gather(*closing)
            await remote.close()
            assert remote._writers == set()
        finally:
            release.set()
            await asyncio.gather(starting, *closing, return_exceptions=True)
            await remote.close()


async def test_server_chmod_failure_closes_listener_and_cleans_owned_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "s.sock")
        core = RuntimeCore(InMemoryTaskRepository(), Simulator().skills)
        server = RuntimeServer(core, socket_path)
        listeners: list[asyncio.Server] = []
        real_start = server_module.asyncio.start_unix_server

        async def tracked_start(*args: Any, **kwargs: Any) -> asyncio.Server:
            listener = await real_start(*args, **kwargs)
            listeners.append(listener)
            return listener

        def fail_chmod(_: str, __: int) -> None:
            raise PermissionError("injected chmod")

        monkeypatch.setattr(server_module.asyncio, "start_unix_server", tracked_start)
        monkeypatch.setattr(
            server_module,
            "os",
            SimpleNamespace(
                path=os.path, chmod=fail_chmod, lstat=os.lstat, unlink=os.unlink
            ),
        )
        try:
            with pytest.raises(PermissionError, match="injected chmod"):
                await server.start()
            assert listeners and not listeners[0].is_serving()
            assert not os.path.lexists(socket_path)
            with pytest.raises(RuntimeError):
                await core.list_skills()
        finally:
            for listener in listeners:
                listener.close()
                await listener.wait_closed()
            await server.close()
            await core.close()


async def test_server_post_ownership_lstat_failure_cleans_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "s.sock")
        core = RuntimeCore(InMemoryTaskRepository(), Simulator().skills)
        server = RuntimeServer(core, socket_path)
        listeners: list[asyncio.Server] = []
        real_start = server_module.asyncio.start_unix_server
        real_lstat = server_module.os.lstat
        calls = 0
        injecting = True

        async def tracked_start(*args: Any, **kwargs: Any) -> asyncio.Server:
            listener = await real_start(*args, **kwargs)
            listeners.append(listener)
            return listener

        def fail_second_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            nonlocal calls
            if injecting and path == socket_path:
                calls += 1
                if calls == 2:
                    raise OSError("injected lstat")
            return real_lstat(path, *args, **kwargs)

        monkeypatch.setattr(server_module.asyncio, "start_unix_server", tracked_start)
        monkeypatch.setattr(
            server_module,
            "os",
            SimpleNamespace(
                path=os.path,
                chmod=os.chmod,
                lstat=fail_second_lstat,
                unlink=os.unlink,
            ),
        )
        try:
            with pytest.raises(OSError, match="injected lstat"):
                await server.start()
            assert listeners and not listeners[0].is_serving()
            assert not os.path.lexists(socket_path)
            with pytest.raises(RuntimeError):
                await core.list_skills()
        finally:
            injecting = False
            for listener in listeners:
                listener.close()
                await listener.wait_closed()
            await server.close()
            await core.close()


async def test_server_failed_start_cleanup_does_not_delete_replacement_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "s.sock")
        core = RuntimeCore(InMemoryTaskRepository(), Simulator().skills)
        server = RuntimeServer(core, socket_path)
        listeners: list[asyncio.Server] = []
        real_start = server_module.asyncio.start_unix_server

        async def tracked_start(*args: Any, **kwargs: Any) -> asyncio.Server:
            listener = await real_start(*args, **kwargs)
            listeners.append(listener)
            return listener

        def replace_and_fail(path: str, _: int) -> None:
            os.unlink(path)
            Path(path).write_text("replacement")
            raise PermissionError("injected replacement")

        monkeypatch.setattr(server_module.asyncio, "start_unix_server", tracked_start)
        monkeypatch.setattr(
            server_module,
            "os",
            SimpleNamespace(
                path=os.path, chmod=replace_and_fail, lstat=os.lstat, unlink=os.unlink
            ),
        )
        try:
            with pytest.raises(PermissionError, match="injected replacement"):
                await server.start()
            assert listeners and not listeners[0].is_serving()
            assert Path(socket_path).read_text() == "replacement"
        finally:
            for listener in listeners:
                listener.close()
                await listener.wait_closed()
            await server.close()
            await core.close()


async def test_remote_subscription_overflow_preserves_business_error() -> None:
    class PausedReplayRepository(InMemoryTaskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.replay_started: Future[None] = Future()
            self.release_replay: Future[None] = Future()

        async def list_events_page(
            self, task_id: str, after_sequence: int = 0, limit: int = 128,
        ) -> list[Event]:
            if not self.replay_started.done():
                self.replay_started.set_result(None)
            await asyncio.wrap_future(self.release_replay)
            return await super().list_events_page(task_id, after_sequence, limit)

    repository = PausedReplayRepository()
    async with running_server(repository) as (server, _, socket_path, _):
        async def set_queue_size() -> None:
            server._core._events._queue_size = 1

        await server._host._loop.call(set_queue_size)
        remote = RemoteRuntime(socket_path)
        subscription = remote.subscribe_task_events("overflow")
        pending = asyncio.create_task(anext(subscription))
        await asyncio.wait_for(asyncio.wrap_future(repository.replay_started), timeout=1)

        async def publish() -> None:
            for sequence in (1, 2):
                server._core._events.publish(
                    Event(
                        event_id=f"overflow-{sequence}",
                        task_id="overflow",
                        sequence=sequence,
                        event_type="SKILL_PROGRESS",
                        data={"state": TaskState.RUNNING.value},
                    )
                )

        await server._host._loop.call(publish)
        repository.release_replay.set_result(None)
        try:
            with pytest.raises(RemoteRuntimeError) as caught:
                await asyncio.wait_for(pending, timeout=1)
            assert caught.value.info.code == "EVENT_STREAM_OVERFLOW"
            assert caught.value.info.details["after_sequence"] == 0
        finally:
            if not repository.release_replay.done():
                repository.release_replay.set_result(None)
            await subscription.aclose()
            await remote.close()


@pytest.mark.parametrize(
    "message",
    [
        {
            "type": "event",
            "event": {
                "event_id": "e",
                "task_id": "task",
                "sequence": 1,
                "event_type": "TASK_RUNNING",
            },
        },
        {"ok": "true", "type": "done"},
        {
            "ok": False,
            "error": {"code": "X", "message": "bad", "details": {}},
        },
        {
            "ok": False,
            "type": "event",
            "error": {"code": "X", "message": "bad", "details": {}},
        },
        {
            "ok": False,
            "type": "done",
            "error": {"code": "X", "message": "bad", "details": {}},
        },
        {"ok": True, "type": "done", "event": {}},
    ],
)
async def test_remote_rejects_invalid_stream_envelopes(message: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="rar-", dir="/tmp") as directory:
        socket_path = str(Path(directory) / "s.sock")

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            request_message = json.loads(await reader.readline())
            response = {**message, "id": request_message["id"]}
            try:
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        fake = await asyncio.start_unix_server(handle, path=socket_path)
        remote = RemoteRuntime(socket_path)
        try:
            with pytest.raises(RemoteTransportError):
                await asyncio.wait_for(
                    anext(remote.subscribe_task_events("task")), timeout=1
                )
        finally:
            await remote.close()
            fake.close()
            await fake.wait_closed()
