# 测试与验证边界

> 返回 [README](../README.md)

## 测试

```bash
docker start robot_core_ubuntu24
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && python3 -m pytest -q'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core/capabilities && python3 -m pytest -q'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core/mcp_bridge && python3 -m pytest -q'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core/agent && python3 -m pytest -q'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core/agent-core && python3 -m pytest -q'
```

本机验证使用保留的 `robot_core_ubuntu24` 容器，基于 `ubt-api-ubuntu24-ros2:latest`，
项目挂载到 `/workspace/robot_core`。测试依赖及附带的 `ubt_robot 2.0.16` 已安装；
验证后保留容器，下次直接复用。测试、语法检查和示例验证均在容器中执行。

完整回归必须分别跑根、`capabilities`、`mcp_bridge`、`agent`、`agent-core` 五个包；根套件
只覆盖 runtime。新增包要在容器中显式 `pip install -e` 安装并确认依赖后再跑，不能把收集时
缺少 `robot_agent_capabilities` 等导入错误当作业务回归。历史宿主 sweep 数字不作为五包验证结论。
本页列出验证方法与覆盖范围，不承诺未运行的硬件行为。

测试按关注点组织：`test_models` 契约、`test_core` 执行路径、`test_faults` 故障注入、
`test_recovery` 重启恢复、`test_preconditions` 机器人状态前置条件、`test_sqlite_repository`
持久化、`test_embedded_sdk` / `test_remote_sdk` 两种接入形态、`test_contracts` 供后续适配器
复用的契约测试、`test_cc_api` 语音事件匹配与取消/断连边界、`test_task_listeners`
统一回调注册、任务隔离与监听生命周期、`test_submit_and_wait` 两种 SDK 的终态等待、
历史回放、幂等与并发、等待超时和取消隔离、`test_callback_executor` 守护线程退出、容量、
排队期限与取消竞态；`test_listener_execution` 覆盖监听执行位置、组内阻塞、独立隔离、
线程预算、排队撤销、远程接入与自关闭；`test_event_paging` 覆盖分页回放、实时去重和批次让出；`test_thread_runtime` / `test_isolation` / `test_kernel_host` /
`test_execution_boundaries` / `test_runtime_isolation` 覆盖线程归属、阻塞外部代码、有限取消、
仓储故障关闭、快照和资源隔离；`test_thread_bindings` 覆盖共享/分组/独占、配置冲突、
实际线程容量、SDK/Server 接入及组内阻塞和取消边界。
`test_listener_public_api` 覆盖公开 track / closed、独立校验快照、拒绝后重新登记、并发候选
次序与有界溢出、wait 跨替代校验、关闭撤销，以及复用执行器的派发检查、取消和迟到协程清理。
它是根测试，已加入根 `.verify/sweep.py` 的收集列表；真实验证仍在容器执行。
线程运行时测试还在循环停止确认后人为保留线程尾部，验证关闭使用同一预算等待实际退出，
防止确认已到达但线程仍存活时提前宣告普通清理完成。
`test_thread_binding_boundaries` 覆盖状态源独占与组内阻塞的对照、空目标拒绝、排队调用
超时撤销后可复用，以及线程名的配置归属。
`test_package_layout` 覆盖新模块导入、旧入口移除、当前路径补丁及独立进程导入顺序。
`test_runtime_ownership` 覆盖内核依赖方向、构造线程、分阶段停止、异常释放与校验隔离；
`test_contracts` 包含本地和托管准入校验器的可复用契约，也用 CC 和非回调假厂商验证
`RobotPlatform` 的共同装配契约。`test_cc_platform` 覆盖共享驱动、响应关联和快照、
断连和过载、控制容量、平台启停及取消等待者后的真实清理，以及 CC 分组和 demo 策略。
`test_cc_api_demos` 验证最小与回调示例的结果、事件交付、监听异常、启动失败清理，
以及命令行的连接配置、错误退出和 SDK 连接释放。
平台契约和嵌入式用例还验证混合绑定、实际线程容量、组内阻塞范围、状态源和对账器绑定。

平台事件相关用例独立组织：

- `test_platform_events`：内存 / SQLite 日志、不可变事件 ID 去重、有界保留、过期与未来游标、
  分页与实时交接、过滤不重编号、快照及慢订阅者隔离。
- `test_platform_event_sdk`：嵌入式 / UDS 订阅、空闲采集、断线补收、可选平台事件源、
  来源失败与端口隔离、来源意外取消对既有 / 新订阅的错误交付，以及正常关闭不误报；
  `test_platform_event_shutdown` 覆盖来源清理、有界关闭，以及已下发
  写入消费取消请求之后不重新启动状态 / 平台事件拉取的回归。
- `test_cc_events`：假 CC API 的故障订阅、初始查询、原生线程回调、重复合并、快照期间增量、
  未验证顺序标记、断连事实和来源错误，不连接机器人。
- `test_all_task_events`：全任务实时观察、首次拉取登记、过滤和既有单任务回放边界。
- `test_event_notifier`：公开流消费、通用出口、每订阅者顺序 / 故障隔离、跨任务去重、
  显式溢出、重复注册清理和不配合取消时的关闭。
- `test_websocket_events`：已连接假 socket 的消息信封、顺序发送、空闲断连、发送失败 / 超时
  及订阅清理、保留迭代器的迟到读取 / 取消后延迟关闭、禁止并发读取和关闭，以及迟到清理
  错误的可见性；不把发送完成当作业务确认。

独立能力包的 `capabilities/tests/test_events.py` 覆盖薄事件门面：原事件与游标转交、筛选参数
快照、旧任务后端与未实现后端、运行时错误透明传播、事件网络错误、取消清理，以及实际
嵌入式 / UDS 回放续传。这组测试按 capabilities 自身配置运行，不加入根 sweep。
`capabilities/tests/test_task_events.py` 另外覆盖已知任务历史与平台汇聚、全任务实时、全部任务
终态、分别恢复水位、已处理终态不重复且能结束、慢源隔离、有限预取，以及延迟读取错误和多重
清理失败的可见性；也按独立包运行。
`capabilities/tests/test_listeners.py` 覆盖分开的任务 / 平台回调、仅自身自动登记、显式历史任务、
全局实时、幂等去重、等待超时、身份冲突与参数变更快照、可选错误报告、同步 / 异步回调、
自关闭 / 关闭全部和阻塞回调有限清理。能力层句柄契约在嵌入式、UDS、MCP 三种任务后端复用，
平台句柄也运行相同的 wait / close / closed 契约；不加入根 sweep。
参数快照回归用事件屏障在提交快照之后、返回之前修改嵌套字典 / 列表，分别检查普通返回、
真实等待超时与超时后补查，另有非法非 JSON 参数仍按原有运行时错误拒绝的兼容检查。
锁、外来复制钩子与带副作用生成器的回归还确认：捕获操作身份不会调用复制钩子或消费生成器。
`capabilities/tests/test_listener_execution.py` 验证 caller / group / dedicated、任务与平台
同组、不同能力客户端与 SDK 的组名隔离、同步返回 awaitable、错误回调执行位置、线程与
排队预算、阻塞隔离和预算保留、自然结束、回调自关闭 / 关闭全部、取消关闭及提交方 loop
退出后的线程自回收。该文件属于能力包，不加入根 sweep。能力契约在真实嵌入式、UDS、MCP
三种后端同时运行 caller 与 group 任务监听。

无硬件示例可在同一容器运行：

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && python3 examples/platform_events_demo.py'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && python3 examples/capability_listeners_demo.py'
```

CC 故障载荷 schema 与快照 / 增量顺序仍需对应固件的现场验证。自动测试不证明故障解除、
设备安全或断线期间事件完整性；WebSocket 转发测试也不覆盖部署方的握手、认证和业务 ACK。
