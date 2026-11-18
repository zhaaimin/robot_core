import math
from datetime import datetime, timezone
from typing import Any

from robot_agent_runtime.errors import InternalError, SnapshotTooLarge
from robot_agent_runtime.models import (
    ActionOutcome,
    ErrorInfo,
    Event,
    PlatformEvent,
    SkillCall,
    SkillDefinition,
    SkillResult,
    Task,
    TaskRequest,
    TaskState,
)


_MODELS = (
    ActionOutcome, ErrorInfo, Event, PlatformEvent, SkillCall, SkillDefinition, SkillResult, Task, TaskRequest,
)


class _Snapshot:
    """Measure before copying so foreign values cannot allocate unbounded snapshots.

    Only built-in traversal and canonical model fields are used. Subclass copy,
    iteration, and serialization hooks are never needed to detach a value.
    The byte budget counts a compact JSON representation, including UTF-8 and
    escaping; tuple and set containers use the same budget as JSON arrays.
    """

    def __init__(self, max_bytes: int, max_depth: int) -> None:
        self.remaining = max_bytes
        self.max_depth = max_depth
        self.active: set[int] = set()

    def charge(self, count: int) -> None:
        self.remaining -= count
        if self.remaining < 0:
            raise SnapshotTooLarge("Extension snapshot exceeds its byte limit")

    def string(self, value: str, build: bool) -> str | None:
        self.charge(str.__len__(value) + 2)
        for character in str.__iter__(value):
            number = ord(character)
            if number < 32:
                self.charge(1 if character in "\b\f\n\r\t" else 5)
            elif character in ('"', "\\"):
                self.charge(1)
            elif number >= 128:
                if 0xD800 <= number <= 0xDFFF:
                    raise InternalError("Extension strings require valid Unicode")
                self.charge(1 if number < 0x800 else 2 if number < 0x10000 else 3)
        return str.__str__(value) if build else None

    def visit(self, value: Any, depth: int, build: bool = False) -> Any:
        if depth > self.max_depth:
            raise SnapshotTooLarge("Extension snapshot exceeds its nesting limit")
        kind = type(value)
        parents = type.__getattribute__(kind, "__mro__")

        def derives(base: type[Any]) -> bool:
            return any(parent is base for parent in parents)

        if value is None or kind is bool:
            self.charge(4 if value is None or value else 5)
            return value
        if derives(TaskState):
            text = object.__getattribute__(value, "_value_")
            self.string(text, build)
            return TaskState(text) if build else None
        if derives(str):
            return self.string(value, build)
        if derives(int):
            if int.bit_length(value) > (self.remaining + 1) * 4:
                raise SnapshotTooLarge("Extension integer exceeds its byte limit")
            try:
                self.charge(len(int.__repr__(value)))
            except ValueError:
                raise SnapshotTooLarge("Extension integer exceeds its byte limit") from None
            return int.__int__(value) if build else None
        if derives(float):
            plain = float.__float__(value)
            if not math.isfinite(plain):
                raise InternalError("Extension numbers must be finite")
            self.charge(len(float.__repr__(plain)))
            return plain if build else None
        if kind is datetime:
            if type(value.tzinfo) is not timezone:
                raise InternalError("Extension timestamps require a plain timezone")
            return self.string(datetime.isoformat(value), build)
        model = next((model for model in _MODELS if derives(model)), None)
        if model is not None:
            fields = object.__getattribute__(value, "__dict__")
            if type(fields) is not dict:
                raise InternalError("Extension model fields require a plain mapping")
            if any(type(key) is not str for key in dict.__iter__(fields)):
                raise InternalError("Extension model field names require plain strings")
            names = model.model_fields
            items = ((name, dict.__getitem__(fields, name)) for name in names if name in fields)
            count = sum(name in fields for name in names)
            container = "model"
        elif derives(dict):
            items = dict.items(value)
            count = dict.__len__(value)
            container = "mapping"
        elif any(derives(base) for base in (list, tuple, set, frozenset)):
            base = next(base for base in (list, tuple, set, frozenset) if derives(base))
            items = base.__iter__(value)
            count = base.__len__(value)
            container = "sequence"
        else:
            raise InternalError("Extension returned a value outside the JSON boundary")
        identity = id(value)
        if identity in self.active:
            raise InternalError("Extension snapshot contains a reference cycle")
        self.active.add(identity)
        try:
            self.charge(2 + max(0, count - 1))
            if container != "sequence":
                copied: dict[str, Any] = {}
                for key, item in items:
                    if not any(parent is str for parent in type.__getattribute__(type(key), "__mro__")):
                        raise InternalError("Extension mappings require string keys")
                    copied_key = self.string(key, build)
                    self.charge(1)
                    copied_item = self.visit(item, depth + 1, build)
                    if build:
                        copied[copied_key] = copied_item
                if not build:
                    return None
                return model.model_validate(copied) if model is not None else copied
            values = (self.visit(item, depth + 1, build) for item in items)
            if build:
                return base(values)
            for _ in values:
                pass
            return None
        finally:
            self.active.remove(identity)


def bounded_snapshot(value: Any, *, max_bytes: int, max_depth: int) -> Any:
    """Detach supported values after checking their complete size and depth.

    Runtime models are rebuilt as their canonical base classes, while container
    subclasses become plain containers. Arbitrary objects and reference cycles
    are rejected instead of invoking foreign copying or serialization hooks.
    """

    snapshot = _Snapshot(max_bytes, max_depth)
    snapshot.visit(value, 0)
    return _Snapshot(max_bytes, max_depth).visit(value, 0, build=True)
