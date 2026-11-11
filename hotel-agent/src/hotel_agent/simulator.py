import math
from typing import Any

from robot_agent_core.errors import AgentConfigurationError
from robot_agent_runtime.adapters.simulator import (
    GetStateSkill, NavigateSkill, SimulatedSkill, SpeakSkill,
)


class HotelSimulator:
    """Use the existing simulator contracts with an explicit hotel origin.

    Initial position is the configured lobby rendezvous, not a claimed sensor
    observation. The simulator implements named-point navigation and speaker
    output only; no guest following, elevator, map localization or real hotel
    connection is supplied. Speech shares the same state dictionary, so an
    announcement can never move the robot or rewrite its position.
    """

    def __init__(self, *, delay_s: float = 0.05) -> None:
        if (isinstance(delay_s, bool) or not isinstance(delay_s, (int, float))
                or not math.isfinite(delay_s) or delay_s < 0):
            raise AgentConfigurationError('Hotel simulator delay must be finite and nonnegative')
        state: dict[str, Any] = {
            'position': 'hotel-lobby', 'navigation_count': 0,
            'last_message': None, 'speech_count': 0,
        }
        self.skills: dict[str, SimulatedSkill] = {
            'navigation.navigate_to': NavigateSkill(state, delay_s=delay_s, failures=set()),
            'robot.get_state': GetStateSkill(state, delay_s=delay_s, failures=set()),
            'audio.speak': SpeakSkill(state, delay_s=delay_s, failures=set()),
        }
