import asyncio
import os
import stat
import sys
from contextlib import suppress
from typing import Any
from collections.abc import Mapping

from pydantic import ValidationError

from robot_agent_runtime.kernel.core import RuntimeCore
from robot_agent_runtime.errors import AgentRuntimeError, InvalidRequest
from robot_agent_runtime.hosting.kernel_host import KernelHost
from robot_agent_runtime.models import TaskRequest
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.ports import PlatformEventRepository, PlatformEventSource, Reconciler, RobotStateProvider, Skill, TaskRepository
from robot_agent_runtime.thread_binding import ThreadBinding
from robot_agent_runtime.transport.wire import (
    MAX_MESSAGE_BYTES,
    SERVICE_NAME,
    SERVICE_VERSION,
    error_response,
    read_message,
    write_message,
)


class RuntimeServer:
    def __init__(
        self,
        repository: TaskRepository | ThreadBinding[TaskRepository],
        skills: Mapping[str, Skill | ThreadBinding[Skill]],
        socket_path: str,
        *,
        reconciler: Reconciler | ThreadBinding[Reconciler] | None = None,
        allowed_permissions: set[str] | None = None,
        robot_state: RobotStateProvider | ThreadBinding[RobotStateProvider] | None = None,
        options: RuntimeOptions | None = None,
        event_source: PlatformEventSource | ThreadBinding[PlatformEventSource] | None = None,
        event_repository: PlatformEventRepository | ThreadBinding[PlatformEventRepository] | None = None,
    ) -> None:
        self._options = options or RuntimeOptions()
        self._host = KernelHost(repository, skills, reconciler, allowed_permissions, robot_state,
                                options=self._options, event_source=event_source, event_repository=event_repository)
        self._socket_path = socket_path
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._owned_socket: tuple[int, int] | None = None
        self._core_started = False
        self._closing = False
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def _core(self) -> RuntimeCore:
        core = self._host._core
        if core is None:
            raise InvalidRequest("Server core has not been prepared")
        return core

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closing or self._closed:
                raise RuntimeError("RuntimeServer is closed")
            if self._server is not None:
                return
            if os.path.lexists(self._socket_path):
                mode = os.lstat(self._socket_path).st_mode
                if not stat.S_ISSOCK(mode):
                    raise RuntimeError(f"Socket path exists and is not a socket: {self._socket_path}")
                raise OSError(f"Socket path already exists: {self._socket_path}")
            await self._host.start()
            self._core_started = True
            server: asyncio.Server | None = None
            try:
                server = await asyncio.start_unix_server(
                    self._client_connected,
                    path=self._socket_path,
                    limit=MAX_MESSAGE_BYTES + 1,
                    **({"cleanup_socket": False} if sys.version_info >= (3, 13) else {}),
                )
                socket_stat = os.lstat(self._socket_path)
                self._owned_socket = (socket_stat.st_dev, socket_stat.st_ino)
                os.chmod(self._socket_path, 0o600)
                self._assert_owned_socket()
            except BaseException:
                if server is not None:
                    server.close()
                    with suppress(Exception):
                        await server.wait_closed()
                self._remove_owned_socket()
                if self._core_started:
                    with suppress(Exception):
                        await self._host.close()
                    self._core_started = False
                raise
            self._server = server

    async def close(self) -> None:
        self._closing = True
        async with self._close_lock:
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close_impl())
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closing = True
            server = self._server
            self._server = None
        try:
            if server is not None:
                server.close()
            await self._host.begin_close()
            handlers = list(self._handlers)
            for handler in handlers:
                handler.cancel()
            if handlers:
                _, pending = await asyncio.wait(
                    handlers, timeout=self._options.cancel_grace_s,
                )
                for handler in handlers:
                    if handler in pending:
                        handler.add_done_callback(self._consume_handler)
                    else:
                        self._consume_handler(handler)
            if server is not None:
                await server.wait_closed()
            await self._host.close()
            self._core_started = False
        finally:
            self._remove_owned_socket()
            self._closed = True

    @staticmethod
    def _consume_handler(handler: asyncio.Task[None]) -> None:
        if not handler.cancelled():
            handler.exception()

    def _assert_owned_socket(self) -> None:
        socket_stat = os.lstat(self._socket_path)
        if (
            not stat.S_ISSOCK(socket_stat.st_mode)
            or (socket_stat.st_dev, socket_stat.st_ino) != self._owned_socket
        ):
            raise RuntimeError("Socket path changed during server startup")

    def _remove_owned_socket(self) -> None:
        owned = self._owned_socket
        self._owned_socket = None
        if owned is None:
            return
        try:
            socket_stat = os.lstat(self._socket_path)
        except FileNotFoundError:
            return
        if (
            stat.S_ISSOCK(socket_stat.st_mode)
            and (socket_stat.st_dev, socket_stat.st_ino) == owned
        ):
            os.unlink(self._socket_path)

    async def _client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            await self._handle_connection(reader, writer)
        finally:
            if task is not None:
                self._handlers.discard(task)
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_id: str | int | None = None
        streaming = False

        async def send_error(code: str, message: str, details: dict[str, Any]) -> None:
            response = error_response(request_id, code, message, details)
            if streaming:
                response["type"] = "error"
            await write_message(writer, response)

        try:
            message = await read_message(reader)
            if message is None:
                return
            request_id = self._request_id(message.get("id"))
            method = message.get("method")
            params = message.get("params", {})
            if not isinstance(method, str) or not method:
                raise InvalidRequest("Request method must be a non-empty string")
            if not isinstance(params, dict):
                raise InvalidRequest("Request params must be an object")
            streaming = method in {"subscribe_task_events", "subscribe_platform_events", "subscribe_all_task_events"}
            if streaming:
                await self._handle_subscription(request_id, params, reader, writer, method=method)
                return
            result = await self._dispatch(method, params)
            await write_message(writer, {"id": request_id, "ok": True, "result": result})
        except AgentRuntimeError as error:
            await send_error(error.info.code, error.info.message, error.info.details)
        except (ValidationError, ValueError, TypeError) as error:
            await send_error("INVALID_REQUEST", str(error), {})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await send_error(
                "INTERNAL_ERROR",
                "Internal server error",
                {"exception_type": type(error).__name__},
            )

    @staticmethod
    def _request_id(value: Any) -> str | int | None:
        if value is None or (isinstance(value, (str, int)) and not isinstance(value, bool)):
            return value
        raise InvalidRequest("Request id must be a string, integer, or null")

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "health":
            return {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION}
        if method == "submit_task":
            if "request" not in params:
                raise InvalidRequest("Missing required parameter: request")
            request = TaskRequest.model_validate(params["request"])
            return (await self._host.call("submit_task", request)).model_dump(mode="json")
        if method == "get_task_status":
            task = await self._host.call("get_task_status", self._task_id(params))
            return task.model_dump(mode="json")
        if method == "cancel_task":
            task = await self._host.call("cancel_task", self._task_id(params))
            return task.model_dump(mode="json")
        if method == "list_skills":
            skills = await self._host.call("list_skills")
            return [skill.model_dump(mode="json") for skill in skills]
        raise InvalidRequest("Unknown method", details={"method": method})

    @staticmethod
    def _task_id(params: dict[str, Any]) -> str:
        task_id = params.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise InvalidRequest("Missing or invalid required parameter: task_id")
        return task_id

    async def _handle_subscription(
        self,
        request_id: str | int | None,
        params: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *, method: str = "subscribe_task_events",
    ) -> None:
        if method == "subscribe_platform_events":
            after_cursor = params.get("after_cursor", 0)
            if type(after_cursor) is not int or after_cursor < 0:
                raise InvalidRequest("after_cursor must be a nonnegative integer")
            subscription = self._host.subscribe_platform_events(after_cursor, self._event_types(params))
        elif method == "subscribe_all_task_events":
            subscription = self._host.subscribe_all_task_events(self._event_types(params))
        else:
            task_id = self._task_id(params)
            after_sequence = params.get("after_sequence", 0)
            if type(after_sequence) is not int or after_sequence < 0:
                raise InvalidRequest("after_sequence must be a non-negative integer")
            subscription = self._host.subscribe_task_events(task_id, after_sequence)
        iterator = subscription.__aiter__()
        next_event = asyncio.create_task(anext(iterator))
        disconnected = asyncio.create_task(reader.read(1))
        try:
            while True:
                done, _ = await asyncio.wait(
                    {next_event, disconnected}, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnected in done:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    return
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    await write_message(
                        writer, {"id": request_id, "ok": True, "type": "done"}
                    )
                    return
                await write_message(
                    writer,
                    {
                        "id": request_id,
                        "ok": True,
                        "type": "event",
                        "event": event.model_dump(mode="json"),
                    },
                )
                next_event = asyncio.create_task(anext(iterator))
        finally:
            for pending in (next_event, disconnected):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(next_event, disconnected, return_exceptions=True)
            await iterator.aclose()

    @staticmethod
    def _event_types(params: dict[str, Any]) -> set[str] | None:
        values = params.get("event_types")
        if values is None:
            return None
        if type(values) is not list or any(type(value) is not str or not value.strip() for value in values):
            raise InvalidRequest("event_types must be a list of nonempty strings")
        return set(values)
