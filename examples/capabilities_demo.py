"""Use the shared robot tools without a language model or an agent framework."""

import asyncio
import json

from robot_agent_capabilities import RobotCapabilities
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def main() -> None:
    simulator = Simulator()
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    try:
        robot = await RobotCapabilities.create(runtime, allowed_skills={'audio.speak'})
        observed = await robot.invoke('robot_speak', {'message': '你好，我是机器人助手'},
                                      operation_id='capabilities-demo-greeting', wait=True)
        print(json.dumps(observed, ensure_ascii=False))
    finally:
        await runtime.close()


if __name__ == '__main__':
    asyncio.run(main())
