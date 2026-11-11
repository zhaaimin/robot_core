"""Run guide dialogue and urgent controls while task monitors own completion."""

import asyncio
import io
import json
import sys
from contextlib import aclosing
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from guide_agent.app import GuideConfig
from guide_agent.catalog import DestinationCatalog
from guide_agent.interaction import JsonInteractionSource, TextInteractionSource, normalize_control
from guide_agent.ports import InteractionEvent, InteractionSource
from guide_agent.profiles import GUIDE_PROFILE
from guide_agent.runtime import open_runtime
from guide_agent.service import GuideWorkflow
from guide_agent.tools import GuideTools
from robot_agent_core.configuration import validate_config
from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.graph import AgentContext, build_graph
from robot_agent_core.models import DecisionModel
from robot_agent_core.shell import invoke_turn, is_skill_command, result_envelope
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_runtime.errors import AgentRuntimeError, ExtensionCapacityExceeded


class GuideApplication:
    """Serialize graph updates while cancellation invalidates stale tool authority.

    The input reader handles stop independently of model latency. Every ordinary
    turn captures a workflow epoch; a later stop prevents that model's tools and
    reply from starting a new action. Event IDs are claimed before handling so
    repeated external callbacks cannot repeat greetings or physical requests.
    """

    def __init__(self, config: GuideConfig, model: DecisionModel, workflow: GuideWorkflow,
                 catalog: DestinationCatalog, registry: SkillRegistry, graph: Any) -> None:
        self.config, self.model, self.workflow = config, model, workflow
        self.catalog, self.registry, self.graph = catalog, registry, graph
        self._queue: asyncio.Queue[tuple[InteractionEvent, str | None]] = asyncio.Queue(maxsize=32)
        self._presented: dict[str, str] = {}
        self._cancellations: dict[str, int] = {}

    async def _emit(self, event: InteractionEvent, *, reply: str = '', **extra: Any) -> None:
        snapshot = await self.workflow.status(event.session_id)
        if snapshot['phase'] == 'awaiting_confirmation':
            self._presented[event.session_id] = snapshot['tour_id']
        else:
            self._presented.pop(event.session_id, None)
        print(json.dumps({
            'agent': 'guide', 'backend': self.config.backend, 'session_id': event.session_id,
            'event_id': event.event_id, 'reply': reply,
            'guide': snapshot, **extra,
        }, ensure_ascii=False), flush=True)

    async def handle(self, event: InteractionEvent, *, resume: bool = False,
                     proposal_id: str | None = None) -> bool:
        event = normalize_control(event)
        generation = self._cancellations.get(event.session_id, 0)
        if not event.final:
            print(json.dumps({'agent': 'guide', 'event_id': event.event_id, 'session_id': event.session_id,
                              'ignored': 'partial'}, ensure_ascii=False), flush=True)
            return False
        try:
            if not await self.workflow.claim_event(event.session_id, event.event_id, event.to_dict()):
                await self._emit(event, duplicate=True)
                return False
            if event.kind == 'cancel':
                self._cancellations[event.session_id] = generation + 1
                await self.workflow.cancel(event.session_id, operation_id='input:' + event.event_id)
                await self._emit(event, reply='已处理停止请求，请以当前任务结果为准。')
                return True
            if event.kind == 'wakeup':
                await self.workflow.wake(event.session_id, operation_id='input:' + event.event_id)
                await self._emit(event, reply='您好，请问想了解哪个地点？')
                return False
            if event.kind == 'confirm':
                await self.workflow.confirm_escort(event.session_id, operation_id='input:' + event.event_id,
                                                    expected_tour_id=proposal_id)
                await self._emit(event, reply='已处理带路确认，请以当前导览状态为准。')
                return False
            if event.kind in {'status', 'wait'}:
                if event.kind == 'wait':
                    await self.workflow.wait_for_settled(event.session_id, timeout_s=self.config.wait_timeout_s)
                await self._emit(event)
                return False
            state = await self.workflow.status(event.session_id)
            if self._cancellations.get(event.session_id, 0) != generation:
                raise AgentConfigurationError('This input was superseded by a stop request')
            skill_command = not resume and is_skill_command(event.text)
            checkpoint = await self.graph.aget_state({'configurable': {'thread_id': event.session_id}})
            if not resume and not skill_command and checkpoint.next:
                raise AgentConfigurationError('Thread has pending work; use --resume before adding input')
            epoch = (state['epoch'] if skill_command else
                     await self.workflow.saved_turn_epoch(event.session_id) if resume else
                     await self.workflow.bind_turn(event.session_id, operation_id='turn:' + event.event_id,
                                                   expected_epoch=state['epoch']))
            tools = GuideTools(self.workflow, self.catalog, event.session_id, epoch=epoch)
            context = AgentContext(model=self.model, tools=tools, max_turns=self.config.max_turns,
                                   skills=self.registry, guidance=tools)
            result = await invoke_turn(
                self.graph, context, thread_id=event.session_id, runtime_id=self.config.runtime_id,
                prompt=None if resume else event.text,
            )
            envelope = result_envelope(self.config, result, thread_id=event.session_id)
            speech = None
            current = await self.workflow.status(event.session_id)
            if current['epoch'] != epoch:
                envelope['reply'] = '该轮对话已被停止请求中止。'
            elif envelope['reply'] and not skill_command:
                speech = await self.workflow.speak(event.session_id, envelope['reply'],
                                                  operation_id='reply:' + event.event_id, expected_epoch=epoch)
            await self._emit(event, reply=envelope['reply'], observations=envelope['observations'], speech=speech)
        except AgentRuntimeError as error:
            await self._emit(event, reply=error.info.message, error=error.to_dict())
        return False

    async def _worker(self) -> None:
        while True:
            event, proposal_id = await self._queue.get()
            try:
                await self.handle(event, proposal_id=proposal_id)
            finally:
                self._queue.task_done()

    def _discard_pending(self, session_id: str) -> None:
        retained: list[tuple[InteractionEvent, str | None]] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            self._queue.task_done()
            if item[0].session_id != session_id:
                retained.append(item)
        for item in retained:
            self._queue.put_nowait(item)

    async def _read(self, source: InteractionSource) -> None:
        async with aclosing(source.events()) as events:
            async for event in events:
                event = normalize_control(event)
                if event.kind == 'cancel' or not event.final:
                    applied = await self.handle(event)
                    if event.kind == 'cancel' and applied:
                        self._discard_pending(event.session_id)
                    continue
                try:
                    proposal_id = (event.proposal_id or self._presented.get(event.session_id)
                                   if event.kind == 'confirm' else None)
                    self._queue.put_nowait((event, proposal_id))
                except asyncio.QueueFull:
                    error = ExtensionCapacityExceeded('输入队列已满，请稍后重试。')
                    await self._emit(event, error=error.to_dict())
        await self._queue.join()

    async def run(self, source: InteractionSource) -> None:
        worker = asyncio.create_task(self._worker())
        reader = asyncio.create_task(self._read(source))
        try:
            done, _ = await asyncio.wait({reader, worker}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            worker.cancel()
            reader.cancel()
            await asyncio.gather(reader, worker, return_exceptions=True)


async def run_application(config: GuideConfig, model: DecisionModel, *, source: InteractionSource | None = None) -> None:
    validate_config(config)
    if config.profile != GUIDE_PROFILE or config.business_database is None:
        raise AgentConfigurationError('Guide application requires its own business storage')
    registry = (SkillRegistry.from_directory(config.skills_dir) if config.skills_dir is not None
                else SkillRegistry.bundled('guide_agent'))
    catalog = DestinationCatalog.from_registry(registry)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    async with open_runtime(config) as runtime:
        workflow = GuideWorkflow(str(config.business_database), runtime, catalog, backend=config.backend,
                                 task_timeout_s=config.task_timeout_s, wait_timeout_s=config.wait_timeout_s)
        try:
            await workflow.start()
            async with AsyncSqliteSaver.from_conn_string(str(config.checkpoint_database)) as saver:
                application = GuideApplication(config, model, workflow, catalog, registry, build_graph(checkpointer=saver))
                if config.resume:
                    await application.handle(InteractionEvent('resume:' + uuid4().hex,
                                                               config.thread_id, 'utterance'), resume=True)
                else:
                    if source is None:
                        stream = io.StringIO(config.prompt + '\n') if config.prompt is not None else sys.stdin
                        source = (JsonInteractionSource(stream) if config.input_mode == 'events'
                                  else TextInteractionSource(stream, session_id=config.thread_id))
                    await application.run(source)
        finally:
            await workflow.close()
