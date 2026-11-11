import asyncio
import codecs
import io
import json
import os
import stat
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TextIO

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph

from robot_agent_core.config import AppConfig
from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.graph import AgentContext
from robot_agent_core.skillkit.selection import handle_skill_command
from robot_agent_runtime.errors import AgentRuntimeError


async def invoke_turn(
    graph: CompiledStateGraph, context: AgentContext, *, thread_id: str,
    runtime_id: str, prompt: str | None,
) -> dict[str, Any]:
    command = prompt.strip().split(maxsplit=1) if prompt is not None else []
    if prompt is not None and command and command[0] in {'/skill', '/skills'}:
        if context.skills is None:
            raise AgentConfigurationError('Skill commands require a configured Agent Skill catalog')
        selected = await handle_skill_command(
            graph, context.skills, thread_id=thread_id, runtime_id=runtime_id, prompt=prompt,
        )
        if selected is not None:
            return selected
    config = {
        'configurable': {'thread_id': thread_id},
        'recursion_limit': 3 * context.max_turns + 5,
    }
    snapshot = await graph.aget_state(config)
    if snapshot.values and snapshot.values.get('runtime_id') != runtime_id:
        raise AgentConfigurationError('Checkpoint belongs to a different robot runtime or backend')
    if prompt is None:
        if not snapshot.values:
            raise AgentConfigurationError('No checkpoint exists for this thread')
        if not snapshot.next:
            raise AgentConfigurationError('No pending work exists to resume')
        state = None
    else:
        if not prompt.strip():
            raise AgentConfigurationError('Prompt must not be empty')
        if snapshot.next:
            raise AgentConfigurationError('Thread has pending work; use --resume before adding a prompt')
        state = {
            'messages': [HumanMessage(content=prompt)], 'model_turns': 0,
            'pending': [], 'observations': [], 'halted': False, 'runtime_id': runtime_id,
        }
    return await graph.ainvoke(state, config, context=context, durability='sync')


def result_envelope(
    config: AppConfig, state: dict[str, Any], *, thread_id: str | None = None,
    catalog: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    reply = ''
    for message in reversed(state.get('messages', [])):
        if isinstance(message, AIMessage):
            reply = message.content if isinstance(message.content, str) else message.text
            break
    result = {
        'model': config.model, 'backend': config.backend, 'thread_id': config.thread_id if thread_id is None else thread_id,
        'reply': reply, 'observations': state.get('observations', []),
    }
    if catalog is not None:
        result.update({
            'skill_catalog': catalog, 'active_skill': state.get('active_skill'),
            'loaded_skills': state.get('loaded_skills', []),
            'loaded_resources': state.get('loaded_resources', []),
            'skill_versions': state.get('skill_versions', {}),
        })
    return result


class AsyncPromptReader:
    """Wait for Unix stdin readiness without occupying the platform's loop.

    The application is stdin's sole consumer. Reading raw, nonblocking chunks
    avoids TextIO.readline waiting for a newline after a partial pipe write.
    Buffered text and decoder state survive between prompts; descriptor watches
    and blocking flags never survive a read's completion or cancellation. The
    caller retains stream ownership. StringIO and regular files need no watch.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._buffer = ''
        self._eof = False
        self._fd: int | None = None
        if not isinstance(stream, io.StringIO):
            try:
                fd = stream.fileno()
            except (AttributeError, OSError, ValueError):
                raise AgentConfigurationError('Interactive input requires a Unix descriptor or StringIO') from None
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                self._fd = fd
        decoder = codecs.getincrementaldecoder(getattr(stream, 'encoding', None) or 'utf-8')(
            errors=getattr(stream, 'errors', None) or 'strict',
        )
        self._decoder = io.IncrementalNewlineDecoder(decoder, translate=True)

    async def _read_bytes(self) -> bytes:
        if self._fd is None:
            raise AgentConfigurationError('Input descriptor is unavailable')
        fd = self._fd
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bytes] = loop.create_future()

        def ready() -> None:
            if future.done():
                return
            try:
                data = os.read(fd, 4096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as error:
                future.set_exception(error)
            else:
                future.set_result(data)

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

    async def readline(self) -> str:
        if self._fd is None:
            line = self._stream.readline()
            if not line:
                raise EOFError
            return line.removesuffix('\n').removesuffix('\r')
        while '\n' not in self._buffer:
            if self._eof:
                if not self._buffer:
                    raise EOFError
                line, self._buffer = self._buffer, ''
                return line
            data = await self._read_bytes()
            self._buffer += self._decoder.decode(data, final=not data)
            self._eof = not data
        line, self._buffer = self._buffer.split('\n', 1)
        return line

def is_skill_command(prompt: str | None) -> bool:
    parts = prompt.split(maxsplit=1) if prompt is not None else []
    return bool(parts and parts[0] in {'/skill', '/skills'})


def error_state(
    error: AgentRuntimeError, state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **(state or {}),
        'messages': [AIMessage(content=json.dumps({'error': error.to_dict()}, ensure_ascii=False))],
        'observations': [],
    }


async def run_shell(
    config: AppConfig, *,
    turn: Callable[[str | None], Awaitable[dict[str, Any]]],
    prompt: Callable[[], str],
    recover: Callable[[str, AgentRuntimeError], Awaitable[dict[str, Any]]] | None = None,
    render: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> None:
    """Share input/output while each scene owns commands and recovery policy.

    One-shot errors propagate to the CLI. Interactive recovery is explicit, so
    a scene cannot accidentally turn an ordinary fatal error into a continuing
    conversation or discard the checkpoint used to report a Skill command error.
    """
    if config.resume or config.prompt is not None:
        result = await turn(None if config.resume else config.prompt)
        if render is not None:
            result = await render(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return
    reader = AsyncPromptReader(sys.stdin)
    while True:
        print(prompt(), file=sys.stderr, end='', flush=True)
        try:
            text = await reader.readline()
        except EOFError:
            return
        command = text.strip()
        if command.lower() in {'exit', 'quit', '/exit'}:
            return
        if not command:
            continue
        try:
            result = await turn(text)
        except AgentRuntimeError as error:
            if recover is None:
                raise
            result = await recover(text, error)
        if render is not None:
            result = await render(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
