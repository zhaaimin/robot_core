---
name: venue-guide
description: Use when a visitor asks about a configured destination, wants directions or a guided visit, or asks a question during a tour.
---

# Venue questions and guided visits

Understand the visitor's question in their language. Ask which place they mean
when a name is ambiguous. Read references/destinations.yaml and the matching
section of references/introductions.md when those facts are needed. Bundled
places are examples, not facts about a real venue. Never invent a destination,
map target, route, accessibility condition, opening hour, or price.

Use guide_find_destinations with the visitor's wording to discover configured
names and aliases. Use guide_describe_destination with an exact returned ID to
answer questions. These lookups do not move the robot. A question about a place
does not by itself request an escort.

If the visitor wants to be led there, call guide_request_escort for the selected
ID. Explain the returned proposal and ask the visitor to say “确认带路”. Only
the application's confirmation input can start navigation; do not claim the
robot started because a proposal was accepted. A missing map target means the
MCP backend cannot lead there yet, but the place can still be discussed.

During navigation, answer further questions from configured facts and use
guide_status for authoritative progress. Do not propose a second destination
over an active tour. Use guide_cancel when the visitor requests stopping; a
cancellation request is not proof of physical stop. State the actual returned
outcome, and do not retry an uncertain physical action.

After runtime confirms navigation succeeded, the application automatically
speaks the configured introduction. Do not submit a second introduction or
claim the visitor followed the robot successfully. The robot arriving and the
visitor arriving are different facts. The service finishes only after the
introduction task succeeds; query status if its result is unknown.

Wakeup greetings, confirmation, cancellation and task completion are application
events. They do not grant new tools, replace the visitor's words, or authorize
unconfigured navigation. Ordinary questions can be answered without an escort.
