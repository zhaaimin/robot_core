from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any
from uuid import uuid4


class ServicePhase(str, Enum):
    RECEPTION = 'reception'
    AWAITING_CONSENT = 'awaiting_consent'
    WAITING_FOR_ROBOT = 'waiting_for_robot'
    WAITING_FOR_GUEST = 'waiting_for_guest'
    ESCORTING = 'escorting'
    AWAITING_ARRIVAL_CONFIRMATION = 'awaiting_arrival_confirmation'
    COMPLETED = 'completed'
    HUMAN_ASSISTANCE = 'human_assistance'
    CANCELED = 'canceled'
    RECONCILIATION_REQUIRED = 'reconciliation_required'


@dataclass
class HotelService:
    session_id: str
    service_id: str = ''
    phase: str = ServicePhase.RECEPTION.value
    room_id: str | None = None
    destination_id: str | None = None
    meeting_point_id: str | None = None
    meeting_point: str | None = None
    floor: int | None = None
    robot_id: str | None = None
    runtime_task_id: str | None = None
    return_task_id: str | None = None
    consent_confirmed: bool = False
    guest_ready: bool = False
    arrival_confirmed: bool = False
    error: dict[str, Any] | None = None
    return_error: dict[str, Any] | None = None
    robot_status: str = 'unassigned'
    human_reason: str | None = None
    receipt_code: str | None = None
    escort_intent: bool = False
    return_intent: bool = False
    revision: int = 0

    def __post_init__(self) -> None:
        if not self.service_id:
            self.service_id = uuid4().hex

    def snapshot(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ('receipt_code', 'escort_intent', 'return_intent', 'revision'):
            value.pop(key)
        return value
