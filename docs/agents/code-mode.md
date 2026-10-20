# Code Mode（#1360）

- 共存：普通任务优先直接工具调用；多步聚合可以搜索并激活 `exec`，执行原始 Python 源码（支持顶层 `await tools.read(path='...')`）。
- 动态曝光由 `ToolRegistry.resolve_exposure(tool)` 统一判定。当前 registry 中除 `exec` / `tool_search` 的工具数 **>30** 时，MCP 默认延迟曝光；核心工具保持直接曝光。显式 exposure 配置优先。
- `exec` 在工具数 **≤30** 时隐藏且不可搜索；超过阈值后延迟曝光，通过 `tool_search` 激活。main / coding 包含 exec；research 不包含。
- 仅支持 Docker：计算容器不挂用户工作区，无网络出口，根文件系统只读，只能写临时 `/tmp`；不回退宿主执行。所有外部副作用走原 ToolExecutor，嵌套调用逐次验证、审批、记账，串行且保留资源锁。
- 默认输出上限 10,000 tokens、执行上限 300 秒；`yield_time_ms` 是同步等待中的控制轮询间隔，不创建后台 cell。
- `parent_exec_call_id` 关联子调用的 SessionEvent 与 Operation Ledger。崩溃后已成功的子操作保持提交，未知外层 exec 需要 reconcile，不能整段自动重跑。SQLite 新列采用可空 additive migration，不删旧数据。
- 保留直接调用；是否扩大使用以配对实验决定，不默认启用 CodeModeOnly。来源、限制与实验原始结果见 [调研及验证记录](../research/2026-10-20-1360-code-mode.md)。
