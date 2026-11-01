"""Drain a simulated battery out from under a navigation task.

Shows all three moments the runtime judges robot conditions:

1. a navigation is admitted while the battery is full;
2. the battery falls below the threshold mid-flight, and the push channel
   interrupts the task that declared ``battery.sufficient``;
3. a fresh navigation is refused at admission, while an announcement — which
   declares nothing — still runs on the same flat battery.
"""

import asyncio
import json

from robot_agent_runtime.adapters.simulator import SimulatedRobot, Simulator
from robot_agent_runtime.models import SkillCall, TaskRequest
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


def navigate(target: str, key: str) -> TaskRequest:
    return TaskRequest(
        task_type="precondition-demo",
        steps=[SkillCall(skill="navigation.navigate_to", params={"target": target})],
        idempotency_key=key,
    )


async def run_to_completion(runtime: RobotRuntime, task_id: str) -> dict[str, object]:
    events = [event.event_type async for event in runtime.subscribe_task_events(task_id)]
    final = await runtime.get_task_status(task_id)
    return {
        "state": final.state.value,
        "events": events,
        "error": None if final.error is None else final.error.code,
    }


async def main() -> None:
    robot = SimulatedRobot(voltage=24.0, low_voltage=22.5, recovery_voltage=23.5)
    runtime = RobotRuntime(
        InMemoryTaskRepository(),
        Simulator(delay_s=1.0).skills,
        robot_state=robot,
    )
    await runtime.start()
    drain = asyncio.create_task(robot.drain(volts_per_second=10.0, floor=22.0))
    try:
        # 1 + 2: admitted on a full battery, interrupted once it drops.
        task = await runtime.submit_task(navigate("meeting_room", "demo-1"))
        interrupted = await run_to_completion(runtime, task.id)
        print(json.dumps({"navigation": interrupted, "voltage": robot.voltage}))

        # 3a: the same request is now refused before any step starts. A
        # rejected submission comes back as a FAILED task rather than an
        # exception, so the caller still gets a task id to report against.
        refused = await runtime.submit_task(navigate("meeting_room", "demo-2"))
        if refused.error is None:
            raise RuntimeError("expected the flat battery to refuse a navigation")
        print(
            json.dumps(
                {
                    "second_navigation": refused.state.value,
                    "code": refused.error.code,
                    "details": refused.error.details,
                }
            )
        )

        # 3b: speaking declares no preconditions, so it is unaffected.
        speak = await runtime.submit_task(
            TaskRequest(
                task_type="precondition-demo",
                steps=[SkillCall(skill="audio.speak", params={"message": "Charging."})],
                idempotency_key="demo-3",
            )
        )
        print(json.dumps({"announcement": await run_to_completion(runtime, speak.id)}))
    finally:
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
