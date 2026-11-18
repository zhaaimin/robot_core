import asyncio
import tempfile
from contextlib import aclosing
from pathlib import Path

import pytest

import robot_agent_runtime.models as models
from robot_agent_runtime.options import RuntimeOptions


def event(value: int = 1, *, event_type: str = "robot.fault.changed"):
    assert hasattr(models, "PlatformEvent"), "PlatformEvent contract is missing"
    return models.PlatformEvent(
        event_id=f"event-{value}", source="test-vendor", event_type=event_type,
        data={"faults": [value]},
    )


def test_platform_event_is_independent_of_tasks_and_crosses_snapshot_boundary() -> None:
    from robot_agent_runtime.hosting.boundary import bounded_snapshot

    original = event()
    copied = bounded_snapshot(original, max_bytes=4096, max_depth=8)
    assert copied.task_id is None and copied.cursor == 0
    copied.data["faults"].append(2)
    assert original.data == {"faults": [1]}


async def check_platform_event_repository_contract(repository) -> None:
    await repository.start()
    try:
        original = event()
        stored = await repository.append_platform_event(original)
        assert stored.cursor == 1 and original.cursor == 0
        duplicate = await repository.append_platform_event(original)
        assert duplicate.cursor == 1
        stored.data["faults"].append(2)
        page = await repository.list_platform_events(0, 1)
        assert page[0].data == {"faults": [1]}
        await repository.append_platform_event(event(2))
        assert [value.cursor for value in await repository.list_platform_events(1, 2)] == [2]
        assert await repository.platform_event_bounds() == (1, 2)
    finally:
        await repository.close()
        await repository.close()


async def test_memory_platform_event_repository_contract() -> None:
    from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository

    await check_platform_event_repository_contract(InMemoryPlatformEventRepository())


async def test_sqlite_platform_event_repository_contract_and_restart() -> None:
    from robot_agent_runtime.repositories.platform_events import SQLitePlatformEventRepository

    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "events.db")
        await check_platform_event_repository_contract(SQLitePlatformEventRepository(path))
        reopened = SQLitePlatformEventRepository(path)
        await reopened.start()
        try:
            assert [value.cursor for value in await reopened.list_platform_events(0, 10)] == [1, 2]
            assert (await reopened.append_platform_event(event(3))).cursor == 3
        finally:
            await reopened.close()


async def test_retention_reports_expired_and_future_cursors() -> None:
    from robot_agent_runtime.errors import InvalidRequest, PlatformEventCursorExpired
    from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository

    repository = InMemoryPlatformEventRepository(max_events=2)
    await repository.start()
    try:
        for number in (1, 2, 3):
            await repository.append_platform_event(event(number))
        assert await repository.platform_event_bounds() == (2, 3)
        with pytest.raises(PlatformEventCursorExpired) as caught:
            await repository.list_platform_events(0, 10)
        assert caught.value.info.details["earliest_cursor"] == 2
        assert [value.cursor for value in await repository.list_platform_events(1, 10)] == [2, 3]
        with pytest.raises(InvalidRequest):
            await repository.list_platform_events(4, 10)
    finally:
        await repository.close()


async def test_platform_service_replays_then_live_without_mutation_or_duplicate() -> None:
    from robot_agent_runtime.notifications.service import PlatformEventService
    from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository

    service = PlatformEventService(InMemoryPlatformEventRepository())
    await service.start()
    try:
        original = event(1)
        await service.publish(original)
        async with aclosing(service.subscribe()) as stream:
            first = await anext(stream)
            assert first.cursor == 1
            first.data["faults"].append(99)
            await service.publish(original)
            await service.publish(event(2))
            assert (await anext(stream)).cursor == 2
        async with aclosing(service.subscribe(after_cursor=1)) as replay:
            assert (await anext(replay)).data == {"faults": [2]}
    finally:
        await service.close()


async def test_platform_service_filters_without_renumbering() -> None:
    from robot_agent_runtime.notifications.service import PlatformEventService
    from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository

    service = PlatformEventService(InMemoryPlatformEventRepository())
    await service.start()
    try:
        await service.publish(event(1, event_type="vendor.other"))
        await service.publish(event(2))
        async with aclosing(service.subscribe(event_types={"robot.fault.changed"})) as stream:
            assert (await anext(stream)).cursor == 2
    finally:
        await service.close()


async def test_platform_service_slow_subscriber_gets_recoverable_overflow() -> None:
    from robot_agent_runtime.errors import EventStreamOverflow
    from robot_agent_runtime.notifications.service import PlatformEventService
    from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository

    service = PlatformEventService(InMemoryPlatformEventRepository(), options=RuntimeOptions(max_event_buffer=1))
    await service.start()
    try:
        await service.publish(event(1))
        async with aclosing(service.subscribe()) as slow:
            assert (await anext(slow)).cursor == 1
            await service.publish(event(2))
            await service.publish(event(3))
            with pytest.raises(EventStreamOverflow) as caught:
                await anext(slow)
            assert caught.value.info.details["after_cursor"] == 1
        async with aclosing(service.subscribe(after_cursor=1)) as recovered:
            assert [(await anext(recovered)).cursor, (await anext(recovered)).cursor] == [2, 3]
    finally:
        await service.close()


async def test_platform_service_source_failure_is_visible_after_saved_events() -> None:
    from robot_agent_runtime.errors import PlatformEventSourceUnavailable
    from robot_agent_runtime.notifications.service import PlatformEventService
    from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository

    service = PlatformEventService(InMemoryPlatformEventRepository())
    await service.start()
    try:
        await service.publish(event(1))
        await service.finish(PlatformEventSourceUnavailable("source disconnected"))
        async with aclosing(service.subscribe()) as stream:
            assert (await anext(stream)).cursor == 1
            with pytest.raises(PlatformEventSourceUnavailable):
                await anext(stream)
    finally:
        await service.close()


async def test_replay_overflow_reports_last_event_actually_delivered() -> None:
    from robot_agent_runtime.errors import EventStreamOverflow
    from robot_agent_runtime.notifications.service import PlatformEventService
    from robot_agent_runtime.repositories.platform_events import InMemoryPlatformEventRepository

    service = PlatformEventService(InMemoryPlatformEventRepository(), options=RuntimeOptions(event_page_size=2, max_event_buffer=1))
    await service.start()
    try:
        await service.publish(event(1))
        await service.publish(event(2))
        async with aclosing(service.subscribe()) as stream:
            delivered = (await anext(stream)).cursor
            await service.publish(event(3))
            await service.publish(event(4))
            with pytest.raises(EventStreamOverflow) as caught:
                while True:
                    delivered = (await anext(stream)).cursor
            assert caught.value.info.details["after_cursor"] == delivered
    finally:
        await service.close()
