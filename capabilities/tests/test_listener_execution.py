import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from concurrent.futures import Future
from typing import Any

import pytest

from robot_agent_capabilities import RobotCapabilities
from robot_agent_capabilities.errors import CapabilityConfigurationError
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.errors import (
    InvalidRequest, ListenerCallbackQueueFull, ListenerCallbackTimeout, ListenerCapacityExceeded,
)
from robot_agent_runtime.models import Event, PlatformEvent, Task, TaskRequest, SkillCall
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime


class Source:
    async def watch(self) -> AsyncIterator[PlatformEvent]:
        yield PlatformEvent(source="test", event_type="test.snapshot")
        await asyncio.Event().wait()


@asynccontextmanager
async def running(options: RuntimeOptions | None = None) -> AsyncIterator[tuple[RobotCapabilities, RobotRuntime]]:
    runtime = RobotRuntime(InMemoryTaskRepository(), Simulator().skills, event_source=Source())
    await runtime.start()
    try:
        kwargs = {} if options is None else {"listener_options": options}
        robot = await RobotCapabilities.create(runtime, **kwargs)
        try:
            yield robot, runtime
        finally:
            await robot.close_listeners()
    finally:
        await runtime.close()


async def _global_ready(runtime: RobotRuntime) -> None:
    async def registered() -> bool:
        return bool(runtime._core._events._all_subscribers)

    async with asyncio.timeout(1):
        while not await runtime._host._loop.call(registered):
            pass


async def test_caller_callbacks_keep_the_existing_observation_loop() -> None:
    caller = asyncio.get_running_loop()
    seen_platform = asyncio.Event()
    locations: list[asyncio.AbstractEventLoop] = []

    async def on_done(task: Task) -> None:
        locations.append(asyncio.get_running_loop())

    async def on_platform(event: PlatformEvent) -> None:
        locations.append(asyncio.get_running_loop())
        seen_platform.set()

    async with running() as (robot, _):
        listener = robot.add_task_listener(on_done=on_done, execution="caller")
        robot.add_platform_listener(on_event=on_platform, execution="caller")
        await robot.invoke("robot_get_state", {}, operation_id="caller")
        await asyncio.wait_for(listener.wait(), 1)
        await asyncio.wait_for(seen_platform.wait(), 1)
    assert locations == [caller, caller]


async def test_task_and_platform_groups_share_one_loop_for_all_callback_kinds() -> None:
    caller = asyncio.get_running_loop()
    received = asyncio.Event()
    locations: list[asyncio.AbstractEventLoop] = []
    threads: list[int] = []

    def record() -> None:
        locations.append(asyncio.get_running_loop())
        threads.append(threading.get_ident())

    async def on_event(event: Event) -> None:
        record()

    def on_done(task: Task) -> None:
        record()

    def on_platform(event: PlatformEvent) -> Awaitable[None]:
        record()

        async def finish() -> None:
            record()
            caller.call_soon_threadsafe(received.set)

        return finish()

    async with running() as (robot, _):
        listener = robot.add_task_listener(on_event=on_event, on_done=on_done,
                                           execution="group", group="reporting")
        robot.add_platform_listener(on_event=on_platform, execution="group", group="reporting")
        await robot.invoke("robot_get_state", {}, operation_id="group")
        await asyncio.wait_for(listener.wait(), 1)
        await asyncio.wait_for(received.wait(), 1)
    assert len(locations) > 3 and len(set(locations)) == 1 and caller not in locations
    assert len(set(threads)) == 1 and threading.get_ident() not in threads


async def test_dedicated_registrations_and_different_groups_use_distinct_loops() -> None:
    caller = asyncio.get_running_loop()
    received = [asyncio.Event(), asyncio.Event()]
    locations: dict[str, asyncio.AbstractEventLoop] = {}

    def callback(name: str) -> Any:
        async def receive(value: Event | PlatformEvent | Task) -> None:
            locations[name] = asyncio.get_running_loop()
            if name.startswith("platform"):
                caller.call_soon_threadsafe(received[int(name[-1])].set)
        return receive

    async with running() as (robot, _):
        listeners = [
            robot.add_task_listener(on_done=callback("task-a"), execution="dedicated"),
            robot.add_task_listener(on_done=callback("task-b"), execution="dedicated"),
            robot.add_task_listener(on_done=callback("group-a"), execution="group", group="a"),
            robot.add_task_listener(on_done=callback("group-b"), execution="group", group="b"),
        ]
        for index in range(2):
            robot.add_platform_listener(on_event=callback(f"platform-{index}"), execution="dedicated")
        await robot.invoke("robot_get_state", {}, operation_id="distinct")
        await asyncio.wait_for(asyncio.gather(*(item.wait() for item in listeners), *(item.wait() for item in received)), 2)
    assert len(locations) == 6 and len(set(locations.values())) == 6 and caller not in locations.values()


async def test_same_group_names_are_local_to_each_capability_client_and_sdk() -> None:
    locations: dict[str, asyncio.AbstractEventLoop] = {}

    def callback(name: str) -> Any:
        async def on_done(task: Task) -> None:
            locations[name] = asyncio.get_running_loop()
        return on_done

    async with running() as (first, runtime):
        second = await RobotCapabilities.create(runtime)
        sdk_listener = runtime.add_task_listener(on_done=callback("sdk"), execution="group", group="same")
        try:
            one = first.add_task_listener(on_done=callback("first"), execution="group", group="same")
            two = second.add_task_listener(on_done=callback("second"), execution="group", group="same")
            await first.invoke("robot_get_state", {}, operation_id="first")
            await second.invoke("robot_get_state", {}, operation_id="second")
            await asyncio.wait_for(asyncio.gather(one.wait(), two.wait(), sdk_listener.wait()), 2)
            assert len(set(locations.values())) == 3
        finally:
            await second.close_listeners()
            await sdk_listener.close()


async def test_global_listener_uses_the_selected_callback_loop() -> None:
    caller = asyncio.get_running_loop()
    received = asyncio.Event()
    locations: list[asyncio.AbstractEventLoop] = []

    async def on_done(task: Task) -> None:
        locations.append(asyncio.get_running_loop())
        caller.call_soon_threadsafe(received.set)

    async with running() as (robot, runtime):
        listener = robot.add_task_listener(all_tasks=True, on_done=on_done, execution="dedicated")
        await _global_ready(runtime)
        await runtime.submit_and_wait(TaskRequest(task_type="outside", steps=[SkillCall(skill="robot.get_state")]))
        await asyncio.wait_for(received.wait(), 1)
        await listener.close()
    assert locations and caller not in locations


async def test_execution_parameters_are_validated_for_both_listener_entries() -> None:
    invalid = (
        {"execution": "unknown"}, {"execution": None}, {"execution": []},
        {"execution": "group"}, {"execution": "group", "group": ""},
        {"execution": "group", "group": "   "}, {"execution": "group", "group": 7},
        {"execution": "caller", "group": "ui"}, {"execution": "dedicated", "group": "ui"},
    )
    async with running() as (robot, _):
        for add in (robot.add_task_listener, robot.add_platform_listener):
            for config in invalid:
                with pytest.raises(InvalidRequest):
                    add(on_event=lambda event: None, **config)


async def test_thread_budget_counts_shared_members_once_and_releases_last_member() -> None:
    options = RuntimeOptions(max_listener_threads=1)
    async with running(options) as (robot, _):
        task = robot.add_task_listener(on_done=lambda task: None, execution="group", group="shared")
        platform = robot.add_platform_listener(on_event=lambda event: None, execution="group", group="shared")
        with pytest.raises(ListenerCapacityExceeded):
            robot.add_task_listener(on_done=lambda task: None, execution="dedicated")
        await platform.close()
        with pytest.raises(ListenerCapacityExceeded):
            robot.add_platform_listener(on_event=lambda event: None, execution="dedicated")
        await task.close()
        replacement = robot.add_platform_listener(on_event=lambda event: None, execution="dedicated")
        await replacement.close()


async def test_isolated_callback_can_close_itself_and_cannot_wait_for_itself() -> None:
    caller = asyncio.get_running_loop()
    for execution in ("group", "dedicated"):
        for kind in ("task", "platform", "global"):
            returned = asyncio.Event()
            delivered: list[str] = []
            config = {"execution": execution, **({"group": "self"} if execution == "group" else {})}
            async with running() as (robot, runtime):
                async def on_event(event: Event | PlatformEvent) -> None:
                    delivered.append(event.event_type)
                    with pytest.raises(InvalidRequest):
                        await listener.wait()
                    await listener.close()
                    caller.call_soon_threadsafe(returned.set)

                if kind == "platform":
                    listener = robot.add_platform_listener(on_event=on_event, **config)
                else:
                    listener = robot.add_task_listener(on_event=on_event, all_tasks=kind == "global", **config)
                    if kind == "global":
                        await _global_ready(runtime)
                    await robot.invoke("robot_get_state", {}, operation_id="self-close")
                await asyncio.wait_for(returned.wait(), 1)
                await asyncio.wait_for(listener.wait(), 1)
                assert listener.closed and len(delivered) == 1 and await runtime.list_skills()


async def test_isolated_callback_can_close_all_capability_listeners() -> None:
    caller = asyncio.get_running_loop()
    for execution in ("group", "dedicated"):
        for kind in ("task", "platform", "global"):
            returned = asyncio.Event()
            config = {"execution": execution, **({"group": "all"} if execution == "group" else {})}
            async with running() as (robot, runtime):
                async def on_event(event: Event | PlatformEvent) -> None:
                    await robot.close_listeners()
                    caller.call_soon_threadsafe(returned.set)

                other = robot.add_task_listener(on_done=lambda task: None, **config)
                if kind == "platform":
                    listener = robot.add_platform_listener(on_event=on_event, **config)
                else:
                    listener = robot.add_task_listener(on_event=on_event, all_tasks=kind == "global", **config)
                    if kind == "global":
                        await _global_ready(runtime)
                    await robot.invoke("robot_get_state", {}, operation_id="close-all")
                await asyncio.wait_for(returned.wait(), 1)
                await asyncio.wait_for(listener.wait(), 1)
                assert listener.closed and other.closed and await runtime.list_skills()


async def test_error_callbacks_run_on_the_same_selected_loop_as_event_callbacks() -> None:
    locations: list[asyncio.AbstractEventLoop] = []
    errors: list[Exception] = []

    async def on_event(event: Event | PlatformEvent) -> None:
        locations.append(asyncio.get_running_loop())
        raise ValueError("callback failed")

    def task_error(task_id: str | None, error: Exception) -> None:
        locations.append(asyncio.get_running_loop())
        errors.append(error)

    async def platform_error(error: Exception) -> None:
        locations.append(asyncio.get_running_loop())
        errors.append(error)

    async with running() as (robot, runtime):
        task = robot.add_task_listener(on_event=on_event, on_error=task_error, execution="group", group="errors")
        platform = robot.add_platform_listener(on_event=on_event, on_error=platform_error,
                                               execution="group", group="errors")
        observed = await robot.invoke("robot_get_state", {}, operation_id="errors", wait=True)
        await asyncio.wait_for(asyncio.gather(task.wait(), platform.wait()), 1)
        assert (await runtime.get_task_status(observed["task_id"])).state.value == "succeeded"
    assert len(errors) == 2 and all(isinstance(error, ValueError) for error in errors)
    assert len(locations) == 4 and len(set(locations)) == 1 and asyncio.get_running_loop() not in locations


async def test_closed_blocked_callback_retains_capacity_and_does_not_block_caller_listeners() -> None:
    caller = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    workers: list[threading.Thread] = []

    async def block(event: PlatformEvent) -> None:
        workers.append(threading.current_thread())
        caller.call_soon_threadsafe(entered.set)
        assert release.wait(3)

    async with running(RuntimeOptions(max_listener_threads=1, cancel_grace_s=0.02)) as (robot, _):
        listener = robot.add_platform_listener(on_event=block, execution="dedicated")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(listener.close(), 0.5)
            with pytest.raises(ListenerCapacityExceeded):
                robot.add_task_listener(on_done=lambda task: None, execution="dedicated")
            done: list[Task] = []
            caller_listener = robot.add_task_listener(on_done=done.append)
            await robot.invoke("robot_get_state", {}, operation_id="still-responsive")
            await asyncio.wait_for(caller_listener.wait(), 1)
            assert len(done) == 1
        finally:
            release.set()
        await asyncio.to_thread(workers[0].join, 1)
        assert not workers[0].is_alive()
        replacement = robot.add_task_listener(on_done=lambda task: None, execution="dedicated")
        await replacement.close()


async def test_isolated_queue_budget_reports_full_or_expired_without_late_delivery() -> None:
    caller = asyncio.get_running_loop()
    for capacity, error_type in ((1, ListenerCallbackQueueFull), (2, ListenerCallbackTimeout)):
        entered = asyncio.Event()
        release = threading.Event()
        contexts: list[dict[str, Any]] = []
        deliveries: list[PlatformEvent] = []
        previous = caller.get_exception_handler()
        caller.set_exception_handler(lambda loop, context: contexts.append(context))

        def block(event: Event) -> None:
            caller.call_soon_threadsafe(entered.set)
            assert release.wait(3)

        try:
            async with running(RuntimeOptions(max_pending_listener_callbacks=capacity,
                                              listener_queue_timeout_s=0.02, cancel_grace_s=0.02)) as (robot, runtime):
                first = robot.add_task_listener(on_event=block, execution="group", group="busy")
                try:
                    observed = await robot.invoke("robot_get_state", {}, operation_id="busy")
                    await asyncio.wait_for(entered.wait(), 1)
                    second = robot.add_platform_listener(on_event=deliveries.append, on_error=lambda error: None,
                                                          execution="group", group="busy")
                    await asyncio.wait_for(first.close(), 0.5)
                    await asyncio.wait_for(second.wait(), 1)
                    assert not deliveries and len(contexts) == 1
                    assert isinstance(contexts[0]["exception"], error_type)
                    assert (await runtime.get_task_status(observed["task_id"])).state.value == "succeeded"
                finally:
                    release.set()
        finally:
            release.set()
            caller.set_exception_handler(previous)


async def test_cancelling_stream_close_still_cancels_its_observer_and_releases_membership() -> None:
    caller = asyncio.get_running_loop()
    entered, cleaning = asyncio.Event(), asyncio.Event()
    release: Future[None] = Future()

    async def resistant(event: PlatformEvent) -> None:
        caller.call_soon_threadsafe(entered.set)
        try:
            await asyncio.shield(asyncio.wrap_future(release))
        except asyncio.CancelledError:
            caller.call_soon_threadsafe(cleaning.set)
            await asyncio.shield(asyncio.wrap_future(release))

    async with running(RuntimeOptions(cancel_grace_s=0.1)) as (robot, _):
        listener = robot.add_platform_listener(on_event=resistant, execution="dedicated")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            closing = asyncio.create_task(listener.close())
            await asyncio.wait_for(cleaning.wait(), 1)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            await asyncio.wait_for(listener.wait(), 1)
            assert listener.closed
        finally:
            release.set_result(None)


async def test_callback_self_close_can_release_its_worker_after_the_caller_loop_exits() -> None:
    callback_closed: Future[threading.Thread] = Future()
    caller_closed: Future[None] = Future()
    release = threading.Event()
    failures: list[BaseException] = []

    def run_caller() -> None:
        async def execute() -> None:
            async with running() as (robot, _):
                async def on_done(task: Task) -> None:
                    await robot.close_listeners()
                    callback_closed.set_result(threading.current_thread())
                    assert release.wait(3)

                robot.add_task_listener(on_done=on_done, execution="dedicated")
                await robot.invoke("robot_get_state", {}, operation_id="caller-exit")
                await asyncio.wrap_future(callback_closed)

        try:
            asyncio.run(execute())
        except BaseException as error:
            failures.append(error)
        finally:
            caller_closed.set_result(None)

    caller_thread = threading.Thread(target=run_caller, daemon=True)
    caller_thread.start()
    try:
        worker = await asyncio.wait_for(asyncio.wrap_future(callback_closed), 2)
        await asyncio.wait_for(asyncio.wrap_future(caller_closed), 2)
    finally:
        release.set()
    await asyncio.to_thread(worker.join, 1)
    await asyncio.to_thread(caller_thread.join, 1)
    assert not worker.is_alive() and not caller_thread.is_alive() and not failures


async def test_naturally_finished_stream_releases_its_callback_loop_capacity() -> None:
    from test_events import EventRuntime

    runtime = EventRuntime([PlatformEvent(source="test", event_type="test.snapshot")])
    robot = await RobotCapabilities.create(runtime, listener_options=RuntimeOptions(max_listener_threads=1))
    try:
        first = robot.add_platform_listener(on_event=lambda event: None, execution="group", group="finite")
        await asyncio.wait_for(first.wait(), 1)
        assert first.closed
        replacement = robot.add_platform_listener(on_event=lambda event: None, execution="dedicated")
        await asyncio.wait_for(replacement.wait(), 1)
        assert replacement.closed
    finally:
        await robot.close_listeners()


async def test_listener_options_reject_invalid_objects_without_registering_resources() -> None:
    async with running() as (_, runtime):
        for value in (False, {}, object()):
            with pytest.raises(CapabilityConfigurationError):
                await RobotCapabilities.create(runtime, listener_options=value)


async def test_timed_out_task_keeps_isolated_completion_observation() -> None:
    caller = asyncio.get_running_loop()
    for execution in ("group", "dedicated"):
        async with running() as (_, runtime):
            robot = await RobotCapabilities.create(runtime, wait_timeout_s=0.01)
            done: list[Task] = []
            locations: list[asyncio.AbstractEventLoop] = []

            async def on_done(task: Task) -> None:
                done.append(task)
                locations.append(asyncio.get_running_loop())

            try:
                listener = robot.add_task_listener(on_done=on_done, execution=execution,
                                                   group="timeout" if execution == "group" else None)
                observed = await robot.invoke("robot_wait_delay", {"delay_s": 30}, operation_id="timeout", wait=True)
                assert observed["error"]["code"] == "TASK_WAIT_TIMEOUT"
                await runtime.cancel_task(observed["task_id"])
                await asyncio.wait_for(listener.wait(), 1)
                assert len(done) == 1 and done[0].id == observed["task_id"]
                assert done[0].state.value == "canceled" and caller not in locations
            finally:
                await robot.close_listeners()
