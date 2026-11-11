import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_config_extraction_preserves_task_paths_and_runtime_identities(tmp_path: Path) -> None:
    from robot_agent.app import parse_args
    from robot_agent.profiles import ROBOT_PROFILE
    from robot_agent_core.config import AppConfig
    from robot_agent_core.configuration import rebuild_config

    config = parse_args(['--data-dir', str(tmp_path)])
    assert isinstance(config, AppConfig)
    assert config.profile is ROBOT_PROFILE
    assert config.task_database == tmp_path / 'simulator-tasks.sqlite3'
    identity = json.dumps(['simulator', str(tmp_path / 'simulator-tasks.sqlite3'), None, None], separators=(',', ':'))
    assert config.runtime_id == hashlib.sha256(identity.encode()).hexdigest()
    cc = parse_args(['--backend', 'cc', '--api-id', 'robot-one', '--data-dir', str(tmp_path)])
    suffix = hashlib.sha256(json.dumps(['robot-one', None], separators=(',', ':')).encode()).hexdigest()[:16]
    assert cc.task_database == tmp_path / f'cc-{suffix}-tasks.sqlite3'
    assert rebuild_config(cc, address='another-robot').task_database != cc.task_database


def test_profile_owns_backends_databases_and_identity_tag() -> None:
    from robot_agent.profiles import ROBOT_PROFILE
    from robot_agent_core.errors import AgentConfigurationError

    assert ROBOT_PROFILE.name == 'robot'
    assert ROBOT_PROFILE.allowed_backends == frozenset({'simulator', 'cc'})
    assert ROBOT_PROFILE.simulator_database == 'simulator-tasks.sqlite3'
    assert ROBOT_PROFILE.runtime_identity_tag is None
    assert ROBOT_PROFILE.checkpoint_database == 'checkpoints.sqlite3'
    assert ROBOT_PROFILE.business_database is None
    assert ROBOT_PROFILE.mcp_checkpoint_database is None
    with pytest.raises(AgentConfigurationError, match='supports only these backends'):
        ROBOT_PROFILE.validate_backend('mcp')


def test_cli_offers_only_the_backends_the_profile_allows(tmp_path: Path) -> None:
    from robot_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError

    for arguments in (['--backend', 'mcp'], ['--backend', 'other']):
        with pytest.raises(AgentConfigurationError):
            parse_args([*arguments, '--data-dir', str(tmp_path)])
    with pytest.raises(AgentConfigurationError):
        parse_args(['--runtime-socket', '/runtime.sock', '--data-dir', str(tmp_path)])
    assert not (tmp_path / 'simulator-tasks.sqlite3').exists()


def test_application_module_can_be_imported_without_loading_the_cli() -> None:
    probe = subprocess.run([
        sys.executable, '-c',
        'import sys; import robot_agent.application; assert "robot_agent.app" not in sys.modules',
    ], text=True, capture_output=True, timeout=10)
    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize('module', ('robot_agent_core.models', 'robot_agent.robot_tools', 'robot_agent_core.skillkit.scripts'))
def test_generic_modules_do_not_load_application_composition(module: str) -> None:
    probe = subprocess.run([
        sys.executable, '-c',
        'import importlib, sys; importlib.import_module(sys.argv[1]); '
        'loaded = [name for name in sys.modules if name in ('
        '"robot_agent_core.configuration", "robot_agent.profiles", "robot_agent.application") '
        'or name.startswith("robot_agent.profiles")]; '
        'assert not loaded, loaded',
        module,
    ], text=True, capture_output=True, timeout=10)
    assert probe.returncode == 0, f'{module}: {probe.stderr}'
