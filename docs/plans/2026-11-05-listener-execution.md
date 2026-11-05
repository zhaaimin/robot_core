# 监听执行位置设计与实施计划

**Goal:** RobotRuntime 与 RemoteRuntime 的监听支持调用方、分组和独立执行，默认行为兼容。

**Architecture:** 订阅、状态查询与监听生命周期仍由提交方循环管理。仅用户回调通过
受控桥接在 SDK 自有 LoopThread 中执行；组内共用循环，独立监听独占循环，不与端口线程混用。

**Tech Stack:** Python 3.11+、asyncio、现有 LoopThread、pytest；不新增依赖。

## 已确认接口与边界

- `add_task_listener(..., execution="caller", group=None)`；仅 group 模式接受非空组名。
- caller 模式保持异步回调在提交方循环、同步回调在既有 CallbackExecutor 的行为。
- group / dedicated 模式的同步函数、异步函数与同步函数返回的 awaitable 均在所选循环执行。
- 每个注册的 on_event / on_done / on_error 使用同一执行位置；同一任务事件保持顺序和深拷贝隔离。
- 每个 SDK 最多 16 个监听循环；同名组计一次，独立监听各计一次。关闭后未真实退出的线程继续
  占容量，不补建。每循环最多 64 个在途或排队回调，排队期限 5 秒；运行中不强制超时终止。
- RuntimeOptions 提供 max_listener_threads、max_pending_listener_callbacks、listener_queue_timeout_s。
- 满队列与排队过期复用 LISTENER_CALLBACK_QUEUE_FULL / LISTENER_CALLBACK_TIMEOUT；
  线程容量耗尽使用 LISTENER_CAPACITY_EXCEEDED。监听故障不改变业务状态。
- 关闭撤销未开始回调、请求取消在途回调并有限等待；不宣称阻塞线程副作用已停止。
- 回调内禁止等待自身监听；支持回调内关闭监听，SDK 清理不无限等待自身回调。
- 异步资源须在所属循环创建和使用；线程隔离不保证 CPU/GIL 或硬实时隔离。

## 实施步骤

- [x] Task 1：新增 tests/test_listener_execution.py（加入 .verify/sweep.py），先验证缺失 API
  失败；覆盖默认/组/独立线程归属、阻塞隔离、事件顺序、校验、远程接入。
  命令：`docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && python3 -m pytest -q tests/test_listener_execution.py'`。
- [x] Task 2：新增 sdk/listener_execution.py，复用 LoopThread 构造回调执行器和 SDK 内分组管理，
  加入容量、排队期限、取消与限时关闭。修改 sdk/listeners.py、sdk/embedded.py、sdk/remote.py，
  构造时注入执行位置，保留 caller 路径。修改 options.py、errors.py。
- [x] Task 3：补充排队满/超时撤销、真实线程容量、组成员关闭、回调自关闭/自等待、SDK 关闭
  与取消抵抗测试；定向运行直到通过，并检查异步调试警告。
- [x] Task 4：同步 README.md 的 API、使用说明、状态机、模块树与当前范围，以及 AGENTS.md
  的监听线程约定；说明组共享边界与跨循环资源约束。
- [x] Task 5：容器执行完整 `python3 -m pytest -q`、定向 `-o asyncio_debug=true -W error`，
  审查 diff 与用户已有改动。原始容器基线：414 passed。

用户已确认三模式方案并要求执行；在当前挂载工作区直接完成，不自动提交或改动分支。

## 验证结果

- 容器完整套件：438 passed（原始 414，新增 24 个测试）。
- 监听执行、既有监听、LoopThread 和默认回调执行器：92 passed，开启 asyncio_debug，警告视为错误。
- 独立审查发现并补回归：调用方循环退出后的自关闭线程回收、工作线程复核绝对排队期限、
  关闭等待被取消后的注销、远程错误类型与动态错误码保留，以及关闭后领取回调的竞态。
- git diff --check 通过。保留用户原有 examples/cc_api_speech_demo.py 改动。
