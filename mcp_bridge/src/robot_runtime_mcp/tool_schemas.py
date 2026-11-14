import copy
from typing import Any

from robot_agent_capabilities.catalog import ToolDefinition


_SCHEMA_MAPS = {'$defs', 'definitions', 'properties', 'patternProperties', 'dependentSchemas', 'dependencies'}
_SCHEMA_VALUES = {'additionalProperties', 'unevaluatedProperties', 'propertyNames', 'contains',
                  'not', 'if', 'then', 'else', 'items', 'additionalItems', 'unevaluatedItems', 'contentSchema'}
_SCHEMA_LISTS = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}


def _nested_schema(value: Any, *, scoped: bool = False, id_keyword: str = '$id') -> Any:
    if isinstance(value, list):
        return [_nested_schema(item, scoped=scoped, id_keyword=id_keyword) for item in value]
    if not isinstance(value, dict):
        return value
    scoped = scoped or isinstance(value.get(id_keyword), str)
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {'$ref', '$dynamicRef', '$recursiveRef'} and isinstance(item, str) and not scoped:
            result[key] = ('#/properties/params' + item[1:] if item == '#' or item.startswith('#/') else item)
        elif key in _SCHEMA_MAPS and isinstance(item, dict):
            result[key] = {name: _nested_schema(child, scoped=scoped, id_keyword=id_keyword)
                           for name, child in item.items()}
        elif key in _SCHEMA_VALUES or key in _SCHEMA_LISTS:
            result[key] = _nested_schema(item, scoped=scoped, id_keyword=id_keyword)
        else:
            result[key] = copy.deepcopy(item)
    return result


def capability_input_schema(tool: ToolDefinition) -> dict[str, Any]:
    """Wrap protocol metadata without colliding with skill parameter names.

    Local root references follow the schema into params. Explicit $id resources
    keep their reference scope; literal data is never rewritten. The original
    catalog remains independent of the protocol envelope.
    """
    if tool.skill is None:
        return copy.deepcopy(tool.input_schema)
    dialect = tool.input_schema.get('$schema', '')
    id_keyword = 'id' if 'draft-04' in dialect or 'draft-03' in dialect else '$id'
    schema = {
        'type': 'object', 'properties': {
            'operation_id': {'type': 'string', 'minLength': 1, 'pattern': r'\S',
                             'description': 'Stable caller-owned operation ID. Reuse it only for the same action; never change it after an uncertain reply.'},
            'params': _nested_schema(tool.input_schema, id_keyword=id_keyword),
            'wait': {'type': 'boolean', 'default': False,
                     'description': 'Optionally wait up to the local deadline. Timeout does not cancel the robot task.'},
        },
        'required': ['operation_id', 'params'], 'additionalProperties': False,
    }
    if '$schema' in tool.input_schema:
        schema['$schema'] = tool.input_schema['$schema']
    return schema
