# 两个 agent 拆开：共享内核包 + 通用 robot agent + 酒店 agent

**Goal:** 把 hotel agent 与通用 robot agent 拆成两个独立项目，靠"哪个入口"区分，不再靠 `--scenario` 参数区分；真正共用的部分收进一个共享内核包。

**Architecture:** 三项目结构 —— `agent-core/`（图、状态、模型端口、shell、配置解析、Skill 宿主、CLI 骨架）、`agent/`（只剩通用 robot agent：RobotTools + cc 连接）、`hotel-agent/`（酒店领域模型、工作流、三个 bundled Skill、MCP 适配器）。场景描述符 `ScenarioDescriptor` 被每个项目自带的 `StorageProfile` 常量取代，`AppConfig.scenario` 字段与 `scenarios/registry.py` 一起删除。runtime 与 mcp_bridge 一行不改。

**Tech Stack:** 现有 LangGraph / SQLite / asyncio；不新增依赖；验证全部在 `robot_core_ubuntu24` 的 `/opt/robot_agent_venv/bin/python`。

## 结论先说

1. 布局按你选的"两个独立项目 + 共享内核包"落地，共享包 import 名 `robot_agent_core`、dist 名 `robot-agent-core`（跟 `robot-agent-runtime` 同族前缀）。通用 agent 保留 dist `robot-agent-app` 与 import `robot_agent`，入口仍是 `robot-agent`；新项目 dist `robot-agent-hotel`、import `hotel_agent`、入口 `hotel-agent`。
2. 删除 `--scenario`。存储策略与后端白名单成为各项目的常量，不再运行时查表；两个 agent 各自的 `--backend` choices 收窄为 `simulator|cc`（robot）与 `simulator|mcp`（hotel）。
3. 持久化身份逐字不变：数据库文件名、`runtime_id` 哈希输入、`hotel:{service_id}:escort|return` 幂等键、已发布脚本指纹全部照旧。现成证据是 [test_configuration.py:38-46](/Users/zhazha/Documents/robot_core/agent/tests/test_configuration.py#L38) 已经把 4 组 `runtime_id` 硬编码成 digest，拆完必须仍然绿。
4. 两处**有意**的行为变化，需要你确认：通用 agent 不再自带酒店 Skill；酒店输出信封的 `'scenario': 'hotel'` 改名为 `'agent': 'hotel'`。
5. 分四步，每步容器跑完不退化再进下一步：抽内核 → 切酒店项目 → 删场景概念 → 文档与基线对账。

## 现在为什么读不懂（实测）

`scenario` 这个词在 `agent/src/robot_agent/**/*.py` 出现 **58 次 / 7 个文件**，测试里另有 124 次。其中真正属于"两个 agent 的差异"的内容被摊在共用层：

| 位置 | 混合形态 |
| --- | --- |
| `scenarios/registry.py` | 一个 dict 同时装着 robot 和 hotel 的存储策略、后端白名单、入口函数 |
| `configuration.py`（14 处） | `resolve_storage()` 里 `if backend == 'mcp'` 与 `if scenario.runtime_identity_tag is not None` 两条分支，只服务 hotel |
| `app.py`（8 处） | 一个 71 行的 `parse_args` 同时校验 hotel 才有的 `--runtime-socket`，并为 mcp 单独覆盖默认技能集 |
| `adapters/mcp.py` | 放在共用层，却硬编码 `{'navigation.navigate_to','audio.speak'}` 和一句 `'Hotel MCP runtime is missing required skills'` |
| `skillkit/catalog.py:144` | `files("robot_agent").joinpath("skills")` —— 通用 agent 因此能 `--skill` 加载酒店 Skill |
| `domains/hotel/*` + `skills/*` | 酒店领域代码与三个酒店 Skill 包住在通用 agent 的包里 |

`agent/tests/` 26 个用例文件里 8 个是纯酒店用例（`test_hotel_*`，共 80 个测试定义），另有 `test_scenarios.py`（5）整体只为验证"一个包能切两个场景"而存在。

## 目标目录

```
robot_core/
├── src/robot_agent_runtime/          # 不动（runtime 归你）
├── mcp_bridge/                       # 不动
├── agent-core/                       # 新：共享内核包
│   ├── pyproject.toml                #   dist robot-agent-core
│   ├── README.md
│   └── src/robot_agent_core/
│       ├── graph.py  state.py  models.py  ports.py  errors.py  validation.py
│       ├── config.py  configuration.py  cli.py
│       ├── shell.py
│       └── skillkit/{catalog,host,scripts,selection,versions}.py
│   └── tests/                        #   图/宿主/配置/shell/契约用例
├── agent/                            # 改：只剩通用 robot agent
│   ├── pyproject.toml                #   dependencies += robot-agent-core
│   ├── README.md
│   └── src/robot_agent/
│       ├── app.py                    #   CLI：--backend simulator|cc
│       ├── application.py            #   原 scenarios/robot.py
│       └── robot_tools.py
│   └── tests/{test_app,test_tools,test_scenarios→…}.py
└── hotel-agent/                      # 新：酒店 agent
    ├── pyproject.toml                #   dist robot-agent-hotel，入口 hotel-agent
    ├── README.md                     #   原 agent/HOTEL.md 升级为项目 README
    └── src/hotel_agent/
        ├── app.py                    #   CLI：--backend simulator|mcp + --runtime-socket
        ├── application.py            #   原 scenarios/hotel.py
        ├── adapters/mcp.py
        ├── domains/{models,errors,repository,service,task_coordination,tools,simulator,script_bindings}.py
        └── skills/{frontdesk-reception,guest-handoff,escort-assistance}/
    └── tests/                        #   8 个 test_hotel_* + 酒店配置用例
```

`domains/hotel/` 摊平成 `hotel_agent/` 的直接模块（项目本身就是酒店域，少一层前缀）；`adapters/` 只剩 `mcp.py`，`__init__.py` 那句"loaded by the selected scenario"随之删除。

## 归属表

| 现文件 | 去处 | 理由 |
| --- | --- | --- |
| `graph.py` `state.py` `models.py` `ports.py` `errors.py` `validation.py` `shell.py` `config.py` `configuration.py` | `robot_agent_core/` | 与场景无关：图、状态快照、模型端口、shell 循环、存储解析本身 |
| `skillkit/`（5 文件） | `robot_agent_core/skillkit/` | Agent-Skill 宿主是通用能力，酒店只是它的一个使用者 |
| `app.py` 的参数校验助手、`_default_data_dir`、`_write_error`、`open_cc_connection` 之外的骨架 | `robot_agent_core/cli.py` | 两个 agent 都要正数校验、数据目录约定、错误脱敏。`open_cc_connection`（依赖 `ubt_robot`）留在 `agent/` |
| `robot_tools.py` | `agent/` | 通用 runtime 工具集，酒店用的是 `HotelTools` 而不是它 |
| `scenarios/registry.py` | **删除** | 见下节 |
| `scenarios/robot.py` | `agent/src/robot_agent/application.py` | 只有一个使用者 |
| `scenarios/hotel.py` | `hotel-agent/src/hotel_agent/application.py` | 同上 |
| `adapters/mcp.py` | `hotel-agent/src/hotel_agent/adapters/mcp.py` | 内容本来就是酒店专属 |
| `domains/hotel/**` | `hotel-agent/src/hotel_agent/**` | 领域代码 |
| `skills/**`（3 个包） | `hotel-agent/src/hotel_agent/skills/**` | 文档内容就是接待/交接/带房 SOP |

移动的文件里凡是 import `robot_agent.graph` 这类共用层的，改成 `robot_agent_core.*`；`hotel_agent` 内部互相引用改自己的包名。`script_bindings.py:48` 的 `import_module(f'robot_agent.skills.{name}.scripts.{script}')` 必须改成 `hotel_agent.skills.…` —— 指纹是按源码内容算的（`SkillScriptRunner` 比 `resource['fingerprint']`，模块路径不在指纹里），所以换包名不会让已发布指纹失效。

## 用 `StorageProfile` 取代 `ScenarioDescriptor`

`robot_agent_core/configuration.py` 里换成只描述存储、不含 `run` 回调的常量：

```python
@dataclass(frozen=True)
class StorageProfile:
    name: str                     # 仍进 identity hash：'robot' / 'hotel'
    simulator_database: str
    runtime_identity_tag: str | None
    checkpoint_database: str
    business_database: str | None
    allowed_backends: frozenset[str]
    mcp_checkpoint_database: str | None = None
    mcp_business_database: str | None = None

ROBOT_PROFILE = StorageProfile('robot', 'simulator-tasks.sqlite3', None,
                               'checkpoints.sqlite3', None, frozenset({'simulator', 'cc'}))
```

酒店那份常量放在 `hotel_agent` 自己的包里。`resolve_storage(..., profile=...)` 只把参数名和取值来源换掉，哈希输入的**顺序与内容一字不改**：mcp 仍是 `json([backend, str(runtime_socket), profile.name])`，非 mcp 仍是 `json([backend, task_database, api_id?, address?])` 再按需追加 tag。

`AppConfig` 去掉 `scenario: str = 'robot'`，改为必填 `profile: StorageProfile` —— 于是通用 agent 的配置快照在类型上就不可能自称是酒店 agent，`validate_config()` 也不必回查全局表（现在 `test_configuration.py:81` 专门测这条"不查表"，拆完这条测试变成结构性事实，可以简化为断言 `config.profile.name`）。`scenarios/registry.py`、`scenario_names()`、`get_scenario()`、`--scenario` 一起删除；`conftest.py` 里 `scripted_model(config.scenario)` 改为各项目的 conftest 注入自己的替身模型。

## 两处有意的语义变化

**一、通用 agent 不再自带酒店 Skill。** `SkillRegistry.bundled()` 参数化为 `bundled(package: str)`，`agent/` 传 `package='robot_agent'` 时找不到 `skills/`，默认走 `--skills-dir` 或内置空目录报错。想要酒店 SOP 就运行 `hotel-agent`；想在通用 agent 里加载任意 Skill 文档，`--skills-dir` 仍然完全可用。这是"减少混合"的直接后果，也是本次拆分的目的之一。

**二、酒店输出信封 `'scenario': 'hotel'` → `'agent': 'hotel'`。** 拆完 `scenario` 这个概念在代码里不存在了，留着键名会让读 JSON 的人去找一个已经不存在的参数。改 1 处写入（`application.py` 的 `_result`）加对应断言，没有其他消费者。

## 必须逐字保留

- 7 个数据库文件名字符串（含 `hotel-mcp-{endpoint}-checkpoints.sqlite3` 模板）与 `data_dir` 约定、`ROBOT_AGENT_DATA_DIR` / `XDG_STATE_HOME`。
- `runtime_id` 的哈希输入顺序与编码（已有 digest 测试守住；酒店额外补一条 mcp digest pin）。
- `hotel:{service_id}:escort` / `:return` 幂等键（`task_coordination.py` 与 `service.py:_navigation_request`）。
- ~~三个酒店 Skill 包内 `scripts/*.py` 的源码内容 —— 一个字节都不改~~（落地时不成立，见下方偏差记录）。
- `robot_runtime_mcp.client.open_mcp_runtime(socket, allowed_skills=...)` 调用形态。
- 各 `--backend` 默认技能集的实际值（simulator 四项 / cc 一项 / mcp 两项），只是不再靠分支判断选择。

## 成本与风险

| 项 | 量 | 处置 |
| --- | --- | --- |
| import 重写 | 共用层 1440 行（含 `skillkit/` 688）+ 酒店层 1132 行与 3 个 Skill 包搬家 | 纯机械；用 `compileall` + 三套完整用例兜住 |
| 容器安装 | 由 1 个 editable 变 3 个 | 需 `pip install -e ./agent-core -e ./agent -e ./hotel-agent --no-deps --no-build-isolation`；宿主 pip 403，只能进容器做 |
| 用例重分配 | 264 个（现 30 文件 / 246 个模块级定义） | 按表拆到 3 个 `tests/`，先写"归属清单"再搬，搬完三套总数对账到 264，一条不许丢 |
| `.verify/sweep.py` | FILES 只扫 `agent/tests` | 本仓库主力已改容器，sweep 基线数字作废；不投入维护 |
| 两处语义变化 | 见上节 | 需要你点头；不同条就只做布局拆分 |
| README 三处 | `agent/README.md`、`HOTEL.md`、根 `README.md` 模块树 | 第 4 步统一改，公开行为变了必须同步 |

最大风险不是搬家，是**第 3 步删场景概念时顺手改到哈希输入**。对策：把 digest pin 测试在动手前先补全（robot / hotel-simulator / hotel-mcp 三条各钉一个固定值），任何走样立刻红。

## 明确不做

不动 `src/robot_agent_runtime/**` 与 `mcp_bridge/**`（你的硬约束）。不给通用 agent 加 mcp 后端，不给酒店 agent 加 cc 后端。不做插件式 agent 注册表、不做"多场景可插拔"框架 —— 那等于把 `--scenario` 换个写法留下。不改幂等键、不改文件名、不改 Skill 文档内容。不删 `SimulatedHotel` / `HotelSimulator`（此前你选了"先不删除"）。不引入新依赖。

## 实施步骤（每步容器验证后才进下一步）

1. **抽共享内核**：建 `agent-core/`，把归属表前两行的文件移过去，`agent/` 依赖它并改成 `robot_agent_core.*` 导入；`--scenario` 暂时原样保留。验收：`agent` 264 passed，runtime 497 passed。
2. **切出酒店项目**：建 `hotel-agent/`，移 `domains/hotel`、`skills`、`adapters/mcp.py`、`scenarios/hotel.py`（→ `application.py`）与 8 个 `test_hotel_*`；补酒店自己的 `app.py`（含 `--runtime-socket`）。验收：`agent` + `hotel-agent` 两套总数 = 264，runtime 497。
3. **删场景概念**：`ScenarioDescriptor` → `StorageProfile`，去掉 `AppConfig.scenario` 与 `--scenario`，删 `scenarios/registry.py`，`scenarios/robot.py` → `application.py`，`SkillRegistry.bundled(package)`，信封键改名。验收：digest pin 三条全绿，两套总数仍 264。
4. **文档与对账**：三处 README 模块树与运行说明、`HOTEL.md` 并入 `hotel-agent/README.md`、根 README 目录树与启动命令；容器 `pip list` 记录三个 editable 安装。验收：三套全绿 + 一次 `hotel-agent` 与一次 `robot-agent --backend simulator` 实跑冒烟。

## 实际落地偏差（2026-11-10 实施后回填）

1. **三份酒店发布脚本的源码指纹变了。** 它们的 import 必须从 `robot_agent.domains.hotel.errors` /
   `robot_agent.skillkit.*` 改成 `hotel_agent.errors` / `robot_agent_core.*`，
   所以"一个字节都不改"做不到。指纹按源码内容计算，`script_bindings.py` 是用
   `SkillRegistry.bundled('hotel_agent')` 的**同一份发布内容**去比对活跃资源的指纹，
   两侧一起移动，绑定仍然成立；代价是升级前保存的 pending 脚本决策返回
   `AGENT_CONFIGURATION_INVALID`，不执行旧指令、不自动刷新指纹。
2. **`domains/hotel/` 摊平时 `adapters.py` 与 `backend.py` 合并成了 `backend.py`**
   （`HotelBackend` 端口、`VerifiedStay`、`SimulatedHotel` 同处一个模块），
   `adapters/` 只留 `mcp.py`。
3. **`agent/pyproject.toml` 去掉了 `mcp` extra 和 `skills/*` package-data**：通用 agent 不携带 Skill 内容，
   也不消费 MCP 桥接；MCP extra 只在 `hotel-agent` 声明。`agent-core/tests/test_contracts.py` 里的
   TaskRuntime 契约用例用 `importorskip('robot_runtime_mcp')`，不依赖该 extra。
4. **用例总数从 264 涨到 291**（agent-core 73 + agent 101 + hotel-agent 117）：
   共享层与配置层的用例在两个应用里各留一份（后端白名单、身份摘要、拒绝另一 agent 的快照），
   酒店额外补了 mcp digest pin 与 profile 归属检查。没有任何一条原有覆盖被删除。
