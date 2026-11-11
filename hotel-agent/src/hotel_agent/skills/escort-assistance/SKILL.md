---
name: escort-assistance
description: Use when a hotel guest needs to begin an assigned robot escort, check an existing escort, confirm arrival, stop the service, or request help during the escort.
---

# Escort assistance

Help the guest begin or follow an existing assigned escort through its confirmed
outcome. This capability can be selected independently. The application or front
desk may already have prepared reception, consent, and assignment; no earlier
instruction package is required.

Read [references/escort-rules.md](references/escort-rules.md) for readiness,
arrival, and physical uncertainty, and
[references/escort-request-schema.md](references/escort-request-schema.md)
before using `scripts/start_escort.py` or interpreting service results.

## Inspect the current service

Use `hotel_status({})` to recover the authoritative stay, consent, assignment,
meeting point, readiness, and saved task. If reception, consent, or assignment is
missing, explain the required business fact and offer suitable application or
front desk assistance. A relevant capability can be chosen through `/skills`;
do not load one automatically or fabricate prerequisites. Respect refusal of
the optional service. Only the application `/consent` event records consent.

If a valid assignment exists, tell the guest its returned robot identifier and
meeting point privately as needed. Invite the trusted `/start` event when they
are physically with the robot and ready to follow. Recheck status for
`guest_ready=true`; conversational agreement and model flags cannot record it.

## Start or observe the existing escort

When verified reception, consent, assignment, and readiness are present and no
escort task exists, call `run_skill_script` with `name="escort-assistance"`,
`path="scripts/start_escort.py"`, and `args={}`. The workflow owns the verified
destination, robot lease, operation keys, admission checks, and stop semantics.
Never supply coordinates, a room, a robot ID, or confirmation flags.

If a task already exists, observe it with `hotel_status({})` instead of creating
another escort. Use status after meaningful events or a guest request, keeping
the conversation responsive rather than busy-polling. Accepted or running work
is still in progress. Do not replay movement after failure, timeout, cancellation,
or uncertainty.

After navigation succeeds, explain that the robot reached its destination and
invite trusted `/arrived` only once the guest is physically there. Report guest
service complete only when status confirms both `phase="completed"` and
`arrival_confirmed=true`. Report the robot's return separately from actual
`robot_status`, `return_task_id`, and `return_error`.

For a stop request, call `hotel_cancel_escort({})` and explain the returned stop
outcome. Lost contact, an unknown stop, blocked route, inability to follow, or
reconciliation needs human help. Use `hotel_request_human({"reason":"..."})`
and say the simulated assistance request was recorded only after success. Do
not claim real staff contact, a physical stop, or arrival without confirmation.
