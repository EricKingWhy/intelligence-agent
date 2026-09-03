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
| **3** | Docker Sandbox + Coding Tools | ✅ COMPLETED | `2bfa457` → `b3e9596` | Sandbox ABC（含 list_files）+ LocalSubprocessSandbox + DockerSandbox（懒加载）+ 9 个 Coding Tools（read/write/bash/edit/grep/glob/apply_patch/git_status/git_diff）+ 批次调度验证 ✅。**仍缺**（独立后续 spec）：Session-scoped Sandbox 生命周期（Phase 4）、Approval / REQUIRE_APPROVAL（Phase 7 Capability）。Gate 达成：edit 多匹配明确失败、pytest exit_code=1 不被 retry、两个无冲突文件操作可并发、冲突写被串行化、路径越界统一 PERMISSION_DENIED。202 passed。 |
| **4** | Storage + Operation Ledger + Recovery | ⬜ NOT STARTED | — | 依赖 Phase 1 SessionEvent 完成。 |
| **5** | Artifact + MinIO + Context Compaction | ⬜ NOT STARTED | — | |
| **6** | Memory Capability / Context Provider | ⬜ NOT STARTED | — | |
| **7** | Capability / Plugin Foundation + Skills | ⬜ NOT STARTED | — | |
| **8** | MCP | ⬜ NOT STARTED | — | |
| **9** | Streaming Surfaces | ⬜ NOT STARTED | — | |
| **10** | Lightweight Web Session Inspector | ⬜ NOT STARTED | — | |
| **11** | Knowledge / RAG | ⬜ NOT STARTED | — | |
| **12** | Web Search / Reliability | ⬜ NOT STARTED | — | |
| **13** | Multi-Agent | ⬜ NOT STARTED | — | |
| **14** | Resume / Replay / Fork 完整化 | ⬜ NOT STARTED | — | 基础 Resume 在 Phase 1 完成；产品级 Replay / Fork / lineage tree 在此 Phase。 |
| **15** | Observability + Evaluation | ⬜ NOT STARTED | — | |
| **16** | Final Full E2E | ⬜ NOT STARTED | — | |

---

## 当前工作焦点

**Phase 3 Docker Sandbox + Coding Tools 已完成**（✅ COMPLETED）。全部 7 个 ticket（#12-#18）交付，9 个 V1 Coding Tools 全部就位。

下一步建议：进入 **Phase 4 Storage + Operation Ledger + Recovery**（已有 SessionEvent 事实源 + Sandbox 边界作为前置），或处理 **Phase 3 遗留的独立后续 spec**：Session-scoped Sandbox 生命周期（Phase 4）、Approval / REQUIRE_APPROVAL（Phase 7 Capability）。

## 更新日志

- 2026-09-03：初始建立。盘点 Phase 0-15 状态。
- 2026-09-03：Phase 1 SessionEvent 完成（Tickets A-D）。SessionEvent DTO + JsonlSessionStore + derive_messages + Session 聚合根 + AgentRuntime event-sourced 改造 + Resume 集成测试。135 passed，ruff clean。Issues #6-#10 已关闭。
- 2026-09-03：Phase 3 剩余 Coding Tools 完成（Tickets 1-7, #12-#18）。Sandbox list_files + edit/apply_patch/glob/grep/git_status/git_diff 6 个新工具 + 批次调度验证。202 passed，ruff clean。Issues #11-#18 已关闭。Session-scoped Sandbox 生命周期和 Approval 作为独立后续 spec。
