import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

from robot_agent_runtime.errors import TaskCancelled


class ResourceManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def acquire(
        self,
        resources: Iterable[str],
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[None]:
        acquired: list[asyncio.Lock] = []
        try:
            for name in sorted(set(resources)):
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
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
