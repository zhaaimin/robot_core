from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class GuideSession:
    session_id: str
    revision: int = 0
    phase: str = 'idle'
    epoch: int = 0
    dialogue_operation_id: str | None = None
    tour_id: str | None = None
    destination_id: str | None = None
    fingerprint: str | None = None
    introduction: str | None = None
    target: str | None = None
    navigation_task_id: str | None = None
    introduction_task_id: str | None = None
    navigation_intent: bool = False
    introduction_intent: bool = False
    robot_status: str = 'available'
    error: dict[str, Any] | None = None
    awake: bool = False
    stopping: bool = False
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop('operations')
        result.pop('revision')
        return result

