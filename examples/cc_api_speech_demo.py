"""Speak through CC API on a Linux ARM64 host with the vendor SDK installed.

Two task listeners are registered and each one's execution mode is selectable.
With ``--listener-block`` the first listener calls ``time.sleep`` inside its
async ``on_event``. Under ``caller`` (default) both listeners' async callbacks
run on the submitting loop, so listener 2 stops receiving events until that
call returns. Moving the blocking listener to ``dedicated`` confines the sleep
to its own callback thread and listener 2 keeps receiving events. Moving only
listener 2 to ``dedicated`` does not help: the events are still delivered
through the submitting loop, which listener 1 is holding.
"""

import argparse
import asyncio
import json
import os
import threading
import time
from collections.abc import Sequence
from typing import Literal

from robot_agent_runtime.adapters.cc_api import create_cc_api_platform
from robot_agent_runtime.adapters.cc_api.contracts import CcApiClient
from robot_agent_runtime.errors import RobotConnectionError, SkillExecutionError
from robot_agent_runtime.models import Event, SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime

ListenerExecution = Literal["caller", "dedicated"]


def report_listener_blocking(
    arrivals: list[float],
    block_s: float,
    listener1_execution: ListenerExecution,
    listener2_execution: ListenerExecution,
) -> None:
    gaps = [later - earlier for earlier, later in zip(arrivals, arrivals[1:])]
    longest = max(gaps, default=0.0)
    blocked = longest >= block_s * 0.75
    print(
        f"[REPORT] listener1={listener1_execution} listener2={listener2_execution} "
        f"block={block_s:g}s listener2_events={len(arrivals) - 1} "
        f"longest_gap={longest:.3f}s "
        f"listener2={'blocked with listener1' if blocked else 'kept receiving events'}",
        flush=True,
    )


async def speak(
    api: CcApiClient,
    message: str,
    *,
    block_s: float = 0.0,
    listener1_execution: ListenerExecution = "caller",
    listener2_execution: ListenerExecution = "caller",
) -> Task:
    """Return the final snapshot after releasing runtime and adapter resources.

    Task failures are returned in state/error so callers can inspect vendor
    errors as well as successful output. Startup and cleanup failures still
    propagate as exceptions. The caller continues to own the SDK connection.

    ``block_s`` makes listener 1 sleep synchronously on its first event; the two
    execution arguments select where each listener's callbacks run.
    """
    platform = create_cc_api_platform(api, skill_thread_group="audio")
    runtime = RobotRuntime.from_platform(
        InMemoryTaskRepository(), platform, allowed_permissions={"audio"},
    )
    origin = time.monotonic()
    listener2_arrivals: list[float] = []
    blocked = False

    def stamp(name: str, event: Event) -> None:
        now = time.monotonic() - origin
        print(
            f"[{now:7.3f}s] {name} {event.event_type} "
            f"thread={threading.current_thread().name}#{threading.get_ident()}",
            flush=True,
        )

    async def on_event1(event: Event) -> None:
        nonlocal blocked
        stamp("listener1", event)
        if block_s and not blocked:
            blocked = True
            print(
                f"[{time.monotonic() - origin:7.3f}s] listener1 blocks the loop "
                f"for {block_s:g}s inside an async callback",
                flush=True,
            )
            time.sleep(block_s)

    async def on_event2(event: Event) -> None:
        listener2_arrivals.append(time.monotonic() - origin)
        stamp("listener2", event)

    async def on_done1(task: Task) -> None:
        print(f"[listener1 DONE] {task.model_dump_json()}", flush=True)

    async def on_done2(task: Task) -> None:
        print(f"[listener2 DONE] {task.state.value}", flush=True)

    async def on_error1(task_id: str, error: Exception) -> None:
        print(f"[listener1 ERROR] {error}", flush=True)

    async def on_error2(task_id: str, error: Exception) -> None:
        print(f"[listener2 ERROR] {error}", flush=True)

    listener1 = runtime.add_task_listener(
        on_event=on_event1, on_done=on_done1, on_error=on_error1,
        execution=listener1_execution,
    )
    listener2 = runtime.add_task_listener(
        on_event=on_event2, on_done=on_done2, on_error=on_error2,
        execution=listener2_execution,
    )

    await platform.start()
    try:
        await runtime.start()
        try:
            submitted = time.monotonic() - origin
            final = await runtime.submit_and_wait(TaskRequest(
                task_type="announcement",
                steps=[SkillCall(skill="audio.speak", params={"message": message})],
            ))
            await asyncio.gather(listener1.wait(), listener2.wait())
            if block_s:
                report_listener_blocking(
                    [submitted, *listener2_arrivals], block_s,
                    listener1_execution, listener2_execution,
                )
            return final
        finally:
            await runtime.close()
    finally:
        await platform.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-id", required=True, help="registered robot API client ID")
    parser.add_argument("--address", help="robot API host:port; omit when running on the robot")
    parser.add_argument("--message", required=True, help="text to speak")
    parser.add_argument(
        "--listener-block", type=float, default=0.0,
        help="seconds listener1 sleeps inside its async on_event; 0 disables the test",
    )
    parser.add_argument(
        "--listener1-execution", choices=("caller", "dedicated"), default="caller",
        help="callback execution for the listener that blocks; caller shares the submitting loop",
    )
    parser.add_argument(
        "--listener2-execution", choices=("caller", "dedicated"), default="caller",
        help="callback execution for the measuring listener; see the combinations in --help",
    )
    args = parser.parse_args(argv)
    if args.listener_block < 0:
        parser.error("--listener-block must not be negative")
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
        final = asyncio.run(speak(
            api, args.message,
            block_s=args.listener_block,
            listener1_execution=args.listener1_execution,
            listener2_execution=args.listener2_execution,
        ))
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
