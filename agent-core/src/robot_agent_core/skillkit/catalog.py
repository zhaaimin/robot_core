import hashlib
import re
from collections.abc import Sequence
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

import yaml

from robot_agent_core.errors import AgentConfigurationError, AgentToolNotAllowed


_MAX_FILE_BYTES = 64 * 1024
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_RESOURCE_FOLDERS = frozenset({"references", "scripts", "assets"})


def valid_name(name: object) -> bool:
    return isinstance(name, str) and len(name) <= 64 and _NAME.fullmatch(name) is not None


def valid_resource_path(path: object) -> bool:
    if not isinstance(path, str) or "\\" in path or "\x00" in path:
        return False
    parts = path.split("/")
    return (len(parts) > 1 and parts[0] in _RESOURCE_FOLDERS
            and all(part not in {"", ".", ".."} for part in parts))


def require_contained(path: Path, root: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AgentConfigurationError("Skill path cannot be resolved") from error
    if not resolved.is_relative_to(root):
        raise AgentConfigurationError("Skill files must stay within the configured directory")


def read_document(path: Traversable, folder_name: str) -> dict[str, str]:
    with path.open("rb") as source:
        data = source.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise AgentConfigurationError("Skill document exceeds 64 KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AgentConfigurationError("Skill documents must be UTF-8") from error

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise AgentConfigurationError("Skill documents require YAML frontmatter")
    closing = next((index for index in range(1, len(lines))
                    if lines[index].rstrip("\r\n") == "---"), None)
    if closing is None:
        raise AgentConfigurationError("Skill YAML frontmatter is not closed")
    frontmatter = "".join(lines[1:closing])
    try:
        metadata = yaml.safe_load(frontmatter)
        mapping = yaml.compose(frontmatter, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError) as error:
        raise AgentConfigurationError("Skill YAML frontmatter is invalid") from error
    if not isinstance(metadata, dict) or not isinstance(mapping, yaml.MappingNode):
        raise AgentConfigurationError("Skill frontmatter must be a metadata object")
    keys = [key.value for key, _ in mapping.value if isinstance(key, yaml.ScalarNode)]
    if len(keys) != len(set(keys)):
        raise AgentConfigurationError("Skill frontmatter contains duplicate metadata keys")
    name = metadata.get("name")
    description = metadata.get("description")
    if not valid_name(name) or name != folder_name:
        raise AgentConfigurationError("Skill name must match its lowercase folder name")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise AgentConfigurationError("Skill description must contain 1 to 1024 characters")
    instructions = "".join(lines[closing + 1:]).strip()
    if not instructions:
        raise AgentConfigurationError("Skill instructions must not be empty")
    return {
        "name": name, "description": description.strip(), "instructions": instructions,
        "fingerprint": hashlib.sha256(data).hexdigest(),
    }


def resource_inventory(folder: Traversable, physical_root: Path | None) -> list[str]:
    paths: list[str] = []

    def visit(node: Traversable, relative: str, ancestors: frozenset[Path]) -> None:
        if node.name.startswith(".") or node.name == "__pycache__" or node.name.endswith((".pyc", ".pyo")):
            return
        resolved: Path | None = None
        if physical_root is not None:
            require_contained(Path(node), physical_root)
            resolved = Path(node).resolve(strict=True)
        if node.is_dir():
            if resolved is not None and resolved in ancestors:
                raise AgentConfigurationError("Skill resource directories must not contain cycles")
            nested = ancestors | {resolved} if resolved is not None else ancestors
            for child in sorted(node.iterdir(), key=lambda item: item.name):
                visit(child, relative + "/" + child.name, nested)
        elif node.is_file():
            if not valid_resource_path(relative):
                raise AgentConfigurationError("Skill resource paths must be relative POSIX paths")
            paths.append(relative)

    for name in sorted(_RESOURCE_FOLDERS):
        subtree = folder.joinpath(name)
        if subtree.is_dir() or subtree.is_file() or (isinstance(subtree, Path) and subtree.is_symlink()):
            visit(subtree, name, frozenset())
    return sorted(paths)


class SkillRegistry:
    """Snapshot instruction documents and inventory scoped supporting resources.

    Validation happens once, before any model sees a catalog. The catalog keeps
    discovery small; load exposes a selected body from that snapshot. Supporting
    text is read only on request, within the initial per-skill inventory, and is
    fingerprinted on each read. Neither operation imports or executes scripts.
    """

    def __init__(
        self, documents: list[dict[str, str]], *,
        resource_roots: dict[str, Traversable] | None = None,
        physical_roots: dict[str, Path] | None = None,
        inventories: dict[str, list[str]] | None = None,
    ) -> None:
        self._documents: dict[str, dict[str, str]] = {}
        for document in sorted(documents, key=lambda item: item["name"]):
            if document["name"] in self._documents:
                raise AgentConfigurationError("Skill names must be unique")
            self._documents[document["name"]] = dict(document)
        self._resource_roots = dict(resource_roots or {})
        self._physical_roots = dict(physical_roots or {})
        self._inventories = {name: tuple(paths) for name, paths in (inventories or {}).items()}

    @classmethod
    def from_directory(cls, directory: Path) -> "SkillRegistry":
        try:
            root = directory.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise AgentConfigurationError("Skill directory cannot be resolved") from error
        return cls._from_root(root, physical_root=root)

    @classmethod
    def bundled(cls, package: str) -> "SkillRegistry":
        """Read the Agent Skill catalogue shipped inside one application package.

        The kernel owns no skill content and never guesses a package: each
        application names its own, so a generic agent cannot serve another
        project's guidance by accident. Only importable packages are consulted,
        and listing resources never executes the scripts it finds.
        """
        root = files(package).joinpath("skills")
        if isinstance(root, Path):
            return cls.from_directory(root)
        return cls._from_root(root)

    @classmethod
    def _from_root(
        cls, root: Traversable, *, physical_root: Path | None = None,
    ) -> "SkillRegistry":
        try:
            if not root.is_dir():
                raise AgentConfigurationError("Skill registry requires a directory")
            documents: list[dict[str, str]] = []
            resource_roots: dict[str, Traversable] = {}
            physical_roots: dict[str, Path] = {}
            inventories: dict[str, list[str]] = {}
            for folder in sorted(root.iterdir(), key=lambda item: item.name):
                if physical_root is not None:
                    require_contained(Path(folder), physical_root)
                if not folder.is_dir():
                    continue
                if not valid_name(folder.name):
                    raise AgentConfigurationError("Skill folder name is invalid")
                path = folder.joinpath("SKILL.md")
                if physical_root is not None:
                    require_contained(Path(path), physical_root)
                if not path.is_file():
                    raise AgentConfigurationError("Each skill folder requires a SKILL.md file")
                documents.append(read_document(path, folder.name))
                skill_root = Path(folder).resolve(strict=True) if physical_root is not None else None
                resource_roots[folder.name] = folder
                if skill_root is not None:
                    physical_roots[folder.name] = skill_root
                inventories[folder.name] = resource_inventory(folder, skill_root)
            return cls(documents, resource_roots=resource_roots, physical_roots=physical_roots,
                       inventories=inventories)
        except OSError as error:
            raise AgentConfigurationError("Skill registry files cannot be read") from error

    def catalog(self) -> list[dict[str, str]]:
        return [{"name": document["name"], "description": document["description"]}
                for document in self._documents.values()]

    def select(self, names: Sequence[str]) -> "SkillRegistry":
        """Limit discovery without rebuilding or widening the verified snapshot."""
        if isinstance(names, (str, bytes)) or any(
            not valid_name(name) or name not in self._documents for name in names
        ):
            raise AgentConfigurationError("Skill selection contains an unknown or invalid name")
        selected = set(names)
        return SkillRegistry(
            [document for name, document in self._documents.items() if name in selected],
            resource_roots={name: root for name, root in self._resource_roots.items() if name in selected},
            physical_roots={name: root for name, root in self._physical_roots.items() if name in selected},
            inventories={name: list(paths) for name, paths in self._inventories.items() if name in selected},
        )

    def load(self, name: str) -> dict[str, str]:
        if not valid_name(name) or name not in self._documents:
            raise AgentToolNotAllowed("Skill is not in the application catalog")
        return dict(self._documents[name])

    def resources(self, name: str) -> list[str]:
        self.load(name)
        return list(self._inventories.get(name, ()))

    def read_resource(self, name: str, path: str) -> dict[str, str]:
        self.load(name)
        if not valid_resource_path(path) or path not in self._inventories.get(name, ()):
            raise AgentToolNotAllowed("Resource is not in the selected skill inventory")
        try:
            resource = self._resource_roots[name].joinpath(*path.split("/"))
            physical_root = self._physical_roots.get(name)
            if physical_root is not None:
                require_contained(Path(resource), physical_root)
                resource = Path(resource).resolve(strict=True)
            if not resource.is_file():
                raise AgentToolNotAllowed("Skill resource is not a readable text file")
            with resource.open("rb") as source:
                data = source.read(_MAX_FILE_BYTES + 1)
            if len(data) > _MAX_FILE_BYTES:
                raise AgentToolNotAllowed("Skill resource exceeds 64 KiB")
            text = data.decode("utf-8")
            if any(ord(character) < 32 and character not in "\t\n\r" for character in text):
                raise AgentToolNotAllowed("Skill resource must contain UTF-8 text")
        except (OSError, UnicodeDecodeError, AgentConfigurationError) as error:
            raise AgentToolNotAllowed("Skill resource cannot be read within its skill directory") from error
        return {"name": name, "path": path, "content": text,
                "fingerprint": hashlib.sha256(data).hexdigest()}
