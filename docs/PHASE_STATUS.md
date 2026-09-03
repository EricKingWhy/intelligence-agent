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
| **1** | SessionEvent + Model + Minimal Agent Loop | 🔄 IN PROGRESS | `f789aac`（AgentRuntime）<br>`8ecb202`（bind_tools） | ModelProvider + minimal AgentRuntime + max_steps 已落地。**缺**：SessionEvent DTO、append-only JSONL SessionStore、derive_messages、基础 Resume、Runtime 改造为 event-sourced。ADR-0003 已冻结设计。 |
| **2** | Tool Runtime | ✅ COMPLETED | `8572fd8` → `00f3753` | Tool Contract / Registry / ToolResult / Validation-first ToolExecutor / 单 Retry Layer / 批次调度 + 严格 ID 配对。Gate 达成：INVALID_ARGUMENT 不重试、transient 可重试、配对 100%。Permission interface 薄，留待 Phase 7 Capability seam 深化。 |
| **3** | Docker Sandbox + Coding Tools | 🔄 PARTIAL | `2bfa457` → `76049d0` | Sandbox ABC + LocalSubprocessSandbox + DockerSandbox（懒加载）+ 24 契约测试 ✅。read/write/bash ✅。**缺**：edit / grep / glob / apply_patch / git_status / git_diff；Session-scoped Sandbox 生命周期；Approval（REQUIRE_APPROVAL）。 |
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

**Phase 1 SessionEvent 模块**（进行中）。设计已通过 grilling 冻结（ADR-0003）。下一步：`/to-spec` → `/to-tickets` → `/implement`。

## 更新日志

- 2026-09-03：初始建立。盘点 Phase 0-15 状态。
