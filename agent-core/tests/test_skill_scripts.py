import asyncio
import hashlib
import json
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from robot_agent_core.errors import AgentConfigurationError
from robot_agent_core.ports import Observation
from robot_agent_core.skillkit.catalog import SkillRegistry


class ExampleTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.observation: Observation = {
            "tool": "example_action", "task_id": "physical-task", "state": "running",
            "result": {"phase": "escorting", "accepted": True},
            "error": {"code": "STOP_UNCONFIRMED", "message": "Query actual task."},
            "requires_attention": True,
        }

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return []

    async def invoke(self, name: str, args: dict[str, Any], *, operation_id: str) -> Observation:
        self.calls.append((name, args, operation_id))
        return self.observation


@contextmanager
def registered_tree() -> Iterator[tuple[SkillRegistry, Path, str]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        folder = root / "alpha"
        (folder / "scripts").mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Use when testing scripts.\n---\nUse registered helpers.\n",
            encoding="utf-8",
        )
        script = folder / "scripts" / "action.py"
        script.write_text("# Published helper source; read never executes it.\n", encoding="utf-8")
        yield SkillRegistry.from_directory(root), script, hashlib.sha256(script.read_bytes()).hexdigest()


async def call_tool(arguments: dict[str, Any], context: Any) -> Observation:
    return await context.tools.invoke("example_action", arguments, operation_id=context.operation_id)


async def test_registered_script_runs_real_handler_with_controlled_context_and_preserves_result() -> None:
    from robot_agent_core.skillkit.scripts import ApprovedScript, SkillScriptRunner

    with registered_tree() as (registry, _, fingerprint):
        tools = ExampleTools()
        runner = SkillScriptRunner(registry, tools, [
            ApprovedScript("alpha", "scripts/action.py", fingerprint, call_tool),
        ])
        observed = await runner.invoke("alpha", "scripts/action.py", {"guest": "checked"},
                                       operation_id="stable-operation")
        assert tools.calls == [("example_action", {"guest": "checked"}, "stable-operation")]
        assert observed == {**tools.observation, "tool": "run_skill_script", "execution": {
            "skill": "alpha", "path": "scripts/action.py", "fingerprint": fingerprint,
        }}
        assert tools.observation["tool"] == "example_action"
        assert "execution" not in tools.observation


async def test_unregistered_or_changed_script_never_executes_a_handler() -> None:
    from robot_agent_core.skillkit.scripts import ApprovedScript, SkillScriptRunner

    with registered_tree() as (registry, source, fingerprint):
        tools = ExampleTools()
        runner = SkillScriptRunner(registry, tools, [
            ApprovedScript("alpha", "scripts/action.py", fingerprint, call_tool),
        ])
        for name, path in (("alpha", "scripts/missing.py"), ("missing", "scripts/action.py"),
                           ("alpha", "../scripts/action.py"), ("alpha", "references/details.md")):
            observed = await runner.invoke(name, path, {}, operation_id="denied")
            assert observed["error"]["code"] == "AGENT_TOOL_NOT_ALLOWED"
            assert observed["requires_attention"] is True
            assert observed["result"] is None
        source.write_text("# Changed since registration.\n", encoding="utf-8")
        changed = await runner.invoke("alpha", "scripts/action.py", {}, operation_id="changed")
        assert changed["error"]["code"] == "AGENT_TOOL_NOT_ALLOWED"
        assert changed["requires_attention"] is True
        assert tools.calls == []


def test_registration_rejects_duplicates_mismatches_and_non_script_paths() -> None:
    from robot_agent_core.skillkit.scripts import ApprovedScript, SkillScriptRunner

    with registered_tree() as (registry, _, fingerprint):
        tools = ExampleTools()
        valid = ApprovedScript("alpha", "scripts/action.py", fingerprint, call_tool)
        for bindings in (
            [None],
            [valid, valid],
            [ApprovedScript([], "scripts/action.py", fingerprint, call_tool)],
            [ApprovedScript("alpha", "scripts/action.py", "0" * 64, call_tool)],
            [ApprovedScript("alpha", "scripts/action.py", "not-a-fingerprint", call_tool)],
            [ApprovedScript("missing", "scripts/action.py", fingerprint, call_tool)],
            [ApprovedScript("alpha", "../scripts/action.py", fingerprint, call_tool)],
            [ApprovedScript("alpha", "references/details.md", fingerprint, call_tool)],
            [ApprovedScript("alpha", "scripts/action.sh", fingerprint, call_tool)],
            [ApprovedScript("alpha", "scripts/action.py", fingerprint, lambda *_: None)],
        ):
            with pytest.raises(AgentConfigurationError):
                SkillScriptRunner(registry, tools, bindings)


def test_runner_requires_finite_positive_timeout() -> None:
    from robot_agent_core.skillkit.scripts import SkillScriptRunner

    with registered_tree() as (registry, _, _):
        for timeout in (True, 0, -1, float("inf"), float("nan"), "5"):
            with pytest.raises(AgentConfigurationError):
                SkillScriptRunner(registry, ExampleTools(), [], timeout_s=timeout)


async def test_script_timeout_is_attention_failure_and_cancels_handler() -> None:
    from robot_agent_core.skillkit.scripts import ApprovedScript, SkillScriptRunner

    canceled = asyncio.Event()

    async def blocked(arguments: dict[str, Any], context: Any) -> Observation:
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()
        raise AssertionError("unreachable")

    with registered_tree() as (registry, _, fingerprint):
        runner = SkillScriptRunner(registry, ExampleTools(), [
            ApprovedScript("alpha", "scripts/action.py", fingerprint, blocked),
        ], timeout_s=0.01)
        async with asyncio.timeout(1):
            observed = await runner.invoke("alpha", "scripts/action.py", {}, operation_id="slow")
            await canceled.wait()
        assert observed["error"]["code"] == "AGENT_SKILL_SCRIPT_TIMEOUT"
        assert observed["requires_attention"] is True
        assert observed["state"] is None and observed["result"] is None
        assert observed["execution"]["fingerprint"] == fingerprint


async def test_caller_cancellation_is_not_converted_to_a_script_failure() -> None:
    from robot_agent_core.skillkit.scripts import ApprovedScript, SkillScriptRunner

    entered = asyncio.Event()

    async def blocked(arguments: dict[str, Any], context: Any) -> Observation:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with registered_tree() as (registry, _, fingerprint):
        runner = SkillScriptRunner(registry, ExampleTools(), [
            ApprovedScript("alpha", "scripts/action.py", fingerprint, blocked),
        ])
        task = asyncio.create_task(runner.invoke("alpha", "scripts/action.py", {}, operation_id="cancel"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_script_exception_is_sanitized_and_does_not_fake_business_success() -> None:
    from robot_agent_core.skillkit.scripts import ApprovedScript, SkillScriptRunner

    async def raises(arguments: dict[str, Any], context: Any) -> Observation:
        raise ValueError("SECRET receipt=/private/guest.txt token=hidden")

    with registered_tree() as (registry, _, fingerprint):
        runner = SkillScriptRunner(registry, ExampleTools(), [
            ApprovedScript("alpha", "scripts/action.py", fingerprint, raises),
        ])
        observed = await runner.invoke("alpha", "scripts/action.py", {}, operation_id="exception")
        assert observed["error"]["code"] == "AGENT_SKILL_SCRIPT_FAILED"
        assert observed["requires_attention"] is True
        assert observed["state"] is None and observed["result"] is None
        assert "SECRET" not in json.dumps(observed)
        assert "private" not in json.dumps(observed)


async def test_handler_raised_timeout_error_is_failure_not_a_runner_deadline() -> None:
    from robot_agent_core.skillkit.scripts import ApprovedScript, SkillScriptRunner

    async def raises(arguments: dict[str, Any], context: Any) -> Observation:
        raise TimeoutError("An internal tool deadline expired")

    with registered_tree() as (registry, _, fingerprint):
        runner = SkillScriptRunner(registry, ExampleTools(), [
            ApprovedScript("alpha", "scripts/action.py", fingerprint, raises),
        ])
        observed = await runner.invoke("alpha", "scripts/action.py", {}, operation_id="inner-timeout")
        assert observed["error"]["code"] == "AGENT_SKILL_SCRIPT_FAILED"
        assert observed["requires_attention"] is True


async def test_invalid_invocation_arguments_and_malformed_handler_output_fail_closed() -> None:
    from robot_agent_core.skillkit.scripts import ApprovedScript, SkillScriptRunner

    async def malformed(arguments: dict[str, Any], context: Any) -> Observation:
        return {"result": {"success": True}}

    with registered_tree() as (registry, _, fingerprint):
        tools = ExampleTools()
        runner = SkillScriptRunner(registry, tools, [
            ApprovedScript("alpha", "scripts/action.py", fingerprint, call_tool),
        ])
        for arguments, operation_id in (([], "operation"), ({}, ""), ({}, None)):
            observed = await runner.invoke("alpha", "scripts/action.py", arguments,
                                           operation_id=operation_id)
            assert observed["requires_attention"] is True
            assert observed["error"]["code"] == "INVALID_REQUEST"
        assert tools.calls == []
        runner = SkillScriptRunner(registry, tools, [
            ApprovedScript("alpha", "scripts/action.py", fingerprint, malformed),
        ])
        observed = await runner.invoke("alpha", "scripts/action.py", {}, operation_id="bad-output")
        assert observed["requires_attention"] is True
        assert observed["error"]["code"] == "AGENT_SKILL_SCRIPT_FAILED"
