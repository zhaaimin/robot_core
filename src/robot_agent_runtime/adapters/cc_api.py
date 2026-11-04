import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from robot_agent_runtime.errors import (
    AgentRuntimeError,
    InvalidRequest,
    InternalError,
    RobotConnectionError,
    RobotRequestError,
    RobotResponseError,
    RobotStopError,
    TaskCancelled,
)
from robot_agent_runtime.models import SkillContext, SkillDefinition, SkillResult
from robot_agent_runtime.ports import CallbackApiResponse, CallbackRobotApi


_FINISHED = "cc.api.work.finished"
_START = "cc.api.work.start"
_CONTROL = "cc.api.work.ctrl"
_MAX_EARLY_EVENTS = 128


@dataclass
class _Speech:
    finished: asyncio.Future[None]
    dispatched: bool = False
    work_id: str | None = None
    early_finished: set[str] = field(default_factory=set)
    stop_task: asyncio.Task[None] | None = None


class CcApiSpeechSkill:
    """Speech on a dedicated, already connected CC API callback transport.

    A matching work.finished means success regardless of its payload, per the
    initial integration contract. A local cancel still stops this exact work
    and waits for finished before acknowledging cancellation. The runtime owns
    the speaker lock, validation and execution deadline; this adapter only
    bounds stop acknowledgement and marshals vendor callbacks onto asyncio.

    Lost connections or uncertain work ownership poison the instance: callers
    must establish that the old speech has stopped before creating another.
    Automatic reconnect/retry could otherwise overlap physical playback.
    Start before the runtime and close after it; SDK connection lifecycle stays
    with the application. Importing this module never imports the native SDK.
    """

    def __init__(
        self, api: CallbackRobotApi, *, stop_timeout_s: float = 5.0
    ) -> None:
        if not 0 < stop_timeout_s < float("inf"):
            raise InvalidRequest("stop_timeout_s must be finite and positive")
        self.definition = SkillDefinition(
            name="audio.speak",
            version="1.0.0",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string", "minLength": 1}},
                "required": ["message"],
                "additionalProperties": False,
            },
            resources={"speaker"},
            permissions={"audio"},
            preconditions=set(),
            timeout_s=30.0,
            supports_cancel=True,
        )
        self._api = api
        self._stop_timeout_s = stop_timeout_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._closed = False
        self._broken = False
        self._speech: _Speech | None = None
        self._owner: asyncio.Task[Any] | None = None
        self._pending: set[asyncio.Future[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._closed or self._broken:
            raise RobotConnectionError("Speech adapter cannot be restarted")
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._started = True
        try:
            self._api.add_service(_FINISHED, self._on_finished)
            self._api.set_disconnect_callback(self._on_disconnect)
            self._api.notify("cc.api.work.subscribe")
        except Exception as error:
            self._started = False
            self._broken = True
            with suppress(Exception):
                self._api.remove_service(_FINISHED)
            with suppress(Exception):
                self._api.set_disconnect_callback(lambda: None)
            raise RobotConnectionError("Could not subscribe to CC API work events") from error

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        self._closed = True
        errors: list[Exception] = []
        owner = self._owner
        if owner is not None and not owner.done():
            owner.cancel()
            results = await asyncio.gather(owner, return_exceptions=True)
            errors.extend(error for error in results
                          if isinstance(error, Exception) and not isinstance(error, TaskCancelled))
        if not self._started:
            return
        for cleanup in (
            lambda: self._api.notify("cc.api.work.unsubscribe"),
            lambda: self._api.remove_service(_FINISHED),
            lambda: self._api.set_disconnect_callback(lambda: None),
        ):
            try:
                cleanup()
            except Exception as error:
                errors.append(RobotConnectionError(
                    "Could not detach CC API speech callbacks",
                    details={"cause": type(error).__name__},
                ))
        self._started = False
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("CC API speech shutdown errors", errors)

    async def execute(
        self, context: SkillContext, params: dict[str, Any]
    ) -> SkillResult:
        loop = self._loop
        if loop is not None and loop is not asyncio.get_running_loop():
            return await self._execute_on_owner(loop, context, params)
        return await self._execute(context, params)

    async def _execute_on_owner(
        self, loop: asyncio.AbstractEventLoop, context: SkillContext, params: dict[str, Any]
    ) -> SkillResult:
        """Keep vendor futures and callbacks on the application-owned loop.

        The handoff future settles only after the owner task really exits.
        Cancelling its waiter requests owner cancellation without falsely
        acknowledging that the robot has stopped.
        """
        completed: Future[SkillResult] = Future()
        owner_task: asyncio.Task[None] | None = None
        owner_cancel: asyncio.Event | None = None
        cancel_requested = False
        values = deepcopy(params)
        task_id, execution_id = context.task_id, context.execution_id
        worker_loop = asyncio.get_running_loop()
        progress = context.progress

        def report(value: float) -> None:
            if progress is not None:
                with suppress(RuntimeError):
                    worker_loop.call_soon_threadsafe(progress, value)

        async def run() -> None:
            nonlocal owner_cancel
            owner_cancel = asyncio.Event()
            if cancel_requested:
                owner_cancel.set()
            try:
                result = await self._execute(
                    SkillContext(task_id, execution_id, owner_cancel,
                                 report if progress is not None else None), values
                )
            except asyncio.CancelledError:
                completed.cancel()
            except Exception as error:
                completed.set_exception(error)
            except BaseException as error:
                completed.set_exception(InternalError(
                    "CC API owner raised an unsafe exception",
                    details={"cause": type(error).__name__},
                ))
            else:
                completed.set_result(deepcopy(result))

        def launch() -> None:
            nonlocal owner_task
            owner_task = asyncio.create_task(run())
            owner_task.add_done_callback(settled)

        def settled(finished: asyncio.Task[None]) -> None:
            if completed.done():
                return
            try:
                finished.result()
            except asyncio.CancelledError:
                completed.cancel()
            except BaseException as error:
                completed.set_exception(InternalError(
                    "CC API owner failed during handoff",
                    details={"cause": type(error).__name__},
                ))

        def signal(*, cancel_task: bool = False) -> None:
            nonlocal cancel_requested
            cancel_requested = True
            if owner_cancel is not None:
                owner_cancel.set()
            if cancel_task and owner_task is not None:
                owner_task.cancel()

        async def forward_cancel() -> None:
            await context.cancel_event.wait()
            loop.call_soon_threadsafe(signal)

        loop.call_soon_threadsafe(launch)
        waiter = asyncio.wrap_future(completed)
        forwarder = asyncio.create_task(forward_cancel())
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            loop.call_soon_threadsafe(lambda: signal(cancel_task=True))
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    continue
            return waiter.result()
        finally:
            forwarder.cancel()
            cleanup = asyncio.gather(forwarder, return_exceptions=True)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue

    async def _execute(
        self, context: SkillContext, params: dict[str, Any]
    ) -> SkillResult:
        if not self._started or self._closed or self._broken:
            raise RobotConnectionError("Speech adapter is unavailable; check robot work before reconnecting")
        if self._speech is not None:
            raise InvalidRequest("Run speech through the runtime's speaker resource lock")
        if context.cancel_event.is_set():
            raise TaskCancelled("Speech cancelled before dispatch")
        speech = _Speech(self._future())
        self._speech = speech
        self._owner = asyncio.current_task()
        launch = asyncio.create_task(self._launch(speech, params["message"]))
        cancelled = asyncio.create_task(context.cancel_event.wait())
        try:
            done, _ = await asyncio.wait({launch, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                await self._stop(speech, launch)
                raise TaskCancelled("Speech cancelled and finished")
            await launch
            done, _ = await asyncio.wait(
                {speech.finished, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancelled in done or context.cancel_event.is_set():
                await self._stop(speech, launch)
                raise TaskCancelled("Speech cancelled and finished")
            await speech.finished
            return SkillResult(success=True, output={
                "message": params["message"], "spoken": True, "work_id": speech.work_id,
            })
        except asyncio.CancelledError:
            # The runtime deadline/shutdown must not leave playback running.
            # A repeated cancellation cannot detach the stop acknowledgement.
            cleanup = asyncio.create_task(self._stop(speech, launch))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            await cleanup
            raise TaskCancelled("Speech cancellation confirmed by finished notification") from None
        except RobotStopError:
            raise
        except Exception as error:
            rejected_start = isinstance(error, RobotRequestError) and speech.work_id is None
            finished = (speech.finished.done() and not speech.finished.cancelled()
                        and speech.finished.exception() is None)
            if speech.dispatched and not rejected_start and not finished:
                await self._stop(speech, launch)
            raise
        finally:
            cancelled.cancel()
            if not launch.done():
                launch.cancel()
            await asyncio.gather(cancelled, launch, return_exceptions=True)
            if not speech.finished.done():
                speech.finished.cancel()
            self._speech = None
            self._owner = None

    async def _launch(self, speech: _Speech, message: str) -> None:
        result = await self._request(_START, {
            "skillCode": "A000001",
            "params": {"text": message, "speed": 50, "volume": 50},
            "traceId": uuid4().hex,
        })
        work_id = result.get("workId") if isinstance(result, dict) else None
        if not isinstance(work_id, str) or not work_id:
            self._broken = True
            raise RobotResponseError("CC API start response did not contain a workId")
        speech.work_id = work_id
        if work_id in speech.early_finished and not speech.finished.done():
            speech.finished.set_result(None)
        speech.early_finished.clear()

    async def _stop(self, speech: _Speech, launch: asyncio.Task[None]) -> None:
        if speech.stop_task is None:
            speech.stop_task = asyncio.create_task(self._stop_work(speech, launch))
        await asyncio.shield(speech.stop_task)

    async def _stop_work(self, speech: _Speech, launch: asyncio.Task[None]) -> None:
        try:
            async with asyncio.timeout(self._stop_timeout_s):
                try:
                    await asyncio.shield(launch)
                except RobotRequestError:
                    return  # A rejected start has no physical work to stop.
                if speech.finished.done():
                    await speech.finished
                    return
                await self._request(_CONTROL, {"workId": speech.work_id, "workCtrl": "stop"})
                await asyncio.shield(speech.finished)
        except (Exception, asyncio.CancelledError) as error:
            self._broken = True
            raise RobotStopError(
                "Could not confirm that speech finished; check the robot before reconnecting",
                details={"work_id": speech.work_id, "cause": type(error).__name__},
            ) from error

    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        if self._broken:
            raise RobotConnectionError("CC API connection is no longer usable")
        response = self._future()

        def received(value: CallbackApiResponse) -> None:
            self._post(self._resolve_response, response, value)

        try:
            if method == _START and self._speech is not None:
                self._speech.dispatched = True
            self._api.request(method, params, received)
        except BaseException as error:
            self._broken = True
            response.cancel()
            raise RobotConnectionError("CC API request dispatch failed", details={"method": method}) from error
        value = await response
        try:
            if not value.is_success:
                raise RobotRequestError(
                    value.error_message or "CC API rejected request",
                    details={"method": method, "vendor_code": value.error_code},
                )
            return value.result
        except RobotRequestError:
            raise
        except BaseException as error:
            self._broken = True
            raise RobotResponseError("Malformed CC API response", details={"method": method}) from error

    def _future(self) -> asyncio.Future[Any]:
        future = asyncio.get_running_loop().create_future()
        self._pending.add(future)
        future.add_done_callback(self._settled)
        return future

    def _settled(self, future: asyncio.Future[Any]) -> None:
        self._pending.discard(future)
        if not future.cancelled():
            future.exception()

    def _post(self, callback: Callable[..., None], *args: Any) -> None:
        loop = self._loop
        if self._started and loop is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(callback, *args)

    @staticmethod
    def _resolve_response(future: asyncio.Future[Any], value: CallbackApiResponse) -> None:
        if not future.done():
            future.set_result(value)

    def _on_finished(self, request_id: Any, params: Any) -> None:
        work_id = params.get("workId") if isinstance(params, dict) else None
        if isinstance(work_id, str) and work_id:
            self._post(self._finished, work_id)

    def _finished(self, work_id: str) -> None:
        speech = self._speech
        if speech is None:
            return
        if speech.work_id == work_id and not speech.finished.done():
            speech.finished.set_result(None)
        elif speech.work_id is None:
            if len(speech.early_finished) >= _MAX_EARLY_EVENTS:
                self._abort(RobotResponseError("Too many work events before start acknowledgement"))
            else:
                speech.early_finished.add(work_id)

    def _on_disconnect(self) -> None:
        self._post(self._abort, RobotConnectionError("CC API disconnected during speech"))

    def _abort(self, error: AgentRuntimeError) -> None:
        self._broken = True
        for future in tuple(self._pending):
            if not future.done():
                future.set_exception(error)
