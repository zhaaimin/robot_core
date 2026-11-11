"""Bounded runtime coordination for the hotel's physical navigation tasks.

The workflow decides what a navigation result means for a guest; this module
decides how the runtime is asked, how long an answer may take, and what counts as
no answer at all. Every interaction ends in either a confirmed task or a reason,
because a lost submission or an unconfirmed stop has to become a reconciliation
instead of a replayed physical action.
"""

import asyncio
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from hotel_agent.models import HotelService
from hotel_agent.repository import HotelRepository
from robot_agent_core.ports import TaskRuntime
from robot_agent_runtime.models import Task, TaskRequest, TaskState


@dataclass(frozen=True)
class Navigation:
    """One navigation kind and the service field that remembers its runtime task.

    Escort and return are separate tasks over the same robot, so each keeps its
    own id and its own reconciliation; neither may report for the other.
    """

    kind: str
    task_id_field: str

    def task_id(self, service: HotelService) -> str | None:
        value = getattr(service, self.task_id_field)
        return value if isinstance(value, str) else None

    def remember(self, service: HotelService, task_id: str) -> None:
        setattr(service, self.task_id_field, task_id)


ESCORT = Navigation(kind='escort', task_id_field='runtime_task_id')
RETURN = Navigation(kind='return', task_id_field='return_task_id')
NAVIGATIONS: tuple[Navigation, ...] = (ESCORT, RETURN)

_STOP_CONFIRMED = {TaskState.SUCCEEDED, TaskState.CANCELED, TaskState.FAILED}


@dataclass(frozen=True)
class StopResult:
    """Whether a stop was asked for, and what the runtime confirmed about it.

    Uncertainty is reported to the rules on the way out, so a result without a
    task means 'already recorded as unresolved', not 'the robot is idle'. Only a
    task in a settled state counts as confirmation, because the runtime may
    itself hand back a state it cannot resolve.
    """

    requested: bool = True
    task: Task | None = None

    @staticmethod
    def confirmed(task: Task) -> 'StopResult':
        return StopResult(task=task)

    @staticmethod
    def unconfirmed() -> 'StopResult':
        return StopResult()

    @staticmethod
    def not_requested() -> 'StopResult':
        return StopResult(requested=False)


@dataclass(frozen=True)
class NavigationRules:
    """The hotel's answers, supplied by the workflow.

    Keeping them as named functions rather than a subclass lets the coordinator
    own the runtime plumbing without owning phase rules, consent or the lease.
    """

    request: Callable[[HotelService, Navigation], TaskRequest]
    is_active: Callable[[HotelService, Navigation], bool]
    ended: Callable[[HotelService, Navigation, Task], None]
    uncertain: Callable[[HotelService, Navigation, str], None]
    is_closing: Callable[[], bool]


class HotelTaskCoordinator:
    """Submit, observe and stop hotel navigation tasks without guessing results.

    Each runtime call is time-boxed. Timeouts, transport failures and event
    streams that end before a terminal state are reported to the rules as
    uncertainty, so the robot is quarantined rather than sent somewhere twice.
    Background observation is owned here: watchers wake on the runtime's terminal
    event and re-enter the workflow's rules under the workflow's lock, and they
    stop being trusted as soon as the workflow begins closing.
    """

    def __init__(
        self, *, runtime: TaskRuntime, repository: HotelRepository,
        lock: asyncio.Lock, timeout_s: float, rules: NavigationRules,
    ) -> None:
        self._runtime = runtime
        self._repository = repository
        self._lock = lock
        self._timeout_s = timeout_s
        self._rules = rules
        self._monitors: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def submit(self, service: HotelService, navigation: Navigation) -> None:
        """Send the navigation the workflow already committed as an intent.

        The returned task id is persisted before its result is applied, so a crash
        between the two leaves an observable task instead of an unwritten action.
        """
        request = self._rules.request(service, navigation)
        try:
            async with asyncio.timeout(self._timeout_s):
                task = await self._runtime.submit_task(request)
        except asyncio.CancelledError:
            self._rules.uncertain(
                service, navigation,
                'Navigation submission was interrupted before its outcome was saved',
            )
            raise
        except Exception:
            self._rules.uncertain(
                service, navigation, 'Navigation submission outcome is unknown; do not replay it',
            )
            return
        navigation.remember(service, task.id)
        self._repository.save(service)
        self._rules.ended(service, navigation, task)
        if not task.state.is_terminal:
            self.watch(service, navigation)

    async def refresh(self, service: HotelService) -> None:
        """Reconcile every active navigation from the runtime's authoritative task.

        Which tasks count as active is decided from the snapshot this call was
        handed, before any result is applied; a task the runtime will not describe
        is uncertainty, not failure. Once a result is known, the newest saved
        service is the one it gets applied to.
        """
        observed = tuple(
            (navigation, navigation.task_id(service), self._rules.is_active(service, navigation))
            for navigation in NAVIGATIONS
        )
        for navigation, task_id, active in observed:
            if not active or task_id is None:
                continue
            try:
                async with asyncio.timeout(min(self._timeout_s, 2.0)):
                    task = await self._runtime.get_task_status(task_id)
            except Exception:
                self._rules.uncertain(
                    service, navigation, 'Saved runtime task could not be queried; do not replay it',
                )
                continue
            current = self._repository.get(service.session_id)
            if current.revision != service.revision:
                service.__dict__.update(current.__dict__)
            self._rules.ended(service, navigation, task)
            if not task.state.is_terminal:
                self.watch(service, navigation)

    async def stop(self, service: HotelService, navigation: Navigation) -> StopResult:
        """Ask the runtime to stop an active navigation, then report what it confirmed.

        A stop the runtime could not confirm is handed to the rules as uncertainty
        before returning, so the caller never has to guess whether silence meant
        'stopped' or 'unknown'.
        """
        task_id = navigation.task_id(service)
        if task_id is None or not self._rules.is_active(service, navigation):
            return StopResult.not_requested()
        try:
            async with asyncio.timeout(min(self._timeout_s, 2.0)):
                task = await self._runtime.cancel_task(task_id)
        except Exception:
            self._rules.uncertain(service, navigation, 'Runtime could not confirm navigation stopped')
            return StopResult.unconfirmed()
        if task.state not in _STOP_CONFIRMED:
            self._rules.uncertain(service, navigation, 'Runtime has not confirmed navigation stopped')
            return StopResult.unconfirmed()
        return StopResult.confirmed(task)

    def watch(self, service: HotelService, navigation: Navigation) -> None:
        """Observe one navigation once, replacing only watchers already finished."""
        key = (service.service_id, navigation.kind)
        existing = self._monitors.get(key)
        if existing is None or existing.done():
            self._monitors[key] = asyncio.create_task(
                self._follow(service.session_id, navigation),
            )

    def pending_monitors(self, service_id: str) -> list[asyncio.Task[None]]:
        return [
            monitor for (watched, _), monitor in self._monitors.items()
            if watched == service_id and not monitor.done()
        ]

    async def cancel_monitors(self, *, timeout_s: float = 1.0) -> None:
        """Refuse further observations, then give reconciles already in flight a bounded end.

        The timeout does not prove those watchers finished; it only stops closing
        from depending on code the runtime controls.
        """
        async with self._lock:
            monitors = list(self._monitors.values())
            for monitor in monitors:
                monitor.cancel()
        if monitors:
            await asyncio.wait(monitors, timeout=timeout_s)

    async def _follow(self, session_id: str, navigation: Navigation) -> None:
        try:
            async with self._lock:
                if self._rules.is_closing():
                    return
                task_id = navigation.task_id(self._repository.get(session_id))
            if task_id is None:
                return
            async with aclosing(self._runtime.subscribe_task_events(task_id)) as events:
                async for event in events:
                    if _confirms_terminal(event):
                        async with self._lock:
                            if not self._rules.is_closing():
                                await self.refresh(self._repository.get(session_id))
                        return
            await self._lost(
                session_id, navigation,
                'Runtime event stream ended without confirming navigation completion',
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._lost(
                session_id, navigation, 'Runtime observation failed; navigation outcome is unknown',
            )

    async def _lost(self, session_id: str, navigation: Navigation, reason: str) -> None:
        async with self._lock:
            if self._rules.is_closing():
                return
            self._rules.uncertain(self._repository.get(session_id), navigation, reason)


def _confirms_terminal(event: Any) -> bool:
    """Read the state an event carries, treating an unreadable one as non-terminal."""
    try:
        return TaskState(event.data.get('state')).is_terminal
    except (TypeError, ValueError):
        return False
