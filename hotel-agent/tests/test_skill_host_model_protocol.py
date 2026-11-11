import json
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from hotel_agent.script_bindings import build_hotel_script_runner
from hotel_agent.service import HotelWorkflow
from hotel_agent.simulator import HotelSimulator
from hotel_agent.tools import HotelTools
from robot_agent_core.graph import AgentContext, build_graph
from robot_agent_core.models import OpenAIModel
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime
from chat_server import TEST_API_KEY, TEST_MODEL, ChatResponse, completion, local_chat_server


def tool_response(name: str, args: dict[str, Any], call_id: str) -> ChatResponse:
    return completion({
        'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)},
        }],
    }, finish_reason='tool_calls')


async def test_actual_model_client_selects_skill_reads_references_and_runs_python_entry() -> None:
    name = 'frontdesk-reception'
    responses = [
        tool_response('load_skill', {'name': name}, 'select'),
        tool_response('read_skill_resource', {'name': name, 'path': 'references/reception-rules.md'}, 'rules'),
        tool_response('read_skill_resource', {'name': name, 'path': 'references/reception-request-schema.md'}, 'schema'),
        tool_response('run_skill_script', {
            'name': name, 'path': 'scripts/query_reception.py', 'args': {'receipt_code': 'DEMO-001'},
        }, 'query'),
        completion({'role': 'assistant', 'content': '模拟入住凭据已核验；是否需要机器人带路？'}),
    ]
    simulator = HotelSimulator(delay_s=0)
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    with tempfile.TemporaryDirectory() as directory:
        workflow = HotelWorkflow(str(Path(directory) / 'hotel.sqlite3'), runtime)
        await workflow.start()
        try:
            async with local_chat_server(responses) as server:
                model = OpenAIModel(TEST_MODEL, TEST_API_KEY, base_url=server.base_url)
                try:
                    registry = SkillRegistry.bundled('hotel_agent')
                    tools = HotelTools(workflow, 'protocol-guest')
                    context = AgentContext(
                        model=model, tools=tools, skills=registry, guidance=tools,
                        scripts=build_hotel_script_runner(registry, tools),
                    )
                    final = await build_graph().ainvoke({
                        'messages': [HumanMessage(content='我要入住，这是已经核验过的模拟凭据 DEMO-001')],
                    }, context=context)
                    assert len(server.requests) == 5
                    initial = server.requests[0].body
                    assert {tool['function']['name'] for tool in initial['tools']} == {
                        'load_skill', 'read_skill_resource', 'run_skill_script',
                    }
                    initial_system = '\n'.join(message['content'] for message in initial['messages'] if message['role'] == 'system')
                    assert registry.load(name)['description'] in initial_system
                    assert registry.load(name)['instructions'] not in initial_system
                    assert {item['name'] for item in registry.catalog()} == {
                        'frontdesk-reception', 'guest-handoff', 'escort-assistance',
                    }
                    assert registry.load('guest-handoff')['instructions'] not in initial_system
                    assert registry.load('escort-assistance')['instructions'] not in initial_system
                    loaded = json.loads(server.requests[1].body['messages'][-1]['content'])
                    assert loaded['tool'] == 'load_skill'
                    assert 'references/reception-rules.md' in loaded['result']['resources']
                    read_result = json.loads(server.requests[2].body['messages'][-1]['content'])
                    assert read_result['tool'] == 'read_skill_resource'
                    assert read_result['result']['path'] == 'references/reception-rules.md'
                    executed = json.loads(server.requests[4].body['messages'][-1]['content'])
                    assert executed['tool'] == 'run_skill_script'
                    assert executed['execution']['path'] == 'scripts/query_reception.py'
                    assert executed['result']['phase'] == 'awaiting_consent'
                    assert executed['result']['room_id'] == '0108'
                    assert final['active_skill'] == name
                    assert final['loaded_skills'] == ['frontdesk-reception']
                    assert len(final['loaded_resources']) == 2
                    assert simulator.skills['navigation.navigate_to'].calls == 0
                    assert (await workflow.snapshot('protocol-guest'))['consent_confirmed'] is False
                finally:
                    await model.aclose()
        finally:
            try:
                await workflow.close()
            finally:
                await runtime.close()


async def test_actual_client_explains_a_skill_failure_with_tools_disabled() -> None:
    name = 'frontdesk-reception'
    responses = [
        tool_response('load_skill', {'name': name}, 'load'),
        tool_response('run_skill_script', {
            'name': name, 'path': 'scripts/query_reception.py', 'args': {'receipt_code': 'INVALID'},
        }, 'bad-receipt'),
        completion({'role': 'assistant', 'content': '该凭据没有核验成功，请检查编号或向前台求助。'}),
    ]
    simulator = HotelSimulator(delay_s=0)
    runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
    await runtime.start()
    with tempfile.TemporaryDirectory() as directory:
        workflow = HotelWorkflow(str(Path(directory) / 'hotel.sqlite3'), runtime)
        await workflow.start()
        try:
            async with local_chat_server(responses) as server:
                model = OpenAIModel(TEST_MODEL, TEST_API_KEY, base_url=server.base_url)
                try:
                    registry = SkillRegistry.bundled('hotel_agent')
                    tools = HotelTools(workflow, 'bad-receipt-guest')
                    final = await build_graph().ainvoke({
                        'messages': [HumanMessage(content='请核验这张凭据 INVALID')],
                    }, context=AgentContext(
                        model=model, tools=tools, skills=registry, guidance=tools,
                        scripts=build_hotel_script_runner(registry, tools),
                    ))
                    assert final['halted'] is True
                    assert final['messages'][-1].content == responses[-1].body['choices'][0]['message']['content']
                    assert len(server.requests) == 3
                    assert 'tools' not in server.requests[-1].body
                    assert final['observations'][0]['error']['code'] == 'HOTEL_WORKFLOW_REJECTED'
                    assert (await workflow.snapshot('bad-receipt-guest'))['phase'] == 'reception'
                    assert simulator.skills['navigation.navigate_to'].calls == 0
                finally:
                    await model.aclose()
        finally:
            try:
                await workflow.close()
            finally:
                await runtime.close()
