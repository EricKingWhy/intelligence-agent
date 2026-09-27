# W-13 · Windows 重启/更新后的真实状态核对与手动续跑
**目标仓库**：intelligence-agent-backend（恢复 API / reconcile）+ intelligence-agent-frontend（恢复界面）；先服务端后客户端。

**类型/优先级**：P0 Recovery UX。**依赖**：W-06、W-12、[#337](https://github.com/EricKingWhy/intelligence-agent/issues/337) 陈旧审批结清、[#342](https://github.com/EricKingWhy/intelligence-agent/issues/342) resume CAS。**范围**：现有 `recovery/coordinator.py`、`recovery/reconcile.py`、Session/Operation 查询 API 与 Web/TUI 恢复页面；不重做 Tool-specific reconcile 算法。

## 工作指令

1. 用户手动打开应用后先列出上次运行的 Task、Run、工作目录与进度文件版本；严格复用 Spec 07 §9 顺序：events→workspace/sandbox→Ledger→reconcile→tool pair→context，之后才出现“继续”动作。未完成加载或版本不匹配时禁止启动模型。
2. UNKNOWN Operation 先使用现有 Tool-specific probe 查询外部系统/文件/服务状态；若确证已成功，补原 `tool_call_id` 结果；确证未执行才可按策略重做；仍未知则显示需要用户裁决。陈旧 Approval 依 #337 结清，不另外写成功。
3. 页面并排显示“已确认结果/仍未知/进度文件不一致/待验证证据/可执行下一步”。用户明确续跑时提交正确 version，409 后刷新权威状态并保留输入；不自动续跑。普通刷新/换 TUI 只读，不改变恢复事实。

**验收**：真实子进程在 SQLite 写入成功后、ToolResult append 前 kill；重启后先查数据库，仅有一份副作用，再经原 ToolResult 恢复；另测外部状态无法判断时用户裁决前 model/tool calls = 0；progress 文件损坏、陈旧审批、并发双续跑各显示准确结果。证据含 SessionEvent seq、Operation ID、DB 状态、UI 截图。**不做**：覆盖 #337/#342 代码范围或新增一套 RecoveryCoordinator。

**成熟参考/复用**：[DeepSeek Desktop 恢复流程](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)在退出/更新前查询活动任务并显示失败；[Anthropic 长任务交接](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)要求新窗口读进度。UI 设计 `PORT DESIGN`，Core reconcile `REUSE`。
