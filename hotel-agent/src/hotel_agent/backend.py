from dataclasses import dataclass
from typing import Protocol

from hotel_agent.errors import HotelWorkflowError


@dataclass(frozen=True)
class VerifiedStay:
    receipt_code: str
    room_id: str
    floor: int
    destination_id: str


class HotelBackend(Protocol):
    """Trusted hotel facts own room mapping; model arguments never supply it.

    A real integration must verify its PMS receipt and use an approved map on
    the same floor. These facts do not provide guest tracking or elevator use.
    """

    robot_id: str
    return_destination_id: str
    meeting_point_id: str
    meeting_point: str
    supported_floor: int

    async def verify_receipt(self, receipt_code: str) -> VerifiedStay: ...


class SimulatedHotel:
    """Two authoritative demo receipts, without pretending to operate a PMS.

    Navigation only moves a simulated robot between named map destinations.
    Guest presence, following, access control and elevators are not simulated.
    """

    robot_id = 'butler-1'
    return_destination_id = 'hotel-lobby'
    meeting_point_id = 'hotel-lobby'
    meeting_point = '大堂前台会合点'
    supported_floor = 1

    async def verify_receipt(self, receipt_code: str) -> VerifiedStay:
        rooms = {'DEMO-001': '0108', 'DEMO-002': '0109'}
        room = rooms.get(receipt_code)
        if room is None:
            raise HotelWorkflowError('Hotel system could not verify the reception receipt')
        return VerifiedStay(receipt_code, room, 1, f'room-{room}-door')
