"""Register separate capability callbacks without hardware or a model."""

import asyncio
import threading
from collections.abc import AsyncIterator

from robot_agent_capabilities import RobotCapabilities
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import Event, PlatformEvent, Task, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


class DemoEventSource:
    """Keep the source open so completion of a task does not end device observation."""

    async def watch(self) -> AsyncIterator[PlatformEvent]:
        yield PlatformEvent(
            source="demo", event_type="demo.fault.reported",
            data={"payload": {"code": "DEMO_SENSOR_FAULT"}, "simulated": True},
        )
        await asyncio.Event().wait()


async def main() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills, event_source=DemoEventSource())
    await runtime.start()
    try:
        robot = await RobotCapabilities.create(runtime)
        caller_loop = asyncio.get_running_loop()
        received_platform = asyncio.Event()
        completed: list[Task] = []
        listener_errors: list[Exception] = []

        def handle_task_event(event: Event) -> None:
            print("[TASK EVENT]", event.task_id, event.sequence, event.event_type,
                  "callback_thread=", threading.get_ident())

        async def handle_task_done(task: Task) -> None:
            completed.append(task)
            print("[TASK DONE]", task.id, task.state.value, task.result, task.error)

        async def handle_task_error(task_id: str | None, error: Exception) -> None:
            listener_errors.append(error)
            print("[TASK LISTENER ERROR]", task_id, error)

        async def handle_platform_event(event: PlatformEvent) -> None:
            print("[PLATFORM EVENT]", event.cursor, event.event_type, event.data,
                  "callback_thread=", threading.get_ident())
            caller_loop.call_soon_threadsafe(received_platform.set)

        async def handle_platform_error(error: Exception) -> None:
            listener_errors.append(error)
            print("[PLATFORM LISTENER ERROR]", error)

        try:
            task_listener = robot.add_task_listener(
                on_event=handle_task_event, on_done=handle_task_done, on_error=handle_task_error,
                execution="group", group="reporting",
            )
            platform_listener = robot.add_platform_listener(
                on_event=handle_platform_event, on_error=handle_platform_error,
                execution="group", group="reporting",
            )
            observed = await robot.invoke("robot_get_state", {}, operation_id="listener-demo-state")
            async with asyncio.timeout(5):
                await task_listener.wait()
                await received_platform.wait()
            assert len(completed) == 1 and completed[0].id == observed["task_id"]
            assert completed[0].state == TaskState.SUCCEEDED and not listener_errors
        finally:
            await robot.close_listeners()
        assert task_listener.closed and platform_listener.closed
        print("[CLOSED] callback resources released; runtime still belongs to the application")
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
