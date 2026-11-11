# Hotel Agent 通过 MCP 接入 Runtime

**目标：** 让现有酒店 Agent 的 HotelWorkflow 经 MCP 调用独立 runtime，复用现有 HotelTools、Skill 与业务确认，不改成通用 RobotTools 场景。

**已确认架构：** `Hotel Agent → HotelTools → HotelWorkflow → MCP 客户端 → MCP stdio 服务 → RemoteRuntime / UDS → RuntimeServer`。runtime 内核和基础依赖保持独立。当前工作树有用户未提交的 Agent 项目，继续原地实现，不提交、不切换分支。使用 planning、TDD 和独立审查流程，全部验证进现有容器。

## 接口与不变量

- 独立子项目 `mcp_bridge/` 发布 `robot-agent-mcp`，Python 包 `robot_runtime_mcp`，依赖 `mcp>=1.30,<2` 和现有 runtime；Agent 仅以可选 extra 引用。
- 第一版 stdio，桥接进程只连接已有 UDS runtime；退出不关闭 runtime 服务。Agent 可自动启动/清理桥接进程。
- 四项基本 MCP tools：`list_skills` 返回 `{skills: [...]}`；`submit_task` 接收 `{request: TaskRequest}`；`get_task_status` / `cancel_task` 接收 `{task_id: ...}`，三者返回 `{task: Task}`。提交必须携带非空稳定幂等键；桥接不生成新键、不自动重试。
- 酒店后台监控需要真实任务事件，增加只读 `read_task_events(task_id, after_sequence=0, limit=32, wait_s=0.2)`：通过公开 SDK 订阅真实事件，按序返回有界批次 `{events, next_sequence, done}`。limit 至多 128、wait_s 至多 1 秒，超时只是该批次结束，不能取消物理任务。不得合成 runtime 事件或序号。
- MCP 工具错误设置 isError，并以 `{error: {code, message, details}}` 保留结构化错误。任务受理与执行成功分别按返回的 TaskState 判断。协议/断连失败是结果不确定，不能假定动作停止。
- 客户端公开 `open_mcp_runtime(socket_path, *, allowed_skills=None, request_timeout_s=10.0)` async context，返回 McpRuntime，提供 list_skills / submit_task / submit_and_wait / get_task_status / cancel_task / subscribe_task_events。事件流由上述真实事件批次恢复，保留序号与等待/取消边界。SDK 现有 submit_and_wait 辅助实现可复用。
- Agent 新增 `--scenario hotel --backend mcp --runtime-socket PATH`；原 simulator / CC 算法不变。robot 场景本轮不新增 MCP 后端。MCP 的任务库归远端所有，本地 task_database 为 None；酒店业务库和图检查点按 endpoint 区分，与模拟器库隔离；runtime_id 绑定规范化 socket 和场景。
- HotelWorkflow 与协调器改依赖应用层 TaskRuntime Protocol；行为仍由原业务层决定。运行时客户端关闭前先完成 workflow 关闭/停止确认。保持已发布脚本指纹、业务确认门禁、机器人租约、护送/到达/回位与不重放规则。
- 本机 socket 是远端身份边界，更换背后的机器人或任务库时须使用新 endpoint 与业务/检查点存储，不把同一个 endpoint 指向另一份历史。

## 实施任务

- [x] 容器准备官方 MCP SDK、记录源码与资源基线；为桥接协议及酒店配置/端到端路径先写失败测试。
- [x] 独立实现 mcp_bridge 的配置、错误、工具服务、stdio CLI 和任务客户端；真实 SDK 协议验证发现、提交、幂等、事件、取消、断连和超时。
- [x] 新增 Agent TaskRuntime 端口与契约检查、MCP 连接适配器；扩展 hotel 场景后端与集中存储解析；保留业务逻辑和关闭顺序。
- [x] 用真实 MCP 子进程和现有 RuntimeServer 验证酒店接待、客人确认、护送、到达、回位、播报、取消、未知结果和持久化恢复。
- [x] 同步 root README 的 API/状态/模块/范围、Agent README 与 HOTEL，提供两进程运行命令、安装和 stdio 接入说明。
- [x] 容器完整回归、compileall、pip check、清理旧构建缓存后构建/独立安装两个 wheel；规格与质量独立审查并修复阻塞项。

## 验证环境

复用 `robot_core_ubuntu24` 与 `/opt/robot_agent_venv`，根 runtime 测试使用原 python3 环境。独立子项目测试不加入历史宿主 sweep。保留容器和工作树。

## 实际结果

- 容器安装官方 MCP SDK 1.30.0；新增依赖只在独立桥接包与 Agent 可选 extra，根 runtime pyproject 和源码不改。
- 酒店配置先 2 failed / 1 passed / 2 skipped；TaskRuntime 端口接口先 ImportError 失败。随后用真实 MCP 子进程运行酒店全部旅程与取消，定位并修复 SDK 默认 stderr 在捕获环境中缺少 fileno 的问题。
- Agent 定向组合 104 passed；增加必需技能拒绝、断连隔离、重连播报幂等及 runtime/业务库重启后导航不重放，酒店 MCP 9 passed。
- 源码完整回归：Agent 278 passed in 8.50s；桥接最终 32 passed in 8.39s；runtime 独立串行有界完整回归 497 passed in 10.62s。compileall 和两套环境 pip check 通过。
- Agent 接入层规格与质量审查均通过。桥接规格审查发现终态批次从头重扫可能在长历史上不前进，新增两个先失败的回归后改为游标前一个真实事件开始续读；慢仓储下仍完整按序前进，准确终态游标有真实证据才完成，超前游标保持未确认。桥接规格与质量复审均通过。
- 安装包测试暴露 30 毫秒期限下合法空批次的测试误判；测试改为允许空批次但检查游标、禁止前缀重读并在总期限内验证完整结果，未修改发布行为。独立并发压力复跑 Agent 278 passed / bridge 32 passed。
- 清理旧缓存后从当前两个项目直接构建 wheel，独立 target 安装并确认实际导入来自 target；Agent 39 个 Python 文件、桥接 5 个 Python 文件与源码逐字节一致，无旧入口或测试替身混入。3 份 Skill 正文、7 份参考资料与3份脚本完整。
- 独立安装包完整测试：Agent 278 passed in 8.88s，桥接 32 passed in 8.13s。桥接测试中的原生 stdio 子进程也显式沿用 target PYTHONPATH，不回落到源码可编辑安装。
- 安装包真实命令行 `--scenario hotel --backend mcp --runtime-socket ... --prompt /skills` 成功：3 个 Skill 可见，无模型或机器人动作调用；退出后原 RuntimeServer 仍可查询。完整护送闭环由上述真实 MCP/UDS 测试覆盖。
- 验收与原源码快照位于 `.verify/hotel-mcp-baseline-ffl400ts/`，旧构建缓存位于 `previous-build/`。runtime 与发布 Skill 共 55 个文件的 SHA-256 与基线保持一致。
- 最终 wheel 位于验收目录的 `wheels/`，安装目标位于 `installed/`，结构化检查为 `release-verification.json` 和 `cli-smoke.json`。Agent README 最后更新仅改变 wheel 元数据，已确认包内全部源码/资源不变并刷新安装。
- 原环境仍可编辑安装 Agent 和桥接包；保留现有容器、工作树与归档，未提交，未连接外部模型服务、真实 PMS 或机器人硬件。
