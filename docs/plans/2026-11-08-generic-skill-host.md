# 通用 Agent Skill 宿主改造计划

本文件记录通用资源/脚本宿主的实现；用户随后明确酒店技能应独立可选，
包粒度与选择入口以 [独立技能计划](2026-11-08-selectable-agent-skills.md) 为准，宿主机制继续复用。

> **For agentic workers:** 使用 subagent-driven-development 分离资源宿主、完整业务包和酒店入口，按 TDD 实现并独立审查。用户已批准本设计；当前工作区修改，不自动提交。

**Goal:** 将阶段提示词实现改造为模型选择 Skill、按需读取资料、执行注册脚本与业务工具的通用 Agent。

**Architecture:** LangGraph 仅管理消息、技能激活、工具和检查点；业务事实通过 guidance 提供但不指定 required_skill。完整 hotel-frontdesk-escort 技能承载接待到带房方法。HotelWorkflow、机器人租约、权威房间、客人确认、幂等和停止确认保持执行约束。

**Tech Stack:** 现有 Python / LangGraph / PyYAML / SQLite，不新增根依赖，验证仅在 robot_core_ubuntu24。

## 已确认边界

- 参照用户本地 ubtech-reimbursement-files：一个完整业务包包含 SKILL.md、references 和有实际用途的 scripts。
- 技能选择由模型的 load_skill 调用完成，启动只提供名称描述；不由酒店阶段硬编码加载正文。
- 默认机器人模式无 Skill 注册时保留原行为；酒店模式提供业务包和受控宿主能力。
- read_skill_resource 只读取指定 Skill 内受控相对路径文本，拒绝路径逃逸、二进制/超大文本和符号链接逃逸。
- run_skill_script 只运行应用注册的可信入口；不提供任意 shell / eval 工具，不将读取文件等同于授权执行。
- 注册脚本实际运行其 Python async run 函数，经受控 ToolSet 调用酒店服务。脚本内容指纹与发布的入口匹配；自定义目录改文档允许，替换脚本需重新注册。
- 脚本通过注册绑定、边界、期限和结构化结果控制，不能宣称为不可信 Python 的安全沙箱。
- 现有 /consent、/start、/arrived 是可信应用事件；模型和脚本不获得直接写确认的接口。
- demo 仍是固定规则的通路演示，真实模型支持目录→加载→参考资料→脚本→结果循环，但没有配置时不宣称真实模型效果验证。
- 酒店新图检查点使用独立宿主版本命名，保留业务/任务数据库，不重放旧流程中的未执行动作。

## 共享接口

### SkillRegistry（skill_catalog.py）

- 保留 catalog()/load()，catalog 仅元信息。
- resources(name) -> list[str]：列出 references/scripts/assets 的受控相对路径，不加载正文。
- read_resource(name,path) -> dict：name/path/content/fingerprint，受控 UTF-8 文本，64 KiB 上限。
- load() 可以保持四个原字段，宿主返回时附加 resources 清单。

### SkillScriptRunner（skill_scripts.py）

- ScriptContext(tools: ToolSet, operation_id: str)，仅提供受控工具接口。
- ApprovedScript(skill: str,path: str,fingerprint: str,handler: async Callable[[dict,ScriptContext],Observation])。
- SkillScriptRunner(registry,tools,bindings,*,timeout_s=5.0)。
- async invoke(name,path,args,*,operation_id) -> Observation，拒绝未注册入口、内容变更、异常/超时。
- Observation 可选 execution 字段记录 skill/path/fingerprint，原业务结果保留 result 中。

### 完整酒店包

- hotel-frontdesk-escort/SKILL.md。
- references/hotel-information.md：只写已有模拟事实，未知早餐/价格不能编造。
- references/reception-rules.md：信息补齐、确认与沟通。
- references/escort-request-schema.md：脚本入参、结果和状态含义。
- scripts/query_reception.py：async run(arguments,context) 调用 hotel_confirm_reception。
- scripts/request_escort.py：async run(arguments,context)，action=assign|start，调用对应酒店工具，拒绝多余字段。
- hotel/script_bindings.py：build_hotel_script_runner(registry,tools,*,timeout_s=5.0)，将发布脚本注册为绑定；执行发布版本的代码，custom Skill 脚本必须匹配注册指纹。

### 通用图

- AgentContext 增加 scripts: SkillScriptRunner|None，不自动选择 Skill。
- 初始只显示宿主工具，模型 load_skill 后开放业务工具；同一模型批次不能借 load_skill 直接跳到物理执行。
- 宿主工具：load_skill(name)、read_skill_resource(name,path)、run_skill_script(name,path,args)。
- 加载前返回目录元信息，加载后持久保存正文/版本；技能版本变化时不静默回放旧动作。
- 资源读取与脚本执行形成 ToolMessage，支持模型进一步思考，不额外写业务阶段。
- 业务 guidance 仅返回事实，不包含技能选择或对话流程。

## Task 1: 资源与注册脚本宿主

- [x] 先写资源清单/引用读取、遍历逃逸、符号链接、超限、未注册/篡改脚本、异常和期限测试。
- [x] 实现资源 API 和真实注册函数调用，保持格式验证与通用性。
- [x] 保留原无 Skill 的 RobotTools 使用方式。

## Task 2: 完整业务 Skill

- [x] 先写完整资源契约与脚本实际调用失败测试。
- [x] 合并三阶段正文，增加有实际用途的资料和两份脚本，注册发布版本。
- [x] 删除旧三个业务阶段 Skill，更新描述/资源路径与打包规则。
- [x] 用缺失信息、已有分配、未确认、导航完成、停止未知等场景检查指令与代码分工。

## Task 3: 通用图与入口集成

- [x] 先写无自动阶段选择、按需加载、引用读取、实际脚本调用和防跳过加载测试。
- [x] 移除 required_skill 路由，保存技能正文/资源使用记录而不让模型伪造。
- [x] 酒店 demo 显式演示宿主调用序列并标注局限，真实模型使用同一工具循环。
- [x] 保留可信客人事件、多客人会话、业务状态约束和恢复安全，隔离旧图检查点。

## Task 4: 验收与文档

- [x] 原有业务可靠性测试继续通过；新增真实客户端 loopback 测试展示实际选择/读取/执行请求链。
- [x] 容器 Agent/runtime 回归、CLI、标准技能校验、安装 wheel 资源与脚本验收、pip check。
- [x] 独立检查加载绕过、资源/脚本边界、状态与恢复；修复后复验。
- [x] 更新 HOTEL.md、Agent README、根 README 与本计划，明确未验证真实模型行为或硬件。

基线：Agent 97 passed，runtime 497 passed。沿用当前未提交的代码，不修改用户本地报销 Skill。

## 验收结果与限制

- Agent：145 passed；原容器 runtime 完整重跑：497 passed。compileall / pip check 通过。
- 单个完整酒店 Skill、3 份引用、2 份真实 Python 入口；通过标准 quick_validate。
- 独立审查确认模型选择而非阶段指定，修复旧 pending 使用新脚本的版本漏洞，覆盖变更源码和缺少指纹两种恢复拒绝。
- 失败后执行保持停止，最多一次没有工具的 Skill 指导回复；loopback 检查该请求不含 tools，返回工具请求也不会执行。
- 已完成/已取消的服务可以记录人工帮助而不改写原结果、占用或移动。
- robot --skills-dir 同样接入通用目录/加载/资料流程，无目录时原行为保留，也不自动授权脚本。
- wheel 构建发现历史 build/lib 的三份旧生成副本，仅删除已知生成文件与空目录后重建；最终 wheel 精确包含一个完整包。
- 最终 wheel 安装到独立临时目录，实际运行接待→同意→注册脚本分派→护送→客人到达→回位，结果 completed / available。
- 本地 HTTP 测试使用真实模型 SDK 和脚本响应验证宿主协议链路，不是实际部署模型的推理能力验收。
- 当前容器 OPENAI_API_KEY / ROBOT_AGENT_MODEL 未配置；未调用真实模型、PMS 或硬件，未修改本地 ubtech-reimbursement-files。
- 一次原 runtime 全套在已有共享状态源并发测试处持续等待；停止后该测试单独通过，完整重跑 497 passed。
  没有修改内核，也没有宣称已定位该次等待的根因。
