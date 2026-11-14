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

## 当前边界

- Runtime 继续独立发布，不依赖能力包、MCP、LangGraph 或某个 Agent。
- 自研 RobotTools 仅做图工具格式和旧错误码适配，旧 agent: 幂等键保持不变。
- 这里开放基础机器人能力，没有把 HotelWorkflow/GuideWorkflow 的可信确认转换为模型可伪造的布尔参数。
- 不同 Agent 能够接入，不等于已支持多个业务应用同时控制同一台机器人；现有酒店/导览的独立租约限制保留。
- 自动启动 Agent、Agent 会话托管、视觉观测系统和失败自动重规划不属于本次能力层提取。
- 已验证真实 MCP SDK/stdio 协议和模拟 runtime；未调用真实模型或真实硬件，也未自动修改用户的 Agent 配置。
