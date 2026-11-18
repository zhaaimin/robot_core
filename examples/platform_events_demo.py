"""Observe task completion and platform facts through one capability stream."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing

from robot_agent_capabilities import RobotCapabilities
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import Event, PlatformEvent
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


class DemoEventSource:
    """Keep the watcher open like a device; its asyncio state stays on its owner loop."""

    async def watch(self) -> AsyncIterator[PlatformEvent]:
        yield PlatformEvent(
            event_id="demo-initial",
            source="demo",
            event_type="demo.snapshot",
            data={"value": 0},
        )
        yield PlatformEvent(
            event_id="demo-change",
            source="demo",
            event_type="demo.changed",
            data={"value": 1},
        )
        await asyncio.Event().wait()


def show(stage: str, event: Event | PlatformEvent) -> None:
    print(json.dumps({"stage": stage, "event": event.model_dump(mode="json")}, ensure_ascii=False))


async def main() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills, event_source=DemoEventSource())
    await runtime.start()
    try:
        robot = await RobotCapabilities.create(runtime)
        async with aclosing(robot.subscribe_events()) as events:
            async with asyncio.timeout(5):
                initial = await anext(events)
                show("processed_initial", initial)
                processed_cursor = initial.cursor
                changed = await anext(events)
                show("received_before_disconnect", changed)

        async with aclosing(robot.subscribe_events(after_cursor=processed_cursor)) as events:
            async with asyncio.timeout(5):
                resumed = await anext(events)
                assert resumed.event_id == changed.event_id
                assert resumed.cursor > processed_cursor
                show("processed_after_resume", resumed)

        observed = await robot.invoke("robot_get_state", {}, operation_id="events-demo-state", wait=True)
        assert observed["state"] == "succeeded" and isinstance(task_id := observed["task_id"], str)
        terminal: Event | None = None
        seen_platform = False
        async with aclosing(robot.subscribe_events(
            sources={"task", "platform"}, task_ids={task_id}, task_terminal_only=True,
        )) as events:
            async with asyncio.timeout(5):
                async for item in events:
                    show("merged_task_and_platform", item)
                    if isinstance(item, Event):
                        terminal = item
                        assert item.data["state"] == "succeeded"
                    else:
                        seen_platform = True
                    if terminal is not None and seen_platform:
                        break
        assert terminal is not None
        async with asyncio.timeout(5):
            already_processed = [item async for item in robot.subscribe_events(
                sources={"task"}, task_ids={task_id}, after_sequences={task_id: terminal.sequence},
            )]
        assert already_processed == []
        print(json.dumps({"stage": "processed_terminal_not_repeated", "task_id": task_id, "sequence": terminal.sequence}))
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
