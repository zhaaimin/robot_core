import copy
from typing import Any

import jsonschema

from hotel_agent.errors import HotelWorkflowError
from hotel_agent.models import ServicePhase
from hotel_agent.service import HotelWorkflow
from robot_agent_core.errors import AgentToolNotAllowed
from robot_agent_core.ports import Observation, failed_observation
from robot_agent_runtime.errors import AgentRuntimeError


_SPECS = {
    "hotel_confirm_reception": (
        "用酒店系统已核验的入住凭据确认房间；不替代真实 PMS 核验或支付。",
        {"receipt_code": {"type": "string", "minLength": 1, "maxLength": 128}},
    ),
    "hotel_assign_butler": ("在客人通过界面确认带路后分派管家；机器人与目的地由应用确定。", {}),
    "hotel_start_escort": ("在客人通过界面确认准备好后发起异步带房；受理不等于到达。", {}),
    "hotel_announce": (
        "让机器人播报一条已核验的酒店信息（设施位置、营业时间等）；"
        "播报结果只是任务结果，不代表入住、同意、到达或带房完成。",
        {"message": {"type": "string", "minLength": 1, "maxLength": 200}},
    ),
    "hotel_status": ("查询当前客人的权威服务状态、机器人和任务结果。", {}),
    "hotel_cancel_escort": ("显式请求取消本次服务，依据真实停止确认结果返回状态。", {}),
    "hotel_request_human": (
        "请求前台人工协助；首版仅记录模拟交接，不能声称真实工作人员已经接单。",
        {"reason": {"type": "string", "minLength": 1, "maxLength": 2000}},
    ),
}


class HotelTools:
    """Expose business actions while keeping confirmations outside model control.

    Navigation destinations and robot IDs come from the workflow. Hotel actions
    may be accepted while their physical task is still running; only a completed
    business phase with a guest arrival confirmation means the service finished.
    An announcement has no business phase at all, so it is reported as the plain
    task observation of the speaker instead of a service snapshot.
    """

    def __init__(self, workflow: HotelWorkflow, session_id: str) -> None:
        self._workflow = workflow
        self._session_id = session_id

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {
                "type": "object", "properties": copy.deepcopy(properties),
                "required": list(properties), "additionalProperties": False,
            },
        }} for name, (description, properties) in _SPECS.items()]

    async def describe(self) -> dict[str, Any]:
        snapshot = await self._workflow.refresh(self._session_id)
        return {
            "workflow": "hotel_reception_escort", "facts": snapshot,
            "confirmation_policy": "只有应用界面 /consent、/start、/arrived 可以记录客人确认；模型不能代替确认。",
            "completion_policy": "导航成功只代表机器人已到目标点，客人到达确认之后才能结束带房。",
        }

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Observation:
        try:
            spec = _SPECS.get(name)
            if spec is None:
                raise AgentToolNotAllowed("Hotel workflow does not expose this tool", details={"tool": name})
            if not isinstance(operation_id, str) or not operation_id:
                raise HotelWorkflowError("A stable operation ID is required")
            properties = spec[1]
            try:
                jsonschema.validate(args, {
                    "type": "object", "properties": properties,
                    "required": list(properties), "additionalProperties": False,
                })
            except jsonschema.ValidationError:
                raise HotelWorkflowError("Hotel tool arguments did not match the registered schema") from None
            if name == "hotel_announce":
                return await self._workflow.announce(
                    self._session_id, args["message"], operation_id=operation_id,
                )
            if name == "hotel_confirm_reception":
                result = await self._workflow.confirm_reception(
                    self._session_id, args["receipt_code"], operation_id=operation_id,
                )
            elif name == "hotel_assign_butler":
                result = await self._workflow.assign_robot(self._session_id, operation_id=operation_id)
            elif name == "hotel_start_escort":
                result = await self._workflow.start_escort(self._session_id, operation_id=operation_id)
            elif name == "hotel_cancel_escort":
                result = await self._workflow.cancel(self._session_id, operation_id=operation_id)
            elif name == "hotel_request_human":
                result = await self._workflow.request_human(
                    self._session_id, args["reason"], operation_id=operation_id,
                )
            else:
                result = await self._workflow.refresh(self._session_id)
            attention = result["phase"] in {
                ServicePhase.HUMAN_ASSISTANCE.value, ServicePhase.RECONCILIATION_REQUIRED.value,
                ServicePhase.CANCELED.value,
            }
            return {
                "tool": name, "task_id": result.get("runtime_task_id"),
                "state": "succeeded" if result["phase"] == ServicePhase.COMPLETED.value else "accepted",
                "result": result, "error": result.get("error"), "requires_attention": attention,
            }
        except AgentRuntimeError as error:
            return failed_observation(name, error.info)
