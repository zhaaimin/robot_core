import asyncio
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TypeVar

from robot_agent_runtime.errors import (
    AgentRuntimeError,
    EventStreamOverflow,
    ExtensionCapacityExceeded,
    InvalidRequest,
    RobotConnectionError,
    RobotRequestError,
    RobotResponseError,
)
from robot_agent_runtime.hosting.boundary import bounded_snapshot
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.adapters.cc_api.contracts import CcApiClient, CcApiResponse


_FINISHED = "cc.api.work.finished"
_FAULT_UPDATE = "cc.api.fault.current.update"
T = TypeVar("T")


def _consume(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


class CcApiDriver:
    """Own one SDK callback registration and multiplex it on a stable loop.

    The application supplies an already connected, exclusively assigned Api.
    Physical connection start/stop remain with that application. Requests and
    callbacks are bounded; SDK response values are detached before crossing
    threads. Disconnect, uncertain dispatch and lost events fail the connection
    closed without reconnecting or repeating a potentially physical command.
    Cancelling a dispatched request loses its authoritative response and also
    closes admission; action skills must shield launch until they can identify
    and stop that work, rather than cancel a request and blindly try again.

    Methods and subscription removals belong to the start/registration loop.
    Only SDK callback entry points accept foreign threads. Finished subscribers
    receive work IDs and decide their own action semantics; this driver never
    treats a request acknowledgement as proof of action completion.
    """

    def __init__(self, api: CcApiClient, *, options: RuntimeOptions | None = None) -> None:
        self._api = api
        self._options = options or RuntimeOptions()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._closed = False
        self._failure: AgentRuntimeError | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._pending: dict[asyncio.Future[Any], bool] = {}
        self._finished: dict[object, Callable[[str], None]] = {}
        self._faults: dict[object, Callable[[Any], None]] = {}
        self._failures: dict[object, Callable[[AgentRuntimeError], None]] = {}
        self._callback_lock = threading.Lock()
        self._accept_callbacks = False
        self._queued_callbacks = 0
        self._overflowed = False
        self._registered = False
        self._subscribe_attempted = False
        self._disconnect_registered = False
        self._fault_registered = False
        self._fault_enabled = False

    def _owner(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise InvalidRequest("CC driver methods must run on their owning loop")
        return loop

    def subscribe_finished(self, callback: Callable[[str], None]) -> Callable[[], None]:
        return self._subscribe(self._finished, callback)

    def subscribe_failure(
        self, callback: Callable[[AgentRuntimeError], None],
    ) -> Callable[[], None]:
        return self._subscribe(self._failures, callback)

    def subscribe_fault(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        return self._subscribe(self._faults, callback)

    def _subscribe(self, subscribers: dict[object, T], callback: T) -> Callable[[], None]:
        self._owner()
        if self._closed or self._failure is not None:
            raise RobotConnectionError("CC driver is unavailable")
        if (len(self._finished) + len(self._failures) + len(self._faults)
                >= self._options.max_subscriptions):
            raise ExtensionCapacityExceeded("CC driver subscription capacity is exhausted")
        token = object()
        subscribers[token] = callback

        def detach() -> None:
            self._owner()
            subscribers.pop(token, None)

        return detach

    async def start(self) -> None:
        self._owner()
        if self._closed or self._failure is not None:
            raise RobotConnectionError("CC driver cannot be restarted")
        if self._started:
            return
        self._started = True
        with self._callback_lock:
            self._accept_callbacks = True
        try:
            self._registered = True
            self._api.add_service(_FINISHED, self._on_finished)
            self._disconnect_registered = True
            self._api.set_disconnect_callback(self._on_disconnect)
            self._subscribe_attempted = True
            self._api.notify("cc.api.work.subscribe")
        except BaseException as error:
            failure = RobotConnectionError("Could not subscribe to CC API work events",
                                           details={"cause": type(error).__name__})
            self.invalidate(failure)
            try:
                await self.close()
            except Exception as cleanup:
                raise ExceptionGroup("CC driver startup and cleanup failed", [failure, cleanup]) from None
            raise failure from None

    async def start_fault_events(self) -> None:
        """Explicitly activate the optional vendor fault stream once.

        Fault updates describe physical state and never invalidate admission.
        The SDK has no fault-unsubscribe method; its callback remains owned by
        this driver until close, while subscribers detach independently.
        """
        await self.start()
        if self._fault_enabled:
            return
        try:
            self._fault_registered = True
            self._api.add_service(_FAULT_UPDATE, self._on_fault)
            self._api.notify("cc.api.fault.subscribe")
        except BaseException as error:
            failure = RobotConnectionError("Could not subscribe to CC API fault events",
                                           details={"cause": type(error).__name__})
            self.invalidate(failure)
            raise failure from None
        self._fault_enabled = True

    async def request(
        self, method: str, params: dict[str, Any], *, on_dispatch: Callable[[], None] | None = None,
        control: bool = False,
    ) -> Any:
        loop = self._owner()
        if not self._started or self._closed or self._failure is not None:
            raise RobotConnectionError("CC API connection is no longer usable")
        if type(control) is not bool:
            raise InvalidRequest("CC request control flag must be a boolean")
        capacity = self._options.max_control_calls if control else self._options.max_pending_calls
        if sum(kind == control for kind in self._pending.values()) >= capacity:
            raise ExtensionCapacityExceeded("CC driver request capacity is exhausted")
        values = bounded_snapshot(params, max_bytes=self._options.max_snapshot_bytes,
                                  max_depth=self._options.max_snapshot_depth)
        response: asyncio.Future[Any] = loop.create_future()
        response.add_done_callback(_consume)
        self._pending[response] = control

        def received(value: CcApiResponse) -> None:
            result: Any = None
            failure: AgentRuntimeError | None = None
            try:
                if type(value.is_success) is not bool:
                    raise RobotResponseError("Malformed CC API success flag")
                if value.is_success:
                    result = bounded_snapshot(value.result, max_bytes=self._options.max_snapshot_bytes,
                                              max_depth=self._options.max_snapshot_depth)
                else:
                    if type(value.error_code) is not int or type(value.error_message) is not str:
                        raise RobotResponseError("Malformed CC API rejection")
                    details = bounded_snapshot({"method": method, "vendor_code": value.error_code},
                                               max_bytes=self._options.max_snapshot_bytes,
                                               max_depth=self._options.max_snapshot_depth)
                    message = bounded_snapshot(value.error_message or "CC API rejected request",
                                               max_bytes=self._options.max_snapshot_bytes,
                                               max_depth=self._options.max_snapshot_depth)
                    failure = RobotRequestError(message, details)
            except BaseException:
                failure = RobotResponseError("Malformed or oversized CC API response", {"method": method})
            self._post(self._resolve, response, result, failure)

        try:
            try:
                if on_dispatch is not None:
                    on_dispatch()
                self._api.request(method, values, received)
            except BaseException as error:
                self.invalidate(RobotConnectionError("CC API request dispatch failed",
                                {"method": method, "cause": type(error).__name__}))
            try:
                async with asyncio.timeout(self._options.port_timeout_s):
                    return await response
            except TimeoutError:
                failure = RobotConnectionError("CC API response deadline expired", {"method": method})
                self.invalidate(failure)
                raise failure from None
        except asyncio.CancelledError:
            self.invalidate(RobotConnectionError("CC API request cancelled after dispatch",
                                                {"method": method}))
            raise
        finally:
            self._pending.pop(response, None)
            if not response.done():
                response.cancel()

    def _resolve(
        self, response: asyncio.Future[Any], result: Any, failure: AgentRuntimeError | None,
    ) -> None:
        if response.done():
            return
        if isinstance(failure, RobotResponseError):
            self.invalidate(failure)
        elif failure is not None:
            response.set_exception(failure)
        else:
            response.set_result(result)

    def invalidate(self, error: AgentRuntimeError) -> None:
        self._owner()
        if self._failure is not None:
            return
        self._failure = error
        for response in tuple(self._pending):
            if not response.done():
                response.set_exception(error)
        self._publish(self._failures, error)

    def _publish(self, subscribers: dict[object, Callable[..., None]], *args: Any) -> None:
        for token, callback in tuple(subscribers.items()):
            if token not in subscribers:
                continue
            try:
                callback(*args)
            except BaseException as error:
                self._owner().call_exception_handler({
                    "message": "CC driver subscriber failed",
                    "exception": RobotResponseError("CC subscriber callback failed",
                                                     {"cause": type(error).__name__}),
                })

    def _post(self, callback: Callable[..., None], *args: Any) -> None:
        with self._callback_lock:
            loop = self._loop
            if not self._accept_callbacks or self._overflowed or loop is None:
                return
            overflow = self._queued_callbacks >= self._options.max_event_buffer
            if overflow:
                self._overflowed = True
            else:
                self._queued_callbacks += 1
        with suppress(RuntimeError):
            if overflow:
                loop.call_soon_threadsafe(self.invalidate, EventStreamOverflow("CC callback buffer overflow"))
            else:
                loop.call_soon_threadsafe(self._deliver, callback, args)

    def _deliver(self, callback: Callable[..., None], args: tuple[Any, ...]) -> None:
        with self._callback_lock:
            self._queued_callbacks -= 1
        if not self._closed and self._failure is None:
            callback(*args)

    def _on_finished(self, request_id: Any, params: Any) -> None:
        work_id = dict.get(params, "workId") if type(params) is dict else None
        if type(work_id) is str and work_id:
            try:
                work_id = bounded_snapshot(work_id, max_bytes=self._options.max_snapshot_bytes,
                                           max_depth=self._options.max_snapshot_depth)
            except BaseException:
                self._post(self.invalidate, RobotResponseError("Invalid or oversized CC work ID"))
                return
            self._post(self._publish, self._finished, work_id)

    def _on_disconnect(self) -> None:
        self._post(self.invalidate, RobotConnectionError("CC API disconnected"))

    def _on_fault(self, request_id: Any, params: Any) -> None:
        del request_id
        try:
            payload = bounded_snapshot(params, max_bytes=self._options.max_snapshot_bytes,
                                       max_depth=self._options.max_snapshot_depth)
        except BaseException:
            self._post(self.invalidate, RobotResponseError("Invalid or oversized CC fault payload"))
            return
        self._post(self._publish_fault, payload)

    def _publish_fault(self, payload: Any) -> None:
        for token, callback in tuple(self._faults.items()):
            if token in self._faults:
                values = bounded_snapshot(payload, max_bytes=self._options.max_snapshot_bytes,
                                          max_depth=self._options.max_snapshot_depth)
                self._publish({token: callback}, values)

    async def close(self) -> None:
        self._owner()
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        self._closed = True
        with self._callback_lock:
            self._accept_callbacks = False
        self.invalidate(RobotConnectionError("CC driver closed"))
        errors: list[Exception] = []
        cleanups: list[Callable[[], None]] = []
        if self._fault_registered:
            cleanups.append(lambda: self._api.remove_service(_FAULT_UPDATE))
        if self._subscribe_attempted:
            cleanups.append(lambda: self._api.notify("cc.api.work.unsubscribe"))
        if self._registered:
            cleanups.append(lambda: self._api.remove_service(_FINISHED))
        if self._disconnect_registered:
            cleanups.append(lambda: self._api.set_disconnect_callback(lambda: None))
        for cleanup in cleanups:
            try:
                cleanup()
            except BaseException as error:
                errors.append(RobotConnectionError("Could not detach CC API callbacks",
                              {"cause": type(error).__name__}))
        self._finished.clear()
        self._faults.clear()
        self._failures.clear()
        self._started = False
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("CC driver shutdown errors", errors)
