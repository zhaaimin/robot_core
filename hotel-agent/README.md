# 酒店前台接待与机器人带房验证版

本项目验证一个 Agent 完成接待信息确认，再指挥单台机器人带领客人到已确认房间的服务闭环。
酒店系统使用模拟实现，机器人任务默认使用模拟器，也可经 MCP 接入独立 runtime；决策模型必须是已配置的真实模型接口，
应用不再提供规则替身。
没有接入真实 PMS、实名核验、支付、门卡、导航硬件、客人随行检测或电梯。

## 代码职责

这是一个独立的 Agent 项目（dist `robot-agent-hotel`，import `hotel_agent`，入口 `hotel-agent`），
不是通用机器人 agent 的一个场景，也不是多个 Agent 协作。`application.py` 负责装配，
复用共享内核 `robot_agent_core.graph` 的 `decide → execute → observe` 循环；每位客人的会话和检查点独立。

| 位置 | 职责 |
| --- | --- |
| `robot_agent_core.graph`、`state`、`models` | 通用决策、工具循环、会话状态与决策模型接口 |
| `robot_agent_core.config`、`configuration` | 不可变解析快照、`StorageProfile`、显式重建和启动校验 |
| `robot_agent_core.validation` | 与装配无关的通用期限参数校验 |
| `robot_agent_core.shell`、`cli` | 共享输入、单轮调用、交互循环、结果信封与退出码 |
| `hotel_agent/profiles.py` | `HOTEL_PROFILE`：允许后端 `simulator|mcp` 与固定库文件名 |
| `hotel_agent/app.py` | CLI、凭据前提、profile 归属校验与启动装配 |
| `hotel_agent/application.py` | 将酒店能力与会话接到通用图，处理可信客人确认 |
| `robot_agent_core.skillkit/host.py` | Skill 工具 schema、参数、指纹冻结/检查、宿主 handler 与批次状态 |
| `hotel_agent/skills/` | 三个独立的任务指导、参考资料与注册脚本包，供 Agent 按需加载 |
| `hotel_agent/{tools,service,repository,backend,simulator}.py` | 酒店业务支持：工具、规则、业务状态、存储、系统端口与模拟实现 |
| `hotel_agent/errors.py` | 酒店业务错误；JSON 错误码保持 `HOTEL_WORKFLOW_REJECTED` |
| `hotel_agent/adapters/mcp.py`、`robot_agent_capabilities.ports` | MCP 连接装配、必需技能检查与共享 TaskRuntime 端口（agent-core 复用） |
| `robot_agent_runtime` | 接收机器人任务，处理执行、资源、取消与恢复 |

`hotel_agent/models.py` 保存业务数据，不是语言模型；`hotel_agent/service.py` 的 `HotelWorkflow`
检查接待、同意、就绪与机器人占用等前提，不负责模型推理或选择 Skill。
导航请求由 Agent 经注册脚本（可选）、`HotelTools`、`HotelWorkflow` 提交给 runtime。
装配依赖内核的 config/shell，不导入本项目的 `app`；客人确认、切换、业务快照与关闭顺序由酒店装配负责。
三个 Skill 中的脚本是酒店专属适配；通用宿主不因此依赖酒店领域。
酒店类型和错误从各自模块导入，包入口仅保留说明。
宿主与图分别负责技能协议和模型/批次编排；同一批次刚加载的技能不能立即读取资源、执行脚本或开放业务工具。

## 通过 MCP 连接独立 runtime

MCP 后端保留当前酒店 Agent：模型调用 HotelTools，HotelWorkflow 管理可信确认、租约与业务阶段，
任务调用通过 MCP stdio 桥接到既有 UDS runtime。它不向模型开放绕过酒店规则的 RobotTools。

在容器内从仓库根目录安装可选接入包：

```bash
/opt/robot_agent_venv/bin/python -m pip install -e ./capabilities -e ./mcp_bridge -e './hotel-agent[mcp,test]'
```

先在一个终端启动独立 runtime。以下使用现有通用模拟器验证，实际部署使用已经装配好导航和播报技能的 RuntimeServer：

```bash
docker exec -it robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m robot_agent_runtime serve --socket /tmp/hotel-runtime.sock --database /tmp/hotel-runtime.sqlite3'
```

再在另一个终端启动酒店 Agent（模型凭据仍必需）：

```bash
docker exec -it robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && set -a && . agent/.env && set +a && /opt/robot_agent_venv/bin/hotel-agent --backend mcp --runtime-socket /tmp/hotel-runtime.sock --thread-id guest-mcp-001 --data-dir /tmp/hotel-mcp-agent'
```

应用自动启动 `python -m robot_runtime_mcp --socket ...` 作为 MCP 子进程，退出时清理桥接连接；
runtime 服务由第一个终端独立管理。酒店交互步骤与下方一致，包括 `/skills`、显式选择、接待、确认、护送与回位。
MCP 服务本身也可供其他本机 MCP 客户端以 stdio 启动，接口见 [桥接说明](../mcp_bridge/README.md)。

启动会核对 `navigation.navigate_to` 与 `audio.speak` 是否可见；缺失时在打开业务库前拒绝运行。
目标 runtime 需提供与 HotelBackend 目的地映射一致的导航与扬声器契约；当前酒店系统适配器仍是模拟入住凭据和同楼层配置。
本轮验证使用模拟机器人，不代表真实地图、PMS 或硬件已接入。

任务提交保留原稳定幂等键，查询/取消保留权威 TaskState；后台监控通过只读有界批次取得真实事件和原序号。
等待超时或 MCP 请求取消只结束观察，物理停止必须使用显式取消并以 runtime 结果确认。
断连、丢失提交结果或不能确认停止时，既有业务逻辑保留占用并进入待核对，不自动重试导航。

## 启动与完整演示

宿主执行（模型凭据按 [接入真实模型](../agent/README.md#接入真实模型) 准备，缺失时启动即以配置错误失败；
`agent/.env` 由两个项目共用，只作为 shell 环境变量来源）：

```bash
docker exec -it robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && set -a && . agent/.env && set +a && /opt/robot_agent_venv/bin/hotel-agent --thread-id guest-selectable-001 --data-dir /tmp/hotel-selectable-demo'
```

依次输入。`/skills`、`/skill NAME`、`/consent`、`/start`、`/arrived`、`/status`、`/wait`、
`/cancel`、`/guest` 由应用输入边界处理；`入住 DEMO-001`、`酒店信息`、`转人工 原因` 交给模型判断，
不保证每次调用同一工具：

```text
/skills
/skill frontdesk-reception
入住 DEMO-001
/skill guest-handoff
/consent
/skill escort-assistance
/start
/wait
/arrived
/wait
/status
```

`DEMO-001` 模拟已由酒店系统核验完成的入住凭据，对应房间 0108；`DEMO-002` 对应 0109。
它们不是实际酒店订单，也没有真实入住办理、身份或支付效力。
酒店模拟器初始位置明确为 `hotel-lobby`，会合点是“大堂前台会合点”。
这是初始化配置，不表示已用传感器验证机器人和客人在场。

| 输入 | 发生的事情 | 预期业务状态 |
| --- | --- | --- |
| `/skills` | 只列可见技能、当前选择和已加载记录 | 不查询业务，输出 `hotel: null` |
| `/skill NAME` | 用户显式加载并选择该技能，不调用模型或业务 | 不改变业务，输出 `hotel: null` |
| `入住 DEMO-001` | 工具查询模拟酒店的已核验凭据，取得受控房间与地图目的地 | `awaiting_consent` |
| `播报 大堂前台会合点在入口右侧` | 模型调用 `hotel_announce`，扬声器读出这句话 | 阶段保持 `awaiting_consent` |
| `/consent` | 应用记录客人同意；Agent 分派空闲管家 | `waiting_for_guest` |
| `/start` | 应用记录客人准备好；Agent 提交后台导航任务 | `escorting`，可能很快进入下一状态 |
| `/wait` | 显式等待已存在的导航任务，最多 2 秒 | `awaiting_arrival_confirmation` |
| `/arrived` | 应用记录客人到达；服务完成，独立启动回位任务 | `completed` |
| `/wait` | 显式等待独立回位任务 | `completed`，机器人 `available` |
| `/status` | 查询当前客人的实际状态，不调用模型 | 保持实际状态 |

最后应看到 `phase=completed`、`arrival_confirmed=true`，正常回位后为 `robot_status=available`。
状态输出另含 `active_skill`、`loaded_skills` 和只包含名称、描述的 `skill_catalog`。
技能选择和查询目录不产生业务快照；使用 `/status` 才查询权威酒店状态。
`exit`、`quit`、`/exit` 或 EOF 结束应用。

## 多客人和后台任务

护送提交后由 runtime 和后台事件监视继续处理，前台不必等待物理任务结束才能接待下一位。
交互中使用 `/guest guest-002` 切换到独立客人会话：

```text
/guest guest-002
/skill frontdesk-reception
入住 DEMO-002
/skill guest-handoff
/consent
```

每位客人有独立图状态和酒店服务记录；模型工具不能指定或读取其他会话。
单机器人仍被第一位客人占用时，第二位客人的分派请求会被拒绝，不能重复占用。
本版不提供排队自动分派或多机器人调度，需在实际可用后重新请求或转人工。

`/guest` 是本地操作员的会话切换入口，不是生产环境的住客身份认证。
部署到前台终端或机器人时，必须由受信界面将住客会话与实际接待记录绑定。

## Agent Skills

三个独立能力包随应用打包，每个都保留“任务指导 + 按需资料 + 实际脚本”的组织方式：

```text
src/hotel_agent/skills/
├── frontdesk-reception/
│   ├── SKILL.md
│   ├── references/{hotel-information,reception-rules,reception-request-schema}.md
│   └── scripts/query_reception.py
├── guest-handoff/
│   ├── SKILL.md
│   ├── references/{handoff-rules,handoff-request-schema}.md
│   └── scripts/assign_butler.py
└── escort-assistance/
    ├── SKILL.md
    ├── references/{escort-rules,escort-request-schema}.md
    └── scripts/start_escort.py
```

| 技能 | 独立职责 |
| --- | --- |
| frontdesk-reception | 酒店信息、入住凭据核验和接待说明；不分派或启动机器人 |
| guest-handoff | 在已有接待和客人同意的前提下分派机器人、说明会合点；不启动导航 |
| escort-assistance | 依据已有分派与准备确认，启动/观察护送，处理到达、取消和协助 |

启动只提供目录名称和描述。用户通过 `/skill NAME` 选择加载，通用 `load_skill` 模型工具也继续保留。
业务状态只作为执行事实，不指定 `required_skill`，不会把三个包一起载入或按阶段强制切换。
可以直接选择任一技能；该技能只检查后端业务前提，不要求先加载另外两个包。
缺少房间、同意、分派或准备确认时会提示缺口或拒绝请求，选择技能不会补造这些事实。

`query_reception.py` 接受 receipt_code 并核验；`assign_butler.py` 只接受空对象并分派；
`start_escort.py` 只接受空对象并启动已有分派的护送。每份脚本属于各自的独立技能。
它们是宿主调用的真实 Python async 函数，不是任意命令行执行器。
脚本得到受控 ToolSet 和宿主生成的 operation_id，不获得直接设置客人确认的接口。

`--skills-dir PATH` 可提供任意非空合法技能目录；`--skill NAME` 可重复，只暴露指定技能。
不传 `--skill` 时三项均可见但没有默认选择；只有接待技能的配置示例：

```bash
/opt/robot_agent_venv/bin/hotel-agent --skill frontdesk-reception
```

加载另一项隐藏技能会报配置错误。减少可见目录后，已有会话若包含不可用技能版本，应使用新会话，
不自动丢弃旧指令或待执行动作。技能可见性不是业务权限，原有业务校验始终存在。
自定义技能可以使用开放的业务工具；只对可见包中的三份发布脚本建立绑定，
且活跃资源必须可读取、源码指纹与发布版本一致。纯文档包不必提供脚本；
脚本缺失、不可读取或被替换时，只跳过该入口并向 stderr 输出说明，文档加载、
`/skills`、`/status`、`/cancel` 及其他已批准脚本仍可使用。
调用未绑定入口返回 `AGENT_TOOL_NOT_ALLOWED`，不会执行替换源码，也不会回退执行发布脚本。
替换脚本若需执行，必须由应用另行批准注册。不能将任意 Skill 的脚本直接交给 shell。
`run_skill_script` 工具的存在不表示目录中每项技能都有已批准脚本；处理方法应依据技能资源与实际工具结果。

Skill 名称必须匹配文件夹，YAML 必须有 name / description；文档和读取的文本资源上限为 64 KiB。
资源路径被限制在该技能包内，拒绝路径/符号链接逃逸、二进制以及字节码和隐藏文件。
正文由 Registry 校验后缓存，参考文本按请求读取并记录指纹；读取资源本身不授予执行权限。
脚本在注册、调用和恢复前校验指纹，加载前或同轮刚加载都不能执行业务动作。

## 模型与工具

酒店 Agent 只运行已配置的真实模型：`hotel-agent` 启动就需要 `ROBOT_AGENT_MODEL` 与
`OPENAI_API_KEY`，没有不接模型也能走通工具的规则模式。
界面确认命令（`/consent`、`/start`、`/arrived`）仍由应用输入边界处理、不经过模型，
所以客人确认不会由模型代答。
技能选择、参考资料读取与脚本执行顺序都由模型决定，宿主只校验技能是否已加载、脚本指纹是否一致。
曾经只识别固定命令的规则替身已移出发布代码，只保留在 `tests/scripted_models.py`，
由 `tests/conftest.py` 注入，用于让命令行测试无需模型凭据运行。

酒店 Agent 开放：

| 工具 | 作用 |
| --- | --- |
| `hotel_confirm_reception(receipt_code)` | 查询已核验凭据，确认房间与地图目的地 |
| `hotel_assign_butler()` | 在已同意带路的情况下分派管家 |
| `hotel_start_escort()` | 在客人准备好后提交异步护送导航 |
| `hotel_announce(message)` | 让机器人播报一条已核验或已配置的信息，返回扬声器任务结果 |
| `hotel_status()` | 读取当前客人的权威业务状态 |
| `hotel_cancel_escort()` | 请求取消并取得实际停止结果 |
| `hotel_request_human(reason)` | 记录模拟人工协助请求，不代表员工已接单 |
| `load_skill(name)` | 读取注册的 Agent Skill 指导 |
| `read_skill_resource(name, path)` | 读取 Skill 内的参考资料或脚本源文本 |
| `run_skill_script(name, path, args)` | 运行对应包的 query_reception / assign_butler / start_escort 入口 |

读取 Skill 之前只提供宿主工具；加载后的业务工具继续受 HotelWorkflow 校验。
脚本调用结果保留原业务 state、task_id、result、error，并在 execution 中记录技能、路径与批准指纹。

模型工具不能设置房间、地图坐标、机器人 ID 或客人确认字段。
酒店工具的 `accepted` 只表示调用受理；最终完成必须看业务 `phase=completed` 与
`arrival_confirmed=true`。单纯的 runtime 导航成功只进入等待客人到达确认。
播报不进入阶段机，也不占用机器人租约：`hotel_announce` 的 `result` 是扬声器任务输出，没有 `phase` 字段，
既不代表客人同意、到达，也不代表带房完成；等待超时只返回可查询的任务，不重播、不隔离机器人。

## 可信确认与停止

`/consent`、`/start`、`/arrived` 由应用输入边界记录，不是模型工具。
聊天中说“我同意”、引用字符串 `/start`，或模型构造 `guest_ready=true` 都不能替代界面事件。
未来按钮可映射到同样的应用事件；首版提供 CLI。

- `/cancel` 是优先控制入口，待执行图检查点存在时也可以请求取消。
- `/status`、`/wait` 不调用模型，也不新增物理任务。
- `/skills` 不读取技能正文或查询业务；`/skill NAME` 不执行模型、分派或移动。选择错误保留原选择。
- 未完成图节点存在时，新的确认或普通输入会被拒绝，要求显式 `--resume`。
- `转人工 原因` 由模型选择人工协助工具；只记录模拟请求，不假装联系了真实员工。

机器人状态需要区分：

- `available`：未出发的分配已取消，或已确认成功回位，可以重新分派。
- `needs_reposition`：移动已确认停止，但尚未确认回到会合点；保留占用，等待人工确认位置。
- `quarantined`：提交结果或停止结果未知，保留占用并等待核对。

取消不会自动引发新的回位动作；返回失败或取消不撤销已完成的客人服务。
验证版没有自动解除后两种占用的接口，不应通过重启或删除数据库假装物理状态已确认。
已取消或已完成的服务也可记录人工协助原因，不改写原结果、不释放占用、不启动新的移动。
发生工具失败时不继续执行动作；模型最多获得一次没有工具的解释机会，可依据 Skill 补问或说明求助方式。
它不能在该回复阶段实际执行新的人工请求，也不能宣称请求已经提交；需要后续有工具回执的操作。

## 持久化与退出

模拟器模式的酒店数据与普通机器人演示隔离：

- `hotel.sqlite3`：业务阶段、确认、持久化导航意图和单机器人租约。
- `hotel-selectable-skills-checkpoints.sqlite3`：独立可选技能的会话消息、已加载正文、资源使用和待执行脚本版本。
- `hotel-simulator-tasks.sqlite3`：runtime 任务与事件。

MCP 模式本地只保存 `hotel-mcp-<端点摘要>.sqlite3` 与 `hotel-mcp-<端点摘要>-checkpoints.sqlite3`，
分别为业务库和图检查点。任务库归远端 runtime 所有，AppConfig.task_database 为 None。
摘要与 runtime_id 来自规范化 socket 路径和 `HOTEL_PROFILE.name`；修改 runtime_socket 后用 rebuild_config 重建。
同一个 endpoint 必须持续代表同一台机器人及其任务历史；更换机器人或替换远端数据库时使用新 endpoint 和新的本地业务/检查点存储。
不要把旧 endpoint 指向另一份历史再恢复旧会话。模型凭据不会传给 MCP 子进程。

文件名策略集中在 `hotel_agent/profiles.py` 的 `HOTEL_PROFILE`，启动装配一次解析进配置；
酒店应用直接使用 checkpoint_database / business_database / task_database。
已有文件名与 runtime_id 字节保持不变，本次不迁移、不改名到 legacy hotel-checkpoints.sqlite3。
应用代码修改后端、profile、连接或数据目录后，应通过 robot_agent_core.configuration.rebuild_config 重算；陈旧解析结果会在创建资源前被拒绝。

房间由酒店适配器返回，并校验为当前支持的同楼层配置。提交前保存意图，使用
`hotel:<service_id>:escort` / `hotel:<service_id>:return` 作为稳定幂等键。
恢复先查询原 task_id，不重放导航；已有意图但没有确认的 task_id 时转待核对。

保留这组数据库，使用相同 `--thread-id` 与 `--data-dir` 继续会话。
有图节点待恢复时使用 `--resume`；不同 agent 或机器人身份不能混用检查点。
模拟设备状态每次进程启动重新初始化，数据库不会恢复实际位置，不能以旧确认代替新的物理核验。
旧 `hotel-checkpoints.sqlite3` 和合并包 `hotel-skill-host-checkpoints.sqlite3` 保留但不自动迁移激活记录，
酒店业务与 runtime 任务数据库继续保留，不重放旧物理动作。
新的待执行脚本固定决策时源码指纹；升级脚本但未修改 SKILL.md 也不能让旧决策自动执行新代码。
酒店错误类从通用 errors.py 归位到 `hotel_agent/errors.py`，以及项目独立化后脚本 import 改为
`hotel_agent.*` / `robot_agent_core.*`，都使三份发布脚本的源码指纹发生有意变化。
此次升级前保存的待执行脚本在当前版本下返回 `AGENT_CONFIGURATION_INVALID`，不执行旧指令、自动刷新指纹或重放动作。
`/skills`、`/status`、`/cancel` 仍可用于查看或处理实际状态；之后由应用方核对既有任务和业务事实再重新规划。
酒店业务和 runtime 数据库文件名、运行身份算法及已持久化错误码保持不变。
自定义目录若复制的是旧发布脚本，启动时只跳过其脚本绑定；需同步当前发布脚本或由应用重新批准，技能正文仍可加载。

退出时先让 HotelWorkflow 通过任务客户端停止活动任务并保存结果，再关闭业务仓储和连接。
模拟器模式随后关闭嵌入式 runtime；MCP 模式只关闭桥接子进程，独立 runtime 服务继续运行。
单次 `--prompt` 模式结束也会关闭嵌入式 runtime，因此长任务请用交互模式保持应用运行。

## 实现与验证边界

业务规则在 `hotel_agent/service.py`，事务租约和乐观版本在 `hotel_agent/repository.py`，
工具门面在 `hotel_agent/tools.py`，只提供业务事实与确认/完成约束，不再选择 Skill。
技能资源宿主在共享内核 `robot_agent_core/skillkit/{catalog,scripts,host}.py`，
注册发布脚本的应用适配在 `hotel_agent/script_bindings.py`；业务变化不要求修改通用图。
`HotelBackend` 是模拟酒店到真实酒店系统的适配边界；`HotelSimulator` 仅复用已有模拟技能契约的导航与播报。

已覆盖加载与路径边界、真实 SDK 下的模拟护送、客人确认门禁、两位客人争用、
导航与到达确认分离、回位与取消、未知结果、持久化恢复和独立会话，以及播报不推进业务阶段。
独立技能、用户选择与资源边界经过测试；宿主还通过真实 ChatOpenAI 客户端与本地 HTTP 响应测试，
验证模型响应选择 Skill、读取引用、执行脚本、获得真实模拟业务结果，以及失败回复不开放工具。
这种受控协议测试不能证明真实部署模型的接待判断效果。

2026-11-08 目录迁移验收：Agent 176 passed；runtime 独立串行完整重跑 497 passed。
三个技能通过格式校验，最终安装包包含 3 份正文、7 份资料、3 份脚本；
安装后验证了显式选择、单技能目录过滤、无业务副作用及完整护送回位。

同日可选脚本修复验收：Agent 186 passed，runtime 497 passed；纯文档和替换脚本目录下的单次/交互控制命令通过，
bundled 完整模拟流程仍为 completed / arrival_confirmed=true / robot_status=available。

同日依赖解耦与错误归位验收：Agent 209 passed，runtime 497 passed；独立安装包通过完整酒店流程、单技能过滤、
同名纯文档/替换/旧发布脚本目录与机器人播报检查。旧指纹恢复被拒，新指纹恢复保留原 operation_id。

2026-11-09 配置解析与 SkillHost 验收：源码和独立安装包的 Agent 全套均 251 passed，runtime 497 passed。
安装包使用真实模型客户端连接本地 HTTP 响应服务跑通完整模拟旅程；数据库文件名、四组固定 runtime_id、发布脚本指纹保持不变。
这类受控协议验收不代表已验收外部模型的业务判断效果。

2026-11-10 播报能力接入验收：Agent 263 passed，runtime 497 passed，`src/robot_agent_runtime` 源码未改动。
`HotelSimulator` 复用 runtime 的 `audio.speak` 契约，`HotelWorkflow.announce` 经同一嵌入式 runtime
提交播报任务但不进入阶段机、不占用机器人租约，`hotel_announce` 由 frontdesk-reception 指导使用。
交互流程 `/skill frontdesk-reception` → `播报 大堂前台会合点在入口右侧` 已用真实装配与酒店任务库跑通，
`tests/scripted_models.py` 增加了对应的脚本替身规则；播报内容的取舍仍属真实模型判断，本轮未做模型效果验收。

2026-11-10 两 agent 拆分验收：`hotel-agent` 独立项目 117 passed，共享内核 `agent-core` 73 passed，
通用 `agent` 101 passed，runtime 497 passed；`src/robot_agent_runtime` 与 `mcp_bridge` 源码未改动。
入口改为 `hotel-agent`，`--scenario` 参数已删除，后端由 `HOTEL_PROFILE` 限定为 `simulator|mcp`。
持久化身份逐字不变：四组 runtime_id 摘要、七个库文件名与 `hotel:{service_id}:escort|return` 幂等键
全部由 digest 测试钉住；输出信封的 `'scenario': 'hotel'` 改名为 `'agent': 'hotel'`。
容器内实跑 `hotel-agent --prompt /skills` 得到 `"agent": "hotel"` 与三份酒店 SQLite 文件。
三份发布脚本的 import 随项目独立化改写，源码指纹因此变化：旧 pending 脚本决策返回
`AGENT_CONFIGURATION_INVALID`，不执行旧指令、不自动刷新指纹，需应用方核对任务与业务事实后重新规划。

## 测试

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -q hotel-agent/tests'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -q agent-core/tests'
```

`tests/conftest.py` 为每个用例注入假凭据与酒店脚本决策模型，测试不需要语言模型凭据。
安装 `mcp` extra 后，酒店用例覆盖真实 stdio 子进程、完整旅程、取消、断连待核对和重连幂等。

当前容器未配置 OPENAI_API_KEY / ROBOT_AGENT_MODEL，因此没有进行真实模型行为效果验收。

尚未验证真实模型的酒店任务表现、真实 PMS、真实导航、住客跟随感知或电梯。
本项目不提供 `cc` 后端：现有真实 CC 平台只有语音能力，而酒店需要导航与播报两个技能，
`HOTEL_PROFILE.allowed_backends` 因此只有 `simulator` 与 `mcp`。
