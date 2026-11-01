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
    @property
    def definition(self) -> SkillDefinition: ...

    async def execute(
        self, context: SkillContext, params: dict[str, Any]
    ) -> SkillResult: ...


class TaskRepository(Protocol):
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


class Reconciler(Protocol):
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
