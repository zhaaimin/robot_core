"""Input events belong to the application, separate from robot task events."""

from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol


EventKind = Literal['wakeup', 'utterance', 'confirm', 'cancel', 'status', 'wait']


@dataclass(frozen=True)
class InteractionEvent:
    event_id: str
    session_id: str
    kind: EventKind
    text: str = ''
    final: bool = True
    proposal_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class InteractionSource(Protocol):
    """Deliver wakeup and recognized text without interpreting model output.

    Event IDs are stable across source retries. Partial ASR results may be
    delivered, but the application must ignore them before durable deduplication
    so a later final result with the same ID remains eligible. Closing input
    releases readers and never implies a physical task has stopped.
    """

    def events(self) -> AsyncGenerator[InteractionEvent, None]: ...
