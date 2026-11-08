import asyncio
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import (
    EventStreamOverflow, InvalidRequest, TaskEventStreamEnded, TaskWaitTimeout,
)
from robot_agent_runtime.models import Event, SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RemoteRuntime, RobotRuntime
from robot_agent_runtime.transport.server import RuntimeServer


def request(message: str = "hello", *, key: str | None = None) -> TaskRequest:
    return TaskRequest(
        task_type="announcement", idempotency_key=key,
        steps=[SkillCall(skill="audio.speak", params={"message": message})],
    )


def long_request() -> TaskRequest:
    return TaskRequest(
        task_type="waiting", steps=[SkillCall(skill="wait.delay", params={"delay_s": 30})],
    )


@asynccontextmanager
async def running_client(
    remote: bool, *, repository: InMemoryTaskRepository | None = None,
    simulator: Simulator | None = None,
) -> AsyncIterator[tuple[RobotRuntime | RemoteRuntime, Simulator]]:
    repository = repository or InMemoryTaskRepository()
    simulator = simulator or Simulator()
    with tempfile.TemporaryDirectory(prefix="rar-wait-") as directory:
        server = (RuntimeServer(repository, simulator.skills, str(Path(directory) / "s.sock"))
                  if remote else None)
        runtime = (RemoteRuntime(str(Path(directory) / "s.sock")) if remote
                   else RobotRuntime(repository, simulator.skills))
        try:
            if server is not None:
                await server.start()
            await runtime.start()
            yield runtime, simulator
        finally:
            try:
                await runtime.close()
            finally:
                if server is not None:
                    await server.close()


async def test_submit_and_wait_returns_final_result_without_registered_listeners() -> None:
    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            async with asyncio.timeout(1):
                final = await runtime.submit_and_wait(request())
            assert final.state == TaskState.SUCCEEDED
            assert final.result is not None
            assert final.result["steps"][0]["output"]["message"] == "hello"
            assert final == await runtime.get_task_status(final.id)


async def test_submit_and_wait_returns_admission_failure_without_subscribing() -> None:
    def forbidden(task_id: str) -> AsyncGenerator[Event, None]:
        raise AssertionError("A terminal submission needs no subscription")

    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            with patch.object(runtime, "subscribe_task_events", forbidden):
                final = await runtime.submit_and_wait(TaskRequest(
                    task_type="invalid", steps=[SkillCall(skill="missing")],
                ))
            assert final.state == TaskState.FAILED
            assert final.error is not None and final.error.code == "SKILL_NOT_FOUND"


async def test_submit_and_wait_returns_execution_failure_as_a_task() -> None:
    for remote in (False, True):
        async with running_client(remote, simulator=Simulator(fail_skills={"audio.speak"})) as (
            runtime, _,
        ):
            final = await runtime.submit_and_wait(request())
            assert final.state == TaskState.FAILED
            assert final.error is not None and final.error.code == "SKILL_EXECUTION_FAILED"


async def test_submit_and_wait_replays_completion_that_precedes_subscription() -> None:
    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            original_submit = runtime.submit_task

            async def submit_after_completion(value: TaskRequest) -> Task:
                submitted = await original_submit(value)
                assert not submitted.state.is_terminal
                async for _ in runtime.subscribe_task_events(submitted.id):
                    pass
                return submitted

            with patch.object(runtime, "submit_task", submit_after_completion):
                async with asyncio.timeout(1):
                    final = await runtime.submit_and_wait(request())
            assert final.state == TaskState.SUCCEEDED


async def test_submit_and_wait_returns_every_existing_terminal_state_without_reexecution() -> None:
    def forbidden(task_id: str) -> AsyncGenerator[Event, None]:
        raise AssertionError("An existing terminal task needs no subscription")

    for remote in (False, True):
        repository = InMemoryTaskRepository()
        terminal = [state for state in TaskState if state.is_terminal]
        for state in terminal:
            await repository.create_task(Task(
                id=state.value, task_type="announcement", state=state,
                steps=request().steps, idempotency_key=state.value,
            ))
        async with running_client(remote, repository=repository) as (runtime, simulator):
            with patch.object(runtime, "subscribe_task_events", forbidden):
                for state in terminal:
                    final = await runtime.submit_and_wait(request(key=state.value))
                    assert final.id == state.value and final.state == state
            assert simulator.skills["audio.speak"].calls == 0


async def test_submit_and_wait_keeps_concurrent_results_and_existing_listeners_independent() -> None:
    for remote in (False, True):
        async with running_client(remote) as (runtime, simulator):
            completed: list[Task] = []

            async def on_done(task: Task) -> None:
                completed.append(task)

            listener = runtime.add_task_listener(on_done=on_done)
            first, second = await asyncio.gather(
                runtime.submit_and_wait(request("first", key="first")),
                runtime.submit_and_wait(request("second", key="second")),
            )
            repeated = await runtime.submit_and_wait(request("first", key="first"))
            await listener.wait()
            assert first.id != second.id and repeated == first
            assert first.result is not None and second.result is not None
            assert first.result["steps"][0]["output"]["message"] == "first"
            assert second.result["steps"][0]["output"]["message"] == "second"
            assert {task.id for task in completed} == {first.id, second.id}
            assert len(completed) == simulator.skills["audio.speak"].calls == 2


async def test_submit_and_wait_timeout_releases_subscription_without_cancelling_task() -> None:
    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            original_subscribe = runtime.subscribe_task_events
            closed = asyncio.Event()

            async def tracked(task_id: str) -> AsyncGenerator[Event, None]:
                try:
                    async with aclosing(original_subscribe(task_id)) as events:
                        async for event in events:
                            yield event
                finally:
                    closed.set()

            with patch.object(runtime, "subscribe_task_events", tracked):
                with pytest.raises(TaskWaitTimeout) as caught:
                    await runtime.submit_and_wait(long_request(), timeout_s=0.03)
            assert caught.value.info.code == "TASK_WAIT_TIMEOUT"
            assert caught.value.info.details["timeout_s"] == 0.03
            assert closed.is_set()
            task_id = caught.value.info.details["task_id"]
            current = await runtime.get_task_status(task_id)
            assert not current.state.is_terminal and not current.cancel_requested
            assert (await runtime.cancel_task(task_id)).state == TaskState.CANCELED


async def test_submit_and_wait_caller_cancellation_releases_only_its_subscription() -> None:
    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            original_subscribe = runtime.subscribe_task_events
            entered, closed = asyncio.Event(), asyncio.Event()
            task_ids: list[str] = []

            async def tracked(task_id: str) -> AsyncGenerator[Event, None]:
                task_ids.append(task_id)
                try:
                    async with aclosing(original_subscribe(task_id)) as events:
                        async for event in events:
                            if event.event_type == "SKILL_STARTED":
                                entered.set()
                            yield event
                finally:
                    closed.set()

            with patch.object(runtime, "subscribe_task_events", tracked):
                waiting = asyncio.create_task(runtime.submit_and_wait(long_request()))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    waiting.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await waiting
                finally:
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
            assert closed.is_set() and len(task_ids) == 1
            current = await runtime.get_task_status(task_ids[0])
            assert not current.state.is_terminal and not current.cancel_requested
            await runtime.cancel_task(current.id)


async def test_submit_and_wait_timeout_starts_after_submission_returns() -> None:
    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            original_submit = runtime.submit_task
            submitted, release = asyncio.Event(), asyncio.Event()

            async def gated_submit(value: TaskRequest) -> Task:
                task = await original_submit(value)
                submitted.set()
                await release.wait()
                return task

            with patch.object(runtime, "submit_task", gated_submit):
                waiting = asyncio.create_task(runtime.submit_and_wait(request(), timeout_s=0.1))
                try:
                    await asyncio.wait_for(submitted.wait(), 1)
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(waiting), 0.15)
                    assert not waiting.done()
                    release.set()
                    final = await asyncio.wait_for(waiting, 1)
                    assert final.state == TaskState.SUCCEEDED
                finally:
                    release.set()
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)


async def test_submit_and_wait_rejects_invalid_wait_timeout_before_submission() -> None:
    async def forbidden(value: TaskRequest) -> Task:
        raise AssertionError("Invalid wait options must not submit a robot task")

    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            with patch.object(runtime, "submit_task", forbidden):
                for value in (0, -1, True, "1", float("inf"), float("nan")):
                    with pytest.raises(InvalidRequest):
                        await runtime.submit_and_wait(request(), timeout_s=value)


async def test_submit_and_wait_rejects_an_event_stream_ending_before_terminal() -> None:
    async def empty(task_id: str) -> AsyncGenerator[Event, None]:
        for event in ():
            yield event

    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            with patch.object(runtime, "subscribe_task_events", empty):
                with pytest.raises(TaskEventStreamEnded) as caught:
                    await runtime.submit_and_wait(long_request())
            task_id = caught.value.info.details["task_id"]
            assert not (await runtime.get_task_status(task_id)).state.is_terminal
            await runtime.cancel_task(task_id)


async def test_submit_and_wait_requires_a_terminal_snapshot_after_terminal_event() -> None:
    async def false_terminal(task_id: str) -> AsyncGenerator[Event, None]:
        yield Event(
            event_id="false-terminal", task_id=task_id, sequence=1,
            event_type="TASK_SUCCEEDED", data={"state": TaskState.SUCCEEDED.value},
        )

    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            with patch.object(runtime, "subscribe_task_events", false_terminal):
                with pytest.raises(TaskEventStreamEnded) as caught:
                    await runtime.submit_and_wait(long_request())
            await runtime.cancel_task(caught.value.info.details["task_id"])


async def test_submit_and_wait_propagates_subscription_errors_without_cancelling_task() -> None:
    error = EventStreamOverflow("overflow", {"after_sequence": 3})

    async def failing(task_id: str) -> AsyncGenerator[Event, None]:
        raise error
        yield

    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            value = long_request().model_copy(update={"idempotency_key": "overflow"})
            with patch.object(runtime, "subscribe_task_events", failing):
                with pytest.raises(EventStreamOverflow) as caught:
                    await runtime.submit_and_wait(value, timeout_s=1)
            assert caught.value is error
            current = await runtime.submit_task(value)
            assert not current.state.is_terminal and not current.cancel_requested
            await runtime.cancel_task(current.id)


async def test_submit_and_wait_preserves_a_query_timeout_that_is_not_its_wait_deadline() -> None:
    error = TimeoutError("query timed out")

    async def failed_query(task_id: str) -> Task:
        raise error

    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            with patch.object(runtime, "get_task_status", failed_query):
                with pytest.raises(TimeoutError) as caught:
                    await runtime.submit_and_wait(request(), timeout_s=1)
            assert caught.value is error


async def test_submit_and_wait_wait_deadline_includes_the_final_query() -> None:
    entered = asyncio.Event()

    async def blocked_query(task_id: str) -> Task:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("The query must be cancelled at the wait deadline")

    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            entered.clear()
            with patch.object(runtime, "get_task_status", blocked_query):
                with pytest.raises(TaskWaitTimeout) as caught:
                    await runtime.submit_and_wait(request(), timeout_s=0.03)
            assert entered.is_set()
            final = await runtime.get_task_status(caught.value.info.details["task_id"])
            assert final.state == TaskState.SUCCEEDED


async def test_submit_and_wait_task_execution_deadline_remains_a_task_failure() -> None:
    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            value = long_request().model_copy(update={"timeout_s": 0.03})
            final = await runtime.submit_and_wait(value, timeout_s=1)
            assert final.state == TaskState.FAILED
            assert final.error is not None and final.error.code == "TASK_TIMEOUT"


async def test_submit_and_wait_simultaneous_idempotent_calls_execute_only_once() -> None:
    for remote in (False, True):
        async with running_client(remote) as (runtime, simulator):
            value = request(key="same-task")
            first, second = await asyncio.gather(
                runtime.submit_and_wait(value), runtime.submit_and_wait(value),
            )
            assert first == second and first.state == TaskState.SUCCEEDED
            assert simulator.skills["audio.speak"].calls == 1


async def test_submit_and_wait_returns_canceled_after_explicit_cancellation() -> None:
    for remote in (False, True):
        async with running_client(remote) as (runtime, _):
            entered = asyncio.Event()
            task_ids: list[str] = []

            async def on_event(event: Event) -> None:
                if event.event_type == "SKILL_STARTED":
                    task_ids.append(event.task_id)
                    entered.set()

            listener = runtime.add_task_listener(on_event=on_event)
            waiting = asyncio.create_task(runtime.submit_and_wait(long_request()))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await runtime.cancel_task(task_ids[0])
                final = await asyncio.wait_for(waiting, 1)
                assert final.id == task_ids[0] and final.state == TaskState.CANCELED
                await listener.wait()
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
