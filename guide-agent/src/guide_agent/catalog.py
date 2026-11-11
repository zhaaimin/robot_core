import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any

import yaml

from guide_agent.errors import GuideCatalogError, GuideError
from robot_agent_core.skillkit.catalog import SkillRegistry
from robot_agent_runtime.errors import AgentRuntimeError


_IDENTIFIER = re.compile(r'[a-z][a-z0-9_-]{0,63}')
_PATHS = ('references/destinations.yaml', 'references/introductions.md')


@dataclass(frozen=True)
class Destination:
    id: str
    name: str
    aliases: tuple[str, ...]
    map_target: str | None
    introduction: str
    details: str


def _text(value: Any, limit: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 and c not in '\n\r\t' for c in value)):
        raise GuideCatalogError('Destination text is blank, invalid or too large')
    return value.strip()


def _unique_yaml(text: str) -> Any:
    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader: UniqueLoader, node: yaml.MappingNode) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            if not isinstance(key, str) or key in result:
                raise GuideCatalogError('Destination YAML keys must be unique strings')
            result[key] = loader.construct_object(value_node, deep=True)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        return yaml.load(text, Loader=UniqueLoader)
    except (yaml.YAMLError, RecursionError, TypeError, ValueError) as error:
        raise GuideCatalogError('Destination YAML is invalid') from error


class DestinationCatalog:
    """Freeze configured facts so a confirmation cannot silently change its target.

    Only registry-contained resources are read. A second read rejects files
    changing during construction; later file edits require a new catalog and
    cannot alter an already confirmed tour's introduction.
    """

    def __init__(self, destinations: tuple[Destination, ...], fingerprint: str) -> None:
        self._destinations = {item.id: item for item in destinations}
        self._fingerprint = fingerprint

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @classmethod
    def from_registry(cls, registry: SkillRegistry, skill_name: str = 'venue-guide') -> 'DestinationCatalog':
        try:
            documents = [registry.read_resource(skill_name, path) for path in _PATHS]
            verified = [registry.read_resource(skill_name, path) for path in _PATHS]
        except (AgentRuntimeError, OSError) as error:
            raise GuideCatalogError('Guide resources cannot be read from the selected skill') from error
        if documents != verified:
            raise GuideCatalogError('Guide resources changed while loading')
        config = _unique_yaml(documents[0]['content'])
        if not isinstance(config, dict) or set(config) != {'destinations'}:
            raise GuideCatalogError('Guide configuration requires only a destinations list')
        records = config['destinations']
        if not isinstance(records, list) or not 1 <= len(records) <= 64:
            raise GuideCatalogError('Guide configuration requires 1 to 64 destinations')
        sections: dict[str, str] = {}
        current: str | None = None
        for line in documents[1]['content'].splitlines():
            if line.startswith('## '):
                current = line[3:].strip()
                if not _IDENTIFIER.fullmatch(current) or current in sections:
                    raise GuideCatalogError('Introduction sections require unique destination IDs')
                sections[current] = ''
            elif current is not None:
                sections[current] += line + '\n'
        destinations: list[Destination] = []
        identifiers: set[str] = set()
        names: set[str] = set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {'id', 'name', 'aliases', 'map_target', 'introduction'}:
                raise GuideCatalogError('Each destination must explicitly declare all guide fields')
            identifier = _text(record['id'], 64)
            if not _IDENTIFIER.fullmatch(identifier) or identifier in identifiers:
                raise GuideCatalogError('Destination IDs must be unique bounded identifiers')
            identifiers.add(identifier)
            name = _text(record['name'], 128)
            aliases = record['aliases']
            if not isinstance(aliases, list) or len(aliases) > 32:
                raise GuideCatalogError('Destination aliases must be a bounded list')
            normalized = tuple(_text(alias, 128) for alias in aliases)
            for label in (name, *normalized):
                key = label.casefold()
                if key in names:
                    raise GuideCatalogError('Destination names and aliases must be unambiguous')
                names.add(key)
            target = None if record['map_target'] is None else _text(record['map_target'], 512)
            detail = sections.get(identifier, '').strip()
            if len(detail) > 16000:
                raise GuideCatalogError('Destination details exceed the configured bound')
            destinations.append(Destination(identifier, name, normalized, target,
                                            _text(record['introduction'], 4000), detail))
        if set(sections) - identifiers:
            raise GuideCatalogError('Introduction references an unknown destination')
        fingerprint = hashlib.sha256(''.join(document['fingerprint'] for document in documents).encode()).hexdigest()
        return cls(tuple(destinations), fingerprint)

    def get(self, destination_id: str) -> Destination:
        if not isinstance(destination_id, str) or destination_id not in self._destinations:
            raise GuideError('Unknown configured destination')
        return self._destinations[destination_id]

    def describe(self, destination_id: str) -> dict[str, Any]:
        result = asdict(self.get(destination_id))
        result['aliases'] = list(result['aliases'])
        return result

    def search(self, query: str) -> list[dict[str, Any]]:
        if not isinstance(query, str) or len(query) > 256:
            raise GuideError('A bounded destination query is required')
        needle = query.strip().casefold()
        return [self.describe(item.id) for item in self._destinations.values()
                if any(needle in label.casefold() for label in (item.id, item.name, *item.aliases))]

    def navigation_target(self, destination_id: str, backend: str) -> str:
        destination = self.get(destination_id)
        if backend == 'simulator':
            return 'demo:' + destination.id
        if backend == 'mcp' and destination.map_target:
            return destination.map_target
        raise GuideError('This destination has no configured navigation target for the backend')

