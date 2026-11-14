import asyncio
import json
from contextlib import aclosing
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from robot_agent_capabilities import RobotCapabilities
from robot_agent_runtime.errors import (
    AgentRuntimeError, InternalError, InvalidRequest, PermissionDenied,
)
from robot_agent_runtime.models import TaskRequest, TaskState
from robot_agent_runtime.schemas import describe_bounds
from robot_agent_runtime.sdk.remote import RemoteRuntime, RemoteTransportError
from robot_runtime_mcp.errors import McpTransportError
from robot_runtime_mcp.tool_schemas import capability_input_schema


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SubmitInput(EmptyInput):
    request: TaskRequest


class TaskInput(EmptyInput):
    task_id: str = Field(min_length=1)


class CapabilityInput(EmptyInput):
    operation_id: str = Field(min_length=1, pattern=r"\S")
    params: dict[str, Any]
    wait: bool = False


class EventsInput(TaskInput):
    after_sequence: int = Field(default=0, ge=0)
    limit: int = Field(default=32, ge=1, le=128)
    wait_s: float = Field(default=0.2, gt=0, le=1, allow_inf_nan=False)


_INPUTS: dict[str, type[BaseModel]] = {
    "list_skills": EmptyInput,
    "submit_task": SubmitInput,
    "get_task_status": TaskInput,
    "cancel_task": TaskInput,
    "read_task_events": EventsInput,
}
_DESCRIPTIONS = {
    "list_skills": "Discover available runtime skills and their declarations.",
    "submit_task": "Submit a task with a caller-owned stable, nonempty idempotency_key. Acceptance is not completion.",
    "get_task_status": "Read the authoritative runtime task snapshot.",
    "cancel_task": "Request cancellation and read the runtime task snapshot; inspect its state for confirmation.",
    "read_task_events": "Read a bounded batch of real task events after a sequence. A batch timeout does not cancel the task.",
}


class RuntimeMcpServer:
    """Expose an existing runtime without acquiring its execution lifecycle.

    Only this process's RemoteRuntime connections are closed on exit. Event
    batches close their own subscriptions and never cancel the observed task.
    """

    def __init__(self, runtime: RemoteRuntime, *, allowed_skills: set[str] | None = None) -> None:
        self._runtime = runtime
        self._allowed = frozenset(allowed_skills) if allowed_skills is not None else None
        self._capability_client: RobotCapabilities | None = None
        self._catalog_lock = asyncio.Lock()
        self.server = Server("robot-agent-runtime", version="0.1.0")
        self.server.list_tools()(self.list_tools)
        self.server.call_tool(validate_input=False)(self.call_tool)

    async def list_tools(self) -> list[types.Tool]:
        tools = [
            types.Tool(
                name=name, description=_DESCRIPTIONS[name],
                inputSchema=self._input_schema(name, model),
                annotations=types.ToolAnnotations(
                    readOnlyHint=name in {"list_skills", "get_task_status", "read_task_events"},
                    openWorldHint=False,
                ),
            )
            for name, model in _INPUTS.items()
        ]
        capabilities = await self._capabilities()
        tools.extend(types.Tool(
            name=tool.name,
            description=tool.description + (" 默认只受理任务；请检查 task_id/state，必要时查询结果。" if tool.skill else ""),
            inputSchema=capability_input_schema(tool),
            annotations=types.ToolAnnotations(readOnlyHint=tool.read_only, openWorldHint=False),
        ) for tool in capabilities.tools)
        return tools

    async def _capabilities(self) -> RobotCapabilities:
        async with self._catalog_lock:
            if self._capability_client is None:
                definitions = await self._runtime.list_skills()
                visible = [definition for definition in definitions
                           if self._allowed is None or definition.name in self._allowed]
                self._capability_client = RobotCapabilities(self._runtime, visible)
            return self._capability_client

    @staticmethod
    def _input_schema(name: str, model: type[BaseModel]) -> dict[str, Any]:
        schema = model.model_json_schema()
        if name == "submit_task":
            request = schema["$defs"]["TaskRequest"]
            request["required"].append("idempotency_key")
            request["properties"]["idempotency_key"] = {
                "type": "string", "minLength": 1, "pattern": r"\S",
                "description": "Stable caller-owned key; the bridge never generates or retries it.",
            }
        return describe_bounds(schema)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            model = _INPUTS.get(name)
            if model is None:
                return await self._call_capability(name, arguments)
            try:
                value = model.model_validate(arguments)
            except ValidationError as error:
                raise InvalidRequest("Invalid MCP tool arguments", {
                    "tool": name,
                    "errors": error.errors(include_url=False, include_context=False, include_input=False),
                }) from error
            result = await self._dispatch(name, value)
            return self._result(result)
        except AgentRuntimeError as error:
            return self._result({"error": error.to_dict()}, is_error=True)
        except RemoteTransportError as error:
            failure = McpTransportError(str(error), {"operation": name})
            return self._result({"error": failure.to_dict()}, is_error=True)
        except Exception:
            failure = InternalError("MCP bridge could not complete the runtime request")
            return self._result({"error": failure.to_dict()}, is_error=True)

    async def _call_capability(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        capabilities = await self._capabilities()
        tool = next((tool for tool in capabilities.tools if tool.name == name), None)
        if tool is None:
            raise InvalidRequest("Unknown MCP tool", {"tool": name})
        try:
            if tool.skill is not None:
                value = CapabilityInput.model_validate(arguments)
                observed = await capabilities.invoke(name, value.params, operation_id=value.operation_id, wait=value.wait)
            else:
                control = TaskInput.model_validate(arguments)
                observed = await capabilities.invoke(name, {"task_id": control.task_id})
        except ValidationError as error:
            raise InvalidRequest("Invalid MCP tool arguments", {"tool": name,
                "errors": error.errors(include_url=False, include_context=False, include_input=False)}) from error
        is_error = observed['error'] is not None if tool.skill is not None else observed['task_id'] is None
        return self._result(dict(observed), is_error=is_error)

    async def _dispatch(self, name: str, value: BaseModel) -> dict[str, Any]:
        if name == "list_skills":
            skills = await self._runtime.list_skills()
            return {"skills": [skill.model_dump(mode="json") for skill in skills
                               if self._allowed is None or skill.name in self._allowed]}
        if isinstance(value, SubmitInput):
            request = value.request
            if not request.idempotency_key or not request.idempotency_key.strip():
                raise InvalidRequest("submit_task requires a nonempty idempotency_key")
            for step in request.steps:
                if self._allowed is not None and step.skill not in self._allowed:
                    raise PermissionDenied("Skill is outside the MCP allowlist", {"skill": step.skill})
            return {"task": (await self._runtime.submit_task(request)).model_dump(mode="json")}
        if isinstance(value, EventsInput):
            return await self._read_events(value)
        if isinstance(value, TaskInput):
            method = self._runtime.get_task_status if name == "get_task_status" else self._runtime.cancel_task
            return {"task": (await method(value.task_id)).model_dump(mode="json")}
        raise InvalidRequest("Unknown MCP tool", {"tool": name})

    async def _read_events(self, value: EventsInput) -> dict[str, Any]:
        await self._runtime.get_task_status(value.task_id)
        events: list[dict[str, Any]] = []
        cursor = value.after_sequence
        done = False
        deadline = asyncio.timeout(value.wait_s)
        # The SDK waits when replay starts strictly after a terminal event.
        # One event of lookbehind confirms that exact cursor without rescanning
        # an already-delivered prefix or spending its next batch's wait budget.
        replay_from = max(0, cursor - 1)
        async with aclosing(self._runtime.subscribe_task_events(value.task_id, replay_from)) as stream:
            try:
                async with deadline:
                    while len(events) < value.limit:
                        try:
                            event = await anext(stream)
                        except StopAsyncIteration:
                            done = True
                            break
                        if event.sequence > cursor:
                            events.append(event.model_dump(mode="json"))
                            cursor = event.sequence
                        try:
                            terminal = TaskState(event.data.get("state")).is_terminal
                        except (TypeError, ValueError):
                            terminal = False
                        if terminal:
                            done = True
                            break
            except TimeoutError:
                if not deadline.expired():
                    raise
        return {"events": events, "next_sequence": cursor, "done": done}

    @staticmethod
    def _result(value: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
            structuredContent=value, isError=is_error,
        )


async def serve(socket_path: str, *, allowed_skills: set[str] | None = None) -> None:
    runtime = RemoteRuntime(socket_path)
    try:
        async with asyncio.timeout(10):
            await runtime.start()
        bridge = RuntimeMcpServer(runtime, allowed_skills=allowed_skills)
        async with stdio_server() as (reader, writer):
            await bridge.server.run(reader, writer, bridge.server.create_initialization_options())
    finally:
        await runtime.close()
