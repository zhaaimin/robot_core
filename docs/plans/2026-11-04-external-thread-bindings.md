# 外部对象线程选择设计与实施计划

> 使用 writing-plans、executing-plans 和 test-driven-development 逐项实施。
> 用户已批准共享、指定组、独占三种方案。保留工作区，不自动提交。

**Goal:** 应用在注册已有外部对象时选择固定事件循环线程归属，默认独占保持兼容。

**Architecture:** `ThreadBinding.shared(target)` 使用本运行时默认共享外部线程；
`ThreadBinding.group(target, name)` 使用本运行时同名组线程；
`ThreadBinding.dedicated(target)` 独占。普通对象等同显式独占。
内核、声明校验与监听执行器维持独立。技能、仓储、状态源、对账器均可绑定，
不改变 Protocol，也不将线程策略写入技能声明。工厂构造与初始化迁移不在本次范围。

**Tech Stack:** Python 3.11+、asyncio、现有 LoopThread；不新增依赖。

## 设计边界

- 同一对象按 identity 去重，重复出现的绑定必须相同；包含普通对象与显式配置之间的冲突。
  启动时以 InvalidRequest 拒绝冲突，创建任何外部线程之前完成检查。
- 默认共享与同名为 shared 的指定组仍是不同线程。不同运行时不共享线程。
- `max_extension_threads` 统计实际外部线程与一个独立校验线程，非对象数。
  每个共享线程共用 max_pending_calls / max_control_calls 容量，不随对象数放大。
- 对象仍各有代理、故障状态和快照；一个技能失败或停止不明不会关闭共享线程或毒化其他对象。
  已有物理动作停止确认、资源隔离、状态唯一写入与重启不重放语义不变。
- 共享线程只统一线程和事件循环，不串行化协程；同步阻塞影响整组。
  已有对象的构造仍发生在调用方，SDK 自行转回 owner loop 的行为不变。
- 不同对象即使共享线程，也不在调用方直接操作其 asyncio 状态。
  仓储默认独占；显式共享仓储可能受同组技能阻塞影响。
- 底层 RuntimeCore 直接使用时只解包目标对象；线程策略由 SDK/Server 托管时应用。

## Task 1：公共绑定与分配（thread_binding.py、isolation.py、test_thread_bindings.py）

- [x] 添加三种模式、默认兼容、identity 冲突、配置校验和实际容量的回归。
- [x] 容器运行新测试，确认尚无 ThreadBinding 接口而失败。
- [x] 新增带类型的不可变 ThreadBinding；隔离层先解析绑定与冲突，再按线程键分配 runner。
  保持每个对象一个 _Port，独立收集 runner 并仅启动/关闭一次。
- [x] 容器运行新测试及 test_isolation.py，确认通过。

```python
skills = {
    "navigation.navigate_to": ThreadBinding.group(navigate, "navigation"),
    "navigation.position": ThreadBinding.group(position, "navigation"),
    "audio.speak": ThreadBinding.shared(speak),
    "vendor.blocking": ThreadBinding.dedicated(blocking),
}
```

## Task 2：SDK/Server 接入与边界（core.py、sdk/embedded.py、sdk/__init__.py、测试）

- [x] 测试 SDK 和 Server 的真实执行线程，验证共享协程可交错执行。
- [x] 测试阻塞组不拖住独占技能与内核，取消一个共享技能不影响同组其他技能。
- [x] RuntimeCore 保存绑定原输入，给低层组件解包目标；扩展 SDK 类型与公共导出。
- [x] 对象级毒化与共享线程生命周期检查；所有事件驱动测试均保证释放阻塞对象。

## Task 3：文档、审查与验证

- [x] 将新增测试文件加入 .verify/sweep.py（仅登记，验证仍全部在容器）。
- [x] 更新 README 的 API、状态机说明、模块树、当前范围及示例，更新 AGENTS 线程约定。
- [x] 独立代码审查，处理重要发现。
- [x] 容器正常与 PYTHONASYNCIODEBUG=1 / -W error 全量 pytest，compileall 及现有示例。
  使用独立 pycache_prefix；容器保留，不连接真实机器人。

## 基线

- 2026-11-04：252a0fb，工作树干净；容器完整套件 376 passed（9.82 秒）。
- Git 创建分支被沙箱拒绝，按 AGENTS 约定向用户提供命令，后续不自动提交。

## 完成与验证

- 新增 15 个行为测试；原 376 项回归保留。先确认接口缺失 5 failed，后确认低层
  RuntimeCore 未解包绑定失败；均完成实现后通过。
- 共享组大量声明并发挤满额度的失败回归已修复：同 runner 声明串行准备，不同 runner 并行。
- 独立审查指出泛型不变性会拒绝保存的具体适配器绑定，已改为协变只读包装与独立泛型工厂。
  容器无 mypy/pyright，类型修复经审查与运行时泛型元数据确认，未新增依赖。
- Docker 挂载曾对 sdk/__init__.py 返回截断内容，已同步原文件；所有改动 Python 源码
  均核对宿主/容器 SHA-256 一致，后续使用独立 pycache_prefix。
- 普通完整套件：391 passed，8.47 秒，退出码 0。
  `python3 -B -X pycache_prefix=/tmp/robot-thread-binding-final-normal -m pytest -q`
- 调试完整套件：391 passed，9.87 秒，退出码 0。
  `PYTHONASYNCIODEBUG=1 python3 -B -X pycache_prefix=/tmp/robot-thread-binding-final-debug -W error -m pytest -q -o asyncio_default_fixture_loop_scope=function`
- 容器 compileall、embedded_demo、remote_demo、precondition_demo 与
  cc_api_speech_demo --help 通过；未连接真实机器人。git diff --check 通过。
- 工作区改动保留，不提交、不推送，现有验证容器保留。

## 评审补充修正

用户确认处理外部评审发现，继续保持现有工作区和不提交约定。

- [x] P1：README 示例把状态源改为默认独占，明确需要及时打断的状态源不得与可能阻塞
  的技能共用循环；测试独占状态源可交付标签，反例测试固定同组阻塞会延迟标签的边界。
- [x] P2：先增加三种绑定与直接构造拒绝 None 的失败回归，再由 __post_init__ 抛 InvalidRequest。
- [x] P3：测试单步/总期限下未开始的技能调用不会执行、不会毒化，恢复后可复用。
  测试仓储排队未开始的读取、写入与生命周期超时后仍可用；已有执行中的超时继续毒化。
  修复顺序为 job.cancel_if_queued() 在 runner 锁内撤销尚未开始的工作；若已开始则先标记
  poisoned 再 job.cancel()，同时避免检查/撤销竞态与先取消后禁用时放行后续写入的竞态。
- [x] 线程名体现 shared、group-组名、dedicated-序号，组名不进入内核业务模型。
- [x] 独立审查，容器普通与 asyncio debug/-W error 完整回归、编译与示例；检查宿主/容器 SHA。

### 评审修正验证

- 新增 test_thread_binding_boundaries.py 七项测试，覆盖状态源对照、四种空目标构造、
  仓储读取/写入/启动/关闭排队超时、单步/总期限与两种取消声明、线程名称与仓储取消竞态。
  已登记 .verify/sweep.py；实际验证全部在现有容器执行。
- 初次新增测试 3 failed / 3 passed，失败对应空目标、仓储误禁用、线程名称；修复后定向
  75 passed。追加取消先于禁用的失败探针，再改为原子排队撤销/先禁用后取消，独立复审通过。
- 最终普通完整套件：398 passed，9.56 秒，退出码 0。
  `python3 -B -X pycache_prefix=/tmp/robot-binding-review-final-normal2 -m pytest -q`
- 最终调试完整套件：398 passed，10.98 秒，退出码 0。
  `PYTHONASYNCIODEBUG=1 python3 -B -X pycache_prefix=/tmp/robot-binding-review-final-debug2 -W error -m pytest -q -o asyncio_default_fixture_loop_scope=function`
- 最终容器 compileall 通过；修正期间 embedded/remote/precondition 示例与 speech --help
  均通过。新增/改动 Python 文件宿主与容器 SHA-256 一致，git diff --check 通过。
- 保留原有工作区改动与容器，无新增依赖、无真实机器人调用、无提交或推送。
