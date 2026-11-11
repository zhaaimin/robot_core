from pathlib import Path

import pytest

from robot_agent_core.skillkit.catalog import SkillRegistry


def registry_at(root: Path, yaml_text: str, markdown: str = '## hall\n展厅详细事实。\n') -> SkillRegistry:
    skill = root / 'venue-guide'
    (skill / 'references').mkdir(parents=True, exist_ok=True)
    (skill / 'SKILL.md').write_text('---\nname: venue-guide\ndescription: 导览\n---\n地点资料。', encoding='utf-8')
    (skill / 'references/destinations.yaml').write_text(yaml_text, encoding='utf-8')
    (skill / 'references/introductions.md').write_text(markdown, encoding='utf-8')
    return SkillRegistry.from_directory(root)


VALID = '''destinations:
  - id: hall
    name: 演示展厅
    aliases: [展厅]
    map_target: null
    introduction: 这是演示展厅。
'''


def test_catalog_freezes_configured_facts_and_requires_real_mapping(tmp_path: Path) -> None:
    from guide_agent.catalog import DestinationCatalog
    from guide_agent.errors import GuideError

    registry = registry_at(tmp_path, VALID)
    catalog = DestinationCatalog.from_registry(registry)
    assert catalog.search('展厅')[0]['id'] == 'hall'
    assert catalog.describe('hall')['details'] == '展厅详细事实。'
    assert catalog.get('hall').aliases == ('展厅',)
    assert catalog.navigation_target('hall', 'simulator') == 'demo:hall'
    with pytest.raises(GuideError):
        catalog.navigation_target('hall', 'mcp')
    before = catalog.fingerprint
    (tmp_path / 'venue-guide/references/introductions.md').write_text('## hall\n修改了。', encoding='utf-8')
    assert catalog.describe('hall')['details'] == '展厅详细事实。'
    assert DestinationCatalog.from_registry(registry).fingerprint != before


@pytest.mark.parametrize('content,markdown', [
    (VALID.replace('name: 演示展厅', 'name: 演示展厅\n    name: 重复'), '## hall\n事实'),
    (VALID + VALID.removeprefix('destinations:\n'), '## hall\n事实'),
    (VALID.replace('aliases: [展厅]', 'aliases: [演示展厅]'), '## hall\n事实'),
    (VALID.replace('aliases: [展厅]', 'aliases: [1]'), '## hall\n事实'),
    (VALID.replace('map_target: null', 'map_target: .nan'), '## hall\n事实'),
    (VALID.replace('introduction: 这是演示展厅。', 'introduction: []'), '## hall\n事实'),
    (VALID, '## absent\n不属于目录'),
    (VALID, '## hall\n事实\n## hall\n重复'),
    (VALID, '## hall\n' + '大' * 16001),
])
def test_catalog_rejects_ambiguous_or_unbounded_configuration(tmp_path: Path, content: str, markdown: str) -> None:
    from guide_agent.catalog import DestinationCatalog
    from guide_agent.errors import GuideCatalogError

    with pytest.raises(GuideCatalogError):
        DestinationCatalog.from_registry(registry_at(tmp_path, content, markdown))


def test_catalog_keeps_search_ambiguity_and_explicit_mcp_target(tmp_path: Path) -> None:
    from guide_agent.catalog import DestinationCatalog

    content = VALID + '''  - id: desk
    name: 演示服务台
    aliases: [前台]
    map_target: building/front-desk
    introduction: 这是演示服务台。
'''
    catalog = DestinationCatalog.from_registry(registry_at(tmp_path, content))
    assert len(catalog.search('演示')) == 2
    assert catalog.navigation_target('desk', 'mcp') == 'building/front-desk'

