# 独立酒店技能与用户按需选择实施计划

> **For agentic workers:** 使用 subagent-driven-development 分离技能包、通用选择命令和酒店演示。用户已明确要求三个独立技能；保留通用宿主与执行约束，按 TDD 在容器验收。

**Goal:** 拆分合并的酒店 Skill，让用户查看目录、显式选择并按需加载独立技能，技能选择不再绑定酒店阶段或整包。

**Architecture:** 保留模型 load_skill / read_skill_resource / run_skill_script。默认目录提供三个互不自动加载的能力包；用户 /skill 通过宿主验证后注入正文与审计记录，不调用模型或业务接口。--skill 过滤可见目录；HotelWorkflow 的房间、确认、租约、幂等和停止规则不变。

**Tech Stack:** 已有 LangGraph/PyYAML/SQLite，不新增根或 Agent 依赖。所有验证在 robot_core_ubuntu24。

## 技能边界

- frontdesk-reception：酒店信息、核验回执、接待结果和可选带路说明；不分派或启动机器人。
- guest-handoff：依据已有权威接待记录、客人同意分派管家，说明会合点和准备方式；不启动导航。
- escort-assistance：在机器人已分派且客人准备好的前提下启动/观察护送、到达确认、取消与人工协助。
- 每个包独立 SKILL.md、references、scripts，可由既有业务状态接入，不要求先加载另一个 Skill。
- 技能之间只共享后端业务事实，不静默加载其他技能。缺少业务前提时说明缺失或提示用户选择其他能力。

## 冻结接口

### 包与脚本

- frontdesk-reception：references/hotel-information.md、reception-rules.md、reception-request-schema.md；scripts/query_reception.py。
- guest-handoff：references/handoff-rules.md、handoff-request-schema.md；scripts/assign_butler.py（args 必须为 {}）。
- escort-assistance：references/escort-rules.md、escort-request-schema.md；scripts/start_escort.py（args 必须为 {}）。
- 注册工厂只对当前可见目录中的这三个发布入口建立受信绑定；省略某包不阻止其他包使用。

### 通用用户入口

- SkillRegistry.select(names) 返回保持资源边界的注册表子集，未知名称报配置错误，未选择的 Skill 从目录隐藏。
- AppConfig.enabled_skills: tuple[str,...]|None，--skill NAME 可重复；--skills-dir 仍可覆盖来源。
- async handle_skill_command(graph,registry,*,thread_id,runtime_id,prompt) -> dict|None，放 skill_selection.py。
- /skills：仅展示可见名称/描述、已加载和当前技能，不读正文、不调用模型、不变更业务。
- /skill NAME：验证名称、运行身份、无 pending、已有正文版本；记录与模型 load_skill 等价的正文、资源和 ToolMessage 审计，aupdate_state(as_node='decide',pending=[]) 后结束。
- 选择操作不记录 consent/ready/arrival，不分派机器人，不启动移动。pending 时拒绝选择，保留取消/查询入口。

### 酒店入口与 demo

- --scenario hotel 默认目录三项，不自动选中任何一项。
- HotelDemoModel 从历史中最后一次成功 load_skill 取得当前选择，不能用命令/阶段自动切换技能。
- 没有选择或命令超出当前技能范围时，只提示查看/选择技能，不执行业务。
- 确认入口仍是 /consent、/start、/arrived；技能选择不能代替真实确认。
- 酒店新图使用 hotel-selectable-skills-checkpoints.sqlite3，保留旧图文件及业务/任务数据库，不自动迁移旧激活记录。

## Task 1: 拆包与可信脚本绑定

- [x] 先测试三个目录、各自独立资源和真实脚本参数/调用。
- [x] 拆分完整包，删除合并包，按目录子集注册三份脚本。
- [x] 验证同一包可单独加载，不能读取未加载/不可见包的资源或脚本。

## Task 2: 用户选择与目录过滤

- [x] 为 select、/skills、/skill 的无业务副作用、未知名称、pending 与版本写 RED 测试。
- [x] 实现通用选择命令，机器人/酒店入口都接入。
- [x] --skill 在启动创建 runtime 之前验证，缺失目录/未知技能不能默默回退。

## Task 3: 酒店按需加载与模型协议

- [x] 重写 demo 与协议测试，展示显式选择后才处理对应业务，不自动加载整个包或其他技能。
- [x] 保留真正模型工具激活入口和通用宿主的指纹/恢复边界。
- [x] 验证三技能切换、只启用一个技能、多客人隔离、已有确认/占用和完整旅程。

## Task 4: 打包、说明与独立审查

- [x] 更新目录/命令/三技能责任与真实模型验证范围，历史合并方案标记已替代。
- [x] 容器 Agent/runtime、标准 Skill 校验、wheel 目录/资源/脚本和完整交互验收。
- [x] 独立审查用户选择是否真的控制加载、没有阶段绑定、业务/资源/恢复约束仍在。
- [x] 最后记录实际通过数、已观察的限制及未进行的真实模型/硬件验收。

基线：Agent 145 passed，runtime 497 passed。保留当前未提交改动，不改用户本地报销技能或核心 runtime。

## 实际结果与验证范围

- Agent 全套：176 passed；compileall 与 pip check 通过。
- 三个 Skill 分别通过标准 quick_validate；最终 wheel 精确包含 3 份 SKILL.md、7 份参考资料和 3 份注册脚本，无合并包残留。
- 从 wheel 独立安装后验证 /skills → 选择接待 → 核验 → 选择交接 → 同意/分派 → 选择护送 → 出发/到达/回位，最终 completed / available。
- 单技能启动目录仅显示 frontdesk-reception，隐藏项选择被拒绝；纯目录/选择命令产生的 hotel_services 行数与 runtime tasks 均为 0。
- 独立审查发现显示层调用 refresh 的副作用，已修复为目录/选择输出 hotel:null；选择错误保留原技能与 pending。
- 机器人交互入口的选择错误不会再退出并关闭 runtime，普通输入原有致命错误行为保持。
- 显示给模型的 load_skill 参数枚举只含可见技能；demo 对隐藏能力明确说明未启用，不提示无效选择。
- 独立源码审查及早期针对性测试已完成；辅助审查服务的最后全套重跑因额度中断，最终完整回归由主代理执行。
- 原 runtime 全套再次在既有共享状态源并发用例持续等待，已保留诊断信息并终止该测试进程；
  独立、有 45 秒上限的串行完整重跑结果为 497 passed in 10.53s。没有修改内核，也不声称已定位等待根因。
- 本轮未调用真实模型服务、PMS、导航硬件、随行感知或电梯；模型协议验证使用本地 HTTP 响应。
