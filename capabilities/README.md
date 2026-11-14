# Robot Capabilities

供 MCP 客户端和自研 Agent 共用的机器人能力层，Python 3.11+。发布包为
`robot-agent-capabilities`，导入名为 `robot_agent_capabilities`，只依赖本仓库的 runtime。
不加载模型、LangGraph、MCP 或业务应用；RobotCapabilities 不拥有 runtime 生命周期。

## 安装与使用

在已安装项目依赖的容器中，从仓库根目录安装本地包：

```bash
/opt/robot_agent_venv/bin/python -m pip install -e . -e ./capabilities
```

```python
from robot_agent_capabilities import RobotCapabilities
from robot_agent_runtime.sdk import RemoteRuntime

runtime = RemoteRuntime('/workspace/robot_core/.data/robot.sock')
await runtime.start()
try:
    robot = await RobotCapabilities.create(runtime, allowed_skills={'audio.speak'})
    for tool in robot.tools:
        print(tool.name, tool.description, tool.input_schema)
    observed = await robot.invoke(
        'robot_speak', {'message': '欢迎光临'}, operation_id='visit-42-greeting-v1',
    )
    print(observed['task_id'], observed['state'])
    # 后续根据真实状态查询；需要物理停止时显式调用 robot_cancel_task。
    status = await robot.invoke('robot_task_status', {'task_id': observed['task_id']})
finally:
    await runtime.close()
```

完整的无模型模拟示例：`/opt/robot_agent_venv/bin/python examples/capabilities_demo.py`。
外部 Agent 的 MCP 配置见 [统一接入说明](../docs/agent-integration.md)。

## 契约

| API | 语义 |
| --- | --- |
| `RobotCapabilities.create(runtime, ...)` | 从公开 TaskRuntime 发现技能，冻结当前目录和白名单；不启动/关闭 runtime |
| `tools` | 返回独立的 ToolDefinition 快照：name、description、input_schema、skill、read_only |
| `invoke(name, params, *, operation_id=None, wait=False)` | 调用能力或任务控制，返回真实 Observation；动作必须提供稳定操作 ID |

`TaskRuntime` 在 `robot_agent_capabilities.ports` 中声明，嵌入式 RobotRuntime、UDS
RemoteRuntime 和独立 MCP 包的 McpRuntime 按结构实现。它继续提供任务查询、显式取消和真实事件订阅。
统一层不访问 KernelHost、RuntimeCore 或仓储私有字段，不替代 runtime 的 schema、权限、前置条件及资源校验。

能力名优先保留 `robot_speak` / `robot_navigate_to` / `robot_get_state`；其他技能按
`robot_` 前缀规范化。冲突或超过 64 字符会拒绝装配。参数 schema 和说明来自实际 SkillDefinition，
数字/长度边界由 `schemas.describe_bounds` 复述。不能把 `read_only=False` 解释为一定产生动作；
runtime 技能尚未声明只读属性，因此只有状态控制工具显式标注只读。

默认提交后立即返回任务句柄；`wait=True` 才在 `wait_timeout_s` 内等待，默认 5 秒。
任务本身的期限为 `task_timeout_s`，默认 60 秒，两个期限相互独立。
等待超时返回原 task_id 和 TASK_WAIT_TIMEOUT，不取消、不重试物理任务；调用方取消也不代表硬件停止。
`requires_attention` 在 state 不是 succeeded 时为 true，包括后台执行中和已取消，并非另一个任务状态。
Task 的 result 和运行时错误（含部分执行、未知停止等事实）按实际返回；本地等待和操作身份冲突也会记录在 Observation.error。
这些本地错误不修改持久化 Task，统一层不推测未报告的物理事实。

动作的 operation_id 必须由调用方持久保存，并在同一 runtime 历史内避免与其他调用方冲突。
默认幂等键为 `robot:{operation_id}`；同一逻辑动作经 MCP 或 SDK 重试时保持 ID、参数和命名空间不变。
同一 ID 不同参数只返回原任务并报告 INVALID_REQUEST，不执行第二次动作。
换一个 ID、根据参数内容重新生成 ID、连接重建后重新编号都不是未知结果的恢复方式。
低层任务 API 仍由 runtime 自己解释幂等键；能力层额外核对返回任务与请求步骤是否一致。

原自研 RobotTools 显式保留历史 `agent:` 命名空间和 agent_action 类型；不要将旧 pending 图动作
迁移到默认 robot: 空间后重发。恢复仍遵守 runtime 的不重放与 Reconciler 规则。

统一层的配置/白名单/传输错误分别为 CAPABILITY_CONFIGURATION_INVALID、CAPABILITY_NOT_ALLOWED、
CAPABILITY_TRANSPORT_UNCERTAIN；runtime 错误保持原码。传输不确定结果保留原 operation_id。
catalog、协议握手等调用仍可能直接抛出结构化运行时/传输异常，调用方负责其连接策略。

Agent Skills 的 SKILL.md 是决策指导，与 runtime 的可执行 SkillDefinition 不同。
酒店和导览的确认、租约和业务完成条件仍归对应 Workflow；直接调用基础机器人能力不能冒充完成这些业务流程。

## 模块和验证

```text
src/robot_agent_capabilities/
├── client.py         统一能力客户端、稳定操作与有限等待
├── catalog.py        工具定义、命名、白名单与 schema
├── observations.py   通用 Observation 与事实转换
├── ports.py          TaskRuntime 客户端 Protocol
├── errors.py         能力层结构化错误
├── __init__.py       RobotCapabilities 入口
└── py.typed          类型标记
```

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -q -c capabilities/pyproject.toml capabilities/tests'
```

契约测试使用真实模拟器、嵌入式 SDK、UDS 与 MCP stdio 子进程；导入隔离检查禁止模型框架或业务包被意外加载。
