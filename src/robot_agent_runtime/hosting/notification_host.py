import asyncio
import threading
from collections.abc import AsyncGenerator
from contextlib import suppress

from robot_agent_runtime.errors import AgentRuntimeError, ExtensionCapacityExceeded, ExtensionTimeout, InvalidRequest, PlatformEventSourceUnavailable
from robot_agent_runtime.hosting.boundary import bounded_snapshot
from robot_agent_runtime.hosting.event_stream import HostedEventStream, _EMPTY, _END
from robot_agent_runtime.hosting.thread_runtime import LoopJob, LoopThread
from robot_agent_runtime.models import PlatformEvent
from robot_agent_runtime.notifications.service import PlatformEventService
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.ports import PlatformEventRepository, PlatformEventSource


class NotificationHost:
    """Own event observation outside the kernel and retain shutdown replay data.

    Source and repository arguments are already-isolated ports. This host owns
    only its service loop and bounded stream bridges, never vendor connections
    or the external port threads managed by KernelHost.
    """

    def __init__(self, repository: PlatformEventRepository, source: PlatformEventSource | None, options: RuntimeOptions) -> None:
        self._options = options
        self._service: PlatformEventService | None = None
        self._repository = repository
        self._source = source
        self._loop = LoopThread("robot-notifications", max_pending=options.max_subscriptions + options.max_pending_calls + 1, control_capacity=3)
        self._streams: set[HostedEventStream] = set()
        self._lock = threading.Lock()
        self._started = False
        self._closing = False
        self._close_job: LoopJob[None] | None = None
        self._collector: LoopJob[None] | None = None

    async def start(self) -> None:
        await self._loop.start()
        try:
            await self._loop.call(self._start, timeout_s=self._options.port_timeout_s, control=True)
        except BaseException:
            await self._loop.close(self._options.cancel_grace_s)
            raise

    async def _start(self) -> None:
        if self._started:
            return
        if self._closing:
            raise InvalidRequest("Notification host is closing")
        self._service = PlatformEventService(self._repository, options=self._options)
        try:
            await self._service.start()
        except BaseException:
            await self._service.close()
            raise
        with self._lock:
            self._started = True
        if self._source is not None:
            self._collector = self._loop.submit(self._collect)

    async def _collect(self) -> None:
        iterator = self._source.watch().__aiter__()
        try:
            async for value in iterator:
                await self._service.publish(value)
                if self._closing:
                    return
        except asyncio.CancelledError:
            if self._closing:
                raise
            await self._service.finish(PlatformEventSourceUnavailable(
                "Platform event source was cancelled", {"cause": "CancelledError"},
            ))
        except Exception as error:
            failure = (error if isinstance(error, AgentRuntimeError) else
                       PlatformEventSourceUnavailable("Platform event source failed", {"cause": type(error).__name__}))
            await self._service.finish(failure)
        else:
            await self._service.finish(PlatformEventSourceUnavailable("Platform event source ended"))
        finally:
            if (close := getattr(iterator, "aclose", None)) is not None:
                with suppress(Exception):
                    await close()

    async def close(self) -> None:
        with self._lock:
            self._closing = True
            if self._close_job is None:
                if not self._started:
                    job = None
                else:
                    self._close_job = self._loop.submit(self._close, control=True, cancellable=False)
                    job = self._close_job
            else:
                job = self._close_job
        try:
            if job is not None:
                await job.wait(self._options.port_timeout_s * 2 + self._options.cancel_grace_s * 3)
        finally:
            await self._loop.close(self._options.cancel_grace_s)

    async def _close(self) -> None:
        errors: list[Exception] = []
        if self._collector is not None:
            self._collector.cancel()
            with suppress(TimeoutError, asyncio.CancelledError):
                await self._collector.wait(self._options.cancel_grace_s)
        try:
            await self._service.finish()
            with self._lock:
                streams = tuple(self._streams)
            draining = asyncio.gather(*(stream.drain() for stream in streams), return_exceptions=True)
            done, _ = await asyncio.wait({draining}, timeout=self._options.port_timeout_s + self._options.cancel_grace_s)
            if draining not in done:
                for stream in streams:
                    stream.abort_close()
                draining.cancel()
                errors.append(ExtensionTimeout("Platform event streams did not finish closing"))
            else:
                errors.extend(value for value in draining.result() if isinstance(value, Exception))
        except Exception as error:
            errors.append(error)
        finally:
            try:
                await self._service.close()
            except Exception as error:
                errors.append(error)
            with self._lock:
                self._started = False
        if errors:
            raise ExceptionGroup("Notification host shutdown errors", errors)

    async def subscribe(self, after_cursor: int = 0, event_types: set[str] | None = None) -> AsyncGenerator[PlatformEvent, None]:
        if type(after_cursor) is not int or after_cursor < 0:
            raise InvalidRequest("after_cursor must be a nonnegative integer")
        if event_types is not None and (type(event_types) not in (set, frozenset) or any(type(value) is not str or not value.strip() for value in event_types)):
            raise InvalidRequest("event_types must be a set of nonempty strings")
        selected = bounded_snapshot(event_types, max_bytes=self._options.max_snapshot_bytes, max_depth=self._options.max_snapshot_depth)
        stream = HostedEventStream(lambda: self._service.subscribe(after_cursor, selected), "platform", after_cursor, self._options, cursor_attribute="cursor")
        with self._lock:
            if not self._started or self._closing:
                raise InvalidRequest("Notification host is not running")
            if len(self._streams) >= self._options.max_subscriptions:
                raise ExtensionCapacityExceeded("Platform event subscription capacity is exhausted")
            self._streams.add(stream)
        pending: LoopJob | None = None
        try:
            while True:
                value = stream.offline_next()
                if value is _EMPTY:
                    try:
                        pending = self._loop.submit(stream.pull)
                    except InvalidRequest:
                        value = stream.offline_next()
                        if value is _EMPTY:
                            stream.abort_close()
                            value = stream.offline_next()
                    else:
                        value = await pending.wait()
                        pending = None
                if value is _END:
                    return
                stream.delivered(value)
                yield value
        finally:
            if pending is not None:
                pending.cancel()
                with suppress(Exception, asyncio.CancelledError):
                    await pending.wait(self._options.cancel_grace_s)
            with self._lock:
                closing = self._closing
                self._streams.discard(stream)
            if not closing:
                with suppress(InvalidRequest, TimeoutError):
                    await self._loop.call(stream.close, timeout_s=self._options.cancel_grace_s, control=True)
