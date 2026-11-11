# robot-agent-core

通用 agent 内核（Python 3.11+ / asyncio）：LangGraph 决策循环、工具端口、Agent Skill 宿主、
命令行 shell 与存储身份解析。它**不含任何业务领域**，也不含机器人厂商 SDK 或 MCP 实现。

应用项目依赖它并各自拥有工具集、业务域与命令行入口：

- `agent/`（dist `robot-agent-app`，入口 `robot-agent`）：通用机器人 agent。
- `hotel-agent/`（dist `robot-agent-hotel`，入口 `hotel-agent`）：酒店接待与带房 agent。

内核不猜测应用：Skill 资源包名由调用方给出，存储策略由应用传入自己的常量，
命令行只共用参数校验、状态目录约定、错误脱敏与退出码。

## 模块结构

```text
src/robot_agent_core/
├── graph.py           decide / execute / observe 节点与路由
├── state.py           可序列化图状态与稳定动作标识
├── models.py          决策模型 Protocol 与 ChatOpenAI 适配
├── ports.py           ToolSet、TaskRuntime 端口与 Observation 事实记录
├── errors.py          应用层结构化错误
├── validation.py      与装配无关的通用期限校验
├── shell.py           输入、单轮图调用、错误状态、结果基础信封与交互循环
├── config.py          只依赖标准库的不可变配置快照（含 profile 字段）
├── configuration.py   StorageProfile、存储/身份解析、显式重建与启动一致性校验
├── cli.py             参数校验、状态目录约定、模型创建与释放、错误脱敏、退出码
└── skillkit/          Agent Skill 宿主，按模块导入，不做统一装配
    ├── catalog.py     Skill 元信息、文档校验、受控加载与 bundled(package) 目录来源
    ├── host.py        三项宿主工具、指导、冻结版本与批次状态更新
    ├── scripts.py     注册脚本执行、指纹校验、期限与结果
    ├── selection.py   用户目录查看与显式选择，不调用模型或业务
    └── versions.py    图执行与显式选择共用的正文/版本校验
```

## 应用必须自己提供的三件事

`StorageProfile`（`configuration.py`）是一份只描述存储与后端白名单的模块常量，取代了原来的场景描述符：
`name` 参与持久化身份哈希，改名字会让该名字产出的全部历史失效；`allowed_backends` 决定 `--backend` 的合法取值；
其余字段是各后端的库文件名，其中 `mcp_*` 两份为 None 表示该 agent 不开放 MCP 后端。
`resolve_storage(..., profile=...)` 只消费这份策略，不回查任何全局注册表；
`rebuild_config(config, **changes)` 用快照自带的 `profile` 重新解析，`validate_config(config)` 校验解析结果是否陈旧。

命令行骨架在 `cli.py`：`ApplicationParser` 把用法错误转成应用错误契约，
`default_data_dir()` 实现 `--data-dir` / `ROBOT_AGENT_DATA_DIR` / `XDG_STATE_HOME` 优先级，
`create_model` / `close_model` 保证模型凭据是启动前提，
`run_cli(parse, launch, argv)` 固定退出码 0 / 130 / 1 并脱敏凭据。
每个应用各自定义参数与装配函数，内核不提供"选哪个 agent"的入口。

Skill 资源同理：`SkillRegistry.bundled(package)` 由调用方给出包名，内核不假设某个包里一定有 `skills/`。
应用若不携带 Skill 内容，就需要 `--skills-dir` 才有可见目录。

## 安装与测试

```bash
python -m pip install -e ./agent-core
/opt/robot_agent_venv/bin/python -m pytest -q agent-core/tests
```

内核用例覆盖图与批次边界、Skill 宿主协议、脚本指纹与版本恢复、shell 信封、
存储解析与身份摘要，以及 `TaskRuntime` 端口在嵌入式、UDS 与 MCP 客户端下的一致契约；
其中 MCP 与 SQLite 用例分别按 `importorskip` 与容器依赖处理。
