import asyncio
import json
import traceback

import pytest
from langchain_core.messages import HumanMessage

from chat_server import (
    TEST_API_KEY, TEST_MODEL, ChatResponse, completion, local_chat_server, tool_completion,
    tool_schemas,
)
from robot_agent_core.errors import AgentConfigurationError, AgentModelError
from robot_agent_core.models import SYSTEM_PROMPT, OpenAIModel



async def test_blank_optional_endpoint_env_keeps_the_actual_default_api_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('OPENAI_BASE_URL', '')
    model = OpenAIModel(TEST_MODEL, TEST_API_KEY, base_url=None)
    try:
        assert str(model._model.root_async_client.base_url) == 'https://api.openai.com/v1/'
    finally:
        await model.aclose()


def test_openai_rejects_missing_configuration_and_unbounded_timeouts() -> None:
    for model, key in (("", TEST_API_KEY), (TEST_MODEL, "")):
        with pytest.raises(AgentConfigurationError):
            OpenAIModel(model=model, api_key=key)
    for timeout in (False, True, 0, -1, float("inf"), float("nan")):
        with pytest.raises(AgentConfigurationError):
            OpenAIModel(model=TEST_MODEL, api_key=TEST_API_KEY, timeout_s=timeout)


async def test_openai_client_sends_real_tool_schema_and_decodes_provider_tool_call() -> None:
    async with local_chat_server([tool_completion()]) as chat:
        model = OpenAIModel(TEST_MODEL, TEST_API_KEY, base_url=chat.base_url)
        try:
            message = await model.decide([HumanMessage(content="播报 你好，机器人")], tool_schemas())
            assert message.tool_calls == [{
                "name": "robot_speak", "args": {"message": "你好，机器人"},
                "id": "call-local-speak", "type": "tool_call",
            }]
            assert not message.invalid_tool_calls
            assert len(chat.requests) == 1
            request = chat.requests[0]
            assert request.method == "POST"
            assert request.target == "/v1/chat/completions"
            assert request.headers["authorization"] == f"Bearer {TEST_API_KEY}"
            assert request.body["model"] == TEST_MODEL
            assert request.body["tools"] == tool_schemas()
            assert request.body["messages"] == [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "播报 你好，机器人"},
            ]
        finally:
            await model.aclose()


async def test_openai_graph_round_trip_sends_real_simulator_result_back_to_provider() -> None:
    from robot_agent_core.graph import AgentContext, build_graph
    from robot_agent.robot_tools import RobotTools
    from robot_agent_runtime.adapters.simulator import Simulator
    from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
    from robot_agent_runtime.sdk import RobotRuntime

    async with local_chat_server([
        tool_completion(), completion({"role": "assistant", "content": "机器人已完成播报。"}),
    ]) as chat:
        model = OpenAIModel(TEST_MODEL, TEST_API_KEY, base_url=chat.base_url)
        simulator = Simulator()
        runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
        await runtime.start()
        try:
            tools = await RobotTools.create(runtime)
            state = await build_graph().ainvoke(
                {"messages": [HumanMessage(content="播报 你好，机器人")], "model_turns": 0},
                context=AgentContext(model=model, tools=tools),
            )
            assert state["messages"][-1].content == "机器人已完成播报。"
            assert simulator.skills["audio.speak"].calls == 1
            assert state["observations"][0]["state"] == "succeeded"
            assert len(chat.requests) == 2
            assert all(request.body["tools"] == tools.schemas for request in chat.requests)
            messages = chat.requests[1].body["messages"]
            assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
            assert messages[-2]["role"] == "assistant"
            assert messages[-2]["tool_calls"][0]["id"] == "call-local-speak"
            assert messages[-1]["role"] == "tool"
            assert messages[-1]["tool_call_id"] == "call-local-speak"
            observed = json.loads(messages[-1]["content"])
            assert observed == state["observations"][0]
            assert observed["task_id"]
            assert observed["error"] is None
            assert observed["result"]["steps"][0]["output"] == {
                "message": "你好，机器人", "spoken": True,
            }
            assert (await runtime.get_task_status(observed["task_id"])).state.value == "succeeded"
        finally:
            try:
                await runtime.close()
            finally:
                await model.aclose()


async def test_openai_provider_errors_are_sanitized_and_not_automatically_retried() -> None:
    marker = "PRIVATE_PROVIDER_ERROR_BODY"
    for status, exception_types in (
        (401, {"AuthenticationError", "OpenAIAuthenticationError"}),
        (500, {"InternalServerError", "OpenAIAPIError"}),
    ):
        response = ChatResponse({"error": {
            "message": f"{marker}: provider echoed {TEST_API_KEY}",
            "type": "test_provider_error", "code": "test_error",
        }}, status=status)
        async with local_chat_server([response]) as chat:
            model = OpenAIModel(TEST_MODEL, TEST_API_KEY, base_url=chat.base_url)
            try:
                with pytest.raises(AgentModelError) as caught:
                    await model.decide([HumanMessage(content="hello")], tool_schemas())
                error = caught.value
                serialized = error.to_dict()
                assert serialized["code"] == AgentModelError.code
                assert serialized["message"] == "Model request failed"
                assert set(serialized["details"]) == {"exception_type"}
                assert serialized["details"]["exception_type"] in exception_types
                visible = "\n".join((str(error), repr(error), json.dumps(error.to_dict()),
                                     "".join(traceback.format_exception(error))))
                assert TEST_API_KEY not in visible
                assert marker not in visible
                assert error.__cause__ is None
                assert error.__suppress_context__ is True
                assert len(chat.requests) == 1
            finally:
                await model.aclose()


async def test_openai_rejects_malformed_provider_tool_arguments() -> None:
    async with local_chat_server([tool_completion("not valid JSON")]) as chat:
        model = OpenAIModel(TEST_MODEL, TEST_API_KEY, base_url=chat.base_url)
        try:
            with pytest.raises(AgentModelError, match="invalid tool arguments"):
                await model.decide([HumanMessage(content="hello")], tool_schemas())
            assert len(chat.requests) == 1
        finally:
            await model.aclose()


async def test_openai_request_deadline_is_bounded_and_returns_a_sanitized_error() -> None:
    async with local_chat_server([ChatResponse({}, release=asyncio.Event())]) as chat:
        model = OpenAIModel(TEST_MODEL, TEST_API_KEY, base_url=chat.base_url, timeout_s=0.2)
        request = asyncio.create_task(model.decide([HumanMessage(content="hello")], tool_schemas()))
        try:
            async with asyncio.timeout(2):
                await chat.request_seen.wait()
                with pytest.raises(AgentModelError) as caught:
                    await request
            error = caught.value.to_dict()
            assert error["code"] == AgentModelError.code
            assert error["message"] == "Model request failed"
            assert error["details"]["exception_type"] in {"TimeoutError", "APITimeoutError"}
            assert TEST_API_KEY not in json.dumps(error)
            assert len(chat.requests) == 1
        finally:
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await model.aclose()


async def test_openai_caller_cancellation_is_not_recast_as_provider_failure() -> None:
    async with local_chat_server([ChatResponse({}, release=asyncio.Event())]) as chat:
        model = OpenAIModel(TEST_MODEL, TEST_API_KEY, base_url=chat.base_url)
        request = asyncio.create_task(model.decide([HumanMessage(content="hello")], tool_schemas()))
        try:
            async with asyncio.timeout(2):
                await chat.request_seen.wait()
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            assert len(chat.requests) == 1
        finally:
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await model.aclose()
