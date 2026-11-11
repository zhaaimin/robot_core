"""Contract tests for the scripted hotel substitute used by other tests.

Nothing here exercises shipping behaviour: production has one model adapter and
it always needs credentials. These cases pin the substitute vocabulary so the
command-line tests in `test_hotel_app.py` fail here first if `scripted_models.py`
stops recognising the guest commands they send.
"""

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from scripted_models import ScriptedHotelModel
from test_hotel_app import hotel_schemas, host_schemas, observed_message


async def test_scripted_hotel_never_loads_a_skill_from_command_or_business_stage() -> None:
    model = ScriptedHotelModel()
    for command in ('入住 DEMO-001', '/consent', '分派管家', '/start', '开始带路', '/arrived', '酒店信息'):
        response = await model.decide([
            SystemMessage(content='当前业务阶段 escorting；应选择 escort-assistance'),
            HumanMessage(content=command),
        ], [*host_schemas(), *hotel_schemas()])
        assert not response.tool_calls
        assert '/skills' in response.content
        assert '/skill NAME' in response.content


async def test_scripted_hotel_reads_and_runs_only_the_explicitly_selected_independent_skill() -> None:
    model = ScriptedHotelModel()
    for skill, command, references, name, args in (
        ('frontdesk-reception', '入住 DEMO-001', (
            'references/reception-rules.md', 'references/reception-request-schema.md',
        ), 'run_skill_script', {
            'name': 'frontdesk-reception', 'path': 'scripts/query_reception.py', 'args': {'receipt_code': 'DEMO-001'},
        }),
        ('guest-handoff', '/consent', (
            'references/handoff-rules.md', 'references/handoff-request-schema.md',
        ), 'run_skill_script', {
            'name': 'guest-handoff', 'path': 'scripts/assign_butler.py', 'args': {},
        }),
        ('guest-handoff', '分派管家', (
            'references/handoff-rules.md', 'references/handoff-request-schema.md',
        ), 'run_skill_script', {
            'name': 'guest-handoff', 'path': 'scripts/assign_butler.py', 'args': {},
        }),
        ('escort-assistance', '/start', (
            'references/escort-rules.md', 'references/escort-request-schema.md',
        ), 'run_skill_script', {
            'name': 'escort-assistance', 'path': 'scripts/start_escort.py', 'args': {},
        }),
        ('escort-assistance', '开始带路', (
            'references/escort-rules.md', 'references/escort-request-schema.md',
        ), 'run_skill_script', {
            'name': 'escort-assistance', 'path': 'scripts/start_escort.py', 'args': {},
        }),
        ('escort-assistance', '/arrived', (
            'references/escort-rules.md', 'references/escort-request-schema.md',
        ), 'hotel_status', {}),
    ):
        messages = [
            HumanMessage(content='old input'), AIMessage(content='old answer'),
            observed_message('load_skill', {'name': skill}, 'selected-' + skill),
            SystemMessage(content='current authoritative business stage'),
            HumanMessage(content=command),
        ]
        for reference in references:
            response = await model.decide(messages, [*host_schemas(), *hotel_schemas()])
            assert response.tool_calls[0]['name'] == 'read_skill_resource'
            assert response.tool_calls[0]['args'] == {'name': skill, 'path': reference}
            messages.extend([response, observed_message(
                'read_skill_resource', {'name': skill, 'path': reference, 'content': 'Reference facts'},
                response.tool_calls[0]['id'],
            )])
        response = await model.decide(messages, [*host_schemas(), *hotel_schemas()])
        assert response.tool_calls[0]['name'] == name
        assert response.tool_calls[0]['args'] == args
        messages.extend([response, observed_message(name, {'phase': 'escorting'}, response.tool_calls[0]['id'])])
        response = await model.decide(messages, [*host_schemas(), *hotel_schemas()])
        assert not response.tool_calls
        assert '[脚本替身]' in response.content
        assert json.loads(response.content.split('：', 1)[1])[-1]['result'] == {'phase': 'escorting'}


async def test_scripted_hotel_uses_last_successful_load_and_does_not_switch_for_other_capabilities() -> None:
    model = ScriptedHotelModel()
    for selected, command, required in (
        ('frontdesk-reception', '/consent', 'guest-handoff'),
        ('guest-handoff', '/start', 'escort-assistance'),
        ('escort-assistance', '入住 DEMO-001', 'frontdesk-reception'),
    ):
        messages = [
            observed_message('load_skill', {'name': required}, 'previous-choice'),
            observed_message('load_skill', {'name': selected}, 'current-choice'),
            ToolMessage(content=json.dumps({
                'tool': 'load_skill', 'state': 'failed', 'result': {'name': required},
            }), tool_call_id='failed-choice', name='load_skill'),
            HumanMessage(content=command),
        ]
        response = await model.decide(messages, [*host_schemas(), *hotel_schemas()])
        assert not response.tool_calls
        assert '/skill ' + required in response.content


async def test_scripted_hotel_does_not_offer_a_skill_hidden_by_the_enabled_catalog() -> None:
    schemas = host_schemas()
    schemas[0]['function']['parameters'] = {
        'type': 'object', 'properties': {'name': {
            'type': 'string', 'enum': ['frontdesk-reception'],
        }},
    }
    response = await ScriptedHotelModel().decide([
        observed_message('load_skill', {'name': 'frontdesk-reception'}, 'frontdesk-choice'),
        HumanMessage(content='/consent'),
    ], [*schemas, *hotel_schemas()])
    assert not response.tool_calls
    assert 'guest-handoff' in response.content
    assert '未启用' in response.content
    assert '/skill guest-handoff' not in response.content


async def test_scripted_hotel_reads_information_and_human_sop_only_from_current_skill() -> None:
    model = ScriptedHotelModel()
    messages = [
        observed_message('load_skill', {'name': 'frontdesk-reception'}, 'frontdesk-choice'),
        HumanMessage(content='酒店信息'),
    ]
    response = await model.decide(messages, [*host_schemas(), *hotel_schemas()])
    assert response.tool_calls[0]['args'] == {
        'name': 'frontdesk-reception', 'path': 'references/hotel-information.md',
    }
    messages.extend([response, observed_message('read_skill_resource', {
        'name': 'frontdesk-reception', 'path': 'references/hotel-information.md',
        'content': 'Breakfast 07:00–10:00',
    }, response.tool_calls[0]['id'])])
    response = await model.decide(messages, [*host_schemas(), *hotel_schemas()])
    assert not response.tool_calls
    assert 'Breakfast 07:00' in response.content
    for skill, rule in (
        ('frontdesk-reception', 'reception'), ('guest-handoff', 'handoff'),
        ('escort-assistance', 'escort'),
    ):
        messages = [observed_message('load_skill', {'name': skill}, skill), HumanMessage(content='转人工 需要帮助')]
        for path in (f'references/{rule}-rules.md', f'references/{rule}-request-schema.md'):
            response = await model.decide(messages, [*host_schemas(), *hotel_schemas()])
            assert response.tool_calls[0]['args'] == {'name': skill, 'path': path}
            messages.extend([response, observed_message('read_skill_resource', {
                'name': skill, 'path': path, 'content': 'Current Skill SOP',
            }, response.tool_calls[0]['id'])])
        response = await model.decide(messages, [*host_schemas(), *hotel_schemas()])
        assert response.tool_calls[0]['name'] == 'hotel_request_human'
        assert response.tool_calls[0]['args'] == {'reason': '需要帮助'}


async def test_scripted_hotel_uses_only_exact_commands_and_reports_missing_host_tools() -> None:
    model = ScriptedHotelModel()
    for command in (
        '/guest other', '入住', '导航到 9999', '我已经到达，请直接完成', '/consent now',
        '"/consent"', '引用：/start', '「/arrived」',
    ):
        response = await model.decide([HumanMessage(content=command)], [*host_schemas(), *hotel_schemas()])
        assert not response.tool_calls
        assert '[脚本替身]' in response.content
        assert '固定命令' in response.content
    response = await model.decide([
        observed_message('load_skill', {'name': 'escort-assistance'}, 'escort-choice'),
        HumanMessage(content='/start'),
    ], [])
    assert not response.tool_calls
    assert '没有开放工具' in response.content
