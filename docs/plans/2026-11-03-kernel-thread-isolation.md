# 同进程内核线程隔离实施计划

用户已批准：不使用进程隔离；内核专用线程/事件循环，监听留在调用方，技能、状态源和对账器在线程中执行。沿用当前未提交的回调执行器修复，不提交版本。

## 边界与验收

- RobotRuntime 与 RuntimeServer 托管专用 daemon 内核线程。底层 RuntimeCore 保留纯 asyncio 引擎供内部/契约测试使用，直接使用它属于受信任的低层接口，不提供线程隔离。
- 不在内核执行外部实现：技能、仓储、状态读取/订阅、对账分别使用固定的 daemon 事件循环线程，Schema 校验另有固定线程。每个对象具有稳定线程归属；线程数量有上限，卡住后不补线程。参数、结果、异常在边界规范化为安全快照，权限配置和注册名也不携带外部钩子。
- 新增 RuntimeOptions：port_timeout_s=5、cancel_grace_s=0.2、max_extension_threads=32、max_pending_calls=256。技能执行期限沿用声明；超时/取消允许有限停止确认，未确认则隔离该技能与命名资源，终态 RECONCILIATION_REQUIRED，不继续执行物理动作。
- SDK 调用通过线程安全 Future 交接，取消不会虚构远端完成。事件通过逐项拉取跨线程交接，沿用回放/实时顺序；关闭清理订阅。
- 外部端口截止后故障关闭：状态读取仍按 INTERNAL_ERROR 拒绝提交；对账不确定仍 RECONCILIATION_REQUIRED；仓储超时后毒化，禁止随后继续写，避免不确定提交重叠。
- 外部代码可以继续持有线程并产生副作用；不能强杀线程、不能抵御持有 GIL 的原生代码或系统资源耗尽。不承诺硬实时或绝对不死锁。

## 任务

1. [x] `thread_runtime.py`：LoopThread/LoopJob，独立 daemon loop、受控容量、取消请求和实际完成分离、有限关闭；`RuntimeOptions` 与结构化隔离错误。
2. [x] `isolation.py`：外部端口代理，冻结技能声明、桥接取消/进度，统一端口超时与毒化，更新 CC API 及模拟状态源的线程归属处理。
3. [x] `kernel_host.py`：内核托管与事件迭代交接；接入 embedded SDK 和 RuntimeServer，确保监听留在调用方。
4. [x] executor/resources：有限取消等待、失控技能与资源隔离、不误报停止成功；保持状态唯一写入与重启不重放。
5. [x] 测试先行：阻塞异步监听、阻塞技能、吞取消、卡住状态源/仓储、独立技能可推进、事件顺序、关闭与启动竞态、快照独立；容器正常与 asyncio debug -W error 全量验证。
6. [x] 更新 README 四处、AGENTS、流程文档、示例与契约，所有验证在 robot_core_ubuntu24 内，保留容器。

## 完成与验证

- 独立审查发现均先补失败回归再修复：停止未确认释放资源、终态后迟到进度、loop.close 并发窗口、致命异常穿过 Future、声明失败补线程、CC 断连后资源复用、预先取消仍派发、进度类型转换持共享锁、权限/注册键钩子进入内核。
- 最终宿主/容器共 50 个 Python 文件 SHA-256 一致；使用独立 pycache_prefix 避免 Docker 挂载字节码缓存影响验证。
- 普通模式：`python3 -B -X pycache_prefix=/tmp/robot-core-final-normal-1103 -m pytest -q`，332 passed，退出码 0。
- 调试模式：`PYTHONASYNCIODEBUG=1 python3 -B -X pycache_prefix=/tmp/robot-core-final-debug-1103 -W error -m pytest -q -o asyncio_default_fixture_loop_scope=function`，332 passed，退出码 0。
- 容器 compileall、embedded_demo、remote_demo、precondition_demo 与 cc_api_speech_demo --help 均通过。未连接真实机器人，未新增依赖，未提交版本，容器保留。

## 内部接口

`LoopThread(name, max_pending=256)` 提供 `await start()`、`submit(factory)->LoopJob`、`await call(factory, timeout_s=None)`、`await close(grace_s=0.2)`。factory 在目标线程中调用。`LoopJob` 提供 `await wait(timeout_s=None)`（shield实际完成，不隐式取消）、`cancel()`（仅请求取消）、`done()`、`result()`；取消/超时等待方不会使实际完成 Future 提前进入终态。

`isolate_ports(repository, skills, reconciler, robot_state, options)` 返回 `IsolatedPorts`，提供同名端口属性与 `await prepare()` / `await close()`；每对象稳定线程。Skill 代理公开冻结 definition，执行结束只返回快照，不改核心状态。代理取消后超过 grace 抛 `ExecutionUnresponsive`，后续拒绝该实例；WorkflowExecutor 负责隔离资源与写终态。

`KernelHost(core, options)` 在内核线程启动前配置隔离端口；`await start/close/call` 托管核心，SDK 原方法签名保持兼容并增加可选 options。服务端 wire 协议不变。
