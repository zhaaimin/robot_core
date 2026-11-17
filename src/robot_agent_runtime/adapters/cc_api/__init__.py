"""CC vendor integration; importing it does not load or connect a native SDK."""

from robot_agent_runtime.adapters.cc_api.driver import CcApiDriver
from robot_agent_runtime.adapters.cc_api.contracts import CcApiClient, CcApiResponse
from robot_agent_runtime.adapters.cc_api.platform import CcApiPlatform, create_cc_api_platform
from robot_agent_runtime.adapters.cc_api.speech import CcApiSpeechSkill
from robot_agent_runtime.adapters.cc_api.robotinfo import CcApiRobotInfoSkill


__all__ = [
    "CcApiClient", "CcApiDriver", "CcApiPlatform", "CcApiResponse", "CcApiSpeechSkill",
    "CcApiRobotInfoSkill", "create_cc_api_platform",
]
