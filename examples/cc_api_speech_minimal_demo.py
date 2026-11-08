"""Minimal CC API speech: submit one task and await its final snapshot.

Run on a Linux ARM64 host with the supplied vendor SDK installed. The application
owns the native connection; the runtime owns task execution and result waiting.
"""

import argparse
import asyncio
import json
import os
from collections.abc import Sequence

from robot_agent_runtime.adapters.cc_api import create_cc_api_platform
from robot_agent_runtime.adapters.cc_api.contracts import CcApiClient
from robot_agent_runtime.errors import RobotConnectionError, SkillExecutionError
from robot_agent_runtime.models import SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def speak(api: CcApiClient, message: str) -> Task:
    """Return any terminal task while the caller retains its native connection.

    Business failures remain in state/error. Startup and cleanup failures raise;
    the runtime is closed before the platform even when startup does not finish.
    """
    platform = create_cc_api_platform(api, skill_thread_group="audio")
    runtime = RobotRuntime.from_platform(
        InMemoryTaskRepository(), platform, allowed_permissions={"audio"},
    )
    try:
        await platform.start()
        await runtime.start()
        return await runtime.submit_and_wait(TaskRequest(
            task_type="announcement",
            steps=[SkillCall(skill="audio.speak", params={"message": message})],
        ))
    finally:
        try:
            await runtime.close()
        finally:
            await platform.close()


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
        print(f"[RESULT] {json.dumps(final.result, ensure_ascii=False)}")
    finally:
        api.stop()


if __name__ == "__main__":
    main()
