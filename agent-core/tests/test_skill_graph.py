import json
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage


class NoPhysicalTools:
    schemas: list[dict[str, Any]] = []

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Any:
        raise AssertionError('Loading a skill must not call a robot or hotel interface')


def write_skill(root: Path, name: str, body: str) -> None:
    folder = root / name
    folder.mkdir()
    (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Use when {name} applies.\n---\n{body}\n')


async def test_business_stage_does_not_select_or_inject_skill_instructions() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent_core.skillkit.catalog import SkillRegistry

    class Guidance:
        async def describe(self) -> dict[str, Any]:
            return {'required_skill': 'frontdesk-reception', 'facts': {'phase': 'reception'}}

    class Model:
        async def decide(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> AIMessage:
            system_text = '\n'.join(str(item.content) for item in messages if isinstance(item, SystemMessage))
            assert 'RECEIPT-FIRST-INSTRUCTIONS' not in system_text
            assert 'HANDOFF-BODY-NOT-YET-NEEDED' not in system_text
            assert 'guest-handoff' in system_text
            assert 'reception' in system_text
            assert any(tool['function']['name'] == 'load_skill' for tool in tools)
            return AIMessage(content='请提供入住凭据')

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_skill(root, 'frontdesk-reception', 'RECEIPT-FIRST-INSTRUCTIONS')
        write_skill(root, 'guest-handoff', 'HANDOFF-BODY-NOT-YET-NEEDED')
        final = await build_graph().ainvoke(
            {'messages': [HumanMessage(content='我要入住')]},
            context=AgentContext(model=Model(), tools=NoPhysicalTools(),
                                 skills=SkillRegistry.from_directory(root), guidance=Guidance()),
        )
        assert final['active_skill'] == ''
        assert final['loaded_skills'] == []


async def test_load_skill_returns_instructions_without_a_robot_task() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent_core.skillkit.catalog import SkillRegistry

    class Model:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> AIMessage:
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content='', tool_calls=[{
                    'id': 'load-1', 'name': 'load_skill', 'args': {'name': 'frontdesk-reception'},
                }])
            observed = json.loads(str(messages[-1].content))
            assert observed['task_id'] is None
            assert observed['result']['instructions'] == 'Read this before reception'
            return AIMessage(content='已加载接待指导')

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_skill(root, 'frontdesk-reception', 'Read this before reception')
        final = await build_graph().ainvoke(
            {'messages': [HumanMessage(content='加载接待技能')]},
            context=AgentContext(model=Model(), tools=NoPhysicalTools(), skills=SkillRegistry.from_directory(root)),
        )
        assert final['loaded_skills'] == ['frontdesk-reception']
        assert final['observations'][0]['state'] == 'succeeded'


async def test_model_cannot_use_load_skill_to_read_an_arbitrary_path() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent_core.skillkit.catalog import SkillRegistry

    class Model:
        async def decide(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> AIMessage:
            return AIMessage(content='', tool_calls=[{
                'id': 'bad-load', 'name': 'load_skill', 'args': {'name': '../private'},
            }])

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_skill(root, 'frontdesk-reception', 'Safe document')
        final = await build_graph().ainvoke(
            {'messages': [HumanMessage(content='read file')]},
            context=AgentContext(model=Model(), tools=NoPhysicalTools(), skills=SkillRegistry.from_directory(root)),
        )
        assert final['halted'] is True
        assert final['observations'][0]['error']['code'] == 'AGENT_TOOL_NOT_ALLOWED'
