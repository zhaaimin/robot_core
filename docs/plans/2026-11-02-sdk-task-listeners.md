# SDK 统一任务监听

2026-11-03 更新：本文保留初版监听设计；执行线程与关闭边界已由
[同进程内核线程隔离实施计划](2026-11-03-kernel-thread-isolation.md) 更新，当前行为以 README 为准。

用户已确认采用“调用方统一注册、内部按任务订阅”的方案。范围为当前 SDK 实例在注册后
成功提交并取得快照的任务，不扫描历史任务，也不监听其他客户端的提交。

## API 与语义

- `runtime.add_task_listener(on_event=None, on_done=None, on_error=None)` 返回
  `TaskListener`，至少提供一个回调；两种 SDK 提供同样的方法。
- 回调可以是同步函数或异步函数。`on_event(Event)` 展示过程；`on_done(Task)` 接收
  成功/失败/取消等终态；`on_error(task_id, exception)` 接收订阅、查询或回调异常。
- 每个注册句柄对每个任务最多启动一次订阅，重复提交幂等任务不重复通知；注册句柄
  保留已登记 ID 直到关闭。不同注册相互独立，收到的事件/结果是独立快照。
- 每个任务按事件顺序调用回调，先完成终态事件回调，再查询并调用 on_done。
  不同任务各有一个监听协程，慢异步回调不阻塞其他任务；同步回调必须快速返回。
- 只有真正观察到终态才调用 on_done；流提前结束不能冒充完成。监听出错后通知
  on_error 并结束该任务监听，不取消业务任务，不影响其他任务。无 on_error 或
  on_error 自身抛异常时交给事件循环异常处理器，不静默吞错。
- `await listener.wait()` 等待调用当时已登记的监听协程；不关闭注册，也不等待未来
  提交。取消 wait 的调用者不取消后台监听。不要从该 listener 的回调内等待自身。
- `await listener.close()` 注销并停止这个注册的所有订阅，不取消业务任务。
  SDK close 先停止监听，再执行既有关闭逻辑；要接收完整结果应在关闭前 await wait。
- 注册本身不启动线程、不改内核状态机、不改 UDS 协议。沿用历史回放与实时订阅接口。

## 实施与验证

- [x] 在 `tests/test_task_listeners.py` 添加免 fixture 测试并加入 `.verify/sweep.py`。
  覆盖多任务、历史回放、失败和取消、幂等、异步回调、异常隔离、停止监听、SDK 关闭、
  远程作用域及关闭不停止服务端任务。
- [x] `sdk/listeners.py` 实现回调类型、TaskListener 和内部注册集合。
  `errors.py` 增加事件流提前结束的结构化错误。
- [x] 两种 SDK 注入共享监听实现，submit 后登记，close 时释放，sdk 导出 TaskListener。
- [x] 语音 demo 用注册式回调展示事件和取得最终 Task，保留原命令行行为。
- [x] README 和整体流程文档说明 API、生命周期、作用域和错误语义。
- [x] 容器运行新增测试、完整 pytest 和示例 --help，不连接真实机器人。

验证容器固定为 `robot_core_ubuntu24`，挂载目录 `/workspace/robot_core`；保留现有未提交
改动，不创建新容器、不变更依赖、不自动提交版本。
