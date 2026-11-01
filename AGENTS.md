# AGENTS.md

写给在本仓库里干活的编码 agent。

项目是什么、公开 API、状态机、前置条件机制的**设计理由**都在 `README.md`，设计与实施计划在
`docs/plans/`。本文件不重复这些，只写**怎么跑起来**、**什么不能改坏**、**代码长什么样**。

## 定位

`robot-agent-runtime`：机器人本机任务执行运行时（Python 3.11+ / asyncio）。内核**不依赖**任何
机器人厂商 SDK、ROS 2 或云服务，硬件通过 `ports.py` 的 Protocol 以适配器注入。

## 环境：先读这段，不要重新诊断

这台 macOS 宿主有几个固定限制，都已经查清楚了：

- **pip 装不上东西**，代理返回 403。`pytest`、`jsonschema`、`aiosqlite` 都没有。
- **`python3` 是系统自带的 3.9.6，不要用它**（`X | None` 注解在运行时就会炸）。
  用 **`python3.13`** —— 它是唯一装了 `pydantic` 2.13.4 的解释器。`python3.11` / `python3.12`
  存在但没有依赖。
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

2026-10-26 在干净工作树上：**95 passed, 2 failed, 42 skipped**。

- 2 个 fail 固定是 `test_sqlite_repository.py` 的
  `test_sqlite_memory_start_and_close_are_idempotent` 和
  `test_sqlite_can_continue_reading_and_writing_after_unique_conflict`，原因是 `aiosqlite` 被
  stub 掉了，不是回归。
- 42 个 skip 是需要 `tmp_path` / `monkeypatch` / `parametrize` 的用例，替身提供不了。

**除这 2 个以外的任何 fail 都是真回归。** 改完代码请对比这组数字，而不是只看“跑完了”。

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

- **绝对导入**：`from robot_agent_runtime.models import Task`，不用相对导入。
- **全量类型注解**，PEP 585/604 风格（`dict[str, Any]`、`Task | None`、`frozenset[str]`）。
- **端口用 `typing.Protocol`** 声明在 `ports.py`，适配器靠结构匹配，不继承、不注册。
- **错误走 `errors.py`**：每个失败模式一个 `AgentRuntimeError` 子类 + 一个 `code` 字符串常量。
  不要裸 `raise RuntimeError`，也不要在业务代码里硬编码错误码字符串。
- **docstring 写“为什么”和边界**，放在类/Protocol 级别，解释不变量与降级行为（看
  `task_state.py`、`ports.py` 的写法）。不写逐行注释，不写复述代码的注释。
- **纯 asyncio**，不引入线程或进程池。超时用 `asyncio.timeout` / `wait_for`。
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
   实现里不要自己做。
7. **事件序号单调递增**，`subscribe_task_events` 先回放历史再接实时，这个顺序不能变。
8. 推送型适配器要做**滞环**和**无变化不推送**（参考 `SimulatedRobot`），否则停在阈值上的
   电池会反复打断刚起来的任务。

## 测试约定

- 按关注点分文件，别往 `test_core.py` 里堆：契约 `test_models`、执行路径 `test_core`、
  故障注入 `test_faults`、重启恢复 `test_recovery`、前置条件 `test_preconditions`、
  持久化 `test_sqlite_repository`、两种接入形态 `test_embedded_sdk` / `test_remote_sdk`、
  供后续适配器复用的 `test_contracts`。
- **新测试尽量免 fixture**。用了 `tmp_path` / `monkeypatch` / `parametrize` 的用例在宿主上
  只能被 skip，等于在容器外没有保护。确实需要就放到 `test_sqlite_repository.py` 这类本来
  就要进容器的文件里。
- 新增测试文件记得加进 `.verify/sweep.py` 的 `FILES`，否则 sweep 根本不会跑它。
- **不要用 `sleep` 等状态。** 用 `wait_for_terminal()` 轮询加 `asyncio.timeout(1)`，或等
  `asyncio.Event`（见 `tests/test_core.py` 顶部的辅助函数）。
- 新增端口（`ports.py` 的 Protocol）要配一个 `test_contracts.py` 里的契约测试，让后续真实
  适配器能直接复用。

## 还没做的事

Agent 集成层（计划生成/重规划）、调度器（`priority` 已持久化但不参与排序，无并发上限与
抢占）、有限重试、结构化日志、真实平台适配器（ROS 2 / 厂商 SDK）、云端管理平面。

动到这些方向时先看 `docs/plans/2026-10-24-robot-agent-runtime-design.md` 里已确认的实施方向，
别另起一套。
