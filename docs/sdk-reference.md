# SDK 与公开 API 参考

> 返回 [README](../README.md) · 相关：[架构](architecture.md) · [生命周期与安全](lifecycle.md)

## 公开 API

统一能力 API 为 `robot_agent_capabilities.RobotCapabilities.create(runtime, ...)`、`tools` 和
`invoke(name, params, *, operation_id=None, wait=False)`。动作必须提供非空稳定 operation_id；
默认后台提交，显式 wait=True 才有界等待。Observation 的 task_id/state/result 依据 runtime，
error 同时区分运行时结果与本地等待、操作身份冲突；无法确认最新状态时 state 为 null。
MCP 原五项任务工具保留，并按同一白名单提供 robot_speak、robot_navigate_to 等实际能力及
robot_task_status/robot_cancel_task；技能调用信封为 `{operation_id, params, wait=false}`。
默认键为 `robot:{operation_id}`，旧 RobotTools 的 `agent:` 键保持不变；不要跨命名空间重发旧动作。
完整 Python 示例和 Codex/Claude Code stdio 配置见 [统一接入说明](agent-integration.md)。

能力客户端还提供两个分开的回调入口，均返回 `robot_agent_capabilities.ports.ListenerHandle`：

| 方法 | 说明 |
| --- | --- |
| `robot.add_task_listener(*, on_event=None, on_done=None, on_error=None, task_ids=None, all_tasks=False, event_types=None, execution="caller", group=None)` | 默认跟踪注册后本能力客户端 invoke 返回的任务；指定 task_ids 则只回放 / 观察这些任务 |
| `robot.add_platform_listener(*, on_event=None, on_error=None, after_cursor=0, event_types=None, execution="caller", group=None)` | 从平台游标回放后接实时；设备故障交付 on_event |
| `await robot.close_listeners()` | 关闭本客户端监听及共享回调池；之后拒绝新增监听，invoke / subscribe_events 仍可使用 |

任务回调分别是 `on_event(Event)`、`on_done(Task)`、`on_error(task_id | None, exception)`；
平台回调是 `on_event(PlatformEvent)`、`on_error(exception)`。任务业务失败仍走 on_done，
设备故障仍走平台 on_event；只有观察 / 查询 / 回调失败才走 on_error。on_error 可省略，缺省由
事件循环异常处理器报告；错误处理器失败不递归，正常注销不报监听错误。
event_types 只筛选任务 on_event，不隐藏 on_done 的权威终态快照。幂等任务每注册只通知一次。
等待超时取得任务 ID 后先在后台核对不可变步骤快照，再观察；冲突不会误推送旧任务或阻断
后续正确调用的登记，查询 / 取消控制工具不自动登记。
预期步骤在等待提交之前保存不可变 JSON 快照，正常返回、超时后台观察和超时后的补查均
复用它，不会因调用方随后修改嵌套参数而改变身份判断。捕获仅遍历基础类型和容器，不调用
外来复制 / 序列化钩子或消费生成器；不可表示输入仍由运行时按原有边界拒绝。

`all_tasks=True` 与 task_ids 互斥，是显式全任务实时模式，无就绪握手 / 历史补收保证，
终态去重缓存为最近 1000 个任务 ID。普通任务 wait 等待调用时已登记的任务观察，平台 / 全局
wait 等待流结束；两者均不代表业务成功。句柄支持 wait / close / 只读 closed，注销不取消
物理任务。取消 wait 不取消后台观察，异步回调可以关闭自身 / 全部监听，但不能 wait 自身。

execution 支持 caller / group / dedicated。caller 默认保持异步回调在观察方循环、同步回调
共用能力客户端有界池（4 线程、64 排队、5 秒排队期限）。group 的同名组在当前能力客户端
共享一个专用线程 / loop，任务与平台可以同组；dedicated 按注册独占。两种隔离模式的同步 /
异步及返回的 awaitable 均在所选 loop 执行，订阅 / 状态查询仍在观察方循环。只有 group
模式接受非空白 group。组与线程预算不和底层 SDK 或其他能力客户端共享。
实现复用 SDK ListenerLoops / CallbackLoop，不另实现线程、排队与期限机制。

`RobotCapabilities.create(..., listener_options=RuntimeOptions(...))` 或直接构造时，可为本
客户端配置 max_listener_threads（默认 16）、max_pending_listener_callbacks（每 loop 默认
64，含在途）、listener_queue_timeout_s（默认 5 秒）和 cancel_grace_s（默认 0.2 秒）；
跨 loop 异常净化使用同一 options 的快照预算。不修改后端配置或 caller 同步池限额。
回调队列满 / 排队过期报告 LISTENER_CALLBACK_QUEUE_FULL / LISTENER_CALLBACK_TIMEOUT；
错误回调受同一配置约束，无法执行时由观察方循环报告。关闭最后成员 / 连续流自然结束后
释放线程；未真实结束的阻塞线程仍占预算。回调内自关闭经 SDK 桥接，返回后自行退出线程。
同组允许异步交错，不保证跨任务 / 来源串行；已有连接和 asyncio 对象须在所属 loop 使用。
每客户端最多 128 个注册，超限报告 LISTENER_CAPACITY_EXCEEDED。
平台 / 全局 / 明确 ID 注册需要运行中的循环。用完监听必须
close_listeners 释放共享池；它不关闭 runtime，已领取的阻塞回调不保证停止。平台处理位点由
应用在业务处理成功后保存。详细使用示例见
[能力监听说明](../capabilities/README.md#注册任务和平台监听)。

能力客户端提供 `robot.subscribe_events(*, sources=None, after_cursor=0, event_types=None,
task_ids=None, after_sequences=None, task_terminal_only=False)`，交付原始 `Event | PlatformEvent`。
默认仅平台；`sources={'task'}` 只观察任务，两者一起则汇聚。指定 task_ids 时逐任务回放后接
实时，after_sequences 只用于所选任务；未指定 task_ids 时使用可选全任务实时流，无历史或就绪
握手保证。task_terminal_only 覆盖全部任务终态，不影响平台事件，也不表示物理停止或成功。
独立可选 `TaskEventRuntime` / `PlatformEventRuntime` 不扩充原 `TaskRuntime`；新
`capabilities.events` 只负责来源选择、每流一个在途读取与有界清理，不增加日志或全局序号。
任务恢复位置是 task_id/sequence，after_cursor 仅是平台位置；内部回看上一条终态后滤掉已处理
记录，避免恢复到已处理终态时继续等待。不同来源之间不保证全局顺序。
关闭订阅只清理自持迭代器，不启停 runtime 或取消动作；工具白名单不充当事件授权。
无事件接口 / 明确未实现时抛 `CapabilityEventsUnsupported`（`CAPABILITY_EVENTS_UNSUPPORTED`）；
runtime 结构化错误原样传播。原始远程传输中断包装为 `CapabilityEventTransportError`
（`CAPABILITY_EVENT_TRANSPORT_FAILED`），详情分开保留平台与任务的已交付位置，均不代表业务 ACK。
清理共享 0.2 秒预算，超限报告 `CAPABILITY_EVENT_CLEANUP_TIMEOUT`；迟到读取会在实际结束后
清理，迟到错误交给事件循环。主错误 / 取消优先，其余清理失败和超限来源附加在异常 notes 中。
已知任务流完成后平台流仍可继续；任一来源失败则结束本次聚合。MCP 工具与通知协议不变，
McpRuntime 仅可用既有单任务接口。用法见 [能力层说明](../capabilities/README.md#任务与平台事件)。

Agent 应用入口为各自独立包提供的 `robot-agent` / `hotel-agent` / `guide-agent`（也可 `python -m robot_agent`、
`python -m hotel_agent`、`python -m guide_agent`），通过下方公开 SDK 调用 runtime，不增加内核 API。
导览支持 `--input text|events`、`--skills-dir PATH` 和 `--backend mcp --runtime-socket PATH`；
唤醒/ASR 适配器通过 `guide_agent.ports.InteractionSource` 输入稳定 ID 的事件，只有最终话语进入模型。
地点资料来自 `venue-guide/references/destinations.yaml` 与 `introductions.md`；地图目标为空可问答及模拟导航，MCP 不提交移动。
`--scenario` 参数已删除：通用验证用 `robot-agent`，酒店接待/带房模拟验证用 `hotel-agent`。
`hotel-agent` 可指定 `--backend mcp --runtime-socket PATH`，由应用启动 stdio 桥接进程；
`robot-agent` 只有 `simulator` 与 `cc` 两个后端。`--skills-dir` 可指定 Agent Skill 目录。
`robot-runtime-mcp --socket PATH` 供本机 MCP 客户端连接；除上述具体能力外，保留 list_skills、submit_task、get_task_status、cancel_task 和有界 read_task_events。
MCP 桥接及客户端位于独立包 `robot_runtime_mcp`；酒店通过应用层 `TaskRuntime` 端口消费它，内核无 MCP 依赖。
酒店装配入口是 `hotel_agent.application`，业务模块使用 `hotel_agent.*` 路径：
`HotelWorkflow`、`ServicePhase`、`SimulatedHotel` 分别从 `hotel_agent.service`、`hotel_agent.models`、
`hotel_agent.backend` 导入；机器人装配入口是 `robot_agent.application`。
公共类型、单轮图调用与 CLI 骨架分别使用 `robot_agent_core.config`、`robot_agent_core.shell`、
`robot_agent_core.cli`；三个应用都不被内核反向依赖。
影响存储/身份的配置变化通过 `robot_agent_core.configuration.rebuild_config` 重建；
启动一致性校验会拒绝旧解析结果，也会拒绝携带另一个 agent profile 的快照，
数据库文件名和既有 runtime_id 保持不变。
通用宿主提供 load_skill / read_skill_resource / run_skill_script，脚本需应用注册；机器人自定义目录不自动批准脚本。
共享 `/skills`、`/skill NAME` 命令不调用模型或业务，显式选择不会代替客人确认或启动机器人。
酒店自定义目录中的脚本是可选能力：只有可见且与发布指纹一致的入口才绑定，缺失或变化不阻断文档加载与查询/取消；调用未批准脚本仍被拒绝。

`RobotRuntime` 与 `RemoteRuntime` 均提供：

服务端从 `robot_agent_runtime.transport.server` 导入 `RuntimeServer`；低层内核从
`robot_agent_runtime.kernel.core` 导入 `RuntimeCore`。使用下方模块结构中的新路径导入。

| 方法 | 说明 |
| --- | --- |
| `start()` | 嵌入式按对象的线程绑定启动并完成重启恢复；远程模式确认服务可用 |
| `submit_task(request)` | 提交任务，返回快照；校验失败直接返回 `FAILED` 任务 |
| `submit_and_wait(request, *, timeout_s=None)` | 提交并等待终态，返回最终快照；等待超时或调用方取消不取消业务任务 |
| `get_task_status(task_id)` | 读取持久化的任务状态 |
| `cancel_task(task_id)` | 请求取消并在有限停止确认期后返回终态；无法确认时进入待对账状态 |
| `list_skills()` | 列出已注册技能定义 |
| `subscribe_task_events(task_id, after_sequence=0)` | 分页回放历史后接续实时事件；订阅容量独立于查询和取消 |
| `subscribe_all_task_events(event_types=None)` | 首次拉取登记后的全任务实时流；无全局游标，历史仍按单个任务恢复 |
| `subscribe_platform_events(*, after_cursor=0, event_types=None)` | 从平台日志分页回放后接续实时事件，按独立 `cursor` 恢复 |
| `add_task_listener(on_event=..., on_done=..., on_error=..., execution="caller", group=None)` | 监听本 SDK 后续提交的任务，返回 `TaskListener`；可选择调用方、分组或独立执行回调 |
| `close()` | 嵌入式停止内核并限时清理监听；远程模式清理本客户端监听和连接 |

只需要最终结果时，两种 SDK 都可以直接调用：

```python
final = await runtime.submit_and_wait(request, timeout_s=None)
print(final.state, final.result, final.error)
```

无需注册监听或创建 Future。方法内部复用提交、历史回放订阅和最终查询，任务即使在
订阅前完成也能取得结果；准入失败或幂等重提已返回终态时直接返回。所有终态都返回
`Task`，包括 `FAILED`、`CANCELED`、`UNKNOWN`、`RECONCILIATION_REQUIRED`，不代表成功。
提交、订阅、查询失败仍抛异常，事件流提前结束不冒充完成。

`timeout_s` 必须为有限正数或 `None`，从提交返回后开始计算，覆盖订阅与最终查询；
`None` 表示无限等待。到期抛出 `errors.TaskWaitTimeout`，错误码 `TASK_WAIT_TIMEOUT`，
`error.info.details["task_id"]` 可用于后续查询或显式取消。等待超时、调用方取消只结束
本次等待并关闭订阅，不自动取消机器人动作。业务任务总期限仍由 `TaskRequest.timeout_s`
控制，主动取消仍使用 `cancel_task(task_id)`。已有注册监听独立交付；需要收齐回调时，
在关闭 SDK 前继续 `await listener.wait()`。

嵌入式另提供 `RobotRuntime.from_platform(repository, platform, *, allowed_permissions=None,
options=None, event_repository=None)`，消费 `RobotPlatform` 并一次装配技能、状态源、对账器和
可选 `event_source`；显式线程绑定原样交给宿主。旧平台没有 `event_source` 属性时按 `None`
处理，无需修改。平台启动和关闭继续由应用负责。

`RobotRuntime(..., options=RuntimeOptions(...))` 与
`RuntimeServer(repository, skills, socket_path, options=RuntimeOptions(...))` 可以配置隔离边界。
厂商接入包通过 `ports.RobotPlatform` 汇总既有端口，应用负责选择工厂和生命周期：

| 平台接口 | 行为 |
|---|---|
| `skills` | 稳定的只读 `Mapping[str, Skill \| ThreadBinding[Skill]]`，保留每个技能的线程选择 |
| `robot_state` / `reconciler` | 各自的原始端口或 `ThreadBinding[端口类型]`；没有对应能力时返回 `None` |
| 可选 `event_source` | `PlatformEventSource` 或其 `ThreadBinding`；不要求旧 `RobotPlatform` 新增属性 |
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

### 平台事件与通知

`RobotRuntime(..., event_source=None, event_repository=None)` 与 `RuntimeServer` 接受相同的
可选关键字，端口均可使用 `ThreadBinding`。配置源而未配置日志时使用
`repositories.platform_events.InMemoryPlatformEventRepository`；需要重启恢复时显式传入
`SQLitePlatformEventRepository(database, max_events=10000)`。仅配置日志可读取历史；两者均
省略则不创建平台事件子系统，调用订阅报告 `INVALID_REQUEST`。

`models.PlatformEvent` 包含 `event_id`、`source`、`event_type`、UTC `timestamp`、`data`、
可选 `task_id` 与非负整数 `cursor`。来源提交 `cursor=0`，日志写入后分配正游标；`source`
及载荷由来源定义，不带运行时的厂商或安全含义。`ports.PlatformEventSource.watch()` 返回
异步事件流；应用负责其原始连接与生命周期。`PlatformEventRepository` 的公开契约为
`start()` / `close()`、`append_platform_event(event)`、
`list_platform_events(after_cursor=0, limit=128)`、`platform_event_bounds()`，边界为
`(首个保留游标, 最后已分配游标)`。

```python
from contextlib import aclosing

from robot_agent_runtime.repositories.platform_events import SQLitePlatformEventRepository
from robot_agent_runtime.sdk import RobotRuntime

runtime = RobotRuntime(
    repository, skills,
    event_source=event_source,
    event_repository=SQLitePlatformEventRepository("runtime.db"),
)
await runtime.start()
try:
    async with aclosing(runtime.subscribe_platform_events(after_cursor=saved_cursor)) as events:
        async for event in events:
            await process_idempotently(event.event_id, event.data)
            await save_cursor(event.cursor)  # 业务处理完成后才保存。
finally:
    await runtime.close()
```

两种日志默认只保留最新 10000 条，可用 `max_events` 调整。去重保证限于保留窗口；窗口内
同一 `event_id` 重提同一事实返回原游标，同 ID 的不同载荷被 `InvalidRequest` 拒绝。
`after_cursor` 表示最后已处理位置，必须非负且不能大于日志最后游标。
`after_cursor < earliest_cursor - 1` 时抛
`PlatformEventCursorExpired`（`PLATFORM_EVENT_CURSOR_EXPIRED`），details 包含
`after_cursor`、`earliest_cursor`、`latest_cursor`；消费方应重新同步设备当前状态，不能把
过期当作没有事件。未来游标报告 `INVALID_REQUEST`。内存日志不保证进程崩溃后的恢复。

订阅先登记实时队列再分页回放，按 `cursor` 消除交接重复；`event_types` 为非空字符串集合，
`None` 或空集合表示全部。过滤不重编号。默认每页 128 条、实时及关闭缓冲各 1000 条，沿用
`event_page_size` / `max_event_buffer`。慢订阅者溢出报告 `EVENT_STREAM_OVERFLOW` 并携带
最后实际交付的 `after_cursor`，不阻塞其他订阅者。来源失败或提前结束会先保留已收到的事实，
再向订阅者报告异常；普通来源异常包装为 `PLATFORM_EVENT_SOURCE_UNAVAILABLE`。
来源自行抛出 CancelledError 也按来源不可用报告，既有与新订阅均先读历史后报错；运行时
正常关闭发起的采集取消仍是正常结束，不伪造来源故障。
日志失败不会修改任务状态。保证从运行时成功保存事件开始，不保证厂商断线期间未观测事实的完整性。

平台采集在 `NotificationHost` 的独立循环运行；源与日志调用使用既有端口隔离，计入外部线程
总预算。关闭停止采集、有界排空已打开订阅并关闭日志，再释放外部线程；异常不跳过后续释放。
取消只请求协作退出，不能证明厂商动作或通知的外部副作用已经停止。

`subscribe_all_task_events(event_types=None)` 只观察第一次拉取完成登记之后的任务事件。
仅创建异步生成器不算登记，也不回放历史；没有跨任务的全局 `sequence`。断线或溢出后，
按每个任务保存的 `task_id/sequence` 调用 `subscribe_task_events` 恢复。已有注册式任务监听
仍只观察本 SDK 后续提交的任务，行为不变。

可选 `notifications.notifier.EventNotifier` 通过 `await notifier.consume(stream)` 消费公开流，
不再接入 Core / EventBus 私有字段。先注册 `add_subscriber(id, channel, event_filter=None)`，
再 `await notifier.start()`；退出时由应用结束消费并 `await notifier.stop()`。
`DeliveryChannel` 仅要求异步 `deliver(event) -> bool` 与 `close()`；Webhook 使用
`add_webhook(id, WebhookEndpoint(...))` 或显式 `WebhookChannel(endpoint, ...)`。
各订阅者顺序交付并独立失败，默认输入队列和各出口队列均为 1000、最多 128 个订阅者，
每出口保留最近 1000 个已交付事件 ID 去重。去重缓存有界且不持久化，任务序号不能跨任务
去重；重复注册替换并关闭旧出口。输入溢出直接抛错；出口溢出或投递失败通过
`subscriber_error(id)` 查询并停止该出口，不静默丢弃。消费方负责检查状态、持久化处理水位和补收。
`deliver()` 返回 False 时默认按 1 / 3 / 10 秒重试；默认关闭等待为 5 秒，网络发送或
`deliver=True` 均不代表业务 ACK。

#### 已连接 WebSocket 的转发

`transport.websocket` 提供两个异步函数，不自带 WebSocket 库或服务：

| 函数 | 消息信封 |
| --- | --- |
| `forward_platform_events(runtime, websocket, *, after_cursor=0, event_types=None, send_timeout_s=5.0)` | `{"type":"platform_event","event":{...}}` |
| `forward_task_events(runtime, websocket, *, event_types=None, send_timeout_s=5.0)` | `{"type":"task_event","event":{...}}`，全任务实时流 |

`websocket` 结构匹配异步 `send(str)` 和 `wait_closed()`；空闲断连也必须唤醒后者。同进程传
`RobotRuntime`，独立网关传 `RemoteRuntime`。调用方完成握手、认证、机器人可见范围校验和连接
关闭，一个 socket 只运行一个转发器 / 发送者，顺序发送：

```python
from robot_agent_runtime.transport.websocket import forward_platform_events

# websocket 已由应用建立并认证，saved_cursor 由客户端在业务处理后保存。
await forward_platform_events(runtime, websocket, after_cursor=saved_cursor)
```

断连正常返回；来源 / 发送失败及发送超时向调用方抛出，并清理订阅。发送完成不推进业务确认
水位；不配合取消的底层发送可能在转发器返回后继续，不能立即在同一连接上启动替代发送者。
订阅清理的前台等待同样受 send_timeout_s 限制。如果读取抵抗取消，返回后保留一次延迟清理，
待读取实际结束再调用 aclose，不并发读取和关闭，不发送迟到结果；迟到清理异常交给事件循环
异常处理器。正在进行的外部操作仍可能继续，不能把有界返回当作副作用已停止。
这些入口不自动激活 Agent；全任务信封仍保留各任务自身的 `sequence`。

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

技能、仓储、状态源、对账器及平台事件源 / 日志都支持绑定。`RuntimeServer` 直接在端口构造参数中
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
| `listener.closed` | 线程安全的只读注销标志，不表示机器人已停止 |
| `listener.track(task_id, *, validate=None)` | 显式登记已知任务，回放后接实时；重复 ID 不重复交付 |

取消 `wait()` 的调用者不会取消后台监听。同一个注册对同一任务 ID 只监听一次，
所以重复提交幂等请求不会重复通知；去重 ID 保留到注销。多次注册得到独立的监听句柄。
SDK `close()` 会停止监听，不保证交付尚未完成的回调，要收齐结果应先 `wait()`。
监听流关闭或失败后 `wait()` 也会返回，因此业务成功要以 `on_done` 的任务状态为准。
回调内部不能 `wait()` 自己的监听句柄，也不要吞掉取消信号。

`track()` 在运行中的观察方循环调用，不负责提交任务。可选 validate 是短小的同步校验函数，
接收独立 Task 快照并返回 None；失败只通过 on_error 报告，发生在事件订阅之前。它用于
观察资格核对，不是任务准入或安全校验，不得在其中操作机器人。校验失败的 ID 可以再次登记；
同一 ID 正在校验时，候选串行排队（最多 64 个），确认的无校验 track 优先。队列溢出且没有
保留候选匹配时报告 LISTENER_CAPACITY_EXCEEDED；wait 包含已排队的替代校验，close 清除
待校验项。已经开始正常观察后的失败仍保留原有去重行为。

需要复用相同回调边界的上层观察者可从 `sdk.callback_executor` 使用
`CallbackExecutor.invoke(callback, *args, can_start=None)`：异步回调 / 同步返回的 awaitable
在调用循环等待，同步函数经既有有界池派发。可选 can_start 是短小同步检查，派发前、工作线程
领取时及等待返回的 awaitable 前再次核对；返回 False 时遗弃结果。调用者取消会撤销未领取
工作并遗弃在途结果，不能强制停止外部副作用。执行器由创建者显式 close。
