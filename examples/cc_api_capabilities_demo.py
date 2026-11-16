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

from robot_agent_capabilities import RobotCapabilities
from robot_agent_capabilities.observations import Observation
from robot_agent_runtime.adapters.cc_api import create_cc_api_platform
from robot_agent_runtime.adapters.cc_api.contracts import CcApiClient
from robot_agent_runtime.errors import RobotConnectionError
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


async def speak(api: CcApiClient, message: str, operation_id: str) -> Observation:
    """Invoke robot_speak through the capability layer; caller keeps the connection.

    wait_timeout_s must cover the real speech duration, not just task
    submission: ``invoke(wait=True)`` waits for a terminal task snapshot and
    reports ``requires_attention`` instead of failing when the wait is cut
    short. The default of 5 seconds is too tight for live TTS.
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
        return await robot.invoke(
            "robot_speak", {"message": message},
            operation_id=operation_id, wait=True,
        )
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
