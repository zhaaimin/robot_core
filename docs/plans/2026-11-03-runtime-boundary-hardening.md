# Runtime 线程边界加固实施计划

用户已批准上轮九项优化，保持同进程线程架构、不新增依赖、不提交版本，保留已有未提交修改。

## 具体设计与验收

- 所有外部异常分支统一在线程内规范化消息与 details，拒绝对象钩子进入内核。单个快照默认最多 1 MiB、嵌套深度 64，超限使用结构化错误；限制在复制到内核前检查。
- 取消请求与实际停止分离。supports_cancel=False 不强制取消真实工作；有限等待自然结束，未确认则 ExecutionUnresponsive、待对账与资源隔离。支持取消的技能以正常返回或 TaskCancelled 明确确认完成/停止，已经开始的真实调用仅抛 CancelledError 不作为物理停止证明。
- 仓储 start/close 是受控生命周期操作，取消后确认真实完成；未确认则禁用旧实例，不能在旧初始化仍活动时重试或清理。
- 普通请求、事件订阅和控制请求分别有界：现有 max_pending_calls=256；新增 max_subscriptions=256、max_control_calls=16。LoopThread 为控制操作预留容量；跨边界传播受控 context，查询/取消/关闭不会被空闲订阅挤出。
- 生命周期由内核线程串行管理，共享真实完成 Future，不跨调用方 loop 共用 asyncio.Lock/Task。关闭先启动内核关闭；监听清理有 cancel_grace_s 期限，顽固监听不能阻止业务停止。
- TaskRequest.timeout_s 总期限从接受请求时起计算，覆盖验证后排队、资源等待及执行；单步预算取其与剩余总预算的较小值。超期未派发或确认已停止则 FAILED/TASK_TIMEOUT，停止不明仍待对账；持久化与停止确认独立有界，不误报成功。
- 历史回放分页，页大小默认 128，优先使用分页仓储端口；旧端口兼容裁页并在工作线程限制大小。关闭事件缓存默认最多 1000 条，超限显式 EventStreamOverflow，保留水位，不静默丢失。
- CC close 完成清理后传播停止未确认错误，多个错误聚合。

## 内部接口与分工

1. [x] 边界代理：isolation.py、新 boundary.py、tests/test_isolation.py。`bounded_snapshot(value, *, max_bytes, max_depth)` 输出基类模型/普通容器；所有路径共同使用。LoopJob 新增 started() 区分从未开始与已开始调用。仓储新增代理 list_events_page(task_id,after_sequence=0,limit=128)，底层没有该方法时在 worker 内兼容裁页。
2. [x] 执行期限：executor.py/core.py/models.py/resources.py 与 execution/runtime isolation 测试。TaskTimeout/TASK_TIMEOUT、总期限与停止确认；保留状态唯一写入、终态进度约束、重启不重放。
3. [x] 托管与控制：kernel_host.py/thread_runtime.py/sdk/embedded.py/sdk/listeners.py/sdk/remote.py/server.py 与相关测试。LoopThread(...,control_capacity=0)，submit(factory,*,control=False)，独立控制槽；execution_context.py 导出 CONTROL_OPERATION ContextVar[bool]，KernelHost 控制方法及生命周期设置为 True，port proxy 透传。
4. [x] 配置/仓储/契约与适配器：options.py/errors.py、ports.py、repository.py/sqlite_repository.py/events.py、CC close 及测试、README/AGENTS/流程文档。可选 PagedEventRepository Protocol + 契约；内核回放逐页/批次让出。
5. [x] 独立审查，所有探针固化为回归；容器正常与 asyncio debug -W error 完整套件、语法及示例验证，核对宿主/容器 SHA，保留容器。

## 新配置与错误

RuntimeOptions 新增 max_control_calls=16、max_subscriptions=256、max_snapshot_bytes=1048576、max_snapshot_depth=64、event_page_size=128、max_event_buffer=1000，均正整数。

errors.py 新增 TaskTimeout(code TASK_TIMEOUT)、SnapshotTooLarge(code SNAPSHOT_TOO_LARGE)。已有结构化容量、资源隔离、执行未响应和事件溢出错误继续复用。


## 完成与验证（2026-11-03）

- 九项边界加固已完成，保留同进程线程架构，无新增依赖，未提交版本；现有未提交改动与容器保留。
- 跨边界结果/错误统一有界快照；仅正常成功结果或 TaskCancelled 作为停止确认，未确认的技能与资源保持隔离。不支持取消的真实调用在关闭后也不会被强制取消。
- 生命周期由内核共享真实完成操作；普通请求、订阅、控制请求分别限额，端口线程额外预留生命周期容量。嵌入式运行时可跨调用方循环启停；远程客户端及服务端网络连接仍归所属调用方循环。
- 总期限覆盖准入、资源等待及执行，持久化吸收取消后仍检查已过期预算；重复关闭/取消不能中断停止确认或将未知停止误报取消完成。
- 内存与 SQLite 分页回放及可复用端口契约已补齐；关闭先排空事件再关闭仓储，顺序排空避免耗尽控制槽。关闭截止后未完成/未访问流显式报告 EVENT_STREAM_OVERFLOW，并保留已缓存事件与最后交付水位。
- CC 关闭先清理再传播停止未确认错误，多项错误聚合；模拟器与 CC 在确实停止后显式报告 TaskCancelled。
- 独立规格和质量审查已通过。发现的无效取消结果、同时完成/取消、截止期间写入、重复取消覆盖异常、控制槽耗尽、关闭跨页、顺序排空超时静默结束等探针均已固化为回归。
- 宿主与容器 54 个 Python 文件 SHA-256 一致，使用独立 pycache_prefix 避免挂载缓存影响。
- 普通模式：`python3 -B -X pycache_prefix=/tmp/robot-full-normal-1103-final2 -m pytest -q`，376 passed，退出码 0。
- 调试模式：`PYTHONASYNCIODEBUG=1 python3 -B -X pycache_prefix=/tmp/robot-full-debug-1103-final2 -W error -m pytest -q -o asyncio_default_fixture_loop_scope=function`，376 passed，退出码 0。
- 容器 compileall、embedded_demo、remote_demo、precondition_demo、cc_api_speech_demo --help 均通过（调试模式与警告视为错误）。未连接真实机器人。
