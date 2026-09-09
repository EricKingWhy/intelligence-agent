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

### 2.2 审批 WS 推送 (#136 后端部分)

当 ToolExecutor 触发 `needs_approval` 时，后端通过 WS 推送审批请求：

**WS 推送消息 `APPROVAL_REQUESTED`：**

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

**HTTP 回传端点 `POST /api/sessions/{id}/approvals/{call_id}`：**

已有端点：`src/agent_harness/web/app.py:1082` 的 `approve_tool_call()`。

```json
// Request body
{"decision": "allow"}   // 或 "reject"

// Response (200)
{"status": "decided", "decision": "allow"}
```

**语义约束：**
- fail-closed：超时默认拒绝
- one-shot：同一 callId 只能决策一次
- WS 断开时审批仍可通过 HTTP 完成
- 所有 tool 执行仍走 ToolExecutor 单一路径（不变量 #7）

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
| WS `/api/ws` | 已有 `handle_websocket()`，需扩展推送 `APPROVAL_REQUESTED` | 已有 WS 连接逻辑，需消费 `approval_requested` 消息 |
| `POST /api/sessions/{id}/approvals/{call_id}` | 已有 `approve_tool_call()` 端点 | ApprovalCard 组件已有，需接真实 WS 推送的 pending 事件 |
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
