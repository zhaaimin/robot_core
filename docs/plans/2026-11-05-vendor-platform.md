# 厂商接入包首版设计与实施计划

**Goal:** 用 CC API 验证“新增厂商包即可接入”的结构，统一驱动、技能与平台装配，不改变任务内核。

**Architecture:** ports.RobotPlatform 只聚合既有 Skill、RobotStateProvider、Reconciler 与
start/close；具体包的工厂创建平台。CC 驱动独占 SDK 的 finished / disconnect 回调，
将请求和通知交接到固定应用循环，多个技能通过订阅令牌共享驱动。语音技能保留动作完成、
精确停止与不确定停止语义。平台先驱动后技能启动，先技能后驱动关闭。

**Tech Stack:** Python 3.11+、asyncio、typing.Protocol、现有边界工具、pytest；不新增依赖。

## 范围与接口

- `RobotPlatform` 提供只读 skills、robot_state、reconciler，以及 async start()/close()。
  不要求厂商使用回调式 SDK。运行时通过现有构造参数接收这些端口，不识别具体平台。
- `create_cc_api_platform(api, *, stop_timeout_s=5.0, options=None)` 返回 RobotPlatform 契约，
  具体对象为 CcApiPlatform。SDK 的 RobotRuntime.from_platform 接收该契约并装配三个端口。
  api 是应用拥有的已连接对象，原生 SDK 的 initialize/start/stop 继续由应用管理。
- `adapters/cc_api.py` 按职责拆为同名包。厂商 SDK 协议 CcApiClient / CcApiResponse
  定义在包内 contracts.py，公共 ports.py 只保留通用端口；用户明确不需要兼容，删除旧名称，
  不提供兼容别名。新包导出驱动、平台、技能、SDK 协议与工厂，导入不创建对象或加载原生 SDK。
- `CcApiDriver` 负责请求响应、SDK 订阅、finished 与故障多订阅者分发、线程回调桥接、
  有界等待和缓冲，以及厂商响应到通用错误的转换。不同技能共用驱动时不会覆盖全局回调。
- 语音技能负责参数映射、workId 关联（含早到事件）、完成判断、取消与停止确认。
  直接给技能传 api 时创建自有驱动；给技能传共享驱动时只管理自身订阅。
- 回调线程只交接快照与消息，asyncio Future / 事件只在所属循环操作。
  驱动固定 start 所在循环，技能延续现有跨循环执行桥接。
- 请求和事件容量使用 RuntimeOptions 的 max_pending_calls / max_event_buffer，快照使用
  max_snapshot_bytes / max_snapshot_depth。请求响应等待使用 port_timeout_s。
  过载、断连和响应不可信时失败关闭；不自动重连或重发物理动作。
- startup/close 共享真实完成结果，等待者取消不重复清理；技能停止失败仍释放订阅与驱动。
- 目前仅语音技能，无真实状态源或对账器时两个属性明确返回 None，不虚构硬件能力。

## 文件与步骤

- [x] 1. 在 tests/test_cc_platform.py 先写失败测试并加入 .verify/sweep.py；覆盖工厂装配、
  多订阅者、事件关联、请求错误、共享驱动成员关闭、平台生命周期与 Runtime 接入。
  命令：`docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && python3 -m pytest -q tests/test_cc_platform.py'`。
- [x] 2. 在 ports.py 增加 RobotPlatform；建立 adapters/cc_api/driver.py、platform.py、
  speech.py、contracts.py 与 __init__.py。抽取请求和回调机制，通用端口移除厂商协议，
  语音声明和物理停止语义仍由技能解释。
  运行 tests/test_cc_api.py、tests/test_cc_platform.py 和 tests/test_isolation.py。
- [x] 3. 在 tests/test_contracts.py 增加可复用平台契约，用 CC 平台和独立的非回调假厂商
  验证相同装配方式，证明通用接口无需 CC SDK 协议。覆盖停止失败仍释放、重复关闭、
  等待者取消、线程回调、超时与事件过载；按失败测试逐项实现。
- [x] 4. 更新现有 examples/cc_api_speech_demo.py 使用平台工厂，保留业务监听行为；同步
  README 的 API、状态机说明、模块树、接入指南和当前范围，以及 AGENTS 的新增结构约定。
- [x] 5. 独立审查后容器运行完整 pytest、定向 asyncio_debug=true -W error，及示例 --help。
  基线 438 passed，不连接真实机器人。检查 diff，保留当前工作区，不自动提交。

## 使用流程

```python
platform = create_cc_api_platform(api)
await platform.start()
runtime = RobotRuntime.from_platform(repository, platform)
try:
    await runtime.start()
    # 业务提交任务和监听事件。
finally:
    try:
        await runtime.close()
    finally:
        await platform.close()
```

新增厂商自行提供工厂、驱动和技能，实现 RobotPlatform；应用选择相应工厂，
无需修改 RuntimeCore、SDK、其他厂商实现或集中式厂商分支。

## 验证结果

- 完整容器套件：460 passed（基线 438，新增 22 个用例）。
- 嵌入式 SDK、CC 原语音、平台与契约测试：62 passed，启用 asyncio_debug，警告视为错误。
- 示例 `python3 examples/cc_api_speech_demo.py --help` 和 `git diff --check` 通过。
- 独立审查发现的派发后取消丢失结果、finished ID 超限均补失败回归后修复；
  原语音正常取消、精确停止确认与运行时资源隔离测试保持通过。
- 原生 SDK 生命周期仍由应用管理；本次未连接真实机器人，容器保留复用。

用户随后明确不保留兼容：将 CC 专属协议移入 contracts.py 并命名为 CcApiClient /
CcApiResponse，公共 ports.py 删除旧定义，所有活动代码迁移，不设别名。新增包归属测试
先验证旧接口仍存在而失败，迁移后定向调试 59 passed、完整套件 459 passed，示例 --help
与 git diff --check 通过。

SDK 的 RobotRuntime.from_platform 现为生产代码中的 RobotPlatform 消费入口，CC 工厂
返回类型也使用该契约。示例、CC 平台和非回调厂商契约测试均通过该入口装配。
新增行为测试验证平台技能、状态源、重启对账、权限和 RuntimeOptions 均生效，
且平台生命周期仍由应用控制。新增入口前的三个关键用例因缺少 from_platform 失败。

## 审查修正：平台保留线程绑定

迁移到 from_platform 时丢失了 demo 的 audio 分组，而且 RobotPlatform 的返回类型
没有表达底层构造入口已经接受的 ThreadBinding。这两项均修正，保持原线程策略。

- [x] RobotPlatform 的 skills、robot_state、reconciler 显式支持各自的 ThreadBinding。
  from_platform 不解包、不覆盖绑定，内核仍只接收宿主准备后的普通端口。
- [x] CC 工厂及平台新增 skill_thread_group=None；默认继续独占，指定组名时包装技能。
  demo 显式传 skill_thread_group="audio"，恢复此前策略。技能生命周期继续操作原始对象。
- [x] 契约检查解包后验证技能声明，真实运行仍把原始映射交给 from_platform。
  新增同组技能线程一致/容量计数、阻塞半径、绑定状态源/对账器和 demo 分组回归。
- [x] 容器定向调试、完整 pytest、示例 --help，更新 README 和约定。

修正验证：新增 7 个免 fixture 用例。5 个关键测试先失败（缺少工厂分组参数、demo
没有传 audio、契约检查直接读取绑定包装的 definition），修正后通过。容器定向调试
67 passed、完整套件 467 passed，示例 --help 与 git diff --check 通过；独立审查未发现
P1/P2 遗漏。此处线程预算及阻塞边界均通过 from_platform 公共入口验证。
