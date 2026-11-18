from robot_agent_runtime.errors import AgentRuntimeError


class CapabilityConfigurationError(AgentRuntimeError):
    code = "CAPABILITY_CONFIGURATION_INVALID"


class CapabilityNotAllowed(AgentRuntimeError):
    code = "CAPABILITY_NOT_ALLOWED"


class CapabilityTransportError(AgentRuntimeError):
    code = "CAPABILITY_TRANSPORT_UNCERTAIN"


class CapabilityEventsUnsupported(AgentRuntimeError):
    code = "CAPABILITY_EVENTS_UNSUPPORTED"


class CapabilityEventTransportError(AgentRuntimeError):
    code = "CAPABILITY_EVENT_TRANSPORT_FAILED"


class CapabilityEventCleanupTimeout(AgentRuntimeError):
    code = "CAPABILITY_EVENT_CLEANUP_TIMEOUT"
