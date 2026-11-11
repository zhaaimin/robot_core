from robot_agent_runtime.errors import AgentRuntimeError


class AgentConfigurationError(AgentRuntimeError):
    code = "AGENT_CONFIGURATION_INVALID"


class AgentToolNotAllowed(AgentRuntimeError):
    code = "AGENT_TOOL_NOT_ALLOWED"


class AgentToolArgumentsError(AgentRuntimeError):
    """Keep malformed host arguments distinct from missing execution authority."""

    code = "INVALID_REQUEST"


class AgentActionSkipped(AgentRuntimeError):
    code = "AGENT_ACTION_SKIPPED"


class AgentModelError(AgentRuntimeError):
    code = "AGENT_MODEL_FAILED"


class AgentStepLimit(AgentRuntimeError):
    code = "AGENT_STEP_LIMIT"


class AgentTransportError(AgentRuntimeError):
    code = "AGENT_TRANSPORT_UNCERTAIN"


class AgentSkillNotLoaded(AgentRuntimeError):
    code = "AGENT_SKILL_NOT_LOADED"


class AgentSkillScriptError(AgentRuntimeError):
    code = "AGENT_SKILL_SCRIPT_FAILED"


class AgentSkillScriptTimeout(AgentRuntimeError):
    code = "AGENT_SKILL_SCRIPT_TIMEOUT"
