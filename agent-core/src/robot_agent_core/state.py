from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from robot_agent_core.ports import Observation


class PendingAction(TypedDict):
    name: str
    args: dict[str, Any]
    call_id: str
    operation_id: str
    script_fingerprint: NotRequired[str]


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    pending: list[PendingAction]
    observations: list[Observation]
    model_turns: int
    halted: bool
    runtime_id: str
    active_skill: str
    loaded_skills: list[str]
    skill_versions: dict[str, str]
    skill_documents: dict[str, dict[str, Any]]
    loaded_resources: list[dict[str, str]]
