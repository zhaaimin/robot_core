import sys
from importlib import import_module

from robot_agent_core.errors import AgentToolNotAllowed
from robot_agent_core.ports import ToolSet
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_core.skillkit.scripts import ApprovedScript, SkillScriptRunner


_SCRIPTS = {
    'frontdesk-reception': 'query_reception',
    'guest-handoff': 'assign_butler',
    'escort-assistance': 'start_escort',
}


def build_hotel_script_runner(
    registry: SkillRegistry, tools: ToolSet, *, timeout_s: float = 5.0,
) -> SkillScriptRunner:
    """Bind published code, without executing Python from a custom skill directory.

    The active catalog may replace guidance or expose any subset of these skills. Only
    published script fingerprints authorize these trusted handlers. A missing or
    different resource leaves that entry unbound, without disabling document
    guidance or application controls. The generic runner still rejects unbound
    calls and rechecks approved sources. This registration is an application
    trust boundary, not a sandbox for untrusted Python.
    """
    bindings: list[ApprovedScript] = []
    visible = {item['name'] for item in registry.catalog()}
    if visible.intersection(_SCRIPTS):
        published = SkillRegistry.bundled('hotel_agent')
        for name, script in _SCRIPTS.items():
            if name not in visible:
                continue
            path = f'scripts/{script}.py'
            try:
                active = registry.read_resource(name, path)
            except AgentToolNotAllowed:
                print(f'未注册酒店脚本 {name}/{path}：资源缺失或不可读取；技能文档仍可加载。',
                      file=sys.stderr)
                continue
            resource = published.read_resource(name, path)
            if active['fingerprint'] != resource['fingerprint']:
                print(f'未注册酒店脚本 {name}/{path}：源码指纹与发布版本不符；技能文档仍可加载。',
                      file=sys.stderr)
                continue
            module = import_module(f'hotel_agent.skills.{name}.scripts.{script}')
            bindings.append(ApprovedScript(
                skill=name, path=path, fingerprint=resource['fingerprint'], handler=module.run,
            ))
    return SkillScriptRunner(registry, tools, bindings, timeout_s=timeout_s)
