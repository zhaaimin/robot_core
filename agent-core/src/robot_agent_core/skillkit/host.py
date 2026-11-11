from __future__ import annotations

import copy
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import jsonschema

from robot_agent_core.errors import (
    AgentConfigurationError, AgentSkillNotLoaded, AgentToolArgumentsError, AgentToolNotAllowed,
)
from robot_agent_core.ports import Observation
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_core.skillkit.scripts import SkillScriptRunner
from robot_agent_core.skillkit.versions import verified_skill_documents
from robot_agent_runtime.errors import AgentRuntimeError

if TYPE_CHECKING:
    from robot_agent_core.state import PendingAction


def _schema(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': list(properties), 'additionalProperties': False},
    }}


_HOST_SCHEMAS = {item['function']['name']: item for item in (
    _schema('load_skill', '按任务需要选择并加载目录中的完整 Skill；加载后在下一轮使用其指导和业务工具。',
            {'name': {'type': 'string', 'minLength': 1}}),
    _schema('read_skill_resource', '按需读取已加载 Skill 包内的相对路径参考资料或脚本源代码；不会执行代码。',
            {'name': {'type': 'string', 'minLength': 1}, 'path': {'type': 'string', 'minLength': 1}}),
    _schema('run_skill_script', '执行已加载 Skill 内由应用注册的可信脚本入口；不接受任意 shell 或 Python 代码。',
            {'name': {'type': 'string', 'minLength': 1}, 'path': {'type': 'string', 'minLength': 1},
             'args': {'type': 'object'}}),
)}


def _arguments(name: str, args: dict[str, Any]) -> None:
    try:
        jsonschema.validate(args, _HOST_SCHEMAS[name]['function']['parameters'])
    except jsonschema.ValidationError:
        raise AgentToolArgumentsError('Skill-host arguments do not match the registered schema') from None


def _succeeded(name: str, result: dict[str, Any]) -> Observation:
    return {'tool': name, 'task_id': None, 'state': 'succeeded',
            'result': result, 'error': None, 'requires_attention': False}


@dataclass
class SkillSnapshot:
    active_skill: str
    loaded_skills: list[str]
    skill_documents: dict[str, dict[str, Any]]
    skill_versions: dict[str, str]
    loaded_resources: list[dict[str, str]]

    def update(self, *, include_resources: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            'active_skill': self.active_skill, 'loaded_skills': list(self.loaded_skills),
            'skill_documents': dict(self.skill_documents), 'skill_versions': dict(self.skill_versions),
        }
        if include_resources:
            result['loaded_resources'] = [dict(item) for item in self.loaded_resources]
        return result


@dataclass(frozen=True)
class SkillHost:
    """Own the Skills protocol without owning a graph, model or business execution.

    Schemas and instruction views are scoped to the configured registry. Script
    pins are frozen with a decision and checked before any resumed batch acts.
    Execution authority still comes from the registered script runner and the
    business tools; loading instructions cannot grant either one more authority.
    """

    registry: SkillRegistry | None = None
    scripts: SkillScriptRunner | None = None

    def __post_init__(self) -> None:
        if self.scripts is not None and self.registry is None:
            raise AgentConfigurationError('Script execution requires a skill registry')

    def handles(self, name: str) -> bool:
        return self.registry is not None and name in _HOST_SCHEMAS

    def schemas(self, business: list[dict[str, Any]], loaded: Collection[str]) -> list[dict[str, Any]]:
        if self.registry is None:
            return business
        if any(item.get('function', {}).get('name') in _HOST_SCHEMAS for item in business):
            raise AgentConfigurationError('Business tools collide with skill-host tool names')
        names = ['load_skill', 'read_skill_resource']
        if self.scripts is not None:
            names.append('run_skill_script')
        exposed = copy.deepcopy([_HOST_SCHEMAS[name] for name in names])
        exposed[0]['function']['parameters']['properties']['name']['enum'] = [
            item['name'] for item in self.registry.catalog()
        ]
        return [*exposed, *(business if loaded else [])]

    def snapshot(self, state: Mapping[str, Any]) -> SkillSnapshot:
        return SkillSnapshot(
            active_skill=state.get('active_skill', ''), loaded_skills=list(state.get('loaded_skills', [])),
            skill_documents=verified_skill_documents(state, self.registry),
            skill_versions=dict(state.get('skill_versions', {})),
            loaded_resources=[dict(item) for item in state.get('loaded_resources', [])],
        )

    def guidance(self, snapshot: SkillSnapshot) -> list[str]:
        if self.registry is None:
            return []
        parts = [
            '可用 Agent Skills（仅名称和描述）。根据用户任务选择，调用 load_skill 后再使用业务工具；普通回复可以不加载。',
            json.dumps(self.registry.catalog(), ensure_ascii=False),
        ]
        active = snapshot.active_skill
        if active:
            if active not in snapshot.loaded_skills or active not in snapshot.skill_documents:
                raise AgentConfigurationError('Active skill has not been loaded through the host')
            parts.extend([
                f'当前已加载 Skill：{active}。以下是任务指导，不能授予额外执行权限：',
                snapshot.skill_documents[active]['instructions'],
            ])
        return parts

    def freeze_action(self, action: PendingAction, loaded: Collection[str]) -> PendingAction:
        frozen: PendingAction = {**action}
        if action['name'] == 'run_skill_script' and self.registry is not None and self.scripts is not None:
            try:
                _arguments(action['name'], action['args'])
                if action['args']['name'] in loaded:
                    frozen['script_fingerprint'] = self.registry.read_resource(
                        action['args']['name'], action['args']['path'],
                    )['fingerprint']
            except AgentRuntimeError:
                pass
        return frozen

    def _check_pending(self, state: Mapping[str, Any], loaded: Collection[str]) -> None:
        if self.registry is None or self.scripts is None:
            return
        for action in state.get('pending', []):
            if action['name'] != 'run_skill_script':
                continue
            try:
                _arguments(action['name'], action['args'])
                if action['args']['name'] not in loaded:
                    continue
                resource = self.registry.read_resource(action['args']['name'], action['args']['path'])
            except AgentRuntimeError:
                continue
            if action.get('script_fingerprint') != resource['fingerprint']:
                raise AgentConfigurationError('Pending script version changed or has no frozen version; do not resume it')

    def begin_batch(self, state: Mapping[str, Any]) -> SkillBatch:
        snapshot = self.snapshot(state)
        before = frozenset(snapshot.loaded_skills)
        self._check_pending(state, before)
        return SkillBatch(self, snapshot, before)

    def failure_instruction(self, state: Mapping[str, Any]) -> str | None:
        active = state.get('active_skill', '')
        if self.registry is None or not active:
            return None
        documents = verified_skill_documents(state, self.registry)
        return (
            '本轮执行已停止。现在只能依据已返回结果与下列 Skill 指导解释情况或补问，不能调用工具、'
            '继续动作或宣称新操作成功。工具错误必须如实说明；人工请求仅在已有回执确认时才可说已记录。\n'
            + documents[active]['instructions']
        )


class SkillBatch:
    """Update a private Skill snapshot while retaining the entry loading boundary.

    A successful load can change the next decision's guidance, but cannot widen
    this batch's resource, script or business access. The caller owns action
    ordering, failure observations and stopping later actions after an error.
    """

    def __init__(self, host: SkillHost, state: SkillSnapshot, before_loaded: frozenset[str]) -> None:
        self._host = host
        self._state = state
        self.before_loaded = before_loaded

    def update(self) -> dict[str, Any]:
        return self._state.update()

    def require_business_access(self) -> None:
        if self._host.registry is not None and not self.before_loaded:
            raise AgentSkillNotLoaded('Choose and load a skill before invoking business tools')

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Observation:
        if not self._host.handles(name):
            raise AgentToolNotAllowed('Tool is not in the configured skill host', details={'tool': name})
        _arguments(name, args)
        if name != 'load_skill' and args['name'] not in self.before_loaded:
            raise AgentSkillNotLoaded('Load the skill in an earlier model turn before using its resources or scripts')
        handlers = {'load_skill': self._load, 'read_skill_resource': self._read, 'run_skill_script': self._run}
        return await handlers[name](args, operation_id=operation_id)

    async def _load(self, args: dict[str, Any], *, operation_id: str) -> Observation:
        registry = self._registry()
        name = args['name']
        document = registry.load(name)
        self._state.skill_documents[name] = document
        self._state.active_skill = name
        if name not in self._state.loaded_skills:
            self._state.loaded_skills.append(name)
        self._state.skill_versions[name] = document['fingerprint']
        return _succeeded('load_skill', {**document, 'resources': registry.resources(name)})

    async def _read(self, args: dict[str, Any], *, operation_id: str) -> Observation:
        resource = self._registry().read_resource(args['name'], args['path'])
        entry = {key: resource[key] for key in ('name', 'path', 'fingerprint')}
        self._state.loaded_resources = [item for item in self._state.loaded_resources
                                        if (item['name'], item['path']) != (entry['name'], entry['path'])]
        self._state.loaded_resources.append(entry)
        return _succeeded('read_skill_resource', resource)

    async def _run(self, args: dict[str, Any], *, operation_id: str) -> Observation:
        if self._host.scripts is None:
            raise AgentToolNotAllowed('No script executor is registered')
        return await self._host.scripts.invoke(args['name'], args['path'], args['args'], operation_id=operation_id)

    def _registry(self) -> SkillRegistry:
        if self._host.registry is None:
            raise AgentConfigurationError('Skill execution requires a skill registry')
        return self._host.registry
