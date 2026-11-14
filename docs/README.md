# 文档索引

返回 [项目 README](../README.md)。

| 文档 | 读它当你需要 |
| --- | --- |
| [架构与模块结构](architecture.md) | 理解分层、线程归属、模块路径、当前范围 |
| [SDK 与公开 API](sdk-reference.md) | 调用 `RobotRuntime` / `RuntimeServer`，配置监听执行模式 |
| [生命周期、前置条件与恢复](lifecycle.md) | 任务 11 态、重启核对、条件失效与降级行为 |
| [技能定义与厂商接入](adapters.md) | 实现 `Skill` / `RobotPlatform`，接入新的机器人 |
| [统一接入说明](agent-integration.md) | 从 Codex / Claude Code / 自研 Agent 接入的最短路径 |
| [任务执行与恢复详解](task-execution-and-recovery.md) | 端到端流程、SQLite 与内存的差异、Reconciler 范围 |
| [测试与验证边界](testing.md) | 覆盖了什么、没覆盖什么 |
| [变更记录](../CHANGELOG.md) | 行为变化与兼容性 |

## 建议阅读顺序

**接入方**：README 快速开始 → 统一接入说明 → SDK 与公开 API → 生命周期（只需"失败怎么判读"一节）。

**实现技能/适配器**：技能定义与厂商接入 → 生命周期（前置条件与隔离） → 架构（模块边界）。

**维护内核**：`AGENTS.md` → 架构 → 生命周期 → 测试与验证边界。

## 设计计划

`plans/` 下是按日期归档的设计与实施记录，解释"当时为什么这么选"，不代表当前行为；
当前行为以本目录其余文档和代码为准。
