from robot_agent_runtime.errors import AgentRuntimeError


class GuideError(AgentRuntimeError):
    code = 'GUIDE_WORKFLOW_REJECTED'


class GuideCatalogError(GuideError):
    code = 'GUIDE_CATALOG_INVALID'

