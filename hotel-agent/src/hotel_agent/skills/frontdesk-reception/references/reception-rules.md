# Reception rules

## Use the minimum private information

First determine whether the guest wants information or receipt verification.
Use their preferred language and current conversation. General information
questions need no receipt. For an existing service, read `hotel_status({})`;
reuse its verified stay instead of restarting reception.

A new reception requires a check-in receipt code supplied privately by the
guest. A room number, name, desired destination, or reference example does not
verify a stay. Do not collect payment details, identity documents, coordinates,
or information the available operation does not need. Ask one short question
for the missing code, then pass it unchanged to `scripts/query_reception.py`.

Only a successful response establishes the guest's room, floor, and named map
destination. If the receipt is rejected, preserve that uncertainty and offer a
corrected code or front desk assistance. Do not infer a room from the receipt
examples or disclose another guest's room. Share this guest's room only when
needed and privately, never in a public robot announcement.

## Finish reception without committing to an escort

Receipt verification and hotel information are useful standalone outcomes. An
optional escort may be explained after reception; do not treat interest or a
conversation's “yes” as consent. Only the application's trusted `/consent`
event can establish `consent_confirmed=true`. The workflow owns this fact.

This capability does not assign a robot or start navigation. If the guest asks
for those actions, explain the available assistance selection through `/skills`
or offer front desk help. Do not automatically activate another capability.
An existing assignment or task in status is a fact to report accurately, not a
reason to create another service.

For unsupported rooms, other floors, accessibility needs, an uncertain meeting
point, or unavailable hotel facts, use `hotel_request_human` with a concise
reason. A successful response records simulated assistance only; it does not
prove that real staff were notified or accepted the case. An error leaves even
that recorded handoff unconfirmed.
