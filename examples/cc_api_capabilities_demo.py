"""Use the shared capability layer against a real CC robot.

Same stable operation name (``robot_speak``) and observation shape as the
simulator demo (``capabilities_demo.py``); only the platform changes. Run on
a Linux ARM64 host with the supplied vendor SDK installed. The application
owns the native connection; the runtime owns task execution and the
capability layer owns tool naming and idempotency.
"""

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from contextlib import aclosing

from robot_agent_capabilities import RobotCapabilities
from robot_agent_capabilities.observations import Observation
from robot_agent_runtime.adapters.cc_api import create_cc_api_platform
from robot_agent_runtime.adapters.cc_api.contracts import CcApiClient
from robot_agent_runtime.errors import RobotConnectionError, TaskEventStreamEnded
from robot_agent_runtime.models import TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


def _print_event(event) -> None:
    """One line per runtime event, as it is delivered."""
    data = {key: value for key, value in event.data.items() if key != "state"}
    state = event.data.get("state", "")
    suffix = f" state={state}" if state else ""
    print(f"[EVENT] seq={event.sequence:>3} {event.event_type}{suffix}"
          + (f" {json.dumps(data, ensure_ascii=False)}" if data else ""))


async def speak(api: CcApiClient, message: str, operation_id: str) -> Observation:
    """Invoke robot_speak through the capability layer and stream every event.

    Submission goes through ``invoke(wait=False)``; the task's event stream is
    then consumed directly so each runtime event is printed as it is delivered.
    The final observation still comes from the capability layer
    (``robot_task_status``), so the ``[RESULT]`` shape is unchanged. Streaming
    has no local deadline: timing out or cancelling the wait must not stop the
    robot, matching the listener contract.
    """
    platform = create_cc_api_platform(api, skill_thread_group="audio")
    runtime = RobotRuntime.from_platform(
        InMemoryTaskRepository(), platform, allowed_permissions={"audio"},
    )
    try:
        await platform.start()
        await runtime.start()
        robot = await RobotCapabilities.create(
            runtime, allowed_skills={"audio.speak"}, wait_timeout_s=30.0,
        )
        submitted = await robot.invoke(
            "robot_speak", {"message": message}, operation_id=operation_id,
        )
        if submitted["error"]:
            return submitted
        task_id = submitted["task_id"]
        if submitted["state"] in {state.value for state in TaskState if state.is_terminal}:
            return submitted

        try:
            async with aclosing(runtime.subscribe_task_events(task_id)) as events:
                async for event in events:
                    _print_event(event)
                    try:
                        terminal = TaskState(event.data.get("state")).is_terminal
                    except (TypeError, ValueError):
                        terminal = False
                    if terminal:
                        break
        except TaskEventStreamEnded:
            print("[EVENT] stream ended before a terminal event")
        return await robot.invoke("robot_task_status", {"task_id": task_id})
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
    parser.add_argument(
        "--operation-id", default="capabilities-demo-greeting",
        help="stable idempotency key; identical keys reuse the same task",
    )
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
        observed = asyncio.run(speak(api, args.message, args.operation_id))
        print(f"[RESULT] {json.dumps(observed, ensure_ascii=False)}")
        if observed.get("error"):
            raise SystemExit(1)
    finally:
        api.stop()


if __name__ == "__main__":
    main()
