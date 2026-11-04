import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

from robot_agent_runtime.errors import ResourceQuarantined, TaskCancelled


class ResourceLocks:
    """Mutual exclusion over named hardware, and nothing else.

    A robot has one chassis and one speaker, so two tasks must not drive the
    base at once. A skill declares the names it needs in
    :attr:`SkillDefinition.resources` and the runtime takes the locks; the
    names are opaque, and nothing here allocates, pools or meters anything.

    :meth:`acquire` takes a step's whole set in sorted order, so callers can
    never disagree about ordering, and races the acquisition against
    ``cancel_event``: a plain ``await lock.acquire()`` would leave a task
    queued behind a long navigation unable to answer a cancel request. When
    both finish together the lock is held and still has to be released, which
    is what the bookkeeping in the ``finally`` block is for.

    Unconfirmed termination quarantines names before releasing locks. Waiters
    check again after acquiring, so ending a software waiter cannot release
    uncertain physical ownership. Quarantine never resets automatically.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._quarantined: set[str] = set()

    def quarantine(self, resources: Iterable[str]) -> None:
        self._quarantined.update(resources)

    def _require_available(self, resources: Iterable[str]) -> None:
        blocked = self._quarantined.intersection(resources)
        if blocked:
            raise ResourceQuarantined(
                "Resource ownership requires reconciliation before reuse",
                details={"resources": sorted(blocked)},
            )

    @asynccontextmanager
    async def acquire(
        self,
        resources: Iterable[str],
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[None]:
        names = sorted(set(resources))
        acquired: list[asyncio.Lock] = []
        try:
            self._require_available(names)
            for name in names:
                lock = self._locks.setdefault(name, asyncio.Lock())
                if cancel_event is None:
                    await lock.acquire()
                    acquired.append(lock)
                    continue

                acquire_task = asyncio.create_task(lock.acquire())
                cancel_waiter = asyncio.create_task(cancel_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {acquire_task, cancel_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_waiter in done and cancel_event.is_set():
                        raise TaskCancelled("Task cancelled while acquiring resources")
                finally:
                    if acquire_task.done():
                        if not acquire_task.cancelled() and acquire_task.result():
                            acquired.append(lock)
                    else:
                        acquire_task.cancel()
                    if not cancel_waiter.done():
                        cancel_waiter.cancel()
                    await asyncio.gather(
                        acquire_task, cancel_waiter, return_exceptions=True
                    )
            self._require_available(names)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
