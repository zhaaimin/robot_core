import asyncio
import math
import os
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, TypeVar

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel, ValidationError

from robot_agent_runtime.errors import InvalidRequest
from robot_agent_runtime.models import Event, SkillDefinition, Task, TaskRequest
from robot_agent_runtime.sdk.task_waiting import submit_and_wait
from robot_runtime_mcp.errors import McpRuntimeError, McpTransportError


_Model = TypeVar("_Model", bound=BaseModel)


class McpRuntime:
    """Task client over one MCP session; no transport failure is retried.

    Session ownership belongs to open_mcp_runtime. Cancelling a caller or an
    event batch stops observation only; explicit cancel_task is the control
    operation for the robot task.
    """

    def __init__(self, session: ClientSession, *, request_timeout_s: float = 10.0) -> None:
        self._session = session
        self._timeout = request_timeout_s

    async def list_skills(self) -> list[SkillDefinition]:
        result = await self._call("list_skills", {})
        skills = result.get("skills")
        if not isinstance(skills, list):
            raise McpTransportError("Invalid MCP skill list")
        return [self._validate(SkillDefinition, value) for value in skills]

    async def submit_task(self, request: TaskRequest) -> Task:
        result = await self._call("submit_task", {"request": request.model_dump(mode="json")})
        return self._validate(Task, result.get("task"))

    async def submit_and_wait(self, request: TaskRequest, *, timeout_s: float | None = None) -> Task:
        return await submit_and_wait(
            request, submit=self.submit_task, subscribe=self.subscribe_task_events,
            get_task=self.get_task_status, timeout_s=timeout_s,
        )

    async def get_task_status(self, task_id: str) -> Task:
        return await self._task_call("get_task_status", task_id)

    async def cancel_task(self, task_id: str) -> Task:
        return await self._task_call("cancel_task", task_id)

    async def _task_call(self, name: str, task_id: str) -> Task:
        result = await self._call(name, {"task_id": task_id})
        task = self._validate(Task, result.get("task"))
        if task.id != task_id:
            raise McpTransportError("MCP task response belongs to another task")
        return task

    async def subscribe_task_events(
        self, task_id: str, after_sequence: int = 0,
    ) -> AsyncGenerator[Event, None]:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise InvalidRequest("after_sequence must be a non-negative integer")
        cursor = after_sequence
        while True:
            result = await self._call("read_task_events", {
                "task_id": task_id, "after_sequence": cursor,
                "limit": 32, "wait_s": min(0.2, self._timeout / 2),
            })
            values = result.get("events")
            next_sequence = result.get("next_sequence")
            done = result.get("done")
            if (not isinstance(values, list) or len(values) > 32
                    or type(next_sequence) is not int or type(done) is not bool):
                raise McpTransportError("Invalid MCP event batch")
            if any(not isinstance(value, dict) or type(value.get("sequence")) is not int
                   for value in values):
                raise McpTransportError("MCP event batch has an invalid sequence")
            events = [self._validate(Event, value) for value in values]
            watermark = cursor
            for event in events:
                if event.task_id != task_id or event.sequence <= watermark:
                    raise McpTransportError("MCP event batch has an invalid task or sequence")
                watermark = event.sequence
            if next_sequence != watermark:
                raise McpTransportError("MCP event batch has an invalid cursor")
            for event in events:
                yield event
            cursor = next_sequence
            if done:
                return

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self._timeout):
                result = await self._session.call_tool(name, arguments)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise McpTransportError("MCP request failed; runtime outcome is uncertain", {"operation": name}) from error
        value = result.structuredContent
        if not isinstance(value, dict):
            raise McpTransportError("MCP response is missing structured content", {"operation": name})
        if result.isError:
            failure = value.get("error")
            if (not isinstance(failure, dict) or not isinstance(failure.get("code"), str)
                    or not isinstance(failure.get("message"), str) or not isinstance(failure.get("details"), dict)):
                raise McpTransportError("MCP response has an invalid structured error", {"operation": name})
            if failure["code"] == McpTransportError.code:
                raise McpTransportError(failure["message"], failure["details"])
            raise McpRuntimeError(failure["message"], failure["details"], code=failure["code"])
        return value

    @staticmethod
    def _validate(model: type[_Model], value: Any) -> _Model:
        try:
            return model.model_validate(value)
        except (ValidationError, TypeError, ValueError) as error:
            raise McpTransportError(f"Invalid {model.__name__} in MCP response") from error


@asynccontextmanager
async def open_mcp_runtime(
    socket_path: str, *, allowed_skills: set[str] | None = None, request_timeout_s: float = 10.0,
) -> AsyncIterator[McpRuntime]:
    """Own official stdio/session contexts in the entering task through teardown.

    The subprocess owns only a remote client. The original caller exception,
    including cancellation, is raised after session and child cleanup so SDK
    task groups cannot wrap business errors in an ExceptionGroup.
    """
    if (isinstance(request_timeout_s, bool) or not isinstance(request_timeout_s, (int, float))
            or not math.isfinite(request_timeout_s) or request_timeout_s <= 0):
        raise InvalidRequest("request_timeout_s must be finite and positive")
    args = ["-m", "robot_runtime_mcp", "--socket", socket_path]
    if allowed_skills is not None:
        if not allowed_skills:
            args.append("--deny-all-skills")
        else:
            for skill in sorted(allowed_skills):
                args.extend(["--allow-skill", skill])
    child_env = {"PYTHONPATH": os.environ["PYTHONPATH"]} if "PYTHONPATH" in os.environ else None
    parameters = StdioServerParameters(command=sys.executable, args=args, env=child_env)
    errlog = sys.stderr
    try:
        errlog.fileno()
    except (AttributeError, OSError, ValueError):
        errlog = sys.__stderr__
    caller_error: BaseException | None = None
    initialized = False
    try:
        async with stdio_client(parameters, errlog=errlog) as (reader, writer):
            async with ClientSession(
                reader, writer, read_timeout_seconds=timedelta(seconds=request_timeout_s),
            ) as session:
                try:
                    async with asyncio.timeout(request_timeout_s):
                        identity = await session.initialize()
                        if identity.serverInfo.name != "robot-agent-runtime" or identity.capabilities.tools is None:
                            raise McpTransportError("MCP peer is not a robot runtime bridge")
                    initialized = True
                    yield McpRuntime(session, request_timeout_s=request_timeout_s)
                except BaseException as error:
                    caller_error = error
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if caller_error is None:
            raise McpTransportError("MCP session failed; runtime outcome is uncertain") from error
        caller_error.add_note(f"MCP teardown also failed: {type(error).__name__}")
    if caller_error is not None:
        if isinstance(caller_error, asyncio.CancelledError):
            raise caller_error
        if not initialized:
            raise McpTransportError("MCP initialization failed; runtime outcome is uncertain") from caller_error
        raise caller_error
