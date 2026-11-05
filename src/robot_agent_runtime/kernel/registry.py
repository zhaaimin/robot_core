from collections.abc import Mapping

from robot_agent_runtime.errors import SkillNotFound
from robot_agent_runtime.models import SkillDefinition
from robot_agent_runtime.ports import Skill


class SkillRegistry:
    """Name-based lookup over the registered skill set."""

    def __init__(self, skills: Mapping[str, Skill]) -> None:
        self._skills = skills

    def find(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as error:
            raise SkillNotFound(
                f"Skill not found: {name}", details={"skill": name}
            ) from error

    def list_skills(self) -> list[SkillDefinition]:
        return [skill.definition for _, skill in sorted(self._skills.items())]
