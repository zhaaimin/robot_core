from collections.abc import Mapping
from typing import Any

from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_runtime.errors import AgentRuntimeError


def verified_skill_documents(
    state: Mapping[str, Any], registry: SkillRegistry | None,
) -> dict[str, dict[str, Any]]:
    """Keep saved instructions authoritative while rejecting changed dependencies.

    Every loaded skill must remain visible and agree across its saved document,
    saved version and current registry. Validation never replaces a saved body
    or writes to checkpoint state; unloaded conversations need no registry.
    """
    documents = dict(state.get('skill_documents', {}))
    for name in state.get('loaded_skills', []):
        if registry is None:
            raise AgentConfigurationError('A loaded skill needs its original registry')
        try:
            current = registry.load(name)
        except AgentRuntimeError:
            raise AgentConfigurationError('Loaded skill is no longer available; start a new conversation') from None
        saved = documents.get(name)
        if (saved is None or saved.get('fingerprint') != current['fingerprint']
                or state.get('skill_versions', {}).get(name) != current['fingerprint']):
            raise AgentConfigurationError('Loaded skill version changed; do not resume saved actions with new instructions')
    return documents
