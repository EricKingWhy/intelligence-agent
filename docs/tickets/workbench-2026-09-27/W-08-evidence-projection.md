# W-08 · 验收项与测试/真实操作/diff 证据关联

**目标仓库**：`intelligence-agent-backend`（Python Core/API）。**类型/优先级**：P0 产品证据。**依赖**：W-07、W-06。**范围**：Event/API/Artifact 引用与工作区快照投影；候选 `src/agent_harness/session/service.py`、`src/agent_harness/web/artifacts.py`、现有 workspace/diff 工具。复用 `docs/SDD_WORKFLOW_PROTOCOL.md` 的真实入口验证与 review ledger，不建第二套工程 Gate。

## 每条证据最小字段

`evidence_id, task_session_id, run_id, criterion_id, kind(test|ui|diff|external), source_event_seq/tool_call_id, captured_at, result(pass|fail|blocked), command_or_action, exit_code_or_observation, artifact_ref, base_head, workspace_manifest_hash`。只引用权限受控的原件；保存失败时显示缺项。Manifest 对当前工作区内被验证的源码/数据文件列路径与 SHA-256，另记 `progress.md` hash；不自动 stage/commit。后续文件变动导致证据失效时显示具体变动，不能沿用旧“通过”。

## 工作指令

1. 从真实 Tool Result、现有 Artifact、实际浏览器结果/截图、Git diff 与 reviewer 结论建立关联；保留原始失败与重跑记录，不覆盖旧证据。
2. 必需 AC 只有存在可回读、版本匹配、结果通过的证据才能标“通过”；无 Chrome/MCP 时浏览器项是“未完成/缺工具”，不是 pass。用户手工验收事实与机器验证事实分列。
3. 差分审阅使用现有工具/端点：展示未跟踪文件、进度文件、当前 HEAD、工作区快照与测试时快照的差别；不执行 `git add`/commit/push。

**验收**：成功/失败 pytest、浏览器缺插件、浏览器实际访问、测试后改一行源码、Artifact 丢失、进度文件元数据更新、用户带缺项接受；API 刷新重建同样证据与 stale 标识，敏感输出值不进正文。focused API/session/artifact tests + 一次真实仓库 diff/命令记录。**不做**：重跑 #319 五个 Runtime Gate 或新审查台账。

**成熟参考/复用**：[Codex app 的 diff/review](https://openai.com/index/introducing-the-codex-app/)与 [Codex long-horizon 测试循环](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex/)供交互与证据次序参考；本仓 V3.1-lite、ArtifactStore、Git diff `REUSE`。
