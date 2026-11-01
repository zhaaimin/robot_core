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

可运行的完整示例见 `examples/embedded_demo.py`、`examples/remote_demo.py` 和
`examples/precondition_demo.py`。

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
| `close()` | 关闭运行时并释放其持有的资源；外部 SDK 连接由应用随后关闭 |

接入 CC API 语音时，应用先调用 `CcApiSpeechSkill.start()` 注册回调，再启动运行时；
退出时先关闭运行时，再关闭技能适配器，最后停止 SDK 连接。

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

CC API 的 `audio.speak` 在启动受理后保持 `RUNNING`，收到匹配 `workId` 的
`cc.api.work.finished` 才成功。首版按约定不解析 finished 的结束原因，外部停止/失败
如果也发出该事件，同样被视为成功。运行时主动取消仍等待停止确认后进入 `CANCELED`；
停止未确认则报告失败。技能超时仍为 `FAILED / SKILL_TIMEOUT`。

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
├── ports.py               技能、仓储、对账、状态源与回调型机器人 API 的 Protocol
├── core.py                RuntimeCore：运行时生命周期与公开任务 API
├── task_state.py          TaskStateStore：本机任务归属与状态机唯一写入点
├── executor.py            WorkflowExecutor：单任务步骤编排
├── registry.py            SkillRegistry：技能注册与查找
├── policy.py              PolicyEnforcer：准入策略（Schema 校验、权限与前置条件检查）
├── resources.py           ResourceLocks：按技能声明的资源互斥
├── events.py              EventBus：历史回放 + 实时事件推送
├── recovery.py            重启恢复与状态对账
├── repository.py          InMemoryTaskRepository
├── sqlite_repository.py   SQLiteTaskRepository
├── wire.py                NDJSON 成帧与编解码
├── server.py              RuntimeServer：UDS 守护进程
├── sdk/
│   ├── embedded.py        RobotRuntime
│   └── remote.py          RemoteRuntime
└── adapters/
    ├── cc_api.py          CcApiSpeechSkill：CC API 语音播报、结束事件与停止确认
    └── simulator.py       模拟技能、Reconciler 与两个模拟机器人状态源（无硬件自测用）
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
    preconditions={"estop.clear", "battery.sufficient"},
    timeout_s=30,
    supports_cancel=True,
)
```

实现 `ports.Skill` Protocol 即可接入：`definition` 属性 + `async execute(context, params)`。

## CC API 语音播报

`CcApiSpeechSkill` 接受已连接、专用的 `ubt_robot.Api` 实例，实现 `audio.speak`：

```python
from robot_agent_runtime.adapters.cc_api import CcApiSpeechSkill

speech = CcApiSpeechSkill(api)
await speech.start()
runtime = RobotRuntime(
    InMemoryTaskRepository(), {"audio.speak": speech}, allowed_permissions={"audio"},
)
```

提交参数为 `{"message": "你好，我是机器人"}`。技能声明 `speaker` 资源互斥、`audio`
权限、空前置条件、30 秒默认超时和支持取消。底层发送 `cc.api.work.start`，技能码
`A000001`，语速与音量均为 50；每次播报具有独立 `traceId`。

启动回调提供 `workId`，适配器按它匹配 finished，允许 finished 早于启动回调到达。
取消、超时和关闭时只停止当前 work，停止请求受理不等于停止完成，还需要 finished。
停止清理最多等待 5 秒；断连、缺失 workId 或停止未确认后，该实例拒绝继续播报。
应用需要先确认旧播报已经结束，再建立新连接和适配器。超时/关闭时的状态沿用运行时
语义，不代表硬件停止已获确认。首版不提供重启对账，也不自动重试播报。

运行示例使用底层异步回调接口，将回调转回 asyncio；不调用 `cc_api/` 中包含阻塞等待的
`RobotClient`。SDK 的连接启动和停止放在事件循环之外。适配器独占该连接的 finished
和 disconnect 回调，不能和其他代码在同一 Api 实例上覆盖这些处理器。

完整入口为 `examples/cc_api_speech_demo.py`。在装好 `ubt_robot` 的 Linux ARM64
环境中执行（`CC_API_TOKEN` 通过部署环境传入）：

```bash
python3 examples/cc_api_speech_demo.py \
  --api-id YOUR_API_ID --address ROBOT_IP:48159 --message '你好，我是机器人'
```

运行在机器人本机时可省略 `--address`。SDK 不属于内核依赖，宿主常规导入不需要它。
容器中的自动测试使用假回调 API，不连接机器人；真实发声和机器人结束事件仍需现场验证。

## 机器人状态前置条件

急停、低电、底盘锁定这类**机器人自身的状态**不是任务状态，不占 `TaskState` 的取值。技能
用 `preconditions` 声明自己需要哪些条件成立，`RobotStateProvider` 报告当前**不成立**的条件
标签集合，运行时只做集合求交：

```python
blocked = skill.definition.preconditions & await provider.unsatisfied()
```

标签对运行时是不透明的。`battery.sufficient` 具体是电压阈值还是 SOC 百分比由适配器决定，
所以内核里没有任何机器人领域模型。**急停因此不是特例**——它只是一个几乎所有运动技能都会
声明的标签。

判定发生在三个时刻，对所有条件一视同仁：

| 时刻 | 位置 | 不满足时 |
| --- | --- | --- |
| 提交准入 | `PolicyEnforcer`，整单取一次快照 | 整单 `FAILED` + `PRECONDITION_FAILED` |
| 每步启动前 | `WorkflowExecutor`，取到资源锁之后、`SKILL_STARTED` 之前 | `FAILED` + `PRECONDITION_FAILED`，该步未执行 |
| 执行中条件变化 | `RuntimeCore` 消费 `provider.watch()` 的推送 | 先置 `cancel_event`，再记 `TASK_PRECONDITION_LOST` 事件，最终 `CANCELED` |

只有**声明了**已损坏标签的技能会被打断：急停期间一个正在播放的语音任务不受影响，因为
`audio.speak` 没有声明 `estop.clear`。

未注入 `RobotStateProvider` 时整条检查跳过。读取状态失败按**失败关闭**处理——读不到机器人
不等于机器人可用，提交会以 `INTERNAL_ERROR` 失败。`watch()` 可以返回一个立即耗尽的迭代器，
此时降级为仅检查点模式：仍然不会启动机器人做不到的事，但已经在跑的任务不再被打断。

> 这套机制是**软件层面**的准入与打断，不是功能安全。真正的急停必须是硬件回路；这里做的
> 是在硬件已经断电之后让软件状态与事件记录追上现实。

### 两个模拟实现

`adapters/simulator.py` 提供两个 `RobotStateProvider`，区别是标签从哪来：

| 类 | 标签来源 | 用途 |
| --- | --- | --- |
| `SimulatedRobotState` | 调用方直接给，`set_unsatisfied({...})` | 测试替身，精确控制时序 |
| `SimulatedRobot` | 从模拟读数**推导**，`set_estop()` / `set_voltage()` / `drain()` | 贴近真实适配器的参考实现 |

`SimulatedRobot` 多做两件真实适配器必须做、测试替身不需要做的事：

- **滞环**：电压掉到 `low_voltage` 以下才破，回到 `recovery_voltage` 以上才恢复。没有这个带宽，
  一块停在阈值上的电池会反复抖动，每次推送都打断刚起来的任务。
- **无变化不推送**：重新推导出同一个集合就不发，否则一个 10 Hz 的传感器会每秒唤醒运行时 10 次
  去重算同一个答案。

电池阈值要留够开回充电桩的余量——被打断的导航会把机器人**停在原地**，阈值定得太低这套机制
救不了场，只是换个姿势搁浅。同理 `power.charge` 这类技能**绝不能**声明 `battery.sufficient`，
否则电量低时连充电都会被拒。

`examples/precondition_demo.py` 把三个时刻跑成一个可执行脚本：满电准入 → 行进中电量跌破阈值
被打断 → 同一块电量下导航被拒而语音播报照常完成。

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
docker start robot_core_ubuntu24
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && python3 -m pytest -q'
```

本机验证使用保留的 `robot_core_ubuntu24` 容器，基于 `ubt-api-ubuntu24-ros2:latest`，
项目挂载到 `/workspace/robot_core`。测试依赖及附带的 `ubt_robot 2.0.16` 已安装；
验证后保留容器，下次直接复用。测试、语法检查和示例验证均在容器中执行。

测试按关注点组织：`test_models` 契约、`test_core` 执行路径、`test_faults` 故障注入、
`test_recovery` 重启恢复、`test_preconditions` 机器人状态前置条件、`test_sqlite_repository`
持久化、`test_embedded_sdk` / `test_remote_sdk` 两种接入形态、`test_contracts` 供后续适配器
复用的契约测试、`test_cc_api` 语音事件匹配与取消/断连边界。

## 当前范围

已实现：运行时内核、技能层框架、Python SDK（嵌入式 + 远程）、UDS 守护进程、SQLite
持久化、模拟适配器、CC API 语音播报适配器（已做离线验证，待真机联调）。

尚未实现：Agent 集成层（计划生成与重规划）、调度器（`priority` 字段已持久化但不参与
排序，无并发上限与抢占）、有限重试、结构化日志、其他真实机器人能力适配（导航、
ROS 2 等）、CC API 结束原因解析与重启对账、云端管理平面。
