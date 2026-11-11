from typing import Any

from hotel_agent.errors import HotelWorkflowError
from robot_agent_core.ports import Observation
from robot_agent_core.skillkit.scripts import ScriptContext


async def run(arguments: dict[str, Any], context: ScriptContext) -> Observation:
    if not isinstance(arguments, dict) or arguments:
        raise HotelWorkflowError('Assignment script requires exactly an empty object')
    return await context.tools.invoke(
        'hotel_assign_butler', {}, operation_id=context.operation_id,
    )
