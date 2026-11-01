import jsonschema

from robot_agent_runtime.errors import (
    InternalError,
    PermissionDenied,
    PreconditionFailed,
    SchemaValidationError,
)
from robot_agent_runtime.models import SkillCall
from robot_agent_runtime.ports import RobotStateProvider, Skill
from robot_agent_runtime.registry import SkillRegistry


class PolicyEnforcer:
    """Admission policy applied to every step before a task is queued.

    Three rules are enforced: a call must be well formed against the skill's
    own input schema, the skill's declared permissions must be covered by the
    deployment's allowlist, and the robot conditions the skill declares must
    currently hold. Semantic validity of a parameter is left to the skill.

    The first two rules are properties of the request and the skill definition
    alone, so they are checked first and deterministically. The third observes
    the robot, which makes it a snapshot: :meth:`unsatisfied` is taken once per
    submission so that every step of a task is judged against the same reading,
    and passing it here is only an early-failure optimisation — the binding
    check happens in :class:`WorkflowExecutor` as each step starts.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        allowed_permissions: set[str] | None = None,
        robot_state: RobotStateProvider | None = None,
    ) -> None:
        self._registry = registry
        self._allowed_permissions = allowed_permissions
        self._robot_state = robot_state

    async def unsatisfied(self) -> frozenset[str]:
        """Snapshot the robot conditions that currently do not hold."""
        if self._robot_state is None:
            return frozenset()
        return frozenset(await self._robot_state.unsatisfied())

    def validate(self, call: SkillCall, unsatisfied: frozenset[str]) -> Skill:
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

        blocked = definition.preconditions & unsatisfied
        if blocked:
            raise PreconditionFailed(
                f"Robot preconditions not met for skill {call.skill}",
                details={"skill": call.skill, "unsatisfied": sorted(blocked)},
            )
        return skill
