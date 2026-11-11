import hashlib
import tempfile
import zipfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

import pytest

from robot_agent_core.errors import AgentConfigurationError, AgentToolNotAllowed
from robot_agent_core.skillkit.catalog import SkillRegistry


@contextmanager
def skill_tree() -> Iterator[tuple[Path, Path]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "skills"
        folder = root / "alpha"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Use when testing resources.\n---\nLoad details as needed.\n",
            encoding="utf-8",
        )
        yield root, folder


def resource(folder: Path, path: str, content: str = "Reference details.") -> Path:
    target = folder / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_resources_inventory_sorted_paths_without_loading_or_running_files() -> None:
    with skill_tree() as (root, folder):
        resource(folder, "references/nested/details.md")
        resource(folder, "scripts/helper.py", "raise AssertionError('must never run on read')")
        resource(folder, "assets/template.json", '{"answer": 42}')
        resource(folder, "private.txt", "not exposed")
        resource(folder, "references/large.bin").write_bytes(b"\x00" * 65537)
        registry = SkillRegistry.from_directory(root)
        expected = ["assets/template.json", "references/large.bin", "references/nested/details.md",
                    "scripts/helper.py"]
        assert registry.resources("alpha") == expected
        registry.resources("alpha").clear()
        assert registry.resources("alpha") == expected
        assert set(registry.load("alpha")) == {"name", "description", "instructions", "fingerprint"}
        assert "Reference details." not in str(registry.catalog())


def test_read_resource_returns_live_utf8_text_and_its_content_fingerprint() -> None:
    with skill_tree() as (root, folder):
        path = resource(folder, "references/details.md", "原始资料\n")
        registry = SkillRegistry.from_directory(root)
        assert registry.read_resource("alpha", "references/details.md") == {
            "name": "alpha", "path": "references/details.md", "content": "原始资料\n",
            "fingerprint": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        path.write_text("Updated facts.", encoding="utf-8")
        updated = registry.read_resource("alpha", "references/details.md")
        assert updated["content"] == "Updated facts."
        assert updated["fingerprint"] == hashlib.sha256(path.read_bytes()).hexdigest()
        updated["content"] = "mutated"
        assert registry.read_resource("alpha", "references/details.md")["content"] == "Updated facts."


def test_reading_script_source_is_inert() -> None:
    with skill_tree() as (root, folder):
        source = "raise AssertionError('read must not import or execute this')\n"
        resource(folder, "scripts/helper.py", source)
        registry = SkillRegistry.from_directory(root)
        assert registry.read_resource("alpha", "scripts/helper.py")["content"] == source


def test_resource_names_and_paths_are_scoped_not_filesystem_or_url_inputs() -> None:
    with skill_tree() as (root, folder):
        resource(folder, "references/details.md")
        resource(folder, "references/%252e%252e.txt", "literal encoded filename")
        registry = SkillRegistry.from_directory(root)
        for name in ("missing", "../alpha", "/alpha", "alpha\\x", "alpha\x00"):
            with pytest.raises(AgentToolNotAllowed):
                registry.resources(name)
            with pytest.raises(AgentToolNotAllowed):
                registry.read_resource(name, "references/details.md")
        for path in (
            "", "/references/details.md", "../alpha/SKILL.md", "SKILL.md", "private.txt",
            "references", "references/../SKILL.md", "references/./details.md",
            "references//details.md", "references/details.md/", "references\\details.md",
            "references/details.md\x00", "references/missing.md", "scripts/../../private.txt",
            "references/%2e%2e/SKILL.md", "references/%252e%252e/SKILL.md",
        ):
            with pytest.raises(AgentToolNotAllowed):
                registry.read_resource("alpha", path)
        assert registry.read_resource("alpha", "references/%252e%252e.txt")["content"] == (
            "literal encoded filename"
        )


def test_resource_inventory_cannot_silently_gain_new_paths() -> None:
    with skill_tree() as (root, folder):
        registry = SkillRegistry.from_directory(root)
        resource(folder, "references/added.md")
        assert registry.resources("alpha") == []
        with pytest.raises(AgentToolNotAllowed):
            registry.read_resource("alpha", "references/added.md")


def test_resource_inventory_ignores_bytecode_and_hidden_build_artifacts() -> None:
    with skill_tree() as (root, folder):
        resource(folder, "scripts/helper.py", "# readable source")
        ignored = (
            "scripts/__pycache__/helper.cpython-312.pyc", "scripts/helper.pyc", "scripts/helper.pyo",
            "references/.DS_Store", "assets/.cache/build.txt", "scripts/.hidden.py",
        )
        for path in ignored:
            resource(folder, path)
        registry = SkillRegistry.from_directory(root)
        assert registry.resources("alpha") == ["scripts/helper.py"]
        for path in ignored:
            with pytest.raises(AgentToolNotAllowed):
                registry.read_resource("alpha", path)


def test_resource_symlink_escape_is_rejected_during_inventory() -> None:
    with skill_tree() as (root, folder):
        outside = root.parent / "private.md"
        outside.write_text("private", encoding="utf-8")
        (folder / "references").mkdir()
        (folder / "references" / "leak.md").symlink_to(outside)
        with pytest.raises(AgentConfigurationError):
            SkillRegistry.from_directory(root)


def test_resource_directory_symlink_cannot_escape_into_another_skill() -> None:
    with skill_tree() as (root, folder):
        other = root / "beta"
        other.mkdir()
        (other / "SKILL.md").write_text(
            "---\nname: beta\ndescription: Use when testing another skill.\n---\nBody.\n",
            encoding="utf-8",
        )
        (other / "private.md").write_text("private", encoding="utf-8")
        (folder / "references").symlink_to(other, target_is_directory=True)
        with pytest.raises(AgentConfigurationError):
            SkillRegistry.from_directory(root)


def test_resource_rechecks_containment_after_inventory_and_rejects_missing_files() -> None:
    with skill_tree() as (root, folder):
        path = resource(folder, "references/details.md")
        registry = SkillRegistry.from_directory(root)
        outside = root.parent / "private.md"
        outside.write_text("private", encoding="utf-8")
        path.unlink()
        path.symlink_to(outside)
        with pytest.raises(AgentToolNotAllowed):
            registry.read_resource("alpha", "references/details.md")
        path.unlink()
        with pytest.raises(AgentToolNotAllowed):
            registry.read_resource("alpha", "references/details.md")


def test_resource_rejects_binary_non_utf8_and_over_limit_but_accepts_exact_limit() -> None:
    with skill_tree() as (root, folder):
        path = resource(folder, "references/details.md")
        registry = SkillRegistry.from_directory(root)
        path.write_bytes(b"a" * 65536)
        assert len(registry.read_resource("alpha", "references/details.md")["content"]) == 65536
        for data in (b"a" * 65537, b"bad\xff", b"binary\x00text", b"binary\x01text"):
            path.write_bytes(data)
            with pytest.raises(AgentToolNotAllowed):
                registry.read_resource("alpha", "references/details.md")


def test_resource_symlink_within_the_skill_is_allowed_without_a_cycle() -> None:
    with skill_tree() as (root, folder):
        path = resource(folder, "references/details.md", "safe")
        (path.parent / "alias.md").symlink_to(path)
        (path.parent / "loop").symlink_to(path.parent, target_is_directory=True)
        with pytest.raises(AgentConfigurationError):
            SkillRegistry.from_directory(root)
        (path.parent / "loop").unlink()
        registry = SkillRegistry.from_directory(root)
        assert registry.read_resource("alpha", "references/alias.md")["content"] == "safe"


def test_resource_api_supports_nonfilesystem_package_resources() -> None:
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("skills/alpha/SKILL.md", (
                "---\nname: alpha\ndescription: Use packaged references.\n---\nRead the reference.\n"
            ))
            bundle.writestr("skills/alpha/references/details.md", "Packaged details.")
        with zipfile.ZipFile(archive) as bundle:
            registry = SkillRegistry._from_root(zipfile.Path(bundle, "skills/"))
            assert registry.resources("alpha") == ["references/details.md"]
            assert registry.read_resource("alpha", "references/details.md")["content"] == "Packaged details."
