import asyncio
import json
import socket
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
import sys

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import SkillCall, TaskRequest
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.transport.server import RuntimeServer
from robot_runtime_mcp.server import serve_http

TOKEN = "correct-horse"


@asynccontextmanager
async def mcp_http(url: str, *, token: str = TOKEN) -> AsyncIterator[ClientSession]:
    async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=30) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                yield session


def request(skill: str = "robot.get_state", *, key: str = "http-key") -> TaskRequest:
    return TaskRequest(
        task_type="mcp-http-test", idempotency_key=key,
        steps=[SkillCall(skill=skill, params={"delay_s": 30} if skill == "wait.delay" else {})],
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def _wait_port(port: int) -> None:
    async with asyncio.timeout(5):
        while True:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.02)


@asynccontextmanager
async def http_bridge(
    *, allowed: set[str] | None = None, token: str = TOKEN,
) -> AsyncIterator[tuple[str, Simulator, asyncio.Task]]:
    with tempfile.TemporaryDirectory(prefix="mcp-http-") as directory:
        sock = str(Path(directory) / "runtime.sock")
        simulator = Simulator()
        server = RuntimeServer(InMemoryTaskRepository(), simulator.skills, sock)
        await server.start()
        port = _free_port()
        task = asyncio.create_task(serve_http(
            sock, allowed_skills=allowed, host="127.0.0.1", port=port, token=token))
        try:
            await _wait_port(port)
            yield f"http://127.0.0.1:{port}/mcp", simulator, task
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await server.close()


async def test_rejects_missing_and_wrong_tokens_then_official_client_completes() -> None:
    async with http_bridge() as (url, simulator, _):
        async with httpx.AsyncClient() as client:
            for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
                response = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "x"})
                assert response.status_code == 401
                assert response.json()["error"]["code"] == "UNAUTHORIZED"
            assert simulator.skills["robot.get_state"].calls == 0
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url.replace("/mcp", "/other"),
                headers={"Authorization": f"Bearer {TOKEN}"}, json={})
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "NOT_FOUND"
        async with mcp_http(url) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "robot-agent-runtime"
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert {"submit_task", "get_task_status", "robot_speak"} <= tools
            submitted = await session.call_tool(
                "submit_task", {"request": request().model_dump(mode="json")})
            replayed = await session.call_tool(
                "submit_task", {"request": request().model_dump(mode="json")})
            assert not submitted.isError and not replayed.isError
            assert submitted.structuredContent["task"]["id"] == \
                replayed.structuredContent["task"]["id"]
        assert simulator.skills["robot.get_state"].calls == 1


async def test_allowlist_filters_http_discovery_and_rejects_steps() -> None:
    async with http_bridge(allowed={"robot.get_state"}) as (url, simulator, _):
        async with mcp_http(url) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert "robot_speak" not in tools
            assert "robot_wait_delay" not in tools
            denied = await session.call_tool(
                "submit_task", {"request": request("wait.delay").model_dump(mode="json")})
            assert denied.isError
            assert denied.structuredContent["error"]["code"] == "PERMISSION_DENIED"
        assert simulator.skills["wait.delay"].calls == 0


async def test_empty_token_is_refused_before_serving() -> None:
    with tempfile.TemporaryDirectory(prefix="mcp-http-") as directory:
        sock = str(Path(directory) / "runtime.sock")
        with pytest.raises(ValueError):
            await serve_http(sock, token="")


def test_cli_refuses_http_mode_without_token() -> None:
    from mcp import StdioServerParameters
    from mcp.client.stdio import get_default_environment

    import subprocess
    process = subprocess.run(
        [sys.executable, "-m", "robot_runtime_mcp", "--mode", "http", "--socket", "/tmp/x.sock"],
        capture_output=True, text=True, timeout=15,
        env=get_default_environment(),
    )
    assert process.returncode == 2
    assert "bearer token" in (process.stderr + process.stdout).lower()


async def test_json_body_round_trip_preserves_unicode_payloads() -> None:
    async with http_bridge() as (url, simulator, _):
        async with mcp_http(url) as session:
            await session.initialize()
            value = request()
            value.steps[0].params = {"message": "咖啡已备好 ☕"}
            spoken = await session.call_tool(
                "robot_speak", {"operation_id": "http-speak-1",
                                "params": {"message": "咖啡已备好 ☕"}, "wait": True})
            assert not spoken.isError
            assert spoken.structuredContent["task_id"]
            task_id = spoken.structuredContent["task_id"]
            status = await session.call_tool("robot_task_status", {"task_id": task_id})
            assert not status.isError
            assert status.structuredContent["task_id"] == task_id
