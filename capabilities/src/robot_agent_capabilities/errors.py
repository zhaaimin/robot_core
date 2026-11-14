from robot_agent_runtime.errors import AgentRuntimeError


class CapabilityConfigurationError(AgentRuntimeError):
    code = "CAPABILITY_CONFIGURATION_INVALID"


class CapabilityNotAllowed(AgentRuntimeError):
    code = "CAPABILITY_NOT_ALLOWED"


class CapabilityTransportError(AgentRuntimeError):
    code = "CAPABILITY_TRANSPORT_UNCERTAIN"
