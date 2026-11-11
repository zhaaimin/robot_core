import asyncio
import io
from contextlib import aclosing
from typing import Any

import pytest
from mcp import types

from robot_agent_runtime.errors import InvalidRequest
from robot_agent_runtime.models import Event, TaskState
from robot_runtime_mcp.client import McpRuntime, open_mcp_runtime
from robot_runtime_mcp.errors import McpTransportError
from test_mcp_bridge_stdio import request, runtime_server, wait_running


class ScriptedSession:
    """Malformed peer messages need a peer that deliberately breaks the contract."""

    def __init__(self, responses: list[types.CallToolResult]) -> None:
        self.responses = responses
        self.calls = 0

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        self.calls += 1
        return self.responses.pop(0)


def batch(*, events: list[dict[str, Any]], cursor: Any, done: Any = True) -> types.CallToolResult:
    return types.CallToolResult(content=[], structuredContent={
        "events": events, "next_sequence": cursor, "done": done,
    })


@pytest.mark.parametrize("invalid", [
    batch(events=[Event(event_id="e", task_id="other", sequence=1, event_type="X").model_dump(mode="json")], cursor=1),
    batch(events=[Event(event_id="e", task_id="t", sequence=1, event_type="X").model_dump(mode="json")] * 2, cursor=1),
    batch(events=[], cursor=1),
    batch(events=[], cursor=False),
    batch(events=[], cursor=0, done="true"),
    batch(events=[{**Event(event_id="e", task_id="t", sequence=1, event_type="X").model_dump(mode="json"),
                   "sequence": True}], cursor=1),
    batch(events=[{**Event(event_id="e", task_id="t", sequence=1, event_type="X").model_dump(mode="json"),
                   "sequence": "1"}], cursor=1),
    types.CallToolResult(content=[]),
    types.CallToolResult(content=[], structuredContent={"error": {"code": "X"}}, isError=True),
])
async def test_invalid_peer_batch_is_rejected_before_any_event_delivery(invalid: types.CallToolResult) -> None:
    session = ScriptedSession([invalid])
    runtime = McpRuntime(session)  # type: ignore[arg-type]
    async with aclosing(runtime.subscribe_task_events("t")) as stream:
        with pytest.raises(McpTransportError):
            await anext(stream)
    assert session.calls == 1


async def test_hanging_request_has_a_finite_deadline_and_no_retry() -> None:
    class HungSession(ScriptedSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            self.calls += 1
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    session = HungSession([])
    runtime = McpRuntime(session, request_timeout_s=0.01)  # type: ignore[arg-type]
    with pytest.raises(McpTransportError) as caught:
        await runtime.submit_task(request())
    assert caught.value.info.details["outcome_uncertain"] is True
    assert session.calls == 1


async def test_unavailable_endpoint_initialization_is_typed_and_bounded() -> None:
    async with asyncio.timeout(2):
        with pytest.raises(McpTransportError):
            async with open_mcp_runtime("/no-such-runtime.sock", request_timeout_s=0.1):
                raise AssertionError("cannot initialize")


async def test_sdk_captured_stderr_without_fileno_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    import robot_runtime_mcp.client as client_module

    unusable = io.StringIO()
    monkeypatch.setattr(client_module.sys, "stderr", unusable)
    monkeypatch.setattr(client_module.stdio_client.__wrapped__, "__defaults__", (unusable,))
    async with runtime_server() as (_, socket, _):
        async with open_mcp_runtime(socket) as runtime:
            assert await runtime.list_skills()


async def test_empty_allowlist_exposes_and_accepts_no_skills() -> None:
    async with runtime_server() as (_, socket, _):
        async with open_mcp_runtime(socket, allowed_skills=set()) as runtime:
            assert await runtime.list_skills() == []
            with pytest.raises(Exception) as caught:
                await runtime.submit_task(request())
            assert caught.value.info.code == "PERMISSION_DENIED"


async def test_cancelling_the_context_owner_preserves_cancellation_and_remote_task() -> None:
    ready = asyncio.Event()
    submitted_ids: list[str] = []
    async with runtime_server() as (_, socket, _):
        async def owner() -> None:
            async with open_mcp_runtime(socket) as runtime:
                task = await runtime.submit_task(request("wait.delay"))
                await wait_running(runtime, task.id)
                submitted_ids.append(task.id)
                ready.set()
                await asyncio.Event().wait()

        owning_task = asyncio.create_task(owner())
        async with asyncio.timeout(3):
            await ready.wait()
        owning_task.cancel()
        async with asyncio.timeout(3):
            with pytest.raises(asyncio.CancelledError):
                await owning_task
        async with open_mcp_runtime(socket) as runtime:
            assert (await runtime.get_task_status(submitted_ids[0])).state == TaskState.RUNNING
            await runtime.cancel_task(submitted_ids[0])


async def test_official_child_exits_after_context_close_and_failed_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp.client.stdio as stdio_module

    processes = []
    original_create = stdio_module._create_platform_compatible_process
    async def track_process(*args: Any, **kwargs: Any) -> Any:
        process = await original_create(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(stdio_module, "_create_platform_compatible_process", track_process)
    async with runtime_server() as (_, socket, _):
        async with open_mcp_runtime(socket) as runtime:
            assert await runtime.list_skills()
        assert processes[-1].returncode == 0
    with pytest.raises(McpTransportError):
        async with open_mcp_runtime("/missing-bridge-test.sock", request_timeout_s=0.1):
            raise AssertionError("unreachable")
    assert processes[-1].returncode is not None
    assert len(processes) == 2


async def test_handshake_deadline_terminates_a_child_waiting_for_runtime_health() -> None:
    writers: list[asyncio.StreamWriter] = []
    async def hang_health(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        await reader.read()
        writer.close()
        await writer.wait_closed()

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="mcp-hung-health-") as directory:
        socket = str(Path(directory) / "hung.sock")
        server = await asyncio.start_unix_server(hang_health, path=socket)
        try:
            async with asyncio.timeout(4):
                with pytest.raises(McpTransportError):
                    async with open_mcp_runtime(socket, request_timeout_s=0.1):
                        raise AssertionError("health response never arrives")
        finally:
            server.close()
            await server.wait_closed()
            for writer in writers:
                writer.close()
                await writer.wait_closed()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
async def test_invalid_connection_timeout_is_rejected_before_spawning(timeout: float) -> None:
    with pytest.raises(InvalidRequest):
        async with open_mcp_runtime("unused", request_timeout_s=timeout):
            raise AssertionError("invalid configuration")
