"""A local OpenAI-compatible server that stands in for the decision provider.

The installed HTTP client is exercised for real; only the endpoint is ours. Both
this project and the hotel agent keep their own copy, because a test fixture in
one project must never import the other.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any


TEST_API_KEY = "sk-local-robot-agent-test-key"
TEST_MODEL = "robot-agent-test-model"


@dataclass(frozen=True)
class ChatRequest:
    method: str
    target: str
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass(frozen=True)
class ChatResponse:
    body: dict[str, Any]
    status: int = 200
    release: asyncio.Event | None = None


class LocalChatServer:
    """Exercise the installed HTTP client without a provider or real credentials.

    A response may be held behind an event to test deadlines and cancellation.
    Every accepted connection is owned and drained by the context manager, so a
    timed-out client cannot leave test server tasks behind.
    """

    def __init__(self, responses: Sequence[ChatResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[ChatRequest] = []
        self.request_seen = asyncio.Event()
        self.handlers: set[asyncio.Task[Any]] = set()
        self.errors: list[Exception] = []
        self.base_url = ""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.handlers.add(task)
        try:
            raw_headers = await reader.readuntil(b"\r\n\r\n")
            lines = raw_headers.decode("iso-8859-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {
                name.lower(): value.strip()
                for line in lines[1:] if line
                for name, value in [line.split(":", 1)]
            }
            raw_body = await reader.readexactly(int(headers.get("content-length", "0")))
            body = json.loads(raw_body)
            assert isinstance(body, dict)
            self.requests.append(ChatRequest(method, target, headers, body))
            self.request_seen.set()
            response_index = len(self.requests) - 1
            assert response_index < len(self.responses), "Unexpected repeated model request"
            response = self.responses[response_index]
            if response.release is not None:
                await response.release.wait()
            encoded = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
            reason = "OK" if response.status == 200 else "Test Provider Error"
            writer.write((
                f"HTTP/1.1 {response.status} {reason}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(encoded)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii") + encoded)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as error:
            self.errors.append(error)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.handlers.discard(task)


@asynccontextmanager
async def local_chat_server(responses: Sequence[ChatResponse]) -> AsyncIterator[LocalChatServer]:
    chat = LocalChatServer(responses)
    server = await asyncio.start_server(chat.handle, host="127.0.0.1", port=0)
    assert server.sockets
    port = server.sockets[0].getsockname()[1]
    chat.base_url = f"http://127.0.0.1:{port}/v1"
    try:
        yield chat
    finally:
        server.close()
        handlers = list(chat.handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await server.wait_closed()
        assert not chat.errors, chat.errors


def tool_schemas() -> list[dict[str, Any]]:
    names = {
        "robot_speak": "message",
        "robot_navigate_to": "target",
        "robot_get_state": None,
        "robot_task_status": "task_id",
        "robot_cancel_task": "task_id",
    }
    return [{"type": "function", "function": {
        "name": name,
        "description": f"Test tool {name}",
        "parameters": {
            "type": "object",
            "properties": {param: {"type": "string"}} if param else {},
            "required": [param] if param else [],
            "additionalProperties": False,
        },
    }} for name, param in names.items()]


def completion(message: dict[str, Any], *, finish_reason: str = "stop") -> ChatResponse:
    return ChatResponse({
        "id": "chatcmpl-local-test",
        "object": "chat.completion",
        "created": 1,
        "model": TEST_MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


def tool_completion(arguments: str = '{"message":"你好，机器人"}') -> ChatResponse:
    return completion({
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call-local-speak",
            "type": "function",
            "function": {"name": "robot_speak", "arguments": arguments},
        }],
    }, finish_reason="tool_calls")
