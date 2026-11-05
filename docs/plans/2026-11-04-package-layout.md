# 按职责整理包目录实施计划

> **当前决定：** 用户随后明确不需要兼容旧导入。17 个根目录兼容入口已删除；以下原实施
> 步骤保留为历史记录，当前源码、README 和 package layout 测试均只使用新路径。

> 用户已批准先整理目录。使用 writing-plans、executing-plans、test-driven-development，
> 保留当前未提交工作，不提交、不推送。Git 写入此前被沙箱拒绝，继续原工作区。

**Goal:** 将已稳定的模块按内核、托管、仓储、传输归类，保持运行时行为和旧导入兼容。

**Architecture:** 根目录保留 models/errors/ports/options/thread_binding 与包入口。
kernel 包收纳任务执行与状态，hosting 包收纳线程与快照边界，repositories 包收纳内存与
SQLite 实现，transport 包收纳 UDS 服务与协议。sdk/adapters 保留。
原模块用显式重导出加 sys.modules 别名指向规范模块，保留对象身份、私有调试入口和补丁行为。
子包 __init__ 不导入业务对象；不改变现有 RuntimeCore.isolate 装配逻辑，不拆分 isolation.py。

**Tech Stack:** Python 3.11+、标准库 importlib/sys、现有容器 pytest；不新增依赖。

## 模块映射

| 原模块 | 新实现路径 |
| --- | --- |
| core/executor/task_state/resources/events/policy/registry/recovery | kernel/同名.py |
| kernel_host/thread_runtime/isolation/boundary/execution_context | hosting/同名.py |
| repository | repositories/memory.py |
| sqlite_repository | repositories/sqlite.py |
| server/wire | transport/同名.py |

## Task 1：兼容回归与迁移

- [x] 新增 tests/test_package_layout.py：所有新旧路径同模块同对象、旧路径 monkeypatch 影响
  SDK/Server、新旧优先导入在独立进程均成功。登记 .verify/sweep.py。
- [x] 容器运行新测试，确认新包不存在而失败；容器完整基线 398 passed。
- [x] 移动当前文件内容，替换生产代码、示例和测试中的绝对导入；原位置保留轻量别名入口。
  所有规范实现逐文件对比移动前文本，仅允许导入路径变化；保留子进程测试中的字符串路径。
- [x] 容器运行兼容测试与完整套件，处理真实迁移问题。

```python
from robot_agent_runtime.kernel.core import RuntimeCore
from robot_agent_runtime.hosting.kernel_host import KernelHost
from robot_agent_runtime.repositories.memory import InMemoryTaskRepository
from robot_agent_runtime.repositories.sqlite import SQLiteTaskRepository
from robot_agent_runtime.transport.server import RuntimeServer
```

兼容入口示例（显式重导出保留静态分析；模块别名保留运行时补丁）：

```python
import sys
from robot_agent_runtime.kernel import core as _implementation
from robot_agent_runtime.kernel.core import RuntimeCore as RuntimeCore
sys.modules[__name__] = _implementation
```

## Task 2：文档与验证

- [x] README 更新真实模块树、新规范导入与旧入口说明，状态机明确目录变动不改变行为。
- [x] 更新 AGENTS 模块路径约定及当前架构 Markdown/HTML、执行恢复文档的源码链接。
  历史 docs/plans 记录保持原样；保留其他人已有的架构文档改动。
- [x] 独立代码审查，检查旧补丁路径、循环导入、包装安装与文档链接。
- [x] 容器普通 + asyncio debug/-W error 全量、compileall、现有示例与 CLI，离线构建 wheel
  验证新子包与兼容入口都被打包。容器保留，工作树不自动提交。

## 完成与验证

- 已迁移 17 个实现模块，增加四个轻量子包和对应旧路径兼容入口。三个新增测试先确认
  新包不存在而失败，迁移后通过；现有测试与示例使用新绝对导入。
- 使用迁移前工作树快照逐文件比对：17 个规范实现及另外 39 个既有 Python 文件仅有
  导入路径变化。78 个 Python 文件的宿主/容器 SHA-256 一致，257 个当前文档源码链接
  目标存在且行号在文件范围内；git diff --check 通过。
- 容器普通完整套件：401 passed，9.47 秒，退出码 0。
  `python3 -B -X pycache_prefix=/tmp/robot-directory-debug-full3 -m pytest -q -o faulthandler_timeout=10`
- 调试模式加 -W error、清理诊断包装运行完整套件：401 passed，11.83 秒，退出码 0。
  `PYTHONASYNCIODEBUG=1 python3 -B -X pycache_prefix=/tmp/robot-directory-monitored-debug-final -W error .verify/debug_layout_cleanup.py -o asyncio_default_fixture_loop_scope=function`
  诊断仅用独立计时线程观察现有反例测试，正常完成后撤销，不替换运行时代码。
- 验证限制：未加诊断的普通/调试重复运行曾在既有的
  `test_state_provider_sharing_a_blocked_skill_loop_cannot_deliver_precondition_loss` 清理阶段
  偶发挂起。把迁移前源码和同一测试放入容器独立临时目录，也复现超时（重复四轮一轮
  超时、三轮通过），所以不是目录迁移引入。此次没有混入取消或关闭行为修复。
- 容器 compileall、embedded_demo、remote_demo、precondition_demo、speech --help 和
  `python3 -m robot_agent_runtime --help` 通过。
- 虚拟环境没有 setuptools/wheel，改用容器已安装构建工具的 /usr/bin/python3 离线构建，
  无下载、无新增依赖。wheel 包含全部子包及旧入口；安装到容器 /tmp 独立目录后，验证
  17 对模块别名身份及一次嵌入式任务成功，确保没有依赖源码目录的可编辑安装。
- 独立审查未发现重要问题：旧补丁点、循环导入、SDK 公共导入和包发现均保持兼容。
- 保留原有未提交工作、历史计划与容器；不提交、不推送。

## 后续：移除兼容入口

- [x] 用户明确要求“不需要兼容”，删除 17 个旧根目录模块，保留新目录实现。
- [x] 包布局测试改为验证新路径可用、旧路径不存在、新路径补丁与独立进程导入顺序。
  删除前先确认两项失败，删除后重跑。
- [x] README 与 AGENTS 改为新路径约定，记录导入路径变更。
- [x] 容器定向、完整回归、干净构建及安装后的 wheel 验证，确认发布包没有残留旧模块。

验证结果：删除前布局测试 2 failed / 1 passed，删除后 3 passed。容器普通完整套件
401 passed（9.38 秒），调试 + -W error 完整套件 401 passed（11.21 秒），均退出码 0；
本轮无需诊断包装。compileall 通过。临时目录仅复制当前 src/pyproject/README，排除
旧 build 与 egg-info 后离线构建 wheel；确认 17 个旧模块缺席、17 个新模块存在，独立
安装后全部导入与一次嵌入式任务通过。独立审查与 git diff --check 通过。未提交。
