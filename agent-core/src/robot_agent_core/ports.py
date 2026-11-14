"""Describe the tool boundary the graph orchestrates without naming any adapter.

`ToolSet` is the only contract the decision loop needs: a schema list plus one
invoke call that returns an `Observation`. Robot and business adapters are
structural implementations of it, so adding a scene never changes the graph.
An `Observation` is a factual record of what a call produced; it is not a new
instruction and callers must not reinterpret `accepted` as completion.
"""

from typing import Any, Protocol

from robot_agent_capabilities.observations import Observation, failed_observation, task_observation
from robot_agent_capabilities.ports import TaskRuntime


class ToolSet(Protocol):
    """Keep graph orchestration independent of robot and business adapters."""

    @property
    def schemas(self) -> list[dict[str, Any]]: ...

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Observation: ...
