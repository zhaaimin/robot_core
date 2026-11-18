# 生命周期、前置条件与恢复

> 返回 [README](../README.md) · 相关：[架构](architecture.md) · [SDK 参考](sdk-reference.md)

## 任务状态机

统一能力层不新增状态，不写状态机。后台受理不等于完成；requires_attention 在非 succeeded 状态为 true，
只是调用方观察提示。等待超时或 MCP/SDK 调用结束不证明物理停止，停止与重启核对仍由 runtime 负责。

Agent 的图节点、会话检查点和本轮暂停只属于应用状态，不增加 `TaskState`；
任务完成、取消和待对账仍以下述 runtime 状态为准。
酒店的接待、等待同意、交接、护送、客人到达确认及服务完成也属于独立业务状态，由 `hotel_agent.service` 管理。
播报只交付一次扬声器任务结果，既不是业务阶段，也不占用机器人租约。
导航成功不等于客人到达；机器人已停止但未确认回到会合点时仍保留业务占用，不增加内核状态。
未批准脚本调用只返回 Agent 工具失败观察，不创建机器人任务或增加 `TaskState`。
共享 shell 和场景描述符只属于应用层；酒店错误归位保持错误码，脚本指纹变化仍拒绝恢复旧 pending，不改变任务状态机。
SkillHost 保留执行批次开始前的加载集合与决策时的脚本指纹，不写 runtime 任务状态；宿主参数格式错误仍返回 INVALID_REQUEST。
通用期限参数校验失败返回应用配置错误；导入酒店错误或业务数据类型仅加载对应契约。
MCP 提交保留调用方的非空幂等键，真实事件保留原序号；等待超时、请求取消和连接断开不表示机器人已停止。
酒店 MCP 后端关闭时先让 workflow 请求停止并保存实际结果，再关闭桥接连接；独立 runtime 服务继续运行。
导览的等待确认、导航、介绍和完成属于 `guide_agent.service` 的业务状态；导航成功后才自动提交一次配置介绍。
导览停止先撤销旧对话的动作权限，再请求 runtime 停止；未知提交/停止保留占用并待核对，重启不重放动作。
重复唤醒不重置导览；ASR 中间结果不进入模型，最终输入事件持久化去重；麦克风与 ASR 引擎由外部适配器提供。

目录按职责归类，任务状态、持久化与停止确认语义保持一致。
`stop()` / `close()` 是运行时生命周期阶段，不增加业务任务状态；停止未确认仍落待对账。

外部循环的停止确认由工作线程发出，可能略早于 Python 线程真正退出。非自关闭路径会在
同一 grace 截止前确认实际退出，不阻塞调用方事件循环或创建额外等待线程；预算结束时仍按
既有有界关闭规则返回，不补建替代线程。线程退出不替代技能对物理停止的权威确认。

```
CREATED → VALIDATING → QUEUED → RUNNING → SUCCEEDED
                           │        │  ↕
                           │        │  WAITING（等待资源互斥锁）
                           │        ├→ FAILED
                           │        └→ CANCELING → CANCELED
                           └→ FAILED（校验未通过）

重启恢复：UNKNOWN / RECONCILIATION_REQUIRED
执行或取消无法确认停止：RUNNING / CANCELING → RECONCILIATION_REQUIRED
```

终态为 `SUCCEEDED`、`FAILED`、`CANCELED`、`UNKNOWN`、`RECONCILIATION_REQUIRED`。

注册式监听只观察这些状态，不增加新的状态或写入状态。`on_done` 包含所有终态，
表示本次任务已结束，不等于成功；`on_error` 及回调队列满、排队超时也不改变机器人任务状态。
`submit_and_wait` 只等待现有终态，不增加任务状态；`TASK_WAIT_TIMEOUT` 是调用方等待错误，
不把业务任务改为超时或取消，区别于任务总期限的 `TASK_TIMEOUT`。
监听的调用方/分组/独立执行方式只决定回调归属，不增加状态或改变业务任务的执行位置。
`from_platform` 从厂商平台装配原有端口并保留线程分组，不增加任务状态；驱动断连、响应不可信或停止未确认，仍通过
技能错误进入现有失败、停止确认和待对账流程。
线程选择不增加任务状态；共享组被阻塞时仍按原有期限、停止确认和资源隔离规则处理。
尚未派发的技能在排队期间单步期限到期为 `FAILED / SKILL_TIMEOUT`，总期限到期为
`FAILED / TASK_TIMEOUT`，撤销后不会补执行或隔离该技能实例。已开始且无法确认停止的
调用仍进入待对账并隔离资源；软件状态不证明机器人已经停止。

CC API 的 `audio.speak` 在启动受理后保持 `RUNNING`，收到匹配 `workId` 的
`cc.api.work.finished` 才成功。首版按约定不解析 finished 的结束原因，外部停止/失败
如果也发出该事件，同样被视为成功。运行时主动取消仍等待停止确认后进入 `CANCELED`；
停止未确认进入 `RECONCILIATION_REQUIRED / EXECUTION_UNRESPONSIVE`。
不支持取消的真实调用不会被强制取消；支持取消的调用也须正常成功返回或显式抛
`TaskCancelled` 确认停止，仅 `CancelledError` 不足以证明物理动作已停止。只有执行已结束的
超时技能才报告 `FAILED / SKILL_TIMEOUT`。总期限到期且未派发或确认已停止时报告
`FAILED / TASK_TIMEOUT`；停止不明仍进入待对账。

无法确认停止时，该技能实例与它声明的资源在当前运行时内被隔离，等待者和新任务都不能
因为软件任务已结束而复用资源，报告 `RESOURCE_QUARANTINED`。迟到结果不会把任务改回
成功，也不会在终态后追加进度。隔离不会因线程后来返回而自动解除；重新创建运行时或
适配器前必须先确认旧动作已停止，重启不等于硬件停止确认。

技能**正常返回失败**时，是否释放资源由 `SkillResult.action` 决定：

| `action` | 处理 |
| --- | --- |
| `None` | 适配器尚未接入该声明，沿用原有行为，不据此推断静止 |
| `executed="none"` | 一步未执行，释放资源并按 `FAILED` 结束 |
| `executed="some"` + `stopped=True` | 执行过且已确认静止，释放资源并按 `FAILED` 结束 |
| `executed="some"` + `stopped` 非 `True`，或 `executed="unknown"` | 隔离资源，任务落 `RECONCILIATION_REQUIRED / EXECUTION_UNSETTLED` |
| `operator_required=True` | 无论执行多少都隔离，等待人工恢复 |

`ActionOutcome` 只陈述执行事实，不解释硬件语义；`stopped=True` 只能由适配器在观察到
执行器静止后给出。新增该字段的适配器不需要改动内核判断。

## 重启恢复语义

运行时**不会**因为数据库里是 `RUNNING` 就重放任务。`start()` 时对每个非终态任务：

1. 若注入了 `Reconciler` 且它返回权威终态（`SUCCEEDED` / `FAILED` / `CANCELED` / `UNKNOWN`），采信该状态；
2. 否则（无 reconciler、reconciler 抛异常、返回不确定结果）任务落入
   `RECONCILIATION_REQUIRED`，等待人工或上层介入。

这条规则用于避免重复执行非幂等的物理动作。


## 平台事件的观察与恢复

`PlatformEvent` 是独立设备事实，不是任务状态或 `RobotStateProvider` 的可用性快照。
无任务时也可采集；运行时不把 `robot.fault.*`、连接丢失或厂商载荷解释为取消指令、
故障解除或安全许可。任务状态仍只有 `TaskStateStore.transition()` 能写，前置条件机制
继续按标签集合判定，平台日志失败不会更改任务状态。

可选子系统在日志就绪后开始监听来源，先保存再交付。来源异常或提前耗尽时，已收到并保存
的事件仍可回放，订阅随后显式失败；这不表示设备故障已清除。关闭停止采集、有界排空
已打开订阅、关闭日志并继续释放其余资源；取消或网络投递返回不证明外部副作用已经停止。
厂商连接与平台启停始终由应用管理，先关闭运行时，再关闭平台与原生连接。

恢复位置有两种，不能混用：

| 事件流 | 恢复位置与范围 |
| --- | --- |
| 单任务 `Event` | 保存 `task_id/sequence`，`subscribe_task_events` 回放后接实时 |
| 全任务实时流 | 首次拉取登记后才观察，不提供全局序号或历史；按各任务恢复 |
| 平台 `PlatformEvent` | 日志分配 `cursor`，`subscribe_platform_events(after_cursor=...)` 回放后接实时 |

消费方在业务处理完成后保存平台游标，并按 `event_id` 幂等；发送成功不能代替处理确认。
内存日志不跨进程恢复；SQLite 平台日志与任务迁移事务独立，重启后只回放已保存事实，
不重放物理任务或声称补齐断线期间未观测事件。日志默认保留最新 10000 条，去重只保证
保留窗口内的 ID。恢复位置过旧会报告 `PLATFORM_EVENT_CURSOR_EXPIRED`，此时须重新同步
设备当前状态；未来游标被拒绝。慢订阅者溢出会携带最后已交付游标，其他订阅者继续运行。

CC 初始快照与回调间的权威顺序尚未验证。`data.sync.initial_snapshot_order="unverified"`
明确此边界，`data.payload` 保留原始内容；不根据空集合或未知结构推断解除。连接失效产生
`robot.connection.lost` 事实，随后报告 `PLATFORM_EVENT_SOURCE_UNAVAILABLE`，不自动重连。

## 机器人状态前置条件

急停、低电、底盘锁定这类**机器人自身的状态**不是任务状态，不占 `TaskState` 的取值。技能
用 `preconditions` 声明自己需要哪些条件成立，`RobotStateProvider` 报告当前**不成立**的条件
标签集合，运行时只做集合求交：

```python
blocked = skill.definition.preconditions & await provider.unsatisfied()
```

标签对运行时是不透明的。`battery.sufficient` 具体是电压阈值还是 SOC 百分比由适配器决定，
所以内核里没有任何机器人领域模型。**急停因此不是特例**——它只是一个几乎所有运动技能都会
声明的标签。

判定发生在三个时刻，对所有条件一视同仁：

| 时刻 | 位置 | 不满足时 |
| --- | --- | --- |
| 提交准入 | `PolicyEnforcer`，整单取一次快照 | 整单 `FAILED` + `PRECONDITION_FAILED` |
| 每步启动前 | `WorkflowExecutor`，取到资源锁之后、`SKILL_STARTED` 之前 | `FAILED` + `PRECONDITION_FAILED`，该步未执行 |
| 执行中条件变化 | `RuntimeCore` 消费代理转交的 `provider.watch()` 推送 | 先置 `cancel_event`，再记原因；确认停止则 `CANCELED`，无法确认则待对账 |

只有**声明了**已损坏标签的技能会被打断：急停期间一个正在播放的语音任务不受影响，因为
`audio.speak` 没有声明 `estop.clear`。

未注入 `RobotStateProvider` 时整条检查跳过。读取状态失败按**失败关闭**处理——读不到机器人
不等于机器人可用，提交会以 `INTERNAL_ERROR` 失败。`watch()` 可以返回一个立即耗尽的迭代器，
此时降级为仅检查点模式：仍然不会启动机器人做不到的事，但已经在跑的任务不再被打断。

关闭期间，若监听正在等待已经下发的状态写入，仍须先确认该写入的真实结果。即使该等待为
确认结果而消费了取消请求，写入返回后也会退出监听，不会重新等待下一条状态变化。

> 这套机制是**软件层面**的准入与打断，不是功能安全。真正的急停必须是硬件回路；这里做的
> 是在硬件已经断电之后让软件状态与事件记录追上现实。

### 两个模拟实现

`adapters/simulator.py` 提供两个 `RobotStateProvider`，区别是标签从哪来：

| 类 | 标签来源 | 用途 |
| --- | --- | --- |
| `SimulatedRobotState` | 调用方直接给，`set_unsatisfied({...})` | 测试替身，精确控制时序 |
| `SimulatedRobot` | 从模拟读数**推导**，`set_estop()` / `set_voltage()` / `drain()` | 贴近真实适配器的参考实现 |

`SimulatedRobot` 多做两件真实适配器必须做、测试替身不需要做的事：

- **滞环**：电压掉到 `low_voltage` 以下才破，回到 `recovery_voltage` 以上才恢复。没有这个带宽，
  一块停在阈值上的电池会反复抖动，每次推送都打断刚起来的任务。
- **无变化不推送**：重新推导出同一个集合就不发，否则一个 10 Hz 的传感器会每秒唤醒运行时 10 次
  去重算同一个答案。

电池阈值要留够开回充电桩的余量——被打断的导航会把机器人**停在原地**，阈值定得太低这套机制
救不了场，只是换个姿势搁浅。同理 `power.charge` 这类技能**绝不能**声明 `battery.sufficient`，
否则电量低时连充电都会被拒。

`examples/precondition_demo.py` 把三个时刻跑成一个可执行脚本：满电准入 → 行进中电量跌破阈值
被打断 → 同一块电量下导航被拒而语音播报照常完成。
