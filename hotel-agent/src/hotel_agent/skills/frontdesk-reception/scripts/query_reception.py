from typing import Any

from hotel_agent.errors import HotelWorkflowError
from robot_agent_core.ports import Observation
from robot_agent_core.skillkit.scripts import ScriptContext


async def run(arguments: dict[str, Any], context: ScriptContext) -> Observation:
    if not isinstance(arguments, dict) or set(arguments) != {'receipt_code'}:
        raise HotelWorkflowError('Reception script requires exactly one receipt_code')
    receipt_code = arguments['receipt_code']
    if (not isinstance(receipt_code, str) or not receipt_code.strip()
            or len(receipt_code) > 128):
        raise HotelWorkflowError('Reception receipt code must be a nonblank string of at most 128 characters')
    return await context.tools.invoke(
        'hotel_confirm_reception', {'receipt_code': receipt_code},
        operation_id=context.operation_id,
    )
