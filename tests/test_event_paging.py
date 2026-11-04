import asyncio

from robot_agent_runtime.events import EventBus
from robot_agent_runtime.models import Event
from robot_agent_runtime.repository import InMemoryTaskRepository


async def test_event_replay_reads_bounded_pages_and_preserves_live_order() -> None:
    class PagedOnly(InMemoryTaskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.pages: list[tuple[int, int]] = []

        async def list_events(self, task_id: str, after_sequence: int = 0) -> list[Event]:
            raise AssertionError("Full history must not be loaded")

        async def list_events_page(self, task_id: str, after_sequence: int = 0,
                                   limit: int = 128) -> list[Event]:
            self.pages.append((after_sequence, limit))
            return await super().list_events_page(task_id, after_sequence, limit)

    repo = PagedOnly()
    bus = EventBus(repo, page_size=2)
    for sequence in range(1, 6):
        await repo.append_event(Event(event_id=str(sequence), task_id="t", sequence=sequence,
                                      event_type="progress"))
    stream = bus.subscribe("t", after_sequence=1)
    try:
        received = [await anext(stream)]
        live = Event(event_id="6", task_id="t", sequence=6, event_type="TASK_SUCCEEDED",
                     data={"state": "succeeded"})
        await repo.append_event(live)
        bus.publish(live)
        async for event in stream:
            received.append(event)
        assert [event.sequence for event in received] == [2, 3, 4, 5, 6]
        assert repo.pages == [(1, 2), (3, 2), (5, 2)]
    finally:
        bus.close()
        await stream.aclose()


async def test_replay_yields_control_between_full_pages() -> None:
    repo = InMemoryTaskRepository()
    bus = EventBus(repo, page_size=1)
    for sequence in range(1, 4):
        await repo.append_event(Event(event_id=str(sequence), task_id="t", sequence=sequence,
                                      event_type="progress"))
    checkpoints: list[bool] = []
    stream = bus.subscribe("t")
    try:
        await anext(stream)
        asyncio.get_running_loop().call_soon(checkpoints.append, True)
        await anext(stream)
        assert checkpoints
    finally:
        bus.close()
        await stream.aclose()
