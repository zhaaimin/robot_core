import asyncio
import importlib
import sys
from unittest.mock import patch

import pytest


MODULES: dict[str, tuple[str, str]] = {
    "core": ("kernel.core", "RuntimeCore"),
    "executor": ("kernel.executor", "WorkflowExecutor"),
    "task_state": ("kernel.task_state", "TaskStateStore"),
    "resources": ("kernel.resources", "ResourceLocks"),
    "events": ("kernel.events", "EventBus"),
    "policy": ("kernel.policy", "PolicyEnforcer"),
    "registry": ("kernel.registry", "SkillRegistry"),
    "recovery": ("kernel.recovery", "recover_nonterminal_tasks"),
    "kernel_host": ("hosting.kernel_host", "KernelHost"),
    "thread_runtime": ("hosting.thread_runtime", "LoopThread"),
    "isolation": ("hosting.isolation", "isolate_ports"),
    "boundary": ("hosting.boundary", "bounded_snapshot"),
    "execution_context": ("hosting.execution_context", "CONTROL_OPERATION"),
    "repository": ("repositories.memory", "InMemoryTaskRepository"),
    "sqlite_repository": ("repositories.sqlite", "SQLiteTaskRepository"),
    "server": ("transport.server", "RuntimeServer"),
    "wire": ("transport.wire", "read_message"),
}


def test_only_canonical_import_paths_are_available() -> None:
    for old, (new, symbol) in MODULES.items():
        canonical = importlib.import_module(f"robot_agent_runtime.{new}")
        assert getattr(canonical, symbol) is not None
        with pytest.raises(ModuleNotFoundError) as caught:
            importlib.import_module(f"robot_agent_runtime.{old}")
        assert caught.value.name == f"robot_agent_runtime.{old}"


def test_cc_sdk_contracts_are_owned_by_the_vendor_package() -> None:
    ports = importlib.import_module("robot_agent_runtime.ports")
    assert not hasattr(ports, "CallbackRobotApi")
    assert not hasattr(ports, "CallbackApiResponse")
    contracts = importlib.import_module("robot_agent_runtime.adapters.cc_api.contracts")
    package = importlib.import_module("robot_agent_runtime.adapters.cc_api")
    for name in ("CcApiClient", "CcApiResponse"):
        contract = getattr(contracts, name)
        assert contract.__module__ == "robot_agent_runtime.adapters.cc_api.contracts"
        assert getattr(package, name) is contract


def test_canonical_patch_points_reach_hosted_and_transport_consumers() -> None:
    embedded = importlib.import_module("robot_agent_runtime.sdk.embedded")
    remote = importlib.import_module("robot_agent_runtime.sdk.remote")
    server = importlib.import_module("robot_agent_runtime.transport.server")
    core = importlib.import_module("robot_agent_runtime.kernel.core")
    isolation = importlib.import_module("robot_agent_runtime.hosting.isolation")
    token = object()
    with patch("robot_agent_runtime.hosting.kernel_host.KernelHost.start", token):
        assert embedded.KernelHost.start is token
        assert server.KernelHost.start is token
    with patch("robot_agent_runtime.transport.server.write_message", token):
        assert server.write_message is token
    with patch("robot_agent_runtime.transport.wire.read_message", token):
        assert importlib.import_module("robot_agent_runtime.transport.wire").read_message is token
    assert remote.read_message.__module__ == "robot_agent_runtime.transport.wire"
    with patch("robot_agent_runtime.hosting.isolation._snapshot", token):
        assert isolation._Port.call.__globals__["_snapshot"] is token
    with patch("robot_agent_runtime.kernel.core.recover_nonterminal_tasks", token):
        assert core.RuntimeCore.start.__globals__["recover_nonterminal_tasks"] is token


async def test_canonical_import_orders_work_in_fresh_processes_without_legacy_modules() -> None:
    for reverse in (False, True):
        code = (
            "import importlib\n"
            f"modules = {MODULES!r}\n"
            f"modules = dict(reversed(list(modules.items()))) if {reverse!r} else modules\n"
            "for old, (new, symbol) in modules.items():\n"
            "    canonical = importlib.import_module('robot_agent_runtime.' + new)\n"
            "    assert getattr(canonical, symbol) is not None\n"
            "    assert importlib.util.find_spec('robot_agent_runtime.' + old) is None, old\n"
            "from robot_agent_runtime.sdk import RobotRuntime, RemoteRuntime, ThreadBinding\n"
            "from robot_agent_runtime.__main__ import build_parser\n"
            "assert build_parser().prog == 'robot-agent-runtime'\n"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-c", code,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        except BaseException:
            process.kill()
            await process.communicate()
            raise
        assert process.returncode == 0, (stdout.decode(), stderr.decode())
