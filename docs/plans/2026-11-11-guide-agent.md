# 导览 Agent 实施计划

**目标：** 新增独立 `guide-agent/`，用户唤醒或说话后交流；从 Agent Skill 读取地点、地图目标和介绍；确认带路后导航，到达后自动介绍。先完整验证模拟器，保留现有 MCP 接入。

**已确认需求：** 不依赖人员靠近感知；地点资料尚不存在，由 Skill 包配置；地图目标为空仍支持问答，真实导航须以后填入 runtime 对应地图目标。用户已要求按此方案执行，不重复审批。当前未提交工作继续原地完成，不提交、不切换分支。

**架构：** `guide_agent` 只依赖 `robot_agent_core` 和 runtime 公共端口，不导入 robot/hotel 应用；MCP 为可选 extra。既有共享图/SkillHost 继续处理模型与工具，导览工作流维护目的地确认、稳定任务标识、机器人占用和介绍结果。语音输入使用应用事件端口（wakeup / utterance 最终文本），提供文本和 NDJSON 适配器，不声称已接入麦克风/ASR；现有 CC 示例可在后续适配此事件端口。

## 固定契约

- 包 `robot-agent-guide`，import `guide_agent`，入口 `guide-agent` / `python -m guide_agent`。后端 simulator|mcp，持久化前缀 guide，独立任务/业务/图检查点，不改变既有包的身份与数据库。
- `skills/venue-guide/SKILL.md` 定义问答/地点确认/介绍流程；`references/destinations.yaml` 包含 destinations 列表，每项 id/name/aliases/map_target/introduction；`introductions.md` 的 `## id` 分节提供详细事实。配置由程序读取校验，拒绝重复键、重复 ID/名称/别名、路径越界、未知引用、过大文本；模型不能提交任意坐标或目标。
- 内置两个明确标注为演示的地点，map_target=null。simulator 使用独立 `demo:<id>` 目标，MCP 必须配置非空 map_target，否则不提交导航；无地图仍可介绍。
- `DestinationCatalog.from_registry(registry, skill_name='venue-guide')` 固定资料快照与 fingerprint，提供 search/describe/get/navigation_target；Destination 为不可变数据。介绍播报使用配置中的正文，不由模型编造地点事实。
- 对话工具 GuideTools：查找地点、读取介绍、提出带路建议、查询、取消。提出建议不移动。应用接受可信 `confirm` 事件或明确的“确认带路”输入后才调用确认入口；确认绑定当前提议，导航期不接受新目的地覆盖。
- `GuideWorkflow(database, runtime, catalog, *, backend, task_timeout_s=60, wait_timeout_s=5)` 提供 start/close/status/request_escort/confirm_escort/cancel/wake/speak/wait_for_settled/claim_event。request_escort/speak 接受 expected_epoch（可选），cancel 增加 epoch，使取消前尚在执行的模型工具无法再次发起动作。所有副作用要求稳定 operation_id。
- 工作流业务持久化先保存动作意图，再调用 runtime；保存真实 task_id 后监控原任务。导航 SUCCEEDED 才自动提交一次介绍；失败/取消/未知不介绍。介绍成功才完成导览。断连、提交响应丢失、停止不确认或恢复缺 task_id 保留占用并待核对；不重放物理动作。导航到达不宣称用户已跟随到达。
- 工作流同一时间仅一个会话占用机器人，问答不占导航租约；重复唤醒不重置导览。任务查询/取消有期限，关闭先阻止新动作，再停止活动任务、清理监控，最后关闭客户端/仓储。
- `InteractionEvent` 带稳定 event_id、session_id、kind、text、final。只处理最终文本，忽略 ASR 中间结果；持久化事件去重。文本模式支持 /wake、/confirm、/stop、/status、/wait、/skills，普通文本为最终话语。NDJSON 为外部 ASR/唤醒事件接入提供同一条路径。
- 应用在模型请求和导航期间继续读取事件；停止优先处理，不等待模型答复，并以 epoch 拒绝迟到模型的动作/播报；普通话语按有界队列串行进入图。导航监控与自动介绍不依赖新的用户输入。
- 新增领域错误走 errors.py；不增加根 runtime 依赖。新应用测试独立配置，不加入历史宿主 sweep，全部进保留容器。

## 文件归属与步骤

- [x] 记录现有源码基线；按 TDD 实现 `guide-agent/src/guide_agent/{errors,catalog,models,repository,service}.py` 和 catalog/workflow 测试，覆盖空地图、歧义、确认、取消、断连、重启不重放、自动介绍和租约。
- [x] 实现 `ports.py` 的 InteractionSource 契约、`interaction.py` 的文本/事件输入与契约测试；事件有界、去重、部分文本不触发模型。
- [x] 实现 tools/app/application/profiles、simulator/MCP 装配，复用共享图，按需加载 venue-guide；模型测试替身仅在 tests。对话自然语言由真实模型处理，确认/停止属于可信输入控制。
- [x] 打包 Skill、pyproject、README；覆盖真实 MCP 子进程和本地 HTTP 模型协议下的完整导览、追问、导航中停止、重复事件与资源版本变化。
- [x] 独立规格审查后做质量审查，修复阻塞项；同步根 README 的 API/状态/模块/范围，容器全套回归、compileall、pip check，干净 wheel 独立安装验证并保留产物。

## 验证

容器 `robot_core_ubuntu24`，新包/三个 Agent 包使用 `/opt/robot_agent_venv/bin/python`，runtime 原套件使用 python3，有界串行回归。

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -c guide-agent/pyproject.toml guide-agent/tests -q --tb=short'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && timeout --kill-after=5s 45s python3 -m pytest -q -o faulthandler_timeout=25'
```

## 已执行的验证与审查

- 唤醒/输入端口先 4 failed；CLI/应用接口先 4 failed。实现后最初导览套件 27 passed，增加恢复与真实 HTTP 模型 + MCP 子进程整链后 37 passed。
- 规格审查发现旧确认绑定、取消后恢复升级 epoch、未知语音意图在导航完成时丢失三项组合边界；新增真实图检查点/SQLite 恢复、冻结 tour_id、跨实例未知意图回归后修复，规格复审通过。
- 新增 /skills 与 /skill 无模型/机器人副作用，导航中问答与停止；在途模型与较早排队确认均不能越过停止边界。
- 已跑既有完整套件：共享内核 73 passed，通用 Agent 101 passed，酒店 Agent 117 passed，MCP 32 passed，runtime 497 passed（10.65s）；已有源码/资源共 124 个文件与实施前 SHA-256 一致。
- 曾构建并独立安装导览 wheel，确认环境不含 robot_agent/hotel_agent，47 项测试全部通过，/skills /wake /status CLI 冒烟通过。质量审查后又补未知语音跨会话隔离回归，最终重新打包和验证结果在下方记录。
- 验收目录 `.verify/guide-agent-baseline-8d2td3tc/`，原文件指纹在 `source-hashes.json`，发布与 CLI 记录使用 `release-verification.json`、`cli-smoke.json`。
- 质量审查补齐独立语音提交未知、已知语音任务未知终态两种跨会话隔离，以及“停止语音不能释放另一会话导航租约”。仓储在同一事务保存会话隔离并拒绝新导航申请；明确停止只清理自己的隔离和租约。相关回归均先 RED 再 GREEN，服务最终 26 passed。
- 最终完整导览源码：53 passed in 2.90s；最终干净 wheel 在不安装 robot_agent/hotel_agent 的隔离环境全套：53 passed in 2.91s。包含本地 HTTP 模型协议、真实 MCP 子进程、完整导航/介绍、导航中追问/停止、旧确认和取消后图恢复。
- 独立规格复审、代码质量复审均通过，无剩余阻塞项；最终 compileall、pip check、git diff --check 通过。现有 runtime、共享内核、酒店与机器人应用及 MCP 的 124 个源码/资源文件哈希不变；根依赖未增加。
- 最终 wheel 在 `final-wheels/robot_agent_guide-0.1.0-py3-none-any.whl`，SHA-256 `4393603fa6badcc69b4b9c12333365acc12ee081c633fbfceb456330a9ef3507`；14 个 Python 文件与源码逐字节一致，Skill 正文、地点 YAML 与介绍 Markdown 均完整。独立环境为 `isolated/`。
- 保留当前工作树、可编辑安装、验收归档与容器，未提交。仅验证模拟器、受控模型 HTTP 服务和输入事件协议，未连接真实模型服务、麦克风/ASR、导航地图或机器人硬件。
