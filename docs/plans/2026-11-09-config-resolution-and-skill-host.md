# 配置解析与 SkillHost 边界实施计划

> **For agentic workers:** 用户已确认先统一路径并消除 config 反查，再抽 SkillHost。使用 writing-plans 记录契约、executing-plans / subagent-driven-development 执行；当前含未提交项目，继续原地工作，不提交。配置任务与图任务分文件实现，最终统一审查和容器验收。

**Goal:** 让 AppConfig 成为不依赖注册表的被动快照，并将 Agent Skills 协议实现从通用图中抽出。

**Architecture:** 配置只保存装配层解析好的任务库、检查点库、可选业务库和 runtime_id。configuration.py 统一路径/身份计算及 rebuild_config，场景描述符保存现有文件名策略。SkillHost 管理三项宿主工具、正文指导、参数验证、脚本指纹冻结/恢复验证与宿主状态更新；graph 保留模型调用、动作批次编排、消息/观察和路由。

**Tech Stack:** 当前 Python / LangGraph / SQLite / pytest，不加依赖。生产入口仍只接受已配置的 OpenAI 兼容模型；测试使用既有测试替身或本地 HTTP 响应。全部验证在 robot_core_ubuntu24。

## 冻结契约

- robot 任务库 simulator-tasks.sqlite3，检查点 checkpoints.sqlite3；hotel 任务库 hotel-simulator-tasks.sqlite3，检查点 hotel-selectable-skills-checkpoints.sqlite3，业务库 hotel.sqlite3。名字不变，不复用 legacy hotel-checkpoints.sqlite3。
- CC 文件名哈希与 runtime_id 原算法逐字节保持：JSON 默认转义、separators、None 位置、身份列表顺序及酒店标签不变。
- AppConfig 不导入注册表、Agent 错误、runtime、模型或业务包；数据库/身份是普通不可变字段。
- 修改场景、后端、连接、data_dir 等身份输入使用 rebuild_config；只改非身份字段可继续 dataclasses.replace。携带失配解析结果启动时须明确拒绝，不静默使用旧任务库/旧身份。
- 三个 Skill 继续独立按需加载；仅允许执行批次开始前已加载的技能。保持同批 load→resource/script/business 均不能获得刚加载的能力。
- 同一已加载技能的既有能力不因同批再次 load 而失效；不能用新选择覆盖无关技能的未加载状态。
- pending 的 operation_id、冻结时机、旧/缺失/变化脚本指纹拒绝策略、未知停止及不重放物理动作保持不变。
- 通用 SkillScriptRunner 的脚本批准边界、超时、净化和结果契约不变；酒店错误类、三个发布脚本和源码指纹不变。
- 宿主参数错误可定义 Agent 专属参数异常，但保留 INVALID_REQUEST 错误码；不把格式错误改成未授权或未加载错误。
- 不保留 graph 旧私有工具 helper 兼容入口，不在包 __init__ 预装配，不引入第四个宿主工具或新的业务能力。

## Task 1: 配置与存储解析

Files: config.py、configuration.py（新增）、app.py、scenarios/registry.py、scenarios/robot.py、scenarios/hotel.py；测试 test_configuration.py（新增）、test_scenarios.py、test_hotel_app.py。

- [x] 当前 Agent 基线 209 passed in 4.62s。
- [x] 记录原算法的 robot/hotel/CC 固定输入及固定 hex，保存源码和脚本校验基线。
- [x] 先测试 config 单独导入不加载注册表/runtime、快照不反查全局表、路径集中、重建重算和启动拒绝不一致结果，观察 RED。
- [x] 配置改普通派生字段，集中实现 resolve_storage / rebuild_config / validate_config，parse_args 一次注入，启动边界做一致性校验。
- [x] 描述符补齐 checkpoint_database / business_database；场景只消费已解析路径，不再拼接库文件名。
- [x] 更新身份字段替换测试为明确 rebuild_config，保留非身份字段 replace 行为；容器目标回归。

## Task 2: 重构前特征测试

Files: tests/test_generic_skill_host.py；必要时新增 test_skill_host.py。

- [x] 将同轮 load→script 的临时验证变成正式回归，并强化已存在的同轮 load→business 测试。
- [x] 覆盖同轮 load→resource、已加载后再 load→script、跨技能未加载、批次失败后跳过动作、冻结脚本指纹和恢复拒绝。
- [x] 特征测试应在现有实现上先通过；新模块接口测试在未实现时 RED。

## Task 3: 提取 SkillHost

Files: skill_host.py（新增）、graph.py、errors.py；只在需要共享 UI load 行为时改 skill_selection.py，保持目录命令无模型/业务副作用。

- [x] 将 schema、目录/指导、参数校验、加载/资源/脚本 handler、待执行指纹冻结和恢复校验移到 SkillHost。
- [x] 宿主通过执行批次快照持有 before_loaded，更新独立副本并返回状态 delta；不直接改传入 graph state，不依赖 AgentContext / LangGraph runtime。
- [x] graph 向宿主获取模型工具与提示指导，并委派宿主动作；图仍统一控制错误停止、Skipped 结果、ToolMessage 和稳定 operation_id。
- [x] 保持不存在 registry 时的普通机器人工具路径，业务工具与宿主保留重名拒绝，以及无脚本执行器时的工具可见性。
- [x] 新宿主接口目标测试、既有图/模型/选择/脚本和酒店恢复回归。

## Task 4: 文档与最终验收

- [x] 更新 root README API/状态/模块/范围、agent/README.md、HOTEL.md；说明新依赖方向、rebuild_config、普通参数错误语义和数据库文件名不变。
- [x] 先规格审查，再质量审查；处理所有阻塞发现。
- [x] Agent 完整套件，runtime 独立有界串行完整套件，compileall 和 pip check。
- [x] 归档旧 build 缓存后直接构建并独立安装 wheel，验证新增模块和三个 Skill 包资源；不恢复生产 demo 模式。
- [x] 从安装包验证本地 HTTP 模型协议、模拟完整酒店旅程、纯文档/替换脚本/单 Skill 过滤、旧 pending 拒绝和当前 pending 恢复。
- [x] 对比 runtime 与酒店脚本源码基线，回读最终文档与实际结果；保留容器与未提交工作。

## 验证命令

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -c agent/pyproject.toml agent/tests -q --tb=short'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && timeout --kill-after=5s 45s python3 -m pytest -q -o faulthandler_timeout=25'
```

## 实际结果

- 配置初始新契约 RED 29 failed / 5 passed；补充直接场景误用和酒店业务库缺失边界先 4 failed 后通过。配置/app/两种场景定向 99 passed in 2.60s。
- 新同批加载/脚本/资源/跳过与重加载特征测试先在原图通过；test_generic_skill_host.py 当时 8 passed。
- 新宿主接口测试先 7 failed，再 7 passed；图/宿主/版本/显式选择/本地模型协议/酒店 pending 组合 48 passed in 0.83s。
- 完整 Agent：251 passed in 4.63s。
- graph.py 从 297 行缩减为 163 行；三项宿主工具派发、schema、指纹与指导均归属 skill_host.py，显式选择共用加载 handler。
- 规格审查通过，已按基线逐项对照导入、提示/schema、action/order、operation_id 和指纹拒绝语义，未发现阻塞项。
- 基线位于容器 `/tmp/config-skillhost-baseline-s6ki0_ed/baseline.json`，包含本轮开始前全部 Agent Python 源码、保留文件 SHA-256 与四组固定运行身份。
- 独立质量审查通过，无阻塞或实质性 minor；规格和质量均直接对照了原始源码快照。
- runtime 有 45 秒上限的独立串行完整回归：497 passed in 10.78s。compileall 与 pip check 通过。
- 当前项目直接构建 wheel 并独立安装，确认实际导入来自 target 目录，安装包全套测试 251 passed in 6.37s。
- 独立进程验证 config 不加载注册表/runtime，SkillHost 不加载 graph/LangGraph；四组固定身份值与原始基线完全一致。
- 安装包通过真实 ChatOpenAI 客户端连接本地 HTTP 服务的 14 次受控响应，完整模拟旅程最终 completed / arrival_confirmed=true / robot_status=available；纯选择/查询/取消不触发模型请求。
- 单技能过滤、纯文档与替换脚本目录、实际 HTTP 工具调用驱动的机器人播报均通过；安装包含 3 份正文、7 份参考资料、3 份脚本，没有规则 demo 或测试替身混入。
- 安装包内 33 个 Python 文件与最终源码逐字节一致；runtime 与已发布酒店脚本/资料共 55 个文件哈希不变，models.py 实现也与基线一致。
- 最后强化 freeze_action 不修改传入动作的独立断言，host 7 项单元测试再次通过（0.08s）；没有再次修改发布实现。
- 固定输入和四个原始 runtime_id hex 已固化在 test_configuration.py::test_original_storage_identity_bytes_are_preserved，启动资源前的一致性校验有独立故障断言。
- 本轮 wheel、安装副本和验收 JSON 位于容器 `/tmp/config-skillhost-release-uoqpxe97/`；旧 build 缓存保存在其 previous-build/。
- 未提交工作树，未增加项目依赖，未连接外部模型服务、真实 PMS 或机器人硬件；容器保留。
