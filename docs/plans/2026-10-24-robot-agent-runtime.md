# Robot Agent Runtime MVP Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Build a tested, platform-independent async Runtime Core usable through both an embedded Python SDK and a UDS daemon with a remote SDK.

**Architecture:** Ports & Adapters with one Runtime Core. The embedded SDK calls the core directly; the UDS service calls the same core through a thin NDJSON protocol layer. SQLite persists task snapshots and ordered events; a simulator validates behavior without hardware.

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2, jsonschema, aiosqlite, pytest, pytest-asyncio.

---

### Task 1: Project and public contracts

**Files:**
- Create: `pyproject.toml`
- Create: `src/robot_agent_runtime/{__init__,models,errors,ports}.py`
- Test: `tests/test_models.py`

**Steps:**
1. Write failing tests for task request validation, terminal states, skill definitions and stable error serialization.
2. Run `pytest tests/test_models.py -v`; expect import failures.
3. Implement enums and Pydantic models with UTC timestamps and `model_dump(mode="json")` serialization.
4. Define Skill, TaskRepository and Reconciler protocols; no vendor imports.
5. Re-run tests; expect PASS.

Key contract:

```python
class SkillCall(BaseModel):
    skill: str
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float | None = Field(default=None, gt=0)

class TaskRequest(BaseModel):
    task_type: str
    steps: list[SkillCall] = Field(min_length=1)
    timeout_s: float | None = Field(default=None, gt=0)
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### Task 2: Repository and ordered events

**Files:**
- Create: `src/robot_agent_runtime/{repository,sqlite_repository}.py`
- Test: `tests/test_sqlite_repository.py`

**Steps:**
1. Write failing async tests for create/get/update, idempotency lookup, ordered event append and nonterminal queries.
2. Run tests and verify missing-module failure.
3. Implement an in-memory repository for fast tests and SQLite implementation with unique task IDs, idempotency keys and `(task_id, sequence)`.
4. Verify restart by closing and reopening the same database.
5. Run repository tests; expect PASS.

### Task 3: Runtime Core execution

**Files:**
- Create: `src/robot_agent_runtime/{events,resources,safety,core}.py`
- Create: `src/robot_agent_runtime/adapters/{__init__,simulator}.py`
- Test: `tests/{test_core,test_faults}.py`

**Steps:**
1. Write failing tests for successful workflows, schema rejection, missing skills, cancellation, timeout, resource exclusion and exception mapping.
2. Run focused tests; expect missing RuntimeCore failure.
3. Implement skill registry, validation, resource locks, event emission and task state transitions.
4. Implement cooperative SkillContext cancellation and per-step `asyncio.wait_for` timeout.
5. Implement simulator navigation/audio/state skills with controllable delays and failures.
6. Run focused tests; expect PASS.

Required transition helper:

```python
async def _transition(task_id: str, state: TaskState, **changes: Any) -> Task:
    task = await self._repository.update_task(task_id, state=state, **changes)
    await self._emit(task_id, EventType.TASK_STATE_CHANGED, {"state": state.value})
    return task
```

### Task 4: Startup recovery

**Files:**
- Create: `src/robot_agent_runtime/recovery.py`
- Modify: `src/robot_agent_runtime/core.py`
- Test: `tests/test_recovery.py`

**Steps:**
1. Write a failing test that persists RUNNING work and verifies a new core does not execute it again.
2. Add cases for authoritative terminal reconciliation and missing reconciliation support.
3. Implement recovery before accepting new work: terminal authoritative results are preserved; unknown work becomes `RECONCILIATION_REQUIRED`.
4. Run recovery and existing core tests; expect PASS.

### Task 5: Embedded and remote SDKs

**Files:**
- Create: `src/robot_agent_runtime/{sdk,protocol,server}.py`
- Test: `tests/{test_embedded_sdk,test_remote_sdk}.py`

**Steps:**
1. Write failing tests for the embedded public API.
2. Implement `RobotRuntime` as a thin RuntimeCore facade.
3. Write failing UDS tests for health, task submission/query/cancel, skill listing, event subscription and structured errors.
4. Implement one-request-per-connection NDJSON for unary methods and long-lived connections for subscriptions.
5. Implement `RemoteRuntime` with the same asynchronous public method shapes.
6. Run SDK tests; expect PASS.

Wire request:

```json
{"id":"req-1","method":"submit_task","params":{"request":{"task_type":"demo","steps":[]}}}
```

Wire response:

```json
{"id":"req-1","ok":true,"result":{}}
```

### Task 6: Examples and full verification

**Files:**
- Create: `examples/{embedded_demo,remote_demo}.py`
- Create: `src/robot_agent_runtime/__main__.py`
- Test: `tests/test_contracts.py`

**Steps:**
1. Add contract tests reusable by future Walker and ROS 2 adapters.
2. Implement embedded and daemon examples with explicit cleanup in `finally` blocks.
3. Install the package editable into the isolated Python environment.
4. Run `python -m pytest -q`; expect all tests passing.
5. Run embedded and remote demos; verify SUCCEEDED output and ordered events.
6. Review public API, cancellation, recovery and resource cleanup against the design.

### Completion criteria

- Same RuntimeCore serves embedded and remote forms.
- Core has no ROS 2, Walker or vendor imports.
- No automatic replay of recovered nonterminal work.
- All public errors are structured; all demos and tests clean up tasks, servers, subscribers and temporary sockets.
- Full test suite and both demos pass in the isolated environment.
