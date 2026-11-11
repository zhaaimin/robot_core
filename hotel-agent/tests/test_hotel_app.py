import asyncio
import io
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


def test_hotel_configuration_is_separate_and_cc_is_not_an_option(tmp_path: Path) -> None:
    from dataclasses import replace

    from hotel_agent.app import main, parse_args
    from hotel_agent.profiles import HOTEL_PROFILE
    from robot_agent_core.configuration import rebuild_config
    from robot_agent_core.errors import AgentConfigurationError

    hotel = parse_args(['--data-dir', str(tmp_path)])
    assert hotel.profile is HOTEL_PROFILE
    other_agent = rebuild_config(hotel, profile=replace(
        HOTEL_PROFILE, name='robot', simulator_database='simulator-tasks.sqlite3',
        runtime_identity_tag=None, checkpoint_database='checkpoints.sqlite3', business_database=None,
    ))
    assert other_agent.task_database == tmp_path / 'simulator-tasks.sqlite3'
    assert other_agent.business_database is None
    assert other_agent.runtime_id != hotel.runtime_id
    assert hotel.task_database.name == 'hotel-simulator-tasks.sqlite3'
    assert hotel.business_database == tmp_path / 'hotel.sqlite3'
    with pytest.raises(AgentConfigurationError, match='invalid choice'):
        parse_args(['--backend', 'cc', '--data-dir', str(tmp_path)])
    assert main(['--backend', 'cc', '--data-dir', str(tmp_path)]) == 1


def test_hotel_model_selection_and_skills_directory(tmp_path: Path) -> None:
    from hotel_agent.app import parse_args

    hotel = parse_args(['--skills-dir', str(tmp_path)])
    assert hotel.skills_dir == tmp_path.resolve()


def custom_reception_catalog(root: Path, script_state: str) -> Path:
    skill = root / 'frontdesk-reception'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        '---\nname: frontdesk-reception\ndescription: Use when answering hotel questions.\n---\n'
        'Answer from supplied facts. Scripts require separate application approval.\n',
        encoding='utf-8',
    )
    if script_state == 'replaced':
        scripts = skill / 'scripts'
        scripts.mkdir()
        (scripts / 'query_reception.py').write_text(
            'raise AssertionError("unapproved code executed")\n', encoding='utf-8',
        )
    return root


@pytest.mark.parametrize('script_state', ('missing', 'replaced'))
@pytest.mark.parametrize('prompt', ('/skills', '/status', '/cancel'))
def test_custom_hotel_skill_scripts_do_not_block_one_shot_controls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], script_state: str, prompt: str,
) -> None:
    from hotel_agent.app import main

    catalog = custom_reception_catalog(tmp_path / 'skills', script_state)
    data = tmp_path / 'data'
    result = main([
        '--skills-dir', str(catalog), '--data-dir', str(data),
        '--thread-id', 'guest', '--prompt', prompt,
    ])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    output = json.loads(captured.out)
    assert [item['name'] for item in output['skill_catalog']] == ['frontdesk-reception']
    assert 'frontdesk-reception/scripts/query_reception.py' in captured.err
    if prompt == '/skills':
        assert output['hotel'] is None
        with closing(sqlite3.connect(data / 'hotel.sqlite3')) as database:
            assert database.execute('SELECT COUNT(*) FROM hotel_services').fetchone()[0] == 0
    else:
        assert output['hotel']['phase'] == ('canceled' if prompt == '/cancel' else 'reception')
        assert output['hotel']['runtime_task_id'] is None


@pytest.mark.parametrize('script_state', ('missing', 'replaced'))
def test_custom_hotel_skill_scripts_do_not_block_interactive_selection_and_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    script_state: str,
) -> None:
    from hotel_agent.app import main

    catalog = custom_reception_catalog(tmp_path / 'skills', script_state)
    monkeypatch.setattr('sys.stdin', io.StringIO(
        '/skills\n/skill frontdesk-reception\n/status\n/cancel\n/skills\n',
    ))
    result = main([
        '--skills-dir', str(catalog), '--data-dir', str(tmp_path / 'data'),
        '--thread-id', 'guest',
    ])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    outputs = [json.loads(line) for line in captured.out.splitlines()]
    assert len(outputs) == 5
    assert outputs[0]['active_skill'] is None
    assert outputs[1]['active_skill'] == 'frontdesk-reception'
    assert outputs[2]['hotel']['phase'] == 'reception'
    assert outputs[3]['hotel']['phase'] == 'canceled'
    assert outputs[3]['hotel']['runtime_task_id'] is None
    assert outputs[4]['hotel'] is None
    assert outputs[4]['loaded_skills'] == ['frontdesk-reception']
    assert captured.err.count('frontdesk-reception/scripts/query_reception.py') == 1


def test_hotel_rejects_empty_skill_catalog_before_creating_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from hotel_agent.app import main
    from robot_agent_core.errors import AgentConfigurationError

    created: list[bool] = []

    def unexpected_runtime(*args: Any, **kwargs: Any) -> Any:
        created.append(True)
        raise AgentConfigurationError('Runtime should not be constructed for an empty catalog')

    monkeypatch.setattr('hotel_agent.application.RobotRuntime', unexpected_runtime)
    root = tmp_path / 'skills'
    root.mkdir()
    data = tmp_path / 'state'
    result = main([
        '--skills-dir', str(root), '--data-dir', str(data),
        '--thread-id', 'guest', '--prompt', '入住 DEMO-001',
    ])
    captured = capsys.readouterr()
    assert result == 1
    assert not created
    error = json.loads(captured.err.splitlines()[-1])['error']
    assert error['code'] == 'AGENT_CONFIGURATION_INVALID'
    assert not (data / 'hotel.sqlite3').exists()
    assert not (data / 'hotel-simulator-tasks.sqlite3').exists()


def test_hotel_refuses_a_navigation_only_allowlist_before_constructing_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Announcement belongs to the hotel scene, so a speakerless allowlist is a config error.

    Booting anyway would turn a first announcement into an unexplained mid
    conversation outage the model can only report as a broken hotel.
    """
    from hotel_agent.app import main
    from robot_agent_core.errors import AgentConfigurationError

    constructed: list[bool] = []

    def unexpected_runtime(*args: Any, **kwargs: Any) -> Any:
        constructed.append(True)
        raise AgentConfigurationError('Runtime should not be constructed without a speaker')

    monkeypatch.setattr('hotel_agent.application.RobotRuntime', unexpected_runtime)
    assert main([
        '--allow-skill', 'navigation.navigate_to',
        '--data-dir', str(tmp_path), '--thread-id', 'guest', '--prompt', '入住 DEMO-001',
    ]) == 1
    assert not constructed


def test_hotel_assembly_wires_the_speaker_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hotel runtime owns both skills, so the workflow can speak without a navigation lease."""
    from hotel_agent.app import main
    from robot_agent_core.errors import AgentConfigurationError

    assembled: dict[str, Any] = {}

    class UnusedRuntime:
        async def start(self) -> None:
            assembled['started'] = True

        async def close(self) -> None:
            assembled['closed'] = True

    def capture_runtime(repository: Any, skills: Any, **kwargs: Any) -> Any:
        assembled['skills'] = set(skills)
        assembled['permissions'] = kwargs.get('allowed_permissions')
        return UnusedRuntime()

    def capture_workflow(database: Any, runtime: Any, **kwargs: Any) -> Any:
        assembled['workflow_kwargs'] = kwargs
        raise AgentConfigurationError('hotel assembly stops after describing the workflow')

    monkeypatch.setattr('hotel_agent.application.RobotRuntime', capture_runtime)
    monkeypatch.setattr('hotel_agent.service.HotelWorkflow', capture_workflow)
    assert main([
        '--data-dir', str(tmp_path), '--wait-timeout', '7',
        '--thread-id', 'guest', '--prompt', '入住 DEMO-001',
    ]) == 1
    assert assembled['skills'] == {'navigation.navigate_to', 'audio.speak'}
    assert assembled['permissions'] == {'navigation', 'audio'}
    assert assembled['workflow_kwargs']['wait_timeout_s'] == 7.0
    assert assembled['started'] is True
    assert assembled['closed'] is True


async def test_hotel_accepts_arbitrary_valid_skill_catalog_selected_by_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from hotel_agent.app import parse_args, run_application

    root = tmp_path / 'skills' / 'visitor-help'
    root.mkdir(parents=True)
    (root / 'SKILL.md').write_text(
        '---\nname: visitor-help\ndescription: Help a visitor using supplied facts.\n---\n'
        'Read the authoritative hotel facts before answering.\n', encoding='utf-8',
    )

    class SelectingModel:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            conversation = [message for message in messages if not isinstance(message, SystemMessage)]
            if isinstance(conversation[-1], ToolMessage):
                return AIMessage(content='custom skill loaded')
            assert {item['function']['name'] for item in tools} == {
                'load_skill', 'read_skill_resource', 'run_skill_script',
            }
            return AIMessage(content='', tool_calls=[{
                'name': 'load_skill', 'args': {'name': 'visitor-help'}, 'id': 'custom-load',
            }])

    config = parse_args([
        '--skills-dir', str(root.parent), '--data-dir', str(tmp_path / 'state'),
        '--thread-id', 'guest', '--prompt', 'help',
    ])
    await run_application(config, SelectingModel())
    output = json.loads(capsys.readouterr().out)
    assert output['active_skill'] == 'visitor-help'
    assert output['loaded_skills'] == ['visitor-help']
    assert output['hotel']['phase'] == 'reception'


def hotel_schemas() -> list[dict[str, Any]]:
    return [{'type': 'function', 'function': {'name': name}} for name in (
        'hotel_confirm_reception', 'hotel_assign_butler', 'hotel_start_escort',
        'hotel_status', 'hotel_cancel_escort', 'hotel_request_human',
    )]


def host_schemas() -> list[dict[str, Any]]:
    return [{'type': 'function', 'function': {'name': name}} for name in (
        'load_skill', 'read_skill_resource', 'run_skill_script',
    )]


def observed_message(name: str, result: dict[str, Any], call_id: str) -> ToolMessage:
    return ToolMessage(content=json.dumps({
        'tool': name, 'task_id': None, 'state': 'succeeded', 'result': result,
        'error': None, 'requires_attention': False,
    }), tool_call_id=call_id, name=name)


def test_hotel_one_shot_requires_selection_before_authoritative_reception(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from hotel_agent.app import main

    result = main([
        '--data-dir', str(tmp_path), '--thread-id', 'guest-one',
        '--prompt', '入住 DEMO-001',
    ])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    output = json.loads(captured.out)
    assert output['agent'] == 'hotel'
    assert output['session_id'] == output['thread_id'] == 'guest-one'
    assert output['hotel']['phase'] == 'reception'
    assert output['hotel']['room_id'] is None
    assert output['loaded_skills'] == []
    assert output['loaded_resources'] == []
    assert output['active_skill'] is None
    assert '/skill NAME' in output['reply']
    with closing(sqlite3.connect(tmp_path / 'hotel.sqlite3')) as database:
        previous_service = database.execute('SELECT * FROM hotel_services').fetchall()
    assert main([
        '--data-dir', str(tmp_path), '--thread-id', 'guest-one',
        '--prompt', '/skill frontdesk-reception',
    ]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected['active_skill'] == 'frontdesk-reception'
    assert selected['hotel'] is None
    with closing(sqlite3.connect(tmp_path / 'hotel.sqlite3')) as database:
        assert database.execute('SELECT * FROM hotel_services').fetchall() == previous_service
    assert main([
        '--data-dir', str(tmp_path), '--thread-id', 'guest-one',
        '--prompt', '入住 DEMO-001',
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['hotel']['phase'] == 'awaiting_consent'
    assert output['hotel']['room_id'] == '0108'
    assert output['hotel']['consent_confirmed'] is False
    assert output['hotel']['runtime_task_id'] is None
    assert {entry['path'] for entry in output['loaded_resources']} == {
        'references/reception-rules.md', 'references/reception-request-schema.md',
    }
    assert len(output['skill_versions']['frontdesk-reception']) == 64
    assert '[脚本替身]' in output['reply']
    trace = json.loads(output['reply'].split('：', 1)[1])
    assert [item['tool'] for item in trace[:2]] == ['read_skill_resource', 'read_skill_resource']
    assert trace[-1]['execution']['path'] == 'scripts/query_reception.py'
    assert trace[-1]['result']['phase'] == 'awaiting_consent'
    assert '模拟' in captured.err
    assert (tmp_path / 'hotel.sqlite3').exists()
    assert (tmp_path / 'hotel-selectable-skills-checkpoints.sqlite3').exists()
    assert not (tmp_path / 'hotel-skill-host-checkpoints.sqlite3').exists()
    assert not (tmp_path / 'hotel-checkpoints.sqlite3').exists()
    assert not (tmp_path / 'checkpoints.sqlite3').exists()


async def test_reception_switches_guest_while_the_previous_escort_is_still_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from hotel_agent.app import parse_args, run_application
    from hotel_agent.simulator import HotelSimulator
    from scripted_models import ScriptedHotelModel

    monkeypatch.setattr('hotel_agent.simulator.HotelSimulator', lambda **kwargs: HotelSimulator(delay_s=30))
    monkeypatch.setattr('sys.stdin', io.StringIO(
        '/skill frontdesk-reception\n入住 DEMO-001\n/skill guest-handoff\n/consent\n'
        '/skill escort-assistance\n/start\n/guest guest-two\n'
        '/skill frontdesk-reception\n入住 DEMO-002\n/skill guest-handoff\n/consent\n'
        '/guest guest-one\n/status\n/cancel\n'
    ))
    config = parse_args(['--data-dir', str(tmp_path), '--thread-id', 'guest-one'])
    async with asyncio.timeout(2):
        await run_application(config, ScriptedHotelModel())
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[5]['hotel']['phase'] == 'escorting'
    assert rows[8]['session_id'] == 'guest-two'
    assert rows[8]['hotel']['phase'] == 'awaiting_consent'
    assert rows[10]['hotel']['phase'] == 'waiting_for_robot'
    assert rows[10]['observations'][-1]['error']['code'] == 'HOTEL_WORKFLOW_REJECTED'
    assert rows[12]['session_id'] == 'guest-one'
    assert rows[12]['hotel']['phase'] == 'escorting'
    assert rows[13]['hotel']['phase'] == 'canceled'
    assert rows[13]['hotel']['robot_status'] == 'needs_reposition'


def test_hotel_interactive_full_flow_distinguishes_navigation_and_guest_arrival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from hotel_agent.app import main

    monkeypatch.setattr('sys.stdin', io.StringIO(
        '/skills\n/skill frontdesk-reception\n入住 DEMO-001\n/skill guest-handoff\n/consent\n'
        '/skill escort-assistance\n/start\n/wait\n/arrived\n/wait\n/status\n'
    ))
    result = main(['--data-dir', str(tmp_path), '--thread-id', 'guest-one'])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    outputs = [json.loads(line) for line in captured.out.splitlines()]
    assert len(outputs) == 11
    assert outputs[0]['active_skill'] is None
    assert outputs[0]['loaded_skills'] == []
    assert outputs[2]['hotel']['phase'] == 'awaiting_consent'
    assert outputs[4]['hotel']['phase'] == 'waiting_for_guest'
    assert outputs[4]['hotel']['consent_confirmed'] is True
    assert outputs[6]['hotel']['guest_ready'] is True
    assert outputs[7]['hotel']['phase'] == 'awaiting_arrival_confirmation'
    assert outputs[7]['hotel']['arrival_confirmed'] is False
    assert outputs[8]['hotel']['phase'] == 'completed'
    assert outputs[-1]['hotel']['phase'] == 'completed'
    assert outputs[-1]['hotel']['arrival_confirmed'] is True
    assert [output['active_skill'] for output in outputs] == [
        None, 'frontdesk-reception', 'frontdesk-reception', 'guest-handoff', 'guest-handoff',
        'escort-assistance', 'escort-assistance', 'escort-assistance', 'escort-assistance',
        'escort-assistance', 'escort-assistance',
    ]
    assert all(outputs[index]['hotel'] is None for index in (0, 1, 3, 5))
    assert outputs[1]['loaded_skills'] == ['frontdesk-reception']
    assert outputs[5]['loaded_skills'] == ['frontdesk-reception', 'guest-handoff', 'escort-assistance']


def test_hotel_announcement_travels_the_assembled_runtime_without_advancing_the_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Prove the speaker is reachable through the hotel assembly, not just the workflow unit.

    The service tests pin the announcement semantics; this run shows the hotel
    scenario really registers the skill, so a spoken sentence lands in the hotel
    task database and leaves the reception phase untouched.
    """
    from hotel_agent.app import main

    monkeypatch.setattr('sys.stdin', io.StringIO(
        '/skill frontdesk-reception\n播报 大堂前台会合点在入口右侧\n/status\n'
    ))
    result = main(['--data-dir', str(tmp_path), '--thread-id', 'guest-announce'])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    outputs = [json.loads(line) for line in captured.out.splitlines()]
    announced = [item for item in outputs[1]['observations'] if item['tool'] == 'hotel_announce']
    assert [item['state'] for item in announced] == ['succeeded']
    assert announced[0]['result']['steps'][0]['output'] == {
        'message': '大堂前台会合点在入口右侧', 'spoken': True,
    }
    assert outputs[1]['hotel']['phase'] == 'reception'
    assert outputs[2]['hotel']['phase'] == 'reception'
    assert (tmp_path / 'hotel-simulator-tasks.sqlite3').exists()


def test_hotel_new_guest_isolated_and_robot_reservation_is_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from hotel_agent.app import main

    monkeypatch.setattr('sys.stdin', io.StringIO(
        '/skill frontdesk-reception\n入住 DEMO-001\n/skill guest-handoff\n/consent\n'
        '/guest guest-two\n/status\n/skill frontdesk-reception\n入住 DEMO-001\n'
        '/skill guest-handoff\n/consent\n/guest guest-one\n/status\n'
    ))
    result = main(['--data-dir', str(tmp_path), '--thread-id', 'guest-one'])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    outputs = [json.loads(line) for line in captured.out.splitlines()]
    assert len(outputs) == 12
    assert outputs[4]['session_id'] == 'guest-two'
    assert outputs[4]['hotel']['phase'] == 'reception'
    assert outputs[4]['hotel']['consent_confirmed'] is False
    assert outputs[4]['hotel']['room_id'] is None
    assert outputs[4]['loaded_skills'] == []
    assert outputs[9]['hotel']['phase'] == 'waiting_for_robot'
    assert outputs[9]['hotel']['runtime_task_id'] is None
    assert outputs[-1]['session_id'] == 'guest-one'
    assert outputs[-1]['hotel']['phase'] == 'waiting_for_guest'


async def test_hotel_ui_selection_is_model_free_and_filtered_to_enabled_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from hotel_agent.app import parse_args, run_application

    class NoModel:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            pytest.fail('Skill UI selection invoked the model')

    async def unexpected_refresh(workflow: Any, session_id: str) -> dict[str, Any]:
        pytest.fail('Skill UI selection queried hotel business state')

    monkeypatch.setattr('hotel_agent.service.HotelWorkflow.refresh', unexpected_refresh)
    old_checkpoint = tmp_path / 'hotel-skill-host-checkpoints.sqlite3'
    old_checkpoint.write_bytes(b'previous merged Skill checkpoint must remain untouched')
    monkeypatch.setattr('sys.stdin', io.StringIO(
        '/skills\n/skill frontdesk-reception\n/skill guest-handoff\n/skill\n'
        '/skill frontdesk-reception extra\n/skills extra\n/skills\n'
    ))
    config = parse_args([
        '--data-dir', str(tmp_path), '--thread-id', 'guest',
        '--skill', 'frontdesk-reception',
    ])
    await run_application(config, NoModel())
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(outputs) == 7
    for output in outputs:
        assert [entry['name'] for entry in output['skill_catalog']] == ['frontdesk-reception']
        assert output['hotel'] is None
    assert outputs[0]['loaded_skills'] == []
    assert all(output['active_skill'] == 'frontdesk-reception' for output in outputs[1:])
    assert all(output['loaded_skills'] == ['frontdesk-reception'] for output in outputs[1:])
    assert outputs[1]['loaded_resources'] == []
    for output in outputs[2:6]:
        assert output['observations'] == []
        assert 'error' in json.loads(output['reply'])
    assert 'guest-handoff' not in outputs[6]['loaded_skills']
    with closing(sqlite3.connect(tmp_path / 'hotel.sqlite3')) as database:
        assert database.execute('SELECT COUNT(*) FROM hotel_services').fetchone()[0] == 0
    assert old_checkpoint.read_bytes() == b'previous merged Skill checkpoint must remain untouched'
    assert (tmp_path / 'hotel-selectable-skills-checkpoints.sqlite3').exists()


@pytest.mark.parametrize('prompt', (
    '/skills', '/skill frontdesk-reception', '/skill unknown-skill',
    '/skill', '/skill frontdesk-reception extra', '/skills extra',
))
async def test_hotel_one_shot_skill_ui_never_queries_or_creates_a_guest_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], prompt: str,
) -> None:
    from hotel_agent.app import parse_args, run_application
    from hotel_agent.service import HotelWorkflow
    from robot_agent_core.errors import AgentConfigurationError

    class NoModel:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            pytest.fail('Skill UI selection invoked the model')

    refreshes: list[str] = []
    original_refresh = HotelWorkflow.refresh

    async def observed_refresh(workflow: HotelWorkflow, session_id: str) -> dict[str, Any]:
        refreshes.append(session_id)
        return await original_refresh(workflow, session_id)

    monkeypatch.setattr('hotel_agent.service.HotelWorkflow.refresh', observed_refresh)
    config = parse_args([
        '--data-dir', str(tmp_path), '--thread-id', 'guest', '--prompt', prompt,
    ])
    if prompt in {'/skills', '/skill frontdesk-reception'}:
        await run_application(config, NoModel())
        assert json.loads(capsys.readouterr().out)['hotel'] is None
    else:
        with pytest.raises(AgentConfigurationError):
            await run_application(config, NoModel())
        assert not capsys.readouterr().out
    assert refreshes == []
    with closing(sqlite3.connect(tmp_path / 'hotel.sqlite3')) as database:
        assert database.execute('SELECT COUNT(*) FROM hotel_services').fetchone()[0] == 0


def test_hotel_cross_skill_confirmation_records_input_without_running_other_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from hotel_agent.app import main

    monkeypatch.setattr('sys.stdin', io.StringIO(
        '/skill frontdesk-reception\n入住 DEMO-001\n/consent\n'
        '/skill guest-handoff\n分派管家\n/start\n/skill escort-assistance\n/status\n'
    ))
    assert main(['--data-dir', str(tmp_path), '--thread-id', 'guest']) == 0
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert outputs[2]['active_skill'] == 'frontdesk-reception'
    assert outputs[2]['hotel']['consent_confirmed'] is True
    assert outputs[2]['hotel']['phase'] == 'waiting_for_robot'
    assert outputs[2]['hotel']['robot_id'] is None
    assert '/skill guest-handoff' in outputs[2]['reply']
    assert outputs[4]['hotel']['phase'] == 'waiting_for_guest'
    assert outputs[5]['active_skill'] == 'guest-handoff'
    assert outputs[5]['hotel']['guest_ready'] is True
    assert outputs[5]['hotel']['runtime_task_id'] is None
    assert '/skill escort-assistance' in outputs[5]['reply']
    assert outputs[6]['hotel'] is None
    assert outputs[7]['hotel']['phase'] == 'waiting_for_guest'
    for field in ('phase', 'runtime_task_id', 'consent_confirmed', 'guest_ready', 'arrival_confirmed'):
        assert outputs[7]['hotel'][field] == outputs[5]['hotel'][field]


async def test_pending_checkpoint_rejects_confirmation_but_cancel_has_priority(tmp_path: Path) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from robot_agent_core.errors import AgentConfigurationError
    from robot_agent_core.graph import AgentContext, build_graph
    from hotel_agent.application import invoke_hotel_turn
    from robot_agent_core.shell import invoke_turn
    from robot_agent_core.skillkit.catalog import SkillRegistry

    class Model:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            return AIMessage(content='', tool_calls=[{
                'name': 'hotel_start_escort', 'args': {}, 'id': 'pending-start',
            }])

    class Tools:
        schemas = hotel_schemas()

        async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Any:
            pytest.fail('pending graph was invoked by control handling')

    class Workflow:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.phase = 'waiting_for_guest'

        async def record_guest_event(self, session_id: str, event: str) -> None:
            self.events.append(event)

        async def cancel(self, session_id: str, *, operation_id: str) -> dict[str, Any]:
            self.phase = 'canceled'
            return await self.snapshot(session_id)

        async def snapshot(self, session_id: str) -> dict[str, Any]:
            return {'phase': self.phase, 'session_id': session_id}

        async def refresh(self, session_id: str) -> dict[str, Any]:
            return await self.snapshot(session_id)

    workflow = Workflow()
    context = AgentContext(model=Model(), tools=Tools(), skills=SkillRegistry.bundled('hotel_agent'))
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'paused.sqlite3')) as saver:
        paused = build_graph(checkpointer=saver, interrupt_before=['execute'])
        await invoke_turn(paused, context, thread_id='one', runtime_id='hotel-runtime', prompt='start')
        graph = build_graph(checkpointer=saver)
        for prompt in ('/consent', '/start', '/arrived', '/skill frontdesk-reception'):
            with pytest.raises(AgentConfigurationError, match='pending|resume'):
                await invoke_hotel_turn(
                    graph, context, workflow, session_id='one', runtime_id='hotel-runtime', prompt=prompt,
                )
        assert workflow.events == []
        listing = await invoke_hotel_turn(
            graph, context, workflow, session_id='one', runtime_id='hotel-runtime', prompt='/skills',
        )
        assert 'frontdesk-reception' in listing['messages'][-1].content
        assert workflow.events == []
        assert (await graph.aget_state({'configurable': {'thread_id': 'one'}})).next
        result = await invoke_hotel_turn(
            graph, context, workflow, session_id='one', runtime_id='hotel-runtime', prompt='/cancel',
        )
        assert 'canceled' in result['messages'][-1].content
        assert (await graph.aget_state({'configurable': {'thread_id': 'one'}})).next


async def test_hotel_model_tools_cannot_create_guest_confirmations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from hotel_agent.app import parse_args, run_application

    class ForgingModel:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            conversation = [message for message in messages if not isinstance(message, SystemMessage)]
            last = conversation[-1]
            if isinstance(last, ToolMessage):
                return AIMessage(content='result received')
            prompt = str(next(message.content for message in reversed(conversation) if isinstance(message, HumanMessage)))
            available = {schema['function']['name'] for schema in tools}
            assert 'hotel_confirm_reception' in available
            if prompt == 'verify':
                name, args = 'hotel_confirm_reception', {'receipt_code': 'DEMO-001'}
            elif prompt == 'fake-consent':
                name, args = 'hotel_assign_butler', {'consent_confirmed': True}
            else:
                name, args = 'hotel_start_escort', {'guest_ready': True, 'arrival_confirmed': True}
            return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': prompt}])

    monkeypatch.setattr('sys.stdin', io.StringIO(
        '/skill frontdesk-reception\nverify\nfake-consent\nfake-ready-and-arrival\n/status\n'
    ))
    config = parse_args(['--data-dir', str(tmp_path), '--thread-id', 'guest'])
    await run_application(config, ForgingModel())
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(outputs) == 5
    for output in outputs[2:4]:
        assert output['observations'][0]['error']['code'] == 'HOTEL_WORKFLOW_REJECTED'
        assert output['observations'][0]['requires_attention'] is True
    final = outputs[-1]['hotel']
    assert final['phase'] == 'awaiting_consent'
    assert final['consent_confirmed'] is False
    assert final['guest_ready'] is False
    assert final['arrival_confirmed'] is False
    assert final['runtime_task_id'] is None


@pytest.mark.parametrize('pending_tool', ('load_skill', 'run_skill_script'))
def test_hotel_resume_executes_its_saved_skill_host_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], pending_tool: str,
) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from hotel_agent.app import create_model, main, parse_args
    from hotel_agent.service import HotelWorkflow
    from hotel_agent.tools import HotelTools
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent_core.shell import invoke_turn
    from robot_agent_core.skillkit.catalog import SkillRegistry
    from robot_agent_runtime.adapters.simulator import Simulator
    from robot_agent_runtime.repositories.sqlite import SQLiteTaskRepository
    from robot_agent_runtime.sdk import RobotRuntime

    config = parse_args(['--data-dir', str(tmp_path), '--thread-id', 'guest'])

    class SelectThenDemo:
        async def decide(self, messages: Any, tools: Any) -> AIMessage:
            if not any(
                isinstance(message, ToolMessage) and message.name == 'load_skill'
                for message in messages
            ):
                return AIMessage(content='', tool_calls=[{
                    'name': 'load_skill', 'args': {'name': 'frontdesk-reception'}, 'id': 'explicit-model-load',
                }])
            return await create_model(config).decide(messages, tools)

    async def save_pending_action() -> None:
        simulator = Simulator()
        runtime = RobotRuntime(SQLiteTaskRepository(str(config.task_database)), simulator.skills)
        workflow = HotelWorkflow(str(tmp_path / 'hotel.sqlite3'), runtime)
        try:
            await runtime.start()
            await workflow.start()
            tools = HotelTools(workflow, 'guest')
            from hotel_agent.script_bindings import build_hotel_script_runner

            registry = SkillRegistry.bundled('hotel_agent')
            context = AgentContext(
                model=SelectThenDemo(), tools=tools, skills=registry, guidance=tools,
                scripts=build_hotel_script_runner(registry, tools),
            )
            async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'hotel-selectable-skills-checkpoints.sqlite3')) as saver:
                paused = build_graph(checkpointer=saver, interrupt_before=['execute'])
                await invoke_turn(
                    paused, context, thread_id='guest', runtime_id=config.runtime_id, prompt='入住 DEMO-001',
                )
                pending = await paused.aget_state({'configurable': {'thread_id': 'guest'}})
                assert pending.values['pending'][0]['name'] == 'load_skill'
                while pending.values['pending'][0]['name'] != pending_tool:
                    await invoke_turn(
                        paused, context, thread_id='guest', runtime_id=config.runtime_id, prompt=None,
                    )
                    pending = await paused.aget_state({'configurable': {'thread_id': 'guest'}})
                assert (await workflow.snapshot('guest'))['phase'] == 'reception'
        finally:
            try:
                await workflow.close()
            finally:
                await runtime.close()

    asyncio.run(save_pending_action())
    result = main(['--data-dir', str(tmp_path), '--thread-id', 'guest', '--resume'])
    captured = capsys.readouterr()
    assert result == 0, captured.err
    output = json.loads(captured.out)
    assert output['hotel']['phase'] == 'awaiting_consent'
    assert output['observations'][0]['execution']['path'] == 'scripts/query_reception.py'
    assert output['hotel']['runtime_task_id'] is None
