"""Bounded text and NDJSON adapters for a wakeup/ASR event source."""

import asyncio
import json
import os
import stat
from collections.abc import AsyncGenerator
from typing import Any, TextIO
from uuid import uuid4

from guide_agent.ports import InteractionEvent
from robot_agent_core.errors import AgentConfigurationError


_MAX_LINE_BYTES = 64 * 1024
_MAX_TEXT = 4096
_CONTROLS = {
    '/wake': 'wakeup', '/confirm': 'confirm', '确认带路': 'confirm',
    '/stop': 'cancel', '/cancel': 'cancel', '停止': 'cancel', '停止带路': 'cancel',
    '/status': 'status', '/wait': 'wait',
}
_KINDS = frozenset({'wakeup', 'utterance', 'confirm', 'cancel', 'status', 'wait'})


class _Lines:
    """Bound each line while waiting on a descriptor without blocking the loop."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._buffer = b''
        self._fd: int | None = None
        try:
            fd = stream.fileno()
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                self._fd = fd
        except (AttributeError, OSError, ValueError):
            pass

    async def _chunk(self) -> bytes:
        fd = self._fd
        if fd is None:
            raise AgentConfigurationError('Input descriptor is unavailable')
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()

        def ready() -> None:
            if future.done():
                return
            try:
                value = os.read(fd, 4096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as error:
                future.set_exception(error)
            else:
                future.set_result(value)

        blocking = os.get_blocking(fd)
        registered = False
        try:
            if blocking:
                os.set_blocking(fd, False)
            loop.add_reader(fd, ready)
            registered = True
            return await future
        finally:
            if registered:
                loop.remove_reader(fd)
            if blocking:
                os.set_blocking(fd, True)

    async def read(self) -> str | None:
        if self._fd is None:
            line = self._stream.readline(_MAX_LINE_BYTES + 1)
            if not line:
                return None
            if len(line.encode('utf-8')) > _MAX_LINE_BYTES:
                raise AgentConfigurationError('Interaction input exceeds 64 KiB')
            return line.rstrip('\r\n')
        while b'\n' not in self._buffer:
            if len(self._buffer) > _MAX_LINE_BYTES:
                raise AgentConfigurationError('Interaction input exceeds 64 KiB')
            chunk = await self._chunk()
            if not chunk:
                if not self._buffer:
                    return None
                break
            self._buffer += chunk
        line, separator, remainder = self._buffer.partition(b'\n')
        self._buffer = remainder if separator else b''
        if len(line) > _MAX_LINE_BYTES:
            raise AgentConfigurationError('Interaction input exceeds 64 KiB')
        try:
            return line.decode('utf-8').rstrip('\r')
        except UnicodeDecodeError as error:
            raise AgentConfigurationError('Interaction input must be UTF-8') from error


def normalize_control(event: InteractionEvent) -> InteractionEvent:
    if event.kind == 'utterance' and event.final and event.text.strip() in _CONTROLS:
        return InteractionEvent(event.event_id, event.session_id, _CONTROLS[event.text.strip()])
    return event


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise AgentConfigurationError('Interaction event contains duplicate fields')
        result[name] = value
    return result


class TextInteractionSource:
    """Console text simulates final recognized utterances and explicit controls."""

    def __init__(self, stream: TextIO, *, session_id: str) -> None:
        self._lines = _Lines(stream)
        self._session_id = session_id

    async def events(self) -> AsyncGenerator[InteractionEvent, None]:
        while (line := await self._lines.read()) is not None:
            text = line.strip()
            if text in {'/exit', 'exit', 'quit'}:
                return
            if not text:
                continue
            if len(text) > _MAX_TEXT:
                raise AgentConfigurationError('Recognized text exceeds 4096 characters')
            yield normalize_control(InteractionEvent(uuid4().hex, self._session_id, 'utterance', text))


class JsonInteractionSource:
    """Accept external wakeup and ASR events; no audio decoding happens here."""

    def __init__(self, stream: TextIO) -> None:
        self._lines = _Lines(stream)

    async def events(self) -> AsyncGenerator[InteractionEvent, None]:
        while (line := await self._lines.read()) is not None:
            if not line.strip():
                continue
            try:
                value = json.loads(line, object_pairs_hook=_unique_object)
            except (ValueError, RecursionError) as error:
                raise AgentConfigurationError('Interaction input must be a JSON event') from error
            if not isinstance(value, dict) or set(value) - {'event_id', 'session_id', 'kind', 'text', 'final', 'proposal_id'}:
                raise AgentConfigurationError('Interaction event has unexpected fields')
            if any(not isinstance(value.get(name), str) or not value[name].strip() or len(value[name]) > 128
                   for name in ('event_id', 'session_id')):
                raise AgentConfigurationError('Interaction event needs stable event_id and session_id')
            kind = value.get('kind')
            text = value.get('text', '')
            final = value.get('final', True)
            if not isinstance(kind, str) or kind not in _KINDS or type(final) is not bool:
                raise AgentConfigurationError('Interaction event has an invalid kind or final flag')
            if not isinstance(text, str) or len(text) > _MAX_TEXT or (kind == 'utterance' and not text.strip()):
                raise AgentConfigurationError('Interaction event has invalid recognized text')
            if kind != 'utterance' and (text or not final):
                raise AgentConfigurationError('Control events cannot carry recognized text or a partial flag')
            proposal_id = value.get('proposal_id')
            if proposal_id is not None and (kind != 'confirm' or not isinstance(proposal_id, str)
                                            or not proposal_id.strip() or len(proposal_id) > 128):
                raise AgentConfigurationError('Only confirmation may specify a bounded proposal_id')
            yield normalize_control(InteractionEvent(value['event_id'], value['session_id'], kind, text, final, proposal_id))
