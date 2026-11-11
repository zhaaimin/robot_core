# robot-agent-mcp

把已有 `robot-agent-runtime` 的 Unix socket 暴露为 MCP stdio 服务。独立发布包
`robot-agent-mcp`，Python 3.11+，使用官方 `mcp>=1.30,<2`。内核不依赖 MCP、Agent
或 LangGraph；此包也不导入酒店 Agent。

在仓库根目录安装：

```bash
python -m pip install -e .
python -m pip install -e './mcp_bridge[test]'
```

先在一个终端启动独立 runtime（以下使用模拟器）：

```bash
mkdir -p .data
python -m robot_agent_runtime serve \
  --socket "$PWD/.data/robot.sock" --database "$PWD/.data/runtime.sqlite3"
```

MCP host 在另一个进程启动桥接；stdio 仅承载 MCP 消息，诊断写 stderr：

```bash
python -m robot_runtime_mcp --socket "$PWD/.data/robot.sock" \
  --allow-skill audio.speak --allow-skill navigation.navigate_to
# 等价入口：robot-runtime-mcp --socket ...
```

不传 `--allow-skill` 时暴露全部技能。白名单同时过滤技能发现和每个提交步骤；它不改变
runtime 的权限、前置条件和资源准入规则。桥接连接现有 RuntimeServer，退出时只关闭自己
的 RemoteRuntime 连接，runtime 和已经受理的物理任务继续运行。

| MCP tool | 参数 | 结构化成功结果 |
| --- | --- | --- |
| `list_skills` | `{}` | `{skills: [SkillDefinition, ...]}` |
| `submit_task` | `{request: TaskRequest}` | `{task: Task}` |
| `get_task_status` | `{task_id: string}` | `{task: Task}` |
| `cancel_task` | `{task_id: string}` | `{task: Task}` |
| `read_task_events` | `{task_id, after_sequence=0, limit=32, wait_s=0.2}` | `{events: [Event, ...], next_sequence, done}` |

每次提交必须提供非空、稳定的 `idempotency_key`。桥接不会生成键或自动重试。工具成功
代表调用返回，应检查 `Task.state` 和 `Task.error` 判断任务是否成功；运行时准入拒绝也
可能返回终态为 `failed` 的 Task。取消是否确认同样以 Task 状态为准。

工具错误返回 `isError=true`，并保留
`structuredContent={error: {code, message, details}}`。运行时业务错误码原样透传。
连接、协议或请求期限失败使用 `MCP_TRANSPORT_ERROR`，详情含 `outcome_uncertain=true`；
这表示无法凭该次回复确定执行结果，应保留原幂等键并查询或对账。

事件是公开 SDK 的真实快照，按序回放历史后接续实时事件，不合成状态事件或序号。
`after_sequence` 为非负整数，`limit` 为 1–128，`wait_s` 为大于 0、至多 1 秒的有限数。
批次达到数量上限或等待期限即返回；超时批次的 `done=false`，不会取消机器人任务。
`next_sequence` 是本批最后一个事件的真实序号，空批次保留传入游标。`done=true` 表示
观察到实际终态事件或 SDK 正常结束；已经读过终态游标后再次读取也会确认结束。
批次从请求游标回看最多一个真实事件，正常续读不会重新扫描历史前缀。调用方应使用此前
返回的真实序号续读；任意超前游标没有事件证据，等待到期后返回空批次、原游标和
`done=false`，即使任务快照已经终结也不会据此推断事件流已读完。

应用可用同一解释器自动启动、清理桥接子进程：

```python
from robot_agent_runtime.models import SkillCall, TaskRequest
from robot_runtime_mcp.client import open_mcp_runtime

async with open_mcp_runtime(
    "/absolute/path/robot.sock",
    allowed_skills={"audio.speak"},
    request_timeout_s=10.0,
) as runtime:
    final = await runtime.submit_and_wait(
        TaskRequest(
            task_type="greeting",
            idempotency_key="visit-42-greeting-v1",
            steps=[SkillCall(skill="audio.speak", params={"message": "欢迎光临"})],
        ),
        timeout_s=30.0,
    )
    print(final.state)
```

`open_mcp_runtime` 返回 `McpRuntime`，提供 `list_skills`、`submit_task`、
`submit_and_wait`、`get_task_status`、`cancel_task` 和 `subscribe_task_events`。
`allowed_skills=None` 允许全部，空集合禁止全部。客户端在进入上下文的同一 task 中关闭
官方 stdio/session 上下文并回收子进程；应用应在退出前关闭自己的监控和工作流。
`request_timeout_s` 限制握手与各次请求；进程退出按官方 SDK 的有界终止流程处理。
子进程沿用当前 `sys.executable`，继承 SDK 的基础环境及显式 `PYTHONPATH`，不转发模型
凭据。`TaskWaitTimeout` 是提交后的本地等待期限，包含任务 ID，不取消远端任务；调用方
取消也只结束等待。业务错误为 `AgentRuntimeError` 子类，保留 `info.code`。

容器验证（在仓库根目录）：

```bash
docker exec robot_core_ubuntu24 /opt/robot_agent_venv/bin/python -m pytest -q \
  -c /workspace/robot_core/mcp_bridge/pyproject.toml /workspace/robot_core/mcp_bridge/tests
```

测试通过真实 MCP stdio 子进程连接真实 RuntimeServer/Simulator，并覆盖协议发现、输入
schema、稳定幂等、白名单、结构化错误、事件历史与实时接续、批次超时、取消、断连、
本地等待期限、握手期限、子进程退出及桥接关闭后服务继续可用。
