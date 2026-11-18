"""Delivery ordering, bounds and lifecycle of optional notification outputs."""

import asyncio
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from robot_agent_runtime.errors import EventStreamOverflow, ExtensionCapacityExceeded
from robot_agent_runtime.models import Event, PlatformEvent


def _notifier_module():
    from robot_agent_runtime.notifications import notifier

    return notifier


def _event(sequence: int, *, task_id: str = "t") -> Event:
    return Event(
        event_id=f"{task_id}-{sequence}", task_id=task_id,
        sequence=sequence, event_type="progress",
    )


class RecordingChannel:
    def __init__(self, expected: int = 1, *, blocked: bool = False) -> None:
        self.expected = expected
        self.active = 0
        self.max_active = 0
        self.events: list[Event | PlatformEvent] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.exited = asyncio.Event()
        self.all_done = asyncio.Event()
        self.closed = asyncio.Event()
        if not blocked:
            self.release.set()

    async def deliver(self, event: Event | PlatformEvent) -> bool:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.events.append(event)
        self.entered.set()
        try:
            await self.release.wait()
            if len(self.events) >= self.expected:
                self.all_done.set()
            return True
        finally:
            self.active -= 1
            self.exited.set()

    async def close(self) -> None:
        self.closed.set()


async def test_deliveries_to_one_endpoint_are_serialized_and_ack_is_monotonic() -> None:
    module = _notifier_module()
    channel = RecordingChannel(expected=3, blocked=True)
    notifier = module.EventNotifier(retry_delays_s=())
    endpoint = module.WebhookEndpoint(url="http://callback.invalid")
    notifier.add_webhook("subscriber", endpoint, channel=channel)
    await notifier.start()
    try:
        for sequence in (1, 2, 3):
            notifier.publish(_event(sequence))
        await asyncio.wait_for(channel.entered.wait(), 1)
        channel.release.set()
        await asyncio.wait_for(channel.all_done.wait(), 1)
        assert channel.max_active == 1
        assert [value.sequence for value in channel.events] == [1, 2, 3]
        assert endpoint.last_acked_sequence == 3
    finally:
        await notifier.stop()


async def test_stop_reclaims_inflight_delivery() -> None:
    module = _notifier_module()
    channel = RecordingChannel(blocked=True)
    notifier = module.EventNotifier()
    notifier.add_subscriber("subscriber", channel)
    await notifier.start()
    notifier.publish(_event(1))
    await asyncio.wait_for(channel.entered.wait(), 1)
    async with asyncio.timeout(1):
        await notifier.stop()
    assert channel.exited.is_set() and channel.closed.is_set()


async def test_readding_webhook_replaces_endpoint_without_split_state() -> None:
    module = _notifier_module()
    stale_channel = RecordingChannel(blocked=True)
    current_channel = RecordingChannel()
    notifier = module.EventNotifier()
    stale = module.WebhookEndpoint(url="http://stale.invalid")
    notifier.add_webhook("subscriber", stale, channel=stale_channel)
    await notifier.start()
    try:
        notifier.publish(_event(1))
        await asyncio.wait_for(stale_channel.entered.wait(), 1)
        current = module.WebhookEndpoint(url="http://callback.invalid")
        notifier.add_webhook("subscriber", current, channel=current_channel)
        notifier.publish(_event(2))
        await asyncio.wait_for(current_channel.all_done.wait(), 1)
        await asyncio.wait_for(stale_channel.closed.wait(), 1)
        assert [value.sequence for value in current_channel.events] == [2]
        assert stale.last_acked_sequence == 0
        assert current.last_acked_sequence == 2
    finally:
        await notifier.stop()


async def test_two_tasks_with_sequence_one_are_both_delivered() -> None:
    module = _notifier_module()
    channel = RecordingChannel(expected=2)
    notifier = module.EventNotifier()
    notifier.add_subscriber("subscriber", channel)
    await notifier.start()
    try:
        notifier.publish(_event(1, task_id="one"))
        await asyncio.wait_for(channel.entered.wait(), 1)
        notifier.publish(_event(1, task_id="two"))
        await asyncio.wait_for(channel.all_done.wait(), 1)
        assert [value.task_id for value in channel.events] == ["one", "two"]
    finally:
        await notifier.stop()


async def test_duplicate_already_queued_or_acked_is_not_delivered_twice() -> None:
    module = _notifier_module()
    channel = RecordingChannel(expected=2, blocked=True)
    notifier = module.EventNotifier()
    notifier.add_subscriber("subscriber", channel)
    await notifier.start()
    try:
        notifier.publish(_event(1))
        await asyncio.wait_for(channel.entered.wait(), 1)
        notifier.publish(_event(1))
        notifier.publish(_event(2))
        channel.release.set()
        await asyncio.wait_for(channel.all_done.wait(), 1)
        notifier.publish(_event(1))
        notifier.publish(_event(3))
        channel.expected = 3
        channel.all_done.clear()
        await asyncio.wait_for(channel.all_done.wait(), 1)
        assert [value.event_id for value in channel.events] == ["t-1", "t-2", "t-3"]
    finally:
        await notifier.stop()


async def test_generic_channel_needs_no_url_and_accepts_platform_events() -> None:
    module = _notifier_module()
    channel = RecordingChannel()
    notifier = module.EventNotifier()
    notifier.add_subscriber("console", channel)
    await notifier.start()
    try:
        value = PlatformEvent(source="vendor", event_type="device.fault", cursor=7)
        notifier.publish(value)
        await asyncio.wait_for(channel.all_done.wait(), 1)
        assert channel.events == [value]
    finally:
        await notifier.stop()


async def test_input_queue_overflow_is_observable() -> None:
    module = _notifier_module()
    notifier = module.EventNotifier(queue_size=1)
    await notifier.start()
    try:
        notifier.publish(_event(1))
        with pytest.raises(EventStreamOverflow) as caught:
            notifier.publish(_event(2))
        assert caught.value.info.details["event_id"] == "t-2"
    finally:
        await notifier.stop()


async def test_subscriber_overflow_is_observable_and_other_subscribers_continue() -> None:
    module = _notifier_module()
    blocked = RecordingChannel(blocked=True)
    healthy = RecordingChannel(expected=2)
    notifier = module.EventNotifier(endpoint_queue_size=1)
    notifier.add_subscriber("blocked", blocked)
    notifier.add_subscriber("healthy", healthy)
    await notifier.start()
    try:
        notifier.publish(_event(1))
        await asyncio.wait_for(blocked.entered.wait(), 1)
        notifier.publish(_event(2))
        await asyncio.wait_for(healthy.all_done.wait(), 1)
        healthy.expected = 3
        healthy.all_done.clear()
        notifier.publish(_event(3))
        await asyncio.wait_for(healthy.all_done.wait(), 1)
        error = notifier.subscriber_error("blocked")
        assert isinstance(error, EventStreamOverflow)
        assert error.info.details["subscriber_id"] == "blocked"
        assert notifier.subscriber_error("healthy") is None
    finally:
        await notifier.stop()


async def test_subscriber_capacity_includes_cancel_resistant_retired_worker() -> None:
    module = _notifier_module()

    class Resistant(RecordingChannel):
        async def deliver(self, event: Event | PlatformEvent) -> bool:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()
            finally:
                self.exited.set()
            return True

    channel = Resistant(blocked=True)
    notifier = module.EventNotifier(max_subscribers=1, drain_timeout_s=0.01)
    notifier.add_subscriber("one", channel)
    await notifier.start()
    notifier.publish(_event(1))
    await asyncio.wait_for(channel.entered.wait(), 1)
    notifier.remove_subscriber("one")
    try:
        with pytest.raises(ExtensionCapacityExceeded):
            notifier.add_subscriber("two", RecordingChannel())
        async with asyncio.timeout(0.5):
            await notifier.stop()
    finally:
        channel.release.set()
        await asyncio.wait_for(channel.exited.wait(), 1)


async def test_total_subscriber_bound_is_enforced_before_workers_start() -> None:
    module = _notifier_module()
    notifier = module.EventNotifier(max_subscribers=1)
    notifier.add_subscriber("one", RecordingChannel())
    with pytest.raises(ExtensionCapacityExceeded):
        notifier.add_subscriber("two", RecordingChannel())
    await notifier.stop()


async def test_channel_failure_is_visible_without_stopping_other_subscribers() -> None:
    module = _notifier_module()

    class Broken(RecordingChannel):
        async def deliver(self, event: Event | PlatformEvent) -> bool:
            raise OSError("dead endpoint")

    broken = Broken()
    healthy = RecordingChannel()
    notifier = module.EventNotifier(retry_delays_s=())
    notifier.add_subscriber("broken", broken)
    notifier.add_subscriber("healthy", healthy)
    await notifier.start()
    try:
        notifier.publish(_event(1))
        await asyncio.wait_for(healthy.all_done.wait(), 1)
        assert notifier.subscriber_error("broken") is not None
        assert [value.event_id for value in healthy.events] == ["t-1"]
    finally:
        await notifier.stop()


async def test_channel_close_failure_does_not_skip_other_channel_cleanup() -> None:
    module = _notifier_module()

    class BadClose(RecordingChannel):
        async def close(self) -> None:
            raise OSError("close failed")

    healthy = RecordingChannel()
    notifier = module.EventNotifier()
    notifier.add_subscriber("broken", BadClose())
    notifier.add_subscriber("healthy", healthy)
    await notifier.start()
    await notifier.stop()
    assert healthy.closed.is_set()


async def test_consume_owns_and_closes_source_on_overflow() -> None:
    module = _notifier_module()
    closed = asyncio.Event()

    async def events():
        try:
            yield _event(1)
            yield _event(2)
        finally:
            closed.set()

    notifier = module.EventNotifier(queue_size=1)
    await notifier.start()
    try:
        with pytest.raises(EventStreamOverflow):
            await notifier.consume(events())
        assert closed.is_set()
    finally:
        await notifier.stop()


async def test_webhook_delivery_does_not_occupy_default_executor() -> None:
    module = _notifier_module()
    loop = asyncio.get_running_loop()
    default = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(default)
    started = threading.Event()
    blocked = threading.Event()
    original = urllib.request.urlopen

    def fake_urlopen(request, timeout=None):
        started.set()
        blocked.wait(5)
        raise OSError("blocked test endpoint")

    urllib.request.urlopen = fake_urlopen
    channel = module.WebhookChannel(module.WebhookEndpoint(url="http://callback.invalid"))
    delivery = asyncio.create_task(channel.deliver(_event(1)))
    try:
        async with asyncio.timeout(1):
            while not started.is_set():
                await asyncio.sleep(0)
        marker: list[int] = []
        await asyncio.wait_for(loop.run_in_executor(None, lambda: marker.append(1)), 1)
        assert marker == [1]
    finally:
        blocked.set()
        urllib.request.urlopen = original
        await asyncio.wait_for(delivery, 2)
        default.shutdown(wait=False)
        await channel.close()


async def test_webhook_response_is_closed_and_executor_submissions_are_bounded() -> None:
    module = _notifier_module()
    entered = threading.Event()
    release = threading.Event()
    response_closed = threading.Event()
    original = urllib.request.urlopen
    original_executor = module.ThreadPoolExecutor
    calls: list[int] = []
    submissions: list[int] = []

    class CountingExecutor(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            submissions.append(1)
            return super().submit(*args, **kwargs)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            response_closed.set()

    def fake_urlopen(request, timeout=None):
        calls.append(1)
        entered.set()
        release.wait(5)
        return Response()

    urllib.request.urlopen = fake_urlopen
    module.ThreadPoolExecutor = CountingExecutor
    channel = module.WebhookChannel(
        module.WebhookEndpoint(url="http://callback.invalid"), max_workers=1,
    )
    first = asyncio.create_task(channel.deliver(_event(1)))
    second = None
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second_entered = asyncio.Event()

        async def deliver_second() -> bool:
            second_entered.set()
            return await channel.deliver(_event(2))

        second = asyncio.create_task(deliver_second())
        await asyncio.wait_for(second_entered.wait(), 1)
        assert submissions == [1]
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        async with asyncio.timeout(1):
            while not response_closed.is_set():
                await asyncio.sleep(0)
        assert calls == [1]
    finally:
        release.set()
        urllib.request.urlopen = original
        module.ThreadPoolExecutor = original_executor
        await channel.close()


async def test_retired_worker_releases_capacity_only_after_delivery_really_finishes() -> None:
    module = _notifier_module()

    class Resistant(RecordingChannel):
        async def deliver(self, event: Event | PlatformEvent) -> bool:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()
            self.exited.set()
            return True

    channel = Resistant(blocked=True)
    notifier = module.EventNotifier(max_subscribers=1)
    notifier.add_subscriber("one", channel)
    await notifier.start()
    try:
        notifier.publish(_event(1))
        await asyncio.wait_for(channel.entered.wait(), 1)
        notifier.remove_subscriber("one")
        await asyncio.wait_for(channel.closed.wait(), 1)
        channel.release.set()
        await asyncio.wait_for(channel.exited.wait(), 1)
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        notifier.add_subscriber("two", RecordingChannel())
        assert notifier.subscriber_count == 1
    finally:
        channel.release.set()
        await notifier.stop()


async def test_stop_is_bounded_when_channel_close_repeatedly_ignores_cancellation() -> None:
    module = _notifier_module()

    class ResistantClose(RecordingChannel):
        async def close(self) -> None:
            self.entered.set()
            try:
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
            finally:
                self.closed.set()

    channel = ResistantClose(blocked=True)
    notifier = module.EventNotifier(drain_timeout_s=0.01)
    notifier.add_subscriber("one", channel)
    await notifier.start()
    try:
        async with asyncio.timeout(0.5):
            await notifier.stop()
        assert channel.entered.is_set() and not channel.closed.is_set()
    finally:
        channel.release.set()
        await asyncio.wait_for(channel.closed.wait(), 1)
