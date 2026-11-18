# 技能定义与厂商接入

> 返回 [README](../README.md) · 相关：[架构](architecture.md) · [SDK 参考](sdk-reference.md)

## 技能定义

每个技能声明自己的契约，由运行时统一执行校验、加锁、超时与取消：

```python
SkillDefinition(
    name="navigation.navigate_to",
    version="1.0.0",
    description="让机器人移动到 target 指定的地点；成功表示已到达，不表示路径最优。",
    input_schema={
        "type": "object",
        "required": ["target"],
        "properties": {"target": {
            "type": "string",
            "minLength": 1,
            "description": "业务定义的地点标识（站点名或房间号），不是坐标。",
        }},
    },
    resources={"base"},          # 同名资源上的技能串行执行
    permissions={"navigation"},
    preconditions={"estop.clear", "battery.sufficient"},
    timeout_s=30,
    supports_cancel=True,
)
```

`description` 与参数级 `description` 是给模型看的语义，不参与校验。工具 schema 组装时，
`robot_agent_runtime.schemas.describe_bounds()` 会把 `minimum` / `minLength` / `maxItems`
等边界复述进对应节点的 description：schema 仍是唯一执行点，复述只是防止客户端简化
schema 后模型看不到边界。`description` 为空时 `RobotTools` 回退到内置兼容表。

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

平台可额外提供 `event_source: PlatformEventSource | ThreadBinding[PlatformEventSource] | None`。
这是 `from_platform` 通过可选属性读取的扩展，旧平台无需补齐。它只提供 `watch()` 异步流，
不规定厂商使用回调、HTTP、SDK 或 ROS 2；不把厂商生命周期放入内核。来源创建
`PlatformEvent(cursor=0, source=..., event_type=..., data=...)`，日志分配恢复游标。
原生回调须桥接到所属循环并交接有界快照，不能跨线程操作 asyncio 队列。来源与日志的
线程绑定和容量沿用既有规则；需要持久化时向 `from_platform` 传 `event_repository`。

## CC 平台故障事件

`create_cc_api_platform(api, enable_events=False)` 默认不启用事件，`event_source` 返回 `None`，
旧语音接入无需改动。显式启用后，平台拥有 `CcApiEventSource` 并随平台启动 / 关闭，
运行时仍仅消费该端口：

```python
from robot_agent_runtime.adapters.cc_api import create_cc_api_platform
from robot_agent_runtime.repositories.platform_events import SQLitePlatformEventRepository
from robot_agent_runtime.sdk import RobotRuntime

platform = create_cc_api_platform(api, enable_events=True)
await platform.start()
try:
    runtime = RobotRuntime.from_platform(
        repository, platform,
        allowed_permissions={"audio"},
        event_repository=SQLitePlatformEventRepository("runtime.db"),
    )
    await runtime.start()
    try:
        # runtime.subscribe_platform_events() 也能观察没有任务时的设备变化。
        ...
    finally:
        await runtime.close()
finally:
    await platform.close()
```

`CcApiDriver` 统一持有 `cc.api.fault.current.update` 回调并调用 `cc.api.fault.subscribe`；
故障通知订阅和通信失效订阅相互独立，源关闭只删除自己的订阅。开始采集时查询
`cc.api.fault.current.get`，发出 `robot.fault.snapshot`，后续回调发出 `robot.fault.changed`，
连续相同的变化载荷会合并，初始查询期间缓冲的不同变化保留。驱动仍由平台最后释放。

事件 `source="cc_api"`，厂商结构保留在 `data.payload`。故障 schema 及快照 / 回调的
顺序栅栏尚未验证，事件标记 `data.sync.initial_snapshot_order="unverified"`；交付先后
不构成设备时间上的权威顺序，不臆造 raised / cleared、严重度或安全结论。断连先产生
`robot.connection.lost`，再以 `PlatformEventSourceUnavailable` 结束来源；不伪造空故障集合，
不自动重连或重试。来源失效不等于任务动作已经停止，停止确认与资源隔离规则保持原样。

`robot-agent-runtime serve-cc` 显式启用事件，并用 `--database` 对应文件创建独立
`SQLitePlatformEventRepository`；CLI 参数不变。平台日志与任务仓储各自管理连接和事务。
远程消费使用 `RemoteRuntime.subscribe_platform_events`，网络转发见
[SDK 参考](sdk-reference.md#平台事件与通知)，不需要启用自研 Agent。

## CC API 语音播报

`create_cc_api_platform` 接受已连接、专用的 `ubt_robot.Api` 实例，装配驱动和
`audio.speak` 与机器人信息查询技能。当前无状态源和对账器。平台管理请求、订阅和技能，应用继续管理
SDK 的 initialize/start/stop，不自动重连。

工厂参数 `skill_thread_group` 默认为 `None`，同驱动技能使用共享线程；显式传入非空组名时，
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

语音示例按用途分为三个独立入口，原入口的参数和行为保持不变：

| 入口 | 用途 |
| --- | --- |
| [cc_api_speech_minimal_demo.py](../examples/cc_api_speech_minimal_demo.py) | 首次接入：`submit_and_wait()` 直接取得终态，不注册任务监听 |
| [cc_api_speech_callback_demo.py](../examples/cc_api_speech_callback_demo.py) | 回调监听：提交前注册一个监听，等待通知完成后查询结果；监听异常向调用方抛出 |
| [cc_api_speech_demo.py](../examples/cc_api_speech_demo.py) | 原有双监听 caller/dedicated 与阻塞实验，保留所有旧命令行参数；监听异常只打印 |

三份均提供 `speak(api, message) -> Task`，原入口还保留线程实验的关键字参数。
完整阅读顺序见 [示例索引](../examples/README.md)。在装好 `ubt_robot` 的 Linux ARM64
环境中，首次调用可以执行（`CC_API_TOKEN` 通过部署环境传入）：

```bash
python3 examples/cc_api_speech_minimal_demo.py \
  --api-id YOUR_API_ID --address ROBOT_IP:48159 --message '你好，我是机器人'
```

查看回调时执行 `examples/cc_api_speech_callback_demo.py`，使用相同连接参数。它打印
`[EVENT]`、`[DONE]` 和 `[LISTENER ERROR]`，采用默认调用方执行，不包含线程切换或阻塞。
业务失败仍通过最终 Task 表达，监听失败在 `listener.wait()` 后抛出。

原 `cc_api_speech_demo.py` 专门保留线程实验：注册两条任务监听，两条的 `execution`
可以分别指定。`--listener-block` 让监听1 在
第一条事件里同步 `time.sleep(N)`，`[REPORT]` 给出监听2 相邻事件的最大间隔：

| 监听1（阻塞） | 监听2（测量） | 监听2 的 `longest_gap` | 原因 |
| --- | --- | --- | --- |
| `caller` | `caller` | ≈ N | 两条异步回调共用一个提交方循环 |
| `caller` | `dedicated` | ≈ N | 回调虽在独立线程，事件仍经提交方循环投递 |
| `dedicated` | `caller` | 毫秒级 | 阻塞留在监听1 自己的线程 |
| `dedicated` | `dedicated` | 毫秒级 | 两条各占一个回调线程 |

```bash
# 默认组合：监听2 被一起卡住约 2 秒。
python3 examples/cc_api_speech_demo.py --api-id YOUR_API_ID --address ROBOT_IP:48159 \
  --message '你好' --listener-block 2

# 只把监听2 挪到独立线程：仍然会被卡住，因为上游投递还在提交方循环。
python3 examples/cc_api_speech_demo.py --api-id YOUR_API_ID --address ROBOT_IP:48159 \
  --message '你好' --listener-block 2 --listener2-execution dedicated

# 把阻塞的监听1 挪到独立线程：监听2 不再受影响。
python3 examples/cc_api_speech_demo.py --api-id YOUR_API_ID --address ROBOT_IP:48159 \
  --message '你好' --listener-block 2 --listener1-execution dedicated
```

每次回调打印 `[秒] 监听名 事件名 thread=线程名#id`；`dedicated` 的线程名是
`rar-listener-dedicated`，两条监听各一个线程 id。示例在报告和关闭前等待两个监听
交付完成，`listener2_events` 统计第二个监听实际收到的事件数。

在项目根目录的 Python 调用方中，可以直接取得最终 `Task` 快照（`api` 须已连接）：

```python
from examples.cc_api_speech_minimal_demo import speak
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
SDK 连接继续由调用方管理。最小示例通过 `submit_and_wait()` 取得最终快照，无需监听或
结果 Future；回调示例使用 `submit_task()`、`listener.wait()` 和最终查询。
原线程实验的两条监听打印事件、终态和监听异常，第二条作为共享循环阻塞实验的对照，
该实验的监听异常不替代业务任务结果。三个命令行入口都保留最终结果输出，任务失败时以异常退出。
返回值为运行时整理后的结果，不包含 CC API 的完整原始响应。

运行在机器人本机时可省略 `--address`。SDK 不属于内核依赖，宿主常规导入不需要它。
容器中的自动测试使用假回调 API，不连接机器人；真实发声和机器人结束事件仍需现场验证。
