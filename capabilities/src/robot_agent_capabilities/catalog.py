import re
from typing import Any

from pydantic import BaseModel

from robot_agent_capabilities.errors import CapabilityConfigurationError
from robot_agent_runtime.models import SkillDefinition
from robot_agent_runtime.schemas import describe_bounds


STATUS_TOOL = "robot_task_status"
CANCEL_TOOL = "robot_cancel_task"
_NAMES = {
    "audio.speak": ("robot_speak", "让机器人播报 message，成功表示播报技能已完成。"),
    "navigation.navigate_to": ("robot_navigate_to", "让机器人导航到 target，返回真实任务状态。"),
    "robot.get_state": ("robot_get_state", "查询机器人状态，返回技能提供的状态快照。"),
}


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    skill: str | None = None
    read_only: bool = False


def build_catalog(
    definitions: list[SkillDefinition], allowed_skills: set[str] | None,
) -> tuple[list[ToolDefinition], dict[str, SkillDefinition]]:
    snapshots = [definition.model_copy(deep=True) for definition in definitions]
    if allowed_skills is not None:
        missing = allowed_skills - {definition.name for definition in snapshots}
        if missing:
            raise CapabilityConfigurationError("Unknown allowed skills", {"skills": sorted(missing)})
        snapshots = [definition for definition in snapshots if definition.name in allowed_skills]
    tools: list[ToolDefinition] = []
    skills: dict[str, SkillDefinition] = {}
    for definition in snapshots:
        name, fallback = _NAMES.get(definition.name, (
            "robot_" + re.sub(r"[^A-Za-z0-9_-]", "_", definition.name),
            f"执行机器人技能 {definition.name}，返回 task_id、state、result 和 error。",
        ))
        if (not re.search(r"[A-Za-z0-9]", definition.name) or len(name) > 64
                or name in skills or name in {STATUS_TOOL, CANCEL_TOOL}):
            raise CapabilityConfigurationError("Skill tool names collide or cannot be represented")
        skills[name] = definition
        tools.append(ToolDefinition(name=name, description=definition.description or fallback,
                                    input_schema=describe_bounds(definition.input_schema), skill=definition.name))
    for name, description in (
        (STATUS_TOOL, "按 task_id 查询原任务；执行中不等于完成。"),
        (CANCEL_TOOL, "按 task_id 显式请求取消；仅根据返回状态判断是否确认停止。"),
    ):
        tools.append(ToolDefinition(name=name, description=description, read_only=name == STATUS_TOOL,
                                    input_schema={"type": "object", "properties": {
                                        "task_id": {"type": "string", "minLength": 1},
                                    }, "required": ["task_id"], "additionalProperties": False}))
    return tools, skills
