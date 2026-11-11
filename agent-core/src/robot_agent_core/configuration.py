"""Resolve storage policy at application assembly and reject stale snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from robot_agent_core.config import AppConfig
from robot_agent_core.errors import AgentConfigurationError


@dataclass(frozen=True)
class StorageProfile:
    """The durable storage and backend policy that one agent application owns.

    An application ships this as a module constant and passes it to the resolver
    itself, so nothing can be assembled against a policy belonging to another
    agent and no command line parameter names a policy at all. ``name`` is part
    of persisted identity: it enters the runtime hash for both the simulator and
    the MCP backend, so changing it orphans every history that name produced.
    """

    name: str
    allowed_backends: frozenset[str]
    simulator_database: str
    runtime_identity_tag: str | None
    checkpoint_database: str
    business_database: str | None
    mcp_checkpoint_database: str | None = None
    mcp_business_database: str | None = None

    def validate_backend(self, backend: str) -> None:
        if backend not in self.allowed_backends:
            allowed = ', '.join(sorted(self.allowed_backends))
            raise AgentConfigurationError(f'{self.name} agent supports only these backends: {allowed}')


@dataclass(frozen=True)
class ResolvedStorage:
    data_dir: Path
    task_database: Path | None
    checkpoint_database: Path
    business_database: Path | None
    runtime_id: str
    runtime_socket: Path | None = None


_RESOLVED_FIELDS = ('task_database', 'checkpoint_database', 'business_database', 'runtime_id')


def resolve_storage(
    *, data_dir: Path, backend: str, api_id: str | None, address: str | None,
    profile: StorageProfile, runtime_socket: Path | None = None,
) -> ResolvedStorage:
    """Preserve database ownership and the persisted identity's exact encoding.

    A runtime is tied to its absolute task database and robot connection. The
    simulator's agent-specific tag keeps separate agents apart even when they
    share one data directory, while the CC filename hash stays independent of
    which agent assembled it.
    """
    profile.validate_backend(backend)
    data_dir = data_dir.expanduser().resolve()
    if backend == 'mcp':
        if runtime_socket is None:
            raise AgentConfigurationError('MCP backend requires --runtime-socket')
        if profile.mcp_checkpoint_database is None or profile.mcp_business_database is None:
            raise AgentConfigurationError('Agent profile has no MCP storage policy')
        runtime_socket = runtime_socket.expanduser().resolve()
        identity = json.dumps([backend, str(runtime_socket), profile.name], separators=(',', ':'))
        runtime_id = hashlib.sha256(identity.encode()).hexdigest()
        return ResolvedStorage(
            data_dir=data_dir, task_database=None,
            checkpoint_database=data_dir / profile.mcp_checkpoint_database.format(endpoint=runtime_id[:16]),
            business_database=data_dir / profile.mcp_business_database.format(endpoint=runtime_id[:16]),
            runtime_id=runtime_id, runtime_socket=runtime_socket,
        )
    if runtime_socket is not None:
        raise AgentConfigurationError('--runtime-socket requires the MCP backend')
    if backend == 'simulator':
        task_database = data_dir / profile.simulator_database
    else:
        identity = json.dumps([api_id, address], separators=(',', ':'))
        suffix = hashlib.sha256(identity.encode()).hexdigest()[:16]
        task_database = data_dir / f'cc-{suffix}-tasks.sqlite3'
    parts = [
        backend, str(task_database), api_id if backend == 'cc' else None,
        address if backend == 'cc' else None,
    ]
    if profile.runtime_identity_tag is not None:
        parts.append(profile.runtime_identity_tag)
    identity = json.dumps(parts, separators=(',', ':'))
    return ResolvedStorage(
        data_dir=data_dir, task_database=task_database,
        checkpoint_database=data_dir / profile.checkpoint_database,
        business_database=data_dir / profile.business_database if profile.business_database is not None else None,
        runtime_id=hashlib.sha256(identity.encode()).hexdigest(),
    )


def rebuild_config(config: AppConfig, **changes: Any) -> AppConfig:
    """Recompute owned storage after changing an identity input or the profile.

    The profile travels inside the snapshot, so a rebuilt configuration is
    resolved by the very policy it declares and can never be handed another
    agent's storage. Callers may change inputs, but cannot inject resolved paths
    or an identity that could reuse another robot's durable tasks. Ordinary
    non-identity changes may still use dataclasses.replace directly.
    """
    overrides = sorted(set(changes).intersection(_RESOLVED_FIELDS))
    if overrides:
        raise AgentConfigurationError('Cannot override resolved configuration fields', details={'fields': overrides})
    candidate = replace(config, **changes)
    storage = resolve_storage(
        data_dir=candidate.data_dir, backend=candidate.backend, api_id=candidate.api_id,
        address=candidate.address, profile=candidate.profile, runtime_socket=candidate.runtime_socket,
    )
    return replace(candidate, **asdict(storage))


def validate_config(config: AppConfig) -> None:
    """Fail before opening storage or native resources when outputs are stale.

    Reading a snapshot resolves nothing: its storage was derived from the profile
    it carries, and this check is what keeps that derivation honest after a
    caller hand-edits a resolved field or the connection it came from.
    """
    expected = resolve_storage(
        data_dir=config.data_dir, backend=config.backend, api_id=config.api_id,
        address=config.address, profile=config.profile, runtime_socket=config.runtime_socket,
    )
    stale = [name for name in ('data_dir', 'runtime_socket', *_RESOLVED_FIELDS)
             if getattr(config, name) != getattr(expected, name)]
    if stale:
        raise AgentConfigurationError(
            'Configuration storage is inconsistent; use rebuild_config after changing identity inputs',
            details={'fields': stale},
        )
