# Phase Status — 实施进度追踪

> **单一事实源**：任何 agent 进入项目前先读本文件，判断"当前做到哪里"。
> 规格文件（`docs/spec/14_IMPLEMENTATION_ROADMAP.md` 等）保持冻结，不被进度修改。
> 每完成一个 Phase，更新本文件对应行的状态 + commit hash + Gate 证据。

更新规则：
- `✅ COMPLETED`：Phase 全部交付物落地 + Gate 达成 + 测试通过。
- `🔄 IN PROGRESS`：部分交付物已落地，剩余项明确。
- `🔄 PARTIAL`：跨 Phase 的能力部分提前落地（如 Phase 3 工具在 Phase 1 阶段已部分实现）。
- `⬜ NOT STARTED`：未涉及。

---

## 进度表

| Phase | 名称 | 状态 | 关键 commit | Gate 证据 / 备注 |
|---|---|---|---|---|
| **0** | Repo Foundation | ✅ COMPLETED | `21b421d` 起 | Python 3.11+ / uv / pytest-asyncio / ruff / pydantic-settings / JSONL Diagnostic Logger。`config.py` + `logging.py` + 测试 harness 就位。 |
| **1** | SessionEvent + Model + Minimal Agent Loop | ✅ COMPLETED | `f789aac`（AgentRuntime）<br>`8ecb202`（bind_tools）<br>`87c3a72`（event-sourced） | SessionEvent DTO + 10 种 event type + JsonlSessionStore（崩溃安全）+ derive_messages（配对+dangling 合成）+ Session 聚合根（start/resume/append/derive/begin_run/end_run）+ AgentRuntime event-sourced 改造 + Resume 集成测试。Gate 达成：进程重启后可从 JSONL 恢复完整对话历史。135 passed。 |
| **2** | Tool Runtime | ✅ COMPLETED | `8572fd8` → `00f3753` | Tool Contract / Registry / ToolResult / Validation-first ToolExecutor / 单 Retry Layer / 批次调度 + 严格 ID 配对。Gate 达成：INVALID_ARGUMENT 不重试、transient 可重试、配对 100%。Permission interface 薄，留待 Phase 7 Capability seam 深化。 |
| **3** | Docker Sandbox + Coding Tools | ✅ COMPLETED | `2bfa457` → `ed96f5a` | Sandbox ABC（含 list_files）+ LocalSubprocessSandbox + DockerSandbox（懒加载 + 确定性命名 + 跨进程恢复）+ 9 个 Coding Tools（read/write/bash/edit/grep/glob/apply_patch/git_status/git_diff）+ 批次调度验证 ✅。**后续独立 spec 全部落地**：Approval / REQUIRE_APPROVAL 机制（PermissionPolicy + ToolPermission + ToolExecutor 审批关卡 stage 2.5 + per-call scoping，默认安全拒绝 danger）；Session-scoped Sandbox 生命周期（WorkspaceRegistry 映射持久化 + Session.start/resume 自动绑定 sandbox + Docker 后端跨进程恢复）。Gate 达成：edit 多匹配明确失败、pytest exit_code=1 不被 retry、两个无冲突文件操作可并发、冲突写被串行化、路径越界统一 PERMISSION_DENIED、DANGER 工具在非 full-access policy 下被审批关卡拦截。252 passed。 |
| **4** | Storage + Operation Ledger + Recovery | ✅ COMPLETED | `5b85e36`（#27 Ledger）<br>`6c94398`（#28 Checkpoint）<br>`fbcf599`（#29 RecoveryCoordinator）<br>`596c53b`（#30 Reconcile）<br>`778c1da`（#32 Kill 集成测试） | 全部 6 个 Ticket 落地：durable Operation Ledger（SQLite + aiosqlite，Ledger-first 写入顺序）+ 稳定边界 Checkpoint 持久化（CheckpointPolicy seam，checkpoint 绝不进事件流）+ RecoveryCoordinator（07 §9 冻结 8 步唯一入口，决策与写入分离，BEGIN EXCLUSIVE sidecar 恢复锁）+ UNKNOWN 人工裁决（ReconcileHint / ReconcileVerdict / ReconcileCallback，RUNNING→UNKNOWN→NEED_RECONCILE 两步状态机强制，RETRY 只能来自用户裁决，operation/reconcile-required 落盘）+ 真实子进程 Kill 恢复集成测试。Gate 达成：duplicate confirmed side effect = 0、dangling tool call = 0、Workspace 按原映射恢复、5 个独立 Kill 场景全过、PENDING 默认 skip 不盲跑。345 passed, 8 skipped；全仓 ruff clean。 |
| **5** | Artifact + S3 + Context Compaction（ADR-0006） | 🔄 IN PROGRESS | `fd7439a`（#45）<br>`c98c9a5`（#46）<br>`de6a894`（#47） | #45 ArtifactStore / Fake / inspect_artifact；#46 ContextBuilder 基础投影 + token 估算；#47 Overflow Handler + Executor + artifact/created 已落地。#48–#50 待实施，尚未达到 Phase Gate。 |
| **6** | Memory Capability / Context Provider | ⬜ NOT STARTED | — | |
| **7** | Capability / Plugin Foundation + Skills | ⬜ NOT STARTED | — | |
| **8** | MCP | ⬜ NOT STARTED | — | |
| **9** | Streaming Surfaces | 🔄 IN PROGRESS (精简版提前) | — | Phase 9+10 精简版提前施工（见 ADR-0005）。FastAPI + SSE + REST POST + AgentRuntime.run_stream。技术硬前置（Phase 1 events + Phase 2 tool events）已满足；跳过 Phase 4-8 完整交付，由用户明确授权。 |
| **10** | Lightweight Web Session Inspector | 🔄 IN PROGRESS (精简版提前) | — | 与 Phase 9 同步提前。三栏 Inspector + chat 流 + 流式 token + 工具卡片 + approval + 历史 session 回放。Step Detail 里 Phase 4-5 字段（checkpoint/artifact/operation state）留空槽 + graceful empty state，后续 Phase 填。React 18 + Vite + Radix + 纯 CSS（液态玻璃风）。 |
| **11** | Knowledge / RAG | ⬜ NOT STARTED | — | |
| **12** | Web Search / Reliability | ⬜ NOT STARTED | — | |
| **13** | Multi-Agent | ⬜ NOT STARTED | — | |
| **14** | Resume / Replay / Fork 完整化 | ⬜ NOT STARTED | — | 基础 Resume 在 Phase 1 完成；产品级 Replay / Fork / lineage tree 在此 Phase。 |
| **15** | Observability + Evaluation | ⬜ NOT STARTED | — | |
| **16** | Final Full E2E | ⬜ NOT STARTED | — | |

---

## 当前工作焦点

**Phase 5 Artifact + Context Compaction 正在实施**。#45–#47 已落地；下一项 #48 S3ArtifactStore，随后 #49 Compaction。#50 负责 Runtime 接线并需要真实七牛云凭证验证 Gate。冻结决策见 ADR-0006 / ADR-0007，交接见 `docs/HANDOFF_PHASE5.md`。

## 更新日志

- 2026-09-04：#47 完成（`de6a894`）。ArtifactOverflowHandler 自动保存大输出、返回首尾摘要与 ref，支持 Bash stdout/stderr、多字段与单行超长输出；Executor 在 Tool retry 之后、Ledger 终态之前处理，存储失败保留 RUNNING 待 reconcile。新增 12 个测试，事件词表同步；全量 `python -X utf8 -m pytest tests/ -q --tb=short`：386 passed、8 skipped、3 deselected；ruff / lock 检查通过，Standards / Spec 审查均无问题。Runtime 接线留 #50。

- 2026-09-04：Codex 接手 Phase 5，补登 #45（`fd7439a`），完成 #46：REUSE tiktoken（MIT，https://github.com/openai/tiktoken），BUILD ContextBuilder 薄集成，复用 Session.derive_messages；仅定义 ContextProvider Protocol，未提前做 Compaction / Memory。新增 6 个行为测试；全量 `python -X utf8 -m pytest tests/ -q --tb=short`：374 passed、8 skipped、3 deselected，ruff clean、uv lock --check 通过，Standards / Spec 双轴审查均无问题。Windows 默认编码下既有 `test_mapping_json_written` 失败，`python -X utf8` 可通过；未修改该范围外测试。

- 2026-09-03：初始建立。盘点 Phase 0-15 状态。
- 2026-09-03：Phase 1 SessionEvent 完成（Tickets A-D）。SessionEvent DTO + JsonlSessionStore + derive_messages + Session 聚合根 + AgentRuntime event-sourced 改造 + Resume 集成测试。135 passed，ruff clean。Issues #6-#10 已关闭。
- 2026-09-03：Phase 3 剩余 Coding Tools 完成（Tickets 1-7, #12-#18）。Sandbox list_files + edit/apply_patch/glob/grep/git_status/git_diff 6 个新工具 + 批次调度验证。202 passed，ruff clean。Issues #11-#18 已关闭。Session-scoped Sandbox 生命周期和 Approval 作为独立后续 spec。
- 2026-09-03：Phase 3 后续独立 spec 全部落地（Tickets A-E, #21-#25）。Approval 机制（PermissionPolicy + ToolPermission + ToolExecutor 审批关卡 + 默认安全拒绝 danger）+ Session-scoped Sandbox 生命周期（WorkspaceRegistry + Session.start/resume 自动绑定 + DockerSandbox 确定性命名 + 跨进程恢复）。252 passed，ruff clean。Issues #19-#25 已关闭。Phase 3 全部交付物落地。
- 2026-09-04：Phase 4 开始实施。Ticket #27 落地 SQLite Operation Ledger、Operation 状态契约、ToolExecutor Ledger-first lifecycle 与 aiosqlite 依赖；#28-#33 待完成。
- 2026-09-04：Phase 9+10 精简版提前施工（grill-with-docs 产出，见 ADR-0005 + CONTEXT.md「Streaming / Web UI 层」术语）。决策：SSE + REST POST（守规格默认）、React 18 + Vite + Radix + 纯 CSS（液态玻璃）、AgentRuntime 加 `run_stream`（保留旧 `run()`）、前端直接消费 raw SessionEvent（守不变量 #22）、工具卡片 bash/diff 专属 + 其余通用、diff 数据工具侧补返回、approval 内联卡片、`web/` 顶层目录。前后端均由本次会话主导，不覆盖后端会话已落地的 Phase 4 ADR/CONTEXT。
- 2026-09-04：Phase 4 全部完成（Tickets #27/#28/#29/#30/#31/#32/#33）。Checkpoint 持久化、RecoveryCoordinator 8 步恢复、UNKNOWN 人工裁决、真实子进程 Kill 集成测试全部落地。Phase Gate 达成：duplicate confirmed side effect = 0、dangling tool call = 0、Workspace 恢复正确、5 个独立 Kill 场景全过。345 passed, 8 skipped，全仓 ruff clean（含 phase9-10 合并带入的历史 lint 清零）。
