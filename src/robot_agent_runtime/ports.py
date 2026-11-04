from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from robot_agent_runtime.models import (
    Event,
    SkillContext,
    SkillDefinition,
    SkillResult,
    Task,
    TaskState,
)


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

    A deadline can leave a write outcome unknown. The host poisons a timed-out
    repository rather than retrying or starting another write alongside it.
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


class CallbackApiResponse(Protocol):
    @property
    def is_success(self) -> bool: ...

    @property
    def result(self) -> Any: ...

    @property
    def error_code(self) -> int: ...

    @property
    def error_message(self) -> str: ...


class CallbackRobotApi(Protocol):
    """An already connected, application-owned callback transport.

    Methods enqueue work without waiting for network responses. Callbacks may
    run on SDK-owned threads; adapters marshal them onto their asyncio loop.
    The application owns connection startup and shutdown, and gives the speech
    adapter exclusive ownership of its finished and disconnect handlers.
    No vendor package is required to import this contract.
    """

    def request(
        self,
        method: str,
        params: dict[str, Any],
        callback: Callable[[CallbackApiResponse], None],
    ) -> None: ...

    def notify(self, method: str) -> None: ...

    def add_service(
        self, name: str, callback: Callable[[Any, Any], None]
    ) -> None: ...

    def remove_service(self, name: str) -> None: ...

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None: ...
