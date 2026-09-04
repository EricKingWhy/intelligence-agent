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
| **5** | Artifact + S3 + Context Compaction（ADR-0006） | ✅ COMPLETED | `fd7439a`（#45）<br>`c98c9a5`（#46）<br>`de6a894`（#47）<br>`80086a9`（#48）<br>`e19905d`（#49）<br>`c0a4b0f`（#50） | ArtifactStore / Fake / S3 / inspect_artifact、Overflow Handler、ContextBuilder、三层 Compaction、Runtime/Web 装配全部落地。真实七牛 5000 行 Bash 溢出、局部回读、历史重建 Gate 通过。全量 424 passed；真实七牛 2 passed；ruff clean。证据：docs/PHASE5_GATE.md。 |
| **6** | Memory Capability / Context Provider | ✅ COMPLETED | `21a9e2f`（#51）+ `93d2e18`（#52）+ `62db7de`（#53）+ `e2a3a92`（#54）+ `df2b3e8`（#55）+ `5c6d40f`（deps）+ `96d02bf`（#56 Web 接线 + Review 修复）+ Gap 收口 commit | IdentityContext + SQLite MemoryRecordStore + Milvus 向量适配 + Outbox Relay + LangMem Capability + Extractor 三层降级 + Writeback + Web 入口接线全部落地；USER/SESSION 隔离、事务 outbox。**真实 Zilliz + SiliconFlow 嵌入 Gate 3 passed（连续两轮）**：真实 CRUD 闭环、语义检索排序、多租户隔离、schema mismatch、清理全过。503 passed、8 skipped、8 deselected，ruff clean。证据：docs/PHASE6_GATE.md。 |
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

**Phase 6 Memory Capability / Context Provider 已完成（真实 Zilliz Gate 收口）**。当前焦点：Phase 9 前端 Gap 补齐（first_user_message / usage / trace_id）+ Directive B 高危隐患修复。下一 Phase 为 Phase 7。

## 更新日志

- 2026-09-04：#50 完成（`c0a4b0f`），Phase 5 Gate 达成。Runtime 单一 ContextBuilder 入口、hard guard 终止、持久事件镜像、Web Settings/S3 装配；补齐批次部分失败时的事件发送及并行调用收敛。全量 424 passed、8 skipped、5 deselected，真实七牛 Provider 与完整 Gate 2 passed，ruff / lock 检查通过。审查问题修复后复核通过；测试专属 S3 对象已清理。密钥仅在本地忽略文件，验收文档不含凭证。

- 2026-09-04：#49 完成（`e19905d`）。ContextCompactor 结构化摘要 → 机械提取 → hard guard；保留当前 turn 和 Tool 原子块，ContextBuilder 集成并追加 context/compacted。新增 18 项测试；全量 415 passed、8 skipped、4 deselected，ruff / lock 检查通过。Standards 无问题；Spec 审查发现摘要未以 auto 为目标，已补回归并修复、复核通过。Runtime 捕获异常和流式事件镜像留 #50。

- 2026-09-04：#48 完成（`80086a9`）。S3ArtifactStore 用可选 aioboto3（Apache-2.0）实现 save/load/inspect，复用 `_slice_lines`，Session 绑定实现可重启寻址，正文与元数据同次上传。新增 11 项 Provider 测试与 1 项默认排除的真实七牛测试。全量（artifact extra）：397 passed、8 skipped、4 deselected；ruff / lock 检查通过，Standards / Spec 审查均无问题。真实七牛连通性仍待 #50 凭证 Gate。

- 2026-09-04：#47 完成（`de6a894`）。ArtifactOverflowHandler 自动保存大输出、返回首尾摘要与 ref，支持 Bash stdout/stderr、多字段与单行超长输出；Executor 在 Tool retry 之后、Ledger 终态之前处理，存储失败保留 RUNNING 待 reconcile。新增 12 个测试，事件词表同步；全量 `python -X utf8 -m pytest tests/ -q --tb=short`：386 passed、8 skipped、3 deselected；ruff / lock 检查通过，Standards / Spec 审查均无问题。Runtime 接线留 #50。

- 2026-09-04：Codex 接手 Phase 5，补登 #45（`fd7439a`），完成 #46：REUSE tiktoken（MIT，https://github.com/openai/tiktoken），BUILD ContextBuilder 薄集成，复用 Session.derive_messages；仅定义 ContextProvider Protocol，未提前做 Compaction / Memory。新增 6 个行为测试；全量 `python -X utf8 -m pytest tests/ -q --tb=short`：374 passed、8 skipped、3 deselected，ruff clean、uv lock --check 通过，Standards / Spec 双轴审查均无问题。Windows 默认编码下既有 `test_mapping_json_written` 失败，`python -X utf8` 可通过；未修改该范围外测试。

- 2026-09-03：初始建立。盘点 Phase 0-15 状态。
- 2026-09-03：Phase 1 SessionEvent 完成（Tickets A-D）。SessionEvent DTO + JsonlSessionStore + derive_messages + Session 聚合根 + AgentRuntime event-sourced 改造 + Resume 集成测试。135 passed，ruff clean。Issues #6-#10 已关闭。
- 2026-09-03：Phase 3 剩余 Coding Tools 完成（Tickets 1-7, #12-#18）。Sandbox list_files + edit/apply_patch/glob/grep/git_status/git_diff 6 个新工具 + 批次调度验证。202 passed，ruff clean。Issues #11-#18 已关闭。Session-scoped Sandbox 生命周期和 Approval 作为独立后续 spec。
- 2026-09-03：Phase 3 后续独立 spec 全部落地（Tickets A-E, #21-#25）。Approval 机制（PermissionPolicy + ToolPermission + ToolExecutor 审批关卡 + 默认安全拒绝 danger）+ Session-scoped Sandbox 生命周期（WorkspaceRegistry + Session.start/resume 自动绑定 + DockerSandbox 确定性命名 + 跨进程恢复）。252 passed，ruff clean。Issues #19-#25 已关闭。Phase 3 全部交付物落地。
- 2026-09-04：Phase 4 开始实施。Ticket #27 落地 SQLite Operation Ledger、Operation 状态契约、ToolExecutor Ledger-first lifecycle 与 aiosqlite 依赖；#28-#33 待完成。
- 2026-09-04：Phase 9+10 精简版提前施工（grill-with-docs 产出，见 ADR-0005 + CONTEXT.md「Streaming / Web UI 层」术语）。决策：SSE + REST POST（守规格默认）、React 18 + Vite + Radix + 纯 CSS（液态玻璃）、AgentRuntime 加 `run_stream`（保留旧 `run()`）、前端直接消费 raw SessionEvent（守不变量 #22）、工具卡片 bash/diff 专属 + 其余通用、diff 数据工具侧补返回、approval 内联卡片、`web/` 顶层目录。前后端均由本次会话主导，不覆盖后端会话已落地的 Phase 4 ADR/CONTEXT。
- 2026-09-04：Phase 4 全部完成（Tickets #27/#28/#29/#30/#31/#32/#33）。Checkpoint 持久化、RecoveryCoordinator 8 步恢复、UNKNOWN 人工裁决、真实子进程 Kill 集成测试全部落地。Phase Gate 达成：duplicate confirmed side effect = 0、dangling tool call = 0、Workspace 恢复正确、5 个独立 Kill 场景全过。345 passed, 8 skipped，全仓 ruff clean（含 phase9-10 合并带入的历史 lint 清零）。

- 2026-09-04：#51 IdentityContext、JWT 中间件完成。PyJWT（MIT）REUSE，HS256 固定验签，local 默认值；身份不进事件流。scopes 构造时复制为 tuple 防共享修改。6 项新测试，457 passed，ruff / lock 检查通过。Standards 问题修复后复核通过，Spec 无问题。

- 2026-09-04：#52 完成，15 项共享契约/事务测试，472 passed。SESSION namespace 追加可信 session_id（用户批准，ADR-0009 补充）；跨租户/用户/会话读取和覆盖拒绝，权限scope校验，record/outbox原子提交。score为查询态不持久化，Fake/SQLite一致。双轴审查通过。

- 2026-09-04：#53 完成，memory extra pymilvus、Milvus/FakeVectorStore、分页 outbox relay。480 passed，ruff clean。双轴发现的饥饿、文档/查询编码和空ID问题已修复复核；真实Zilliz正式adapter连接/list_collections通过，无数据写入；语义Gate待embedding配置与#54–#56。

- 2026-09-04：#54 完成。LangMem 0.0.30（MIT）仅 memory extra；Formation/Consolidation 经项目 BaseStore 写 SQLite + outbox，Core 无 concrete class 依赖。Extractor 三层降级、异步取消保留；10 项新测试，全量 490 passed，ruff / lock clean。双轴审查修复 nearest-hit 误判为合并的问题并复核通过。

- 2026-09-04：#55 完成。MemoryContextProvider 按 relevance/recency/importance 选择、单条 SystemMessage 注入；Builder 最终预算裁剪及可替换 Provider 故障隔离。Runtime run/run_stream 结束后后台抽取当前 run，继承身份、绑定可信 session_id；writer 支持 drain/close。memory/degraded 纳入统一事件词表和生成类型。双轴审查问题修复复核通过。

- 2026-09-04：#56 部分验收：真实 Zilliz 连接/list_collections、缺失 Collection 和无效 token 两项集成测试通过；修复 SDK 包裹 gRPC 认证错误的映射并补回归。全量 502 passed、8 skipped、7 deselected。无真实数据写入，完整 Gate 等待 embedding 模型，#56 保持未完成。
