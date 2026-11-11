# LangGraph Agent 同进程验证实施计划

> **For agentic workers:** 按任务顺序在当前 `agent/` 子项目实现；遵循 test-driven-development，完成后按 requesting-code-review 进行独立检查。

**Goal:** 在保留的 `robot_core_ubuntu24` 容器安装独立依赖，交付可以离线验证和连接真实模型、通过公开 SDK 控制模拟器或 CC 语音的 Agent。

**Architecture:** `agent/` 独立打包。LangGraph 的 decide → execute → observe 图通过 context 接收模型和客户端；检查点仅保存可序列化状态。应用统一管理连接和 runtime 生命周期，稳定动作标识在决策节点写入后才执行。runtime 源码与根依赖保持不变。

**Tech Stack:** Python 3.11+、LangGraph、langchain-openai、SQLite 检查点、现有 RobotRuntime、pytest / pytest-asyncio。

## 已确认设计

- 用户已确认本仓库 `agent/` 子项目与同进程验证路线，并授权在现有容器安装所需库。
- 验证应用先支持单 Agent、串行工具执行、模拟器与 CC 语音；不实现调度器或物理动作自动重试。
- 默认 demo 模型为明确标注的离线规则演示；真实模型通过环境变量配置兼容 OpenAI 的工具调用接口，不预设模型名称或访问凭据。
- 技能通过 `list_skills()` 发现并过滤，工具只能生成业务参数；应用控制期限、权限与白名单。
- 模型返回的多个工具按顺序执行。失败、等待超时、取消或待对账会结束本轮，后续动作返回跳过结果。
- 图的决策节点生成稳定动作标识并进入检查点，然后执行节点将其用作幂等键。恢复沿用原参数与标识。
- 查询与取消通过独立工具暴露；等待超时不代表取消或成功。
- 模型迭代次数、模型请求时间和任务等待时间均有界；不无限等待外部请求。
- CLI 支持单次输入、交互、会话标识与恢复，持久化 runtime 任务和图状态。

## Task 1: 工具适配与隔离环境

**Files:** `agent/pyproject.toml`、`agent/src/robot_agent/errors.py`、`agent/src/robot_agent/tools.py`、`agent/tests/test_tools.py`。

- [x] 在 `/opt/robot_agent_venv` 安装框架与测试库；保留原容器环境。
- [x] 先写并运行测试，观察未实现工具适配器时失败。
- [x] 实现发现、白名单、Schema 透传、稳定幂等键、终态结果、等待超时、查询与取消。
- [x] 对同进程与远程 SDK 验证适配边界，根 runtime 保持原样。

Run: `docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -c agent/pyproject.toml agent/tests/test_tools.py -q'`

## Task 2: 图、模型与检查点

**Files:** `agent/src/robot_agent/state.py`、`models.py`、`graph.py`、`agent/tests/test_graph.py`。

- [x] 先验证脚本模型驱动真实 Simulator 的图闭环、失败中止、调用上限与检查点恢复。
- [x] 实现模型 Protocol、离线规则模型及 ChatOpenAI 适配；模型异常使用结构化错误。
- [x] 实现三个异步节点，确保操作标识在执行前进入检查点。
- [x] 用 SQLite 检查点重建图恢复，验证已经执行的动作不会重复发生。

Run: 上述 pytest 命令改为 `agent/tests/test_graph.py`。

## Task 3: 应用入口与平台生命周期

**Files:** `agent/src/robot_agent/app.py`、`__main__.py`、`agent/tests/test_app.py`、`agent/.env.example`。

- [x] 先写 CLI 配置、离线单次执行、交互及平台清理测试。
- [x] 实现独立 CLI、模型配置、Simulator / CC 装配、任务与检查点数据库。
- [x] 显式管理恢复，防止新输入覆盖尚未完成的图执行。
- [x] 不输出凭据；帮助与默认离线模拟器模式不连接模型服务或真实机器人。

Run: 上述 pytest 命令改为 `agent/tests/test_app.py`。

## Task 4: 文档、验收与独立审查

**Files:** `agent/README.md`、根 `README.md`、本计划。

- [x] 更新安装、模型配置、交互、恢复、真实 CC 接入与能力边界说明。
- [x] 容器执行 Agent 全套、原环境 runtime 全套、独立环境 runtime 全套。
- [x] 执行 CLI 离线闭环、`pip check`、包边界与差异检查；记录安装版本。
- [x] 独立审查恢复、重复执行、取消、模型边界和生命周期；修复发现的问题并复验。
- [x] 报告实际完成内容及未进行的真实模型、硬件验证。

基线：2026-11-08，原容器环境 `497 passed`。此次不自动提交 Git。

## 验收结果

- Agent 完整套件：48 passed；原容器 runtime：497 passed；Agent 独立环境 runtime：497 passed。
- 模块与命令行单次、交互、真实客户端协议回传和 wheel 安装离线闭环通过。
- 分别构建 runtime 与 Agent wheel，确认包内容独立，runtime 依赖未加入框架。
- `pip check`、源码编译与文本空白检查通过；容器无 git，文本检查使用容器内 Python 完成。
- 审查发现并回归修复：超时后查询成功仍须中止本轮、交互输入阻塞 CC 循环、
  普通安装数据目录、英文命令前缀和可选 API 地址空值。
- 验证依赖：LangGraph 1.2.20、langchain-openai 1.7.4、checkpoint-sqlite 3.1.3、ubt_robot 2.0.16。
- 未使用真实模型凭据、连接真实硬件、进行进程强杀或多实例并发验证。
