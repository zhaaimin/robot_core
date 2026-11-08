# CC API 语音示例分工

## 设计

用户选择拆分最小示例、回调示例与线程实验，并在前文明确关注已有 demo 的调用兼容。
本次新增两份可独立运行的脚本，现有 `examples/cc_api_speech_demo.py` 保持内容不变：

| 入口 | 学习目标 |
| --- | --- |
| `examples/cc_api_speech_minimal_demo.py` | `submit_and_wait()` 返回终态，无用户任务监听或结果 Future |
| `examples/cc_api_speech_callback_demo.py` | 默认调用方执行的单个监听，提交前注册、提交后可继续业务、等待回调并查询结果 |
| `examples/cc_api_speech_demo.py` | 既有双监听 caller/dedicated 与同步阻塞实验，保留参数、返回值、输出和异常行为 |

相比将原入口重写为分发器，新增文件保留了旧入口及其补丁位置；相比一个脚本使用模式
开关，独立脚本让用户能直接阅读并复制对应场景。每份脚本显式展示连接参数、平台与
运行时的生命周期，可直接执行或从项目根目录导入 `speak(api, message)`；原生 SDK
只在 `main()` 中导入，模块加载与 `--help` 不建立连接。

两份新增脚本的 `speak(api, message) -> Task` 接受应用已连接的 API；都保留 audio
技能分组、audio 权限及应用拥有 SDK 连接的约定。成功、执行失败、准入失败均返回
终态 Task；启动和清理错误继续抛出，先关闭运行时再关闭平台。最小示例只等待结果；
回调示例使用 `submit_task()`、`listener.wait()`、`get_task_status()`，单次注册三个
异步回调，监听异常记录后向调用方抛出，业务失败仍由 Task 表达，不依赖 Future。

CLI 保持常见的 `--api-id`、`--address`、`--message` 参数与 `CC_API_TOKEN`，连接和
断开放在事件循环之外。连接未成功、业务失败、业务协程异常均完成应用拥有连接的
释放。两份脚本显式展示相同连接代码，便于独立复制与运行，不增加示例导入路径配置。

本次不修改 SDK、内核、平台实现、现有线程实验或已有 API 行为。`examples/README.md`
按学习顺序列出三个入口；项目 README 与流程文档采用最小示例作为首次入口，明确
回调与线程实验各自路径。

## 实施计划

- [x] 新增免 fixture 的 `tests/test_cc_api_demos.py` 并加入 `.verify/sweep.py`。
  使用假 CC API 和真实 Runtime 验证两份新示例：成功需等 finished，厂商拒绝与参数
  失败返回 Task，回调按序交付且完成后再退出，监听异常抛出，启动失败释放资源。
  同步 CLI 测试验证连接配置、成功/失败释放连接、连接失败、`--help` 和入口用途。
- [x] 容器运行 `python3 -m pytest -q tests/test_cc_api_demos.py`，确认因入口缺失失败；
  新增两个独立脚本，再运行相同命令确认通过。
- [x] 新增示例索引，更新 README 和流程/架构文档中入口说明，保留原线程实验命令。
- [x] 比较本次任务开始和结束时原 demo 的 SHA-256，确认内容完全未改。
- [x] 容器运行 CC/SDK/监听相关回归、三个入口 `--help` 和完整套件，检查 diff。
  若完整连跑再遇已知线程分组旧用例阻塞，分别执行其余用例和该用例并如实记录。

## 验证记录

开始时工作区含上一项 submit_and_wait 及更早用户的未提交改动，本次在其上新增示例。
原 demo 的任务开始 SHA-256 另在会话中记录，最终比较确认。

- 改动前 CC 平台、CC 技能与提交等待相关回归：65 passed。
- 新增测试先因两个脚本缺失而 10 failed，新增脚本后 10 passed；只读审查复跑也为
  10 passed，未发现实质问题。
- 三个入口直接执行 `--help` 均成功；从项目根目录导入两份新示例的 `speak` 成功。
- 原 demo 开始与结束的 SHA-256 均为
  `fcefc1130d294fed6adff428237bddf714811865aa98af64ec4db8d462b11a81`。
- 容器完整套件：497 passed in 11.38s，本次完整连跑未出现已知旧用例的偶发阻塞。
- `git diff --check` 通过。未连接真实机器人，未新增依赖，未自动提交版本。
