"""Run the same RuntimeCore behind UDS and access it with RemoteRuntime."""

import asyncio
import json
import tempfile
from pathlib import Path

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.core import RuntimeCore
from robot_agent_runtime.models import SkillCall, TaskRequest, TaskState
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sdk import RemoteRuntime
from robot_agent_runtime.server import RuntimeServer


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="robot-runtime-demo-") as directory:
        socket_path = str(Path(directory) / "runtime.sock")
        simulator = Simulator(delay_s=0.01)
        server = RuntimeServer(
            RuntimeCore(InMemoryTaskRepository(), simulator.skills), socket_path
        )
        remote = RemoteRuntime(socket_path)
        await server.start()
        try:
            await remote.start()
            task = await remote.submit_task(
                TaskRequest(
                    task_type="remote-demo",
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
                    idempotency_key="remote-demo-1",
                )
            )
            events = []
            async for event in remote.subscribe_task_events(task.id):
                events.append(event.sequence)
            final = await remote.get_task_status(task.id)
            if final.state != TaskState.SUCCEEDED:
                raise RuntimeError(f"Demo task failed: {final.error}")
            print(
                json.dumps(
                    {"state": final.state.value, "events": events, "result": final.result},
                    ensure_ascii=False,
                )
            )
        finally:
            await remote.close()
            await server.close()


if __name__ == "__main__":
    asyncio.run(main())
