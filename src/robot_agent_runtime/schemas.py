"""Make JSON Schema bounds survive clients that simplify the schema.

The schema remains the enforcement point: the runtime still validates against
``input_schema`` unchanged. Repeating a bound in its own property description
is a hint for clients that forward only ``type`` and ``properties``, because a
model that never sees ``minLength`` will produce a value the runtime rejects
after a full round trip.

The text is deliberately language-neutral so every adapter and every bridge can
use the same helper regardless of the language its own descriptions use.
"""

import copy
from typing import Any

_BOUNDS = (
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "pattern",
)


def describe_bounds(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy whose annotated nodes mention their own bounds."""
    annotated = copy.deepcopy(schema)
    _annotate(annotated)
    return annotated


def _annotate(node: Any) -> None:
    if not isinstance(node, dict):
        return
    for child in _children(node):
        _annotate(child)
    bounds = [f"{key}={node[key]}" for key in _BOUNDS if key in node]
    if not bounds:
        return
    text = "[" + ", ".join(bounds) + "]"
    existing = node.get("description")
    node["description"] = f"{existing} {text}" if existing else text


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for key in ("properties", "$defs", "definitions"):
        group = node.get(key)
        if isinstance(group, dict):
            children.extend(value for value in group.values() if isinstance(value, dict))
    items = node.get("items")
    if isinstance(items, dict):
        children.append(items)
    return children
