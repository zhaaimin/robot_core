import jsonschema

from robot_agent_runtime.errors import (
    InternalError,
    PermissionDenied,
    SchemaValidationError,
)
from robot_agent_runtime.models import SkillCall
from robot_agent_runtime.ports import Skill
from robot_agent_runtime.registry import SkillRegistry


class PolicyEnforcer:
    """Admission policy applied to every step before a task is queued.

    Two rules are enforced: a call must be well formed against the skill's own
    input schema, and the skill's declared permissions must be covered by the
    deployment's allowlist. Both are properties of the request and the skill
    definition — nothing here observes the robot. Semantic validity of a
    parameter is left to the skill itself.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        allowed_permissions: set[str] | None = None,
    ) -> None:
        self._registry = registry
        self._allowed_permissions = allowed_permissions

    def validate(self, call: SkillCall) -> Skill:
        skill = self._registry.find(call.skill)
        definition = skill.definition
        try:
            validator = jsonschema.validators.validator_for(definition.input_schema)
            validator.check_schema(definition.input_schema)
        except jsonschema.SchemaError as error:
            raise InternalError(
                f"Invalid JSON Schema for skill {call.skill}: {error.message}",
                details={
                    "skill": call.skill,
                    "exception_type": type(error).__name__,
                },
            ) from error
        try:
            jsonschema.validate(call.params, definition.input_schema)
        except jsonschema.ValidationError as error:
            raise SchemaValidationError(
                f"Invalid parameters for skill {call.skill}: {error.message}",
                details={
                    "skill": call.skill,
                    "path": list(error.absolute_path),
                },
            ) from error

        if self._allowed_permissions is not None:
            missing = definition.permissions - self._allowed_permissions
            if missing:
                raise PermissionDenied(
                    f"Permissions denied for skill {call.skill}",
                    details={"skill": call.skill, "missing": sorted(missing)},
                )
        return skill
