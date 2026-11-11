# Agent 模块职责与目录迁移实施计划

> **For agentic workers:** 使用 superpowers:executing-plans 在当前会话依次执行；用户已确认下述目录设计。本次改动基于当前尚未提交的 agent 项目原地完成，不新建工作树、不提交此前改动。

**Goal:** 从目录上明确通用 Agent、酒店业务支持、场景装配与离线决策替身的区别。

**Architecture:** `graph.py` 继续负责唯一的通用决策循环。酒店业务实现移到 `domains/hotel/`，酒店场景入口移到 `scenarios/hotel.py`，酒店离线决策实现移到 `demos/hotel_model.py`。三个独立 Skill 保持原路径、正文、脚本和指纹；没有新的 Agent、业务阶段、权限或数据迁移。

**Tech Stack:** 现有 Python、LangGraph、SQLite 与 pytest；不新增依赖，所有执行验证在 robot_core_ubuntu24 容器中进行。

## 已确认的目录与约束

```text
agent/src/robot_agent/
├── graph.py
├── skills/
├── domains/
│   └── hotel/
│       ├── adapters.py
│       ├── models.py
│       ├── repository.py
│       ├── script_bindings.py
│       ├── service.py
│       ├── simulator.py
│       └── tools.py
├── scenarios/
│   └── hotel.py
└── demos/
    └── hotel_model.py
```

- 新目录添加说明职责的 `__init__.py`，不创建业务实例、不装配图。
- 不保留旧 `robot_agent.hotel`、`robot_agent.hotel_app` 兼容入口；内部导入和 monkeypatch 字符串全部使用新绝对路径。
- `robot-agent --scenario hotel`、Skill 名称、脚本源码、检查点名称、业务和任务数据库路径保持原样。
- 不修改 `src/robot_agent_runtime`、根依赖或任务状态机。

## Task 1: 建立基线与验证新入口

- [x] 核对当前目录、导入、动态导入、补丁字符串、打包配置及未提交改动。
- [x] 容器执行 Agent 全套基线：176 passed in 3.77s。
- [x] 先将已有 `test_hotel_model_selection_and_skills_directory` 中的模型导入改为 `robot_agent.demos.hotel_model`，运行该用例确认新路径尚不可用。

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -c agent/pyproject.toml agent/tests/test_hotel_app.py::test_hotel_model_selection_and_skills_directory -q'
```

## Task 2: 迁移模块并统一引用

- [x] `hotel/demo_model.py` → `demos/hotel_model.py`；`hotel_app.py` → `scenarios/hotel.py`；其余 `hotel/` → `domains/hotel/`。
- [x] 对 Agent 源码和测试进行精确路径替换，先替换两个特殊入口，再替换业务前缀：

```text
robot_agent.hotel.demo_model → robot_agent.demos.hotel_model
robot_agent.hotel_app → robot_agent.scenarios.hotel
robot_agent.hotel. → robot_agent.domains.hotel.
```

- [x] 新包添加职责说明；保留已有实现与构造装配方式。
- [x] 重跑入口用例和 Agent 完整套件，确认工具调用、三个 Skill、确认门禁、恢复与完整旅程的既有测试全部通过。

## Task 3: 同步文档

- [x] 更新根 `README.md` 的 Agent 介绍、公开入口、业务状态、模块树和当前范围中的路径说明。
- [x] 更新 `agent/README.md` 的入口说明、完整模块树及决策模型/数据模型区别。
- [x] 更新 `agent/HOTEL.md` 的代码职责和实现边界；保留命令、协议与实际验证限制。
- [x] 历史实施计划保持为历史记录，以本计划说明目录迁移。

## Task 4: 容器与发布包验证

- [x] 容器执行 Agent 全套、compileall 和 pip check。
- [x] 在容器临时目录从干净源码副本构建 wheel，独立安装后确认新模块齐全、旧入口不存在，三个 Skill 资源及脚本仍完整。
- [x] 从安装包运行酒店显式三技能接待/交接/护送/到达/回位和单技能过滤场景，核对最终状态。
- [x] 使用有界命令独立运行 runtime 全套；若出现已知的线程绑定用例等待，记录真实结果，不推断已修复。
- [x] 可恢复地归档旧 build 缓存，直接从当前 agent 项目构建 wheel，确认默认构建不会带入旧模块。
- [x] 回读改动、搜索有效源码/测试/当前文档中的旧路径并记录验证结果。

## 验证结果

- 入口新路径先测 RED：1 failed，原因为尚不存在 `robot_agent.demos`；迁移后完整套件 176 passed in 3.78s。
- runtime 使用 45 秒上限独立串行验证：497 passed in 10.44s；本次未出现历史线程绑定用例等待，不声称已修复其根因。
- compileall 与 pip check 均通过。
- 对迁移前快照做 AST 比较：10 个迁移模块除导入路径和模块 docstring 外逻辑一致；app.py 只有导入路径变化。
- 通用图、模型接口、Skill 宿主、全部 Skill 资源和 runtime 源码共 62 个文件的 SHA-256 保持一致。
- 干净源码副本 wheel 独立安装通过：13 个新目录/迁移模块齐全、旧入口缺席，3 份技能正文、7 份参考资料、3 份脚本完整。
- 安装包完整显式技能旅程最终 completed / arrival_confirmed=true / robot_status=available；单技能过滤、隐藏技能拒绝和纯选择不取业务快照通过。
- 构建环境最初缺少 setuptools，使用 pip 的临时隔离构建环境解决；未修改依赖声明或 Agent 运行环境依赖。
- 独立只读审查未发现迁移阻塞项；旧构建缓存另行归档并执行当前项目直接构建验收。
- 当前项目直接构建及独立安装再次通过完整旅程与单技能过滤；扫描源码、测试、当前构建输出和当前文档共 74 个文件，旧入口引用为 0。
- 本次直接构建产物与验收记录在容器 `/tmp/agent-module-layout-x34ar3m7/`；原 build 缓存可恢复地保存在其中的 `previous-build/`。
- 未提交当前工作树，未调用真实模型、PMS 或导航硬件。
