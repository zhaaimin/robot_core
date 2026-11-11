"""Scripted decision substitutes for the hotel command-line tests.

Shipping code contains no fake brain: the application always requires an
OpenAI-compatible model, and `conftest.py` wires these substitutes in at the
`create_model` seam. Recognising fixed command text here is a test fixture, not
a product capability, so nothing outside `tests/` may import it.
"""

import json
import re
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


_HELP = (
    '[脚本替身] 先用 /skills 查看目录，再用 /skill NAME 显式选择技能。'
    '只识别固定命令：入住 DEMO-001、酒店信息、/consent（同意带路）、分派管家、'
    '/start（准备出发）、/wait（等待导航）、/arrived（客人确认到达）、'
    '/status、/cancel、转人工 原因；/guest 会话ID 切换客人。'
)

_RULES = {
    'frontdesk-reception': 'reception',
    'guest-handoff': 'handoff',
    'escort-assistance': 'escort',
}


class ScriptedHotelModel:
    """Skill-aware substitute for the hotel agent tests.

    Guest confirmations still belong to the application input boundary; this
    only chooses an operation inside the last successfully loaded Skill, so
    sharing one instance never transfers selections between guests.
    """

    async def aclose(self) -> None:
        return

    async def decide(
        self, messages: Sequence[BaseMessage], tools: list[dict[str, Any]],
    ) -> AIMessage:
        conversation = [message for message in messages if not isinstance(message, SystemMessage)]
        human_index = next((
            index for index in range(len(conversation) - 1, -1, -1)
            if isinstance(conversation[index], HumanMessage)
        ), None)
        if human_index is None:
            return AIMessage(content=_HELP)
        observed: list[tuple[ToolMessage, dict[str, Any]]] = []
        selected: str | None = None
        for index, message in enumerate(conversation):
            if not isinstance(message, ToolMessage):
                continue
            try:
                value = json.loads(str(message.content))
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            result = value.get('result')
            tool_name = message.name or value.get('tool')
            if (
                tool_name == 'load_skill' and value.get('state') == 'succeeded'
                and isinstance(result, dict) and isinstance(result.get('name'), str)
            ):
                selected = result['name']
            if index > human_index:
                observed.append((message, value))
        if selected is None:
            return AIMessage(content=_HELP)

        text = str(conversation[human_index].content).strip()
        receipt = re.fullmatch(r'入住\s+([^\s]+)', text)
        human = re.fullmatch(r'转人工\s+(.+)', text)
        announcement = re.fullmatch(r'播报\s+(.+)', text)
        required: str | None = None
        name: str | None = None
        args: dict[str, Any] = {}
        if receipt:
            required, name = 'frontdesk-reception', 'run_skill_script'
            args = {
                'name': required, 'path': 'scripts/query_reception.py',
                'args': {'receipt_code': receipt.group(1)},
            }
        elif text in {'/consent', '分派管家'}:
            required, name = 'guest-handoff', 'run_skill_script'
            args = {'name': required, 'path': 'scripts/assign_butler.py', 'args': {}}
        elif text in {'/start', '开始带路'}:
            required, name = 'escort-assistance', 'run_skill_script'
            args = {'name': required, 'path': 'scripts/start_escort.py', 'args': {}}
        elif text == '/arrived':
            required, name = 'escort-assistance', 'hotel_status'
        elif text in {'酒店信息', '早餐时间', '退房时间'}:
            required, name = 'frontdesk-reception', 'read_skill_resource'
            args = {'name': required, 'path': 'references/hotel-information.md'}
        elif announcement and selected == 'frontdesk-reception':
            required, name = 'frontdesk-reception', 'hotel_announce'
            args = {'message': announcement.group(1).strip()}
        elif human and selected in _RULES:
            required, name = selected, 'hotel_request_human'
            args = {'reason': human.group(1).strip()}
        if name is None or required is None:
            return AIMessage(content=_HELP)
        if selected != required:
            enabled = next((
                schema['function'].get('parameters', {}).get('properties', {}).get('name', {}).get('enum')
                for schema in tools if schema['function']['name'] == 'load_skill'
            ), None)
            if isinstance(enabled, list) and required not in enabled:
                return AIMessage(content=(
                    f'[脚本替身] 当前目录未启用 {required} 能力，无法处理此操作；'
                    '请用 /skills 查看已启用的能力。'
                ))
            return AIMessage(content=(
                f'[脚本替身] 当前技能 {selected} 不处理此操作；'
                f'请使用 /skill {required} 选择对应能力。'
            ))

        if any(
            value.get('requires_attention') or (message.name or value.get('tool')) == name
            for message, value in observed
        ):
            return AIMessage(content='[脚本替身] 工具返回事实：' + json.dumps(
                [value for _, value in observed], ensure_ascii=False,
            ))
        rule = _RULES[selected]
        references = (
            ['references/hotel-information.md'] if name == 'read_skill_resource'
            else [f'references/{rule}-rules.md', f'references/{rule}-request-schema.md']
        )
        read_paths = {
            value['result'].get('path') for message, value in observed
            if (message.name or value.get('tool')) == 'read_skill_resource'
            and value.get('state') == 'succeeded' and isinstance(value.get('result'), dict)
            and value['result'].get('name') == selected
        }
        reference = next((path for path in references if path not in read_paths), None)
        if reference is not None:
            name, args = 'read_skill_resource', {'name': selected, 'path': reference}
        available = {schema['function']['name'] for schema in tools}
        if name not in available:
            return AIMessage(content=f'[脚本替身] 当前平台没有开放工具 {name}。')
        return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': uuid4().hex}])


def scripted_model() -> ScriptedHotelModel:
    return ScriptedHotelModel()
