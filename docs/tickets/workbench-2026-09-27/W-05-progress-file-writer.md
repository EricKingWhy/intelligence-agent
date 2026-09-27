# W-05 · 项目可见进度文件：来源、脱敏、原子写
**目标仓库**：intelligence-agent-backend（Python SessionStore / 进度文件）。

**类型/优先级**：P0 产品事实投影。**依赖**：W-02。**范围**：新的小型 ProgressFile writer + SessionEvent 投影接口、测试；候选位置 `src/agent_harness/session/`，不把文件写入 Memory V2。

## 文件契约

默认 `<用户选定项目根>/agent-progress/<session-id>/progress.md`，每个 Session 独立。固定标题/字段：schema_version、session_id、parent_session_id/fork_point_seq（可空）、source_event_seq、generated_at、原目标、约束/授权、验收项与状态、已验证里程碑、决策、失败尝试/坑点、未决 Operation、证据 refs、阻塞与下一步。保留精确 ID/金额/用户禁令的原文字面和来源 seq，但**绝不**写凭证值、Cookie、`.env` 值、私密原始 Tool 输出。大原文只留 Artifact ref。文件可进入 Git diff；系统不 `git add`。

## 工作指令

1. 从 W-02 保护事实与既有 SessionEvent 确定性生成内容；首次在目标/约束确认后创建，里程碑/决策/失败结论/压缩前/暂停前/交付前更新。
2. 使用同目录临时文件、flush/fsync（平台可行处）与原子替换；保留可恢复的上一版本及 source seq/hash。Windows 文件锁、进程 kill、权限拒绝、磁盘满不得留下半文件。
3. 脱敏在写入前；检测无法安全呈现的内容时阻止该段而标出缺项。写失败在任务状态可见，不得把旧文件说成最新。

**验收**：正常创建/重复更新幂等，两个 Session 不互覆；写入中 kill 后旧版或新版完整可读；外部只读/锁定目录出现明确失败；文件在 Git diff 中可见但 index 不被自动修改；用假凭证断言正文与日志都无值。focused Windows 文件系统测试与真实目录操作。**不做**：UI 编辑、Fork、自动提交。

**成熟参考/复用**：[Anthropic `claude-progress.txt`](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)与 [Codex 长任务项目文件](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex)提供外置交接思路，`PORT DESIGN`；本仓事件与 Artifact 直接 `REUSE`。
