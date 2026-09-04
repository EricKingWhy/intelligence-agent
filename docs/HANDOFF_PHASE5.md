# Phase 5 交接手册 — Artifact + Context Compaction

> **交接对象**：Codex（Secondary / Task Agent）
> **交接时间**：2026-09-04
> **交接人**：ZCode（本会话）
> **当前进度**：#45–#47 已完成，#48–#50 待实施（Codex 2026-09-04 更新）

---

## 1. 背景与决策来源

Phase 5 的全部决策已通过 `/grill-with-docs` 四轮拷打冻结，文档产出在：

| 文档 | 内容 |
|------|------|
| `CONTEXT.md` | Phase 5 新增 12 条术语（Artifact / ArtifactStore / Overflow / inspect_artifact / ContextBuilder / estimate_tokens / Compaction / 三层降级 / ContextProvider / artifact/created / context/compacted） |
| `docs/adr/0006-artifact-store-qiniu-s3-compatible.md` | 对象存储用七牛云 Kodo S3 兼容（aioboto3），不做 Local/MinIO |
| `docs/adr/0007-context-compaction-three-tier-fallback.md` | Compaction 三层降级（LLM→机械→拒绝）+ tiktoken cl100k_base 精确计数 + max_context_tokens=200000 |
| GitHub Issue #44 | Phase 5 Spec（完整 user stories + implementation decisions + acceptance criteria） |

**Codex 必读**：开始任何 ticket 前先读 #44 + 两个 ADR + CONTEXT.md 的「Artifact / Context 层」段落。

---

## 2. Ticket 状态

| # | GitHub | 标题 | 状态 | Blocked by |
|---|--------|------|------|------------|
| 1 | #45 | ArtifactStore ABC + FakeArtifactStore + inspect_artifact Tool | ✅ DONE (fd7439a) | — |
| 2 | #46 | estimate_tokens + ContextBuilder base (no Compaction) | ✅ DONE (c98c9a5) | — |
| 3 | #47 | Overflow Handler + Executor 集成 + artifact/created | ✅ DONE (de6a894) | #45 ✅ |
| 4 | #48 | S3ArtifactStore (七牛云 S3 兼容) | ⬜ TODO | #45 ✅ |
| 5 | #49 | Context Compactor 三层降级 + context/compacted | ⬜ TODO | #46 |
| 6 | #50 | AgentRuntime 集成 + Phase 5 端到端测试 | ⬜ TODO | #47, #48, #49 |

**可立即开始的**：#48、#49（前置 #45 / #46 已完成）。

**建议执行顺序**：#48 → #49 → #50（串行）。

**#46 接口说明**：`ContextBuilder.build` 和 `ContextProvider.select` 为 async，调用时使用 `await`；token 估算计入消息结构（含 tool_calls），记录在诊断日志，不写 SessionEvent。当前 build 即使超过阈值也返回完整投影，阈值执行留给 #49。

---

## 3. #45 已交付内容（供后续 ticket 参考）

### 新建文件

- `src/agent_harness/storage/artifact.py`
  - `Artifact(BaseModel)`: artifact_id / session_id / size / mime_type / source_tool / tool_call_id / created_at / content(可选)
  - `ArtifactSlice(BaseModel)`: artifact_id / lines / total_lines / returned_lines / truncated / query
  - `ArtifactStore(ABC)`: `save()` / `load()` / `inspect()` 三个 async 抽象方法
  - `compute_artifact_id(content) -> str`: SHA-256 前 16 字符
  - `_slice_lines(...)`: 通用切片函数（按行范围或关键词），FakeArtifactStore 和 S3ArtifactStore 共享
  - `FakeArtifactStore(ArtifactStore)`: 内存 dict 实现

- `src/agent_harness/tools/inspect_artifact.py`
  - `InspectArtifactTool(Tool)`: 构造注入 `ArtifactStore`（不是 `Sandbox`）
  - `name="inspect_artifact"` / `side_effect=READ_ONLY` / `permission=READ_ONLY`
  - `reconcile_hint` 返回 `verifiable=True`
  - args: artifact_id(必填) / start_line / end_line / keyword / max_lines=200

- `tests/storage/test_artifact_store.py`: 14 个测试
- `tests/tools/test_inspect_artifact_tool.py`: 9 个测试

### 注意事项

- `inspect_artifact` 尚未注册到 `tools/__init__.py` 的 `__all__`——后续 ticket 集成时再加（Phase 5 不需要自动注册，Runtime 手动构造注入）。
- `_slice_lines` 是共享工具函数，#48 S3ArtifactStore 的 `inspect()` 直接复用它做切片，不要重复实现。

---

## 4. 各 Ticket 实施要点

### #46: estimate_tokens + ContextBuilder base

**核心交付**：
- `src/agent_harness/context/__init__.py` + `src/agent_harness/context/tokens.py`:
  `estimate_tokens(text: str) -> int` 用 `tiktoken.get_encoding("cl100k_base")`
- `src/agent_harness/context/builder.py`:
  `ContextBuilder` 类，`build(session: Session) -> list[AnyMessage]`
  - 构造参数：`model_provider` / `max_context_tokens=200_000` / `auto_compact_threshold=0.70` / `hard_guard_threshold=0.85` / `context_providers: list[ContextProvider] | None = None`
  - 内部调 `session.derive_messages()` 直接返回（不做 compaction，那是 #49 的活）
- `src/agent_harness/context/provider.py`:
  `ContextProvider(Protocol)` 定义 `select(session, token_budget) -> list[AnyMessage]`，不实现
- `config.py` Settings 增加：`max_context_tokens` / `auto_compact_threshold` / `hard_guard_threshold`
- `pyproject.toml` dependencies 加 `tiktoken>=0.7.0`

**测试 seam**：单元测试 `tests/context/test_tokens.py` + `tests/context/test_builder.py`
**注意**：`tiktoken` 第一次 import 会下载编码文件——确保测试环境能联网或缓存了编码。

### #47: Overflow Handler + Executor 集成

**已完成的接线约定（供 #50 使用）**：
- `ToolExecutor(..., overflow_handler=ArtifactOverflowHandler(store))`；`execute()` / `execute_batch()` 需要显式传 `session=session`。没有 handler 的原调用兼容；缺 Session 或与 OperationContext 不匹配时在副作用前拒绝。
- Runtime 的 Session 传递、事件流镜像和默认构造仍由 #50 集成，本 ticket 不修改 AgentRuntime。
- 兼容现有 BashTool 的 `stdout/stderr`。单个大字段存原文；多个大字段存可完整还原的 JSON 对象，共用一个 ref，各字段返回摘要。短字段、exit_code、错误语义与 metadata 保留。
- 默认阈值 2000 字符，摘要首尾各最多 10 行并限制字符数。阈值小于固定引用提示长度时仍保留提示，摘要可能超过这个极小阈值。
- 上传或事件持久化失败直接传播，不能进入 Tool retry；已有 Ledger 保留 RUNNING 供恢复对账，不伪造 artifact_ref。
- 复用决策：REUSE 已有 ArtifactStore / ToolResult / Ledger，BUILD 本项目后处理接线；参考 DeepSeek Harness 的 post-execute spill 设计（MIT，`docs/subsystems/spill.md`，master）。未移植代码；本项目存储失败中断，与上游保留内联大输出的 best-effort 策略不同。

**核心交付**：
- `src/agent_harness/tooling/overflow.py`:
  - `OverflowHandler(ABC)`: `async maybe_overflow(session, tool_call_id, tool_name, result) -> ToolResult`
  - `ArtifactOverflowHandler(OverflowHandler)`: 构造注入 `ArtifactStore` + `overflow_chars` 阈值
  - 溢出逻辑：提取 ToolResult 主输出字段（`data.get("output")` 或 `data.get("content")` 或 `message`），字符数超阈值 → `store.save()` → 截断替换 → `result.artifact_ref = artifact_id`
  - 截断格式：前 N 行 + `"... [truncated, {total} lines total, use inspect_artifact({artifact_id}) to view]"` + 后 N 行
- `src/agent_harness/tooling/executor.py`:
  - 构造函数增加 `overflow_handler: OverflowHandler | None = None`
  - 在 `tool.execute()` 成功返回后、`Ledger.update_state()` 之前调 `overflow_handler.maybe_overflow()`
- `src/agent_harness/session/event.py`:
  - EVENT_TYPES 增加 `ARTIFACT_CREATED = "artifact/created"`
  - data 格式：`{artifact_id, session_id, source_tool, tool_call_id, size, mime_type}`

**插入点精确位置**（读 `executor.py` 的 `execute` 方法）：
```
现有流程：
  1. 校验 → 2. 权限 → 3. Ledger PENDING → 4. Ledger RUNNING → 5. tool.execute → 6. Ledger SUCCEEDED → 7. return

加溢出后：
  5. tool.execute → 5.5. overflow_handler.maybe_overflow() → 6. Ledger SUCCEEDED → 7. return
```

**测试**：用 `FakeArtifactStore`（#45 已有），验证未超阈值原样返回 / 超阈值截断 + artifact_ref / artifact/created 事件被 append。

### #48: S3ArtifactStore (七牛云)

**核心交付**：
- `src/agent_harness/storage/s3_artifact.py`:
  `S3ArtifactStore(ArtifactStore)`，用 `aioboto3` 的 async S3 client
  - `save()`: `put_object` 到 bucket，key = `{session_id}/{artifact_id}`
  - `load()`: `get_object` 读回完整内容
  - `inspect()`: `get_object` 读回后调 `_slice_lines()` 切片（复用 #45 的共享函数）
- `config.py` Settings 增加：`artifact_store_endpoint` / `artifact_store_bucket` / `artifact_store_access_key` / `artifact_store_secret_key` / `artifact_store_region`
- `pyproject.toml` optional-dependencies 加 `[project.optional-dependencies] artifact = ["aioboto3>=13.0"]`
- 测试：mock aioboto3 client 验证调用参数；集成测试标 `@pytest.mark.qiniu` 默认 skip

**七牛云 S3 兼容端点配置**（用户提供，到 #50 才需要）：
- endpoint、bucket、access_key、secret_key 从 `.env` 读
- `aioboto3` 的 `Session().client("s3", endpoint_url=..., ...)` 对接

**注意**：`_slice_lines` 已在 `storage/artifact.py` 中定义，直接 import 复用。

### #49: Context Compactor 三层降级

**核心交付**：
- `src/agent_harness/context/compactor.py`:
  `ContextCompactor` 类，三层降级逻辑
  - 第一层：LLM 摘要——取早期 turns 送 `ModelProvider.ainvoke()`，prompt 要求结构化 summary（facts/decisions/constraints/failed_attempts/unresolved/artifact_refs/citations/tool outcomes），产出 `SystemMessage` 注入头部
  - 第二层：deterministic 机械提取——HumanMessage 截断 / AIMessage 只留 tool_calls / ToolMessage 只留 tool_call_id + 截断 content
  - 第三层：两层降级后 token 仍超 `hard_guard_threshold` → 抛 `ContextWindowExceededError`
  - AIMessage(tool_calls) + 紧跟 ToolMessage 块为不可分割原子单元
- `src/agent_harness/context/builder.py`:
  `ContextBuilder.build()` 集成 compaction（检测 token → 调 compactor → 返回安全 messages）
- `src/agent_harness/session/event.py`:
  EVENT_TYPES 增加 `CONTEXT_COMPACTED = "context/compacted"`
  data: `{compacted_turn_count, summary_message_count, token_estimate, fallback_used: bool}`

**依赖**：#46（estimate_tokens + ContextBuilder base）

### #50: AgentRuntime 集成 + 端到端测试

**核心交付**：
- `src/agent_harness/agent/runtime.py`:
  - 构造函数增加 `context_builder: ContextBuilder`
  - loop 第 1 步从 `session.derive_messages()` 改为 `context_builder.build(session)`
- 端到端测试（**用真实七牛云 S3ArtifactStore**，不用 Fake）
- Phase 5 Gate 达成证据

**用户提供的七牛云凭证**：到 #50 实施时需要向用户索取 endpoint / bucket / access_key / secret_key。

**注意**：现有 345+23=368 个测试不能回归。Runtime 改第 1 步后所有用 `session.derive_messages()` 的测试仍通过——因为 ContextBuilder.build() 内部调 derive_messages。

---

## 5. 测试命令

```bash
# 全量测试
cd D:/intelligence-agent-backend
.venv/Scripts/python.exe -X utf8 -m pytest tests/ -q --tb=no

# 只跑 Phase 5 相关
.venv/Scripts/python.exe -m pytest tests/storage/test_artifact_store.py tests/tools/test_inspect_artifact_tool.py tests/context/ tests/tooling/test_overflow*.py -q

# ruff
.venv/Scripts/python.exe -m ruff check .
```

**当前基线（#47 后）**：386 passed, 8 skipped, 3 deselected, ruff All checks passed。Windows 使用上面的 `-X utf8`，避免既有 mapping JSON 测试用默认编码读取中文路径导致失败。

---

## 6. 必须守的约束

1. **Scope Lock**（AGENTS.md §8）：不顺手重构、不提前做未来 Phase、不扩大架构
2. **Reuse First**（AGENTS.md §6）：`_slice_lines` 已在 artifact.py，S3ArtifactStore 直接复用
3. **21 条架构不变量**（AGENTS.md §7）：特别关注 #6（完整保存 ≠ 完整注入）、#4（Event ≠ Log）
4. **七牛云凭证不在代码里硬编码**——走 Settings / .env
5. **每完成一个 ticket**：commit + push + close issue + 更新 `docs/PHASE_STATUS.md`
6. **Phase 5 与 Phase 6 边界**：Phase 5 不做 Memory（MemoryCapability / LangMemProvider），ContextProvider Protocol 只定义不实现
7. **Python venv**：`D:/intelligence-agent-backend/.venv/Scripts/python.exe`
8. **worktree**：只在 `D:/intelligence-agent-backend`（feat/backend）操作，不碰 frontend

---

## 7. Phase 5 Gate（spec 06 §9）

实施完 #50 后验证：
- [ ] 大 Tool Output 完整保存（Artifact）
- [ ] Model 默认只收到 summary + ref（截断摘要 + artifact_ref）
- [ ] `inspect_artifact` 能找回细节
- [ ] 七牛云 Provider 可替换 Fake Provider
- [ ] Compaction 后历史不删除（SessionEvent 不变）
- [ ] 替换成 Fake Provider 时 Agent Core 无需修改（契约隔离）
