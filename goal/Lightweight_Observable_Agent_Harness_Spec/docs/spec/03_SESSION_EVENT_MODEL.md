# 03 — Session Event Model

## 1. 核心决定

Session MUST 采用 **append-only typed SessionEvent log** 作为 Agent 交互历史的主要事实源。

设计参考 DeepSeek Harness 的 event-sourced Session；Session Tree / Fork / Compaction 思路参考 Pi。

不要维护一套无法和 Event Log 对账的“隐藏 messages 真相”。

## 2. Event Envelope

建议最小字段：

```text
event_id
seq
time
type
session_id
run_id?
agent_id?
step_id?
data
source_event_ids?
```

要求：
- `seq` 在单 Session 内单调递增；
- event append 后不可原地修改；
- 修订使用新 Event 表达；
- payload 采用类型化 DTO。

## 3. 核心 Event Vocabulary

至少包含：

```text
session/started
session/resumed
session/forked

run/started
run/completed
run/failed

user/message

step/started
step/completed

model/started
model/delta        # 是否持久化逐 delta 可配置
model/completed
model/failed

tool/call
tool/result

context/built
context/compacted

checkpoint/saved
operation/reconcile-required
artifact/created

agent/delegated
agent/completed

approval/requested
approval/resolved
```

`ModelDelta` 可以实时 Event 流发送，但默认不要求每个 token 永久 JSONL，以避免日志爆炸。完整 AIMessage MUST 持久化。

## 4. Derive Messages

模型历史 SHOULD 由 SessionEvent 投影得到：

```text
events
→ derive_messages()
→ message history
→ ContextBuilder
```

要求：
- Tool Call 与 Tool Result 必须正确配对；
- Recovery ToolResult 使用原 `tool_call_id`；
- Compaction Summary 是一种新的 Context 投影，不删除原 Event；
- UI 也尽量消费同一 Event 流。

## 5. Resume

Resume：

```text
load session events
→ validate lineage / seq
→ restore persistence state
→ restore sandbox mapping
→ reconcile operations
→ repair message consistency
→ continue
```

Resume MUST NOT 默认重放所有 Tool。

## 6. Replay

Replay 目标：
- 从已持久化 Event 重新派生 UI / messages / trace；
- 允许“逻辑回放”；
- 默认冻结已发生 Tool Result，不重新产生外部副作用。

如果需要“重新执行式 replay”，必须显式进入不同模式，并要求权限/隔离。

## 7. Fork

Fork MUST 创建新的 Session lineage：

```text
parent session
→ select fork boundary
→ seed child with prefix / snapshot
→ child first live event
→ continue independently
```

要求：
- 父 Session 不改变；
- child 保存 parent identity + fork point；
- UI 能显示 lineage/tree；
- Artifact Ref 可以按权限复用；
- Sandbox fork 的物理策略可独立于 SessionEvent fork。

## 8. Compaction 与 Session

Compaction 只是 Runtime Context 的投影优化。

- Full SessionEvent History MUST 保留。
- Summary MUST 记录其 source range / source events。
- 不允许删除原 tool interaction 事实。
- Fork 到 compaction 之前的历史节点仍应可解释。

## 9. Durable Backend

第一版支持：
- JSONL：最透明、可 grep/tail；
- SQLite：本地索引/查询；
- PostgreSQL：生产型可选 Adapter。

JSONL 与数据库可以同时启用，但需要定义一个清晰的 commit 顺序与恢复策略，避免“双主事实源”。

推荐：SessionStore 抽象负责 append，具体 backend 决定 JSONL-only 或 DB+JSONL projection。

## 10. Acceptance Criteria

- 进程重启后可读取 Session；
- 可从 Event 重建消息链；
- dangling tool call 被检测；
- Resume 不重复已确认成功 Tool；
- Fork 后父子 Session 独立；
- Replay 不触发真实副作用；
- Compaction 后旧历史仍存在；
- UI 可根据事件重新构建主要视图。
