# 通用机器人 Agent

本项目是仓库内独立打包的**通用**单 Agent 应用：LangGraph 决策循环来自共享内核
`robot-agent-core`（`src/robot_agent_core/`），本项目只提供机器人工具集 `RobotTools`、
CC 厂商连接和命令行入口 `robot-agent`。酒店接待与带房是另一个独立项目
[`hotel-agent/`](../hotel-agent/README.md)，两者不再靠参数区分，也不共享业务代码。

runtime 的源码与根依赖保持独立；框架依赖只声明在 `agent-core/` 与本项目的
`pyproject.toml`，不进 runtime 发布包。

## 后端与能力

决策模型只有一种，机器人后端有两个（`--backend`，默认 `simulator`）：

- `simulator`：嵌入式 runtime + 仓库自带模拟器，默认开放 `audio.speak`、
  `navigation.navigate_to`、`robot.get_state`、`wait.delay`。
- `cc`：经 `ubt_robot` SDK 连接真实机器人，当前只开放已实现的 `audio.speak`，
  没有真实导航或状态查询技能。

`--backend` 的合法取值由本项目的 `ROBOT_PROFILE` 决定，不存在 `mcp` 后端；
MCP 桥接只由酒店项目消费。运行时技能可用重复的 `--allow-skill NAME` 缩小白名单。

模型接口是唯一运行方式：每次运行都必须提供模型名称和凭据，可以是 `--model`／
`ROBOT_AGENT_MODEL` 与 `OPENAI_API_KEY`，兼容服务可填写 `OPENAI_BASE_URL`。
应用不内置规则演示模型，因此不存在“不接语言模型也能跑”的模式；连接不到模型时
启动即以 `AGENT_CONFIGURATION` 失败，不会静默降级。

## Agent Skill 宿主

Agent Skill 是可选能力包：`SKILL.md` 描述处理方法，`references/` 保存按需读取的资料，
`scripts/` 保存有实际用途的执行入口，`assets/` 可放任务确实需要的资源。
宿主实现（工具 schema、参数校验、指纹冻结、批次边界）在共享内核 `skillkit/`，本项目不重复实现。

**本项目不携带任何 Skill 内容**，因此默认没有可见目录；要用 Skill 必须显式指定目录：

```bash
/opt/robot_agent_venv/bin/robot-agent --skills-dir /path/to/skills
```

`--skill NAME` 可重复，用于把可见目录限制在指定包；它选择 Agent Skill，
与选择 runtime 机器人技能的 `--allow-skill` 用途不同。加载技能不是业务授权，
实际请求仍受 runtime 约束。

用户可输入 `/skills` 查看可见目录，输入 `/skill NAME` 明确加载并选择其中一项。
这两种命令不调用模型或业务接口；加载只修改图中的技能状态和审计。
待执行动作存在时允许查看目录，选择操作要求先恢复或处理该动作。

注册脚本执行是可信边界，不是运行任意不可信 Python 的沙箱：新增脚本入口必须由应用显式注册，
不会因为文件出现在目录中就获得执行权。`src/` 里没有伪造的决策实现，测试用的脚本替身留在
`tests/scripted_models.py`，由 `tests/conftest.py` 注入，不参与应用装配。

## 容器环境

安装与验证均在保留的 `robot_core_ubuntu24` 容器完成，项目挂载到 `/workspace/robot_core`。
Agent 环境是 `/opt/robot_agent_venv`，与原有 runtime 验证环境隔离；宿主无需安装依赖。
仓库现在有三个可编辑安装的 Agent 包，从仓库根目录执行：

```bash
python3 -m venv /opt/robot_agent_venv
/opt/robot_agent_venv/bin/python -m pip install -e . -e ./agent-core -e ./agent -e './agent[test]'
/opt/robot_agent_venv/bin/python -m pip install ./cc_api/ubt_robot-2.0.16-py3-none-linux_aarch64.whl
```

厂商 wheel 只用于本容器和 CC 部署环境，不是通用 Agent 包的依赖。
酒店项目的安装与 MCP 可选依赖见 [hotel-agent/README.md](../hotel-agent/README.md)。

2026-11-08 验证版本：LangGraph 1.2.20、langchain-openai 1.7.4、
langgraph-checkpoint-sqlite 3.1.3、ubt_robot 2.0.16，`pip check` 通过；
PyYAML 6.0.3 由共享内核声明。

### 构建发布包

发布前先归档项目中的 `build/` 和旧 egg-info，再构建 wheel。setuptools 会复用 `build/lib`，
移动或删除源模块后，旧缓存可能把已经移除的入口一起打进包中。在容器内从仓库根目录执行：

```bash
agent_build_archive=$(mktemp -d /tmp/robot-agent-build.XXXXXX)
for artifact in agent/build agent/src/robot_agent_app.egg-info; do
    if [ -e "$artifact" ]; then
        mv "$artifact" "$agent_build_archive/"
    fi
done
/opt/robot_agent_venv/bin/python -m pip wheel --no-deps \
  --wheel-dir "$agent_build_archive/wheels" ./agent-core ./agent
```

产物位于 `$agent_build_archive/wheels`，旧缓存保留在同一归档目录供核对。

## 首次运行

模型凭据是启动前提，先按下方“接入真实模型”准备 `agent/.env`，再执行一次播报：

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && set -a && . agent/.env && set +a && /opt/robot_agent_venv/bin/python -m robot_agent --prompt "播报 你好，我是机器人助手"'
```

交互模式：

```bash
docker exec -it robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && set -a && . agent/.env && set +a && /opt/robot_agent_venv/bin/robot-agent --thread-id local-1'
```

输入什么由模型理解，应用没有固定命令表；只有 `/skills`、`/skill NAME` 等输入边界命令
由应用直接处理。每轮结果以 JSON 写入 stdout，包含 `model`、`backend`、`thread_id`、
`reply`、`observations`；提示和错误写入 stderr。
交互输入不会阻塞平台所属事件循环，等待输入期间仍可处理 CC 完成回调。

## 接入真实模型

进入容器后，从仓库根目录执行：

```bash
cp agent/.env.example agent/.env
```

填写 `OPENAI_API_KEY` 与 `ROBOT_AGENT_MODEL`；兼容服务可填写 `OPENAI_BASE_URL`。
`.env` 作为 shell 配置使用，应用不会自动读取它：

```bash
set -a
. agent/.env
set +a
/opt/robot_agent_venv/bin/robot-agent --thread-id model-test
```

模型必须支持工具调用。应用使用 Chat Completions 接口，不绑定特定模型名称，
也不推断兼容服务提供方的模型能力。默认单次模型请求期限为 30 秒，客户端不自动重试。
模型输入包含实际开放的技能 Schema，工具结果再次送回模型。

当前已用本地 HTTP 测试服务验证真实 ChatOpenAI 客户端的请求、工具调用解析、结果回传、
超时和错误处理；尚未使用真实模型服务凭据验证推理质量。

## CC 语音

在具备厂商 SDK 且能够连接机器人的环境运行：

```bash
/opt/robot_agent_venv/bin/robot-agent --backend cc --api-id YOUR_API_ID \
  --address ROBOT_IP:48159 --prompt '播报 你好'
```

机器人本机可省略 `--address`。也可通过 `CC_API_ID`、`CC_API_ADDRESS` 和
`CC_API_TOKEN` 设置连接参数；Token 只从环境变量读取。模型凭据仍然必需。

应用在事件循环外创建、启动和关闭原生连接；平台和 runtime 的生命周期在所属循环内进行。
退出顺序为 runtime → platform → 模型客户端 → 原生连接，异常路径也执行释放。
播报成功含义沿用厂商适配器对 `work.finished` 的现有约定，尚不解析结束原因；
当前 CC 平台没有状态源和重启对账器。

厂商 SDK 已在 Agent 环境完成安装和导入检查；本次没有连接真实机器人或执行硬件动作。

## 任务结果和执行边界

技能由 `list_skills()` 发现，应用可用重复的 `--allow-skill` 参数缩小白名单。
模型只提供业务参数；权限、任务期限和等待期限由应用确定。
查询和取消通过管理工具完成，不需要绕过 SDK 读取内核对象。

- 同一轮模型最多返回 8 个工具调用，按顺序执行；默认最多调用模型 8 次。
- 每个动作的稳定标识由决策节点生成，先通过同步检查点持久化，再提交为 runtime 幂等键。
- 失败、取消、等待超时、状态不确定或待对账会停止本轮后续动作，后续调用返回跳过结果。
- 仅 `succeeded` 表示技能报告成功。任务被接受、返回 task_id 或到达终态都不等于成功。
- `--wait-timeout` 默认 5 秒；到期不会自动取消动作，可以在仍运行的交互应用中查询或显式取消。
  即使紧接着的查询看到成功，仍保留本轮等待超时并暂停后续动作。
- `--task-timeout` 默认 60 秒；任务执行与停止确认继续遵循 runtime 语义。
- 单次模式返回结果后会关闭嵌入式 runtime，关闭阶段会停止尚未完成的任务。
  想保持任务运行并继续查询时使用交互模式。

第一阶段按一台机器人一个应用实例验证，不支持多个应用实例并发拥有同一机器人；
资源互斥属于各自 runtime 实例。其他限制选项见 `robot-agent --help`。

## 检查点与恢复

数据目录按以下优先级选择：`--data-dir`、非空 `ROBOT_AGENT_DATA_DIR`、
有效绝对路径 `$XDG_STATE_HOME/robot-agent`、`~/.local/state/robot-agent`。
数据位置与安装包目录无关，可显式传入 `--data-dir agent/.data` 将验证数据放在本项目内。
图状态和任务分开保存：

- `checkpoints.sqlite3` 保存会话、工具调用和稳定动作标识。
- `simulator-tasks.sqlite3` 保存模拟任务和事件。
- `cc-<连接身份摘要>-tasks.sqlite3` 保存对应 CC 连接的任务和事件。

本项目没有业务库（`business_database` 为 None）；酒店业务库归 `hotel-agent` 所有。
Simulator 的设备状态每次启动重新创建，任务数据库不会恢复模拟器的当前位置。
需要恢复时保留同一组检查点和任务数据库，不删除、替换或回退其中一个数据库。
不要把 SQLite 图检查点理解为机器人动作重放日志。

使用相同 `--thread-id` 可继续已结束的会话；若检查点仍有待执行节点，新输入会被拒绝，需显式恢复：

```bash
/opt/robot_agent_venv/bin/robot-agent --thread-id model-test --resume
```

恢复使用原动作标识和参数。已提交任务通过 runtime 幂等语义取得原结果。
会话绑定后端、任务数据库路径和 CC 连接身份，不能恢复到另一个机器人后端。
重启时 runtime 仍先按自身规则核对未完成任务；没有权威终态会进入待对账，
Agent 不自动重新执行物理动作，也不自动重试失败动作。

## 模块结构

```text
src/robot_agent/
├── app.py           CLI 参数、凭据前提、CC 原生连接所有权与退出装配
├── profiles.py      ROBOT_PROFILE：后端白名单与固定库文件名
├── application.py   通用机器人装配、SDK/平台生命周期与交互错误策略
├── robot_tools.py   把白名单 runtime 技能翻译成模型工具
└── __main__.py      python -m robot_agent 入口
```

图、状态、模型、shell、配置解析、Skill 宿主与 CLI 骨架都在
[`agent-core/src/robot_agent_core/`](../agent-core/README.md)；本项目不复制这些通用模块，
也不提供旧 `robot_agent.graph`／`robot_agent.scenarios` 路径的兼容入口。
影响存储或身份的配置变化用 `robot_agent_core.configuration.rebuild_config(config, **changes)` 重建；
`prompt`、`max_turns` 等非身份字段仍可继续用 `dataclasses.replace`。
`task_database`、`checkpoint_database`、`business_database` 和 `runtime_id` 是只读解析结果，
不能通过重建入口覆盖；陈旧解析结果会在创建任何资源前被 `validate_config` 拒绝。
应用启动时还会核对快照里的 `profile` 是不是 `ROBOT_PROFILE`，
因此拿着酒店 agent 的配置快照调用本项目会在打开存储前失败，不会读到另一套历史。

## 测试

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -q agent-core/tests'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -q agent/tests'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -q hotel-agent/tests'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && python3 -m pytest -q'
```

Agent 测试有独立配置和框架依赖，使用上述容器命令收集，不加入历史宿主 `.verify`。
`tests/conftest.py` 为每个用例注入假凭据与脚本决策模型，所以测试不需要语言模型凭据。
本项目用例覆盖真实 runtime / Simulator、远程 SDK、SQLite 检查点重建与幂等恢复、
失败后跳过后续动作、查询与取消、模型工具调用协议和客户端释放、CC 生命周期、异步交互输入，
以及 `ROBOT_PROFILE` 的后端白名单、身份摘要取值和“拒绝另一 agent 的快照”这条边界。
自定义 Skill 目录的可见性、显式选择与恢复边界在共享内核用例中覆盖。

runtime 的权威语义见 [仓库 README](../README.md) 与
[任务执行和恢复说明](../docs/task-execution-and-recovery.md)。
