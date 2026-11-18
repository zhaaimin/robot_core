# Robot Capabilities

供 MCP 客户端和自研 Agent 共用的机器人能力层，Python 3.11+。发布包为
`robot-agent-capabilities`，导入名为 `robot_agent_capabilities`，只依赖本仓库的 runtime。
不加载模型、LangGraph、MCP 或业务应用；RobotCapabilities 不拥有 runtime 生命周期。

## 安装与使用

在已安装项目依赖的容器中，从仓库根目录安装本地包：

```bash
/opt/robot_agent_venv/bin/python -m pip install -e . -e ./capabilities
```

```python
from robot_agent_capabilities import RobotCapabilities
from robot_agent_runtime.sdk import RemoteRuntime

runtime = RemoteRuntime('/workspace/robot_core/.data/robot.sock')
await runtime.start()
try:
    robot = await RobotCapabilities.create(runtime, allowed_skills={'audio.speak'})
    for tool in robot.tools:
        print(tool.name, tool.description, tool.input_schema)
    observed = await robot.invoke(
        'robot_speak', {'message': '欢迎光临'}, operation_id='visit-42-greeting-v1',
    )
    print(observed['task_id'], observed['state'])
    # 后续根据真实状态查询；需要物理停止时显式调用 robot_cancel_task。
    status = await robot.invoke('robot_task_status', {'task_id': observed['task_id']})
finally:
    await runtime.close()
```

完整的无模型模拟示例：`/opt/robot_agent_venv/bin/python examples/capabilities_demo.py`。
外部 Agent 的 MCP 配置见 [统一接入说明](../docs/agent-integration.md)。

## 契约

| API | 语义 |
| --- | --- |
| `RobotCapabilities.create(runtime, ..., listener_options=None)` | 从公开 TaskRuntime 发现技能；可独立配置本客户端的监听预算，不启动/关闭 runtime |
| `tools` | 返回独立的 ToolDefinition 快照：name、description、input_schema、skill、read_only |
| `invoke(name, params, *, operation_id=None, wait=False)` | 调用能力或任务控制，返回真实 Observation；动作必须提供稳定操作 ID |
| `add_task_listener(*, on_event=None, on_done=None, on_error=None, task_ids=None, all_tasks=False, event_types=None, execution="caller", group=None)` | 注册任务回调；默认跟踪本能力客户端之后提交的任务 |
| `add_platform_listener(*, on_event=None, on_error=None, after_cursor=0, event_types=None, execution="caller", group=None)` | 注册平台事实回调；设备故障也交付 on_event |
| `close_listeners()` | 关闭本能力客户端的全部监听和共享回调资源，不关闭 runtime |
| `subscribe_events(*, sources=None, after_cursor=0, event_types=None, task_ids=None, after_sequences=None, task_terminal_only=False)` | 汇聚原始任务 Event 与平台 PlatformEvent，恢复位置分别保存 |

`TaskRuntime` 在 `robot_agent_capabilities.ports` 中声明，嵌入式 RobotRuntime、UDS
RemoteRuntime 和独立 MCP 包的 McpRuntime 按结构实现。它继续提供任务查询、显式取消和真实事件订阅。
统一层不访问 KernelHost、RuntimeCore 或仓储私有字段，不替代 runtime 的 schema、权限、前置条件及资源校验。

### 注册任务和平台监听

普通应用可分别注册两个监听，然后继续调用能力。下面 runtime 已启用平台事件：

```python
from robot_agent_runtime.models import Event, PlatformEvent, Task

async def handle_task_event(event: Event) -> None:
    print('任务进度', event.task_id, event.event_type)

async def handle_task_done(task: Task) -> None:
    print('任务结果', task.id, task.state, task.result, task.error)

async def handle_task_error(task_id: str | None, error: Exception) -> None:
    print('任务监听失败', task_id, error)

async def handle_platform_event(event: PlatformEvent) -> None:
    print('设备事实', event.event_type, event.data)  # 包括机器人故障。

async def handle_listener_error(error: Exception) -> None:
    print('平台监听失败', error)

task_listener = robot.add_task_listener(
    on_event=handle_task_event, on_done=handle_task_done, on_error=handle_task_error,
)
platform_listener = robot.add_platform_listener(
    on_event=handle_platform_event, on_error=handle_listener_error, after_cursor=0,
)
try:
    observed = await robot.invoke('robot_speak', {'message': '你好'}, operation_id='greeting-42')
    await task_listener.wait()  # 等待当前已登记任务的回调，不代表任务成功。
finally:
    await robot.close_listeners()
```

两种注册都返回 `ports.ListenerHandle`：`closed` 是只读标志，`await close()` 注销观察，
`await wait()` 等待观察完成。默认任务监听只跟踪**注册后本 RobotCapabilities 提交并返回 ID**
的任务，查询 / 取消工具不新增跟踪，直接向其他 SDK 提交的任务不在此范围。幂等重提同一任务
只通知一次；已经开始观察的 ID 保留到注销。`wait=True` 等待超时不取消任务，后台先核对权威
步骤快照，匹配后再观察；校验冲突通过 on_error 报告，不交付旧任务，也不占用后续合法登记。
预期步骤在提交 await 前保存不可变 JSON 快照；普通返回、超时后台监听与超时后补查使用
同一份预期输入，调用方在等待期间修改嵌套字典或列表不会改变它。捕获不调用外来复制 /
序列化钩子或消费生成器；非法输入仍由运行时边界拒绝。
同一 ID 的待校验候选最多保留 64 个，串行尝试；溢出且保留候选均不匹配时，通过 on_error
报告 LISTENER_CAPACITY_EXCEEDED。业务任务不会因为监听校验或容量错误被取消。

已知旧任务用 `add_task_listener(task_ids={'saved-id'}, on_done=...)`，会回放历史，但不跟踪
后续 invoke。显式 `all_tasks=True` 观察其他客户端的任务，与 task_ids 互斥；它使用可选全任务
实时流，没有就绪握手或历史补收保证。该模式终态去重只保留最近 1000 个任务 ID。
`event_types` 只筛选任务 on_event，不隐藏 on_done。on_done 收到独立的权威终态 Task 快照，
成功、失败、取消、未知、待对账都走这个回调；状态本身不代表物理停止。

平台 on_event 接收设备事实，包括故障和连接变化；平台 `on_error(error)` 只报告订阅、
传输或回调处理失败。任务的错误回调是 `on_error(task_id, error)`；全任务订阅故障可能没有
task_id。on_error 均可省略，默认交给 asyncio 事件循环异常处理器；错误回调自身失败只报告
一次，不递归调用它。正常注销不触发 on_error；观察失败不会自动重连或取消业务任务。
平台 cursor 应在业务处理成功后由应用保存，监听不记录业务 ACK。

默认 / 明确 ID 的任务 `wait()` 等待调用时已登记的观察；平台和全任务实时 `wait()` 则持续
到流结束、失败或注销。wait 返回不表示业务成功。取消 wait 的调用者不会取消后台观察，
回调内部不能等待自己的句柄。异步回调可关闭自身或调用 `await robot.close_listeners()`。

两个入口均支持与 SDK 相同的执行模式，一次注册的所有回调使用同一模式：

| execution | 回调执行位置 |
| --- | --- |
| `"caller"`（默认） | 异步回调在观察方循环；同步回调在本能力客户端的共享有界池（4 线程、64 排队、5 秒排队期限） |
| `"group"` | 同一能力客户端中，同名组共享一个专用线程和 loop，任务 / 平台 / 全任务回调可以同组 |
| `"dedicated"` | 每次监听注册独占一个专用线程和 loop |

caller 的观察方循环：自动任务是提交 invoke 的循环，明确 ID / 平台 / 全任务是注册时的循环。
group / dedicated 的同步函数、异步函数及同步返回的 awaitable 全在所选 loop 执行；订阅与
权威查询仍在观察方循环。执行器直接复用 SDK ListenerLoops / CallbackLoop，组空间和预算
归当前 RobotCapabilities，不与另一个能力客户端或底层 SDK 的同名组共享。
只有 group 模式接受非空白 group，按原字符串匹配。共享 loop 不保证不同任务 / 来源串行；
阻塞一个回调会影响同组其他回调。dedicated 的隔离单位是注册，同一注册的多个任务仍共用 loop。

```python
task_listener = robot.add_task_listener(
    on_done=handle_task_done, execution='group', group='reporting',
)
platform_listener = robot.add_platform_listener(
    on_event=handle_platform_event, execution='group', group='reporting',
)
isolated = robot.add_platform_listener(on_event=handle_platform_event, execution='dedicated')
```

默认最多 16 个 group / dedicated 线程位置，同一组计一次，每 loop 最多 64 个在途及排队
回调，排队期限 5 秒。队列满或排队过期分别报告 LISTENER_CALLBACK_QUEUE_FULL /
LISTENER_CALLBACK_TIMEOUT，不会在过期后补执行。on_error 也使用所选 loop 并受相同限制；
无法执行时由观察方循环报告。它们是排队期限，不是正在运行的回调的执行期限。
构造或 create 时可显式配置本能力客户端的资源：

```python
from robot_agent_runtime.options import RuntimeOptions

robot = await RobotCapabilities.create(runtime, listener_options=RuntimeOptions(
    max_listener_threads=16,
    max_pending_listener_callbacks=64,
    listener_queue_timeout_s=5,
    cancel_grace_s=0.2,
))
```

listener_options 不改变后端运行时配置，也不改变 caller 同步池的既有限额；跨 loop 异常
净化使用其中的 max_snapshot_bytes / max_snapshot_depth。最后一个组成员关闭或连续流
自然结束后回收线程；阻塞 / 抵抗取消的线程保留预算直到真实退出，不补建替代线程。
异步回调可以自关闭 / 关闭全部，当前 callback job 返回后线程自行退出。
不同 loop 的 asyncio Event / Queue / Future 和已有 WebSocket 等连接应由其原 loop 使用，
通知原 loop 用 loop.call_soon_threadsafe，协程调用用 asyncio.run_coroutine_threadsafe 桥接。
在 caller 的异步回调中也应避免同步阻塞。
每个能力客户端最多保留 128 个监听注册，超限报告 `LISTENER_CAPACITY_EXCEEDED`。
明确 ID / 平台 / 全任务注册需要运行中的事件循环。

单个句柄 close 只释放自己的观察；用完客户端监听后须 `await robot.close_listeners()` 释放
共享回调池。它是监听资源的最终关闭，多次调用共享结果，之后不能新增监听，但 invoke 和
subscribe_events 仍可使用。runtime 的生命周期仍由应用管理。关闭只有限等待，已开始的同步
回调或抵抗取消的代码可能继续运行，其迟到结果被遗弃；不能据此宣称副作用停止。
需要收齐任务结果应先 wait，再关闭。完整无硬件示例见
[`capability_listeners_demo.py`](../examples/capability_listeners_demo.py)。

### 任务与平台事件

命令调用返回 `Observation`；`subscribe_events()` 可在同一个异步迭代器中接收原始任务
`Event` 与设备事实 `PlatformEvent`。不增加统一事件模型、日志或全局顺序；同一来源内的顺序
保持不变，来源之间没有严格的因果或时间先后保证。

| 参数 | 语义 |
| --- | --- |
| `sources=None` | 默认只订阅 `{'platform'}`，保留原平台接口行为；也可显式选择 `{'task'}` 或两者 |
| `task_ids=None` | 任务模式默认使用可选全任务实时流，只观察登记后的事件 |
| `task_ids={'id', ...}` | 对已知任务分别回放历史后接实时，适合必须取得自己所提交任务结果的场景 |
| `after_sequences={'id': n}` | 指定任务中已处理的序号；缺省从 0 开始，键必须属于 task_ids |
| `after_cursor=0` | 只表示平台事件日志位置，不能当作任务恢复位置 |
| `event_types=None` | 空集合 / None 表示不限类型；已知任务的类型筛选由门面执行，其余转交后端 |
| `task_terminal_only=False` | 为 True 时只交付任务终态，平台事件不受此开关影响 |

终态包括成功、失败、取消、未知与待对账，不等于成功或物理停止。终态事件也不等于 listener
`on_done` 查询到的完整 `Task` 快照；需要最终结果时可调用 `robot_task_status`。

下面假定应用已保存提交任务返回的 task_id，并已启动配置了平台事件源 / 日志的 runtime：

```python
from contextlib import aclosing
from robot_agent_runtime.models import Event

robot = await RobotCapabilities.create(runtime)
async with aclosing(robot.subscribe_events(
    sources={'task', 'platform'},
    task_ids={task_id},
    after_cursor=processed_platform_cursor,
    after_sequences={task_id: processed_task_sequence},
    task_terminal_only=True,
)) as events:
    async for event in events:
        if isinstance(event, Event):
            print('任务终态', event.task_id, event.data['state'])
            # 处理成功后按 task_id 持久保存 event.sequence。
        else:
            print('设备事实', event.event_type, event.data)
            # 处理成功后持久保存平台 event.cursor。
```

已知任务可以在提交完成之后订阅，任务很快结束也能从历史取得终态。内部回看恢复水位前一条，
以识别已经处理的终态，并过滤所有 `sequence <= after_sequences[id]`，不会重复交付。
已知任务流全部结束后，平台流仍继续。全任务实时模式不扫描旧任务，不提供就绪握手或全局恢复
游标；可靠结果通知优先使用 task_ids，断线补收按应用保存的每任务位置进行。

首次拉取时创建订阅，参数独立复制。独立可选 `TaskEventRuntime` 与 `PlatformEventRuntime`
分别表示全任务实时与平台订阅，原 `TaskRuntime` 保持不变。每来源最多一个在途读取，没有输出
队列，也不缓存全任务目录；安静来源不阻塞其他来源。任一来源失败会结束本次聚合并清理其他
自持订阅。关闭不关闭 runtime 或取消物理任务；能力层不解释厂商故障码、自动重试或记录 ACK。
`allowed_skills` 不决定事件可见范围，任务 / 设备事件授权由应用 / 网关控制。

清理共享 0.2 秒预算，超限抛 `CapabilityEventCleanupTimeout`
（`CAPABILITY_EVENT_CLEANUP_TIMEOUT`），不宣称底层读取或外部副作用已经停止。抵抗取消的
读取实际返回后才关闭迭代器，迟到值丢弃，迟到读取 / 清理错误交给所属事件循环异常处理器。
清理失败优先保留原始订阅错误或取消，其余失败与未结束来源附加在异常 notes 中。

缺少事件接口或明确未实现时抛 `CapabilityEventsUnsupported`，错误码
`CAPABILITY_EVENTS_UNSUPPORTED`。后端提供接口但未配置平台事件、游标过期、来源失效等
运行时结构化错误原样传播。远程传输中断抛 `CapabilityEventTransportError`
（`CAPABILITY_EVENT_TRANSPORT_FAILED`），详情包含 `requested_after_cursor` 与
`last_delivered_cursor`（选了平台时）；指定任务时另外提供 `requested_after_sequences` 与
`last_delivered_sequences`。全任务实时模式只保留 `last_delivered_task` 诊断位置，不缓存无界
任务序号表。任务模式错误会标明来源，最后交付位置不是业务 ACK，恢复仍使用应用保存的位置。

`RobotRuntime` / `RemoteRuntime` 支持这三种来源接口。当前 `McpRuntime` 可通过已有单任务
接口观察明确的 task_ids，没有全任务实时或平台接口；请求缺失来源会在打开任何订阅之前明确
报不支持，不静默跳过。门面不会自动增加 MCP 工具或通知协议。
无硬件示例见 `examples/platform_events_demo.py`。

能力名优先保留 `robot_speak` / `robot_navigate_to` / `robot_get_state`；其他技能按
`robot_` 前缀规范化。冲突或超过 64 字符会拒绝装配。参数 schema 和说明来自实际 SkillDefinition，
数字/长度边界由 `schemas.describe_bounds` 复述。不能把 `read_only=False` 解释为一定产生动作；
runtime 技能尚未声明只读属性，因此只有状态控制工具显式标注只读。

默认提交后立即返回任务句柄；`wait=True` 才在 `wait_timeout_s` 内等待，默认 5 秒。
任务本身的期限为 `task_timeout_s`，默认 60 秒，两个期限相互独立。
等待超时返回原 task_id 和 TASK_WAIT_TIMEOUT，不取消、不重试物理任务；调用方取消也不代表硬件停止。
`requires_attention` 在 state 不是 succeeded 时为 true，包括后台执行中和已取消，并非另一个任务状态。
Task 的 result 和运行时错误（含部分执行、未知停止等事实）按实际返回；本地等待和操作身份冲突也会记录在 Observation.error。
这些本地错误不修改持久化 Task，统一层不推测未报告的物理事实。

动作的 operation_id 必须由调用方持久保存，并在同一 runtime 历史内避免与其他调用方冲突。
默认幂等键为 `robot:{operation_id}`；同一逻辑动作经 MCP 或 SDK 重试时保持 ID、参数和命名空间不变。
同一 ID 不同参数只返回原任务并报告 INVALID_REQUEST，不执行第二次动作。
换一个 ID、根据参数内容重新生成 ID、连接重建后重新编号都不是未知结果的恢复方式。
低层任务 API 仍由 runtime 自己解释幂等键；能力层额外核对返回任务与请求步骤是否一致。

原自研 RobotTools 显式保留历史 `agent:` 命名空间和 agent_action 类型；不要将旧 pending 图动作
迁移到默认 robot: 空间后重发。恢复仍遵守 runtime 的不重放与 Reconciler 规则。

统一层的配置/白名单/传输错误分别为 CAPABILITY_CONFIGURATION_INVALID、CAPABILITY_NOT_ALLOWED、
CAPABILITY_TRANSPORT_UNCERTAIN；runtime 错误保持原码。传输不确定结果保留原 operation_id。
catalog、协议握手等调用仍可能直接抛出结构化运行时/传输异常，调用方负责其连接策略。

Agent Skills 的 SKILL.md 是决策指导，与 runtime 的可执行 SkillDefinition 不同。
酒店和导览的确认、租约和业务完成条件仍归对应 Workflow；直接调用基础机器人能力不能冒充完成这些业务流程。

## 模块和验证

```text
src/robot_agent_capabilities/
├── client.py         统一能力客户端、稳定操作、有限等待与事件门面
├── events.py         来源选择、有限读取汇聚、独立恢复位置与有界清理
├── listeners.py      分开的任务 / 平台回调、自动跟踪与共享有界回调资源
├── catalog.py        工具定义、命名、白名单与 schema
├── observations.py   通用 Observation 与事实转换
├── ports.py          TaskRuntime、可选事件端口与 ListenerHandle
├── errors.py         能力层结构化错误
├── __init__.py       RobotCapabilities 入口
└── py.typed          类型标记
```

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -q -c capabilities/pyproject.toml capabilities/tests'
```

契约测试使用真实模拟器、嵌入式 SDK、UDS 与 MCP stdio 子进程；导入隔离检查禁止模型框架或业务包被意外加载。
`tests/test_events.py` 验证事件透明转交、旧后端兼容、错误与游标保留、关闭 / 取消清理，以及
真实嵌入式和 UDS 的回放续传；不加入根 `.verify/sweep.py`。
`tests/test_task_events.py` 验证任务 / 平台汇聚、已完成任务与终态水位回放、全任务实时、
终态筛选、取消清理、延迟读取和多重关闭错误；这些测试也只在独立能力包运行。
`tests/test_contracts.py` 提供可复用的全任务实时端口检查，并用相同的已知任务终态 / 水位恢复
契约验证嵌入式、UDS 和 MCP 三种后端的 caller 与 group 回调。
`tests/test_listeners.py` 验证自动 / 指定 / 全局任务、平台故障与监听错误分离、缺省错误报告、
等待超时与操作身份、输入快照、同步 / 异步回调、自关闭和有界清理；句柄契约在真实三种任务
后端复用。不加入根 sweep。
`tests/test_listener_execution.py` 覆盖 caller / group / dedicated、跨来源同组、客户端与 SDK
命名空间隔离、同步返回 awaitable、错误回调、真实线程和排队预算、自然结束、自关闭 /
关闭全部、取消关闭，以及提交方循环退出后回收自身线程；按能力包配置独立运行。
