import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from robot_agent_runtime.errors import AgentRuntimeError, InvalidRequest
from robot_agent_runtime.models import Event, SkillDefinition, Task, TaskRequest
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.sdk.listeners import (
    DoneCallback,
    ErrorCallback,
    EventCallback,
    TaskListener,
    TaskListeners,
)
from robot_agent_runtime.sdk.listener_execution import ListenerExecution, on_callback_owner
from robot_agent_runtime.sdk.task_waiting import submit_and_wait
from robot_agent_runtime.transport.wire import read_message, write_message


class RemoteRuntimeError(AgentRuntimeError):
    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        code: str = "RUNTIME_ERROR",
    ) -> None:
        self.code = code
        super().__init__(message, details)


class RemoteTransportError(RuntimeError):
    pass


class RemoteRuntime:
    def __init__(self, socket_path: str, *, options: RuntimeOptions | None = None) -> None:
        self._socket_path = socket_path
        self._writers: set[asyncio.StreamWriter] = set()
        self._closed = False
        self._options = options or RuntimeOptions()
        self._lifecycle_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._listeners = TaskListeners(
            self.subscribe_task_events, self.get_task_status,
            cancel_grace_s=self._options.cancel_grace_s,
            options=self._options,
        )

    def add_task_listener(
        self,
        *,
        on_event: EventCallback | None = None,
        on_done: DoneCallback | None = None,
        on_error: ErrorCallback | None = None,
        execution: ListenerExecution = "caller",
        group: str | None = None,
    ) -> TaskListener:
        return self._listeners.add(on_event=on_event, on_done=on_done, on_error=on_error,
                                   execution=execution, group=group)

    async def start(self) -> None:
        health = await self._request("health")
        if (
            not isinstance(health, dict)
            or health.get("status") != "ok"
            or health.get("service") != "robot-agent-runtime"
            or not isinstance(health.get("version"), str)
        ):
            raise RemoteTransportError("Invalid health response from runtime server")

    async def close(self) -> None:
        if await on_callback_owner(self.close):
            return
        self._closed = True
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close_impl())
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        async with self._lifecycle_lock:
            self._closed = True
            writers = list(self._writers)
            self._writers.clear()
        for writer in writers:
            writer.close()
        try:
            await self._listeners.close()
        finally:
            await asyncio.gather(
                *(self._wait_closed(writer) for writer in writers), return_exceptions=True
            )

    async def submit_task(self, request: TaskRequest) -> Task:
        result = await self._request(
            "submit_task", {"request": request.model_dump(mode="json")}
        )
        task = self._validate(Task, result)
        self._listeners.track(task)
        return task

    async def submit_and_wait(
        self, request: TaskRequest, *, timeout_s: float | None = None,
    ) -> Task:
        """Return any terminal task; a local wait timeout never cancels its action.

        The optional deadline starts after submission. TaskWaitTimeout retains
        the task ID for querying or explicit cancellation. Registered callbacks
        complete independently; use their listener.wait() to drain delivery.
        """
        return await submit_and_wait(
            request, submit=self.submit_task, subscribe=self.subscribe_task_events,
            get_task=self.get_task_status, timeout_s=timeout_s,
        )

    async def get_task_status(self, task_id: str) -> Task:
        result = await self._request("get_task_status", {"task_id": task_id})
        return self._validate(Task, result)

    async def cancel_task(self, task_id: str) -> Task:
        result = await self._request("cancel_task", {"task_id": task_id})
        return self._validate(Task, result)

    async def list_skills(self) -> list[SkillDefinition]:
        result = await self._request("list_skills")
        if not isinstance(result, list):
            raise RemoteTransportError("Invalid list_skills response from runtime server")
        return [self._validate(SkillDefinition, item) for item in result]

    async def subscribe_task_events(
        self, task_id: str, after_sequence: int = 0
    ) -> AsyncGenerator[Event, None]:
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await self._open_connection()
            request_id = uuid4().hex
            await write_message(
                writer,
                {
                    "id": request_id,
                    "method": "subscribe_task_events",
                    "params": {"task_id": task_id, "after_sequence": after_sequence},
                },
            )
            while True:
                try:
                    message = await read_message(reader)
                except InvalidRequest as error:
                    raise RemoteTransportError(
                        f"Invalid event stream response: {error.info.message}"
                    ) from error
                if message is None:
                    if self._closed:
                        return
                    raise RemoteTransportError("Event stream ended without a done message")
                self._validate_stream_envelope(message, request_id)
                if message.get("ok") is False:
                    raise self._remote_error(message)
                message_type = message.get("type")
                if message_type == "done":
                    return
                if message_type != "event" or "event" not in message:
                    raise RemoteTransportError("Invalid event stream message")
                yield self._validate(Event, message["event"])
        except (OSError, ConnectionError) as error:
            if not self._closed:
                raise RemoteTransportError(f"Event stream transport failed: {error}") from error
        finally:
            if writer is not None:
                self._writers.discard(writer)
                writer.close()
                with suppress(ConnectionError, OSError):
                    await writer.wait_closed()

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await self._open_connection()
            request_id = uuid4().hex
            await write_message(
                writer,
                {"id": request_id, "method": method, "params": params or {}},
            )
            message = await read_message(reader)
        except RemoteTransportError:
            raise
        except (OSError, ConnectionError, InvalidRequest) as error:
            raise RemoteTransportError(f"Runtime request transport failed: {error}") from error
        finally:
            if writer is not None:
                self._writers.discard(writer)
                writer.close()
                with suppress(ConnectionError, OSError):
                    await writer.wait_closed()
        if message is None:
            raise RemoteTransportError("Runtime server closed the connection without a response")
        self._validate_response_envelope(message, request_id)
        if message["ok"] is False:
            raise self._remote_error(message)
        if "result" not in message:
            raise RemoteTransportError("Successful runtime response is missing result")
        return message["result"]

    async def _open_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        async with self._lifecycle_lock:
            if self._closed:
                raise RemoteTransportError("RemoteRuntime is closed")
        try:
            reader, writer = await asyncio.open_unix_connection(self._socket_path)
        except (OSError, ConnectionError) as error:
            raise RemoteTransportError(f"Unable to connect to runtime server: {error}") from error
        async with self._lifecycle_lock:
            closed = self._closed
            if closed:
                writer.close()
            else:
                self._writers.add(writer)
        if closed:
            await self._wait_closed(writer)
            raise RemoteTransportError("RemoteRuntime closed while connecting")
        return reader, writer

    @staticmethod
    async def _wait_closed(writer: asyncio.StreamWriter) -> None:
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()

    @staticmethod
    def _validate(model: type[Task] | type[SkillDefinition] | type[Event], value: Any) -> Any:
        try:
            return model.model_validate(value)
        except (ValidationError, TypeError, ValueError) as error:
            raise RemoteTransportError(f"Invalid {model.__name__} in runtime response") from error

    @staticmethod
    def _validate_response_envelope(message: dict[str, Any], request_id: str) -> None:
        if message.get("id") != request_id or not isinstance(message.get("ok"), bool):
            raise RemoteTransportError("Invalid runtime response envelope")

    @staticmethod
    def _validate_stream_envelope(message: dict[str, Any], request_id: str) -> None:
        if message.get("id") != request_id or not isinstance(message.get("ok"), bool):
            raise RemoteTransportError("Invalid event stream response envelope")
        message_type = message.get("type")
        expected_keys = {
            "event": {"id", "ok", "type", "event"},
            "done": {"id", "ok", "type"},
            "error": {"id", "ok", "type", "error"},
        }.get(message_type)
        if expected_keys is None or set(message) != expected_keys:
            raise RemoteTransportError("Invalid event stream message type or fields")
        if message_type == "error":
            if message["ok"] is not False:
                raise RemoteTransportError("Stream error envelope must have ok=false")
        elif message["ok"] is not True:
            raise RemoteTransportError("Stream event/done envelope must have ok=true")

    @staticmethod
    def _remote_error(message: dict[str, Any]) -> RemoteRuntimeError:
        error = message.get("error")
        if not isinstance(error, dict):
            raise RemoteTransportError("Error response is missing error details")
        code = error.get("code")
        text = error.get("message")
        details = error.get("details")
        if (
            not isinstance(code, str)
            or not isinstance(text, str)
            or not isinstance(details, dict)
        ):
            raise RemoteTransportError("Invalid business error response")
        return RemoteRuntimeError(text, details, code=code)
