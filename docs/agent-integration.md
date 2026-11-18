# 各类 Agent 共用机器人能力

框架的基础路径是 Runtime + 统一能力层 + MCP/Python SDK。
Codex、Claude Code 和其他 MCP host 可以使用具体机器人能力工具；自研 Python Agent 直接使用同一能力客户端。
调用不经过仓库自带 LangGraph 决策图；酒店、导览与通用 Agent 都是平台消费者。

## 启动独立 runtime

在保留容器内、仓库根目录安装并启动模拟器 runtime：

```bash
/opt/robot_agent_venv/bin/python -m pip install -e . -e ./capabilities -e ./mcp_bridge
mkdir -p .data
/opt/robot_agent_venv/bin/python -m robot_agent_runtime serve \
  --socket /workspace/robot_core/.data/robot.sock \
  --database /workspace/robot_core/.data/robot.sqlite3
```

保持此终端运行。此命令使用模拟器；连接真实硬件仍需应用自己装配并启动平台，不会自动连接机器人。

## MCP host 配置

宿主 macOS 的 Agent 通过 `docker exec -i` 启动容器内 stdio 桥接。不要使用 `-t`，
不要往桥接 stdout 写日志；模型凭据由各 Agent 自己管理，桥接不需要语言模型凭据。

Codex 的 `config.toml` 配置片段：

```toml
[mcp_servers.robot]
command = "docker"
args = ["exec", "-i", "robot_core_ubuntu24", "/opt/robot_agent_venv/bin/python", "-m", "robot_runtime_mcp", "--socket", "/workspace/robot_core/.data/robot.sock", "--allow-skill", "audio.speak", "--allow-skill", "navigation.navigate_to"]
```

Claude Code 的项目 `.mcp.json` 配置片段：

```json
{
  "mcpServers": {
    "robot": {
      "type": "stdio",
      "command": "docker",
      "args": ["exec", "-i", "robot_core_ubuntu24", "/opt/robot_agent_venv/bin/python", "-m", "robot_runtime_mcp", "--socket", "/workspace/robot_core/.data/robot.sock", "--allow-skill", "audio.speak", "--allow-skill", "navigation.navigate_to"]
    }
  }
}
```

其他 MCP host 使用同一 command/args，配置文件的外围结构遵循各 host 约定。
Agent 与 runtime 同在容器时可以直接执行 Python，不经 docker exec。
实际权限批准与配置加载由 Agent host 管理；仓库的 stdio 验证不代替这些客户端的交互验收。

配置格式依据官方文档（2026-11-14 核对）：
[Codex MCP](https://developers.openai.com/codex/mcp/) 和
[Claude Code MCP](https://code.claude.com/docs/en/mcp)。

## 工具调用

`tools/list` 同时包含五项原任务工具和 allowlist 内的具体能力。例如调用 `robot_speak`：

```json
{
  "operation_id": "visit-42-greeting-v1",
  "params": {"message": "欢迎光临"},
  "wait": false
}
```

operation_id 是调用方保存的稳定动作身份；params 是目录中 SkillDefinition 的原参数。
二者分开可避免用户技能也有 operation_id、wait 等参数时发生覆盖。
默认只受理，返回 `tool/task_id/state/result/error/requires_attention`。
`wait=true` 可做有界等待，等待超时仍保留 task_id。

使用 `robot_task_status({task_id})` 查询，使用 `robot_cancel_task({task_id})` 显式请求停止。
两项控制工具取得权威任务快照时 isError=false，即便该任务本身 failed/canceled/需要对账；
控制调用失败才返回 isError=true。技能工具返回执行错误或等待错误时 isError=true，且保留实际任务句柄。
任何工具的 isError=false 都不能代替检查 Task.state 和业务完成条件。

请求或连接失败后保持原 operation_id 与参数，不得换 ID 重发物理动作。
查询和取消使用已有 task_id；没有确认 task_id 时可用原 ID 和同参数请求原任务，或由应用对账。
低层 `submit_task`、`get_task_status`、`cancel_task`、`read_task_events` 的原请求和响应信封保持不变。

## Python SDK

```python
from robot_agent_capabilities import RobotCapabilities

# runtime 是应用已启动的 RobotRuntime、RemoteRuntime 或 McpRuntime。
robot = await RobotCapabilities.create(runtime, allowed_skills={'audio.speak'})
observed = await robot.invoke(
    'robot_speak', {'message': '欢迎光临'}, operation_id='visit-42-greeting-v1', wait=True,
)
```

这与 MCP 具体能力使用同一目录、任务请求、robot: 幂等命名空间和结果转换。
SDK 的 operation_id/wait 使用关键字参数，MCP 将它们放入协议信封；业务 params 的 schema 相同。
RobotCapabilities 不负责 runtime 连接、厂商平台或模型的 start/close。

### Python 主动事件

需要注册一次后继续 invoke 时，分别使用任务和平台监听：

```python
async def handle_done(task):
    print('任务终态', task.id, task.state, task.result, task.error)

async def handle_platform(event):
    print('设备事实', event.event_type, event.data)  # 故障也在这里处理。

task_listener = robot.add_task_listener(on_done=handle_done)
platform_listener = robot.add_platform_listener(on_event=handle_platform)
try:
    await robot.invoke('robot_speak', {'message': '欢迎光临'}, operation_id='greeting-43')
    await task_listener.wait()
finally:
    await robot.close_listeners()
```

这里 runtime 已配置平台事件源。任务监听默认只跟踪本能力客户端注册后的 invoke，旧任务
使用 task_ids；on_done 包含成功 / 失败 / 取消等完整终态 Task。平台监听包含设备故障，
不因此调用监听错误回调。on_error 可选，省略时由事件循环报告观察 / 回调异常；任务回调
形参为 `(task_id | None, error)`，平台为 `(error)`。正常注销不触发 on_error。
close_listeners 只释放本能力客户端监听和共享回调池，之后不再新增监听；runtime 仍由应用
关闭。完整接口、线程和恢复边界见 [能力监听说明](../capabilities/README.md#注册任务和平台监听)。
当前 McpRuntime 支持此任务监听，但不提供平台或全任务实时接口。

两个监听入口均可设置 execution='caller'（默认）、'group' 或 'dedicated'。例如给任务与
平台同时传 execution='group', group='reporting'，就会在本能力客户端共用一个回调 loop。
同步函数、异步函数和返回的 awaitable 均在所选隔离 loop；订阅仍留在业务观察 loop。
同名组不跨能力客户端或底层 SDK 共享。需要直接访问业务 loop 的 WebSocket、队列等对象时
保持 caller，或通过线程安全桥接通知其所属 loop；不能从组 loop 直接复用这些对象。
资源预算通过 RobotCapabilities.create(..., listener_options=RuntimeOptions(...)) 配置。

Agent 可以通过同一个能力客户端接收任务终态和平台事实。已知任务用历史回放避免错过快速完成；
下面 runtime 同时配置了平台事件，task_id 与两个已处理位置由应用保存：

```python
from contextlib import aclosing
from robot_agent_runtime.models import Event

async with aclosing(robot.subscribe_events(
    sources={'task', 'platform'}, task_ids={task_id},
    after_cursor=processed_platform_cursor,
    after_sequences={task_id: processed_task_sequence}, task_terminal_only=True,
)) as events:
    async for event in events:
        print(event.event_type, event.data)
        if isinstance(event, Event):
            print(event.task_id, event.sequence)  # 按任务保存已处理序号。
        else:
            print(event.cursor)  # 分别保存已处理的平台游标。
```

门面保留原始 `Event | PlatformEvent`，默认仍只订阅平台。全任务监控省略 task_ids，使用可选
全任务实时流，没有就绪握手、历史补收或跨任务游标承诺；可靠结果通知优先指定任务 ID。
任务终态包括失败 / 取消 / 未知 / 待对账，不代表物理动作已停止；需要完整结果时查询任务快照。
退出订阅不关闭共享连接或取消物理任务，不自动重试、重连或激活模型。
`allowed_skills` 只限制工具，事件可见范围须在
应用 / 网关单独控制。无事件接口的后端明确抛 `CAPABILITY_EVENTS_UNSUPPORTED`；
原始网络中断报告 `CAPABILITY_EVENT_TRANSPORT_FAILED`，运行时结构化错误保持原码与详情。
当前 `McpRuntime` 可观察明确 task_ids，但没有平台 / 全任务实时接口；缺少所请求来源会明确
报错，不静默降级。Python 门面不自动改变 MCP 的工具或通知协议。

## 当前边界

- Runtime 继续独立发布，不依赖能力包、MCP、LangGraph 或某个 Agent。
- 自研 RobotTools 仅做图工具格式和旧错误码适配，旧 agent: 幂等键保持不变。
- 这里开放基础机器人能力，没有把 HotelWorkflow/GuideWorkflow 的可信确认转换为模型可伪造的布尔参数。
- 不同 Agent 能够接入，不等于已支持多个业务应用同时控制同一台机器人；现有酒店/导览的独立租约限制保留。
- 自动启动 Agent、Agent 会话托管、视觉观测系统和失败自动重规划不属于本次能力层提取。
- 已验证真实 MCP SDK/stdio 协议和模拟 runtime；未调用真实模型或真实硬件，也未自动修改用户的 Agent 配置。
