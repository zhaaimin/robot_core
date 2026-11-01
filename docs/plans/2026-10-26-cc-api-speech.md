# CC API 语音播报接入

日期：2026-10-26。范围：通过已有运行时执行 `audio.speak`。

## 已确认的行为

用户确认：收到对应 `workId` 的 `cc.api.work.finished` 就视为播报成功，暂不解析结束原因。
启动回调仅表示受理。已经由运行时请求的取消、超时与关闭仍保留原来的任务语义。

## 接入设计

使用 `cc_api/examples/03_async_request_demo.py` 展示的 `ubt_robot.Api.request` 回调接口，
发送与 `VoiceInterface.speak_async()` 相同的 `cc.api.work.start` 请求：技能 `A000001`，
参数 `text`、`speed=50`、`volume=50`，每次调用使用独立 `traceId`。

不直接调用 `RobotClient`：其 `RobotExecutor._run_with_timeout()` 内部创建线程并 join，
即便方法名包含 async，也可能阻塞调用方。适配器只将 SDK 回调用
`loop.call_soon_threadsafe()` 送回 asyncio 事件循环，不创建线程或进程池。
SDK 自身的连接线程仍属于厂商库。

原生 SDK 的初始化、连接等待和停止在示例入口的 `asyncio.run()` 外完成。
适配器使用应用提供的已连接、专用 Api 实例，负责订阅结束事件和注册断连回调。
不修改原有 `cc_api/`，不把 Linux ARM64 SDK 加入内核依赖。

`CcApiSpeechSkill` 声明 `resources={speaker}`、`permissions={audio}`、空前置条件、
30 秒技能超时和支持取消。运行时继续负责参数校验、资源互斥、技能超时和状态写入。

适配器处理以下边界：

- 先注册结束事件，再启动播报；启动响应前到达的结束事件暂存于有界集合。
- 只匹配本次启动返回的 `workId`；无关和重复事件不结束任务。
- 取消时发送 `cc.api.work.ctrl` 的 `stop`，并等待匹配的 finished。
- 超时/关闭导致协程取消时，同样停止当前播报。清理最多等待 5 秒，此界限是停止确认
  的通信清理界限，不替代运行时技能超时。
- 发起后拿不到 workId、停止无法确认或断连时，适配器拒绝后续播报，防止资源锁释放后
  与未知状态的旧播报重叠。需要关闭原连接并创建新适配器。
- 对开始请求的明确拒绝直接报告结构化错误；普通 finished 的内容不影响成功判定。
- 不提供 Reconciler，不在重启时重放播报。

## 文件与实施步骤

- [x] `tests/test_cc_api.py`：先写离线假 API 与免 fixture 行为测试。
  验证仅启动不能成功、匹配 finished 才成功、早到/无关/重复事件、请求拒绝、
  缺少 workId、断连、正常取消、启动途中取消、超时停止和 speaker 互斥。
- [x] `src/robot_agent_runtime/ports.py`：声明无厂商依赖的 `CallbackApiResponse` 与
  `CallbackRobotApi`，表达请求、通知、服务回调和断连回调。
- [x] `src/robot_agent_runtime/errors.py`：增加连接、请求、响应与停止确认错误。
- [x] `src/robot_agent_runtime/adapters/cc_api.py`：实现 `CcApiSpeechSkill`。
- [x] `tests/test_contracts.py`：为回调 API 及技能契约添加免 fixture 契约测试。
- [x] `.verify/sweep.py`：加入 `test_cc_api.py`。
- [x] `examples/cc_api_speech_demo.py`：参数化 API ID、地址、文本；token 从环境变量读取。
  SDK 惰性导入，使 `--help` 和常规运行时导入不需要机器人依赖。
- [x] `README.md`：更新 API 生命周期说明、状态机说明、模块树和当前范围，记录
  finished 即成功的临时契约与 Linux ARM64 真机验证限制。
- [x] 在 `robot_core_ubuntu24` 运行新增测试、完整 pytest、compileall 和示例 `--help`，复查 diff。

## 验证基线

当前 shell 的 `python3.13` 指向另一个项目的 venv，缺少 pydantic；使用
`/Users/zhazha/miniconda3/bin/python3.13`（pydantic 2.13.4）。
沙箱外离线 sweep 的改动前结果为 **95 passed, 2 failed, 42 skipped**，
两项失败均为已知 SQLite stub。沙箱内还会禁止 Unix socket 绑定。

不运行会连接真实机器人并发声的 SDK demo；真机测试留给应用部署环境。

用户随后要求全部验证在容器里，并明确授权基于 `edu_walker_ubuntu24` 的镜像创建并保留
新容器。已创建 `robot_core_ubuntu24`，镜像 `ubt-api-ubuntu24-ros2:latest`
（sha256:2b1f255ddb81d8f4969d163319e57b810c3caa54a2d22c1bc955a552ff2fc654），
绑定当前项目到 `/workspace/robot_core`，安装 `.[test]` 及附带的 `ubt_robot 2.0.16`。
之后所有验证均在该容器里进行。

容器 Python 为 3.12.3，原有远程测试暴露 `cleanup_socket` 仅在 Python 3.13+ 可用；
服务端按解释器版本传入此参数，保持项目 Python 3.11+ 的公开兼容性与原有 socket
所有权清理语义。原测试 `test_remote_full_api_and_health` 已复现此失败。

## 验证结果（2026-10-27）

- 容器内完整 `python3 -m pytest -q --tb=short`：**225 passed**。
- `compileall`、语音示例 `--help`：通过。
- `ubt_robot 2.0.16` 实际导入、初始化、注册和移除回调：通过，未启动连接。
- 修复了断连通知先于启动请求执行时仍发送请求的竞态；新增测试先复现失败再通过。
- 原有 `test_remote_rejects_invalid_stream_envelopes` 的假服务遗漏关闭 writer，导致
  Python 3.12 的 `Server.wait_closed()` 不退出；限定时间复现后补齐连接清理，六项均通过。
- `robot_core_ubuntu24` 保留运行，项目和依赖可复用。未执行真实机器人播报。
