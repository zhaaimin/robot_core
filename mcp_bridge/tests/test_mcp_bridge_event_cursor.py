import asyncio
from contextlib import aclosing

from robot_agent_runtime.models import Event, SkillCall, TaskRequest
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk.remote import RemoteRuntime
from test_mcp_bridge_stdio import raw_session, request, runtime_server


class SlowPrefixRepository(InMemoryTaskRepository):
    """Make revisiting an old prefix exceed any event batch waiting budget.

    The repository still serves cursor-directed reads immediately. An Event
    rather than a sleep makes the regression independent of machine speed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.block_before = 0
        self.read_cursors: list[int] = []

    async def list_events_page(
        self, task_id: str, after_sequence: int = 0, limit: int = 128,
    ) -> list[Event]:
        self.read_cursors.append(after_sequence)
        if after_sequence < self.block_before:
            await asyncio.Event().wait()
        return await super().list_events_page(task_id, after_sequence, limit)


async def test_terminal_history_paging_progresses_without_revisiting_slow_prefix() -> None:
    repository = SlowPrefixRepository()
    async with runtime_server(repository) as (_, socket, _):
        remote = RemoteRuntime(socket)
        try:
            task = await remote.submit_and_wait(TaskRequest(
                task_type="long-history", idempotency_key="long-history-once",
                steps=[SkillCall(skill="robot.get_state") for _ in range(40)],
            ), timeout_s=3)
            async with aclosing(remote.subscribe_task_events(task.id)) as stream:
                history = [event async for event in stream]
            starting = history[len(history) // 2].sequence
            expected = [event.model_dump(mode="json") for event in history if event.sequence > starting]
            repository.read_cursors.clear()
            repository.block_before = starting - 1
            observed = []
            cursor = starting
            async with raw_session(socket) as session:
                async with asyncio.timeout(10):
                    while True:
                        result = await session.call_tool("read_task_events", {
                            "task_id": task.id, "after_sequence": cursor, "limit": 3, "wait_s": 0.2,
                        })
                        assert not result.isError
                        value = result.structuredContent
                        assert min(repository.read_cursors, default=starting) >= starting - 1
                        if value["events"]:
                            assert value["next_sequence"] > cursor
                        else:
                            assert value["next_sequence"] == cursor
                            assert value["done"] is False
                        observed.extend(value["events"])
                        cursor = value["next_sequence"]
                        if value["done"]:
                            break
                    while True:
                        terminal = await session.call_tool("read_task_events", {
                            "task_id": task.id, "after_sequence": cursor, "wait_s": 0.5,
                        })
                        assert not terminal.isError
                        assert terminal.structuredContent["events"] == []
                        assert terminal.structuredContent["next_sequence"] == cursor
                        if terminal.structuredContent["done"]:
                            break
            assert observed == expected
            assert repository.read_cursors
            assert min(repository.read_cursors) >= starting - 1
        finally:
            await remote.close()


async def test_future_cursor_without_event_evidence_remains_unconfirmed() -> None:
    async with runtime_server() as (_, socket, _):
        remote = RemoteRuntime(socket)
        try:
            task = await remote.submit_and_wait(request(), timeout_s=1)
            async with aclosing(remote.subscribe_task_events(task.id)) as stream:
                history = [event async for event in stream]
            future = history[-1].sequence + 10
            async with raw_session(socket) as session:
                for _ in range(2):
                    result = await session.call_tool("read_task_events", {
                        "task_id": task.id, "after_sequence": future, "wait_s": 0.01,
                    })
                    assert not result.isError
                    assert result.structuredContent == {"events": [], "next_sequence": future, "done": False}
        finally:
            await remote.close()
