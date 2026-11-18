# 变更记录

> **项目创建**：2026-10-24

本文件记录对使用者的行为影响。内部重构、测试补充不在此重复。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增

- 能力层任务 / 平台监听均支持 execution='caller'|'group'|'dedicated' 和 group，直接复用
  SDK 的回调 loop 与线程 / 排队预算机制。当前能力客户端的跨来源同名组共享 loop；不同
  客户端及底层 SDK 的组空间独立。默认行为不变，构造 / create 可通过 listener_options
  独立配置监听资源；支持跨 loop 自关闭和关闭全部，阻塞线程保留容量直到实际退出。
- 能力客户端新增独立的 `add_task_listener(...)` / `add_platform_listener(...)`：默认自动
  观察本客户端后续 invoke 的任务，也支持指定历史任务和显式全任务实时观察。任务终态通过
  on_done 提供权威 Task，设备故障通过平台 on_event 交付；可选 on_error 仅报告观察 / 回调失败。
  等待超时继续观察并核对不可变的步骤快照，身份冲突不误交付旧任务。句柄提供 wait / close /
  closed，`close_listeners()` 释放本客户端有界共享回调资源，不关闭 runtime 或取消任务。
  新增无硬件示例 `examples/capability_listeners_demo.py`。
- SDK `TaskListener` 公开 track / closed，允许上层复用已有任务回放、去重和权威终态交付；
  可选观察资格校验失败不改变任务，同 ID 待校验候选有界串行处理。CallbackExecutor 新增
  invoke，复用既有同步线程池与异步回调派发边界。
- 能力层事件门面支持 `sources={'task','platform'}` 汇聚原始任务与平台事件，默认平台行为不变。
  指定 task_ids 时按 after_sequences 回放后接实时，task_terminal_only 可筛选所有任务终态；
  全任务模式仅实时，通过独立可选 TaskEventRuntime 接入。恢复位置分开保存，无全局游标 / 顺序。
  订阅清理有界，超限报告 `CAPABILITY_EVENT_CLEANUP_TIMEOUT`；迟到错误与次要清理失败显式可见。
- 能力层新增可选 `RobotCapabilities.subscribe_events(...)` 门面，默认直接复用 `PlatformEvent`；
  显式任务模式保留原始 `Event`，独立可选端口不扩充旧任务端口。缺少订阅能力报告
  `CAPABILITY_EVENTS_UNSUPPORTED`，事件网络中断报告 `CAPABILITY_EVENT_TRANSPORT_FAILED`，
  运行时结构化错误保持原样。订阅关闭不关闭 runtime 或取消任务，MCP 工具与协议不变。
- 可选的平台事件子系统：`PlatformEventSource`、`PlatformEventRepository`、
  `PlatformEvent` 与内存 / SQLite 日志。事件先保存后分发，日志游标独立于任务序号；
  默认保留 10000 条，保留窗口内按事件 ID 去重，同 ID 的不同载荷被拒绝。
- 两种 SDK 与 UDS 支持 `subscribe_platform_events(after_cursor=0, event_types=None)`；
  `RobotRuntime` / `RuntimeServer` 可注入事件源与日志，`from_platform` 可读取可选事件源，
  兼容没有该属性的旧平台。历史过期报告 `PLATFORM_EVENT_CURSOR_EXPIRED`，未来游标被拒绝。
- `subscribe_all_task_events(event_types=None)` 提供从首次拉取登记起的全任务实时流；
  断线补收仍按各任务的 `task_id/sequence`，不提供全局任务游标。
- CC 工厂通过 `enable_events=True` 启用故障快照、变化和连接丢失事件；默认关闭。
  `serve-cc` 默认启用并使用同数据库中的独立 SQLite 事件日志，原有 CLI 参数不变。
  未验证的厂商载荷保留在 `data.payload`，不推断故障解除、严重度或安全状态，也不自动重连。
- `transport.websocket` 为应用已连接的 socket 提供平台 / 全任务事件顺序转发；不新增
  WebSocket 依赖或服务器。调用方负责认证、连接生命周期和业务处理后的游标保存。
- 无硬件示例 `examples/platform_events_demo.py` 演示空闲采集、初始事件、变化和游标补收。

### 行为变化

- 移除 Core / EventBus 的通知私有接线，可选出口迁至 `notifications.notifier`，通过公开流
  `consume()`；通用 `DeliveryChannel` 只定义 `deliver(event)` / `close()`，Webhook 单独装配。
  各订阅者串行、有界并独立失败；去重缓存不作为持久化 ACK，错误可通过 `subscriber_error()` 查询。
- 平台事件在内核之外的独立循环采集，源与日志复用既有线程绑定和外部线程总容量。
  来源失败保留已入库事件并显式结束订阅，不改写任务状态。网络发送完成与有界关闭均不证明
  业务已处理或外部副作用已停止。

### 修复

- 平台事件源自行取消时明确报告 `PLATFORM_EVENT_SOURCE_UNAVAILABLE`，既有和新订阅在读完
  已保存历史后结束，不再无限等待；运行时正常关闭仍不报告来源故障。
- WebSocket 转发的读取抵抗取消时，在有界返回后保留延迟清理，待读取实际结束再关闭迭代器，
  避免断线重连积累未释放的订阅。迟到值不发送，迟到清理异常交给事件循环报告。
- 能力调用在提交前保存独立的预期步骤快照，普通返回、等待超时后的观察与补查共用该快照，
  避免等待期间修改原始嵌套参数导致误判操作身份冲突或丢失任务回调。非法输入仍由运行时拒绝。
- 非自关闭的循环线程在原 grace 截止前补充实际线程退出确认，修复收到循环停止确认后
  线程仍短暂存活的竞态；不阻塞调用方循环、不增加等待线程或关闭预算，保留超时与自关闭行为。
- 修复平台事件宿主入口命名不一致导致嵌入式 / UDS 平台订阅失败的问题。
- 关闭时若状态监听或平台采集正在确认一次已下发的持久化写入，写入返回后立即退出采集，
  不再等待下一条状态 / 设备事件，避免已消费取消请求的后台监听使关闭持续等待。
  已开始写入的真实结果确认与任务状态的原子保存规则保持不变。

## [0.1.0] - 2026-11-14

首个公开版本。

### 新增

- **MCP 桥支持 Streamable HTTP 传输**：`serve_http(socket_path, allowed_skills, host, port, token)`，
  端点 `/mcp`，`--mode stdio|http`，`--token` / `--token-env`。HTTP 模式默认监听 `127.0.0.1`，
  令牌为空时启动即失败；401/404 在进入 MCP 处理前拦截。两种传输暴露同一份工具目录，
  准入与执行语义一致。对应测试 `mcp_bridge/tests/test_mcp_bridge_http.py`。
- **技能失败时返回执行事实**（`SkillResult.action: ActionOutcome`）：`executed` 为
  `none|some|unknown`，`stopped` 三态，外加 `operator_required`。
- **`SkillDefinition.description` 参与 MCP 与内置工具的描述渲染**；边界值经
  `robot_agent_runtime.schemas.describe_bounds()` 复述进参数 description。schema 仍是唯一执行点。

### 行为变化

- 失败但无法确认静止（`executed != none` 且 `stopped != true`，或 `operator_required`）时，
  内核在释放资源锁**之前**隔离资源并抛 `ExecutionUnsettled`（`EXECUTION_UNSETTLED`，
  继承 `ExecutionUnresponsive` 的隔离后果）。新增错误码不改变既有 `EXECUTION_UNRESPONSIVE` 语义。
- 移除 `--scenario` 参数与 `scenarios/` 包；三个应用各自携带 `StorageProfile` 常量
  （`ROBOT_PROFILE` / `HOTEL_PROFILE` / `GUIDE_PROFILE`），配置快照不跨应用复用。
- 根目录模块迁入 `kernel` / `hosting` / `repositories` / `transport`，旧导入路径已删除。
  路径对照见 [架构 § 模块结构](docs/architecture.md#模块结构)。
- 后端白名单收窄为：robot `simulator|cc`，hotel/guide `simulator|mcp`。
  通用 agent 不再自带酒店技能，需要技能时必须提供 `--skills-dir`。

### 兼容性

- `SkillResult` 新增可选字段 `action`，未实现该字段的适配器行为不变（内核不假设静止）。
- `SkillDefinition.description` 默认为空字符串，旧定义无需改动。

### 文档

- README 从 1058 行重组为「一份 README + 五个专题文档」，正文内容未删减：
  [架构](docs/architecture.md)、[SDK 参考](docs/sdk-reference.md)、
  [生命周期与安全](docs/lifecycle.md)、[技能与厂商接入](docs/adapters.md)、
  [测试](docs/testing.md)。
- 修正过期表述：MCP 桥此前标注「只支持本机 stdio，不提供 HTTP 服务」，
  实际已提供 `--mode http`（Streamable HTTP + Bearer）。
- 修正技能示例中的权限名（`robot.navigation` → `navigation`），与 `simulator.py`、
  `allowed_permissions` 的取值一致。
- 新增 `assets/architecture.svg` 架构图，README 引用。
