import asyncio
import os
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import AgentRuntimeError, TaskWaitTimeout
from robot_agent_runtime.models import SkillCall, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk.remote import RemoteRuntime
from robot_agent_runtime.transport.server import RuntimeServer


@asynccontextmanager
async def runtime_server(
    repository: InMemoryTaskRepository | None = None,
) -> AsyncIterator[tuple[RuntimeServer, str, Simulator]]:
    with tempfile.TemporaryDirectory(prefix="mcp-bridge-") as directory:
        socket = str(Path(directory) / "runtime.sock")
        simulator = Simulator()
        server = RuntimeServer(repository or InMemoryTaskRepository(), simulator.skills, socket)
        await server.start()
        try:
            yield server, socket, simulator
        finally:
            await server.close()


@asynccontextmanager
async def raw_session(socket: str, *args: str) -> AsyncIterator[ClientSession]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "robot_runtime_mcp", "--socket", socket, *args],
        env={"PYTHONPATH": os.environ["PYTHONPATH"]} if "PYTHONPATH" in os.environ else None,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            async with asyncio.timeout(5):
                initialized = await session.initialize()
            assert initialized.serverInfo.name == "robot-agent-runtime"
            yield session


def request(skill: str = "robot.get_state", *, key: str | None = "stable-key") -> TaskRequest:
    return TaskRequest(
        task_type="mcp-test", idempotency_key=key,
        steps=[SkillCall(skill=skill, params={"delay_s": 30} if skill == "wait.delay" else {})],
    )


async def wait_running(runtime: Any, task_id: str) -> None:
    async with asyncio.timeout(2):
        while (await runtime.get_task_status(task_id)).state != TaskState.RUNNING:
            pass


async def test_official_stdio_discovery_schema_and_runtime_idempotency() -> None:
    async with runtime_server() as (_, socket, simulator):
        async with raw_session(socket) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert set(tools) == {
                "list_skills", "submit_task", "get_task_status", "cancel_task", "read_task_events",
            }
            assert tools["submit_task"].inputSchema["required"] == ["request"]
            assert "steps" in str(tools["submit_task"].inputSchema)
            definition = tools["submit_task"].inputSchema["$defs"]["TaskRequest"]
            assert "idempotency_key" in definition["required"]
            assert definition["properties"]["idempotency_key"]["minLength"] == 1
            skills = await session.call_tool("list_skills", {})
            assert not skills.isError
            assert "robot.get_state" in {
                item["name"] for item in skills.structuredContent["skills"]
            }
            submitted = await session.call_tool("submit_task", {"request": request().model_dump(mode="json")})
            replayed = await session.call_tool("submit_task", {"request": request().model_dump(mode="json")})
            assert not submitted.isError and not replayed.isError
            assert submitted.structuredContent["task"]["id"] == replayed.structuredContent["task"]["id"]
        remote = RemoteRuntime(socket)
        await remote.start()
        try:
            final = await remote.submit_and_wait(request(), timeout_s=1)
            assert final.state == TaskState.SUCCEEDED
            assert simulator.skills["robot.get_state"].calls == 1
        finally:
            await remote.close()


async def test_structured_validation_and_runtime_errors() -> None:
    async with runtime_server() as (_, socket, _):
        async with raw_session(socket) as session:
            for arguments in ({}, {"request": request(key=None).model_dump(mode="json")},
                              {"request": request(key="  ").model_dump(mode="json")}):
                result = await session.call_tool("submit_task", arguments)
                assert result.isError
                assert result.structuredContent["error"]["code"] == "INVALID_REQUEST"
            missing = await session.call_tool("get_task_status", {"task_id": "missing"})
            assert missing.isError
            assert missing.structuredContent["error"]["code"] == "TASK_NOT_FOUND"
            unknown = await session.call_tool("unknown_tool", {})
            assert unknown.isError
            assert unknown.structuredContent["error"]["code"] == "INVALID_REQUEST"
            missing_skill = await session.call_tool("submit_task", {
                "request": request("no-such-skill").model_dump(mode="json"),
            })
            assert not missing_skill.isError
            assert missing_skill.structuredContent["task"]["state"] == TaskState.FAILED.value
            assert missing_skill.structuredContent["task"]["error"]["code"] == "SKILL_NOT_FOUND"


async def test_allowlist_filters_discovery_and_rejects_every_step() -> None:
    async with runtime_server() as (_, socket, simulator):
        async with raw_session(socket, "--allow-skill", "robot.get_state") as session:
            skills = await session.call_tool("list_skills", {})
            assert [item["name"] for item in skills.structuredContent["skills"]] == ["robot.get_state"]
            value = request()
            value.steps.append(SkillCall(skill="wait.delay", params={"delay_s": 0}))
            result = await session.call_tool("submit_task", {"request": value.model_dump(mode="json")})
            assert result.isError
            assert result.structuredContent["error"]["code"] == "PERMISSION_DENIED"
            assert simulator.skills["robot.get_state"].calls == 0


async def test_real_event_batches_history_limit_timeout_and_cancel() -> None:
    from robot_runtime_mcp.client import open_mcp_runtime

    async with runtime_server() as (_, socket, _):
        async with open_mcp_runtime(socket) as runtime:
            submitted = await runtime.submit_task(request("wait.delay"))
            await wait_running(runtime, submitted.id)
            async with raw_session(socket) as session:
                batch = await session.call_tool("read_task_events", {
                    "task_id": submitted.id, "limit": 1, "wait_s": 0.01,
                })
                payload = batch.structuredContent
                assert len(payload["events"]) == 1
                assert payload["events"][0]["event_type"] == "TASK_CREATED"
                assert payload["next_sequence"] == payload["events"][-1]["sequence"]
                assert payload["done"] is False
                history = await session.call_tool("read_task_events", {
                    "task_id": submitted.id, "wait_s": 0.02,
                })
                cursor = history.structuredContent["next_sequence"]
                empty = await session.call_tool("read_task_events", {
                    "task_id": submitted.id, "after_sequence": cursor, "wait_s": 0.02,
                })
                assert empty.structuredContent == {"events": [], "next_sequence": cursor, "done": False}
                assert (await runtime.get_task_status(submitted.id)).state == TaskState.RUNNING
                await runtime.cancel_task(submitted.id)
                async with asyncio.timeout(3):
                    events = [event async for event in runtime.subscribe_task_events(submitted.id)]
                assert events[-1].data["state"] == TaskState.CANCELED.value
                assert all(event.task_id == submitted.id for event in events)
                assert [event.sequence for event in events] == sorted({event.sequence for event in events})
                terminal = await session.call_tool("read_task_events", {
                    "task_id": submitted.id, "after_sequence": events[-1].sequence,
                })
                assert terminal.structuredContent == {
                    "events": [], "next_sequence": events[-1].sequence, "done": True,
                }


async def test_client_preserves_error_code_allowlist_and_server_lifetime() -> None:
    from robot_runtime_mcp.client import open_mcp_runtime

    async with runtime_server() as (_, socket, _):
        async with open_mcp_runtime(socket, allowed_skills={"robot.get_state"}) as runtime:
            assert [skill.name for skill in await runtime.list_skills()] == ["robot.get_state"]
            with pytest.raises(AgentRuntimeError) as caught:
                await runtime.get_task_status("missing")
            assert caught.value.info.code == "TASK_NOT_FOUND"
            with pytest.raises(AgentRuntimeError) as denied:
                await runtime.submit_task(request("wait.delay"))
            assert denied.value.info.code == "PERMISSION_DENIED"
        remote = RemoteRuntime(socket)
        try:
            await remote.start()
            assert (await remote.submit_and_wait(request(), timeout_s=1)).state == TaskState.SUCCEEDED
        finally:
            await remote.close()


async def test_local_wait_timeout_and_cancellation_preserve_physical_task() -> None:
    from robot_runtime_mcp.client import open_mcp_runtime

    async with runtime_server() as (_, socket, _):
        async with open_mcp_runtime(socket) as runtime:
            with pytest.raises(TaskWaitTimeout) as caught:
                await runtime.submit_and_wait(request("wait.delay"), timeout_s=0.03)
            task_id = caught.value.info.details["task_id"]
            assert (await runtime.get_task_status(task_id)).state == TaskState.RUNNING
            stream = runtime.subscribe_task_events(task_id)
            async with aclosing(stream):
                first = await anext(stream)
                pending = asyncio.create_task(anext(stream))
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            assert first.task_id == task_id
        remote = RemoteRuntime(socket)
        try:
            assert (await remote.get_task_status(task_id)).state == TaskState.RUNNING
            await remote.cancel_task(task_id)
        finally:
            await remote.close()


async def test_disconnect_is_typed_and_does_not_retry_submission() -> None:
    from robot_runtime_mcp.client import open_mcp_runtime
    from robot_runtime_mcp.errors import McpTransportError

    async with runtime_server() as (server, socket, simulator):
        async with open_mcp_runtime(socket) as runtime:
            await server.close()
            with pytest.raises(McpTransportError) as caught:
                await runtime.submit_task(request())
            assert caught.value.info.details["outcome_uncertain"] is True
            assert simulator.skills["robot.get_state"].calls == 0


async def test_event_batch_input_bounds_and_missing_task() -> None:
    async with runtime_server() as (_, socket, _):
        async with raw_session(socket) as session:
            for field, value in (("limit", 129), ("limit", 0), ("wait_s", 2),
                                 ("wait_s", -1), ("after_sequence", -1), ("limit", True)):
                result = await session.call_tool("read_task_events", {"task_id": "missing", field: value})
                assert result.isError
                assert result.structuredContent["error"]["code"] == "INVALID_REQUEST"
            missing = await session.call_tool("read_task_events", {"task_id": "missing"})
            assert missing.structuredContent["error"]["code"] == "TASK_NOT_FOUND"


async def test_live_events_match_runtime_history_exactly() -> None:
    from robot_runtime_mcp.client import open_mcp_runtime

    async with runtime_server() as (_, socket, _):
        async with open_mcp_runtime(socket) as runtime:
            submitted = await runtime.submit_task(request("wait.delay"))
            running_seen = asyncio.Event()
            async def observe() -> list[Any]:
                events = []
                async with aclosing(runtime.subscribe_task_events(submitted.id)) as stream:
                    async for event in stream:
                        events.append(event)
                        if event.data.get("state") == TaskState.RUNNING.value:
                            running_seen.set()
                return events

            observing = asyncio.create_task(observe())
            async with asyncio.timeout(3):
                await running_seen.wait()
                await runtime.cancel_task(submitted.id)
                events = await observing
            remote = RemoteRuntime(socket)
            try:
                async with aclosing(remote.subscribe_task_events(submitted.id)) as stream:
                    authoritative = [event async for event in stream]
                assert events == authoritative
                cursor = events[1].sequence
                resumed = [event async for event in runtime.subscribe_task_events(submitted.id, cursor)]
                assert resumed == events[2:]
            finally:
                await remote.close()
