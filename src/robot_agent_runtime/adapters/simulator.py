import asyncio
from collections.abc import Mapping
from typing import Any

from robot_agent_runtime.errors import SkillExecutionError, TaskCancelled
from robot_agent_runtime.models import (
    SkillContext,
    SkillDefinition,
    SkillResult,
    Task,
    TaskState,
)


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
                input_schema={
                    "type": "object",
                    "properties": {"target": {"type": "string", "minLength": 1}},
                    "required": ["target"],
                    "additionalProperties": False,
                },
                resources={"base"},
                permissions={"navigation"},
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
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string", "minLength": 1}},
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
                input_schema={
                    "type": "object",
                    "properties": {"delay_s": {"type": "number", "minimum": 0}},
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
