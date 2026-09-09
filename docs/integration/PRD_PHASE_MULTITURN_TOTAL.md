# 总 PRD：Phase Multiturn T6-T9 + 前端审批/CSS

> **所有会话必读。** 本文件是前后端的唯一契约来源——跨端接口形状、WS 消息格式、SSE 事件结构只在这里定义一次。
> 前端看完知道后端会提供什么接口；后端看完知道前端会怎么消费。

---

## 1. 整体目标

完成 Phase Multiturn 的剩余切片：

| Ticket | 标题 | 类型 |
|---|---|---|
| #136 T6 | 审批 WS 推送 + HTTP 回传 | 跨端（后端 WS + 前端 modal） |
| #137 T7 | 模型切换 + Fork API/UI/CLI | 跨端（后端 API + 前端 UI + CLI） |
| #138 T8 | 崩溃恢复 + Ledger reconcile | 纯后端 |
| #139 T9 | Langfuse 多轮埋点 + DoD 全量回归 | 纯后端（验证+回归） |
| #37 | 交互式审批走通 | 纯前端（blocked by #136 后端 WS） |
| #35 | 主题亮色变量组双份手工同步 | 纯前端 CSS chore |

---

## 2. 跨端接口契约

### 2.1 WebSocket 通道 (`/api/ws`)

已有基础设施：`src/agent_harness/web/websocket.py` 的 `handle_websocket()`。

**客户端 → 服务端消息：**

```json
{"type": "subscribe", "session_id": "..."}
{"type": "ping"}
```

**服务端 → 客户端消息（增量事件推送）：**

所有 session 事件通过 WS 多路复用推送给已订阅的客户端。事件形状与 SSE 通道一致（`_event_to_sse_dict` / `_session_event_to_sse_dict`）。

### 2.2 审批事件推送 + HTTP 回传 (#136 后端部分)

> **勘误（as-built 契约，2026-09-09）**：本节旧稿的自定义 `{"type":"approval_requested"}`
> 帧与 `POST /api/sessions/{id}/approvals/{call_id}` 端点**均不存在**，会误导前后端对接，
> 现替换为实际契约。实现位置：`src/agent_harness/session/service.py`
> （`_InteractiveCallbackHolder`）、`src/agent_harness/web/app.py`（`approve_tool_call`）、
> `src/agent_harness/web/serialization.py`（信封构建）。
> 依据：SDD 03 §9 PermissionDecision / PermissionResolvedData；不变量 #7（Tool 只有一条执行路径）。

**推送：复用既有 session 事件流，不新增 WS 帧类型。**

ToolExecutor 触发 `needs_approval` 且 session 为交互式审批（`permission_mode=interactive`）时，
后端向该 session 的既有事件通道（SSE `GET /api/sessions/{id}/events` 与 WS `/api/ws`
subscribe 流，帧形状严格同形）追加一条 **durable SessionEvent**：

```json
{
  "type": "tool/approval-requested",
  "seq": 42,
  "run_id": "run-xxx",
  "step_id": "step-xxx",
  "session_id": "sess-xxx",
  "durability": "durable",
  "data": {
    "approval_id": "appr-xxx",
    "tool_name": "write_file",
    "tool_call_id": "call-xxx",
    "action_type": "workspace_write",
    "title": "write_file (workspace_write)",
    "description": "<审批原因>",
    "arguments_preview": {"path": "/foo/bar.py"},
    "permission": "workspace_write",
    "policy": "read_only",
    "reason": "<审批原因>",
    "allowed_decisions": ["deny", "approve_once"]
  }
}
```

**传输封装（SSE 与 WS 的外层不同，内层信封同形）：**

- SSE：`{"data": "<上述信封 JSON 字符串>"}`。
- WS：`{"type": "event", "session_id": "...", "event": {<上述信封>}}`；重连快照是
  `{"type": "snapshot", "session_id": "...", "events": [<信封>, ...]}`。

**客户端必须取 `event` 字段后再匹配内层 `type`**——WS 帧顶层 `type` 恒为
`event` / `snapshot`，不会是 `tool/approval-requested`。

决策落地后同通道追加 `permission/resolved`：

```json
{"type": "permission/resolved", "data": {"approval_id": "appr-xxx", "decision": "deny", "reason": "..."}}
```

**HTTP 回传端点：`POST /api/sessions/{session_id}/approve`。**

```json
// Request body
{"approval_id": "appr-xxx", "approved": true, "decision": "approve_once", "reason": ""}

// Response (200)
{"status": "resolved", "approval_id": "appr-xxx", "decision": "approve_once"}
```

- `approval_id` 必传；`decision` 是契约字段（`deny` / `approve_once`），`approved` 是兼容字段，
  两者同传时 `decision` 优先；只传 `approved` 时推导（`true`→`approve_once`，`false`→`deny`）。
- 状态码：`decision` 不在该请求的 `allowed_decisions` 内 → 422；
  已决策 → 409（one-shot）；`approval_id` 不存在 → 404；session 不存在 → 404。
- `approval_id` 缺省走旧 seam 兼容分支，返回 `200 {"status":"received"}`，不解析决策。

**语义约束：**
- fail-closed：交互式审批等待超过 `APPROVAL_TIMEOUT_SECONDS`（默认 300s，`≤0` = 无限等待）
  仍无决策 → 自动 `deny` 并写 `permission/resolved`；**永不默认放行**。
- one-shot：同一 `approval_id` 只能决策一次，第二次 409。
- WS 断开时审批仍可通过 HTTP 完成（决策不依赖 WS 连接）。
- 所有 tool 执行仍走 ToolExecutor 单一路径（不变量 #7）。
- 非交互式 session（默认 auto-approve / deny callback）不产生审批事件。

### 2.3 模型切换 API (#137 后端部分 A)

**端点：`POST /api/sessions/{id}/model`**

```json
// Request body
{"provider": "zhipu", "model_id": "glm-4.5"}

// Response (200)
{"status": "changed", "provider": "zhipu", "model_id": "glm-4.5"}
```

**新增 typed event `MODEL_CHANGED`：**

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

Runtime 在下一轮 run 时从 session 读取当前模型（而非创建时锁定的模型）。

### 2.4 Fork API (#137 后端部分 B)

**端点：`POST /api/sessions/{id}/forks`**

```json
// Request body
{"from_seq": 42}

// Response (200)
{"session_id": "sess-new-xxx", "from_seq": 42}
```

复用已有 `src/agent_harness/session/fork.py` 和 `lineage.py`，不重写。

### 2.5 崩溃恢复事件 (#138)

进程重启后扫描无终态 run 的 session，追加 `RUN_INTERRUPTED` 事件：

```json
{
  "type": "run_interrupted",
  "session_id": "sess-xxx",
  "data": {
    "interrupted_seq": 42,
    "reason": "process_restart"
  }
}
```

用户重新打开有 `RUN_INTERRUPTED` 的 session 时，前端显示"上次运行在第 N 步中断"，提供继续/重发/忽略三个动作。

### 2.6 CLI slash 命令 (#137 CLI 部分)

已有入口在 `src/agent_harness/cli.py`：
- `/model <provider> <model>` — 切换模型
- `/fork` — 从历史点 fork

---

## 3. 前后端分工表

| 接口/功能 | 后端负责 | 前端负责 |
|---|---|---|
| WS `/api/ws` | 已有 `handle_websocket()`，复用事件流推送 `tool/approval-requested` | 已有 WS 连接逻辑，需消费 `tool/approval-requested` 事件 |
| `POST /api/sessions/{id}/approve` | 已有 `approve_tool_call()` 端点（body 带 `approval_id`） | ApprovalCard 组件已有，需接真实事件推送的 pending 审批 |
| `POST /api/sessions/{id}/model` | 新增端点 + `MODEL_CHANGED` event | 前端从历史消息右键/菜单触发 fork（可选，后续迭代） |
| `POST /api/sessions/{id}/forks` | 新增端点，复用 fork.py | 同上 |
| 崩溃恢复 `RUN_INTERRUPTED` | 进程启动扫描 + 追加事件 + Ledger reconcile | 显示中断提示 + 继续/重发/忽略按钮 |
| Langfuse 多轮埋点 | metadata 加 `turn_index` + compaction 子 span | 无 |
| DoD 全量回归 | 跑通 PRD §10 全部 11 条 | 无 |
| 主题亮色变量组 | 无 | 接受现状保留注释 OR 构建期生成收敛 |

---

## 4. 依赖关系图

```
#131 T1 SessionService ✅ DONE
  ├─→ #132 T2 续聊端点+WS ✅ DONE
  │     ├─→ #136 T6 审批 WS 推送 ⬜ BACKEND
  │     │     └─→ #37 交互式审批走通 ⬜ FRONTEND (blocked by #136)
  │     ├─→ #138 T8 崩溃恢复 ⬜ BACKEND
  │     └─→ #139 T9 Langfuse+DoD ⬜ BACKEND
  │           (blocked by #132, #134, #138)
  └─→ #137 T7 模型切换+Fork ⬜ BACKEND+FRONTEND+CLI
        (blocked by #131 ✅)

#35 CSS 亮色变量组 ⬜ FRONTEND (无阻塞，可立即开始)
```

---

## 5. 并行执行计划

### 后端会话 (`feat/backend`) — 可立即开始

| 优先级 | Ticket | 说明 |
|---|---|---|
| P0 | #136 T6 审批 WS 推送 | 解锁前端 #37 |
| P1 | #138 T8 崩溃恢复 | 纯后端，解锁 #139 |
| P2 | #137 T7 模型切换+Fork | 后端 API 部分 |
| P3 | #139 T9 Langfuse+DoD | 最终验证，依赖 #138 |

### 前端会话 (`feat/frontend`) — 可立即开始

| 优先级 | Ticket | 说明 |
|---|---|---|
| P0 | #35 CSS 亮色变量组 | 无阻塞，chore |
| P1 | #37 交互式审批走通 | blocked by #136 后端 WS，等后端完成后开始 |
| P2 | #136 前端部分 | ApprovalCard 接真实 WS 推送，等后端 WS 推送就绪 |
| P3 | #137 前端部分 | fork/model UI，等后端 API 就绪 |

---

## 6. 集成规则

1. 后端在 `feat/backend` 开发，完成后由集成 AI 合并到 `main`。
2. 前端在 `feat/frontend` 开发，完成后由集成 AI 合并到 `main`。
3. 跨端接口变更必须先更新本文件（总 PRD），再通知对应会话。
4. 集成顺序：`feat/backend → main` 验证通过后 → `feat/frontend → main` 验证。
5. 默认先合并到本地 `main` 并验证，再 push GitHub。
