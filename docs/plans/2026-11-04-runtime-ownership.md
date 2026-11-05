# 托管装配与生命周期职责调整

> 用户已批准前两步：装配归托管层、显式分阶段关闭。使用 writing-plans 与 TDD 实施，
> 暂不拆 hosting/isolation.py；保持当前未提交工作与容器，不提交或推送。

**Goal:** 内核只接收准备好的端口，宿主管理线程、外部代理与关闭次序，移除私有属性注入。

**Architecture:** KernelHost 接收原始端口和 ThreadBinding，在内核线程准备稳定的
IsolatedPorts，再创建唯一 RuntimeCore；失败重试复用已创建的线程和内核，准备失败不可替换
线程。RuntimeCore 接受普通端口、RuntimeOptions 与 AdmissionValidator Protocol，
默认 LocalAdmissionValidator，托管模式使用隔离校验代理；不导入 hosting/ThreadBinding。
Core.stop() 共享真实停止结果并结束事件生产，Core.close() 复用停止结果后关闭仓储和清理
本机状态；Host 在 stop 与 close 之间排空事件，最后有限关闭外部线程与内核线程。

**Tech Stack:** Python 3.11+、现有 LoopThread/pytest，禁止新增依赖。

## 生命周期与接口

- RobotRuntime(...) 的业务构造/方法保持；不预先创建内核。RuntimeServer 改为
  RuntimeServer(repository, skills, socket_path, *, reconciler=None,
  allowed_permissions=None, robot_state=None, options=None)，接入原始端口。
- KernelHost 同样接收原始端口与 options；SDK/Server 调用 begin_close() 启动共享关闭任务，
  不返回内部 LoopJob；close() 等待同一次关闭结果。监听清理仍在业务停止发起之后。
- Core 保留直接 start/close 用法；stop 在仓储存活期间完成任务终态和结束实时事件。
  stop/close 各自共享完成/异常。stop 失败时仍排空事件、关闭仓储和线程，错误不重复报告。
- 代理与内核只在内核循环创建和准备。仓储 start/close 仍由 Core 的普通端口生命周期方法
  调用，外部循环由 Host 管理；低层 Core 无线程边界，按调用方契约直接管理仓储。
- 仓储超时毒化、任务总期限、停止确认、资源隔离、分页回放与有界关闭缓冲保持。
  核心依赖不在构造后替换，移除 _event_drainer / isolate() / _port_inputs / _isolated_ports。
- AdmissionValidator.validate(call, unsatisfied)->None 只执行准入规则；规则在 kernel/policy.py，
  隔离执行位置在 hosting，权限配置为调用前复制的普通数据。新增可复用端口契约测试。

## Task 1：先写回归

- [x] AST 检查 kernel 不依赖 hosting/thread_binding；验证 SDK 未启动无内核、构造发生在内核
  线程且依赖一次绑定；Core.stop 可完成终态/回放而未关闭仓储，重复 stop/close 结果共享。
- [x] 验证 host begin_close 与取消等待者、启动失败复用、排空失败仍释放资源。
- [x] 新服务端构造跑远程任务；本地和隔离 AdmissionValidator 契约覆盖 schema/权限/条件。
- [x] 容器确认新测试因缺失接口/旧职责而失败；当前基线 401 passed（9.36 秒）。

## Task 2：实现与迁移

- [x] ports.py 新增 AdmissionValidator；policy.py 本地实现；isolation.py 提供有界校验代理。
- [x] Core 删除线程装配，构造时传入配置，stop/close 分阶段且共享真实结果，EventBus 构造
  接受 page_size/queue_size，避免私有写入。
- [x] Host 延迟装配与 _create_core，在 kernel loop 创建；明确 prepare/start/stop/drain/close
  顺序、异常聚合、初始化失败禁止补线程；公开 begin_close()。
- [x] SDK/Server/CLI/示例迁移；现有测试的启动前内核补丁改为托管创建钩子，保留测试场景。

## Task 3：审查与完整验证

- [x] README 四处与当前架构 Markdown/HTML、执行恢复文档、AGENTS 更新，历史计划不改。
- [x] 容器定向与普通/asyncio debug -W error 全量回归，compileall/示例/CLI/干净 wheel。
- [x] 独立审查启动失败、跨循环启停、停止错误、事件排空与仓储关闭；修复发现并保留回归。
- [x] 核对最终文件与验证容器一致、diff 检查；容器和工作区保留。

## 完成与验证

- 新增 11 项职责/生命周期回归与 2 项准入校验契约。首批职责测试 6 failed，校验接口
  2 failed，确认缺失接口后实现；原 401 项基线保留，现 414 项。
- 容器普通完整套件：414 passed，9.90 秒，退出码 0。
  `python3 -B -X pycache_prefix=/tmp/robot-ownership-full2 -m pytest -q -o faulthandler_timeout=15`
- 容器调试 + 警告视为错误：414 passed，11.51 秒，退出码 0。
  `PYTHONASYNCIODEBUG=1 python3 -B -X pycache_prefix=/tmp/robot-ownership-debug1 -W error -m pytest -q -o asyncio_default_fixture_loop_scope=function -o faulthandler_timeout=15`
- 停止异常的单层聚合先被原前置条件回归捕获，修复后保持旧错误可见性；独立审查生命周期
  未发现剩余问题。文档两处旧职责说明与偏移行号经审查修正，254 处源码链接均有效。
- 宿主/容器 62 个 Python 文件 SHA-256 一致，compileall、embedded/remote/precondition
  示例、speech --help、CLI --help 均通过。干净临时源码目录离线构建并独立安装 wheel，
  新导入存在、17 个旧入口缺席，嵌入式任务成功；未添加依赖或调用真实机器人。
- RuntimeServer 构造已改为原始端口 + socket_path；低层 Core 只接收普通端口，SDK
  业务构造保持。hosting/isolation.py 本轮仅增加准入代理，没有拆分文件。
- git diff --check 通过，保留全部未提交修改与现有验证容器，无提交或推送。
