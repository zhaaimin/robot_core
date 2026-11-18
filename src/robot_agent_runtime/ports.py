from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

from robot_agent_runtime.models import (
    Event,
    PlatformEvent,
    SkillContext,
    SkillDefinition,
    SkillCall,
    SkillResult,
    Task,
    TaskState,
)
from robot_agent_runtime.thread_binding import ThreadBinding


class PlatformEventSource(Protocol):
    """Provide detached platform facts without choosing their delivery transport.

    The application owns vendor connections and source lifecycle. Hosted watch
    calls belong to the source's selected loop; native callbacks must bridge to
    it. Exhaustion or failure is observable, never proof that faults cleared.
    """

    def watch(self) -> AsyncIterator[PlatformEvent]: ...


class PlatformEventRepository(Protocol):
    """Own ordered platform history independently of task transactions.

    Append assigns cursor positions and deduplicates immutable event IDs within
    retained history. Pages are ordered snapshots; expired or future cursors
    fail explicitly. A page may be shorter than limit to respect byte budgets;
    an empty page marks the current end. Bounds are (first retained cursor,
    last assigned cursor).
    """

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def append_platform_event(self, event: PlatformEvent) -> PlatformEvent: ...

    async def list_platform_events(self, after_cursor: int = 0, limit: int = 128) -> list[PlatformEvent]: ...

    async def platform_event_bounds(self) -> tuple[int, int]: ...


class AdmissionValidator(Protocol):
    """Apply admission rules independently of where validation is scheduled.

    The caller takes one condition snapshot for the whole request. Implementors
    raise structured validation errors, never mutate task state, and return only
    after their actual validation result is available. Hosted implementations
    keep schema hooks off the kernel and bound their external work.
    """

    async def validate(self, call: SkillCall, unsatisfied: frozenset[str]) -> None: ...


class Skill(Protocol):
    """External execution owned by its isolation thread while hosted.

    Hosts freeze declarations before admission and invoke execute on an adapter
    loop. Parameters/results are snapshots; cancellation and progress are bridged
    without sharing kernel asyncio objects. An uncertain physical stop must raise
    RobotStopError instead of claiming cancellation was acknowledged.
    Unsupported cancellation never cancels real started work. A supported stop
    requires a successful result or TaskCancelled; CancelledError alone leaves
    physical ownership uncertain and therefore cannot release resources.
    """

    @property
    def definition(self) -> SkillDefinition: ...

    async def execute(
        self, context: SkillContext, params: dict[str, Any]
    ) -> SkillResult: ...


class TaskRepository(Protocol):
    """Atomic persistence on one owned event loop while a runtime is hosted.

    A deadline after dispatch can leave a write outcome unknown. The host poisons
    that repository rather than starting another write alongside it. Calls
    cancelled before dispatch cannot commit and do not poison the repository.
    Callers must not use the same repository outside its owner while hosted.
    """

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def create_task(self, task: Task) -> Task: ...

    async def get_task(self, task_id: str) -> Task | None: ...

    async def update_task(self, task: Task) -> Task: ...

    async def append_event(self, event: Event) -> Event: ...

    async def apply_transition(self, task: Task, event: Event) -> tuple[Task, Event]: ...

    async def list_events(
        self, task_id: str, after_sequence: int = 0
    ) -> list[Event]: ...

    async def get_nonterminal_tasks(self) -> list[Task]: ...

    async def find_by_idempotency_key(self, key: str) -> Task | None: ...


class PagedEventRepository(TaskRepository, Protocol):
    """Optional ordered replay without materializing the whole task history.

    Pages contain at most limit independent snapshots, strictly after the
    supplied sequence. Existing repositories can retain list_events; hosted
    compatibility slicing happens on their worker before crossing the boundary.
    """

    async def list_events_page(
        self, task_id: str, after_sequence: int = 0, limit: int = 128,
    ) -> list[Event]: ...


class Reconciler(Protocol):
    """External authority queried on an adapter loop under a finite deadline.

    A failed or timed-out answer is indeterminate, never permission to replay
    a physical action. The reconciler receives a detached task snapshot.
    """

    async def reconcile(self, task: Task) -> TaskState | None: ...


class RobotStateProvider(Protocol):
    """The robot's own conditions, reported as the labels that do *not* hold.

    Labels are opaque to the runtime. A skill declares what it needs in
    :attr:`SkillDefinition.preconditions`; the runtime only intersects the two
    sets. Mapping ``battery.sufficient`` onto a voltage threshold is the
    adapter's business, so no robot domain model leaks into the kernel, and an
    emergency stop is not a special case — it is simply a label that every
    motion skill happens to declare.

    :meth:`unsatisfied` is polled at admission and before each step starts.
    :meth:`watch` pushes the new set whenever it changes, which is what lets
    the runtime interrupt work already in flight. An adapter with no push
    channel may return an immediately exhausted iterator; the runtime then
    degrades to checkpoint-only enforcement.

    Hosted providers belong to an adapter event-loop thread. Producers on other
    threads must marshal pushes to that loop. Checkpoint deadlines fail closed;
    an idle watch can wait without a polling deadline.
    """

    async def unsatisfied(self) -> frozenset[str]: ...

    def watch(self) -> AsyncIterator[frozenset[str]]: ...


class RobotPlatform(Protocol):
    """A vendor-owned assembly injected through existing runtime ports.

    Platforms determine how to connect to their vendor and expose stable skill
    objects. An unimplemented state source or reconciler is explicitly None.
    Applications start the platform before the runtime and close it afterwards;
    the runtime never imports vendor drivers or manages their SDK lifecycle.
    start/close share their actual outcome when callers retry or cancel waiting.
    Hosted ports may include explicit thread bindings. The SDK passes those
    bindings unchanged to the host; bare objects retain dedicated ownership.
    Bindings configure the host, never the kernel's skill/state contracts.
    """

    @property
    def skills(self) -> Mapping[str, Skill | ThreadBinding[Skill]]: ...

    @property
    def robot_state(self) -> RobotStateProvider | ThreadBinding[RobotStateProvider] | None: ...

    @property
    def reconciler(self) -> Reconciler | ThreadBinding[Reconciler] | None: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...
