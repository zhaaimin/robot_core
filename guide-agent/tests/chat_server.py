"""Controlled HTTP endpoint for testing the real model client."""

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


def completion(message: dict[str, Any], *, finish_reason: str = "stop") -> ChatResponse:
    return ChatResponse({
        "id": "chatcmpl-local-test",
        "object": "chat.completion",
        "created": 1,
        "model": TEST_MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


