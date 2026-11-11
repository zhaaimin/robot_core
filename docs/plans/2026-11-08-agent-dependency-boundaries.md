# Agent 依赖方向与共享基础实施计划

> **For agentic workers:** 使用 executing-plans 执行用户已确认的顺序；用 subagent-driven-development / dispatching-parallel-agents 将无共享写入的版本校验任务单独实现。当前工作树包含此前未提交 Agent，按用户要求继续原地修改，不提交或迁出工作区。

**Goal:** 解除 app 与场景的双向依赖，通过描述符选择场景，统一版本校验，并将酒店错误归属到领域包。

**Architecture:** app 只处理 CLI、模型/原生连接所有权和场景分发；config 保存公共配置和稳定身份。shell 提供输入读取、单轮图调用、结果基础信封、错误状态与公共交互循环；场景提供命令处理、恢复错误策略和业务结果。registry 使用不可变描述符与懒加载工厂，不在包 __init__ 中装配。HotelWorkflowError 独立迁入酒店领域，保留错误码，不自动迁移旧脚本版本或重放动作。

**Tech Stack:** 已有 Python / LangGraph / SQLite / pytest，不加依赖。所有验证在 robot_core_ubuntu24 的现有虚拟环境中执行。

## 模块与冻结契约

```text
robot_agent/app.py → config.py + scenarios/registry.py
robot_agent/config.py → scenarios/registry.py（registry 的 config 引用仅限 TYPE_CHECKING）
scenarios/registry.py → 懒加载的场景入口与 demo 工厂
scenarios/robot.py、scenarios/hotel.py → config.py + shell.py + graph.py + 各自工具
graph.py、skill_selection.py → skill_versions.py
domains/hotel/*、三个酒店脚本 → domains/hotel/errors.py
```

- 新增：config.py、shell.py、scenarios/registry.py、scenarios/robot.py、skill_versions.py、domains/hotel/errors.py。
- AppConfig.task_database / runtime_id 的已有值保持不变，场景名称、后端、数据库文件、demo 工厂和运行入口从描述符获取。
- 单次与交互的 stdout JSON 结构、酒店 /guest /consent /start /arrived /status /wait /cancel、纯 Skill 选择无业务副作用保持不变。
- robot 场景仍仅恢复交互 Skill 命令错误，普通致命错误仍结束；hotel 仍保留原有错误恢复策略。
- runtime 装配留在场景，保留 hotel 的 navigation 权限、robot 的音频/导航及 CC 生命周期，不抽成扩大默认权限的统一工厂。
- 不再从 app 导入共享 shell 函数；更新测试/补丁路径，不加旧私有函数兼容包装。
- graph 的 Skill 工具派发与 runtime InvalidRequest 本轮不改；只将重复正文/指纹校验提到一处。
- 酒店错误码保持 HOTEL_WORKFLOW_REJECTED。三个脚本改 import 后指纹有意更新，旧 pending 脚本必须拒绝恢复，不刷新旧版本、不自动重试。

## Task 1: 共享 shell、配置与场景分发

- [x] 基线：186 passed in 4.02s。
- [x] 新入口/描述符/稳定身份/共享循环回归测试先 RED。
- [x] 提取 AppConfig、invoke_turn、AsyncPromptReader、result_envelope、error_state 与 run_shell。
- [x] 将机器人场景及 open_runtime 移到 scenarios/robot.py；酒店依赖 config/shell，不再依赖 app。
- [x] registry 描述符提供场景名称、允许后端、模拟任务库文件名、身份标签、demo 工厂和异步运行入口；app 仅查表。
- [x] 更新现有测试导入和 monkeypatch 路径，验证两种场景、输入读取、关闭顺序、错误恢复与权限。

## Task 2: 统一版本校验

- [x] 新建共享校验 helper 及缺 registry、隐藏技能、正文/版本不一致的测试，先 RED。
- [x] graph 和 skill_selection 使用同一 helper，保留 /skills 在 pending 时仍可用的现有行为。
- [x] 容器目标测试并做规格与质量审查。

## Task 3: 酒店错误与脚本版本迁移

- [x] 酒店领域错误入口及旧 pending 指纹拒绝恢复测试先 RED。
- [x] 新建 domains/hotel/errors.py，移除通用错误模块中的酒店定义，统一领域、测试和三个脚本的导入。
- [x] 保留业务错误码与调用结果，验证新脚本可运行、旧 pending 动作不执行；记录新旧指纹。

## Task 4: 文档、发布包与最终验收

- [x] 更新根 README 的入口/状态/模块树/范围，以及 agent/README.md、HOTEL.md 的模块边界和脚本版本说明。
- [x] Agent 完整套件、runtime 有界串行完整套件、compileall 和 pip check。
- [x] 归档旧 build 缓存后直接打包，独立安装验证新模块、三份 Skill 和同名纯文档/替换脚本。
- [x] 新安装包完整接待/交接/护送/到达/回位、单技能过滤与机器人模式冒烟。
- [x] 检查依赖方向与旧导入残留，做独立规格/质量审查，修复发现的问题并记录最终结果。

## 验证命令

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -c agent/pyproject.toml agent/tests -q --tb=short'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && timeout --kill-after=5s 45s python3 -m pytest -q -o faulthandler_timeout=25'
```

## 实际结果

- Task 1 的新边界测试：6 failed；补充渲染错误不进入业务恢复的测试另先失败。实现后 app/hotel/shell/scenario 定向 75 passed。
- Task 2 新增版本校验测试：RED 12 failed，GREEN 12 passed。中间态旧 app.invoke_turn 导入失败已随 shell 导入迁移修正；Task 1/2 合并完整套件 205 passed in 5.91s。
- Task 3 错误入口先 2 failed；版本迁移前 1 failed / 1 passed；迁移后新增 4 passed，酒店与脚本定向回归 49 passed in 1.05s。
- 最终 Agent 完整套件：209 passed in 4.37s。
- runtime 使用 45 秒上限独立串行完整验证：497 passed in 10.69s；compileall 与 pip check 通过。
- 独立规格审查与代码质量审查均通过 Task 1/2，并增量通过 Task 3；没有阻塞问题。
- 当前项目直接构建 wheel 并在容器临时目录独立安装通过，安装来源确认在 target 目录；场景可独立导入且不加载 CLI。
- 安装包含 3 份技能正文、7 份参考资料、3 份新指纹脚本；完整旅程最终 completed / arrival_confirmed=true / robot_status=available。
- 安装包单技能过滤、同名纯文档/替换脚本/旧发布脚本副本的控制命令和机器人播报冒烟均通过。
- 扫描 30 个通用/业务源码模块：CLI 反向依赖 0，app/config/shell 的酒店名称常量 0，两份重复版本 helper 均已移除。
- 包内脚本哈希与最终源码完全匹配；runtime 源码和 Skill 正文/参考资料共 52 个文件与迁移前 SHA-256 保持一致。
- 本轮产物与验收记录位于容器 `/tmp/agent-dependency-boundaries-0le2ermj/`，旧 build 缓存保存在其 previous-build/。
- 未提交工作树；未增加项目依赖，未连接真实模型、PMS 或导航硬件。

### 发布脚本指纹迁移

| 脚本 | 迁移前 SHA-256 | 迁移后 SHA-256 |
| --- | --- | --- |
| query_reception.py | `5edd5e444e404b54ead5f1967fafefef16596243e1083e0aa25c23853f6df087` | `0e61ec24ed31c3c5b5aa2ecf1b14b0dc8c6c9776b08fdc1edef1bbf82fe67e6a` |
| assign_butler.py | `40f020c6b0f53c148467af7af34f627ba31cb4a72acc943354cdbff07f31aac0` | `6b8b59925c33d491dd8912bbb5b429a7c8d1b3ae52462a5019b2c2cfc2c4cbaa` |
| start_escort.py | `26bb74428ba0f67b8c2f8c9410dc7d56debb2e0d935d77fd20eaf8fcf1862c57` | `50f8fe8fdb445cae4f4ed98d2b012b1498967931136b721367521ac7c8a6fa90` |

旧指纹仅用于历史版本回归，不是可批准来源；不新增兼容入口或自动版本迁移。
