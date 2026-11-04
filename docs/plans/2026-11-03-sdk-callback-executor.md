# SDK 回调执行器实施计划

> **For agentic workers:** 使用 superpowers:executing-plans 在当前会话逐项执行；用户已批准设计与实施。保留当前工作区的未提交改动，不自动提交。

**Goal:** 同步回调不阻塞内核，监听关闭可取消排队工作，永久阻塞回调不阻止进程退出。

**Architecture:** SDK 内自管 daemon 线程与有界队列，不依赖 ThreadPoolExecutor 私有字段。每项工作在同一把条件锁下领取、取消或遗弃；Future 和定时器只在所属 asyncio 循环操作。内核、状态机、总线和 UDS 协议保持现有实现。

**Tech Stack:** Python 3.11+、asyncio、threading、collections.deque；不增加依赖。

## 已批准设计的具体约定

- 默认 4 个工作线程、64 个排队位置，排队期限 5 秒；内部构造参数允许测试缩小容量与期限。
- 工作状态为 queued/running/finished/cancelled/abandoned。线程在锁内领取后视为在途；close 不等待在途同步代码，也不保证其外部副作用停止。
- 取消监听时取消尚未领取的工作；运行中的工作遗弃结果。完成回传检查 Future 状态和循环生命周期，清理未执行的协程结果，避免迟到异常和协程警告。
- 队列满、排队超时均使用 errors.py 中独立结构化错误；执行器关闭后提交使用 InvalidRequest。
- 排队超时不终止已运行代码；被永久阻塞的线程继续占用槽位，不扩容补线程。同步错误回调也受容量限制，无法执行时交给循环异常处理器。
- async 函数及 async __call__ 在循环执行；普通函数在线程执行，返回 awaitable 时回到循环等待。回调自行抛 CancelledError 转 ListenerCallbackError，真正取消继续传播。
- 语音示例中操作 asyncio Future 的回调改为 async；测试中的跨线程通知使用 call_soon_threadsafe。

## Task 1：执行器回归与替换

**Files:** `tests/test_callback_executor.py`、`.verify/sweep.py`、`src/robot_agent_runtime/sdk/callback_executor.py`、`src/robot_agent_runtime/errors.py`。

- [x] 先新增退出、取消排队、关闭遗弃结果、容量耗尽、排队超时、关闭后提交测试。
  子进程通过 `await asyncio.wait_for(process.communicate(), 3)` 检查真实退出，失败时 kill 并回收；普通测试使用 threading.Event，在 finally 释放。
- [x] 容器执行 `python3 -m pytest -q tests/test_callback_executor.py`，确认旧实现失败且不挂住 pytest（7 failed，子进程已回收）。
- [x] 用 daemon Thread、Condition、deque 实现受控队列，submit 返回 asyncio Future，cancel 立即撤销排队工作，close 取消/遗弃所有已登记工作。
- [x] 添加 ListenerCallbackQueueFull / ListenerCallbackTimeout，分别使用 LISTENER_CALLBACK_QUEUE_FULL / LISTENER_CALLBACK_TIMEOUT 错误码。
- [x] 重跑执行器测试，确认通过且进程实际退出。

## Task 2：监听集成与兼容

**Files:** `tests/test_task_listeners.py`、`tests/test_cc_api.py`、`src/robot_agent_runtime/sdk/listeners.py`、`sdk/embedded.py`、`sdk/remote.py`、`examples/cc_api_speech_demo.py`。

- [x] 先补 async callable、返回 awaitable、自取消、关闭排队和过载错误上报测试，并将两个阻塞测试改为可释放的线程 Event。
- [x] 对照 `_invoke` 中 `inspect.isawaitable(result)` 与 `current_task().cancelling()` 语义确认失败原因。
- [x] 取消 watcher 时同步调用执行器 cancel；恢复异步回调兼容与自取消转换。删除无实际工作的 start 钩子。
- [x] 示例中 `async def on_done(task: Task)` / `async def on_error(task_id: str, error: Exception)` 操作 Future；同步回调通知循环使用 call_soon_threadsafe。
- [x] 容器运行监听、执行器和语音测试，开启 `-o asyncio_debug=true` 检查线程边界（最终定向 63 passed，-W error）。

## Task 3：文档、审查与最终验证

**Files:** `README.md`、`AGENTS.md`、`docs/task-listener-and-eventbus-design.html`。

- [x] 更新公开 API、状态机说明、模块树、当前范围及线程/关闭/容量边界；修正旧流程文档与纯 asyncio 同步回调说明。
- [x] 审查关闭和完成交错、超时与领取交错、迟到协程清理及错误上报，修复发现的问题。额外覆盖取消交付间隙、空闲引用释放、关闭直接撤销排队及遗弃协程清理异常；独立审查发现均先复现再修复。
- [x] 容器执行完整 pytest（正常模式和 asyncio_debug=true），确认退出码 0；执行 compileall 与语音示例 --help。最终正常模式 270 passed，调试模式加 -W error 同样 270 passed，两者均正常退出；语法检查和示例帮助通过。
- [x] 对照 git diff 确认未改内核与协议、不新增依赖、不覆盖用户现有工作、不自动提交。独立审查最终无剩余阻塞项，容器保留。
