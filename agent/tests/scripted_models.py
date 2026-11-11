"""Scripted decision substitutes for the robot command-line tests.

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

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage


class ScriptedRobotModel:
    """Deterministic tool-calling substitute for the robot scenario.

    It only recognizes the commands the robot tests exercise. The real graph,
    SDK, task and simulator still execute underneath it.
    """

    async def aclose(self) -> None:
        return

    async def decide(
        self, messages: Sequence[BaseMessage], tools: list[dict[str, Any]],
    ) -> AIMessage:
        if messages and isinstance(messages[-1], ToolMessage):
            observations = []
            for message in reversed(messages):
                if not isinstance(message, ToolMessage):
                    break
                observations.append(json.loads(str(message.content)))
            return AIMessage(content="[脚本替身] 工具执行完成：" + json.dumps(
                list(reversed(observations)), ensure_ascii=False,
            ))
        if not messages or not isinstance(messages[-1], HumanMessage):
            return AIMessage(content="[脚本替身] 请输入：播报 内容、导航到 目标、状态、查询任务 ID 或取消任务 ID。")
        text = str(messages[-1].content).strip()
        choices = (
            (r"(?:播报|说)\s*[:：]?\s*(.+)", "robot_speak", "message"),
            (r"speak(?:\s+|[:：]\s*)(.+)", "robot_speak", "message"),
            (r"(?:导航到|前往)\s*[:：]?\s*(.+)", "robot_navigate_to", "target"),
            (r"navigate(?:\s+|[:：]\s*)(.+)", "robot_navigate_to", "target"),
            (r"(?:查询任务|task)\s+(.+)", "robot_task_status", "task_id"),
            (r"(?:取消任务|取消|cancel)\s+(.+)", "robot_cancel_task", "task_id"),
        )
        name: str | None = None
        args: dict[str, Any] = {}
        if text in {"状态", "查询状态", "state"}:
            name = "robot_get_state"
        else:
            for pattern, tool_name, param in choices:
                match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
                if match:
                    name, args = tool_name, {param: match.group(1).strip()}
                    break
        available = {tool["function"]["name"] for tool in tools}
        if name is None:
            return AIMessage(content="[脚本替身] 只识别固定命令：播报 内容、导航到 目标、状态、查询任务 ID、取消任务 ID。")
        if name not in available:
            return AIMessage(content=f"[脚本替身] 当前平台没有开放工具 {name}。")
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": uuid4().hex}])


def scripted_model() -> ScriptedRobotModel:
    return ScriptedRobotModel()
