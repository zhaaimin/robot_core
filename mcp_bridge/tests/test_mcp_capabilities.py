import jsonschema
import pytest

from robot_agent_runtime.sdk import RemoteRuntime
from test_mcp_bridge_stdio import raw_session, runtime_server


async def test_stdio_exposes_the_shared_catalog_and_per_skill_parameter_schema() -> None:
    async with runtime_server() as (_, socket, _):
        async with raw_session(socket) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert 'robot_speak' in tools
            from robot_agent_capabilities import RobotCapabilities

            runtime = RemoteRuntime(socket)
            try:
                capabilities = await RobotCapabilities.create(runtime)
                shared = {tool.name: tool for tool in capabilities.tools}
                for name, definition in shared.items():
                    assert tools[name].description.startswith(definition.description)
                schema = tools['robot_speak'].inputSchema
                assert set(schema['required']) == {'operation_id', 'params'}
                assert schema['properties']['params'] == shared['robot_speak'].input_schema
                assert tools['robot_task_status'].annotations.readOnlyHint is True
                assert tools['robot_cancel_task'].annotations.readOnlyHint is False
            finally:
                await runtime.close()


async def test_native_mcp_and_python_sdk_share_the_same_operation_and_task() -> None:
    async with runtime_server() as (_, socket, simulator):
        async with raw_session(socket) as session:
            native = await session.call_tool('robot_speak', {
                'operation_id': 'shared-operation', 'params': {'message': 'shared'}, 'wait': True,
            })
            assert not native.isError
            from robot_agent_capabilities import RobotCapabilities

            runtime = RemoteRuntime(socket)
            try:
                capabilities = await RobotCapabilities.create(runtime)
                observed = await capabilities.invoke('robot_speak', {'message': 'shared'},
                                                      operation_id='shared-operation', wait=True)
                assert native.structuredContent == observed
                assert simulator.skills['audio.speak'].calls == 1
            finally:
                await runtime.close()


async def test_native_background_status_and_cancel_preserve_real_task_facts() -> None:
    async with runtime_server() as (_, socket, _):
        async with raw_session(socket) as session:
            accepted = await session.call_tool('robot_wait_delay', {
                'operation_id': 'background-mcp', 'params': {'delay_s': 30},
            })
            assert not accepted.isError
            task_id = accepted.structuredContent['task_id']
            assert task_id
            status = await session.call_tool('robot_task_status', {'task_id': task_id})
            assert status.structuredContent['task_id'] == task_id
            canceled = await session.call_tool('robot_cancel_task', {'task_id': task_id})
            assert not canceled.isError
            assert canceled.structuredContent['state'] == 'canceled'
            assert canceled.structuredContent['requires_attention'] is True


async def test_native_allowlist_and_invalid_envelopes_never_dispatch_hardware() -> None:
    async with runtime_server() as (_, socket, simulator):
        async with raw_session(socket, '--allow-skill', 'audio.speak') as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert 'robot_speak' in names and 'robot_navigate_to' not in names
            for arguments in (
                {'params': {'message': 'hello'}},
                {'operation_id': ' ', 'params': {'message': 'hello'}},
                {'operation_id': 'bad', 'params': []},
                {'operation_id': 'bad', 'params': {'message': 'hello'}, 'wait': 'true'},
                {'operation_id': 'bad', 'params': {'message': 'hello'}, 'other': 1},
            ):
                result = await session.call_tool('robot_speak', arguments)
                assert result.isError and result.structuredContent['error']['code'] == 'INVALID_REQUEST'
            denied = await session.call_tool('robot_navigate_to', {
                'operation_id': 'hidden', 'params': {'target': 'room'},
            })
            assert denied.isError
            assert simulator.skills['audio.speak'].calls == 0
            assert simulator.skills['navigation.navigate_to'].calls == 0


async def test_native_runtime_failure_is_an_observation_with_a_task_handle() -> None:
    async with runtime_server() as (_, socket, _):
        async with raw_session(socket) as session:
            rejected = await session.call_tool('robot_speak', {
                'operation_id': 'invalid-message', 'params': {'message': ''}, 'wait': True,
            })
            assert rejected.isError
            value = rejected.structuredContent
            assert value['task_id'] and value['state'] == 'failed'
            assert value['error']['code'] == 'SCHEMA_VALIDATION_FAILED'
            queried = await session.call_tool('robot_task_status', {'task_id': value['task_id']})
            assert not queried.isError
            assert queried.structuredContent['error'] == value['error']


def test_wrapping_skill_schemas_preserves_local_references_and_literal_data() -> None:
    from robot_agent_capabilities.catalog import ToolDefinition
    from robot_runtime_mcp.tool_schemas import capability_input_schema

    source = {
        'type': 'object', '$defs': {'Value': {'type': 'string', 'minLength': 1}},
        'properties': {'value': {'$ref': '#/$defs/Value'}, 'child': {'$ref': '#'},
                       'const': {'$ref': '#/$defs/Value'}, '$id': {'type': 'string'},
                       'literal': {'const': {'$ref': '#/unchanged'}}},
        'required': ['value'], 'additionalProperties': False,
    }
    tool = ToolDefinition(name='robot_custom', description='custom', input_schema=source, skill='custom')
    schema = capability_input_schema(tool)
    jsonschema.validate({'operation_id': 'references', 'params': {
        'value': 'a', 'child': {'value': 'b'}, 'const': 'c', '$id': 'parameter',
        'literal': {'$ref': '#/unchanged'},
    }}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'operation_id': 'bad', 'params': {'value': 1}}, schema)
    assert source['properties']['value']['$ref'] == '#/$defs/Value'


async def test_native_empty_allowlist_only_keeps_task_controls() -> None:
    async with runtime_server() as (_, socket, _):
        async with raw_session(socket, '--deny-all-skills') as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names == {'list_skills', 'submit_task', 'get_task_status', 'cancel_task',
                             'read_task_events', 'robot_task_status', 'robot_cancel_task'}


async def test_allowlist_is_a_visibility_filter_not_a_required_capability_list() -> None:
    async with runtime_server() as (_, socket, _):
        async with raw_session(socket, '--allow-skill', 'audio.speak', '--allow-skill', 'unavailable.skill') as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert 'robot_speak' in names and 'robot_unavailable_skill' not in names
            skills = await session.call_tool('list_skills', {})
            assert not skills.isError
            assert [skill['name'] for skill in skills.structuredContent['skills']] == ['audio.speak']


def test_wrapped_schema_retains_the_runtime_json_schema_dialect() -> None:
    from robot_agent_capabilities.catalog import ToolDefinition
    from robot_runtime_mcp.tool_schemas import capability_input_schema

    for dialect in ('http://json-schema.org/draft-07/schema#', 'http://json-schema.org/draft-04/schema#'):
        source = {'$schema': dialect, 'type': 'object', 'properties': {
            'values': {'type': 'array', 'items': [{'type': 'string'}, {'type': 'number'}],
                       'additionalItems': False},
        }, 'required': ['values']}
        tool = ToolDefinition(name='robot_tuple', description='tuple', input_schema=source, skill='tuple')
        wrapped = capability_input_schema(tool)
        assert wrapped['$schema'] == dialect
        jsonschema.validate({'operation_id': 'tuple', 'params': {'values': ['x', 1]}}, wrapped)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({'operation_id': 'tuple', 'params': {'values': [1, 'x']}}, wrapped)


def test_wrapped_schema_keeps_explicit_resource_id_scopes() -> None:
    from robot_agent_capabilities.catalog import ToolDefinition
    from robot_runtime_mcp.tool_schemas import capability_input_schema

    for dialect, id_keyword in (('http://json-schema.org/draft-04/schema#', 'id'),
                                ('http://json-schema.org/draft-07/schema#', '$id')):
        source = {'$schema': dialect, id_keyword: 'urn:robot:parameters', 'type': 'object',
                  'definitions': {'Value': {'type': 'string'}},
                  'properties': {'value': {'$ref': '#/definitions/Value'}}, 'required': ['value']}
        tool = ToolDefinition(name='robot_scoped', description='scoped', input_schema=source, skill='scoped')
        wrapped = capability_input_schema(tool)
        jsonschema.validate({'operation_id': 'resource', 'params': {'value': 'valid'}}, wrapped)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({'operation_id': 'resource', 'params': {'value': 1}}, wrapped)
