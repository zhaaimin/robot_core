# Robot Agent Runtime

机器人本机任务执行运行时。管理 AI 任务的生命周期、技能调度、状态与事件、取消语义和重启恢复。

运行时内核不依赖任何机器人厂商 SDK、ROS 2 或云端服务：硬件接入通过 `ports.py` 中的
Protocol 以适配器形式注入。

## 安装

```bash
pip install -e '.[test]'
```

需要 Python 3.11+。

## 两种使用形态

同一个 `RuntimeCore` 支撑两种接入方式，公开方法签名一致。

### 嵌入式（同进程）

```python
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import SkillCall, TaskRequest
from robot_agent_runtime.repository import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime

simulator = Simulator()
runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
await runtime.start()
try:
    task = await runtime.submit_task(
        TaskRequest(
            task_type="announcement",
            steps=[
                SkillCall(skill="navigation.navigate_to", params={"target": "meeting_room"}),
                SkillCall(skill="audio.speak", params={"message": "The meeting starts at 3 PM."}),
            ],
        )
    )
    async for event in runtime.subscribe_task_events(task.id):
        print(event.event_type, event.data)
finally:
    await runtime.close()
```

### 远程（跨进程，Unix Domain Socket）

启动守护进程：

```bash
robot-agent-runtime serve --socket /tmp/runtime.sock --database /tmp/tasks.db
```

客户端：

```python
from robot_agent_runtime.sdk import RemoteRuntime

runtime = RemoteRuntime("/tmp/runtime.sock")
await runtime.start()
try:
    task = await runtime.submit_task(request)
finally:
    await runtime.close()
```

可运行的完整示例见 `examples/embedded_demo.py` 和 `examples/remote_demo.py`。

## 公开 API

`RobotRuntime` 与 `RemoteRuntime` 均提供：

| 方法 | 说明 |
| --- | --- |
| `start()` | 启动运行时；先完成重启恢复，再接受新任务 |
| `submit_task(request)` | 提交任务，返回快照；校验失败直接返回 `FAILED` 任务 |
| `get_task_status(task_id)` | 读取持久化的任务状态 |
| `cancel_task(task_id)` | 请求取消并等待任务进入终态 |
| `list_skills()` | 列出已注册技能定义 |
| `subscribe_task_events(task_id, after_sequence=0)` | 订阅事件，先回放历史再接续实时事件 |
| `close()` | 关闭运行时并释放所有资源 |

## 任务状态机

```
CREATED → VALIDATING → QUEUED → RUNNING → SUCCEEDED
                           │        │  ↕
                           │        │  WAITING（等待资源互斥锁）
                           │        ├→ FAILED
                           │        └→ CANCELING → CANCELED
                           └→ FAILED（校验未通过）

重启恢复：UNKNOWN / RECONCILIATION_REQUIRED
```

终态为 `SUCCEEDED`、`FAILED`、`CANCELED`、`UNKNOWN`、`RECONCILIATION_REQUIRED`。

## 重启恢复语义

运行时**不会**因为数据库里是 `RUNNING` 就重放任务。`start()` 时对每个非终态任务：

1. 若注入了 `Reconciler` 且它返回权威终态（`SUCCEEDED` / `FAILED` / `CANCELED` / `UNKNOWN`），采信该状态；
2. 否则（无 reconciler、reconciler 抛异常、返回不确定结果）任务落入
   `RECONCILIATION_REQUIRED`，等待人工或上层介入。

这条规则用于避免重复执行非幂等的物理动作。

## 模块结构

```
src/robot_agent_runtime/
├── models.py              任务/事件/技能的数据模型与状态枚举
├── errors.py              结构化错误码
├── ports.py               Skill / TaskRepository / Reconciler 三个 Protocol
├── core.py                RuntimeCore：运行时生命周期与公开任务 API
├── task_state.py          TaskStateStore：本机任务归属与状态机唯一写入点
├── executor.py            WorkflowExecutor：单任务步骤编排
├── registry.py            SkillRegistry：技能注册与查找
├── policy.py              PolicyEnforcer：准入策略（Schema 校验与权限检查）
├── resources.py           ResourceManager：按技能声明的资源互斥
├── events.py              EventBus：历史回放 + 实时事件推送
├── recovery.py            重启恢复与状态对账
├── repository.py          InMemoryTaskRepository
├── sqlite_repository.py   SQLiteTaskRepository
├── protocol.py            NDJSON 线协议
├── server.py              RuntimeServer：UDS 守护进程
├── sdk/
│   ├── embedded.py        RobotRuntime
│   └── remote.py          RemoteRuntime
└── adapters/
    └── simulator.py       模拟技能与 Reconciler（无硬件自测用）
```

## 技能定义

每个技能声明自己的契约，由运行时统一执行校验、加锁、超时与取消：

```python
SkillDefinition(
    name="navigation.navigate_to",
    version="1.0.0",
    input_schema={
        "type": "object",
        "required": ["target"],
        "properties": {"target": {"type": "string"}},
    },
    resources={"base"},          # 同名资源上的技能串行执行
    permissions={"robot.navigation"},
    timeout_s=30,
    supports_cancel=True,
)
```

实现 `ports.Skill` Protocol 即可接入：`definition` 属性 + `async execute(context, params)`。

## 内部分工

三个模块的边界是“谁可以写状态”：

- `TaskStateStore` 持有六个以 task id 为键的字典（任务、锁、序号、取消事件、进度任务、
  执行任务），并提供唯一的 `transition()`。它在持有该任务锁的前提下把快照与事件交给仓储
  一次原子写入，再把权威结果回填到活的 `Task` 对象，最后才发布到事件总线。
- `WorkflowExecutor` 自身无状态，只负责顺序：申请资源 → 带超时启动技能 → 把结果、超时、
  取消、意外异常映射成恰好一次终态转换。
- `RuntimeCore` 负责生命周期（启动恢复、关闭）与公开 API，并在提交时完成校验。

所有带 guard 的转换都走同一把任务锁，因此“取消请求”与“执行推进”是公平竞速，不存在已
取消的任务又被推进成功的窗口。

## 测试

```bash
python -m pytest -q
```

测试按关注点组织：`test_models` 契约、`test_core` 执行路径、`test_faults` 故障注入、
`test_recovery` 重启恢复、`test_sqlite_repository` 持久化、`test_embedded_sdk` /
`test_remote_sdk` 两种接入形态、`test_contracts` 供后续适配器复用的契约测试。

## 当前范围

已实现：运行时内核、技能层框架、Python SDK（嵌入式 + 远程）、UDS 守护进程、SQLite
持久化、模拟适配器。

尚未实现：Agent 集成层（计划生成与重规划）、调度器（`priority` 字段已持久化但不参与
排序，无并发上限与抢占）、有限重试、结构化日志、真实机器人平台适配器（ROS 2 / 厂商
SDK）、云端管理平面。
