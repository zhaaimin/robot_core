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
| `add_task_listener(on_event=..., on_done=..., on_error=..., execution="caller", group=None)` | 监听本 SDK 后续提交的任务，返回 `TaskListener`；可选择调用方、分组或独立执行回调 |
| `close()` | 嵌入式停止内核并限时清理监听；远程模式清理本客户端监听和连接 |

嵌入式另提供 `RobotRuntime.from_platform(repository, platform, *, allowed_permissions=None,
options=None)`，消费 `RobotPlatform` 并一次装配技能、状态源和对账器；显式线程绑定原样
交给宿主，权限与选项传给同一个运行时构造入口。平台启动和关闭继续由应用负责。

`RobotRuntime(..., options=RuntimeOptions(...))` 与
`RuntimeServer(repository, skills, socket_path, options=RuntimeOptions(...))` 可以配置隔离边界。
厂商接入包通过 `ports.RobotPlatform` 汇总既有端口，应用负责选择工厂和生命周期：

| 平台接口 | 行为 |
|---|---|
| `skills` | 稳定的只读 `Mapping[str, Skill \| ThreadBinding[Skill]]`，保留每个技能的线程选择 |
| `robot_state` / `reconciler` | 各自的原始端口或 `ThreadBinding[端口类型]`；没有对应能力时返回 `None` |
| `start()` / `close()` | 启动驱动和技能、确认停止并释放订阅；重复等待共享真实结果 |

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
    max_listener_threads=16,
    max_pending_listener_callbacks=64,
    listener_queue_timeout_s=5.0,
)
```

端口操作默认期限 5 秒。`TaskRequest.timeout_s` 从接受请求开始计算总期限，覆盖准入校验、
排队、资源等待与执行；每步受技能声明或单步覆盖和剩余总预算共同限制。
停止确认与持久化各自有界，不计入执行预算。技能收到取消信号后有 0.2 秒协作退出，
声明支持取消时才进一步请求协程取消，并再等待最多 0.2 秒。
需要更长硬件停止确认时间的部署须相应增加 `cancel_grace_s`。
普通调用、订阅与控制操作分别限额，查询、取消和关闭不被空闲订阅挤出；外部线程
数量（含声明校验线程）有上限，卡住的线程不会补建。容量耗尽报告
`EXTENSION_CAPACITY_EXCEEDED`。监听线程另有独立预算，超限报告
`LISTENER_CAPACITY_EXCEEDED`。`RemoteRuntime(socket_path, options=...)` 使用本地监听的
停止确认期限、监听线程与回调排队限额，不配置服务端限额。

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

绑定只控制运行时后续调用，`MySkill(...)` 构造仍在调用方线程；运行时不提供线程内工厂构造或自动迁移
已绑定的 asyncio 对象。CC API 仍会将厂商操作交回适配器 `start()` 所在循环。
低层 `RuntimeCore` 只接收普通端口对象并在当前循环执行；线程绑定交给 SDK/Server。

端口截止后按失败关闭：状态读取失败仍以 `INTERNAL_ERROR` 拒绝提交；对账超时不猜测
终态；仓储操作超时后报告 `EXTENSION_TIMEOUT`。若调用已开始，则禁止继续使用该仓储，
避免不确定提交之后继续写入；仅排队且已确认撤销、从未开始的调用不会禁用仓储，也不会
在队列恢复后补执行。仓储不可用时，任务最终状态可能无法落盘，应在恢复仓储后对账。

接入 CC API 时，应用先调用 `platform.start()` 启动驱动和技能，再启动运行时；
退出时先关闭运行时，再关闭平台，最后停止应用拥有的 SDK 连接。

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

回调支持普通函数和 `async def` 函数（包括 `async __call__`），至少提供一个。
每个监听器对同一任务按事件顺序调用，终态事件的 `on_event` 完成后，查询最终快照并
调用一次 `on_done`。不同任务各自监听；回调收到独立的深拷贝快照。

一次注册的 `on_event`、`on_done`、`on_error` 使用同一执行模式：

| `execution` | 回调执行位置 | 同步阻塞的影响 |
|---|---|---|
| `"caller"`（默认） | 异步回调在提交任务的业务循环，同步回调在 SDK 共享线程池 | 异步回调里的同步阻塞会卡住业务循环；同步回调占用共享线程槽位 |
| `"group"` | 同一 SDK 内，同名组共用一个专用线程和事件循环 | 同组监听相互影响，其他组和调用方循环可继续 |
| `"dedicated"` | 每次监听注册独占一个专用线程和事件循环 | 阻塞限制在本次注册，不直接卡住其他监听循环 |

```python
# 保持现有行为，适合要直接访问业务循环中异步对象的回调。
runtime.add_task_listener(on_event=update_ui, execution="caller")

# 同组共用一个循环，适合愿意共享阻塞影响的监听。
runtime.add_task_listener(on_event=record_event, execution="group", group="reporting")
runtime.add_task_listener(on_done=record_result, execution="group", group="reporting")

# 可能执行同步网络等待的回调独占一个循环。
runtime.add_task_listener(on_event=slow_callback, execution="dedicated")
```

只有 `group` 模式接受 `group` 参数，组名必须是非空白字符串；组名按原字符串匹配，
作用域仅限当前 SDK 实例。不同 SDK 的同名组不共享线程，监听线程也不与技能或仓储线程共用。
组内不同任务仍可协作并发，分组不保证跨任务串行。独立模式也以注册为单位，
同一注册的多个任务共享其循环。

`caller` 模式的普通函数返回 awaitable 时，回到提交方循环等待它。
`group` / `dedicated` 模式的普通函数、异步函数，以及普通函数返回的 awaitable，
都在所选循环中执行，不占用默认同步回调线程池。订阅和状态查询仍在提交方循环，
仅用户回调跨线程交接。

选择分组或独立模式后，异步客户端、连接及 asyncio Event/Queue/Future 必须在其所属循环
创建和使用，不能直接操作业务循环里的对象。通知业务循环用 `loop.call_soon_threadsafe()`；
调用依赖业务循环的异步接口用 `asyncio.run_coroutine_threadsafe()` 桥接，尤其是
`RemoteRuntime` 的请求接口。监听句柄的 `wait()` / `close()` 支持跨循环调用，
从隔离回调调用 SDK `close()` 时自动交接回提交方循环。

默认模式的异步回调即使阻塞调用方循环，也不在内核循环执行；该调用方自己的请求和 `close()` 仍可能
无法继续。可以从其他正常运行的调用方查询内核，不能把调用方循环停住等同于内核停住。

默认模式的同步回调不能直接操作 asyncio Future、Event 或 Queue，也不能直接调用 SDK 的异步方法；
需要这些操作时使用 `async def` 回调，或通过 `loop.call_soon_threadsafe()` 通知事件循环。
同一任务按事件顺序执行；不同任务或不同注册的同步回调可能并发，调用方共享的数据须自行同步。

默认模式下，每个 SDK 实例共享 4 个工作线程，最多排队 64 个同步回调，排队期限 5 秒。队列满时报告
`LISTENER_CALLBACK_QUEUE_FULL`，排队过期报告 `LISTENER_CALLBACK_TIMEOUT`，结束对应任务
的监听，不影响业务任务。同步 `on_error` 同样受容量与期限约束，无法执行时交给事件循环
异常处理器；异步 `on_error` 不占线程槽位。这些默认同步线程池限额不提供公开配置。

分组与独立模式默认最多保留 16 个监听线程位置，同名组计一次，每个独立注册计一次。
线程在首次回调时启动；注册时预留位置。关闭最后一个组成员或独立注册后释放线程，
但永久阻塞或抵抗取消的线程继续占用预算，直到真实退出，不补建替代线程。
每个循环默认最多接纳 64 个回调（在途与排队合计），排队期限 5 秒；满额和过期分别报告
`LISTENER_CALLBACK_QUEUE_FULL`、`LISTENER_CALLBACK_TIMEOUT`，结束对应任务的监听。
`on_error` 同样在所选循环执行并受限，无法执行时交给提交方循环异常处理器。
限额由 `RuntimeOptions.max_listener_threads`、`max_pending_listener_callbacks`、
`listener_queue_timeout_s` 配置，与默认同步线程池及外部端口线程预算分开。

嵌入式 `close()` 先启动内核关闭；两种 SDK 均限时注销监听、取消尚未被线程领取的工作，
不等待已经领取的同步回调。分组和独立模式也撤销未开始回调、请求取消在途回调，
限时等待线程退出；无法停止的回调保留线程，丢弃迟到结果，不再交付后续事件。
回调内可以关闭自身监听或 SDK，当前回调返回后自行退出其已关闭的专用循环。
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
监听的调用方/分组/独立执行方式只决定回调归属，不增加状态或改变业务任务的执行位置。
`from_platform` 从厂商平台装配原有端口并保留线程分组，不增加任务状态；驱动断连、响应不可信或停止未确认，仍通过
技能错误进入现有失败、停止确认和待对账流程。
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
├── ports.py               外部能力与 RobotPlatform 装配契约
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
│   ├── embedded.py        RobotRuntime 与保留线程绑定的 from_platform 装配
│   ├── remote.py          RemoteRuntime
│   ├── listeners.py       TaskListener
│   ├── callback_executor.py  默认同步回调执行器
│   └── listener_execution.py  监听分组/独立循环、容量与回调桥接
└── adapters/              机器人能力实现
    ├── cc_api/            CC 厂商接入包
    │   ├── __init__.py    导出类和工厂，不创建连接
    │   ├── contracts.py   CcApiClient / CcApiResponse 厂商 SDK 协议
    │   ├── platform.py    CcApiPlatform 与 create_cc_api_platform
    │   ├── driver.py      请求响应、订阅分发和 SDK 回调桥接
    │   └── speech.py      语音参数、完成与停止确认
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

## 厂商接入包

任务内核通过 `Skill`、`RobotStateProvider`、`Reconciler` 执行机器人操作。SDK 的
`RobotRuntime.from_platform(...)` 消费 `RobotPlatform`，从中取得这三个端口并构造运行时。
厂商包提供实现该契约的平台、工厂、驱动和技能。新增厂商可放在
`adapters/vendor_x/` 或独立 Python 包中；应用选择相应工厂并传入配置，运行时和其他
厂商模块不需要添加分支。原生 SDK 依赖留在对应部署环境，普通导入不加载原生库。

```python
from robot_agent_runtime.adapters.cc_api import create_cc_api_platform
from robot_agent_runtime.ports import RobotPlatform

# api 由应用创建并连接；不同厂商的工厂可以接受自己的 SDK 或配置。
platform: RobotPlatform = create_cc_api_platform(api, skill_thread_group="audio")
try:
    await platform.start()
    runtime = RobotRuntime.from_platform(repository, platform, allowed_permissions={"audio"})
    try:
        await runtime.start()
        task = await runtime.submit_task(request)
        # 业务代码查询状态或监听任务事件。
    finally:
        await runtime.close()
finally:
    await platform.close()
```

工厂在调用方装配对象，不改变运行时的线程绑定机制。平台启动、关闭与驱动操作应在
同一应用循环进行；厂商 SDK 的回调通过驱动桥接。平台生命周期由应用负责，
`RobotRuntime` / `RuntimeServer` 不自动启动或关闭它。资源停止无法确认时，先保留
运行时的待对账与隔离结果，再释放通信资源，不能用释放连接表示物理动作已经停止。

平台可在 `skills` 中混合提供裸技能与 `ThreadBinding.shared/group/dedicated`，状态源和
对账器也可声明绑定。`from_platform` 将这些值原样传给宿主，不解包或重新选择线程。
裸对象保持独占，同组对象共用一个线程且共享同步阻塞的影响；不同组和独占线程各自运行。
容量按实际线程加声明校验线程计算。绑定只决定运行时的调用位置，平台启停仍操作原始对象。
需及时交付前置条件的状态源应保持独占，避免与可能阻塞的技能共用循环。

`RobotPlatform` 不规定厂商必须有 request、callback 或 workId；HTTP、原生同步 SDK、
ROS 2 都可在自己的驱动和技能里转换。`adapters.cc_api.contracts` 中的 `CcApiClient` /
`CcApiResponse` 描述 CC SDK 边界，通用 `ports.py` 只保留厂商无关的运行时契约。
未实现状态源或对账器时明确返回 `None`，不推测设备可用或动作已经完成。

## CC API 语音播报

`create_cc_api_platform` 接受已连接、专用的 `ubt_robot.Api` 实例，装配驱动和
`audio.speak` 技能。首版无状态源和对账器。平台管理请求、订阅和技能，应用继续管理
SDK 的 initialize/start/stop，不自动重连。

工厂参数 `skill_thread_group` 默认为 `None`，保持技能独占；显式传入非空组名时，
平台返回 `ThreadBinding.group(skill, name)`。语音 demo 显式使用 `"audio"`，保留示例原有
分组。技能本身仍把厂商通信交回驱动所属的应用循环，此分组配置不迁移 SDK 连接。

直接构造 `CcApiSpeechSkill(api)` 时会创建独占驱动：

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
`RobotClient`。SDK 的连接启动和停止放在事件循环之外。
`CcApiDriver` 独占该连接的 finished 和 disconnect 回调。多个技能共用同一个驱动时，
通过 `subscribe_finished` / `subscribe_failure` 订阅；返回的函数仅注销本次订阅。
`CcApiSpeechSkill(driver)` 不拥有共享驱动，关闭技能只删除自己的订阅，驱动由平台或
应用最后关闭。直接操作同一 Api 对象覆盖这些回调仍不受支持。

驱动把回调响应净化为快照后交接，不把厂商响应对象带到运行时。普通请求与控制请求
分别受 `RuntimeOptions.max_pending_calls` / `max_control_calls` 限制，停止请求使用
保留的控制容量；响应等待受 `port_timeout_s` 限制。SDK 回调缓冲受 `max_event_buffer`
限制，满额报告 `EVENT_STREAM_OVERFLOW` 并关闭该驱动的后续准入。响应超时、断连和
无效响应也会失败关闭，已发送动作可能仍在执行，需要停止确认或对账。
已下发请求的等待被取消时同样拒绝后续准入，防止丢弃启动响应后重复发送物理动作。
请求被厂商明确拒绝时保留 `vendor_code`，允许应用之后另行提交，不自动重试。

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
排队期限与取消竞态；`test_listener_execution` 覆盖监听执行位置、组内阻塞、独立隔离、
线程预算、排队撤销、远程接入与自关闭；`test_event_paging` 覆盖分页回放、实时去重和批次让出；`test_thread_runtime` / `test_isolation` / `test_kernel_host` /
`test_execution_boundaries` / `test_runtime_isolation` 覆盖线程归属、阻塞外部代码、有限取消、
仓储故障关闭、快照和资源隔离；`test_thread_bindings` 覆盖共享/分组/独占、配置冲突、
实际线程容量、SDK/Server 接入及组内阻塞和取消边界。
`test_thread_binding_boundaries` 覆盖状态源独占与组内阻塞的对照、空目标拒绝、排队调用
超时撤销后可复用，以及线程名的配置归属。
`test_package_layout` 覆盖新模块导入、旧入口移除、当前路径补丁及独立进程导入顺序。
`test_runtime_ownership` 覆盖内核依赖方向、构造线程、分阶段停止、异常释放与校验隔离；
`test_contracts` 包含本地和托管准入校验器的可复用契约，也用 CC 和非回调假厂商验证
`RobotPlatform` 的共同装配契约。`test_cc_platform` 覆盖共享驱动、响应关联和快照、
断连和过载、控制容量、平台启停及取消等待者后的真实清理，以及 CC 分组和 demo 策略。
平台契约和嵌入式用例还验证混合绑定、实际线程容量、组内阻塞范围、状态源和对账器绑定。

## 当前范围

源码按 `kernel`、`hosting`、`repositories`、`transport`、`sdk`、`adapters` 职责归类，
根目录只保留公共契约和配置，旧模块入口已删除。
外部代理装配与整体关闭次序由托管层负责，内核支持保留仓储的停止阶段。

已实现：运行时内核、技能层框架、Python SDK（嵌入式 + 远程）、UDS 守护进程、SQLite
持久化、模拟适配器、厂商接入包契约、SDK 通用平台装配入口与 CC 平台工厂、共享 CC 驱动和语音技能、
SDK 统一注册式任务监听（仅本客户端后续提交）、
同步回调的有界守护线程隔离与排队期限、监听回调调用方/分组/独立执行配置、
同进程内核专用线程和外部端口共享/分组/独占配置、平台端口线程绑定透传、有限停止确认与失控资源隔离、
任务总期限、控制容量预留、快照限额、事件分页及有界关闭缓冲。

线程边界保护外部 Python 阻塞或不配合取消时的内核响应，不提供硬实时保证。原生扩展
长期持有 GIL、进程退出或系统资源耗尽仍会影响整个进程；不能强制终止已经运行的线程。

尚未实现：Agent 集成层（计划生成与重规划）、调度器（`priority` 字段已持久化但不参与
排序，无并发上限与抢占）、有限重试、结构化日志、其他真实机器人能力适配（导航、
ROS 2 等）、CC API 结束原因解析与重启对账、回调进程隔离与强制终止、云端管理平面。
