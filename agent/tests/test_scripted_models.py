"""Contract tests for the scripted robot substitute used by other tests.

Nothing here exercises shipping behaviour: production has one model adapter and
it always needs credentials. These cases pin the substitute vocabulary so the
command-line tests in `test_app.py` fail here first if `scripted_models.py`
stops recognising the commands they send.
"""

import json
import re
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from scripted_models import ScriptedRobotModel
from chat_server import tool_schemas


async def test_scripted_robot_recognizes_documented_commands_and_arguments_exactly() -> None:
    model = ScriptedRobotModel()
    cases: list[tuple[str, str, dict[str, str]]] = [
        ("播报 hello", "robot_speak", {"message": "hello"}),
        ("说：早上好", "robot_speak", {"message": "早上好"}),
        ("  SPEAK: hello world  ", "robot_speak", {"message": "hello world"}),
        ("导航到 大厅", "robot_navigate_to", {"target": "大厅"}),
        ("前往：充电区", "robot_navigate_to", {"target": "充电区"}),
        ("NAVIGATE: room 1", "robot_navigate_to", {"target": "room 1"}),
        ("状态", "robot_get_state", {}),
        ("查询状态", "robot_get_state", {}),
        ("state", "robot_get_state", {}),
        ("查询任务 task-1", "robot_task_status", {"task_id": "task-1"}),
        ("TASK task-2", "robot_task_status", {"task_id": "task-2"}),
        ("取消任务 task-3", "robot_cancel_task", {"task_id": "task-3"}),
        ("取消 task-4", "robot_cancel_task", {"task_id": "task-4"}),
        ("CANCEL task-5", "robot_cancel_task", {"task_id": "task-5"}),
    ]
    ids: set[str] = set()
    for command, name, args in cases:
        message = await model.decide([HumanMessage(content=command)], tool_schemas())
        assert isinstance(message, AIMessage)
        assert message.content == ""
        assert not message.invalid_tool_calls
        assert len(message.tool_calls) == 1
        call = message.tool_calls[0]
        assert call["name"] == name, command
        assert call["args"] == args, command
        assert call["id"] and re.fullmatch(r"[0-9a-f]{32}", call["id"])
        assert call["id"] not in ids
        ids.add(call["id"])


async def test_scripted_robot_does_not_act_on_unsupported_or_incomplete_commands() -> None:
    model = ScriptedRobotModel()
    for command in (
        "请跳舞", "请帮我去大厅再播报你好", "机器人状态", "状态 然后跳舞", "",
        "播报", "说", "speak", "导航到", "navigate", "查询任务", "task", "取消任务", "cancel",
    ):
        message = await model.decide([HumanMessage(content=command)], tool_schemas())
        assert not message.tool_calls, command
        assert "[脚本替身]" in message.content
        assert "固定命令" in message.content


async def test_scripted_robot_rejects_english_command_prefixes_inside_other_words() -> None:
    model = ScriptedRobotModel()
    issued: dict[str, list[dict[str, Any]]] = {}
    for command in (
        "speaker hello", "speaking hello", "speakable hello",
        "navigated room", "navigate_to room",
    ):
        message = await model.decide([HumanMessage(content=command)], tool_schemas())
        if message.tool_calls:
            issued[command] = message.tool_calls
    assert not issued, f"Unsupported words issued robot tools: {issued}"


async def test_scripted_robot_never_emits_an_unavailable_tool() -> None:
    model = ScriptedRobotModel()
    for command, name in (
        ("播报 hello", "robot_speak"),
        ("导航到 大厅", "robot_navigate_to"),
        ("状态", "robot_get_state"),
        ("查询任务 one", "robot_task_status"),
        ("取消任务 one", "robot_cancel_task"),
    ):
        schemas = [schema for schema in tool_schemas() if schema["function"]["name"] != name]
        message = await model.decide([HumanMessage(content=command)], schemas)
        assert not message.tool_calls
        assert name in message.content
        assert "没有开放工具" in message.content


async def test_scripted_robot_help_is_labeled_and_does_not_emit_tools_without_new_human_input() -> None:
    for messages in ([], [AIMessage(content="previous assistant message")]):
        response = await ScriptedRobotModel().decide(messages, tool_schemas())
        assert "[脚本替身]" in response.content
        assert "请输入" in response.content
        assert not response.tool_calls


async def test_scripted_robot_reports_only_current_observations_in_order() -> None:
    observations = [
        {"tool": "robot_speak", "state": "succeeded", "result": {"spoken": True}},
        {"tool": "robot_navigate_to", "state": "failed", "error": {"code": "TEST_FAILURE"}},
    ]
    messages: list[BaseMessage] = [
        ToolMessage(content='{"old": true}', tool_call_id="old"),
        HumanMessage(content="播报 hello"),
        *[ToolMessage(content=json.dumps(observed), tool_call_id=f"current-{index}")
          for index, observed in enumerate(observations)],
    ]
    response = await ScriptedRobotModel().decide(messages, tool_schemas())
    assert not response.tool_calls
    assert response.content.startswith("[脚本替身]")
    assert json.loads(response.content.split("：", 1)[1]) == observations


