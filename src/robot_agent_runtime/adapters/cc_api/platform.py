import asyncio
from collections.abc import Mapping
from types import MappingProxyType

from robot_agent_runtime.adapters.cc_api.driver import CcApiDriver
from robot_agent_runtime.adapters.cc_api.contracts import CcApiClient
from robot_agent_runtime.adapters.cc_api.speech import CcApiSpeechSkill
from robot_agent_runtime.adapters.cc_api.robotinfo import CcApiRobotInfoSkill
from robot_agent_runtime.errors import InvalidRequest, RobotConnectionError
from robot_agent_runtime.options import RuntimeOptions
from robot_agent_runtime.ports import Reconciler, RobotPlatform, RobotStateProvider, Skill
from robot_agent_runtime.thread_binding import ThreadBinding


class CcApiPlatform:
    """Assemble CC ports without teaching the runtime vendor-specific lifecycle.

    The supplied Api is already connected and remains application-owned. Start
    subscribes the driver and skills on the application's stable loop. Close
    first confirms skill stop, then releases driver callbacks even after a stop
    failure. Start/close share real results across waiters; cancelling a waiter
    cannot repeat or interrupt the lifecycle operation. No state source or
    reconciler is invented for a vendor that has not supplied those capabilities.
    An explicit skill group only configures hosted skill invocation. The raw
    skill remains responsible for lifecycle on the application's driver loop.
    """

    def __init__(
        self, api: CcApiClient, *, stop_timeout_s: float = 5.0,
        options: RuntimeOptions | None = None,
        skill_thread_group: str | None = None,
    ) -> None:
        self._driver = CcApiDriver(api, options=options)
        self._speech = CcApiSpeechSkill(self._driver, stop_timeout_s=stop_timeout_s)
        self._robotinfo = CcApiRobotInfoSkill(self._driver)
        # Both skills share one CcApiDriver, whose methods must run on a single
        # owning loop. They are therefore bound to the same loop in every case:
        # a named group when one is requested, otherwise a shared loop.
        speech_skill: Skill | ThreadBinding[Skill] = (
            ThreadBinding.shared(self._speech) if skill_thread_group is None
            else ThreadBinding.group(self._speech, skill_thread_group)
        )
        robotinfo_skill: Skill | ThreadBinding[Skill] = (
            ThreadBinding.shared(self._robotinfo) if skill_thread_group is None
            else ThreadBinding.group(self._robotinfo, skill_thread_group)
        )
        self._skills: Mapping[str, Skill | ThreadBinding[Skill]] = MappingProxyType({
            self._speech.definition.name: speech_skill,
            self._robotinfo.definition.name: robotinfo_skill,
        })
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def skills(self) -> Mapping[str, Skill | ThreadBinding[Skill]]:
        return self._skills

    @property
    def robot_state(self) -> RobotStateProvider | None:
        return None

    @property
    def reconciler(self) -> Reconciler | None:
        return None

    def _owner(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise InvalidRequest("CC platform lifecycle must run on its owning loop")

    async def start(self) -> None:
        self._owner()
        if self._closing:
            raise RobotConnectionError("Cannot start a closing CC platform")
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start())
        await asyncio.shield(self._start_task)

    async def _start(self) -> None:
        try:
            await self._speech.start()
            await self._robotinfo.start()
        except Exception as error:
            try:
                await self._driver.close()
            except Exception as cleanup:
                raise ExceptionGroup("CC platform startup and cleanup failed", [error, cleanup]) from None
            raise

    async def close(self) -> None:
        self._owner()
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self._start_task is not None:
            try:
                await asyncio.shield(self._start_task)
            except Exception:
                pass
        errors: list[Exception] = []
        for close in (self._speech.close, self._robotinfo.close, self._driver.close):
            try:
                await close()
            except Exception as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("CC platform shutdown errors", errors)


def create_cc_api_platform(
    api: CcApiClient, *, stop_timeout_s: float = 5.0, options: RuntimeOptions | None = None,
    skill_thread_group: str | None = None,
) -> RobotPlatform:
    return CcApiPlatform(api, stop_timeout_s=stop_timeout_s, options=options,
                         skill_thread_group=skill_thread_group)
