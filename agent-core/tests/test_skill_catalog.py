import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from robot_agent_core.errors import AgentConfigurationError, AgentToolNotAllowed


def document(
    name: str, *, description: str = "Use when a guest needs assistance.",
    body: str = "Ask for the missing information.",
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def write_skill(root: Path, name: str, content: str | None = None) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(content if content is not None else document(name), encoding="utf-8")
    return path


def test_catalog_exposes_only_sorted_metadata_and_load_returns_verified_body() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_skill(root, "zulu", document("zulu", body="Keep this out of the catalog."))
        path = write_skill(root, "alpha", document("alpha", body="# Welcome\n\nAsk one question."))
        registry = SkillRegistry.from_directory(root)
        assert registry.catalog() == [
            {"name": "alpha", "description": "Use when a guest needs assistance."},
            {"name": "zulu", "description": "Use when a guest needs assistance."},
        ]
        assert registry.load("alpha") == {
            "name": "alpha", "description": "Use when a guest needs assistance.",
            "instructions": "# Welcome\n\nAsk one question.",
            "fingerprint": hashlib.sha256(path.read_bytes()).hexdigest(),
        }


def test_catalog_and_loaded_documents_are_independent_copies() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = write_skill(root, "alpha")
        registry = SkillRegistry.from_directory(root)
        registry.catalog()[0]["description"] = "mutated"
        registry.load("alpha")["instructions"] = "mutated"
        path.write_text("replaced after validation", encoding="utf-8")
        assert registry.catalog()[0]["description"] == "Use when a guest needs assistance."
        assert registry.load("alpha")["instructions"] == "Ask for the missing information."


def test_load_accepts_registered_names_only_and_never_a_path() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_skill(root, "alpha")
        registry = SkillRegistry.from_directory(root)
        for name in ("missing", "", "../alpha", "alpha/SKILL.md", "/alpha", "alpha\\SKILL.md"):
            with pytest.raises(AgentToolNotAllowed):
                registry.load(name)


def test_registry_rejects_invalid_folder_names_and_metadata_mismatch() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    for name in ("Upper", "-leading", "trailing-", "double--hyphen", "two words", "café", "a" * 65):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(root, name)
            with pytest.raises(AgentConfigurationError):
                SkillRegistry.from_directory(root)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_skill(root, "alpha", document("beta"))
        with pytest.raises(AgentConfigurationError):
            SkillRegistry.from_directory(root)


def test_registry_accepts_the_name_and_description_length_boundaries() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        name = "a" * 64
        write_skill(root, name, document(name, description="d" * 1024))
        registry = SkillRegistry.from_directory(root)
        assert registry.catalog() == [{"name": name, "description": "d" * 1024}]


def test_registry_requires_string_name_description_and_nonempty_instructions() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    invalid_documents = (
        "---\ndescription: Missing name.\n---\nBody",
        "---\nname: alpha\n---\nBody",
        "---\nname: 123\ndescription: A description.\n---\nBody",
        "---\nname: alpha\ndescription: [not, text]\n---\nBody",
        "---\nname: alpha\ndescription: true\n---\nBody",
        document("alpha", description="''"),
        document("alpha", description="'   '"),
        document("alpha", description="d" * 1025),
        document("alpha", body=" \n\t "),
    )
    for content in invalid_documents:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(root, "alpha", content)
            with pytest.raises(AgentConfigurationError):
                SkillRegistry.from_directory(root)


def test_registry_requires_well_formed_safe_yaml_frontmatter() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    invalid_documents = (
        "name: alpha\ndescription: No opening delimiter.\n---\nBody",
        "---\nname: alpha\ndescription: No closing delimiter.\nBody",
        "---\nname: [unterminated\n---\nBody",
        "---\n- alpha\n- description\n---\nBody",
        "---\nname: alpha\ndescription: !!python/object/apply:builtins.str ['unsafe']\n---\nBody",
        "---\nname: alpha\nname: beta\ndescription: Duplicate name.\n---\nBody",
    )
    for content in invalid_documents:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(root, "alpha", content)
            with pytest.raises(AgentConfigurationError):
                SkillRegistry.from_directory(root)


def test_registry_limits_file_bytes_and_rejects_non_utf8() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = write_skill(root, "alpha")
        original = path.read_bytes()
        path.write_bytes(original + b"x" * (65536 - len(original)))
        assert SkillRegistry.from_directory(root).load("alpha")["name"] == "alpha"
        path.write_bytes(path.read_bytes() + b"x")
        with pytest.raises(AgentConfigurationError):
            SkillRegistry.from_directory(root)
        path.write_bytes(b"---\nname: alpha\ndescription: text\n---\n\xff")
        with pytest.raises(AgentConfigurationError):
            SkillRegistry.from_directory(root)


def test_registry_rejects_missing_root_and_missing_skill_document() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with pytest.raises(AgentConfigurationError):
            SkillRegistry.from_directory(root / "missing")
        (root / "alpha").mkdir()
        with pytest.raises(AgentConfigurationError):
            SkillRegistry.from_directory(root)


def test_registry_rejects_directory_symlinks_that_escape_the_root() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    with tempfile.TemporaryDirectory() as directory:
        parent = Path(directory)
        root = parent / "skills"
        root.mkdir()
        outside = parent / "outside"
        write_skill(outside, "alpha")
        (root / "alpha").symlink_to(outside / "alpha", target_is_directory=True)
        with pytest.raises(AgentConfigurationError):
            SkillRegistry.from_directory(root)


def test_registry_rejects_document_symlinks_that_escape_the_root() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    with tempfile.TemporaryDirectory() as directory:
        parent = Path(directory)
        root = parent / "skills"
        (root / "alpha").mkdir(parents=True)
        outside = parent / "private.md"
        outside.write_text(document("alpha"), encoding="utf-8")
        (root / "alpha" / "SKILL.md").symlink_to(outside)
        with pytest.raises(AgentConfigurationError):
            SkillRegistry.from_directory(root)


def test_references_and_scripts_are_inert_instruction_text() -> None:
    from robot_agent_core.skillkit.catalog import SkillRegistry

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        body = "Read [private](../../private.txt), then run scripts/action.py."
        write_skill(root, "alpha", document("alpha", body=body))
        script = root / "alpha" / "scripts" / "action.py"
        script.parent.mkdir()
        script.write_text("raise AssertionError('must not execute')", encoding="utf-8")
        assert SkillRegistry.from_directory(root).load("alpha")["instructions"] == body
