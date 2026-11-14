# Unified Robot Capabilities Implementation Plan

> **For agentic workers:** Execute inline with `superpowers:executing-plans`, using TDD and an independent code review. The user approved Runtime + unified capabilities + MCP/SDK on 2026-11-14. Continue through implementation and validation without another approval gate.

**Goal:** Let external MCP agents and Python applications discover and invoke the same robot capabilities without importing the LangGraph agent.

**Architecture:** A separate `robot-agent-capabilities` package depends only on `robot-agent-runtime`. It owns the task-client Protocol, tool catalog, stable-operation submission, bounded waiting and factual observations. MCP translates protocol envelopes; the existing RobotTools becomes a thin application adapter. Runtime remains authoritative for admission, state, hardware execution, stopping and recovery.

**Tech Stack:** Existing Python 3.11+, asyncio, Pydantic/runtime SDK and installed MCP SDK. No new third-party dependency. Run every verification in `robot_core_ubuntu24`.

## Accepted boundaries

- Codex, Claude Code and custom agents consume tools; they need not run through `agent-core` or a model managed by this repository.
- Package: `capabilities/`, distribution `robot-agent-capabilities`, import `robot_agent_capabilities`.
- `RobotCapabilities.create(runtime, allowed_skills=...)` takes an application-owned structural TaskRuntime. It never starts or closes that runtime.
- `tools` returns independent ToolDefinition snapshots (`name`, `description`, `input_schema`, `skill`, `read_only`). Names and parameter descriptions retain the existing RobotTools conventions.
- `invoke(name, params, operation_id=..., wait=False)` submits one capability and returns an Observation. Explicit `wait=True` performs bounded waiting; a recovery query may spend only the original remaining wait budget. Unknown latest state is null, with the real task ID retained. Waiting and caller cancellation never imply physical cancellation.
- Action operation IDs are caller-owned nonblank strings. Default keys are `robot:{operation_id}`. The existing Agent adapter explicitly keeps `agent:{operation_id}` and `agent_action` so persisted pending actions continue to address the same tasks.
- SDK observations retain task results/errors without inventing execution facts. New capability errors live outside agent-core; the application adapter maps them to its existing application error contract.
- MCP keeps its five task tools. It additionally exposes allowlisted catalog tools. Skill calls use `{operation_id, params, wait=false}`; task controls use `{task_id}`. Nested input schemas preserve references, dialect and resource IDs. Skill errors set isError; a control call returning an authoritative snapshot does not turn its task's failure/cancellation into a protocol failure. No isError value permits replay of an uncertain physical action.
- The shared TaskRuntime and Observation contracts move out of agent-core. Its graph-facing ToolSet continues to use these shared facts. Business workflows keep their existing confirmation, lease and completion checks.
- No kernel, task-state, vendor, skill-resource or native hardware changes are part of this refactor.

## Task 1: Add independent capability contracts and client

- [x] Create `capabilities/pyproject.toml`, `src/robot_agent_capabilities/{__init__,ports,observations,errors,catalog,client}.py` and `py.typed`.
- [x] Add `capabilities/tests/test_capabilities.py` and `test_contracts.py` with a reusable `check_capability_contract(runtime)` and real embedded/UDS/MCP tests, plus catalog isolation, allowlist, timeout/cancel, stable IDs, recovery and dependency-boundary checks.
- [x] Run the new tests with `PYTHONPATH=/workspace/robot_core/capabilities/src`; first verify missing public API failures, then implement and rerun.

```python
capabilities = await RobotCapabilities.create(runtime, allowed_skills={"audio.speak"})
observed = await capabilities.invoke(
    "robot_speak", {"message": "hello"}, operation_id="request-42", wait=True,
)
assert observed["state"] == "succeeded"
```

## Task 2: Make the built-in agent a consumer

- [x] Add the local capability distribution dependency to `agent-core/pyproject.toml` and `agent/pyproject.toml`.
- [x] Update `agent-core/src/robot_agent_core/ports.py` to reuse the shared execution/fact contracts; retain the graph ToolSet contract.
- [x] Replace `agent/src/robot_agent/robot_tools.py` with a thin schema/application-error adapter over RobotCapabilities; preserve operation IDs, existing task type, keys and tool names.
- [x] Run existing `agent-core/tests` and `agent/tests` independently; verify existing restart and graph boundary tests pass unchanged.

## Task 3: Expose the shared capabilities through real MCP stdio

- [x] Add the local capability dependency to `mcp_bridge/pyproject.toml`.
- [x] Extend `mcp_bridge/src/robot_runtime_mcp/server.py` with cached catalog discovery and capability dispatch. Put protocol wrapping and local JSON reference relocation in `tool_schemas.py`.
- [x] Add `mcp_bridge/tests/test_mcp_capabilities.py` for real stdio catalog, shared SDK/MCP idempotency, background status/cancel, hidden skills, malformed arguments, failed outcomes and JSON Schema references.
- [x] Preserve task-tool payloads, existing event cursors and server ownership; update only the old exact discovery assertion to include the new tools.
- [x] Run all bridge and hotel/guide MCP tests with the installed official MCP SDK.

## Task 4: Document, package and verify

- [x] Add capability README and direct SDK example; document MCP configuration, stable IDs, pending/unknown results and the difference between runtime skills and Agent instruction packages.
- [x] Update root README positioning, public API, state boundary, module tree and current scope; update agent-core, agent and MCP READMEs plus AGENTS.md architecture/test commands.
- [x] Install only local packages with `--no-deps --no-build-isolation` in the existing container environment.
- [x] Run the runtime, capabilities, agent-core, agent, hotel-agent, guide-agent and mcp_bridge suites in separate pytest processes; run compileall, dependency and wheel/import-isolation checks in the container.
- [x] Request independent review of architecture, protocol, idempotency, allowlists, lifecycle and tests. Address blocking findings and rerun affected verification.

## Verification commands

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && PYTHONPATH=/workspace/robot_core/capabilities/src /opt/robot_agent_venv/bin/python -m pytest -q capabilities/tests'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && for suite in . capabilities agent-core agent hotel-agent guide-agent mcp_bridge; do /opt/robot_agent_venv/bin/python -m pytest -q -c "$suite/pyproject.toml" "$suite/tests" || exit "$?"; done'
```

Independent subprojects remain outside the historical root-only `.verify/sweep.py` file list, matching the existing project convention. Existing untracked architecture artifacts belong to the user and remain outside this change.

## Results

2026-11-14 已完成，改动保留在当前工作区，未提交或发布。

- 基线：runtime 507 / agent-core 73 / agent 103 / hotel 117 / guide 53 / MCP 32，共 885 passed。
- 最终源码按项目独立进程完整回归：runtime **507**、capabilities **17**、agent-core **73**、agent **104**、hotel **117**、guide **53**、MCP **42**，共 **913 passed**。最后一轮所有命令均返回 0。
- TDD：能力层首次 14 failed，MCP 具体能力首次 7 failed，共享端口归属首次 1 failed，均确认缺失行为后再实现。后续布尔/数字身份、等待恢复预算、旧 schema 方言/资源 ID、缺失白名单等回归同样先复现再修正。
- 能力包仅声明 runtime 发布依赖；容器为构建补装项目已声明的 setuptools/wheel，未增加产品第三方依赖。runtime 的 pyproject 和全部实现源码保持无工作树差异。
- 构建五个 wheel，安装到独立 target；runtime/capabilities/core/agent/MCP 共 **81** 个 Python/py.typed 文件与当前源码逐字节一致，并确认导入位置来自 target。
- 独立安装包的六组客户端测试：17 + 73 + 104 + 117 + 53 + 42 = **406 passed**；真实 MCP stdio 子进程沿用 target PYTHONPATH，包含跨 SDK/MCP 的同操作同任务验证。
- compileall、pip check 和无模型 capabilities_demo 均通过；示例返回 succeeded、spoken=true。没有连接真实模型或硬件，没有修改用户的 Agent 配置。
- 独立代码审查最初确认三项 Important：超时后无界补查、True/1 被误认为相同操作参数、schema 方言丢失。均修复并独立复核；另保留提前超时/阻塞补查和 Draft4 id 作用域回归。最终白名单复核确认未扩大权限。
- 兼容性：旧 Agent 的 agent: 键、agent_action 类型与错误码保留。完整酒店回归发现 MCP 白名单缺失项不应阻断工具发现，已恢复原过滤语义，由业务应用继续验证必需技能。

### 额外 Runtime 验证中的不稳定项

对独立安装包追加的 Runtime 全套/分文件重复验证，曾在状态推送和线程边界用例出现超时；不能据此宣称安装包 Runtime 的重复压力回归已稳定通过。
排查确认两个测试在 super.watch 真正注册队列前就跨线程发出 ready，已只调整 test_isolation.py 和 test_thread_binding_boundaries.py 的就绪通知顺序；不修改 runtime 实现。
独立审查确认同步顺序正确，相关三个用例单独通过；重复压力执行仍曾挂起，需后续单独定位。最终源码全套 913 passed 与安装包客户端 406 passed 的结论不包含对这项重复执行稳定性的保证。

构建、逐文件对照与额外诊断保留在 `.verify/unified-capabilities-gmfnahx7/`，包括 `package-verification.json`、`installed-runtime-tests.log` 和分文件日志；早期源码 Runtime 日志为 `.verify/unified-capabilities-runtime.log`。
已有未跟踪的 docs/architecture/ 未被重建，正式结构与接入说明已同步到根 README 和 docs/agent-integration.md。
