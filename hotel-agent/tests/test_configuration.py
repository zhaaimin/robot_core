import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from hotel_agent.profiles import HOTEL_PROFILE


def test_original_storage_identity_bytes_are_preserved() -> None:
    from hotel_agent.app import parse_args

    config = parse_args(['--data-dir', '/tmp/robot-core-identity-v1'])
    assert config.task_database == Path('/tmp/robot-core-identity-v1') / 'hotel-simulator-tasks.sqlite3'
    assert config.runtime_id == '4e9ed74614d3f8ef460d2863e8b69e041fa88cfe3b60789d2bffaf477276edda'


def test_snapshot_reads_do_not_reconsult_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hotel_agent.app as application

    from hotel_agent.app import parse_args
    from hotel_agent.profiles import HOTEL_PROFILE

    config = parse_args(['--data-dir', str(tmp_path)])
    expected = (config.task_database, config.checkpoint_database, config.business_database, config.runtime_id)
    monkeypatch.setattr(application, 'HOTEL_PROFILE', replace(
        HOTEL_PROFILE, simulator_database='other-tasks.sqlite3', business_database='other.sqlite3',
    ))
    assert (config.task_database, config.checkpoint_database, config.business_database, config.runtime_id) == expected


@pytest.mark.parametrize('changes', (
    {'business_database': Path('/wrong-business.sqlite3')},
    {'checkpoint_database': Path('/wrong-checkpoints.sqlite3')},
    {'task_database': Path('/wrong-tasks.sqlite3')},
    {'runtime_id': 'stale'},
    {'profile': replace(HOTEL_PROFILE, checkpoint_database='other.sqlite3')},
))
def test_validation_rejects_stale_storage_outputs(tmp_path: Path, changes: dict[str, Any]) -> None:
    from hotel_agent.app import parse_args
    from robot_agent_core.configuration import validate_config
    from robot_agent_core.errors import AgentConfigurationError

    with pytest.raises(AgentConfigurationError, match='rebuild_config'):
        validate_config(replace(parse_args(['--data-dir', str(tmp_path)]), **changes))


async def test_hotel_requires_a_business_database_even_if_policy_omits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hotel_agent.app as application
    from hotel_agent import application as hotel

    from hotel_agent.app import parse_args, run_application
    from hotel_agent.profiles import HOTEL_PROFILE
    from robot_agent_core.errors import AgentConfigurationError

    policy = replace(HOTEL_PROFILE, business_database=None)
    monkeypatch.setattr(application, 'HOTEL_PROFILE', policy)
    config = parse_args(['--data-dir', str(tmp_path / 'new')])

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('hotel without business storage constructed runtime storage')

    monkeypatch.setattr(hotel, 'SQLiteTaskRepository', forbidden)
    for run in (run_application, hotel.run_hotel_application):
        with pytest.raises(AgentConfigurationError, match='business database'):
            await run(config, object())
    assert not (tmp_path / 'new').exists()


async def test_application_rejects_stale_config_before_storage_or_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hotel_agent import application as hotel
    from hotel_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('stale configuration opened storage or runtime')

    config = parse_args(['--data-dir', str(tmp_path)])
    monkeypatch.setattr(hotel, 'SQLiteTaskRepository', forbidden)
    monkeypatch.setattr(hotel, 'RobotRuntime', forbidden)
    monkeypatch.setattr(hotel.AsyncSqliteSaver, 'from_conn_string', forbidden)
    with pytest.raises(AgentConfigurationError, match='rebuild_config'):
        await hotel.run_hotel_application(replace(config, data_dir=tmp_path / 'new'), object())
    assert not (tmp_path / 'new').exists()


async def test_application_rejects_another_agents_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hotel_agent import application as hotel
    from hotel_agent.app import parse_args
    from hotel_agent.profiles import HOTEL_PROFILE
    from robot_agent_core.configuration import resolve_storage
    from robot_agent_core.errors import AgentConfigurationError

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('another agent configuration opened storage or runtime')

    foreign = replace(
        HOTEL_PROFILE, name='robot', simulator_database='simulator-tasks.sqlite3', runtime_identity_tag=None,
        checkpoint_database='checkpoints.sqlite3', business_database=None,
    )
    storage = resolve_storage(
        data_dir=tmp_path / 'new', backend='simulator', api_id=None, address=None, profile=foreign,
    )
    config = replace(parse_args(['--data-dir', str(tmp_path)]), profile=foreign, **asdict(storage))
    monkeypatch.setattr(hotel, 'SQLiteTaskRepository', forbidden)
    monkeypatch.setattr(hotel, 'RobotRuntime', forbidden)
    monkeypatch.setattr(hotel.AsyncSqliteSaver, 'from_conn_string', forbidden)
    with pytest.raises(AgentConfigurationError, match='profile'):
        await hotel.run_hotel_application(config, object())
    assert not (tmp_path / 'new').exists()


async def test_application_consumes_the_storage_policy_it_ships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import hotel_agent.app as application

    from hotel_agent.app import parse_args, run_application
    from hotel_agent.profiles import HOTEL_PROFILE

    class NoModel:
        async def decide(self, messages: Any, tools: Any) -> Any:
            pytest.fail('Skill listing invoked the model')

    policy = replace(
        HOTEL_PROFILE, checkpoint_database='selected-checkpoints.sqlite3',
        business_database='selected-business.sqlite3',
    )
    monkeypatch.setattr(application, 'HOTEL_PROFILE', policy)
    config = parse_args([
        '--data-dir', str(tmp_path), '--prompt', '/skills', '--skill', 'frontdesk-reception',
    ])
    await run_application(config, NoModel())
    assert (tmp_path / 'selected-checkpoints.sqlite3').is_file()
    assert (tmp_path / 'selected-business.sqlite3').is_file()
    assert not (tmp_path / 'hotel.sqlite3').exists()
    assert not (tmp_path / 'hotel-selectable-skills-checkpoints.sqlite3').exists()
    capsys.readouterr()
