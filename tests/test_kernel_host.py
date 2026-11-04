import asyncio
import threading
from collections.abc import AsyncGenerator
from concurrent.futures import Future

import pytest

from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import EventStreamOverflow, ExtensionCapacityExceeded, ExtensionTimeout, InvalidRequest
from robot_agent_runtime.models import Event, SkillCall, Task, TaskRequest, TaskState
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime, RuntimeOptions


def request() -> TaskRequest:
    return TaskRequest(task_type="state", steps=[SkillCall(skill="robot.get_state")])


async def test_embedded_core_lifecycle_runs_on_a_dedicated_thread() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    owner = threading.get_ident()
    starts: list[int] = []
    closes: list[int] = []
    original_start = runtime._core.start
    original_close = runtime._core.close

    async def start() -> None:
        if not runtime._core._started:
            starts.append(threading.get_ident())
        await original_start()

    async def close() -> None:
        closes.append(threading.get_ident())
        await original_close()

    runtime._core.start = start
    runtime._core.close = close
    try:
        await runtime.start()
        await runtime.start()
        await runtime.submit_task(request())
    finally:
        await runtime.close()
        await runtime.close()
    assert len(starts) == 1
    assert starts == closes
    assert starts[0] != owner


async def test_blocking_async_listener_cannot_stall_the_kernel_thread() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    query_threads: list[int] = []
    original_get = runtime._core.get_task_status

    async def get_task_status(task_id: str) -> Task:
        query_threads.append(threading.get_ident())
        return await original_get(task_id)

    runtime._core.get_task_status = get_task_status
    await runtime.start()
    entered = threading.Event()
    release = threading.Event()
    queried: list[TaskState] = []
    callback_threads: list[int] = []
    failures: list[BaseException] = []
    owner = threading.get_ident()
    submitted = await runtime.submit_task(request())

    def query() -> None:
        try:
            if not entered.wait(1):
                raise AssertionError("Listener never entered")
            task = asyncio.run(runtime.get_task_status(submitted.id))
            queried.append(task.state)
        except BaseException as error:
            failures.append(error)
        finally:
            release.set()

    async def on_event(event: Event) -> None:
        callback_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(1), "Independent query could not reach the kernel"

    worker = threading.Thread(target=query, daemon=True)
    listener = runtime.add_task_listener(on_event=on_event)
    worker.start()
    try:
        runtime._listeners.track(submitted)
        await asyncio.wait_for(listener.wait(), 2)
        assert queried
        assert not failures
        assert callback_threads and set(callback_threads) == {owner}
        assert query_threads and set(query_threads).isdisjoint({owner, worker.ident})
    finally:
        release.set()
        await runtime.close()


async def test_active_subscription_retains_shutdown_events_after_loop_stops() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    submitted = await runtime.submit_task(
        TaskRequest(
            task_type="long",
            steps=[SkillCall(skill="wait.delay", params={"delay_s": 30})],
        )
    )
    stream = runtime.subscribe_task_events(submitted.id)
    events = [await anext(stream)]
    await runtime.close()
    async with asyncio.timeout(1):
        async for event in stream:
            events.append(event)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert TaskState(events[-1].data["state"]).is_terminal
    assert not runtime._core._events._subscribers


async def test_cancelling_pending_subscription_closes_its_kernel_generator() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    stream = runtime.subscribe_task_events("missing")
    pending = asyncio.create_task(anext(stream))
    try:
        async with asyncio.timeout(1):
            while not runtime._core._events._subscribers:
                await asyncio.sleep(0)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
        assert not runtime._core._events._subscribers
        assert not runtime._host._streams
    finally:
        await runtime.close()


async def test_embedded_close_before_start_is_idempotent() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.close()
    await runtime.close()
    assert runtime._core._closed


async def test_sdk_rejects_submissions_as_soon_as_shutdown_begins() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    entered, release = asyncio.Event(), asyncio.Event()
    original_close = runtime._listeners.close

    async def paused_close() -> None:
        entered.set()
        await release.wait()
        await original_close()

    runtime._listeners.close = paused_close
    closing = asyncio.create_task(runtime.close())
    await entered.wait()
    try:
        with pytest.raises(InvalidRequest):
            await runtime.submit_task(request())
    finally:
        release.set()
        await closing


async def test_cancelled_start_waiter_does_not_cancel_shared_initialization() -> None:
    class PausedRepository(InMemoryTaskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.starts = 0
            self.entered: Future[None] = Future()
            self.release: Future[None] = Future()

        async def start(self) -> None:
            self.starts += 1
            if self.starts == 1:
                self.entered.set_result(None)
                await asyncio.wrap_future(self.release)

    repository = PausedRepository()
    runtime = RobotRuntime(repository, Simulator().skills)
    starting = asyncio.create_task(runtime.start())
    await asyncio.wait_for(asyncio.wrap_future(repository.entered), 1)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starting, 1)
    repository.release.set_result(None)
    try:
        await runtime.start()
        assert repository.starts == 1
        assert (await runtime.submit_task(request())).id
    finally:
        await runtime.close()


async def test_shutdown_has_capacity_while_a_subscription_is_waiting() -> None:
    repository = InMemoryTaskRepository()
    await repository.append_event(Event(
        event_id="seed", task_id="missing", sequence=1,
        event_type="SKILL_PROGRESS", data={"state": TaskState.RUNNING.value},
    ))
    runtime = RobotRuntime(
        repository, Simulator().skills,
        options=RuntimeOptions(max_pending_calls=1),
    )
    await runtime.start()
    stream = runtime.subscribe_task_events("missing")
    assert (await anext(stream)).sequence == 1
    pending = asyncio.create_task(anext(stream))
    async with asyncio.timeout(1):
        while not runtime._core._events._subscribers.get("missing"):
            await asyncio.sleep(0)
    try:
        await asyncio.wait_for(runtime.close(), 1)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, 1)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


async def test_failed_start_can_retry_without_replacing_the_kernel_thread() -> None:
    class FailingRepository(InMemoryTaskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.starts = 0

        async def start(self) -> None:
            self.starts += 1
            if self.starts == 1:
                raise ValueError("first startup failed")

    repository = FailingRepository()
    runtime = RobotRuntime(repository, Simulator().skills)
    try:
        with pytest.raises(ValueError, match="first startup failed"):
            await runtime.start()
        thread = runtime._host._loop._thread
        await runtime.start()
        assert repository.starts == 2
        assert runtime._host._loop._thread is thread
    finally:
        await runtime.close()


async def test_shutdown_reports_a_stuck_stream_and_keeps_its_real_job_alive() -> None:
    runtime = RobotRuntime(
        InMemoryTaskRepository(), Simulator().skills,
        options=RuntimeOptions(port_timeout_s=0.03, cancel_grace_s=0.01),
    )
    entered: Future[None] = Future()
    release: Future[None] = Future()

    async def stuck(
        task_id: str, after_sequence: int = 0
    ) -> AsyncGenerator[Event, None]:
        entered.set_result(None)
        while not release.done():
            try:
                await asyncio.shield(asyncio.wrap_future(release))
            except asyncio.CancelledError:
                continue
        if False:
            yield Event(event_id="unused", task_id=task_id, sequence=1, event_type="unused")

    runtime._core.subscribe_task_events = stuck
    await runtime.start()
    stream = runtime.subscribe_task_events("stuck")
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(asyncio.wrap_future(entered), 1)
    try:
        with pytest.raises(ExceptionGroup) as caught:
            await asyncio.wait_for(runtime.close(), 1)
        assert any(
            isinstance(error, ExtensionTimeout) and "subscriptions" in error.info.message
            for error in caught.value.exceptions
        )
        thread = runtime._host._loop._thread
        assert thread is not None and thread.is_alive()
    finally:
        release.set_result(None)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, 1)
        await stream.aclose()


async def test_control_and_subscription_capacity_are_independent_of_ordinary_calls() -> None:
    runtime = RobotRuntime(
        InMemoryTaskRepository(), Simulator().skills,
        options=RuntimeOptions(max_pending_calls=1, max_control_calls=1, max_subscriptions=1),
    )
    await runtime.start()
    task = await runtime.submit_task(request())
    async with asyncio.timeout(1):
        while not (await runtime.get_task_status(task.id)).state.is_terminal:
            await asyncio.sleep(0)
    entered: Future[None] = Future()
    release: Future[None] = Future()

    async def blocked_skills() -> list[object]:
        entered.set_result(None)
        await asyncio.wrap_future(release)
        return []

    runtime._core.list_skills = blocked_skills
    ordinary = asyncio.create_task(runtime.list_skills())
    await asyncio.wait_for(asyncio.wrap_future(entered), 1)
    stream = runtime.subscribe_task_events("missing")
    pending = asyncio.create_task(anext(stream))
    try:
        async with asyncio.timeout(1):
            while not runtime._core._events._subscribers.get("missing"):
                if pending.done():
                    pending.result()
                await asyncio.sleep(0)
        assert (await asyncio.wait_for(runtime.get_task_status(task.id), 1)).id == task.id
        assert (await asyncio.wait_for(runtime.cancel_task(task.id), 1)).id == task.id
        extra = runtime.subscribe_task_events("other")
        with pytest.raises(ExtensionCapacityExceeded):
            await anext(extra)
        await extra.aclose()
        with pytest.raises(ExtensionCapacityExceeded):
            await runtime.list_skills()
    finally:
        release.set_result(None)
        await ordinary
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
        await runtime.close()


async def test_concurrent_start_and_close_share_completion_across_caller_loops() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    entered: Future[None] = Future()
    release: Future[None] = Future()
    starts: list[int] = []
    original_start = runtime._core.start

    async def paused_start() -> None:
        starts.append(threading.get_ident())
        if not entered.done():
            entered.set_result(None)
        await asyncio.wrap_future(release)
        await original_start()

    runtime._core.start = paused_start
    first_done: Future[None] = Future()

    def start_on_another_loop() -> None:
        try:
            asyncio.run(runtime.start())
        except BaseException as error:
            first_done.set_exception(error)
        else:
            first_done.set_result(None)

    threading.Thread(target=start_on_another_loop, daemon=True).start()
    await asyncio.wait_for(asyncio.wrap_future(entered), 1)
    second = asyncio.create_task(runtime.start())
    release.set_result(None)
    try:
        await asyncio.wait_for(second, 1)
        await asyncio.wait_for(asyncio.wrap_future(first_done), 1)
        assert len(starts) == 1
        await runtime.close()
        other_closed: Future[None] = Future()

        def close_on_another_loop() -> None:
            try:
                asyncio.run(runtime.close())
            except BaseException as error:
                other_closed.set_exception(error)
            else:
                other_closed.set_result(None)

        threading.Thread(target=close_on_another_loop, daemon=True).start()
        await asyncio.wait_for(asyncio.wrap_future(other_closed), 1)
    finally:
        await runtime.close()


async def test_shutdown_event_buffer_overflow_reports_the_delivered_watermark() -> None:
    runtime = RobotRuntime(
        InMemoryTaskRepository(), Simulator().skills,
        options=RuntimeOptions(max_event_buffer=2),
    )

    async def events(task_id: str, after_sequence: int = 0) -> AsyncGenerator[Event, None]:
        for sequence in range(1, 8):
            yield Event(
                event_id=str(sequence), task_id=task_id, sequence=sequence,
                event_type="SKILL_PROGRESS", data={"state": TaskState.RUNNING.value},
            )

    runtime._core.subscribe_task_events = events
    await runtime.start()
    stream = runtime.subscribe_task_events("overflow")
    assert (await anext(stream)).sequence == 1
    await runtime.close()
    assert (await anext(stream)).sequence == 2
    assert (await anext(stream)).sequence == 3
    with pytest.raises(EventStreamOverflow) as caught:
        await anext(stream)
    assert caught.value.info.details["task_id"] == "overflow"
    assert caught.value.info.details["after_sequence"] == 3
    await stream.aclose()


async def test_control_reservation_reaches_repository_while_replay_is_blocked() -> None:
    entered: Future[None] = Future()
    release: Future[None] = Future()

    class PausedReplay(InMemoryTaskRepository):
        async def list_events_page(
            self, task_id: str, after_sequence: int = 0, limit: int = 128,
        ) -> list[Event]:
            if task_id == "hold":
                entered.set_result(None)
                await asyncio.wrap_future(release)
            return await super().list_events_page(task_id, after_sequence, limit)

    runtime = RobotRuntime(
        PausedReplay(), Simulator().skills,
        options=RuntimeOptions(max_pending_calls=1, max_control_calls=1),
    )
    await runtime.start()
    task = await runtime.submit_task(request())
    async with asyncio.timeout(1):
        while not (await runtime.get_task_status(task.id)).state.is_terminal:
            await asyncio.sleep(0)
    stream = runtime.subscribe_task_events("hold")
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(asyncio.wrap_future(entered), 1)
    try:
        assert (await asyncio.wait_for(runtime.get_task_status(task.id), 0.5)).id == task.id
        assert (await asyncio.wait_for(runtime.cancel_task(task.id), 0.5)).id == task.id
    finally:
        release.set_result(None)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
        await runtime.close()


async def test_task_request_subclass_hooks_do_not_cross_into_the_kernel() -> None:
    class ForeignRequest(TaskRequest):
        def model_dump_json(self, **kwargs: object) -> str:
            raise AssertionError("Request serialization hooks must not run")

    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    await runtime.start()
    try:
        task = await runtime.submit_task(ForeignRequest(
            task_type="foreign", steps=[SkillCall(skill="robot.get_state")],
        ))
        assert task.id
    finally:
        await runtime.close()


async def test_cancelled_close_waiter_during_start_does_not_duplicate_lifecycle() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    entered: Future[None] = Future()
    release: Future[None] = Future()
    shutdown_entered: Future[None] = Future()
    starts: list[int] = []
    closes: list[int] = []
    original_start, original_close = runtime._core.start, runtime._core.close

    async def paused_start() -> None:
        starts.append(threading.get_ident())
        entered.set_result(None)
        await asyncio.wrap_future(release)
        await original_start()

    async def close_once() -> None:
        closes.append(threading.get_ident())
        await original_close()

    original_shutdown = runtime._host._shutdown

    async def shutdown() -> None:
        shutdown_entered.set_result(None)
        await original_shutdown()

    runtime._core.start, runtime._core.close = paused_start, close_once
    runtime._host._shutdown = shutdown
    starting = asyncio.create_task(runtime.start())
    await asyncio.wait_for(asyncio.wrap_future(entered), 1)
    closing = asyncio.create_task(runtime.close())
    await asyncio.wait_for(asyncio.wrap_future(shutdown_entered), 1)
    starting.cancel()
    closing.cancel()
    await asyncio.gather(starting, closing, return_exceptions=True)
    release.set_result(None)
    await asyncio.wait_for(runtime.close(), 1)
    assert len(starts) == len(closes) == 1
    assert starts == closes


async def test_cancelled_close_before_thread_readiness_still_shuts_down() -> None:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills)
    entered = asyncio.Event()
    closed: Future[None] = Future()
    first = True
    original_ready = runtime._host._loop.start
    original_close = runtime._core.close

    async def paused_readiness() -> None:
        nonlocal first
        if first:
            first = False
            entered.set()
            await asyncio.Event().wait()
        await original_ready()

    async def close() -> None:
        await original_close()
        closed.set_result(None)

    runtime._host._loop.start = paused_readiness
    runtime._core.close = close
    closing = asyncio.create_task(runtime.close())
    await asyncio.wait_for(entered.wait(), 1)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 1)
    try:
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(closed)), 0.5)
        assert runtime._core._closed
    finally:
        await runtime.close()


async def test_shutdown_drains_remaining_history_pages_before_repository_close() -> None:
    repository = InMemoryTaskRepository()
    for sequence in range(1, 9):
        await repository.append_event(Event(
            event_id=f"history-{sequence}", task_id="paged", sequence=sequence,
            event_type="TASK_SUCCEEDED" if sequence == 8 else "SKILL_PROGRESS",
            data={"state": TaskState.SUCCEEDED.value if sequence == 8 else TaskState.RUNNING.value},
        ))
    runtime = RobotRuntime(
        repository, Simulator().skills,
        options=RuntimeOptions(event_page_size=2, max_event_buffer=20),
    )
    await runtime.start()
    stream = runtime.subscribe_task_events("paged")
    events = [await anext(stream)]
    await runtime.close()
    async with asyncio.timeout(1):
        async for event in stream:
            events.append(event)
    assert [event.sequence for event in events] == list(range(1, 9))
    assert events[-1].data["state"] == TaskState.SUCCEEDED.value
    assert not runtime._core._events._subscribers


async def test_shutdown_replay_does_not_overfill_repository_control_capacity() -> None:
    entered: Future[None] = Future()
    release: Future[None] = Future()

    class PausedPages(InMemoryTaskRepository):
        async def list_events_page(
            self, task_id: str, after_sequence: int = 0, limit: int = 128,
        ) -> list[Event]:
            if after_sequence == 1 and not release.done():
                if not entered.done():
                    entered.set_result(None)
                await asyncio.shield(asyncio.wrap_future(release))
            return await super().list_events_page(task_id, after_sequence, limit)

    repository = PausedPages()
    task_ids = tuple(f"task-{index}" for index in range(6))
    for task_id in task_ids:
        for sequence in (1, 2, 3):
            await repository.append_event(Event(
                event_id=f"{task_id}-{sequence}", task_id=task_id, sequence=sequence,
                event_type="TASK_SUCCEEDED" if sequence == 3 else "SKILL_PROGRESS",
                data={"state": TaskState.SUCCEEDED.value if sequence == 3 else TaskState.RUNNING.value},
            ))
    runtime = RobotRuntime(
        repository, Simulator().skills,
        options=RuntimeOptions(event_page_size=1, max_control_calls=1),
    )
    await runtime.start()
    streams = [runtime.subscribe_task_events(task_id) for task_id in task_ids]
    for stream in streams:
        assert (await anext(stream)).sequence == 1
    closing = asyncio.create_task(runtime.close())
    await asyncio.wait_for(asyncio.wrap_future(entered), 1)

    async def barrier() -> None:
        pass

    await runtime._host._loop.call(barrier)
    release.set_result(None)
    await closing
    for stream in streams:
        assert [event.sequence async for event in stream] == [2, 3]


async def test_shutdown_timeout_marks_unvisited_streams_with_replay_watermarks() -> None:
    runtime = RobotRuntime(
        InMemoryTaskRepository(), Simulator().skills,
        options=RuntimeOptions(port_timeout_s=0.03, cancel_grace_s=0.01),
    )
    release: Future[None] = Future()
    stopped: Future[None] = Future()
    blocked: str | None = None

    async def source(task_id: str, after_sequence: int = 0) -> AsyncGenerator[Event, None]:
        try:
            for sequence in (1, 2, 3):
                if task_id == blocked and sequence == 3:
                    while not release.done():
                        try:
                            await asyncio.shield(asyncio.wrap_future(release))
                        except asyncio.CancelledError:
                            continue
                yield Event(
                    event_id=f"{task_id}-{sequence}", task_id=task_id, sequence=sequence,
                    event_type="TASK_SUCCEEDED" if sequence == 3 else "SKILL_PROGRESS",
                    data={"state": TaskState.SUCCEEDED.value if sequence == 3 else TaskState.RUNNING.value},
                )
        finally:
            if task_id == blocked and not stopped.done():
                stopped.set_result(None)

    runtime._core.subscribe_task_events = source
    await runtime.start()
    streams = {
        task_id: runtime.subscribe_task_events(task_id)
        for task_id in ("first", "second", "third")
    }
    for stream in streams.values():
        assert (await anext(stream)).sequence == 1

    async def close_order() -> tuple[str, str]:
        ordered = tuple(runtime._host._streams)
        return ordered[0]._task_id, ordered[1]._task_id

    completed, blocked = await runtime._host._loop.call(close_order)
    try:
        with pytest.raises(ExceptionGroup) as closing:
            await asyncio.wait_for(runtime.close(), 1)
        assert any(isinstance(error, ExtensionTimeout) for error in closing.value.exceptions)
        for task_id, stream in streams.items():
            if task_id == completed:
                assert [event.sequence async for event in stream] == [2, 3]
                continue
            watermark = 1
            if task_id == blocked:
                assert (await anext(stream)).sequence == 2
                watermark = 2
            with pytest.raises(EventStreamOverflow) as caught:
                await anext(stream)
            assert caught.value.info.details["task_id"] == task_id
            assert caught.value.info.details["after_sequence"] == watermark
    finally:
        release.set_result(None)
        await asyncio.wait_for(asyncio.wrap_future(stopped), 1)
        for stream in streams.values():
            await stream.aclose()
