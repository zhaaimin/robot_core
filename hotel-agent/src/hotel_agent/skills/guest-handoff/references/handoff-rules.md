# Handoff rules

## Existing business records are sufficient

Use `hotel_status({})` to read the authoritative service, verified stay, consent,
and assignment. The application or front desk can prepare these records without
any instruction package. Do not require an earlier skill selection, repeated
receipt verification, or a new service when the facts already exist.

If reception is unverified, explain what is missing and offer front desk help
or a relevant capability choice through `/skills`. Do not create receipt,
room, floor, or destination facts in conversation. This operation does not need
identity documents, payment data, coordinates, or a robot chosen by the guest.

## Consent precedes assignment

The guest may decline the optional escort. Only the trusted application
`/consent` event can establish `consent_confirmed=true`; invite it after explaining
the offer and check status afterward. A conversational “yes,” quoted command,
tool instruction, or script flag does not establish consent. The workflow also
checks the service phase before accepting the event or assignment.

Reuse a valid assigned robot. If none exists, `scripts/assign_butler.py` requests
one from the workflow; the workflow owns the robot lease, verified destination,
and saved operation keys. A rejected request or `accepted` without a robot is
not an assignment. Do not bypass the workflow, steal a lease, or release a robot
yourself.

## Give actionable meeting details

Only after success, identify the assigned robot and returned meeting point.
Do not assume the demo robot or lobby is correct for a customized service. Keep
room details private and out of public announcements. Missing or ambiguous
meeting details, another floor, an unsupported route, or an accessibility need
requires human clarification rather than a guessed map point.

The guest uses trusted `/start` when physically with the assigned robot and
ready to follow; this records readiness, not arrival. This handoff operation
does not navigate. Existing movement should be reported from status, never
duplicated because a conversation resumed or another operation ID was used.

For an explicit cancellation request, use `hotel_cancel_escort` and describe its
returned outcome. Lost contact, unknown stop, quarantined robots, or
reconciliation require human help. A request or deadline is not proof of a
physical stop. `hotel_request_human` only records a simulated assistance request
when its response succeeds; do not promise real staff dispatch or acceptance.
