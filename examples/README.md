# 示例阅读顺序

不接语言模型时，可先运行 [capabilities_demo.py](capabilities_demo.py)：安装 `capabilities/` 后，
用统一工具目录和稳定操作 ID 调用真实模拟 runtime。命令为
`/opt/robot_agent_venv/bin/python examples/capabilities_demo.py`，无需 Agent 或 MCP 框架。

## 能力层任务和平台监听

[`capability_listeners_demo.py`](capability_listeners_demo.py) 使用模拟器和假平台事件源，
分别注册 `robot.add_task_listener(...)` / `robot.add_platform_listener(...)`，演示任务自动登记、
同步事件回调、异步终态 / 设备回调，以及 `close_listeners()` 释放资源。
任务和平台监听使用同名 reporting 组，打印同一回调线程；回调通过 call_soon_threadsafe
通知业务 loop。两个监听入口也支持默认 caller 与按注册独占的 dedicated。
任务业务失败仍走 on_done，设备故障仍走平台 on_event；on_error 只报告监听失败并可省略。

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && python3 examples/capability_listeners_demo.py'
```

示例不连接硬件或模型。两类句柄的 wait / close 与线程边界见
[能力层说明](../capabilities/README.md#注册任务和平台监听)。

## 空闲平台事件与恢复

[platform_events_demo.py](platform_events_demo.py) 用假异步事件源产生初始快照与变化，
通过 `RobotCapabilities.subscribe_events()` 接收，再从应用已处理的 `cursor` 重订阅补收。
运行前安装本仓库的 runtime 与 capabilities 两个包；无需硬件、厂商 SDK 或新增第三方依赖：

```bash
python3 examples/platform_events_demo.py
```

示例把首次收到的变化当作尚未处理，只保存初始事件游标；重订阅后按事件 ID 收到同一变化。
随后提交模拟器只读状态任务，在任务完成后通过同一订阅汇聚该任务终态与平台事实，验证不会
错过快速完成；再从已处理终态序号恢复，确认不重复且能结束。不会连接或操作真实机器人。
内存日志只在当前进程内有效。重启恢复需注入 `SQLitePlatformEventRepository`，消费方在完成业务
处理后保存游标并按 `event_id` 幂等。日志默认保留 10000 条，过期后须重新同步设备当前状态。
接口和已连接 WebSocket 的转发示例见 [SDK 参考](../docs/sdk-reference.md#平台事件与通知)。

## CC API 语音入口

CC API 语音分成三个独立入口，按当前要做的事选择即可：

| 入口 | 展示内容 | 任务结果从哪里来 |
| --- | --- | --- |
| [cc_api_speech_minimal_demo.py](cc_api_speech_minimal_demo.py) | 连接、平台装配、提交一个任务并等待终态 | `submit_and_wait()` 返回的 `Task` |
| [cc_api_speech_callback_demo.py](cc_api_speech_callback_demo.py) | 一个监听的事件、终态和监听异常回调 | `submit_task()` 后等待 `listener.wait()`，再查询最终 `Task` |
| [cc_api_speech_demo.py](cc_api_speech_demo.py) | 原有双监听线程与阻塞实验 | `submit_and_wait()` 取得结果，报告前等待两个监听完成 |

三份脚本都能独立执行，也都提供 `async speak(api, message) -> Task`。原
`cc_api_speech_demo.py` 的入口、额外参数和行为保持不变。

## 最小调用

在已安装项目和附带 `ubt_robot` 的 Linux ARM64 环境，从项目根目录执行：

```bash
python3 examples/cc_api_speech_minimal_demo.py \
  --api-id YOUR_API_ID --address ROBOT_IP:48159 --message '你好，我是机器人'
```

脚本只通过 `submit_and_wait()` 等待终态，命令行成功时打印 `[RESULT]`。
如果应用已经持有连接，可直接调用：

```python
from examples.cc_api_speech_minimal_demo import speak

final = await speak(api, "你好")
print(final.id, final.state, final.result, final.error)
```

`speak()` 对业务成功或失败都返回终态 `Task`；启动、查询和清理异常向调用方抛出。
命令行会检查任务状态，业务失败时抛出 `SkillExecutionError`。

## 回调监听

```bash
python3 examples/cc_api_speech_callback_demo.py \
  --api-id YOUR_API_ID --address ROBOT_IP:48159 --message '你好，我是机器人'
```

示例使用默认的 `execution="caller"`，三个回调均为异步函数：

- `[EVENT]`：按顺序打印任务事件。
- `[DONE]`：打印终态任务，包括成功、失败、取消或待对账。
- `[LISTENER ERROR]`：打印订阅、查询或回调自身的异常。

监听在 `submit_task()` 之前注册，只观察本 SDK 后续提交的任务。提交返回后可以继续
处理其他异步业务；示例通过 `listener.wait()` 收齐通知，再查询最终快照并释放资源。
业务失败仍返回 `Task`；监听异常在等待后向调用方抛出。示例不需要维护结果 Future。

## 线程与阻塞实验

原入口保留 `--listener-block`、`--listener1-execution`、`--listener2-execution`。
它在监听1 的异步回调中故意同步阻塞，打印回调线程与监听2 的事件间隔：

```bash
python3 examples/cc_api_speech_demo.py \
  --api-id YOUR_API_ID --address ROBOT_IP:48159 --message '你好' --listener-block 2
```

把产生阻塞的监听1 移到独立线程后对照：

```bash
python3 examples/cc_api_speech_demo.py \
  --api-id YOUR_API_ID --address ROBOT_IP:48159 --message '你好' \
  --listener-block 2 --listener1-execution dedicated
```

只把监听2 移到独立线程，仍会受到提交方循环阻塞的影响。完整组合说明见
[厂商接入文档](../docs/adapters.md#cc-api-语音播报)。此实验的监听异常只打印，不替代业务任务结果。

## 连接与生命周期

三份脚本都通过 `CC_API_TOKEN` 读取远程连接 Token，本机运行可省略 `--address`。
CLI 在事件循环外初始化、连接和停止原生 SDK；`speak()` 接受应用已连接的 API，
依次管理平台和运行时，退出时先关闭运行时，再关闭平台，原生连接仍归调用方所有。
`--help` 不加载厂商 SDK 或建立连接。

不接机器人时，可以运行 [embedded_demo.py](embedded_demo.py)、
[remote_demo.py](remote_demo.py) 和 [precondition_demo.py](precondition_demo.py) 了解
任务提交、远程调用和前置条件。
