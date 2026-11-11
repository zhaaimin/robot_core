import hashlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from robot_agent.profiles import ROBOT_PROFILE


def test_config_import_has_no_composition_dependencies() -> None:
    probe = subprocess.run([
        sys.executable, '-c',
        'import sys; import robot_agent_core.config; '
        'assert "robot_agent_core.configuration" not in sys.modules; '
        'assert "robot_agent_core.errors" not in sys.modules; '
        'assert "robot_agent_core.models" not in sys.modules; '
        'assert not any(name.startswith("robot_agent_runtime") for name in sys.modules)',
    ], text=True, capture_output=True, timeout=10)
    assert probe.returncode == 0, probe.stderr


def test_app_config_stores_immutable_resolved_fields(tmp_path: Path) -> None:
    from robot_agent.app import parse_args
    from robot_agent.profiles import ROBOT_PROFILE
    from robot_agent_core.config import AppConfig

    config = parse_args(['--data-dir', str(tmp_path)])
    names = {field.name for field in fields(AppConfig)}
    assert {'task_database', 'checkpoint_database', 'business_database', 'runtime_id', 'profile'} <= names
    assert config.profile is ROBOT_PROFILE
    assert config.checkpoint_database == tmp_path / 'checkpoints.sqlite3'
    assert config.business_database is None
    with pytest.raises(FrozenInstanceError):
        config.runtime_id = 'changed'  # type: ignore[misc]


@pytest.mark.parametrize(('arguments', 'filename', 'runtime_id'), (
    ([], 'simulator-tasks.sqlite3', '20a6d6dd64edbef3df3a254b90843621ecd5a8ba78fad6ae897609565026d537'),
    (['--backend', 'cc', '--api-id', 'robot-one'], 'cc-4702c611392a633d-tasks.sqlite3',
     '774e0b9b47c83565d4862d13beb56ec3c2e558ee0f5c718a9268935c18ba8254'),
    (['--backend', 'cc', '--api-id', 'robot-one', '--address', '192.0.2.10'],
     'cc-a713a75a1a873066-tasks.sqlite3', 'dc017874865a4eaad6d13192b38d222357a4558e191b8ea5e85c69c7da190aa2'),
))
def test_original_storage_identity_bytes_are_preserved(
    arguments: list[str], filename: str, runtime_id: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent.app import parse_args

    monkeypatch.delenv('CC_API_ID', raising=False)
    monkeypatch.delenv('CC_API_ADDRESS', raising=False)
    config = parse_args([*arguments, '--data-dir', '/tmp/robot-core-identity-v1'])
    assert config.task_database == Path('/tmp/robot-core-identity-v1') / filename
    assert config.runtime_id == runtime_id


def test_resolver_normalizes_data_dir_and_preserves_unicode_json_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent.profiles import ROBOT_PROFILE
    from robot_agent_core.configuration import resolve_storage

    monkeypatch.setenv('HOME', str(tmp_path))
    storage = resolve_storage(
        data_dir=Path('~/data/../state'), backend='cc', api_id='机器人', address=None,
        profile=ROBOT_PROFILE,
    )
    data_dir = tmp_path / 'state'
    suffix = hashlib.sha256(b'["\\u673a\\u5668\\u4eba",null]').hexdigest()[:16]
    task_database = data_dir / f'cc-{suffix}-tasks.sqlite3'
    identity = json.dumps(['cc', str(task_database), '机器人', None], separators=(',', ':'))
    assert storage.data_dir == data_dir
    assert storage.task_database == task_database
    assert storage.checkpoint_database == data_dir / 'checkpoints.sqlite3'
    assert storage.business_database is None
    assert storage.runtime_id == hashlib.sha256(identity.encode()).hexdigest()


def test_snapshot_reads_do_not_reconsult_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import robot_agent.app as application

    from robot_agent.app import parse_args
    from robot_agent.profiles import ROBOT_PROFILE

    config = parse_args(['--data-dir', str(tmp_path)])
    expected = (config.task_database, config.checkpoint_database, config.business_database, config.runtime_id)
    monkeypatch.setattr(application, 'ROBOT_PROFILE', replace(
        ROBOT_PROFILE, simulator_database='other-tasks.sqlite3', checkpoint_database='other.sqlite3',
    ))
    assert (config.task_database, config.checkpoint_database, config.business_database, config.runtime_id) == expected


def test_policy_changes_do_not_mutate_existing_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import robot_agent.app as application

    from robot_agent.app import parse_args
    from robot_agent.profiles import ROBOT_PROFILE
    from robot_agent_core.configuration import rebuild_config

    original = parse_args(['--data-dir', str(tmp_path)])
    policy = replace(
        ROBOT_PROFILE, simulator_database='other-tasks.sqlite3', checkpoint_database='other-checkpoints.sqlite3',
        runtime_identity_tag='other-robot',
    )
    monkeypatch.setattr(application, 'ROBOT_PROFILE', policy)
    assert original.task_database == tmp_path / 'simulator-tasks.sqlite3'
    assert original.checkpoint_database == tmp_path / 'checkpoints.sqlite3'
    rebuilt = rebuild_config(original, profile=policy)
    assert rebuilt.task_database == tmp_path / 'other-tasks.sqlite3'
    assert rebuilt.checkpoint_database == tmp_path / 'other-checkpoints.sqlite3'
    assert rebuilt.runtime_id != original.runtime_id


def test_rebuild_resolves_changed_identity_and_preserves_other_fields(tmp_path: Path) -> None:
    from robot_agent.app import parse_args
    from robot_agent_core.configuration import rebuild_config, validate_config

    robot = parse_args(['--data-dir', str(tmp_path), '--thread-id', 'one'])
    normalized = rebuild_config(robot, data_dir=tmp_path / 'child' / '..')
    assert normalized.data_dir == tmp_path
    assert normalized.task_database == tmp_path / 'simulator-tasks.sqlite3'
    assert normalized.checkpoint_database == tmp_path / 'checkpoints.sqlite3'
    assert normalized.business_database is None
    assert normalized.thread_id == 'one'
    cc = rebuild_config(normalized, backend='cc', api_id='one', address='192.0.2.1')
    changed = rebuild_config(cc, address='192.0.2.2')
    assert changed.task_database != cc.task_database
    assert changed.runtime_id != cc.runtime_id
    validate_config(changed)
    validate_config(replace(changed, prompt='new turn', max_turns=3))


@pytest.mark.parametrize('name', ('task_database', 'checkpoint_database', 'business_database', 'runtime_id'))
def test_rebuild_rejects_overriding_resolved_outputs(tmp_path: Path, name: str) -> None:
    from robot_agent.app import parse_args
    from robot_agent_core.configuration import rebuild_config
    from robot_agent_core.errors import AgentConfigurationError

    config = parse_args(['--data-dir', str(tmp_path)])
    with pytest.raises(AgentConfigurationError, match='resolved|derived'):
        rebuild_config(config, **{name: getattr(config, name)})


@pytest.mark.parametrize('changes', (
    {'profile': replace(ROBOT_PROFILE, checkpoint_database='other-checkpoints.sqlite3')},
    {'backend': 'cc', 'api_id': 'robot-one'},
    {'data_dir': Path('relative-data')}, {'task_database': Path('/wrong-tasks.sqlite3')},
    {'checkpoint_database': Path('/wrong-checkpoints.sqlite3')},
    {'business_database': Path('/wrong-business.sqlite3')}, {'runtime_id': 'stale'},
))
def test_validation_rejects_stale_storage_outputs(tmp_path: Path, changes: dict[str, Any]) -> None:
    from robot_agent.app import parse_args
    from robot_agent.profiles import ROBOT_PROFILE
    from robot_agent_core.configuration import validate_config
    from robot_agent_core.errors import AgentConfigurationError

    original = parse_args(['--data-dir', str(tmp_path)])
    stale = replace(original, **changes)
    with pytest.raises(AgentConfigurationError, match='rebuild_config'):
        validate_config(stale)


def test_validation_rejects_a_changed_cc_connection(tmp_path: Path) -> None:
    from robot_agent.app import parse_args
    from robot_agent_core.configuration import validate_config
    from robot_agent_core.errors import AgentConfigurationError

    cc = parse_args(['--backend', 'cc', '--api-id', 'one', '--data-dir', str(tmp_path)])
    for config in (replace(cc, address='192.0.2.1'), replace(cc, api_id='two')):
        with pytest.raises(AgentConfigurationError, match='rebuild_config'):
            validate_config(config)


async def test_application_rejects_stale_config_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import robot_agent.app as application

    from robot_agent.app import parse_args, run_application
    from robot_agent_core.errors import AgentConfigurationError

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('stale configuration reached application dispatch')

    monkeypatch.setattr(application, 'run_robot_application', forbidden)
    config = parse_args(['--data-dir', str(tmp_path)])
    with pytest.raises(AgentConfigurationError, match='rebuild_config'):
        await run_application(replace(config, data_dir=tmp_path / 'new'), object())
    assert not (tmp_path / 'new').exists()


def test_cc_connection_rejects_stale_config_before_native_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent.app import open_cc_connection, parse_args
    from robot_agent_core.errors import AgentConfigurationError

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('stale configuration constructed a native connection')

    def forbidden_module(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('stale configuration constructed a native connection')

    monkeypatch.setitem(sys.modules, 'ubt_robot', SimpleNamespace(Config=forbidden_module, Api=forbidden_module))
    config = parse_args(['--backend', 'cc', '--api-id', 'one', '--data-dir', str(tmp_path)])
    with pytest.raises(AgentConfigurationError, match='rebuild_config'):
        with open_cc_connection(replace(config, address='192.0.2.1')):
            pytest.fail('stale connection was opened')


async def test_robot_runtime_rejects_stale_config_before_repository_or_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent import application
    from robot_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('stale configuration constructed a repository or runtime')

    config = parse_args(['--data-dir', str(tmp_path)])
    monkeypatch.setattr(application, 'SQLiteTaskRepository', forbidden)
    monkeypatch.setattr(application, 'RobotRuntime', forbidden)
    with pytest.raises(AgentConfigurationError, match='rebuild_config'):
        async with application.open_runtime(replace(config, data_dir=tmp_path / 'new')):
            pytest.fail('stale runtime was opened')


async def test_application_rejects_stale_config_before_storage_or_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robot_agent import application
    from robot_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('stale configuration opened storage or runtime')

    config = parse_args(['--data-dir', str(tmp_path)])
    monkeypatch.setattr(application, 'SQLiteTaskRepository', forbidden)
    monkeypatch.setattr(application, 'RobotRuntime', forbidden)
    monkeypatch.setattr(application.AsyncSqliteSaver, 'from_conn_string', forbidden)
    with pytest.raises(AgentConfigurationError, match='rebuild_config'):
        await application.run_robot_application(replace(config, data_dir=tmp_path / 'new'), object())
    assert not (tmp_path / 'new').exists()


def another_agent_snapshot(tmp_path: Path) -> Any:
    """Build a snapshot resolved by a profile this application does not own."""
    from robot_agent.app import parse_args
    from robot_agent.profiles import ROBOT_PROFILE
    from robot_agent_core.configuration import resolve_storage

    foreign = replace(
        ROBOT_PROFILE, name='hotel', simulator_database='hotel-simulator-tasks.sqlite3',
        runtime_identity_tag='hotel', checkpoint_database='hotel-checkpoints.sqlite3',
        business_database='hotel.sqlite3',
    )
    storage = resolve_storage(
        data_dir=tmp_path, backend='simulator', api_id=None, address=None, profile=foreign,
    )
    return replace(parse_args(['--data-dir', str(tmp_path)]), profile=foreign, **asdict(storage))


@pytest.mark.parametrize('entry', ('application', 'runtime'))
async def test_application_rejects_another_agents_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str,
) -> None:
    from robot_agent import application
    from robot_agent_core.errors import AgentConfigurationError

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('another agent configuration opened storage or runtime')

    config = another_agent_snapshot(tmp_path / 'new')
    monkeypatch.setattr(application, 'SQLiteTaskRepository', forbidden)
    monkeypatch.setattr(application, 'RobotRuntime', forbidden)
    monkeypatch.setattr(application.AsyncSqliteSaver, 'from_conn_string', forbidden)
    with pytest.raises(AgentConfigurationError, match='profile'):
        if entry == 'runtime':
            async with application.open_runtime(config):
                pytest.fail('another agent runtime was opened')
        else:
            await application.run_robot_application(config, object())
    assert not (tmp_path / 'new').exists()


async def test_application_consumes_the_storage_policy_it_ships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import robot_agent.app as application

    from robot_agent.app import parse_args, run_application
    from robot_agent.profiles import ROBOT_PROFILE

    class NoModel:
        async def decide(self, messages: Any, tools: Any) -> Any:
            pytest.fail('Skill listing invoked the model')

    catalog = tmp_path / 'skills'
    skill = catalog / 'example-skill'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        '---\nname: example-skill\ndescription: Use when listing a catalog.\n---\n'
        'Answer from supplied facts.\n',
        encoding='utf-8',
    )
    policy = replace(ROBOT_PROFILE, checkpoint_database='selected-checkpoints.sqlite3')
    monkeypatch.setattr(application, 'ROBOT_PROFILE', policy)
    config = parse_args([
        '--data-dir', str(tmp_path), '--prompt', '/skills', '--skills-dir', str(catalog),
        '--skill', 'example-skill',
    ])
    await run_application(config, NoModel())
    assert (tmp_path / 'selected-checkpoints.sqlite3').is_file()
    assert not (tmp_path / 'checkpoints.sqlite3').exists()
    capsys.readouterr()
