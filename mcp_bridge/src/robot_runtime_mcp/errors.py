from typing import Any

from robot_agent_runtime.errors import AgentRuntimeError


class McpTransportError(AgentRuntimeError):
    """A lost reply cannot establish whether a physical action was accepted.

    Callers retain the original idempotency key and query/reconcile explicitly;
    reconnecting, retrying or cancelling is never an implicit recovery action.
    """

    code = "MCP_TRANSPORT_ERROR"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, {**(details or {}), "outcome_uncertain": True})


class McpRuntimeError(AgentRuntimeError):
    """Preserve a runtime business error without requiring a mirrored registry."""

    def __init__(self, message: str, details: dict[str, Any], *, code: str) -> None:
        self.code = code
        super().__init__(message, details)
