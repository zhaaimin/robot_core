"""Observe CC API speech with one listener registered before task submission.

Callbacks use the default caller execution mode. The application can continue
after submit_task returns, then wait for callback delivery before closing.
Thread selection and blocking experiments live in cc_api_speech_demo.py.
"""

import argparse
import asyncio
import json
import os
from collections.abc import Sequence

from robot_agent_runtime.adapters.cc_api import create_cc_api_platform
from robot_agent_runtime.adapters.cc_api.contracts import CcApiClient
from robot_agent_runtime.errors import RobotConnectionError, SkillExecutionError
from robot_agent_runtime.models import Event, SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def speak(api: CcApiClient, message: str) -> Task:
    """Deliver callbacks before returning a terminal task or raising a listener error.

    Business failure reaches on_done and remains in Task.error. Observation
    failure reaches on_error and is raised after waiting; neither path claims
    success merely because the listener finished. Callbacks stay on the caller
    loop, so their error collection needs no cross-thread result bridge.
    """
    platform = create_cc_api_platform(api, skill_thread_group="audio")
    runtime = RobotRuntime.from_platform(
        InMemoryTaskRepository(), platform, allowed_permissions={"audio"},
    )
    errors: list[Exception] = []

    async def on_event(event: Event) -> None:
        print(f"[EVENT] {event.model_dump_json()}", flush=True)

    async def on_done(task: Task) -> None:
        print(f"[DONE] {task.model_dump_json()}", flush=True)

    async def on_error(task_id: str, error: Exception) -> None:
        errors.append(error)
        print(f"[LISTENER ERROR] {task_id}: {error}", flush=True)

    try:
        await platform.start()
        await runtime.start()
        listener = runtime.add_task_listener(
            on_event=on_event, on_done=on_done, on_error=on_error,
        )
        submitted = await runtime.submit_task(TaskRequest(
            task_type="announcement",
            steps=[SkillCall(skill="audio.speak", params={"message": message})],
        ))
        await listener.wait()
        if errors:
            raise errors[0]
        return await runtime.get_task_status(submitted.id)
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
