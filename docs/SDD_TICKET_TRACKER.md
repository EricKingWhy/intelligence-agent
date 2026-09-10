# SDD Ticket Tracker

> **持久化活文档** — 跨 context window 追踪 SDD 循环进度。
> 每次进入新 context window 时，先读本文件恢复状态。

---

## 当前状态

| 项 | 值 |
| --- | --- |
| Worktree | `D:\intelligence-agent-frontend` |
| Branch | `feat/frontend` |
| 协议版本 | `docs/SDD_WORKFLOW_PROTOCOL.md` v1 |
| 后端交接手册 | `D:\intelligence-agent-backend\docs\HANDOFF_FRONTEND_T7_T9.md` |

---

## Ticket 状态总览

| Ticket | 描述 | 状态 | Commit |
| --- | --- | --- | --- |
| FE-T7 | 会话级模型切换 + Fork UI | `pending` | — |
| FE-T8 | 崩溃恢复 UI — run/interrupted + 409 守卫 | `pending` | — |
| FE-T9 | 轮次标签（turn_index 显示） | `pending` | — |

### 全部完成后的步骤

| 步骤 | 状态 |
| --- | --- |
| /improve-codebase-architecture | `pending` |
| 写集成 AI 交接提示词 | `pending` |

---

## FE-T7: 会话级模型切换 + Fork UI

### 后端契约（来自 HANDOFF_FRONTEND_T7_T9.md §一.T7）

**新增端点：**

```
POST /api/sessions/{id}/model
Body: { "provider": "deepseek", "model_id": "deepseek-chat" }
→ 200 { "status": "changed", "provider": "deepseek", "model_id": "deepseek-chat" }
→ 404 session 不存在
→ 422 provider/model_id 不在 catalog
```

```
POST /api/sessions/{id}/forks
Body: { "from_seq": 5 }
→ 200 { "session_id": "child-uuid", "from_seq": 5 }
→ 404 session 不存在
→ 409 在途 run（历史未 settled）
→ 422 from_seq 不是合法 fork 锚点
```

**新增事件：**

```typescript
MODEL_CHANGED: 'model/changed',
// data: { from_provider, from_model_id, to_provider, to_model_id }
```

**关键行为：**
- 切换不打断在途 run——下一轮 run 从事件流派生当前模型生效
- `GET /api/models` 的默认条目（`is_default=true`）也是合法 POST target = 切回默认链
- 与默认条目同 provider + 同名的 catalog 条目会被遮蔽（选不中），`GET /api/models` 不列出该死选项
- 响应回传规范 model_id（service 解析出的 picker id），**不回显请求值**——否则上游 `model_name` / `"default"` 别名会与事件里的 `to_model_id` 及 `GET /api/models` 的 id 对不上

### 前端实现范围

1. **API 层**（`web/src/lib/api.ts`）：
   - 新增 `changeSessionModel(sessionId, provider, modelId)` → POST /api/sessions/{id}/model
   - 新增 `forkSession(sessionId, fromSeq)` → POST /api/sessions/{id}/forks

2. **事件类型**（`web/src/generated/event-types.ts`）：
   - 添加 `MODEL_CHANGED: 'model/changed'`

3. **投影层**（`web/src/lib/projection.ts`）：
   - 处理 `model/changed` 事件 → 更新 `conversation.model`

4. **UI 层**：
   - ModelPicker 选择后调 `POST /api/sessions/{id}/model`（而非仅本地状态更新）
   - 用响应里的 `model_id` 更新本地状态
   - 监听 SSE 事件流中的 `model/changed` 事件，更新 UI 显示当前模型
   - Fork 选择器：在历史用户消息上提供 fork 入口
   - 调 `POST /api/sessions/{id}/forks` body `{from_seq}`
   - 成功后跳转到 child session

### SDD 循环记录

#### 第 1 轮：/implement

- 状态：`done`
- 改了哪些文件：
  - `web/src/lib/api.ts` — 新增 `changeSessionModel()` 和 `forkSession()`
  - `web/src/lib/projection.ts` — MODEL_CHANGED → 更新 conversation.model；RUN_INTERRUPTED → finalizeRun
  - `web/src/hooks/useSession.ts` — useSession 暴露 changeModel 和 fork 操作
  - `web/src/App.tsx` — handleModelChange 在已有会话时触发 POST /model；handleFork 调用 forkSession 并跳转
  - `web/src/components/Conversation.tsx` — 用户消息上添加「分叉」按钮
  - `web/src/styles/app.css` — fork-btn 样式
- 门禁结果：tsc 0 / vitest 377 passed / oxlint 0 errors / playwright 46 passed

#### 第 1 轮：/code-review

- 状态：`pending`

#### 修复循环

（如有问题，记录每轮修复内容）

---

## FE-T8: 崩溃恢复 UI — run/interrupted + 409 守卫

### 后端契约（来自 HANDOFF_FRONTEND_T7_T9.md §一.T8）

**新增事件：**

```typescript
RUN_INTERRUPTED: 'run/interrupted',
// data: { interrupted_seq, reason }
// 信封字段: run_id, step_id
```

**崩溃扫描流程（web lifespan 启动时自动执行）：**

```
进程重启
→ 遍历所有 session
→ 检测无终态 run（run/started 之后没有 run/completed / run/failed / run/interrupted）
→ 补记 run/interrupted 事件
→ 强制跑 RecoveryCoordinator（Ledger-first reconcile）
→ UNKNOWN 工具调用无 ReconcileCallback 时安全拒绝
  （ReconcileRequired → ScanRecovery.NEEDS_MANUAL_RECONCILE）
→ 不伪造结果、不盲重跑（不变量 #14）
```

**续跑守卫（前端需要处理的 409）：**

当用户对一个有崩溃遗留（UNKNOWN 高风险 tool_call）的 session 发 `POST /api/sessions/{id}/messages`（继续/重发）时：

```json
// HTTP 409
{
  "detail": "存在需要人工裁决的 UNKNOWN Operation（bash(tool_call_id=call-1)）：..."
}
```

前端应：
1. 捕获 409，解析 detail 中的 `tool_name` 和 `tool_call_id`
2. 提示用户「此会话有未确认的高风险工具调用，需人工裁决」
3. 提供「调 `POST /api/sessions/{id}/recover` 重试」或「忽略」的选项
4. **不得**伪造「结果未知」继续——这是后端硬拒绝的

### 前端实现范围

1. **事件类型**（`web/src/generated/event-types.ts`）：
   - 添加 `RUN_INTERRUPTED: 'run/interrupted'`

2. **投影层**（`web/src/lib/projection.ts`）：
   - 处理 `run/interrupted` 事件 → 标记 run 为 interrupted 状态
   - 在 ConversationState 中添加 `run_interrupted` 字段

3. **UI 层**：
   - 打开有 `run/interrupted` 的 session 时，显示「上次运行在第 N 步中断」
   - 三个动作按钮：继续 / 重发 / 忽略
   - 409 人工裁决提示：当 `POST /api/sessions/{id}/messages` 返回 409 且 detail 含「UNKNOWN」时
   - 提示用户「此会话有未确认的高风险工具调用，需人工裁决」
   - 提供「调 `POST /api/sessions/{id}/recover` 重试」或「忽略」的选项

### SDD 循环记录

#### 第 1 轮：/implement

- 状态：`pending`
- 改了哪些文件：—
- 门禁结果：—

#### 第 1 轮：/code-review

- 状态：`pending`
- 发现的问题：—

---

## FE-T9: 轮次标签（turn_index 显示）

### 后端契约（来自 HANDOFF_FRONTEND_T7_T9.md §一.T9）

`RUN_STARTED` 事件 data 现在多一个字段：

```json
{
  "type": "run/started",
  "data": { "turn_index": 1 }
}
```

前端可以选择显示「第 N 轮」标签，也可以忽略这个字段（不影响现有行为）。

### 前端实现范围

1. **投影层**（`web/src/lib/projection.ts`）：
   - 在 `RUN_STARTED` 分支中提取 `data.turn_index`
   - 存入 ConversationState（新增 `turn_index` 字段）

2. **UI 层**：
   - 在 TurnView 中显示「第 N 轮」标签（如果 turn_index 存在）

### SDD 循环记录

#### 第 1 轮：/implement

- 状态：`pending`
- 改了哪些文件：—
- 门禁结果：—

#### 第 1 轮：/code-review

- 状态：`pending`
- 发现的问题：—

---

## 全部完成后的步骤

### /improve-codebase-architecture

- 状态：`pending`
- 扫描结果：—
- 修复记录：—

### 写集成 AI 交接提示词

- 状态：`pending`
- 内容：—
