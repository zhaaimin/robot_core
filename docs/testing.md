# 测试与验证边界

> 本文由 README 拆分而来，正文未改动。
> 返回 [README](../README.md)

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
统一回调注册、任务隔离与监听生命周期、`test_submit_and_wait` 两种 SDK 的终态等待、
历史回放、幂等与并发、等待超时和取消隔离、`test_callback_executor` 守护线程退出、容量、
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
`test_cc_api_demos` 验证最小与回调示例的结果、事件交付、监听异常、启动失败清理，
以及命令行的连接配置、错误退出和 SDK 连接释放。
平台契约和嵌入式用例还验证混合绑定、实际线程容量、组内阻塞范围、状态源和对账器绑定。
