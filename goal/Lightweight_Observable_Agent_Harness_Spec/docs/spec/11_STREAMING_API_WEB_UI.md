# 11 — Streaming / API / Web UI

## 1. Event ≠ Log

`AgentEvent` 是 Runtime 对外业务执行事件，可被 CLI/SSE/UI/Test/Trace 消费。

`Diagnostic Log` 是 Debug/运维内部记录。

Runtime 禁止直接把 `print()` 当事件机制。

## 2. AgentEvent

建议：

```text
AgentStarted
RunStarted
ModelStarted
ModelDelta
ModelCompleted
ToolStarted
ToolCompleted
ToolFailed
ApprovalRequested
CheckpointSaved
ContextCompacted
ArtifactCreated
AgentDelegated
RecoveryRequired
RunCompleted
AgentFailed
```

事件必须携带统一 ID。

## 3. CLI

CLI 是第一公民。

至少支持：
- 新建 Session；
- resume；
- replay；
- fork；
- 查看当前 session/run；
- 流式模型输出；
- Tool 调用状态；
- Approval；
- context budget；
- compact；
- 基础 artifact inspect；
- 预算 / 暂停展示（原因、触发维度、已消耗 vs 上限、continuation）与恢复（提交**绝对** ceiling + `expected_version`）；
- stuck 暂停恢复前置：必须提供相关 steer 或选择已检测到的环境 / policy 变更，不提供无依据的一键继续。

## 4. FastAPI SSE

最小接口可为：

```text
POST /chat/stream
→ create/run Agent
→ AgentEvent async iterator
→ SSE frames
```

SSE 只负责 Surface，不得拥有 Runtime 状态。

客户端断开：
- 检测 disconnect；
- 明确 Runtime task 是否 cancel 或继续；
- 清理 generator/queue；
- 不破坏 Session/Operation 一致性。

## 5. 轻量 Web Session Inspector

目标不是做 IDE，而是“看见 Agent 全链路”。

### 左侧
- Sessions
- Runs
- Fork Tree

### 中间
- Conversation
- Agent activity
- Tool calls

### 右侧 Step Detail
- model request/result metadata
- tool args/result
- retry
- operation state
- artifact
- context/compaction
- checkpoint
- recovery

### 操作
- Resume
- Replay
- Fork
- Approve / Reject
- inspect artifact

## 6. UI 状态来源

Web 前端 SHOULD 直接消费：

```text
SessionEvent history
+
live AgentEvent stream
```

UI 不得维护第二套不可对账业务真相。

### 6.1 预算 / 暂停的接口契约（REST · SSE/WS · CLI · Web）

**请求**：会话创建、显式恢复与 idle 会话消息启动接受可选 `budget` 对象：

```json
{
  "budget": {
    "expected_version": 3,
    "local": { "max_agent_turns": 500 },
    "run": {
      "max_agent_turns_total": null, "max_model_requests": null, "max_tool_calls": null,
      "max_total_tokens": null, "max_cost_usd": null, "deadline_at": null, "tool_call_limits": {}
    },
    "session": {
      "max_agent_turns_total": null, "max_model_requests": null, "max_tool_calls": null,
      "max_total_tokens": null, "max_cost_usd": null, "deadline_at": null,
      "tool_call_limits": {}, "max_delegations": 8
    }
  }
}
```

校验（冻结）：整数 ceiling 是正整数或 `null`（`null` = 该作用域无上限）；`max_cost_usd` 是非负十进制
或 `null`（wire 序列化 MUST NOT 要求二进制浮点相等）；`deadline_at` 是 RFC 3339 UTC 或 `null`；
`tool_call_limits` 映射**已注册**工具名到正整数绝对上限；首次创建省略 `expected_version`，更新已持久化的
暂停预算时必填；缺省 `budget` 用 AgentProfile 与 Deployment 默认；默认情况下 model requests / tool calls /
tokens / cost / deadline **只观测不限制**；SessionBudget 默认 `max_delegations=8`；`max_steps` 是迁移期
alias（相等接受、不等 422，见 `02 §5.1`）；活动 run MUST NOT 把 ceiling 降到已消耗之下；
恢复只接受**绝对** ceiling，不接受增量、不重置 counter。

**状态码**：
- **422**（无副作用）：形状非法、别名冲突、越权 ceiling、所选 Provider 链无法强制执行的显式 token/cost ceiling；
- **409**（无副作用）：版本过期、ceiling 低于已消耗、活动 run 冲突、stuck 暂停缺少所需变更依据、
  存在未 reconcile 的副作用。

被 422 / 409 拒绝的请求 MUST NOT 启动 model / tool / child 工作，也 MUST NOT 写消耗预算的事件。

**投影**（会话与活动 / 暂停的 run，按作用域）：不可变身份、当前 `version`、配置的绝对 ceiling、
已消耗 counter、有 ceiling 时的 remaining、token/cost 维度的**可执行性**、当前 deadline、暂停原因与
continuation 引用。Provider 账目缺失表示为 **unavailable**，MUST NOT 表示为 0。

**SSE / WebSocket**：暂停 / 恢复事件走**既有**信封与顺序保证；重连 / 回放从 append-only 存储取回同样事件，
MUST NOT 合成第二套客户端状态；暂停后当前直播流在暂停事件持久化后**干净收束**，恢复时以**同一 `run_id`**
接回正常流；重复帧按既有 seq 幂等处理。

**CLI 与 Web**：状态**只**来自 SessionEvent / 服务端投影——进程本地内存与客户端本地暂停态都不是权威。
刷新与重连后 MUST 重建出同样的暂停原因、continuation、已消耗 / 上限与版本；stuck 暂停 MUST NOT 提供
无依据的一键重试；409 冲突后 MUST 刷新权威状态并**保留用户未发送的输入**。
`paused` / `completed` / `failed` / `NEED_RECONCILE` 四类 MUST 可区分。

## 7. Transport

V1 可以：
- SSE：Agent server → UI 实时事件；
- REST：Session/Run/Artifact 查询。

不要求 WebSocket，除非双向 steering/approval 证明有必要。

## 8. Backpressure / Queue

如果内部使用 `asyncio.Queue`：
- queue 有明确生命周期；
- Agent completion/error 必须发送终止信号；
- disconnect 时不得永久泄露 producer task；
- durable event 不依赖 Queue 存活；
- Queue 只是实时传输，不是持久化。

## 9. Acceptance Criteria

- CLI 可实时看到 ModelDelta；
- ToolStarted/Completed 可实时显示；
- SSE disconnect 无 task/queue 泄漏；
- UI 可刷新后从 SessionEvent 重建；
- Fork Tree 可显示；
- Approval 可以闭环；
- Langfuse/前端挂掉不影响 durable Session；
- 非法形状 / 别名冲突 / 无法强制的显式 token-cost ceiling 返回 422 且**不开工**；版本过期、ceiling 低于已消耗、
  缺变更依据、未 reconcile 返回 409 且**不开工**；
- 投影字段齐全（身份 / version / ceiling / consumed / remaining / 可执行性 / deadline / 暂停原因与 continuation），
  账目缺失显示为 unavailable 而非 0；
- SSE/WS 重连与 replay 按 seq 顺序取回暂停 / 恢复事件，不产生重复的投影跃迁；
- CLI 与 Web 在刷新 / 重连后显示一致的暂停原因、已消耗 / 上限、版本与 continuation，且都不是本地权威；
- `paused` / `completed` / `failed` / `NEED_RECONCILE` 在 Web 与 CLI 上均可区分。
