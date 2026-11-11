import jsonschema
from typing import Any

from guide_agent.catalog import DestinationCatalog
from guide_agent.service import GuideWorkflow
from robot_agent_core.errors import AgentToolArgumentsError, AgentToolNotAllowed
from robot_agent_core.ports import Observation, failed_observation
from robot_agent_runtime.errors import AgentRuntimeError


_SPECS = {
    'guide_find_destinations': ('按已配置的名称或别名查找地点；不移动机器人。', {'query': {'type': 'string', 'maxLength': 256}}),
    'guide_describe_destination': ('读取配置地点的可信介绍和导航可用性。', {'destination_id': {'type': 'string', 'minLength': 1}}),
    'guide_request_escort': ('提出带路建议，等待用户确认；本调用不启动导航。', {'destination_id': {'type': 'string', 'minLength': 1}}),
    'guide_status': ('查询当前导览和真实任务状态。', {}),
    'guide_cancel': ('请求停止当前导览；根据实际返回状态判断是否确认停止。', {}),
}


class GuideTools:
    """Bind model actions to one user turn without granting confirmation authority."""

    def __init__(self, workflow: GuideWorkflow, catalog: DestinationCatalog, session_id: str, *, epoch: int) -> None:
        self._workflow = workflow
        self._catalog = catalog
        self._session = session_id
        self._epoch = epoch

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [{'type': 'function', 'function': {
            'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False},
        }} for name, (description, fields) in _SPECS.items()]

    async def describe(self) -> dict[str, Any]:
        return {
            'guide': await self._workflow.status(self._session),
            'confirmation': '提出建议不移动。用户说“确认带路”或可信 confirm 事件才能开始；模型不能代替确认。',
            'facts': '只根据配置资料回答；缺少资料应说明。机器人导航成功不代表用户已经跟随到达。',
        }

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Observation:
        try:
            if name not in _SPECS:
                raise AgentToolNotAllowed('Tool is not exposed by the guide application')
            fields = _SPECS[name][1]
            try:
                jsonschema.validate(args, {'type': 'object', 'properties': fields,
                                           'required': list(fields), 'additionalProperties': False})
            except jsonschema.ValidationError:
                raise AgentToolArgumentsError('Guide tool arguments do not match the registered schema') from None
            if name == 'guide_find_destinations':
                result = {'destinations': self._catalog.search(args['query'])}
            elif name == 'guide_describe_destination':
                result = self._catalog.describe(args['destination_id'])
            elif name == 'guide_request_escort':
                result = await self._workflow.request_escort(
                    self._session, args['destination_id'], operation_id=operation_id, expected_epoch=self._epoch,
                )
            elif name == 'guide_cancel':
                result = await self._workflow.cancel(self._session, operation_id=operation_id)
            else:
                result = await self._workflow.status(self._session)
            attention = result.get('phase') in {'failed', 'reconciliation_required'}
            return {'tool': name, 'task_id': result.get('navigation_task_id'),
                    'state': 'failed' if attention else 'succeeded', 'result': result,
                    'error': result.get('error'), 'requires_attention': attention}
        except AgentRuntimeError as error:
            return failed_observation(name, error.info)
