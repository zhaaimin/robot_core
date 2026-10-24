"""NDJSON wire protocol for the Robot Agent Runtime UDS transport."""

import asyncio
import json
from typing import Any

from robot_agent_runtime.errors import InvalidRequest

MAX_MESSAGE_BYTES = 1024 * 1024
SERVICE_NAME = "robot-agent-runtime"
SERVICE_VERSION = "0.1.0"


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    while True:
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as error:
            raise InvalidRequest(
                "NDJSON message exceeds 1 MiB",
                details={"max_bytes": MAX_MESSAGE_BYTES},
            ) from error
        if not line:
            return None
        if len(line) > MAX_MESSAGE_BYTES:
            raise InvalidRequest(
                "NDJSON message exceeds 1 MiB",
                details={"max_bytes": MAX_MESSAGE_BYTES},
            )
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise InvalidRequest("Malformed JSON", details={"reason": str(error)}) from error
        if not isinstance(message, dict):
            raise InvalidRequest("NDJSON message must be a JSON object")
        return message


def encode_message(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


async def write_message(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(encode_message(message))
    await writer.drain()


def error_response(
    request_id: str | int | None, code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message, "details": details or {}},
    }
