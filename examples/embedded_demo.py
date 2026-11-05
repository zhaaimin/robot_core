"""Run a two-step task through the in-process SDK and simulator."""

import asyncio
import json

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import SkillCall, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def main() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator(delay_s=0.01).skills)
    await runtime.start()
    try:
        task = await runtime.submit_task(
            TaskRequest(
                task_type="embedded-demo",
                steps=[
                    SkillCall(
                        skill="navigation.navigate_to",
                        params={"target": "meeting_room"},
                    ),
                    SkillCall(
                        skill="audio.speak",
                        params={"message": "The meeting is ready."},
                    ),
                ],
                idempotency_key="embedded-demo-1",
            )
        )
        events = []
        async for event in runtime.subscribe_task_events(task.id):
            events.append(event.sequence)
        final = await runtime.get_task_status(task.id)
        if final.state != TaskState.SUCCEEDED:
            raise RuntimeError(f"Demo task failed: {final.error}")
        print(
            json.dumps(
                {"state": final.state.value, "events": events, "result": final.result},
                ensure_ascii=False,
            )
        )
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
