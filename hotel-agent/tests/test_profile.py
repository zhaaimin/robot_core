import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest


def test_config_extraction_preserves_task_paths_and_runtime_identities(tmp_path: Path) -> None:
    from hotel_agent.app import parse_args
    from hotel_agent.profiles import HOTEL_PROFILE
    from robot_agent_core.config import AppConfig
    from robot_agent_core.configuration import rebuild_config

    config = parse_args(['--data-dir', str(tmp_path)])
    assert isinstance(config, AppConfig)
    assert config.profile is HOTEL_PROFILE
    assert config.task_database == tmp_path / 'hotel-simulator-tasks.sqlite3'
    identity = json.dumps(
        ['simulator', str(tmp_path / 'hotel-simulator-tasks.sqlite3'), None, None, 'hotel'], separators=(',', ':'),
    )
    assert config.runtime_id == hashlib.sha256(identity.encode()).hexdigest()
    assert config.business_database == tmp_path / 'hotel.sqlite3'
    moved = rebuild_config(config, data_dir=tmp_path / 'elsewhere')
    assert moved.runtime_id != config.runtime_id


def test_profile_owns_backends_databases_and_identity_tag() -> None:
    from hotel_agent.profiles import HOTEL_PROFILE
    from robot_agent_core.errors import AgentConfigurationError

    assert HOTEL_PROFILE.name == 'hotel'
    assert HOTEL_PROFILE.allowed_backends == frozenset({'simulator', 'mcp'})
    assert HOTEL_PROFILE.runtime_identity_tag == 'hotel'
    assert HOTEL_PROFILE.checkpoint_database == 'hotel-selectable-skills-checkpoints.sqlite3'
    assert HOTEL_PROFILE.business_database == 'hotel.sqlite3'
    assert HOTEL_PROFILE.mcp_checkpoint_database == 'hotel-mcp-{endpoint}-checkpoints.sqlite3'
    with pytest.raises(AgentConfigurationError, match='supports only these backends'):
        HOTEL_PROFILE.validate_backend('cc')


def test_cli_offers_only_the_backends_the_profile_allows(tmp_path: Path) -> None:
    from hotel_agent.app import parse_args
    from robot_agent_core.errors import AgentConfigurationError

    for arguments in (['--backend', 'cc'], ['--backend', 'other']):
        with pytest.raises(AgentConfigurationError):
            parse_args([*arguments, '--data-dir', str(tmp_path)])
    with pytest.raises(AgentConfigurationError, match='runtime-socket|runtime socket'):
        parse_args(['--backend', 'mcp', '--data-dir', str(tmp_path)])
    assert not (tmp_path / 'hotel.sqlite3').exists()


def test_a_profile_without_mcp_policy_cannot_reach_the_mcp_branch() -> None:
    from hotel_agent.profiles import HOTEL_PROFILE
    from robot_agent_core.configuration import resolve_storage
    from robot_agent_core.errors import AgentConfigurationError

    policy = replace(HOTEL_PROFILE, mcp_checkpoint_database=None)
    with pytest.raises(AgentConfigurationError, match='MCP storage policy'):
        resolve_storage(
            data_dir=Path('/tmp/hotel-policy'), backend='mcp', api_id=None, address=None,
            profile=policy, runtime_socket=Path('/runtime.sock'),
        )


def test_application_module_can_be_imported_without_loading_the_cli() -> None:
    probe = subprocess.run([
        sys.executable, '-c',
        'import sys; import hotel_agent.application; assert "hotel_agent.app" not in sys.modules',
    ], text=True, capture_output=True, timeout=10)
    assert probe.returncode == 0, probe.stderr
