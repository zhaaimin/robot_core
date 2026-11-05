"""Speak through CC API on a Linux ARM64 host with the vendor SDK installed."""

import argparse
import asyncio
import json
import os
from collections.abc import Sequence

from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill
from robot_agent_runtime.errors import RobotConnectionError, SkillExecutionError
from robot_agent_runtime.models import Event, SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.ports import CallbackRobotApi
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def speak(api: CallbackRobotApi, message: str) -> Task:
    """Return the final snapshot after releasing runtime and adapter resources.

    Task failures are returned in state/error so callers can inspect vendor
    errors as well as successful output. Startup and cleanup failures still
    propagate as exceptions. The caller continues to own the SDK connection.
    """
    skill = CcApiSpeechSkill(api)
    runtime = RobotRuntime(
        InMemoryTaskRepository(), {skill.definition.name: skill},
        allowed_permissions={"audio"},
    )
    completed: asyncio.Future[Task] = asyncio.get_running_loop().create_future()

    def on_event(event: Event) -> None:
        print(event.model_dump_json(), flush=True)

    async def on_done(task: Task) -> None:
        if not completed.done():
            completed.set_result(task)

    async def on_error(task_id: str, error: Exception) -> None:
        if not completed.done():
            completed.set_exception(error)

    runtime.add_task_listener(on_event=on_event, on_done=on_done, on_error=on_error)
    await skill.start()
    try:
        await runtime.start()
        try:
            await runtime.submit_task(TaskRequest(
                task_type="announcement",
                steps=[SkillCall(skill="audio.speak", params={"message": message})],
            ))
            return await completed
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
        final = asyncio.run(speak(api, args.message))
        if final.state != TaskState.SUCCEEDED:
            raise SkillExecutionError(
                "Speech task did not succeed",
                details={"task_id": final.id, "error": final.error.to_dict() if final.error else None},
            )
        print(json.dumps(final.result, ensure_ascii=False))
    finally:
        api.stop()


if __name__ == "__main__":
    main()
