"""Validate shared settings without importing application composition."""

import math

from robot_agent_core.errors import AgentConfigurationError


def positive_timeout(value: float, name: str) -> float:
    """Accept only a finite positive deadline, so a bad setting fails at assembly.

    A silent zero or non-finite timeout would make every external call look
    instantly expired, which is indistinguishable from a broken robot.
    """
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise AgentConfigurationError(f"{name} must be finite and positive")
    return float(value)
