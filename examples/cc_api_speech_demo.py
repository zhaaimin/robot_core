"""Speak through CC API on a Linux ARM64 host with the vendor SDK installed."""

import argparse
import asyncio
import json
import os
from collections.abc import Sequence

from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill
from robot_agent_runtime.errors import RobotConnectionError, SkillExecutionError
from robot_agent_runtime.models import SkillCall, TaskRequest, TaskState
from robot_agent_runtime.ports import CallbackRobotApi
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def speak(api: CallbackRobotApi, message: str) -> None:
    skill = CcApiSpeechSkill(api)
    runtime = RobotRuntime(
        InMemoryTaskRepository(), {skill.definition.name: skill},
        allowed_permissions={"audio"},
    )
    await skill.start()
    try:
        await runtime.start()
        try:
            task = await runtime.submit_task(TaskRequest(
                task_type="announcement",
                steps=[SkillCall(skill="audio.speak", params={"message": message})],
            ))
            async for event in runtime.subscribe_task_events(task.id):
                print(event.model_dump_json(), flush=True)
            final = await runtime.get_task_status(task.id)
            if final.state != TaskState.SUCCEEDED:
                raise SkillExecutionError(
                    "Speech task did not succeed",
                    details={"task_id": task.id, "error": final.error.to_dict() if final.error else None},
                )
            print(json.dumps(final.result, ensure_ascii=False))
        finally:
            await runtime.close()
    finally:
        await skill.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-id", required=True, help="registered robot API client ID")
    parser.add_argument("--address", help="robot API host:port; omit when running on the robot")
    parser.add_argument("--message", required=True, help="text to speak")
    args = parser.parse_args(argv)
    try:
        import ubt_robot
    except ImportError as error:
        raise RobotConnectionError(
            "Install the supplied ubt_robot Linux ARM64 wheel in the deployment environment"
        ) from error

    config = ubt_robot.Config()
    config.api_id = args.api_id
    config.is_reconnect = False
    if args.address:
        config.address = args.address
        config.token = os.environ.get("CC_API_TOKEN", "")
    api = ubt_robot.Api()
    api.initialize(config)
    try:
        api.start()
        if not api.wait_until_connected(5000):
            raise RobotConnectionError("CC API did not connect within 5 seconds")
        asyncio.run(speak(api, args.message))
    finally:
        api.stop()


if __name__ == "__main__":
    main()
