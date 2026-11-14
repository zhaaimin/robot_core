# 导览 Agent

用户唤醒或提供最终话语后开始交流，从 `venue-guide` Skill 读取地点与介绍，经模拟器或 MCP 执行导航与播报。
本项目独立使用 `robot-agent-core`，不依赖酒店或通用机器人 Agent。

流程为：唤醒问候 → 理解询问 → 查询配置地点 → 提出带路建议 → 用户确认 → 导航 → 自动播报介绍。
模型只能提出带路建议，用户说“确认带路”或可信 `confirm` 事件才启动移动。导航期间仍可提问或停止。
机器人导航成功只代表机器人到达，不代表已检测到用户跟随或到达；介绍播报成功后才完成该次导览。

## 安装和启动

所有命令在保留的 `robot_core_ubuntu24` 容器、仓库根目录执行：

```bash
/opt/robot_agent_venv/bin/python -m pip install -e . -e ./capabilities -e ./agent-core -e './guide-agent[test]'
```

准备 `OPENAI_API_KEY` 和 `ROBOT_AGENT_MODEL`，兼容服务可用 `OPENAI_BASE_URL`。应用不会自动读取 `.env`。
生产入口只使用配置的模型接口，没有规则演示模型；测试替身只在 `tests/`。

```bash
/opt/robot_agent_venv/bin/guide-agent --thread-id visitor-1 --data-dir /tmp/guide-demo
```

交互示例：

```text
/wake
展厅有什么？
请带我去展厅
确认带路
/wait
/status
/exit
```

`/wake` 模拟唤醒；普通文本代表 ASR 最终文本。`/confirm` 与“确认带路”确认当前建议；
确认绑定接收输入时已展示的建议编号，在建议出现前提前输入确认会被拒绝，建议替换后旧确认也不会启动导航。
`/stop`、`/cancel`、“停止”、“停止带路”请求停止。`/status` 查询，`/wait` 有限等待导航和介绍。
`/skills`、`/skill venue-guide` 继续使用共享 Skill 宿主。Skill 可由模型按需加载，不按业务阶段强制选择。
输入结束会关闭应用并请求停止活动任务，保持交互输入打开才能持续导览。

## 在 Skill 中配置地点

```text
skills/venue-guide/
├── SKILL.md
└── references/
    ├── destinations.yaml
    └── introductions.md
```

`destinations.yaml` 的每个条目显式提供以下字段：

```yaml
destinations:
  - id: exhibition_hall
    name: 展厅
    aliases: [展区]
    map_target: null
    introduction: 这里展示产品与发展历程。
```

`introductions.md` 用 `## exhibition_hall` 这样的地点 ID 分节，填写供问答读取的详细资料。
到达后自动播报 YAML 中的 `introduction`，问答可引用 Markdown 详细资料；未知资料必须明确说明。
名称和别名不能重复，ID、文本长度、文件大小、未知字段和引用路径都有校验。
修改资料后重启加载新快照；旧提议不能在资料变化后直接确认，已执行动作不会因更新资料而重放。

内置“演示展厅”和“演示服务台”仅供验证，`map_target` 均为空。模拟器使用 `demo:<id>` 目标；
MCP 模式对空目标拒绝导航，但仍可问答和介绍。部署前把它替换为真实地图已经配置的目标标识，
不要把地点名称或模型猜测当成机器人能导航的坐标。

自定义目录通过 `--skills-dir /path/to/skills` 指定，目录必须包含 `venue-guide` 及上述资料。

## MCP 接入

```bash
/opt/robot_agent_venv/bin/python -m pip install -e ./capabilities -e ./mcp_bridge -e './guide-agent[mcp,test]'
```

一个终端运行独立 runtime，以下为模拟器：

```bash
/opt/robot_agent_venv/bin/python -m robot_agent_runtime serve \
  --socket /tmp/guide-runtime.sock --database /tmp/guide-runtime.sqlite3
```

另一个终端运行导览 Agent，使用已填地图目标的资料目录：

```bash
/opt/robot_agent_venv/bin/guide-agent --backend mcp \
  --runtime-socket /tmp/guide-runtime.sock --skills-dir /path/to/skills
```

启动时检查 `navigation.navigate_to` 与 `audio.speak`。应用拥有 MCP stdio 子进程和客户端，
独立 runtime 服务由它的应用管理；退出先关闭导览工作流，再清理连接，不终止独立 runtime。
同一个端点需持续代表同一台机器人和任务历史，更换机器人或任务库时使用新的端点与本地存储。
第一版一台机器人运行一个业务应用，酒店与导览的业务租约彼此独立，不能同时把同一机器人交给两个应用。

## 接入唤醒和 ASR 事件

`--input events` 从 stdin 接受逐行 JSON；这是语音系统的事件边界，未内置麦克风录音、唤醒词或 ASR 引擎。

```json
{"event_id":"wake-001","session_id":"visitor-1","kind":"wakeup"}
{"event_id":"asr-001","session_id":"visitor-1","kind":"utterance","text":"带我去展厅","final":true}
{"event_id":"confirm-001","session_id":"visitor-1","kind":"confirm","proposal_id":"输出中guide.tour_id的值"}
{"event_id":"stop-001","session_id":"visitor-1","kind":"cancel"}
```

也可发送 `status`、`wait`。ASR 中间结果 `final=false` 被忽略，最终结果可沿用同一 ID；
外部界面可在确认事件中显式提交 `proposal_id`，否则绑定此连接最近展示的建议。
最终事件 ID 持久化去重，重发相同内容不会重复动作，复用 ID 改变内容会报错。
输入行最多 64 KiB，话语最多 4096 字符，普通输入队列最多 32 条。停止独立处理并清除该会话较早排队的输入，
同时撤销旧模型请求的动作权限；已经执行的物理副作用仍需 runtime 确认停止。
外部适配器也可实现 `guide_agent.ports.InteractionSource`，把 `InteractionEvent` 送进 `run_application`。

## 持久化和恢复

模拟器的业务库、任务库和图检查点分别为 `guide.sqlite3`、`guide-simulator-tasks.sqlite3`、`guide-checkpoints.sqlite3`。
MCP 本地只保存 `guide-mcp-<endpoint>.sqlite3` 和 `guide-mcp-<endpoint>-checkpoints.sqlite3`，任务库归独立 runtime。
动作先保存意图，再提交带稳定幂等键的任务；重启查询已知 task_id，缺少确认 task_id 或停止结果未知则待核对并保留占用。
重复唤醒不重置导览；不会自动重放未知动作。图有待执行节点时用相同 `--thread-id`、后端和存储 `--resume`。
事件去重记录在执行前提交，发生进程中断后不会自动重放被领取但尚未完成的事件；先查任务、业务状态和图检查点再恢复。

## 模块

```text
src/guide_agent/
├── app.py             CLI、GuideConfig 与模型生命周期
├── profiles.py        独立的存储和后端策略
├── application.py     对话队列、优先停止与图装配
├── ports.py           唤醒和最终话语的输入端口
├── interaction.py     文本与 NDJSON 有界输入
├── catalog.py         Skill 地点事实与地图目标校验
├── tools.py           模型查找、介绍、提议、查询与取消工具
├── service.py         确认、导航、自动介绍、停止与恢复
├── models.py          导览业务快照
├── repository.py      SQLite 事件去重、动作意图与机器人租约
├── runtime.py         模拟器和 MCP 连接装配
├── errors.py          导览结构化错误
├── skills/venue-guide/ 导览指导与可配置资料
└── __main__.py        模块入口
```

## 验证

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -c guide-agent/pyproject.toml guide-agent/tests -q'
```

测试涵盖确认门禁、导航成功后自动介绍、取消、旧模型权限撤销、事件去重、未知停止、重启不重放，
以及真实 ChatOpenAI 客户端连接本地 HTTP 响应并经 MCP stdio 子进程完成导览。
这些证明协议与模拟流程；未验证真实模型推理效果、麦克风/ASR、真实导航地图、用户跟随或硬件效果。
