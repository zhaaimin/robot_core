# Agent 目录审查收尾

**目标：** 处理用户已确认的四项审查发现，保留现有业务、脚本指纹、任务与会话恢复语义。

**方案：** `validation.py` 只依赖标准库和应用错误；模型、机器人工具与脚本执行器从此导入 `positive_timeout`。酒店包入口仅保留说明，类型与实现使用精确模块路径。旧构建目录和元数据可恢复归档，再从当前项目直接构建 wheel。

**执行环境：** 当前 `agent/` 尚未提交，继续在现有工作树完成授权修改，不提交、不切换分支；测试和构建均复用 `robot_core_ubuntu24`。不增加依赖。

## 步骤

- [x] 在现有 `test_scenarios.py` 与 `test_hotel_errors.py` 增加独立进程导入检查，确认通用模块不加载场景装配、酒店错误和数据类型不加载 service/仓储/SDK；容器执行这两项，先观察失败。
- [x] 新建 `agent/src/robot_agent/validation.py`，原样迁移 `positive_timeout`，更新三个调用模块的绝对导入，并从 `configuration.py` 移除旧定义；酒店 `__init__.py` 去掉重导出。复跑导入检查和既有超时/工具/场景测试。
- [x] 同步根 README、Agent README 的模块树与导入边界，修正 HOTEL.md 的三处旧模块路径；补充发布前清理构建缓存的操作说明。
- [x] 将 `agent/build` 与 `agent/src/robot_agent_app.egg-info` 移至忽略的 `.verify/` 归档目录；从当前项目直接构建 wheel，独立安装并核对新模块、旧入口缺席、3 份技能正文、7 份参考资料、3 份脚本及批准入口调用。
- [x] 容器执行 Agent 全套、runtime 有界独立全套、compileall、pip check；独立审查改动，回读源码/文档/最终构建产物，记录真实结果。

## 已有证据

- 上轮容器源码测试：Agent 264 passed，runtime 497 passed。
- 独立导入 `robot_agent.skillkit.scripts` 会加载 `configuration` 与 `scenarios.registry`；导入酒店错误会加载 service、task_coordination 与 SDK。
- 干净源码副本 wheel 没有旧入口；包含现有 `build/` 的副本构建产物混入 tools.py 和五个旧 skill_*.py。

## 验证命令

```bash
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && /opt/robot_agent_venv/bin/python -m pytest -c agent/pyproject.toml agent/tests -q --tb=short'
docker exec robot_core_ubuntu24 bash -lc 'cd /workspace/robot_core && timeout --kill-after=5s 45s python3 -m pytest -q -o faulthandler_timeout=25'
```

## 结果

- 两项新增导入检查先得到 2 failed：通用模型加载了 configuration/scenarios.registry，酒店错误加载了 service/task_coordination/repository/SDK。迁移后，导入边界、模型、工具、脚本和场景组合 33 passed in 1.84s。
- Agent 源码完整套件：266 passed in 5.54s；当前 wheel 独立安装后使用安装目录优先的 PYTHONPATH 执行同一套件：266 passed in 5.38s。
- runtime 使用 45 秒上限独立串行完整回归：497 passed in 10.54s；两套环境 compileall 和 pip check 均通过。
- 当前项目直接 wheel 构建成功，10 个关键模块齐全，六个旧入口缺席；37 个 Python 文件与源码逐字节一致，当前 build/lib 的 Python 清单也与源码一致。
- 安装包包含 3 份技能正文、7 份参考资料和 3 份脚本，三个批准脚本入口均可调用。runtime 与技能资源共 55 个文件的 SHA-256 未变；迁移前后 positive_timeout 的 AST 完全一致。
- 独立只读审查未发现阻塞问题：调用方和当前文档无旧导入残留，业务与图恢复实现未修改。实现前快照包含当时已新增的测试，RED 证据来自上述实际测试输出。
- 验收目录：`.verify/agent-review-cleanup-venfa8io/`；旧缓存位于 `previous-build/`，wheel 位于 `wheels/`，独立安装位于 `installed/`，结构化产物检查结果为 `wheel-verification.json`。构建方法已写入 Agent README。
- 保留当前工作树与容器，未提交，未增加依赖，未连接真实模型或机器人硬件。
