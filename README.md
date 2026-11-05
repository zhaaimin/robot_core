# Robot Agent Runtime

机器人本机任务执行运行时。管理 AI 任务的生命周期、技能调度、状态与事件、取消语义和重启恢复。

运行时内核不依赖任何机器人厂商 SDK、ROS 2 或云端服务：硬件接入通过 `ports.py` 中的
Protocol 以适配器形式注入。

完整流程与能力边界见 [任务执行、结果获取、持久化与重启状态核对](docs/task-execution-and-recovery.md)，
包含 CC API 语音示例、SQLite 与 InMemory 的区别，以及 Reconciler 当前能处理的范围。

## 安装

```bash
pip install -e '.[test]'
```

需要 Python 3.11+。

## 两种使用形态

同一个 `RuntimeCore` 支撑两种接入方式，公开方法签名一致。

`RobotRuntime` 和 `RuntimeServer` 都把内核托管在专用 daemon 线程的 asyncio 循环上。
应用、SDK 监听、技能和厂商接口的循环不与内核共用。状态机、资源锁和事件总线仍只在
内核循环操作，线程交接由边界模块负责；本方案不使用子进程。

### 嵌入式（同进程）

```python
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import SkillCall, TaskRequest
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
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

服务端从 `robot_agent_runtime.transport.server` 导入 `RuntimeServer`；低层内核从
`robot_agent_runtime.kernel.core` 导入 `RuntimeCore`。使用下方模块结构中的新路径导入。

| 方法 | 说明 |
| --- | --- |
| `start()` | 嵌入式按对象的线程绑定启动并完成重启恢复；远程模式确认服务可用 |
| `submit_task(request)` | 提交任务，返回快照；校验失败直接返回 `FAILED` 任务 |
| `get_task_status(task_id)` | 读取持久化的任务状态 |
| `cancel_task(task_id)` | 请求取消并在有限停止确认期后返回终态；无法确认时进入待对账状态 |
| `list_skills()` | 列出已注册技能定义 |
| `subscribe_task_events(task_id, after_sequence=0)` | 分页回放历史后接续实时事件；订阅容量独立于查询和取消 |
| `add_task_listener(on_event=..., on_done=..., on_error=...)` | 监听本 SDK 后续提交的任务，返回 `TaskListener`；异步回调在事件循环，同步回调在有界守护线程池 |
| `close()` | 嵌入式停止内核并限时清理监听；远程模式清理本客户端监听和连接 |

`RobotRuntime(..., options=RuntimeOptions(...))` 与
`RuntimeServer(repository, skills, socket_path, options=RuntimeOptions(...))` 可以配置隔离边界。
服务端直接接收端口，不再接收已创建的 `RuntimeCore`：

```python
from robot_agent_runtime.transport.server import RuntimeServer

server = RuntimeServer(
    repository, skills, socket_path,
    reconciler=reconciler,
    allowed_permissions={"navigation", "audio"},
    robot_state=robot_state,
)
```

隔离配置示例：

```python
from robot_agent_runtime.sdk import RuntimeOptions

options = RuntimeOptions(
    port_timeout_s=5.0,
    cancel_grace_s=0.2,
    max_extension_threads=32,
    max_pending_calls=256,
    max_control_calls=16,
    max_subscriptions=256,
    max_snapshot_bytes=1_048_576,
    max_snapshot_depth=64,
    event_page_size=128,
    max_event_buffer=1000,
)
```

端口操作默认期限 5 秒。`TaskRequest.timeout_s` 从接受请求开始计算总期限，覆盖准入校验、
排队、资源等待与执行；每步受技能声明或单步覆盖和剩余总预算共同限制。
停止确认与持久化各自有界，不计入执行预算。技能收到取消信号后有 0.2 秒协作退出，
声明支持取消时才进一步请求协程取消，并再等待最多 0.2 秒。
需要更长硬件停止确认时间的部署须相应增加 `cancel_grace_s`。
普通调用、订阅与控制操作分别限额，查询、取消和关闭不被空闲订阅挤出；外部线程
数量（含声明校验线程）有上限，卡住的线程不会补建。容量耗尽报告
`EXTENSION_CAPACITY_EXCEEDED`。`RemoteRuntime(socket_path, options=...)` 仅使用本地
监听的停止确认期限，不配置服务端限额。

单个跨边界快照默认最多 1 MiB、嵌套深度 64，结果与错误在工作线程规范化后交接，
超限报告 `SNAPSHOT_TOO_LARGE`。事件历史默认每页 128 条；内存和 SQLite 仓储提供
可选 `PagedEventRepository.list_events_page()`，旧仓储在工作线程内裁页兼容。
实时队列与关闭后事件缓冲默认各限 1000 条，溢出显式报告 `EVENT_STREAM_OVERFLOW`，
附带最后交付的序号，调用方可据此恢复回放。

技能、仓储、状态源和对账器各有稳定的外部线程归属，运行期间不要在应用中直接调用或修改
这些对象中的 asyncio 状态。技能取消信号、进度、参数和结果由代理交接；事件仍先回放后
接实时。输入和结果须能表示为 JSON 数据。低层 `RuntimeCore` 单独使用时是受信任的
asyncio 引擎，不自动提供线程隔离；面向业务的入口应使用 SDK 或 `RuntimeServer`。

`KernelHost` 在专用内核循环中准备外部代理，随后创建唯一的 `RuntimeCore`。内核从构造时
就使用最终端口、事件容量和准入校验器，不接收线程绑定，也不在运行期间替换依赖。
`AdmissionValidator` 只负责准入规则：默认本地实现在当前循环校验，托管实现将相同规则
交给有界校验线程；任务状态始终由内核写入。

关闭由宿主协调：先 `RuntimeCore.stop()` 完成停止确认和终态持久化并结束事件生产，
再排空已打开订阅的事件，之后 `RuntimeCore.close()` 关闭仓储，最后关闭外部循环和内核
循环。`stop()` 与 `close()` 各自共享真实完成结果，重复调用和等待方取消不重复清理。
`KernelHost.begin_close()` 发起同一次关闭并立即返回，`close()` 等待结果；SDK 先发起
业务停止，再限时清理监听。停止或排空失败仍继续资源释放并汇总异常。

### 外部对象的线程归属

注册已有对象时可以用 `ThreadBinding` 选择线程，不传绑定保持原有的对象独占行为：

| 注册方式 | 在一个运行时内的线程归属 |
| --- | --- |
| `ThreadBinding.shared(obj)` | 所有选择 shared 的对象共用一个外部线程 |
| `ThreadBinding.group(obj, "navigation")` | 同名组共用一个外部线程，不同组彼此独立 |
| `ThreadBinding.dedicated(obj)` 或直接传 `obj` | 该对象独占一个外部线程 |

```python
from robot_agent_runtime.sdk import RobotRuntime, ThreadBinding

runtime = RobotRuntime(
    repository,  # 仓储保持默认独占
    {
        "navigation.navigate_to": ThreadBinding.group(navigate_skill, "navigation"),
        "navigation.position": ThreadBinding.group(position_skill, "navigation"),
        "audio.speak": ThreadBinding.shared(speak_skill),
        "vendor.blocking": ThreadBinding.dedicated(blocking_skill),
    },
    robot_state=navigation_state,  # 状态源保持默认独占，及时交付前置条件变化
)
```

上面的变量代表应用预先创建的对象。`blocking_skill` 是示例变量名，表示可能调用同步阻塞
接口的技能对象，例如等待返回的厂商 SDK 方法、同步网络请求或 `time.sleep()`；
`"vendor.blocking"` 是示例注册名。两者都由应用自行定义，框架没有内置 blocking 技能。
`ThreadBinding.dedicated(blocking_skill)` 为它选择独占线程，避免同步阻塞拖住共享线程
中的其他对象。线程策略由应用显式选择，运行时不会自动检测技能是否阻塞。
真正的异步 I/O 或 `await asyncio.sleep()` 会让出事件循环，适合使用共享或指定组线程；
仅把包含同步阻塞调用的函数声明成 `async def`，仍会阻塞它所在的线程。

技能、仓储、状态源和对账器都支持绑定。`RuntimeServer` 直接在端口构造参数中
接收相同绑定，远程客户端不能改变服务端线程归属。共享和指定组均为运行时内部线程，
不使用应用循环；默认 shared 线程与名为 `"shared"` 的指定组分开，不同运行时也不共享。
同一对象按身份去重，重复注册必须使用相同模式和组名；普通对象等同 dedicated，
与 shared/group 混用会在启动外部线程前以 `INVALID_REQUEST` 拒绝。
绑定目标必须是实际对象，`ThreadBinding.shared(None)` 等空目标会在构造时以
`INVALID_REQUEST` 拒绝；可选端口直接传 `None` 仍表示不启用该端口。

线程名标明配置归属：默认共享为 `robot-extension-shared`，指定组为
`robot-extension-group-组名`，独占为 `robot-extension-dedicated-序号`。
这些名称可在线程调试器中核对；不同运行时可能出现相同名称，仍是不同线程。

`max_extension_threads` 按实际分配的外部线程加一个校验线程计数，内核和 SDK 回调线程
另计。共享线程上的所有对象共用 `max_pending_calls` 和控制调用额度，超限立即报错。
同一线程内逐个读取技能声明，不会因技能数量多而在启动时挤满调用额度。

共享只统一线程与事件循环，不保证调用串行；协程在 `await` 时可以交错，资源互斥仍由
运行时资源锁负责。同步阻塞影响同组全部对象。**需要及时触发任务打断的状态源，不得与
可能同步阻塞的技能共用事件循环**，否则 `watch()` 无法交付标签变化，运行中的任务不能
及时响应前置条件丢失。状态源建议保持默认独占；适配器若自行交回其他循环，实际状态读取
和推送所在的循环也必须避免同步阻塞。线程配置不会检测或自动禁止这些业务层组合。
仓储建议保持默认独占，避免技能阻塞拖住任务持久化。某技能停止未确认只隔离该技能
实例和资源，不关闭同组其他对象的线程。
组内其他调用若也因阻塞而到期，仍分别遵守原有超时和故障关闭规则。

绑定只控制运行时后续调用，`MySkill(...)` 构造仍在调用方线程；不提供工厂构造或自动迁移
已绑定的 asyncio 对象。CC API 仍会将厂商操作交回适配器 `start()` 所在循环。
低层 `RuntimeCore` 只接收普通端口对象并在当前循环执行；线程绑定交给 SDK/Server。

端口截止后按失败关闭：状态读取失败仍以 `INTERNAL_ERROR` 拒绝提交；对账超时不猜测
终态；仓储操作超时后报告 `EXTENSION_TIMEOUT`。若调用已开始，则禁止继续使用该仓储，
避免不确定提交之后继续写入；仅排队且已确认撤销、从未开始的调用不会禁用仓储，也不会
在队列恢复后补执行。仓储不可用时，任务最终状态可能无法落盘，应在恢复仓储后对账。

接入 CC API 语音时，应用先调用 `CcApiSpeechSkill.start()` 注册回调，再启动运行时；
退出时先关闭运行时，再关闭技能适配器，最后停止 SDK 连接。

### 统一注册任务监听

`RobotRuntime` 与 `RemoteRuntime` 使用同样的接口。在提交任务之前注册一次，回调通过
`event.task_id` / `task.id` 区分任务；不扫描旧任务，也不接收其他 SDK 实例提交的任务。

```python
from robot_agent_runtime.models import Event, Task

def on_event(event: Event) -> None:
    print("执行过程", event.task_id, event.event_type, event.data)

def on_done(task: Task) -> None:
    print("最终结果", task.id, task.state, task.result, task.error)

def on_error(task_id: str, error: Exception) -> None:
    print("监听异常", task_id, error)

listener = runtime.add_task_listener(
    on_event=on_event, on_done=on_done, on_error=on_error,
)
await runtime.start()
try:
    first = await runtime.submit_task(request_a)
    second = await runtime.submit_task(request_b)
    # 此处可以继续做其他异步工作。
    await listener.wait()
finally:
    await runtime.close()
```

回调支持普通函数和 `async def` 函数，至少提供一个。每个任务内部按事件顺序调用，
终态事件的 `on_event` 完成后，查询最终快照并调用一次 `on_done`。不同任务各自监听，
异步回调（包括 `async __call__`）在调用方事件循环运行、可协作取消，函数内仍须避免同步
阻塞代码。普通函数在 daemon 工作线程运行；若返回 awaitable，运行时会回到事件循环
等待它。回调收到独立的深拷贝快照。

异步回调即使阻塞调用方循环，也不在内核循环执行；该调用方自己的请求和 `close()` 仍可能
无法继续。可以从其他正常运行的调用方查询内核，不能把调用方循环停住等同于内核停住。

同步回调不能直接操作 asyncio Future、Event 或 Queue，也不能直接调用 SDK 的异步方法；
需要这些操作时使用 `async def` 回调，或通过 `loop.call_soon_threadsafe()` 通知事件循环。
同一任务按事件顺序执行；不同任务或不同注册的同步回调可能并发，调用方共享的数据须自行同步。

每个 SDK 实例共享 4 个工作线程，最多排队 64 个同步回调，排队期限 5 秒。队列满时报告
`LISTENER_CALLBACK_QUEUE_FULL`，排队过期报告 `LISTENER_CALLBACK_TIMEOUT`，结束对应任务
的监听，不影响业务任务。同步 `on_error` 同样受容量与期限约束，无法执行时交给事件循环
异常处理器；异步 `on_error` 不占线程槽位。这些是 SDK 内部限额，目前不提供公开配置。

嵌入式 `close()` 先启动内核关闭；两种 SDK 均限时注销监听、取消尚未被线程领取的工作，
不等待已经领取的同步回调。
异步监听即使吞掉取消，也只能占用 `cancel_grace_s` 的清理等待；副作用可能继续，
运行时会停止后续交付并遗弃迟到结果。
已领取的回调视为在途：其外部副作用可能继续，运行时丢弃完成结果和异常，进程退出不会
等待这些守护线程。永久阻塞的线程一直占用槽位，不会被超时终止或补建；所有槽位阻塞后，
后续同步回调只能排队过期或因队列满而失败。需要观察最终回调须先 `wait()` 再 `close()`，
但 `wait()` 本身不会为正在运行的回调设置执行期限。

任务成功、失败、取消都交给 `on_done`；订阅、查询、回调异常交给
`on_error(task_id, exception)`，随后结束该任务的监听，不改变业务任务状态。
没有错误回调或错误回调自身失败时，由 asyncio 事件循环异常处理器报告。回调自行抛出
`CancelledError` 按 `LISTENER_CALLBACK_FAILED` 上报；真正关闭监听时的取消仍正常传播。
事件流提前结束不会冒充任务完成。慢回调可能触发现有的事件队列溢出，不会自动重试；
需要重放时仍可用 `subscribe_task_events` 及 `after_sequence`。

| 监听句柄操作 | 行为 |
| --- | --- |
| `await listener.wait()` | 等待调用时已经登记的任务监听完成；以后仍可提交新任务并再次等待 |
| `await listener.close()` | 注销并取消排队回调，遗弃在途同步回调结果；不取消业务任务，可以重复调用 |

取消 `wait()` 的调用者不会取消后台监听。同一个注册对同一任务 ID 只监听一次，
所以重复提交幂等请求不会重复通知；去重 ID 保留到注销。多次注册得到独立的监听句柄。
SDK `close()` 会停止监听，不保证交付尚未完成的回调，要收齐结果应先 `wait()`。
监听流关闭或失败后 `wait()` 也会返回，因此业务成功要以 `on_done` 的任务状态为准。
回调内部不能 `wait()` 自己的监听句柄，也不要吞掉取消信号。

## 任务状态机

目录按职责归类，任务状态、持久化与停止确认语义保持一致。
`stop()` / `close()` 是运行时生命周期阶段，不增加业务任务状态；停止未确认仍落待对账。

```
CREATED → VALIDATING → QUEUED → RUNNING → SUCCEEDED
                           │        │  ↕
                           │        │  WAITING（等待资源互斥锁）
                           │        ├→ FAILED
                           │        └→ CANCELING → CANCELED
                           └→ FAILED（校验未通过）

重启恢复：UNKNOWN / RECONCILIATION_REQUIRED
执行或取消无法确认停止：RUNNING / CANCELING → RECONCILIATION_REQUIRED
```

终态为 `SUCCEEDED`、`FAILED`、`CANCELED`、`UNKNOWN`、`RECONCILIATION_REQUIRED`。

注册式监听只观察这些状态，不增加新的状态或写入状态。`on_done` 包含所有终态，
表示本次任务已结束，不等于成功；`on_error` 及回调队列满、排队超时也不改变机器人任务状态。
线程选择不增加任务状态；共享组被阻塞时仍按原有期限、停止确认和资源隔离规则处理。
尚未派发的技能在排队期间单步期限到期为 `FAILED / SKILL_TIMEOUT`，总期限到期为
`FAILED / TASK_TIMEOUT`，撤销后不会补执行或隔离该技能实例。已开始且无法确认停止的
调用仍进入待对账并隔离资源；软件状态不证明机器人已经停止。

CC API 的 `audio.speak` 在启动受理后保持 `RUNNING`，收到匹配 `workId` 的
`cc.api.work.finished` 才成功。首版按约定不解析 finished 的结束原因，外部停止/失败
如果也发出该事件，同样被视为成功。运行时主动取消仍等待停止确认后进入 `CANCELED`；
停止未确认进入 `RECONCILIATION_REQUIRED / EXECUTION_UNRESPONSIVE`。
不支持取消的真实调用不会被强制取消；支持取消的调用也须正常成功返回或显式抛
`TaskCancelled` 确认停止，仅 `CancelledError` 不足以证明物理动作已停止。只有执行已结束的
超时技能才报告 `FAILED / SKILL_TIMEOUT`。总期限到期且未派发或确认已停止时报告
`FAILED / TASK_TIMEOUT`；停止不明仍进入待对账。

无法确认停止时，该技能实例与它声明的资源在当前运行时内被隔离，等待者和新任务都不能
因为软件任务已结束而复用资源，报告 `RESOURCE_QUARANTINED`。迟到结果不会把任务改回
成功，也不会在终态后追加进度。隔离不会因线程后来返回而自动解除；重新创建运行时或
适配器前必须先确认旧动作已停止，重启不等于硬件停止确认。

## 重启恢复语义

运行时**不会**因为数据库里是 `RUNNING` 就重放任务。`start()` 时对每个非终态任务：

1. 若注入了 `Reconciler` 且它返回权威终态（`SUCCEEDED` / `FAILED` / `CANCELED` / `UNKNOWN`），采信该状态；
2. 否则（无 reconciler、reconciler 抛异常、返回不确定结果）任务落入
   `RECONCILIATION_REQUIRED`，等待人工或上层介入。

这条规则用于避免重复执行非幂等的物理动作。

## 模块结构

实现按职责归类；根目录只保留公共契约、配置与命令行入口。

```text
src/robot_agent_runtime/
├── models.py              任务、事件、技能数据模型
├── errors.py              结构化错误
├── ports.py               外部能力 Protocol
├── options.py             运行时容量、期限和快照配置
├── thread_binding.py      外部对象线程归属配置
├── __main__.py            命令行组装入口
├── kernel/                内核任务执行与状态
│   ├── core.py            生命周期与公开任务 API
│   ├── task_state.py      唯一状态写入点
│   ├── executor.py        步骤编排、超时与停止确认
│   ├── resources.py       资源互斥与隔离
│   ├── events.py          历史回放与实时事件
│   ├── policy.py          准入规则与本地 AdmissionValidator
│   ├── registry.py        技能查找
│   └── recovery.py        重启状态核对
├── hosting/               内核托管与外部调用边界
│   ├── kernel_host.py     内核装配、线程与分阶段关闭、请求和事件交接
│   ├── thread_runtime.py  固定循环线程与真实完成结果
│   ├── isolation.py       外部端口代理与线程分配
│   ├── boundary.py        有界快照与外部结果净化
│   └── execution_context.py  控制容量上下文
├── repositories/          仓储实现
│   ├── memory.py          InMemoryTaskRepository
│   └── sqlite.py          SQLiteTaskRepository
├── transport/             UDS 服务与协议
│   ├── server.py          RuntimeServer
│   └── wire.py            NDJSON 成帧与编解码
├── sdk/                   应用调用接口
│   ├── embedded.py        RobotRuntime
│   ├── remote.py          RemoteRuntime
│   ├── listeners.py       TaskListener
│   └── callback_executor.py  同步回调执行器
└── adapters/              机器人能力实现
    ├── cc_api.py          CC API 语音适配器
    └── simulator.py       模拟技能、状态源与对账器
```

| 原模块（已移除旧入口） | 当前实现路径 |
| --- | --- |
| `core`、`executor`、`task_state`、`resources`、`events`、`policy`、`registry`、`recovery` | `kernel.<同名模块>` |
| `kernel_host`、`thread_runtime`、`isolation`、`boundary`、`execution_context` | `hosting.<同名模块>` |
| `repository` / `sqlite_repository` | `repositories.memory` / `repositories.sqlite` |
| `server` / `wire` | `transport.server` / `transport.wire` |

表中的路径均以 `robot_agent_runtime.` 开头。旧根目录模块已删除，所有导入和补丁路径
均使用当前实现路径。`sdk` 的公开导入保持一致。
托管层负责代理与内核装配；内核只依赖普通端口及 `AdmissionValidator`，不依赖 `hosting`。

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
运行时只等待配置的停止确认期，可能先于适配器的 5 秒清理期限进入待对账状态；此时
speaker 资源保持隔离。应用需要先确认旧播报已经结束，再建立新连接和适配器。
首版不提供重启对账，也不自动重试播报。

CC API 适配器保留 `start()` 时所属的应用循环，跨线程执行会交回该循环；厂商请求与
回调不会在内核循环执行。应用循环阻塞也可能拖住适配器的停止确认，此时按上述规则隔离。

运行示例使用底层异步回调接口，将回调转回 asyncio；不调用 `cc_api/` 中包含阻塞等待的
`RobotClient`。SDK 的连接启动和停止放在事件循环之外。适配器独占该连接的 finished
和 disconnect 回调，不能和其他代码在同一 Api 实例上覆盖这些处理器。

完整入口为 `examples/cc_api_speech_demo.py`。在装好 `ubt_robot` 的 Linux ARM64
环境中执行（`CC_API_TOKEN` 通过部署环境传入）：

```bash
python3 examples/cc_api_speech_demo.py \
  --api-id YOUR_API_ID --address ROBOT_IP:48159 --message '你好，我是机器人'
```

在项目根目录的 Python 调用方中，可以直接取得最终 `Task` 快照（`api` 须已连接）：

```python
from examples.cc_api_speech_demo import speak
from robot_agent_runtime.models import TaskState

final = await speak(api, "你好，我是机器人")
print(final.id, final.state)
if final.state == TaskState.SUCCEEDED:
    output = final.result["steps"][0]["output"]
    print(output["message"], output["spoken"], output["work_id"])
elif final.error is not None:
    print(final.error.code, final.error.message, final.error.details)
```

`speak()` 等待任务终态并清理运行时和适配器后返回；任务执行或参数校验失败也会返回
`Task`，通过 `state` 和 `error` 判断原因，连接初始化与清理异常仍向调用方抛出。
SDK 连接继续由调用方管理。函数通过统一注册的 `on_event` 打印过程，`on_done` 将最终
快照交给等待中的 Future，`on_error` 将监听异常交给调用方；命令行入口保留最终结果输出，
任务失败时以异常退出。返回值为运行时整理后的结果，不包含 CC API 的完整原始响应。

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
| 执行中条件变化 | `RuntimeCore` 消费代理转交的 `provider.watch()` 推送 | 先置 `cancel_event`，再记原因；确认停止则 `CANCELED`，无法确认则待对账 |

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
- `KernelHost` 拥有内核线程，外部端口代理拥有自己的循环；真实调用结束与“取消请求已发送”
  分开记录。跨线程超时不会被当作物理动作已经停止的证据。

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
复用的契约测试、`test_cc_api` 语音事件匹配与取消/断连边界、`test_task_listeners`
统一回调注册、任务隔离与监听生命周期、`test_callback_executor` 守护线程退出、容量、
排队期限与取消竞态；`test_event_paging` 覆盖分页回放、实时去重和批次让出；`test_thread_runtime` / `test_isolation` / `test_kernel_host` /
`test_execution_boundaries` / `test_runtime_isolation` 覆盖线程归属、阻塞外部代码、有限取消、
仓储故障关闭、快照和资源隔离；`test_thread_bindings` 覆盖共享/分组/独占、配置冲突、
实际线程容量、SDK/Server 接入及组内阻塞和取消边界。
`test_thread_binding_boundaries` 覆盖状态源独占与组内阻塞的对照、空目标拒绝、排队调用
超时撤销后可复用，以及线程名的配置归属。
`test_package_layout` 覆盖新模块导入、旧入口移除、当前路径补丁及独立进程导入顺序。
`test_runtime_ownership` 覆盖内核依赖方向、构造线程、分阶段停止、异常释放与校验隔离；
`test_contracts` 包含本地和托管准入校验器的可复用契约。

## 当前范围

源码按 `kernel`、`hosting`、`repositories`、`transport`、`sdk`、`adapters` 职责归类，
根目录只保留公共契约和配置，旧模块入口已删除。
外部代理装配与整体关闭次序由托管层负责，内核支持保留仓储的停止阶段。

已实现：运行时内核、技能层框架、Python SDK（嵌入式 + 远程）、UDS 守护进程、SQLite
持久化、模拟适配器、CC API 语音播报适配器、SDK 统一注册式任务监听（仅本客户端后续提交）、
同步回调的有界守护线程隔离与排队期限、同进程内核专用线程和外部端口共享/分组/独占配置、有限停止
确认与失控资源隔离、任务总期限、控制容量预留、快照限额、事件分页及有界关闭缓冲。

线程边界保护外部 Python 阻塞或不配合取消时的内核响应，不提供硬实时保证。原生扩展
长期持有 GIL、进程退出或系统资源耗尽仍会影响整个进程；不能强制终止已经运行的线程。

尚未实现：Agent 集成层（计划生成与重规划）、调度器（`priority` 字段已持久化但不参与
排序，无并发上限与抢占）、有限重试、结构化日志、其他真实机器人能力适配（导航、
ROS 2 等）、CC API 结束原因解析与重启对账、回调进程隔离与强制终止、云端管理平面。
