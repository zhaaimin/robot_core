import math
from dataclasses import dataclass

from robot_agent_runtime.errors import InvalidRequest


@dataclass(frozen=True)
class RuntimeOptions:
    """Bound actual owner loops, including validation, without replacing workers.

    Shared/group objects count once per loop and share that loop's call limits.
    The kernel, caller-mode callback pool and hosted listener loops have separate
    thread budgets. Hosted callback capacity includes both queued and running work.
    """

    port_timeout_s: float = 5.0
    cancel_grace_s: float = 0.2
    max_extension_threads: int = 32
    max_pending_calls: int = 256
    max_control_calls: int = 16
    max_subscriptions: int = 256
    max_snapshot_bytes: int = 1_048_576
    max_snapshot_depth: int = 64
    event_page_size: int = 128
    max_event_buffer: int = 1000
    max_listener_threads: int = 16
    max_pending_listener_callbacks: int = 64
    listener_queue_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        for name in ("port_timeout_s", "cancel_grace_s", "listener_queue_timeout_s"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise InvalidRequest(f"{name} must be finite and positive")
        for name in (
            "max_extension_threads", "max_pending_calls", "max_control_calls",
            "max_subscriptions", "max_snapshot_bytes", "max_snapshot_depth",
            "event_page_size", "max_event_buffer",
            "max_listener_threads", "max_pending_listener_callbacks",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidRequest(f"{name} must be a positive integer")
