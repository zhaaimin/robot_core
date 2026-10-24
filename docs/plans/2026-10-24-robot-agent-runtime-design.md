# Robot Agent Runtime 双形态架构设计

**版本**：MVP v0.1  
**日期**：2026-10-24  
**状态**：已确认实施方向

## 1. 目标

实现一个平台无关的机器人 Agent 运行时框架。同一份 Runtime Core 同时支持进程内 Python SDK 与本机 UDS 守护进程；首版通过模拟机器人验证任务执行、取消、超时、事件追踪和重启恢复，后续再增加 Walker、ROS 2 与厂商适配器。

## 2. 首版范围

### 必须实现

- Task、SkillCall、SkillDefinition、Event、Result、Error 等公共契约。
- 任务状态机、顺序工作流、技能注册与 JSON Schema 参数校验。
- asyncio 异步执行、协作式取消、单步超时、资源互斥。
- SQLite 任务与事件持久化、事件序列号、启动时非终态任务恢复。
- 模拟导航、音频与状态查询技能。
- 进程内 Python SDK、UDS 守护进程、远程 Python SDK。
- 单元测试、契约测试、集成测试与故障注入测试。

### 暂缓实现

- LLM/Agent 自动规划与重规划。
- ROS 2、Walker 和真实厂商 Adapter。
- 云端设备管理、OTA、多机调度与多租户。
- 抢占式调度、复杂优先级队列和分布式事务。
- 用 Runtime 替代硬件急停或实时安全控制器。

## 3. 总体架构

采用 Ports & Adapters。Core 只依赖抽象端口，不导入 ROS 2、edu_walker 或厂商 SDK。

```text
Embedded SDK ───────────────┐
                            ├─→ Runtime Core → Skill / State Ports → Simulator Adapter
Remote SDK → UDS NDJSON ────┘                  → SQLite Repository
```

Runtime Core 负责任务生命周期、工作流、事件、资源、安全与恢复。两种 SDK 只是不同接入方式，不复制任务逻辑。

## 4. 核心公共模型

### TaskRequest

- `task_type`：任务类型。
- `steps`：有序 SkillCall 列表，首版至少一步。
- `priority`：保留字段，首版不实现抢占。
- `timeout_s`：可选任务总超时。
- `idempotency_key`：可选幂等键。
- `metadata`：调用方元数据。

### SkillCall 与 SkillDefinition

SkillCall 包含技能名、参数和可选单步超时。SkillDefinition 声明名称、版本、输入 Schema、资源、权限、超时和取消能力。

### TaskState

```text
CREATED → VALIDATING → QUEUED → RUNNING
RUNNING → WAITING → RUNNING
RUNNING → CANCELING → CANCELED
RUNNING → SUCCEEDED / FAILED
非终态任务在启动对账后可进入 RECONCILIATION_REQUIRED / UNKNOWN
```

`SUCCEEDED`、`FAILED`、`CANCELED`、`UNKNOWN`、`RECONCILIATION_REQUIRED` 为首版终态。成功必须由所有步骤成功及底层 SkillResult 共同判定。

### Event

事件包含 `event_id`、`task_id`、`sequence`、`event_type`、`timestamp` 和 `data`。每个任务的序列号严格递增；事件与任务快照均持久化。

## 5. 执行与取消语义

1. 验证请求、技能存在性、Schema 和权限后，任务进入队列。
2. 工作流按步骤执行；每一步先获取声明的资源锁。
3. 取消请求先将任务置为 CANCELING，再向 SkillContext 发布取消事件。
4. 支持取消的技能必须协作退出；不支持取消的技能完成后不得继续后续步骤。
5. “收到取消”和“底层停止确认”使用不同事件表达。
6. 单步超时映射为 `SKILL_TIMEOUT`，任务进入 FAILED。
7. 任何未处理异常都转换为结构化内部错误并记录事件，不丢失任务状态。

## 6. 持久化与恢复

SQLite 保存任务快照、事件和幂等键。进程重启时：

- 查询所有非终态任务。
- 如果平台提供权威状态对账能力，使用对账结果更新任务。
- 没有对账能力或结果不确定时，进入 `RECONCILIATION_REQUIRED`。
- 不自动重放 RUNNING 步骤，不重复执行非幂等技能。

## 7. SDK 与守护进程

### 进程内 SDK

`RobotRuntime` 直接持有 RuntimeCore，提供 `start`、`close`、`submit_task`、`get_task_status`、`cancel_task`、`list_skills` 和 `subscribe_task_events`。

### 守护进程与远程 SDK

守护进程在同一 RuntimeCore 外增加 UDS 服务壳。传输采用换行分隔 JSON（NDJSON），每个普通请求使用一次连接；事件订阅保持长连接。远程 SDK 与本地 SDK 暴露同构异步 API。

首版方法：`health`、`submit_task`、`get_task_status`、`cancel_task`、`list_skills`、`subscribe_task_events`。

## 8. 错误处理

公共错误包含稳定错误码、可读消息和可选详情。主要错误码：`INVALID_REQUEST`、`SKILL_NOT_FOUND`、`SCHEMA_VALIDATION_FAILED`、`PERMISSION_DENIED`、`TASK_NOT_FOUND`、`SKILL_TIMEOUT`、`SKILL_EXECUTION_FAILED`、`TASK_CANCELLED`、`RECONCILIATION_REQUIRED`、`INTERNAL_ERROR`。

远程协议保留相同错误码和详情；传输异常与业务异常分别处理。

## 9. 测试与验收

- 模拟多步骤任务成功，事件序列连续，结果可查询。
- 非法技能参数在执行前被拒绝。
- 长任务可取消，取消事件与最终状态正确。
- 超时技能不会继续后续步骤。
- 资源互斥生效。
- 重启后不自动重放非终态任务。
- 本地与远程 SDK 对同一核心契约行为一致。
- 全量 pytest 通过，示例可直接运行。

## 10. 后续适配

新增机器人平台时实现 Skill/State Port，并通过相同契约测试。Walker Adapter 可封装 edu_walker WebSocket 与 robot-control；ROS 2 Adapter 可将 Action 映射为长任务技能。适配器不得改变核心任务状态语义。
