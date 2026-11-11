# 自定义酒店 Skill 的可选脚本绑定修复计划

> **For agentic workers:** 按 receiving-code-review、systematic-debugging 和 test-driven-development 在当前工作树执行。本轮先修复已复现的目录/脚本能力混淆；场景描述符、shell 拆分、业务错误搬迁和 skill-host 抽取另行评估，不混入本修复。

**Goal:** 合法的纯文档或自定义脚本 Skill 不会因使用酒店内置名称而使整个酒店会话失效。

**Architecture:** 酒店工厂仅为当前可见、可读取且与发布源码指纹一致的脚本注册发布 handler。缺失、不可读取或指纹不符只取消该入口的脚本执行能力，并在 stderr 说明；实际调用未注册入口仍由通用 runner 以 AgentToolNotAllowed 拒绝。Skill 指导、其他已批准脚本、可信确认、控制命令和恢复校验保持原契约。

**Tech Stack:** 现有 Python / LangGraph / pytest，不新增依赖；只在 robot_core_ubuntu24 验证。

## 已确认的复现与基线

- Agent 基线：176 passed in 3.77s。
- 只含 SKILL.md 的 frontdesk-reception：`--prompt /skills` 返回 1，错误为 AGENT_CONFIGURATION_INVALID。
- 同目录交互输入 `/skills`、`/status`、`/cancel`：进程返回 0，但三次结果均为该配置错误。
- 工厂无条件把三个可见名称对应的发布 binding 交给严格 runner；runner 正确拒绝缺失/变化的资源。应修复的是工厂筛选，不是放宽通用 runner。

## Task 1: 回归测试先行

- [x] 修改“替换脚本使构造失败”的旧用例，改为构造可用、调用未绑定脚本被拒绝，且工具零调用。
- [x] 覆盖三个内置名称的纯文档包，以及混合目录中无脚本/替换脚本不会影响另一份发布脚本。
- [x] 覆盖缺失和替换两种资源下，单次 `/skills`、`/status`、`/cancel` 成功，以及交互选择、查询、取消仍可用。
- [x] 运行上述用例，确认在原实现上因绑定构造失败而 RED。

## Task 2: 最小修复

- [x] 修改 `agent/src/robot_agent/domains/hotel/script_bindings.py`：读取活跃资源，与发布指纹比较；缺失/不可读取或不匹配则输出一行警告并跳过；仅匹配时导入并注册发布 handler。
- [x] 不导入或执行用户脚本代码，只作有界文本读取和指纹比较；不放宽 SkillScriptRunner 注册、调用和恢复指纹校验。
- [x] 重跑回归测试及 Agent 全套。

## Task 3: 文档与验收

- [x] 更新 agent/HOTEL.md、agent/README.md 和根 README.md，明确目录可见、文档加载与脚本批准是三个不同边界。
- [x] 容器执行 compileall / pip check，并从 CLI 独立验收缺失、替换脚本和 bundled 完整旅程。
- [x] 复核审查中的结构性建议，记录成立之处、建议顺序及不能直接套用的部分。
- [x] 回读最终改动和实际结果；保留当前未提交工作，不提交或修改 runtime 内核。

## 实际结果

- 修复前 11 项目标回归均失败；修复后 11 passed, 36 deselected。
- Agent 完整套件：186 passed in 3.97s。
- runtime 独立串行完整套件：497 passed in 10.55s；compileall 与 pip check 通过。
- 独立 CLI 子进程验收：缺失和替换两种目录的单次/交互返回码均为 0，选择保留、查询返回 reception、取消返回 canceled，警告每个上下文一次。
- bundled 完整显式三技能旅程仍为 completed / arrival_confirmed=true / robot_status=available，正常发布脚本无跳过警告。
- 与迁移基线比对：通用 Agent、全部 Skill 资源及 runtime 源码共 62 个文件 SHA-256 保持一致，发布脚本指纹未变。
- 独立只读审查没有 Critical / Important 问题；计划中“不读取脚本”的表述已更正为“不导入或执行，只作有界文本读取和指纹比较”。

## 结构性反馈的核对结果

本轮只修改酒店脚本注册工厂，不把下面的职责调整混入功能修复：

1. `app.py` 确实包含场景名称、后端限制、数据库名、demo 工厂和运行入口选择；新增业务时值得改成描述符分发。适合放在显式的 `scenarios/registry.py`，避免在包 `__init__.py` 中提前装配。
2. `app.py` 与 `scenarios/hotel.py` 确实形成双向导入，目前靠运行时懒导入避免初始化错误。应先抽出共享配置、输入读取、单轮执行和结果基础包装，解除依赖环，再接场景描述符。酒店控制事件、客人切换、业务快照和生命周期仍应由场景负责，不能照搬通用主循环。
3. 两处 runtime 构造有不同权限与生命周期：酒店只开放模拟导航，通用入口还支持音频和 CC 平台。抽公共构造时必须显式保留能力与平台所有权，不能简单合并成同一套默认权限。
4. `HotelWorkflowError` 放在通用 errors.py 是职责归属问题；迁入酒店领域更清楚。不过三个发布脚本引用这个错误类，改导入会改变脚本指纹，影响待恢复动作的版本校验，需要单独说明兼容/重新确认行为，不宜在本次偷偷变更。
5. `graph._loaded_documents` 和 `skill_selection._verified_documents` 的校验确实重复，且文案不一致。适合独立提取共享校验，保留原有缺失 registry、不可见技能、正文/版本不匹配及 pending 边界的测试。
6. 图识别通用 load/read/run 协议属于 Agent Skill 宿主耦合，不是酒店业务耦合；graph.py 没有酒店导入。抽 skill_host.py 是可选模块职责优化，可以后置；复用 runtime InvalidRequest 也不构成已复现的功能故障。
