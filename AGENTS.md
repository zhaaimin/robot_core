# AGENTS.md

写给在本仓库里干活的编码 agent。

项目是什么、公开 API、状态机、前置条件机制的**设计理由**都在 `README.md`，设计与实施计划在
`docs/plans/`。本文件不重复这些，只写**怎么跑起来**、**什么不能改坏**、**代码长什么样**。

## 定位

`robot-agent-runtime`：机器人本机任务执行运行时（Python 3.11+ / asyncio）。内核**不依赖**任何
机器人厂商 SDK、ROS 2 或云服务，硬件通过 `ports.py` 的 Protocol 以适配器注入。

仓库对外定位为 Runtime + 统一机器人能力层 + MCP/Python SDK。独立 `capabilities/`
（robot-agent-capabilities）只依赖 runtime，拥有 TaskRuntime、工具目录、稳定操作与 Observation。
MCP 和自研 RobotTools 共用它；外部 Agent 不经过 agent-core 的 LangGraph。业务确认/租约仍归各 Workflow。
默认能力键为 robot:，旧 Agent 的 agent: 键不能迁移重发；不要在统一层复制内核执行策略。

## 环境：先读这段，不要重新诊断

这台 macOS 宿主有几个固定限制，都已经查清楚了：

- **pip 装不上东西**，代理返回 403。`pytest`、`jsonschema`、`aiosqlite` 都没有。
- **`python3` 是系统自带的 3.9.6，不要用它**（`X | None` 注解在运行时就会炸）。
  以前用 **`python3.13`** —— 它是唯一装了 `pydantic` 2.13.4 的解释器。**2026-11-06 实测这台机器
  上 `python3.13` 已不存在**（`python3.11` / `python3.12` 还在，但都没有依赖），
  宿主离线回归暂时跑不了，验证一律进容器。
- **`/tmp` 不可写**。临时文件放 session scratchpad 或项目内的 scratch 目录。
- **Docker Desktop 只能人手点图标启动**，所有程序化路径都失败（`open -a Docker`、
  `launchctl asuser`、`docker desktop start` 全试过）。需要容器时请用户点一下。
- **`git` 写操作偶发被拦**（`.git/index.lock: Operation not permitted`）。失败就把命令交给
  用户，让他用 `!` 前缀跑。

### `.verify/` 是什么

宿主跑不了真 pytest，所以 `.verify/` 里放了 `pytest`、`jsonschema`、`aiosqlite` 的最小替身和
两个 runner。它**只是宿主环境的变通手段**：不属于发布包，`src/` 里任何代码都不许 import 它，
也不要拿它当通用实现来扩展——缺什么就补到刚好够跑测试为止。

`aiosqlite` 的替身只保证 import 成功，真连接会直接抛错。

## 常用命令

```bash
# 宿主离线回归（主力手段，跑所有免 fixture 的用例）
PYTHONPATH=.verify:src python3.13 .verify/sweep.py

# 只跑前置条件那一组（自己处理 sys.path，不用 PYTHONPATH）
python3.13 .verify/run.py

# 语法/导入体检
python3.13 -m compileall -q src/robot_agent_runtime

# 可执行示例
PYTHONPATH=.verify:src python3.13 examples/precondition_demo.py

# 真实完整套件（含 SQLite 与需要 fixture 的用例）—— 只能在容器里
python -m pytest -q
```

### sweep 的已知基线

2026-11-05 在当前工作树（含未提交的线程绑定、按职责分目录、托管装配重构与厂商接入包
`RobotPlatform` / `adapters/cc_api/`）上：**348 passed, 3 failed, 42 skipped**。
（2026-11-06 容器里又多了 7 个用例，宿主因缺 `python3.13` 未复跑这组数字。）

- 3 个 fail 固定是 `aiosqlite` 被 stub 掉的后果，不是回归：
  `test_sqlite_repository.py` 的 `test_sqlite_memory_start_and_close_are_idempotent`、
  `test_sqlite_can_continue_reading_and_writing_after_unique_conflict`，以及
  `test_contracts.py` 的 `test_sqlite_paged_event_repository_contract`（报
  `RuntimeError: aiosqlite is stubbed in .verify; run the SQLite tests in the container`）。
- 42 个 skip 是需要 `tmp_path` / `monkeypatch` / `parametrize` 的用例，替身提供不了。
- sweep 只收集**模块级、无参数、名字以 `test_` 开头**的函数（`.verify/sweep.py:69-73`）。
  `test_contracts.py` 里 unittest 风格的 `class TestSimulatorSkillContract` 在宿主**根本不跑**，
  只有容器里的 pytest 会收集。新的可复用契约检查请写成模块级 `async def check_*` + 一个模块级用例。

**除这 3 个以外的任何 fail 都是真回归。** 改完代码请对比这组数字，而不是只看“跑完了”。

数字会随用例增加往上走，历史基线（2026-10-26：95 / 2 / 42；2026-11-04：286 / 3 / 42；
2026-11-05 上午：302 / 3 / 42）不要再拿来对账。
容器完整套件的同期基线是 **467 passed**（`python3 -m pytest -q`，约 10 秒）。

### 容器

2026-10-27 更新：用户要求**所有验证均在容器里进行**。已基于
`edu_walker_ubuntu24` 对应的 `ubt-api-ubuntu24-ros2:latest` 镜像创建并保留
`robot_core_ubuntu24`，本项目挂载到 `/workspace/robot_core`，已安装项目及测试依赖和
附带的 `ubt_robot 2.0.16`。后续优先复用：

```bash
docker start robot_core_ubuntu24
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && python3 -m pytest -q'
```

此约定取代上面的宿主主力验证方式；宿主离线命令保留作历史环境记录。

真实套件要在容器里跑（`/Users/zhazha/Documents/robot_core` 挂到 `/workspace`）。约定：
**跑完不要 `docker rm -f`，容器留着**。依赖在宿主装不上，每次重建重装纯属浪费；下次
`docker start` + `docker exec` 复用即可，只有镜像或依赖集变了才重建。

历史 `rar-verify` 容器已不存在；`edu_walker_ubuntu24` 是另一项目容器名。
新建容器若需要更换镜像，先跟用户确认，别擅自拉镜像。

## 代码约定

照着现有文件写，别引入新风格：

- **按职责放实现**：`kernel/` 管任务状态与执行，`hosting/` 管线程与调用边界，
  `repositories/` 放仓储实现，`transport/` 放服务端与协议；`sdk/`、`adapters/` 保持原职责。
  公共模型、错误、端口、配置与线程绑定留在包根目录。旧根目录模块已删除，导入与补丁
  均使用子包绝对路径；不要重新添加旧路径兼容入口。不在子包 `__init__` 提前装配。
  内核不得依赖 `hosting` 或 `ThreadBinding`；宿主准备代理后在内核循环创建 RuntimeCore。
  依赖在构造时确定，禁止创建后替换其他对象的私有依赖。准入规则仍由 kernel/policy.py
  实现，通过 AdmissionValidator 端口选择本地或有界隔离执行。
  厂商接入包放 adapters/<vendor>/：platform.py 装配既有端口，driver.py 管 SDK 请求和
  回调分发，技能文件解释完成和停止语义。RobotPlatform 仅聚合 skills/state/reconciler
  与生命周期，内核和 SDK 不依赖具体平台。CC 传入已连接的应用自有 Api，驱动独占 SDK
  回调后多订阅者分发，技能注销只删自己的订阅；先关闭运行时，再关闭平台和原生连接。
  CC 专属 SDK 协议 CcApiClient / CcApiResponse 放 adapters/cc_api/contracts.py，
  通用 ports.py 不放厂商协议，不提供旧名称兼容别名。
  SDK 通过 RobotRuntime.from_platform(repository, platform, ...) 消费 RobotPlatform，
  创建时一次装配平台端口。skills/state/reconciler 的契约均接受对应 ThreadBinding，
  显式绑定原样交给宿主，裸对象仍独占；平台启停操作原始对象。CC 平台可显式指定
  skill_thread_group，语音 demo 保留 audio 分组。平台生命周期仍由应用管理，
  不在内核中启动/关闭厂商连接。
- **绝对导入**：`from robot_agent_runtime.models import Task`，不用相对导入。
- **全量类型注解**，PEP 585/604 风格（`dict[str, Any]`、`Task | None`、`frozenset[str]`）。
- **通用端口用 `typing.Protocol`** 声明在 `ports.py`，厂商 SDK 协议放各自接入包的
  `contracts.py`；适配器靠结构匹配，不继承、不注册。
- **新增运行时模型要同时进快照白名单**：`hosting/boundary.py` 的 `_MODELS` 决定哪些
  pydantic 模型能跨线程传递，漏加会让所有 SDK 路径报 `INTERNAL_ERROR: Extension
  returned an invalid snapshot`（新增 `ActionOutcome` 时踩过）。
- **错误走 `errors.py`**：每个失败模式一个 `AgentRuntimeError` 子类 + 一个 `code` 字符串常量。
  不要裸 `raise RuntimeError`，也不要在业务代码里硬编码错误码字符串。
- **docstring 写“为什么”和边界**，放在类/Protocol 级别，解释不变量与降级行为（看
  `kernel/task_state.py`、`ports.py` 的写法）。不写逐行注释，不写复述代码的注释。
- **内核线程独占，内核逻辑仍为纯 asyncio**。SDK/Server 经 `KernelHost` 托管专用 daemon
  事件循环；状态机、资源锁和总线只在此循环操作，不在线程内直接读写它们。技能、仓储、
  状态源、对账与外部声明校验由 `hosting/isolation.py` 交给固定的 `LoopThread`，不使用进程隔离。
  外部对象注册可用 `ThreadBinding.shared/group/dedicated`，普通对象默认独占；同一对象的
  绑定必须一致，冲突在启动外部线程前拒绝。共享线程只启动/关闭一次，容量按真实线程
  加校验线程计数；对象级故障独立，同组同步阻塞会拖住整组，但不能占用内核循环。
  需及时交付前置条件变化的状态源不得与可能阻塞的技能共用循环，示例默认保持状态源独占。
  调用输入/结果是快照；跨线程取消只表示请求，真实结束才能确认停止。期限或停止确认期
  结束后不得无限 gather 外部代码，不补建被阻塞线程；不确定停止须落待对账并隔离资源。
  仓储调用已开始时超时可能已提交，须毒化停止后续写入；排队超时须先在 runner 锁内撤销，
  再确认未开始，此时不毒化。直接使用低层 RuntimeCore 不提供上述隔离。
  SDK 监听默认 execution="caller"：异步回调留在提交方循环，同步回调用自管
  CallbackExecutor（4 线程、64 排队、5 秒排队期限）。execution="group" 按同 SDK 内
  组名共用专用 LoopThread，execution="dedicated" 按注册独占；两种模式的同步/异步回调
  与返回的 awaitable 都在所选循环，订阅和查询仍留在提交方循环。监听线程预算独立，
  默认 16 个位置，每循环 64 个在途/排队回调，排队 5 秒；工作线程开始前再次核对期限。
  最后一个成员关闭后回收线程，未真实结束的阻塞线程仍计容量。回调自关闭只保留当前
  job，完成后线程自行退出，不依赖提交方循环中的后台清理。不得使用 ThreadPoolExecutor 私有字段或退出钩子。关闭取消未领取工作并
  遗弃在途结果，不能宣称副作用已停止。不要跨线程直接操作 asyncio 对象；只用受控桥接。
  普通请求、订阅和控制操作分别限额，查询/取消/关闭的预留容量必须传递到端口线程。
  生命周期共享真实完成结果并只在内核循环串行；SDK 关闭先启动内核停止，再限时清理监听。
  宿主按 core.stop → 排空已打开订阅 → core.close（仓储）→ 外部线程 → 内核线程关闭。
  通过 begin_close()/close() 协作，不注入 _event_drainer 或跨层调用私有关闭入口。
  stop/close 各自共享真实结果；停止或排空失败不能跳过后续释放，重复调用不得重做清理。
  快照默认 1 MiB/64 层，外部结果与异常在工作线程净化；历史事件分页（默认 128 条），
  实时队列与关闭缓冲有界，溢出保留交付水位并显式报错。任务总期限覆盖准入、排队、
  等资源和执行；停止确认/持久化独立有界，重复取消不得绕过停止确认或误释放资源。
- **不加依赖**。宿主装不上，加一个依赖就等于让所有人只能进容器干活。
- 公开行为变了，**同步改 `README.md`**（API 表、状态机、模块结构树、当前范围四处）；
  新增模块要出现在模块结构树里。

## 必须守住的不变量

这些是运行时的正确性地基，改之前先想清楚，不要为了方便绕过：

1. **`TaskStateStore.transition()` 是任务状态的唯一写入点。** 它在持有该任务锁的前提下，把
   快照与事件用一次仓储调用原子写入，回填权威结果，最后才发事件总线。别在别处改
   `task.state`，也别绕开锁。
2. **重启不重放。** 数据库里是 `RUNNING` 不代表可以接着跑。没有 `Reconciler` 给出权威终态，
   任务就落 `RECONCILIATION_REQUIRED`。这条规则是为了不重复执行非幂等的物理动作。
3. **内核不含机器人领域模型。** 前置条件只做 `skill.preconditions & provider.unsatisfied()`
   的集合求交，标签对运行时不透明。电压阈值、SOC 口径一律在适配器里，别把急停、电量做成
   内核特例或 `TaskState` 的取值。
4. **读机器人状态失败按失败关闭处理**：读不到不等于可用，提交以 `INTERNAL_ERROR` 失败。
5. **`power.charge` 这类技能绝不能声明 `battery.sufficient`**，否则电量低时连充电都会被拒。
   同理电池阈值要留够回充电桩的余量。
6. **新技能必须显式声明** `input_schema` / `resources` / `permissions` / `preconditions` /
   `timeout_s` / `supports_cancel`——校验、加锁、超时、取消都由运行时按声明统一执行，技能
   实现里不要自己做。`description` 与参数级 `description` 是模型可见语义，也应写明；工具
   schema 由 `schemas.describe_bounds()` 把边界复述进 description，schema 本身仍是执行点。
7. **事件序号单调递增**，`subscribe_task_events` 先回放历史再接实时，这个顺序不能变。
8. 推送型适配器要做**滞环**和**无变化不推送**（参考 `SimulatedRobot`），否则停在阈值上的
   电池会反复打断刚起来的任务。
9. **失败不等于没动。** 技能返回 `success=False` 时，能否释放资源只看 `SkillResult.action`：
   `executed="none"` 或 `stopped=True` 才释放，`executed="unknown"`、`stopped` 非 `True`、
   `operator_required=True` 一律隔离并落待对账。适配器不得因为“调用返回了失败”就假定执行器
   静止；`action=None` 表示尚未接入，内核沿用原有异常路径，不做任何静止假设。

## 测试约定

- 按关注点分文件，别往 `test_core.py` 里堆：契约 `test_models`、执行路径 `test_core`、
  故障注入 `test_faults`、重启恢复 `test_recovery`、前置条件 `test_preconditions`、
  持久化 `test_sqlite_repository`、两种接入形态 `test_embedded_sdk` / `test_remote_sdk`、
  供后续适配器复用的 `test_contracts`。
- **新测试尽量免 fixture**。用了 `tmp_path` / `monkeypatch` / `parametrize` 的用例在宿主上
  只能被 skip，等于在容器外没有保护。确实需要就放到 `test_sqlite_repository.py` 这类本来
  就要进容器的文件里。
- 新增测试文件记得加进 `.verify/sweep.py` 的 `FILES`，否则 sweep 根本不会跑它。
  此项指根 `tests/`；capabilities/agent-core/各 Agent/MCP 的独立测试按各自 pyproject 在容器单独运行，不加入根 sweep。
- **不要用 `sleep` 等状态。** 用 `wait_for_terminal()` 轮询加 `asyncio.timeout(1)`，或等
  `asyncio.Event`（见 `tests/test_core.py` 顶部的辅助函数）。
- 新增端口（`ports.py` 的 Protocol）要配一个 `test_contracts.py` 里的契约测试，让后续真实
  适配器能直接复用。

## 还没做的事

已具备统一能力 SDK、原生 MCP 工具与基础 Agent 决策闭环。尚未完成复杂计划生成/自动重规划、
调度器（`priority` 已持久化但不参与排序，无并发上限与抢占）、有限重试、结构化日志、
其他真实平台能力（导航、ROS 2 等）、云端管理平面。CC 厂商语音适配已存在。

动到这些方向时先看 `docs/plans/2026-10-24-robot-agent-runtime-design.md` 里已确认的实施方向，
别另起一套。
