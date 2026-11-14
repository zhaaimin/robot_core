# 架构与模块结构

> 本文由 README（2026-11-14，1058 行）拆分而来，正文未改动，仅补充标题与导语。
> 返回 [README](../README.md) · 相关：[生命周期与安全](lifecycle.md) · [SDK 参考](sdk-reference.md)

## 两种使用形态

同一个 `RuntimeCore` 支撑两种接入方式，公开方法签名一致。

`RobotRuntime` 和 `RuntimeServer` 都把内核托管在专用 daemon 线程的 asyncio 循环上。
应用、SDK 监听、技能和厂商接口的循环不与内核共用。状态机、资源锁和事件总线仍只在
内核循环操作，线程交接由边界模块负责；本方案不使用子进程。

### 嵌入式（同进程）

```python
from robot_agent_runtime.adapters.simulator import Simulator
from robot_agent_runtime.models import SkillCall, TaskRequest
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.sdk import RobotRuntime

simulator = Simulator()
runtime = RobotRuntime(InMemoryTaskRepository(), simulator.skills)
await runtime.start()
try:
    final = await runtime.submit_and_wait(
        TaskRequest(
            task_type="announcement",
            steps=[
                SkillCall(skill="navigation.navigate_to", params={"target": "meeting_room"}),
                SkillCall(skill="audio.speak", params={"message": "The meeting starts at 3 PM."}),
            ],
        )
    )
    print(final.state, final.result, final.error)
finally:
    await runtime.close()
```

### 远程（跨进程，Unix Domain Socket）

启动守护进程：

```bash
robot-agent-runtime serve --socket /tmp/runtime.sock --database /tmp/tasks.db
```

客户端：

```python
from robot_agent_runtime.sdk import RemoteRuntime

runtime = RemoteRuntime("/tmp/runtime.sock")
await runtime.start()
try:
    final = await runtime.submit_and_wait(request)
finally:
    await runtime.close()
```

可运行的完整示例见 `examples/embedded_demo.py`、`examples/remote_demo.py` 和
`examples/precondition_demo.py`。CC 语音的最小调用、回调监听和线程实验分别见
[示例索引](../examples/README.md)。

## Agent 验证项目

Agent 层由共享内核和三个独立消费者应用组成，靠入口区分；它们不是外部 Agent 使用机器人的前提。
独立 [capabilities/](../capabilities/README.md)（dist `robot-agent-capabilities`）由 MCP 与 Python 客户端共用，
仅依赖 runtime，不依赖 Agent、LangGraph 或 MCP。

- [agent-core/](../agent-core/README.md)（dist `robot-agent-core`，import `robot_agent_core`）：共享内核，
  提供 LangGraph 决策循环、工具端口、Agent Skill 宿主、shell 与存储身份解析，不含任何业务领域。
- [agent/](../agent/README.md)（dist `robot-agent-app`，入口 `robot-agent`）：通用机器人 agent，
  后端 `simulator|cc`，通过嵌入式 SDK 或厂商平台验证机器人工具；本身不携带 Skill 内容，
  要用 Skill 必须给 `--skills-dir`。
- [hotel-agent/](../hotel-agent/README.md)（dist `robot-agent-hotel`，入口 `hotel-agent`）：酒店接待与带房 agent，
  后端 `simulator|mcp`，MCP 后端通过独立的 [MCP 桥接项目](../mcp_bridge/README.md) 连接既有 UDS runtime，
  调用链为 HotelTools → HotelWorkflow → MCP → RemoteRuntime；自带接待、交接、护送三个 Agent Skill 包。
- [guide-agent/](../guide-agent/README.md)（dist `robot-agent-guide`，入口 `guide-agent`）：导览 Agent，
  接收唤醒/最终话语事件，从 `venue-guide` Skill 配置地点与介绍，确认后导航并自动播报介绍；后端 `simulator|mcp`。

各项目的框架依赖都只声明在各自 `pyproject.toml`，不加入 runtime 发布包。
通用 Agent 的 RobotTools 只适配统一能力目录和结果；TaskRuntime 与 Observation 契约归能力包所有。
存储策略不再查全局注册表：每个项目自带一份 `StorageProfile` 模块常量（`ROBOT_PROFILE` /
`HOTEL_PROFILE` / `GUIDE_PROFILE`），由 `configuration.resolve_storage` 在装配时解析，`AppConfig.scenario` 字段与
`scenarios/` 包一起删除。`skillkit/host.py` 集中实现 Skills 协议，图只编排模型与动作批次；
`cli.py` 提供参数校验、状态目录约定、错误脱敏与退出码，应用入口各自实现。
已实现决策、执行与观察闭环，模型接口只有一种（兼容 OpenAI 的工具调用，必须提供凭据），
支持模拟器与 CC 语音、交互输入、任务查询和取消、SQLite 图检查点与幂等恢复。
容器独立环境的运行命令与验证边界见各子项目说明。
酒店项目用 `/skills` 查看、`/skill NAME` 按需选择技能包，`--skill NAME` 可限制可见目录；
包内 references 和注册 Python scripts 保留，不按业务阶段强制加载。
它使用模拟入住凭据、可信客人确认、单机器人租约、后台导航与扬声器播报，
完整操作见 [hotel-agent/README.md](../hotel-agent/README.md)。


## 模块结构

仓库包含统一能力包 `capabilities/`、共享 Agent 内核 `agent-core/`、三个 Agent 应用 `agent/`、`hotel-agent/` 与 `guide-agent/`、
`mcp_bridge/` 接入包与 `src/robot_agent_runtime/` 运行时包，分别打包。
依赖方向是协议/应用 → capabilities → runtime；runtime 不反向依赖上层。接入边界见各子项目说明。

```text
capabilities/src/robot_agent_capabilities/
├── client.py        RobotCapabilities、稳定操作、后台提交与有限等待
├── catalog.py       工具目录、白名单、命名和参数 schema
├── observations.py  共享 Observation 与事实转换
├── ports.py         TaskRuntime 客户端 Protocol
├── errors.py        能力层结构化错误
├── __init__.py      SDK 入口
└── py.typed         类型标记
```

```text
agent-core/src/robot_agent_core/
├── graph.py         LangGraph 决策、执行、观察与路由
├── state.py         图状态与稳定动作标识
├── models.py        决策模型 Protocol 与 ChatOpenAI 适配
├── ports.py         图工具 ToolSet，复用能力包 TaskRuntime/Observation
├── errors.py        应用结构化错误
├── validation.py    与装配无关的通用期限校验
├── shell.py         输入、单轮图调用、结果基础信封、错误状态与公共循环
├── config.py        标准库依赖的不可变配置快照（携带 profile）
├── configuration.py StorageProfile、存储/身份解析、重建与启动一致性校验
├── cli.py           参数校验、状态目录约定、模型生命周期、错误脱敏与退出码
└── skillkit/        Agent Skills 宿主，按模块导入不做统一装配
    ├── catalog.py   Skills 目录、校验、受控文档加载与 bundled(package)
    ├── host.py      宿主 schema/指导/handler、脚本指纹与批次状态
    ├── scripts.py   注册脚本执行、指纹与期限校验
    ├── selection.py 用户查看目录与显式技能选择
    └── versions.py  图执行与显式选择共用的正文/版本校验
```

```text
agent/src/robot_agent/          # 通用机器人 agent，入口 robot-agent
├── app.py                      CLI、凭据前提、CC 原生连接所有权与启动装配
├── profiles.py                 ROBOT_PROFILE：后端白名单与固定库文件名
├── application.py              机器人装配、SDK/平台生命周期与交互错误策略
├── robot_tools.py              统一能力的图工具格式与旧操作身份适配
└── __main__.py                 模块入口
```

```text
hotel-agent/src/hotel_agent/    # 酒店 agent，入口 hotel-agent
├── app.py                      CLI、凭据前提、profile 归属校验与启动装配
├── profiles.py                 HOTEL_PROFILE：后端白名单与七个库文件名
├── application.py              酒店装配、可信确认、客人会话与关闭顺序
├── models.py                   酒店服务阶段与业务记录
├── errors.py                   酒店业务错误，保持 HOTEL_WORKFLOW_REJECTED
├── repository.py               业务持久化、版本与机器人租约
├── service.py                  接待门禁、阶段规则、回位、播报和恢复
├── task_coordination.py        带期限的提交、查询、取消与观察原语
├── tools.py                    模型可用酒店工具与业务事实
├── simulator.py                从大厅初始化的酒店导航与播报模拟器
├── backend.py                  HotelBackend 端口、已核验入住凭据与 SimulatedHotel
├── script_bindings.py          按活跃资源指纹选择可批准的发布脚本
├── adapters/
│   └── mcp.py                  MCP 连接装配、必需技能检查与连接生命周期
├── skills/                     三个独立酒店 Skill，含资料和酒店专属脚本
└── __main__.py                 模块入口
```

```text
guide-agent/src/guide_agent/
├── app.py           CLI、配置与模型生命周期
├── profiles.py      独立的导览存储与后端策略
├── application.py   对话队列、停止优先处理与共享图装配
├── ports.py         唤醒/最终话语输入事件端口
├── interaction.py   文本与 NDJSON 有界输入
├── catalog.py       Skill 地点目录、事实快照与地图目标校验
├── tools.py         查找、介绍、带路建议、查询与取消工具
├── service.py       确认、导航、自动介绍、停止与恢复
├── models.py        导览业务快照
├── repository.py    SQLite 事件去重、动作意图与机器人租约
├── runtime.py       模拟器和 MCP 连接装配
├── errors.py        导览结构化错误
├── skills/venue-guide/ 导览方法与可配置资料
└── __main__.py      模块入口
```

```text
mcp_bridge/src/robot_runtime_mcp/
├── server.py        MCP 工具、真实事件批次与 UDS 客户端装配
├── tool_schemas.py  统一能力协议信封、嵌套 schema 引用转换
├── client.py        MCP 任务客户端、事件回放与 stdio 生命周期
├── errors.py        结构化远端错误与传输结果不确定错误
└── __main__.py      robot-runtime-mcp stdio 入口
```

实现按职责归类；根目录只保留公共契约、配置与命令行入口。

```text
src/robot_agent_runtime/
├── models.py              任务、事件、技能数据模型
├── errors.py              结构化错误
├── ports.py               外部能力与 RobotPlatform 装配契约
├── schemas.py             JSON Schema 边界复述，供被简化 schema 的客户端使用
├── options.py             运行时容量、期限和快照配置
├── thread_binding.py      外部对象线程归属配置
├── __main__.py            命令行组装入口
├── kernel/                内核任务执行与状态
│   ├── core.py            生命周期与公开任务 API
│   ├── task_state.py      唯一状态写入点
│   ├── executor.py        步骤编排、超时与停止确认
│   ├── resources.py       资源互斥与隔离
│   ├── events.py          历史回放与实时事件
│   ├── policy.py          准入规则与本地 AdmissionValidator
│   ├── registry.py        技能查找
│   └── recovery.py        重启状态核对
├── hosting/               内核托管与外部调用边界
│   ├── kernel_host.py     内核装配、线程与分阶段关闭、请求和事件交接
│   ├── thread_runtime.py  固定循环线程与真实完成结果
│   ├── isolation.py       外部端口代理与线程分配
│   ├── boundary.py        有界快照与外部结果净化
│   └── execution_context.py  控制容量上下文
├── repositories/          仓储实现
│   ├── memory.py          InMemoryTaskRepository
│   └── sqlite.py          SQLiteTaskRepository
├── transport/             UDS 服务与协议
│   ├── server.py          RuntimeServer
│   └── wire.py            NDJSON 成帧与编解码
├── sdk/                   应用调用接口
│   ├── embedded.py        RobotRuntime 与保留线程绑定的 from_platform 装配
│   ├── remote.py          RemoteRuntime
│   ├── listeners.py       TaskListener
│   ├── task_waiting.py    两种 SDK 共用的提交与终态等待、独立等待期限
│   ├── callback_executor.py  默认同步回调执行器
│   └── listener_execution.py  监听分组/独立循环、容量与回调桥接
└── adapters/              机器人能力实现
    ├── cc_api/            CC 厂商接入包
    │   ├── __init__.py    导出类和工厂，不创建连接
    │   ├── contracts.py   CcApiClient / CcApiResponse 厂商 SDK 协议
    │   ├── platform.py    CcApiPlatform 与 create_cc_api_platform
    │   ├── driver.py      请求响应、订阅分发和 SDK 回调桥接
    │   └── speech.py      语音参数、完成与停止确认
    └── simulator.py       模拟技能、状态源与对账器
```

| 原模块（已移除旧入口） | 当前实现路径 |
| --- | --- |
| `core`、`executor`、`task_state`、`resources`、`events`、`policy`、`registry`、`recovery` | `kernel.<同名模块>` |
| `kernel_host`、`thread_runtime`、`isolation`、`boundary`、`execution_context` | `hosting.<同名模块>` |
| `repository` / `sqlite_repository` | `repositories.memory` / `repositories.sqlite` |
| `server` / `wire` | `transport.server` / `transport.wire` |

表中的路径均以 `robot_agent_runtime.` 开头。旧根目录模块已删除，所有导入和补丁路径
均使用当前实现路径。`sdk` 的公开导入保持一致。
托管层负责代理与内核装配；内核只依赖普通端口及 `AdmissionValidator`，不依赖 `hosting`。


## 内部分工

三个模块的边界是“谁可以写状态”：

- `TaskStateStore` 持有六个以 task id 为键的字典（任务、锁、序号、取消事件、进度任务、
  执行任务），并提供唯一的 `transition()`。它在持有该任务锁的前提下把快照与事件交给仓储
  一次原子写入，再把权威结果回填到活的 `Task` 对象，最后才发布到事件总线。
- `WorkflowExecutor` 自身无状态，只负责顺序：申请资源 → 带超时启动技能 → 把结果、超时、
  取消、意外异常映射成恰好一次终态转换。
- `RuntimeCore` 负责生命周期（启动恢复、关闭）与公开 API，并在提交时完成校验。
- `KernelHost` 拥有内核线程，外部端口代理拥有自己的循环；真实调用结束与“取消请求已发送”
  分开记录。跨线程超时不会被当作物理动作已经停止的证据。

所有带 guard 的转换都走同一把任务锁，因此“取消请求”与“执行推进”是公平竞速，不存在已
取消的任务又被推进成功的窗口。


## 当前范围

已提供独立统一能力 SDK 与原生 MCP 能力工具，外部 Agent 可直接调用，不经自研决策图。
能力包不启动模型或机器人；权限、前置条件、资源互斥、停止确认及重启不重放仍以 runtime 为准。
基础能力接入不授予酒店/导览业务确认权，也不新增跨业务的机器人共同调度机制。

Agent 层由共享内核 `agent-core/` 与三个独立应用 `agent/`、`hotel-agent/`、`guide-agent/` 组成：
LangGraph 工具闭环、模型接口适配、装配层存储/身份解析与被动配置快照、独立 SkillHost、
固定加载边界的工具批次与 CLI 骨架都在内核；通用项目只剩 RobotTools 与 CC 装配，
酒店项目只剩 HotelTools、业务确认、租约与三个 Skill 包；导览项目使用独立 GuideTools 与可配置的 venue-guide Skill。
导览支持唤醒/最终话语输入、地点问答、可信带路确认、后台导航、到达自动介绍和停止；
内置地点仅为演示，真实 MCP 导航需要部署方填入地图目标，尚未连接真实麦克风/ASR 或跟随检测。
`--scenario` 与场景描述符注册表已删除，三个应用各自携带 `StorageProfile` 常量并在启动时
核对快照归属，因此配置快照不能跨 agent 复用；后端白名单收窄为 robot `simulator|cc`、
hotel/guide `simulator|mcp`，通用 agent 不再自带酒店 Skill，要用 Skill 必须给 `--skills-dir`。
酒店可经 stdio MCP 桥接访问既有 runtime，保留原 HotelTools、业务确认和租约；
独立接入包提供技能发现、稳定幂等提交、查询/取消和真实事件批次。桥接进程支持两种传输：
默认 `--mode stdio`（客户端拉起的子进程），以及 `--mode http`（`serve_http` + Streamable HTTP，
端点 `/mcp`，必须提供 Bearer 令牌，默认只监听 `127.0.0.1`）。HTTP 模式用于跨机接入或客户端
无法管理子进程的场景；两种模式暴露同一份工具目录，准入与执行语义完全一致。
发布代码没有离线演示决策实现，命令行测试用的脚本替身分别留在 `agent/tests/scripted_models.py`
与 `hotel-agent/tests/scripted_models.py`。
已加载 Skill 正文与版本只由内核一份共享校验检查；通用模型、工具与脚本执行器的期限校验
独立于任何应用常量，酒店业务实现按需导入。
三份酒店脚本因包名与错误归位改写而指纹变化，旧 pending 脚本拒绝自动恢复；
数据路径、任务身份和 JSON 错误码保持原样。
Agent Skills 通用宿主保留三个独立业务包，用户可选择按需加载、读资料并执行注册脚本；
酒店同名自定义技能可以只有文档；未批准脚本被跳过，控制命令及其他已批准能力继续可用。
不合并整包，也不由酒店阶段硬编码切换正文。酒店应用保留模拟接待到护送闭环、独立客人确认与单机器人占用、
后台导航与独立回位结果；未知停止和待人工归位保留占用。真实 PMS、导航硬件、随行感知与电梯尚未接入。
真实模型协议使用本地 HTTP 测试服务验证，尚未使用真实模型凭据或硬件验证规划质量与动作效果。

源码按 `kernel`、`hosting`、`repositories`、`transport`、`sdk`、`adapters` 职责归类，
根目录只保留公共契约和配置，旧模块入口已删除。
外部代理装配与整体关闭次序由托管层负责，内核支持保留仓储的停止阶段。

已实现：运行时内核、技能层框架、Python SDK（嵌入式 + 远程）、UDS 守护进程、SQLite
持久化、模拟适配器、厂商接入包契约、SDK 通用平台装配入口与 CC 平台工厂、共享 CC 驱动和语音技能、
SDK 统一注册式任务监听（仅本客户端后续提交）、
两种 SDK 的 `submit_and_wait` 终态等待与独立等待期限、
同步回调的有界守护线程隔离与排队期限、监听回调调用方/分组/独立执行配置、
同进程内核专用线程和外部端口共享/分组/独占配置、平台端口线程绑定透传、有限停止确认与失控资源隔离、
任务总期限、控制容量预留、快照限额、事件分页及有界关闭缓冲。

CC 语音示例按最小终态等待、单监听回调、双监听线程实验分开，入口与阅读顺序见
[示例索引](../examples/README.md)；原线程实验入口及参数保持不变。

线程边界保护外部 Python 阻塞或不配合取消时的内核响应，不提供硬实时保证。原生扩展
长期持有 GIL、进程退出或系统资源耗尽仍会影响整个进程；不能强制终止已经运行的线程。

尚未实现：复杂 Agent 规划、多 Agent 协作与失败动作自动重规划、调度器（`priority` 字段已持久化但不参与
排序，无并发上限与抢占）、有限重试、结构化日志、其他真实机器人能力适配（导航、
ROS 2 等）、CC API 结束原因解析与重启对账、回调进程隔离与强制终止、云端管理平面。
