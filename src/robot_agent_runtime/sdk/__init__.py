"""Public SDK surface: the embedded and remote runtime clients."""

# Re-exported so that `robot_agent_runtime.sdk.asyncio` stays a valid patch
# point for tests that intercept the client's socket calls.
import asyncio  # noqa: F401

from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.sdk.embedded import RobotRuntime
from robot_agent_runtime.sdk.listeners import TaskListener
from robot_agent_runtime.sdk.remote import (
    RemoteRuntime,
    RemoteRuntimeError,
    RemoteTransportError,
)

__all__ = [
    "RemoteRuntime",
    "RemoteRuntimeError",
    "RemoteTransportError",
    "RobotRuntime",
    "RuntimeOptions",
    "TaskListener",
]
