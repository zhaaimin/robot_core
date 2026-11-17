import asyncio
from concurrent.futures import Future
from copy import deepcopy
from typing import Any

from robot_agent_runtime.errors import (
    InvalidRequest,
    RobotConnectionError,
)
from robot_agent_runtime.models import ActionOutcome, SkillContext, SkillDefinition, SkillResult
from robot_agent_runtime.adapters.cc_api.contracts import CcApiClient
from robot_agent_runtime.adapters.cc_api.driver import CcApiDriver


_STATUS_GET = "cc.api.status.get"
_STATUS_LIST = "cc.api.status.list"

# An empty request asks the vendor for every status item it currently reports,
# rather than guessing IDs that may not exist on this firmware.
_DEFAULT_STATUS_IDS: tuple[str, ...] = ()


def _extract_status_ids(listing: Any) -> list[str]:
    """Pull the status item IDs out of a ``cc.api.status.list`` payload.

    The vendor returns ``{"list": [id, ...]}`` (possibly as a JSON string) or a
    bare list. Each entry may be a plain string or a mapping carrying the id.
    Anything unrecognised yields an empty list so callers can fall back safely.
    """
    payload: Any = listing
    if isinstance(payload, str):
        import json

        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if isinstance(payload, dict):
        payload = payload.get("list", payload)
    if not isinstance(payload, list):
        return []
    ids: list[str] = []
    for entry in payload:
        if isinstance(entry, str) and entry:
            ids.append(entry)
        elif isinstance(entry, dict):
            value = entry.get("id") or entry.get("name") or entry.get("status_id")
            if isinstance(value, str) and value:
                ids.append(value)
    return ids


class CcApiRobotInfoSkill:
    """Read-only robot status query on an owned or shared communication driver.

    This skill never changes robot state: it issues a single ``cc.api.status.get``
    request and returns the vendor payload unchanged. There is no work lifecycle,
    no stop acknowledgement and no cancel support, so it cannot leave physical
    playback running behind it. It shares the driver with ``CcApiSpeechSkill``
    when the platform owns both; closing this skill removes only its own driver
    ownership (it holds no event subscriptions of its own).

    Like the speech skill, the shared driver requires every request on its
    owning loop. When the runtime isolates this skill onto a different loop,
    ``execute`` hands the request back to the owner loop and waits.
    """

    def __init__(self, api: CcApiClient | CcApiDriver) -> None:
        self.definition = SkillDefinition(
            name="robot.status",
            version="1.0.0",
            description="查询机器人当前状态（系统状态、工作模式、舵机/GPU 温度、腿部状态等）；只读，不改变机器人行为。",
            input_schema={
                "type": "object",
                "properties": {
                    "status_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "description": "要查询的状态项 ID 列表；留空表示查询全部。可选值：state / work_mode / servos_temperature / gpu_temperature / leg_status / navState。",
                    },
                },
                "additionalProperties": False,
            },
            resources=set(),
            permissions={"robotinfo"},
            preconditions=set(),
            timeout_s=10.0,
            supports_cancel=False,
        )
        self._owns_driver = not isinstance(api, CcApiDriver)
        self._driver = api if isinstance(api, CcApiDriver) else CcApiDriver(api)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RobotConnectionError("Robot info adapter cannot be restarted")
        if self._started:
            return
        try:
            await self._driver.start()
        except Exception:
            raise
        self._loop = asyncio.get_running_loop()
        self._started = True

    async def close(self) -> None:
        self._closed = True
        if self._owns_driver:
            await self._driver.close()
        self._started = False

    async def execute(
        self, context: SkillContext, params: dict[str, Any]
    ) -> SkillResult:
        del context  # A read-only query has no cancel/progress semantics.
        if not self._started or self._closed:
            raise RobotConnectionError("Robot info adapter is unavailable")
        status_ids = params.get("status_ids")
        if status_ids is None:
            # Query everything the vendor reports; pass an empty list so the
            # vendor returns its full status set instead of a guessed subset.
            requested: list[str] = list(_DEFAULT_STATUS_IDS)
        elif isinstance(status_ids, list) and all(isinstance(s, str) and s for s in status_ids):
            requested = deepcopy(status_ids)
        else:
            raise InvalidRequest("status_ids must be a list of nonempty strings")

        owner = self._loop
        if owner is not None and owner is not asyncio.get_running_loop():
            return await self._execute_on_owner(owner, requested)
        return await self._execute(requested)

    async def _execute_on_owner(
        self, owner: asyncio.AbstractEventLoop, requested: list[str],
    ) -> SkillResult:
        completed: Future[SkillResult] = Future()

        def run() -> None:
            if completed.done():
                return
            try:
                result = owner.create_task(self._execute(requested))
            except BaseException as error:
                completed.set_exception(error)
                return

            def settled(finished: asyncio.Task[Any]) -> None:
                if completed.done():
                    return
                try:
                    completed.set_result(finished.result())
                except BaseException as error:
                    completed.set_exception(error)

            result.add_done_callback(settled)

        owner.call_soon_threadsafe(run)
        return await asyncio.wrap_future(completed)

    async def _execute(self, requested: list[str]) -> SkillResult:
        if not requested:
            # No IDs supplied: discover the vendor's real status items first,
            # then query each one, instead of guessing names this firmware may
            # not support (which the vendor rejects with an unsupported-id error).
            listing = await self._driver.request(_STATUS_LIST, {})
            requested = _extract_status_ids(listing)
        result = await self._driver.request(_STATUS_GET, {"list": requested})
        return SkillResult(
            success=True,
            output={"status_ids": requested, "status": result},
            action=ActionOutcome(executed="none", stopped=True),
        )

