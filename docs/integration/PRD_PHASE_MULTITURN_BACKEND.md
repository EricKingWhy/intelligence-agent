# 后端 PRD：Phase Multiturn T6-T9

> **后端会话专用。** 必须先读 `PRD_PHASE_MULTITURN_TOTAL.md`（跨端接口契约的唯一来源）。
> 本文件只定义后端要实现的 API 端点详细定义 + WS 推送协议 + 后端独立任务 + 测试接缝。

---

## 1. 任务清单

| 优先级 | Ticket | 标题 | 阻塞 |
|---|---|---|---|
| P0 | #136 | T6 审批 WS 推送 + HTTP 回传 | 解锁前端 #37 |
| P1 | #138 | T8 崩溃恢复 + Ledger reconcile | 解锁 #139 |
| P2 | #137 | T7 模型切换 + Fork API | 后端 API 部分 |
| P3 | #139 | T9 Langfuse 多轮埋点 + DoD 全量回归 | 依赖 #138 |

---

## 2. Ticket #136: T6 审批 WS 推送 + HTTP 回传

### 2.1 现有基础设施

- **WS 入口**：`src/agent_harness/web/websocket.py` 的 `handle_websocket()`
  - 已有 `subscribe` / `ping` / `send_message` / `cancel` 消息处理
  - 已有多路复用 session 事件流推送
- **审批端点**：`src/agent_harness/web/app.py:1082` 的 `approve_tool_call()`
  - 已有 `POST /api/sessions/{id}/approvals/{call_id}` 端点
  - 已有 `ApproveRequest` Pydantic model（`decision` 字段）
- **ApprovalCallback**：已有 callback 机制（默认 auto-approve）

### 2.2 要做什么

**A. WS 推送 `APPROVAL_REQUESTED` 事件**

当 ToolExecutor 触发 `needs_approval` 时，通过 WS 推送审批请求给已订阅该 session 的客户端：

```json
{
  "type": "approval_requested",
  "session_id": "sess-xxx",
  "call_id": "call-xxx",
  "tool_name": "write_file",
  "args_summary": "path=/foo/bar.py, content=...",
  "scope": "workspace-write",
  "allowed_decisions": ["allow", "reject"]
}
```

实现路径：
1. 在 `websocket.py` 的 `handle_websocket()` 中新增 `approval_requested` 消息类型处理
2. 或在现有 subscribe 流中注入审批推送

**B. HTTP 回传端点（已有，需验证）**

已有端点 `POST /api/sessions/{id}/approvals/{call_id}`：
- body: `{"decision": "allow"}` 或 `{"decision": "reject"}`
- 走 HTTP 保证 WS 抖动时也能完成审批
- 统一汇聚到现有 ToolExecutor approval callback（不变量 #7）

**C. 审批语义**

- fail-closed：超时默认拒绝
- one-shot：同一 callId 只能决策一次
- WS 断开时审批仍可通过 HTTP 完成

### 2.3 验收标准

- [ ] WS 推送 `APPROVAL_REQUESTED` 带完整审批上下文
- [ ] HTTP 回传端点正确更新 PendingApprovalQueue 并唤醒 callback
- [ ] 审批 fail-closed（超时默认拒绝）
- [ ] 审批 one-shot（同一 callId 只能决策一次）
- [ ] WS 断开时审批仍可通过 HTTP 完成
- [ ] 所有 tool 执行仍走 ToolExecutor 单一路径（不变量 #7）
- [ ] ruff clean + 测试 green

### 2.4 测试接缝

- **WS 推送测试**：复用 `tests/web/test_web_multiturn.py` 的 TestClient + WS 连接模式
- **HTTP 回传测试**：复用 `tests/web/test_web_phase5_approval.py` 的审批端点测试
- **fail-closed 测试**：构造超时场景，验证默认拒绝
- **one-shot 测试**：同一 callId 重复决策，第二次返回 409

---

## 3. Ticket #138: T8 崩溃恢复 + Ledger reconcile

### 3.1 要做什么

**A. 进程启动扫描**

进程启动时扫描 `SessionRegistry`（或 JSONL store）中所有 session：
1. 发现没有终态（`RUN_COMPLETED` / `RUN_FAILED`）的 run
2. 追加 `RUN_INTERRUPTED` 事件（含 `interrupted_seq`, `reason="process_restart"`）

**B. Ledger reconcile**

对这些 session 强制跑 Operation Ledger reconcile：
1. 正确分类工具状态（COMPLETED / UNKNOWN / FAILED）
2. UNKNOWN 状态的工具调用标记需人工确认（不盲重跑，不变量 #14）

**C. 用户重新打开 session**

用户重新打开有 `RUN_INTERRUPTED` 的 session 时看到中断点，提供继续/重发/忽略。

### 3.2 验收标准

- [ ] run 进行中 kill 进程，重启后 session 标记 `RUN_INTERRUPTED`
- [ ] Ledger reconcile 正确分类工具状态（COMPLETED / UNKNOWN / FAILED）
- [ ] UNKNOWN 工具不盲重跑，标记需人工确认
- [ ] 用户重新打开 session 时能看到中断点
- [ ] 提供"继续"/"从中断点重发"/"忽略"三个动作
- [ ] 符合不变量 #12（Checkpoint ≠ 副作用恢复）、#13（Ledger reconcile）、#14（不盲重跑 UNKNOWN）
- [ ] ruff clean + 测试 green（含 kill test）

### 3.3 测试接缝

- **Kill test**：启动 server → 发起 run → kill 进程 → 重启 → 验证 `RUN_INTERRUPTED` 事件
- **Ledger reconcile test**：构造 UNKNOWN 状态的工具调用，验证不盲重跑
- Prior art：`tests/web/test_web_stream.py` 的 `_start_server` + ScriptedModel 模式

---

## 4. Ticket #137: T7 模型切换 + Fork API

### 4.1 A. 模型切换（同 session 内）

**要做什么：**
1. 新增 `MODEL_CHANGED` typed event（含 from/to provider+model）
2. 新增 `POST /api/sessions/{id}/model` 端点（body: `{provider, model_id}`）
3. Runtime 在下一轮 run 时从 session 读取当前模型（而非创建时锁定的模型）
4. CLI `/model <provider> <model>` 已在 T3 提供入口

**API 契约：**

```json
// POST /api/sessions/{id}/model
// Request
{"provider": "zhipu", "model_id": "glm-4.5"}
// Response (200)
{"status": "changed", "provider": "zhipu", "model_id": "glm-4.5"}
// Response (422) — unknown provider/model
{"detail": "unknown model 'glm-4.5' for provider 'zhipu'"}
```

**事件契约：**

```json
{
  "type": "model_changed",
  "session_id": "sess-xxx",
  "data": {
    "from_provider": "openai",
    "from_model_id": "gpt-4o",
    "to_provider": "zhipu",
    "to_model_id": "glm-4.5"
  }
}
```

### 4.2 B. Fork（从历史点派生新 session）

**要做什么：**
1. 新增 `POST /api/sessions/{id}/forks` 端点（body: `{from_seq}`），返回新 session_id
2. 复用已有 `src/agent_harness/session/fork.py` / `lineage.py`，不重写
3. CLI `/fork` 已在 T3 提供入口
4. 前端从历史消息右键/菜单触发 fork（可选，后续迭代）

**API 契约：**

```json
// POST /api/sessions/{id}/forks
// Request
{"from_seq": 42}
// Response (200)
{"session_id": "sess-new-xxx", "from_seq": 42}
// Response (404) — session not found
{"detail": "session not found"}
```

### 4.3 验收标准

- [ ] 同一 session 中途换模型，`MODEL_CHANGED` 写入 JSONL
- [ ] 下一轮 run 使用新模型，上下文不丢
- [ ] `POST /api/sessions/{id}/model` 工作 + 校验 provider 可用性
- [ ] `POST /api/sessions/{id}/forks` 从指定 seq 派生新 session
- [ ] fork 出的新 session 独立，原 session 不受影响
- [ ] CLI `/model` 和 `/fork` 工作
- [ ] 复用 fork.py/lineage.py，不重写
- [ ] ruff clean + 测试 green

### 4.4 测试接缝

- **模型切换测试**：创建 session → POST /model → 验证 MODEL_CHANGED 事件 + 下一轮用新模型
- **Fork 测试**：创建 session → 跑几轮 → POST /forks → 验证新 session 独立
- Prior art：`tests/web/test_web_multiturn.py` + `tests/session/test_fork.py`

---

## 5. Ticket #139: T9 Langfuse 多轮埋点 + DoD 全量回归

### 5.1 A. Langfuse 多轮埋点

**要做什么：**
1. 每轮 run 仍是独立 root span `agent-run`
2. metadata 新增 `turn_index`（第几轮）和已有 `session_id`
3. Langfuse UI 按 session_id 过滤可见完整多轮链路
4. 压缩本身作为子 span `compaction` 埋点

### 5.2 B. 长期记忆多轮验证（LangMem 已实现，本期验证而非重建）

**要做什么：**
1. 验证 session 绑定 MemoryNamespace（已有 `ca6fe88` commit）在多轮续聊后 namespace 内有足够内容
2. 验证 MemoryWriteback 从 outbox 异步提取记忆条目
3. 验证后续 session 中 MemoryContextProvider 召回注入（或 graceful degrade 验证）
4. 验证 Milvus/embedding 不可用时 runtime 不崩（#21）

### 5.3 C. DoD 全量回归（PRD §10 十一条）

1. 多轮续聊：同一 session 连续 10 条消息，第 10 条能引用第 1 条
2. 压缩 bracket：构造超长对话触发压缩，验证 4 事件 bracket + shadow + replay 一致
3. 大产物外置：工具返回 >2k token 验证 MinIO + 摘要+引用 + read_artifact
4. steer/queue：run 进行中发消息验证默认排队 + 立即 steer + 队列可取消
5. CLI/Web 一致性：CLI 新建 session 在 Web 可见、反之亦然
6. 崩溃恢复：kill + 重启 + INTERRUPTED + 不盲重跑
7. 模型切换：MODEL_CHANGED + 新模型 + 上下文不丢
8. fork：从历史点 fork 独立
9. Langfuse 多轮 trace 按 session_id 串联
10. 长期记忆生效（或 graceful degrade）
11. 热会话续聊 P95 < 200ms（不含模型调用）

### 5.4 验收标准

- [ ] Langfuse 多轮 trace 按 session_id 串联可见 + turn_index
- [ ] 压缩子 span `compaction` 可见
- [ ] LangMem 异步提取在多轮对话后产生记忆条目
- [ ] MemoryContextProvider 召回注入（或 graceful degrade 验证通过）
- [ ] Milvus 不可用时 runtime 不崩
- [ ] PRD §10 全部 11 条 DoD 验证通过
- [ ] 全量 regression green

### 5.5 测试接缝

- **Langfuse trace 验证**：检查 Langfuse UI 中 root span metadata 含 `session_id` + `turn_index`
- **Compaction 子 span**：触发压缩后检查 Langfuse 中 `compaction` 子 span 存在
- **DoD 回归**：逐条构造场景验证
- Prior art：`tests/web/test_web_stream.py` + `tests/scripted_model.py`

---

## 6. 执行顺序

```
#136 T6 审批 WS 推送     ← P0，解锁前端 #37
  ↓
#138 T8 崩溃恢复          ← P1，纯后端，解锁 #139
  ↓
#137 T7 模型切换+Fork API ← P2，后端 API 部分
  ↓
#139 T9 Langfuse+DoD     ← P3，最终验证，依赖 #138
```

每个 ticket 按 to-tickets → /implement → code-review 流程执行。
完成后通知集成 AI 合并到 `main`。
