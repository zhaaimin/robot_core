from robot_agent_runtime.errors import AgentRuntimeError


class HotelWorkflowError(AgentRuntimeError):
    code = "HOTEL_WORKFLOW_REJECTED"
