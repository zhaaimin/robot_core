# 变更记录

> **项目创建**：2026-10-24

本文件记录对使用者的行为影响。内部重构、测试补充不在此重复。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-11-14

首个公开版本。

### 新增

- **MCP 桥支持 Streamable HTTP 传输**：`serve_http(socket_path, allowed_skills, host, port, token)`，
  端点 `/mcp`，`--mode stdio|http`，`--token` / `--token-env`。HTTP 模式默认监听 `127.0.0.1`，
  令牌为空时启动即失败；401/404 在进入 MCP 处理前拦截。两种传输暴露同一份工具目录，
  准入与执行语义一致。对应测试 `mcp_bridge/tests/test_mcp_bridge_http.py`。
- **技能失败时返回执行事实**（`SkillResult.action: ActionOutcome`）：`executed` 为
  `none|some|unknown`，`stopped` 三态，外加 `operator_required`。
- **`SkillDefinition.description` 参与 MCP 与内置工具的描述渲染**；边界值经
  `robot_agent_runtime.schemas.describe_bounds()` 复述进参数 description。schema 仍是唯一执行点。

### 行为变化

- 失败但无法确认静止（`executed != none` 且 `stopped != true`，或 `operator_required`）时，
  内核在释放资源锁**之前**隔离资源并抛 `ExecutionUnsettled`（`EXECUTION_UNSETTLED`，
  继承 `ExecutionUnresponsive` 的隔离后果）。新增错误码不改变既有 `EXECUTION_UNRESPONSIVE` 语义。
- 移除 `--scenario` 参数与 `scenarios/` 包；三个应用各自携带 `StorageProfile` 常量
  （`ROBOT_PROFILE` / `HOTEL_PROFILE` / `GUIDE_PROFILE`），配置快照不跨应用复用。
- 根目录模块迁入 `kernel` / `hosting` / `repositories` / `transport`，旧导入路径已删除。
  路径对照见 [架构 § 模块结构](docs/architecture.md#模块结构)。
- 后端白名单收窄为：robot `simulator|cc`，hotel/guide `simulator|mcp`。
  通用 agent 不再自带酒店技能，需要技能时必须提供 `--skills-dir`。

### 兼容性

- `SkillResult` 新增可选字段 `action`，未实现该字段的适配器行为不变（内核不假设静止）。
- `SkillDefinition.description` 默认为空字符串，旧定义无需改动。

### 文档

- README 从 1058 行重组为「一份 README + 五个专题文档」，正文内容未删减：
  [架构](docs/architecture.md)、[SDK 参考](docs/sdk-reference.md)、
  [生命周期与安全](docs/lifecycle.md)、[技能与厂商接入](docs/adapters.md)、
  [测试](docs/testing.md)。
- 修正过期表述：MCP 桥此前标注「只支持本机 stdio，不提供 HTTP 服务」，
  实际已提供 `--mode http`（Streamable HTTP + Bearer）。
- 修正技能示例中的权限名（`robot.navigation` → `navigation`），与 `simulator.py`、
  `allowed_permissions` 的取值一致。
- 新增 `assets/architecture.svg` 架构图，README 引用。
