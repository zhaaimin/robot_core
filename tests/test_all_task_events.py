import asyncio
from contextlib import aclosing

import pytest

from robot_agent_runtime.errors import EventStreamOverflow
from robot_agent_runtime.kernel.events import EventBus
from robot_agent_runtime.models import Event
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository


async def checkpoint() -> None:
    ready = asyncio.Event()
    asyncio.get_running_loop().call_soon(ready.set)
    await ready.wait()


def event(task_id: str, sequence: int = 1, event_type: str = "TASK_CREATED") -> Event:
    return Event(event_id=f"{task_id}-{sequence}", task_id=task_id, sequence=sequence, event_type=event_type)


async def test_all_task_events_keep_independent_task_sequences_and_snapshots() -> None:
    bus = EventBus(InMemoryTaskRepository())
    async with aclosing(bus.subscribe_all()) as stream:
        pending = asyncio.create_task(anext(stream))
        await checkpoint()
        first = event("first")
        bus.publish(first)
        assert (await pending).task_id == "first"
        bus.publish(event("second"))
        assert (await anext(stream)).sequence == 1
    bus.close()


async def test_all_task_events_filter_and_report_overflow_without_global_sequence() -> None:
    bus = EventBus(InMemoryTaskRepository(), queue_size=1)
    async with aclosing(bus.subscribe_all({"TASK_FAILED"})) as stream:
        pending = asyncio.create_task(anext(stream))
        await checkpoint()
        bus.publish(event("ignored"))
        bus.publish(event("first", event_type="TASK_FAILED"))
        assert (await pending).task_id == "first"
        bus.publish(event("second", event_type="TASK_FAILED"))
        bus.publish(event("third", event_type="TASK_FAILED"))
        with pytest.raises(EventStreamOverflow) as caught:
            await anext(stream)
        assert "after_sequence" not in caught.value.info.details
    bus.close()


async def test_core_does_not_construct_network_notification_modules() -> None:
    from robot_agent_runtime.kernel.core import RuntimeCore

    core = RuntimeCore(InMemoryTaskRepository(), {})
    assert not hasattr(core, "_notifier")
