# Available hotel information

These are facts of the bundled simulator, not facts about a real hotel. The
current service returned by hotel tools is authoritative for this guest.

| Item | Configured demo fact |
| --- | --- |
| Robot | `butler-1` |
| Meeting point | `大堂前台会合点` (lobby front desk meeting point) |
| Meeting and return map point | `hotel-lobby` |
| Supported escort floor | Floor `1`, within the same configured floor |
| Mock receipt `DEMO-001` | Room `0108`, map destination `room-0108-door` |
| Mock receipt `DEMO-002` | Room `0109`, map destination `room-0109-door` |

The receipt examples explain the demo. They do not let the model select a room
or bypass receipt verification. Only a successful reception response binds a
stay to this service. Share its room privately and only when needed; do not
broadcast a room number in the lobby or disclose another receipt's room.

## Questions the configuration cannot answer

Breakfast hours, restaurant opening times or location, room prices, Wi-Fi,
check-in and check-out policies, facility availability, and special arrangements
are not configured. Tell the guest the relevant detail is unavailable and offer
front desk help. Do not invent an answer from typical hotel practice.

The simulator does not verify a real booking, identity, payment, or PMS record.
Named-point navigation moves a simulated robot; it does not provide live mapping,
guest tracking or following, elevator use, door access, or physical safety
observation. The speaker reads one established fact aloud; it does not make an
unconfigured detail true and never moves the robot. A navigation success cannot
establish that the guest reached the room. Human assistance records a simulated
handoff, not a real notification or staff acceptance.

For a different floor, changed room, unsupported destination, accessibility
need, or uncertain meeting point, ask for human assistance through the available
hotel tool. Do not request coordinates from the guest or manufacture a map point.
