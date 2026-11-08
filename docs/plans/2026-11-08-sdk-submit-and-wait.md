# SDK submit_and_wait

## 设计与边界

按调用方指定的 API，为 `RobotRuntime` 和 `RemoteRuntime` 增加：

```python
final = await runtime.submit_and_wait(request, timeout_s=None)
```

公开签名为 `async def submit_and_wait(self, request: TaskRequest, *,
timeout_s: float | None = None) -> Task`。成功、失败、取消、未知、待对账均返回终态
快照，业务失败仍通过 `Task.error` 表达。提交、订阅、查询错误继续抛出。

实现选择复用 `submit_task`、历史回放订阅和最终查询。相比临时注册全局监听，此方案
只观察当前任务，没有监听注册先后限制，也不会让并发调用收到彼此的回调。相比轮询，
直接复用已有的有界事件交接，不增加周期查询。两种 SDK 使用一个内部辅助模块。

`timeout_s` 在提交返回后开始计时，覆盖订阅和最终查询；`None` 表示无限等待。
有限值须为正数且有限，提交前检查。期限到达抛 `TaskWaitTimeout`
（`TASK_WAIT_TIMEOUT`），详情包含 `task_id`、`timeout_s`，不改变业务任务。
调用方取消也只关闭本次订阅，不主动取消机器人动作。真正任务总期限仍由
`TaskRequest.timeout_s` 控制；取消动作继续显式调用 `cancel_task()`。

已返回终态的提交（准入失败、幂等重提等）直接返回。非终态任务通过历史回放接实时，
收到终态后关闭订阅并读取权威快照。流提前结束或终态事件后仍查询到非终态均抛
`TaskEventStreamEnded`，不能冒充完成。等待结束不保证独立的注册监听回调已经交付，
需要收齐回调时仍使用 `listener.wait()`。

不修改内核、任务状态机、传输协议、厂商连接所有权或既有监听的登记范围。
CC demo 保留两监听阻塞实验，用本方法获取最终任务，移除 Future 和跨循环结果桥接。

## 实施计划

- [x] 新增免 fixture 的 `tests/test_submit_and_wait.py`，加入 `.verify/sweep.py`；
  两种 SDK 均覆盖正常结果、准入与执行失败、全部既有终态、完成早于订阅、并发隔离、
  幂等、等待超时与调用方取消、订阅释放、流提前结束、底层错误传播。
  容器运行 `python3 -m pytest -q tests/test_submit_and_wait.py`，确认因方法缺失失败。
- [x] 在 `sdk/task_waiting.py` 实现共享提交与等待逻辑，在两种 SDK 中增加薄包装；
  `errors.py` 定义 `TaskWaitTimeout`。重跑新增测试并核对错误原因。
- [x] CC demo 用 `submit_and_wait` 取得结果，保留监听实验并在报告前等待监听结束；
  用假 CC API 验证播报成功与失败返回，避免仅验证语法。
- [x] 更新 README 的 API 表、状态机说明、模块树、当前范围与快速用法；
  同步结果获取文档与架构文档中的 API/结果流说明。
- [x] 容器跑 SDK、监听、CC 相关测试及完整套件，运行嵌入式/远程/前置条件示例与
  CC demo 的 `--help`；检查 diff 后交付，不自动提交版本。

## 验证记录

改动前首次全套在 428 个用例通过后阻塞，107 秒时中断；对应下一用例
`test_state_provider_sharing_a_blocked_skill_loop_cannot_deliver_precondition_loss`
单独运行通过（1 passed）。未改其实现，重新运行带栈转储和总时限的全套核对基线。

基线重跑：467 passed in 9.72s。首批新增 14 个测试均先因方法缺失失败，
实现后 14 passed；补齐查询期限、执行期限区别、并发幂等和主动取消后，
等待与 CC demo 相关测试为 21 passed。

最终验证：SDK、监听和 CC 相关组合 177 passed；审查复跑等待与 CC 平台测试
41 passed，未发现实现层面的实质问题，两处旧 Future 文案已同步。
嵌入式、远程和前置条件示例及 CC demo `--help` 均成功。

完整连跑在新增 20 个用例后仍遇到相同的旧用例偶发阻塞，448 passed 后到达
60 秒总时限；未修改该旧用例或内核。分组覆盖全部用例：排除此用例
486 passed, 1 deselected in 10.41s；该用例单独 1 passed in 0.19s。
因此记录为全部 487 个用例分组通过，不宣称完整连跑已消除偶发阻塞。
