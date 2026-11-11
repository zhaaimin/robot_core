# 酒店前台接待与机器人带房实施计划

本文件记录初版阶段 SOP 实现与验收；该技能组织方式现已由
[通用 Skill 宿主改造](2026-11-08-generic-skill-host.md) 替代。业务状态和机器人执行约束继续复用。

> **For agentic workers:** 使用 subagent-driven-development 分离 Skill 加载、酒店业务与 CLI 工作；先写失败测试，完成后独立审查，全部验证在 robot_core_ubuntu24。

**Goal:** 将现有 Agent 扩展为可运行的酒店接待—交接—带房验证应用，增加按需加载的 Agent Skills 和可恢复的业务状态约束。

**Architecture:** 保留通用 LangGraph 工具循环与 runtime。Agent Context 可选注入 SkillRegistry 与业务上下文；酒店业务状态使用独立 SQLite 文件，不扩展内核 TaskState。模拟 PMS 返回已核验入住凭据，房间到目的地映射属于酒店适配器；机器人通过公开 SDK 执行导航任务。关键确认来自 CLI 控制事件，不来自模型参数。

**Tech Stack:** 现有 LangGraph / SQLite / RobotRuntime；Agent 子项目显式声明已安装的 PyYAML，不增加根依赖。

## 范围与默认值

- 用户已批准三个 Agent Skills 与接待—交接—护送产品流程，直接在当前 agent/ 实现。
- 首版使用同楼层、单机器人模拟器；真实 PMS、导航、随行感知和乘梯不虚构实现。
- 默认 robot 场景保持原行为；新增 `--scenario hotel`，支持 demo 与现有真实模型接口。
- 酒店 CLI 拒绝 cc 后端，直到有真实导航适配；真实 CC 语音入口原样保留。
- 模拟入住凭据 `DEMO-001` 由模拟酒店系统确认到房间 0108，映射到受控目的地，不接受模型随意设置房号或坐标。
- 客人的 `/consent`、`/start`、`/arrived` 是独立的应用事件。模型只能读取确认结果，不能制造这些事件。
- 业务阶段包含接待、等待带路同意、等待机器人/客人、护送中、等待客人到达确认、完成、取消、人工协助和待核对。
- 导航成功只进入等待客人确认；客人确认后才完成带房，机器人回位与客人服务分别记录。
- 同一机器人不能同时分给两个客人；状态/停止不明不释放资源，不自动重执行旧动作。
- 物理提交使用服务级稳定幂等键；保存意图后才提交，已知 task_id 优先查询，重启不直接重放物理动作。

## 共享接口

### SkillRegistry（agent/skill_catalog.py）

- `SkillRegistry.from_directory(Path)` / `SkillRegistry.bundled()`。
- `.catalog() -> list[dict[str,str]]`：只含 name / description。
- `.load(name) -> dict[str,str]`：name / description / instructions / fingerprint。
- 默认文件随 Python 包安装在 `robot_agent/skills/<name>/SKILL.md`；只支持受控的文档加载，不执行脚本。
- 校验 UTF-8、YAML 前言、文件夹与名称一致、重复名、边界与长度；禁止越出根目录或任意路径读取。

### HotelWorkflow（agent/hotel/service.py）

- 构造：`HotelWorkflow(database: str, runtime: RobotRuntime | RemoteRuntime, *, hotel=None, task_timeout_s=60.0)`。
- `start()` / `close()`：独立酒店仓储与后台状态监视；runtime 生命周期仍由应用负责。
- `snapshot(session_id)` / `refresh(session_id)`：返回 JSON 可序列化服务快照，包含 service_id / phase / room_id / destination_id / robot_id / runtime_task_id / consent_confirmed / guest_ready / arrival_confirmed / error。
- `confirm_reception(session_id, receipt_code, *, operation_id)`：只采信酒店端已核验凭据。
- `record_guest_event(session_id, event)`：event 为 consent / ready / arrived；仅应用入口调用，不暴露给模型工具。
- `assign_robot(session_id, *, operation_id)`；`start_escort(session_id, *, operation_id)`。
- `request_human(session_id, reason, *, operation_id)`；`cancel(session_id, *, operation_id)`。
- `wait_for_settled(session_id, *, timeout_s=2.0)`：通过任务事件等待导航结束，供确定性验收使用，不用 sleep 等状态。
- 快照 phase 值由 hotel 模块定义并导出；Skill 选择由阶段决定。

### Agent 扩展（主代理）

- 工具执行接口仍为 schemas / async invoke(name,args,operation_id)，泛化类型使酒店工具可以组合，不改 runtime。
- AgentContext 增加可选 skills 和 guidance；guidance `async describe() -> dict` 返回当前业务事实与 `required_skill`。
- decide 加入技能名称描述目录，按业务阶段激活当前 Skill 正文，并记录 active_skill / loaded_skills；load_skill 工具用于显式按需加载。
- HotelTools 工具为 hotel_confirm_reception(receipt_code)、hotel_assign_butler()、hotel_start_escort()、hotel_status()、hotel_cancel_escort()、hotel_request_human(reason)。不暴露直接任意导航接口。
- 工具结果仍使用 Observation；业务等待阶段作为成功受理数据返回，避免把长任务当同步完成。

## Task 1: SkillRegistry 与三个业务 Skill

- [x] 为缺少加载器、非法前言、路径逃逸、元信息与正文分离、打包资源写失败测试。
- [x] 实现 Registry，依次编写 frontdesk-reception、guest-handoff、escort-assistance。
- [x] 用明确场景检查：机器人未分派、客人未确认开始、导航到达但客人未确认、人工交接仅模拟。
- [x] 验证无脚本执行与任意文件读取能力。

## Task 2: 酒店业务状态与模拟适配

- [x] 先写前台凭据、确认门禁、机器人占用、导航与客人到达区别、稳定幂等与恢复测试。
- [x] 实现模拟酒店、独立持久化、SDK 导航与后台监视。
- [x] 取消/失败/待核对转人工或保留隔离，重复调用不重复派发。
- [x] 验证回位不改变已经完成的客人服务结果。

## Task 3: 图、技能上下文与工具集成

- [x] 先验证阶段激活、load_skill 无物理副作用、所有工具名检查、状态绕过拒绝。
- [x] 将业务事实与当前技能正文作为受控系统上下文，逐阶段加载，不把所有正文塞入常驻提示词。
- [x] 保留原有模型与机器人测试行为。

## Task 4: CLI 与端到端

- [x] 加 scenario 配置、HotelDemoModel、独立酒店运行入口与控制命令。
- [x] 保持交互输入异步；后台任务不阻塞前台；支持新客人会话隔离和状态查询。
- [x] hotel 场景绑定独立身份与数据库，防止普通机器人模式和酒店模式混用恢复。
- [x] demo 端到端执行：入住凭据→同意→分派→开始→导航完成→客人到达→回位。

## Task 5: 文档、验证、独立审查

- [x] 更新根 README 的入口、模块树、状态边界和当前范围；更新 Agent 操作说明。
- [x] 容器运行 Agent 与 runtime 完整套件、技能校验、CLI、wheel 与 pip check。
- [x] 独立检查确认伪造、错误房间、重复护送、取消未确认和恢复安全；修复后复验。
- [x] 记录实际验证与真实 PMS / 导航 / 随行 / 乘梯 / 模型效果尚未验证的边界。

基线：Agent 48 passed，runtime 497 passed。当前工作区已有上轮未提交的 Agent，保留并扩展，不自动提交。

## 实际验收结果

- Agent：97 passed；原容器 runtime：497 passed；Agent 独立环境 runtime：497 passed。
- 三份 Skill 通过 skill-creator 标准 quick_validate；目录元信息小于 1 KiB，正文受控加载。
- 独立角色依据三份 SOP 推演八个边界场景，据反馈修正已有分配复用、完成后查询发现和人工求助回执表述。
- 独立代码审查发现取消后错误释放机器人问题，使用真实 SDK 的模拟器状态重现后修复：
  已停止但未归位为 needs_reposition，停止不明为 quarantined，均保留租约。
- 酒店模拟器显式从 hotel-lobby 初始化；酒店回执、目的地和支持楼层在提交前校验。
- 长导航期间新客人可继续接待，单机器人仍拒绝重复分配；该行为在 2 秒应用测试期限内验证。
- 分别构建 wheel；Agent wheel 含三份 SKILL.md，runtime wheel 不含 Agent；从安装后的 wheel 跑通完整酒店旅程并成功回位。
- compileall、源文本检查、pip check 通过；根依赖与 runtime 源码未改动。
- 真实 PMS、真实导航/随行感知/电梯、真实工作人员交接和部署模型的接待表现均未验证。
