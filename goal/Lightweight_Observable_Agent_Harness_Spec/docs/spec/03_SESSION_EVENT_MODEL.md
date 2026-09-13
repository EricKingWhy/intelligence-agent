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

operation/reconcile-required
artifact/created

agent/delegated            # 历史名 → 实装 agent/delegation-started（见 §3.2）
agent/completed            # 历史名 → 实装 agent/delegation-finished（见 §3.2）

approval/requested         # 历史名 → 实装 tool/approval-requested（见 §3.2）
approval/resolved          # 历史名 → 实装 permission/resolved（见 §3.2）
```

`ModelDelta` 可以实时 Event 流发送，但默认不要求每个 token 永久 JSONL，以避免日志爆炸。完整 AIMessage MUST 持久化。

## 3.2 历史名 → 实装名映射（**旧名不是契约**）

本表是为了让按早期 spec 名字检索的人能找到落点。**契约只有一个方向：实装名。**
旧名是历史/概念名，MUST NOT 被当成有效事件名去实现或匹配（改的是文档，不改事件名——
见母票 #173 §4 非目标）。

| spec 历史名 | 实装名（唯一契约） | 证据 |
| --- | --- | --- |
| `agent/delegated` | `agent/delegation-started` | `src/agent_harness/session/event.py`（`AGENT_DELEGATION_STARTED`） |
| `agent/completed` | `agent/delegation-finished` | `src/agent_harness/session/event.py`（`AGENT_DELEGATION_FINISHED`） |
| `approval/requested` | `tool/approval-requested` | `src/agent_harness/session/event.py`（`TOOL_APPROVAL_REQUESTED`）；`web/app.py` 的广播注释亦写作 `tool/approval-requested`（durable） |
| `approval/resolved` | `permission/resolved` | `src/agent_harness/session/event.py`（`PERMISSION_RESOLVED` + `PermissionResolvedData`，见本文 §9） |

判据：事件名的唯一事实源是 `src/agent_harness/session/event.py`；前端词汇表由
`scripts/gen_event_types.py` 从它生成（`web/src/generated/event-types.ts`），有守卫测试
`tests/test_event_types_generated.py`。

## 3.1 与 SessionEvent **分层**的存储层概念（不是事件）

下面这些概念属于**存储层**，MUST NOT 作为 SessionEvent 写入事件流 / JSONL，
也 MUST NOT 出现在上面的事件表里。分层的理由是同一条：事件流记的是「**对话里发生过什么**」
（`derive_messages` 要读的事实），而这些是「**存储层怎么恢复**」的实现辅助——混进事件流
会让 replay 语义被存储细节污染（ADR-0004 Round 5 Q16）。

| 概念 | 存放位置 | 为什么不是 SessionEvent |
| --- | --- | --- |
| **Checkpoint**（`checkpoint/saved`） | 存储层（见 `07_STORAGE_PERSISTENCE_RECOVERY.md`） | checkpoint 是恢复辅助状态，不是对话事实；`derive_messages` 不需要它。**`checkpoint/saved` 永远不进 SessionEvent**（ADR-0004 Round 5 Q16；实现侧硬约束见 `agent/runtime.py` 与 `storage/checkpoint.py` 顶部注释） |
| **Operation Ledger** | SQLite `operations` 表（`operation_id = tool_call_id`，ADR-0004 Round 5 Q17） | Ledger 记的是**副作用账本**（配合 reconcile 与恢复），不是对话内容。它不参与 `derive_messages`，只被 `RecoveryCoordinator` 读。（`operation/reconcile-required` 是**事件**——那一条是「模型该知道上次有个 operation 进了 NEED_RECONCILE」的对话事实，由 RecoveryCoordinator append。） |
| **Artifact 大内容** | Local / MinIO / S3 兼容存储（spec 06） | 事件里只留 **ref**（`artifact/created` / `artifact/externalized` 携带 `artifact_id` + `size` + `mime_type`），模型只拿 summary + ref，正文不进事件流也不进上下文 |

判据一句话：**会不会改变"模型该看到什么"**。会 → 事件；只是"重启后怎么接着跑"的辅助 →
存储层，不进事件流。

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
