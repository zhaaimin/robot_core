import asyncio
import threading
from collections.abc import AsyncIterator, Mapping
from typing import Any

from robot_agent_runtime.errors import SkillExecutionError, TaskCancelled
from robot_agent_runtime.models import (
    SkillContext,
    SkillDefinition,
    SkillResult,
    Task,
    TaskState,
)

#: Condition labels shared by the simulated skills and the simulated robot.
#: A skill declaring a label the provider never reports is a silent no-op, so
#: both sides spell them once, here.
ESTOP_CLEAR = "estop.clear"
BATTERY_SUFFICIENT = "battery.sufficient"


class SimulatedSkill:
    def __init__(
        self,
        definition: SkillDefinition,
        *,
        delay_s: float,
        failures: set[str],
    ) -> None:
        self.definition = definition
        self.delay_s = delay_s
        self._failures = failures
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def execute(self, context: SkillContext, params: dict[str, Any]) -> SkillResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if context.progress is not None:
                context.progress(0.0)
            await self._wait_or_cancel(context, self._delay_for(params))
            if self.definition.name in self._failures:
                raise SkillExecutionError(
                    f"Simulated failure: {self.definition.name}",
                    details={"skill": self.definition.name},
                )
            output = await self._perform(context, params)
            if context.progress is not None:
                context.progress(1.0)
            return SkillResult(success=True, output=output)
        except asyncio.CancelledError:
            raise TaskCancelled("Simulation stopped before returning") from None
        finally:
            self.active -= 1

    async def _perform(self, context: SkillContext, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _delay_for(self, params: dict[str, Any]) -> float:
        return self.delay_s

    async def _wait_or_cancel(self, context: SkillContext, delay_s: float) -> None:
        if not self.definition.supports_cancel:
            await asyncio.sleep(max(0.0, delay_s))
            return
        if context.cancel_event.is_set():
            raise TaskCancelled("Simulation cancelled before operation")
        delay_waiter = asyncio.create_task(asyncio.sleep(max(0.0, delay_s)))
        cancel_waiter = asyncio.create_task(context.cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {delay_waiter, cancel_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_waiter in done and context.cancel_event.is_set():
                raise TaskCancelled("Simulation cancelled before operation")
        finally:
            for waiter in (delay_waiter, cancel_waiter):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(delay_waiter, cancel_waiter, return_exceptions=True)


class NavigateSkill(SimulatedSkill):
    def __init__(self, state: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(
            SkillDefinition(
                name="navigation.navigate_to",
                version="1.0.0",
                description="让机器人移动到 target 指定的地点；成功表示已到达，不表示路径最优。",
                input_schema={
                    "type": "object",
                    "properties": {"target": {
                        "type": "string",
                        "minLength": 1,
                        "description": "业务定义的地点标识（站点名或房间号），由适配器解释；不是坐标。",
                    }},
                    "required": ["target"],
                    "additionalProperties": False,
                },
                resources={"base"},
                permissions={"navigation"},
                preconditions={ESTOP_CLEAR, BATTERY_SUFFICIENT},
                supports_cancel=True,
            ),
            **kwargs,
        )
        self._state = state

    async def _perform(self, context: SkillContext, params: dict[str, Any]) -> dict[str, Any]:
        target = params["target"]
        self._state["position"] = target
        self._state["navigation_count"] += 1
        return {"target": target, "position": target}


class SpeakSkill(SimulatedSkill):
    def __init__(self, state: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(
            SkillDefinition(
                name="audio.speak",
                version="1.0.0",
                description="让机器人播报一段文本；成功表示播报已完成。",
                input_schema={
                    "type": "object",
                    "properties": {"message": {
                        "type": "string",
                        "minLength": 1,
                        "description": "待播报的完整文本，按原样朗读，不做摘要。",
                    }},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                resources={"speaker"},
                permissions={"audio"},
                supports_cancel=True,
            ),
            **kwargs,
        )
        self._state = state

    async def _perform(self, context: SkillContext, params: dict[str, Any]) -> dict[str, Any]:
        message = params["message"]
        self._state["last_message"] = message
        self._state["speech_count"] += 1
        return {"message": message, "spoken": True}


class GetStateSkill(SimulatedSkill):
    def __init__(self, state: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(
            SkillDefinition(
                name="robot.get_state",
                version="1.0.0",
                description="读取机器人状态快照；只读，不占用资源也不改变状态。",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                supports_cancel=False,
            ),
            **kwargs,
        )
        self._state = state

    async def _perform(self, context: SkillContext, params: dict[str, Any]) -> dict[str, Any]:
        return {"state": dict(self._state)}


class DelaySkill(SimulatedSkill):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            SkillDefinition(
                name="wait.delay",
                version="1.0.0",
                description="等待指定秒数；不产生物理动作，受任务 timeout_s 约束。",
                input_schema={
                    "type": "object",
                    "properties": {"delay_s": {
                        "type": "number",
                        "minimum": 0,
                        "description": "等待秒数，可为小数；超过任务 timeout_s 会被取消。",
                    }},
                    "required": ["delay_s"],
                    "additionalProperties": False,
                },
                supports_cancel=True,
            ),
            **kwargs,
        )

    def _delay_for(self, params: dict[str, Any]) -> float:
        return float(params["delay_s"])

    async def _perform(self, context: SkillContext, params: dict[str, Any]) -> dict[str, Any]:
        return {"delayed_s": float(params["delay_s"])}


class SimulatorReconciler:
    def __init__(self, authoritative_states: Mapping[str, TaskState] | None = None) -> None:
        self._authoritative_states = dict(authoritative_states or {})

    async def reconcile(self, task: Task) -> TaskState | None:
        return self._authoritative_states.get(task.id)


class _LabelSource:
    """Push plumbing shared by the simulated condition sources.

    Each :meth:`watch` call gets its own unbounded queue, so a slow consumer
    delays only itself, and ``_publish`` never blocks the caller that changed
    the robot.
    """

    def __init__(self, unsatisfied: frozenset[str]) -> None:
        self._unsatisfied = unsatisfied
        self._watchers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[frozenset[str]]]] = []
        self._watchers_lock = threading.Lock()

    async def unsatisfied(self) -> frozenset[str]:
        return self._unsatisfied

    async def watch(self) -> AsyncIterator[frozenset[str]]:
        queue: asyncio.Queue[frozenset[str]] = asyncio.Queue()
        watcher = (asyncio.get_running_loop(), queue)
        with self._watchers_lock:
            self._watchers.append(watcher)
        try:
            while True:
                yield await queue.get()
        finally:
            with self._watchers_lock:
                self._watchers.remove(watcher)

    def _publish(self, unsatisfied: frozenset[str]) -> None:
        with self._watchers_lock:
            if unsatisfied == self._unsatisfied:
                return
            self._unsatisfied = unsatisfied
            watchers = tuple(self._watchers)
        for loop, queue in watchers:
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                queue.put_nowait(unsatisfied)
            elif not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, unsatisfied)
                except RuntimeError:
                    pass


class SimulatedRobotState(_LabelSource):
    """A robot state source driven by the caller instead of by hardware.

    :meth:`set_unsatisfied` is the driver: it replaces the set of conditions
    that do not hold and pushes the new set to every live :meth:`watch`
    iterator, which is how a simulated emergency stop or battery drop reaches
    tasks that are already running.

    This is the blunt instrument — the caller names the labels directly. Use
    :class:`SimulatedRobot` when you want the labels *derived* from readings
    the way a real adapter derives them.
    """

    def __init__(self, unsatisfied: set[str] | None = None) -> None:
        super().__init__(frozenset(unsatisfied or ()))

    def set_unsatisfied(self, unsatisfied: set[str]) -> None:
        self._publish(frozenset(unsatisfied))


class SimulatedRobot(_LabelSource):
    """A robot whose condition labels are computed from simulated readings.

    Where :class:`SimulatedRobotState` lets the caller state which labels are
    broken, this one models the hardware: press :meth:`set_estop`, move the
    battery with :meth:`set_voltage` or :meth:`drain`, and the labels follow.
    That makes it the honest stand-in for a real adapter, because it also
    carries the two behaviours a real adapter needs and a test double does not:

    * **Hysteresis.** ``battery.sufficient`` breaks below ``low_voltage`` but
      only recovers above ``recovery_voltage``. Without the gap, a battery
      resting on the threshold flaps and every push cancels fresh work.
    * **Silence when nothing changed.** A reading that recomputes to the same
      set is not published, so a 10 Hz sensor does not wake the runtime 10
      times a second to re-derive the same answer.

    The opening voltage is read against ``low_voltage``, so constructing a
    robot already below it starts out with the label broken.
    """

    def __init__(
        self,
        *,
        voltage: float = 24.0,
        low_voltage: float = 22.5,
        recovery_voltage: float = 23.5,
        estop_engaged: bool = False,
    ) -> None:
        if low_voltage >= recovery_voltage:
            raise ValueError("recovery_voltage must be above low_voltage")
        self._low_voltage = low_voltage
        self._recovery_voltage = recovery_voltage
        self._voltage = voltage
        self._estop_engaged = estop_engaged
        super().__init__(frozenset())
        self._unsatisfied = self._derive()

    @property
    def voltage(self) -> float:
        return self._voltage

    @property
    def estop_engaged(self) -> bool:
        return self._estop_engaged

    def set_estop(self, engaged: bool) -> None:
        self._estop_engaged = engaged
        self._refresh()

    def set_voltage(self, volts: float) -> None:
        self._voltage = volts
        self._refresh()

    async def drain(
        self,
        *,
        volts_per_second: float,
        interval_s: float = 0.02,
        floor: float = 0.0,
    ) -> None:
        """Lower the voltage until it reaches ``floor``.

        A coroutine rather than a managed background task: the caller decides
        its lifetime with :func:`asyncio.create_task` and cancels it on the way
        out, so this class owns no task it would have to clean up.
        """
        if volts_per_second <= 0:
            raise ValueError("volts_per_second must be positive")
        while self._voltage > floor:
            await asyncio.sleep(interval_s)
            self.set_voltage(max(floor, self._voltage - volts_per_second * interval_s))

    def _refresh(self) -> None:
        unsatisfied = self._derive()
        if unsatisfied != self._unsatisfied:
            self._publish(unsatisfied)

    def _derive(self) -> frozenset[str]:
        unsatisfied = set()
        if self._estop_engaged:
            unsatisfied.add(ESTOP_CLEAR)
        was_low = BATTERY_SUFFICIENT in self._unsatisfied
        threshold = self._recovery_voltage if was_low else self._low_voltage
        if self._voltage < threshold:
            unsatisfied.add(BATTERY_SUFFICIENT)
        return frozenset(unsatisfied)


class Simulator:
    def __init__(
        self,
        *,
        delay_s: float = 0.0,
        fail_skills: set[str] | None = None,
    ) -> None:
        if delay_s < 0:
            raise ValueError("delay_s must be non-negative")
        state: dict[str, Any] = {
            "position": "home",
            "last_message": None,
            "navigation_count": 0,
            "speech_count": 0,
        }
        failures = set(fail_skills or ())
        self.skills: dict[str, SimulatedSkill] = {
            "navigation.navigate_to": NavigateSkill(
                state, delay_s=delay_s, failures=failures
            ),
            "audio.speak": SpeakSkill(state, delay_s=delay_s, failures=failures),
            "robot.get_state": GetStateSkill(state, delay_s=delay_s, failures=failures),
            "wait.delay": DelaySkill(delay_s=delay_s, failures=failures),
        }
