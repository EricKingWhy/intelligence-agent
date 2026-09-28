# ADR-0030 — 在途 run 的输入通道：queue 排队 / steer 引导 / query 取代（supersede）

- **Status**: Proposed（契约已冻结，待实现）
- **Date**: 2026-09-13
- **Deciders**: 用户（产品语义逐条裁定）+ 本 Agent（机制设计）
- **Related**:
  - `docs/PRD_ENTERPRISE_MULTI_TURN_SESSION.md` §5.3 / §6 / 决策 D-7 / AC4（本 ADR 是它的**落地实现设计**，不是新需求）
  - ADR-0016（流式块记账 / detached run / 取消臂语义）
  - ADR-0023（prompt 注册表：新增注入文案走 `DEFAULT_REGISTRY`）
  - ADR-0027（session cwd）、ADR-0029（session 硬删除）——本 ADR 不改变它们
  - Issues：#194（Esc 提示重叠）、#195（用户消息动作行 + 编辑）、#196（queue/steer 死路）
  - 前端登记：`docs/FRONTEND_ISSUES_LOG.md`（前端 worktree）

> **本 ADR 的读者**：接下来实现 #195 / #196 的人（可能是另一个模型/agent）。
> **阅读顺序**：先读 §2 术语表（全文唯一口径）→ §3 决策 → §4 实现契约 → §8 测试要求。
> 凡是实现上有两种做法的地方本 ADR 都**指定了一种**并给出理由，不留"自行发挥"。

---

## 1. Context

### 1.1 已冻结的需求（不是本 ADR 提出来的）

| 事实 | 证据 |
| --- | --- |
| run 进行中用户继续输入：默认进队列，可自选 steer | `docs/PRD_ENTERPRISE_MULTI_TURN_SESSION.md:285`（AC4）、决策 D-7、Q2/Q13 决策表 |
| 四种 durable 事件词汇已定义 | `session/event.py:88-91`：`message/queued` / `queue/cancelled` / `steer/requested` / `steer/applied` |
| HTTP 契约已暴露 mode | `web/app.py:276-294` `SendMessageRequest.mode: "queue" \| "steer"`（docstring 引用 D-7），路由 `:1497-1561` |
| 服务层已实现"注册 + durable 记录" | `session/service.py:680-757`（`send_message`）、`:779-800`（`drain_queued_message`）、`:785`（`cancel_queue`） |
| 内存队列管理器已实现 | `session/queue.py`（`enqueue` / `drain_next` / `cancel` / `register_steer` / `drain_steers`） |

### 1.2 缺口：**契约在、消费不在**（本 ADR 要解决的唯一问题）

| 事实 | 证据 |
| --- | --- |
| 队列**永不被消费** | `SessionService.drain_queued_message`（`service.py:779-800`）**零生产调用者**；`MessageQueueManager` 自己的文档承诺"run 结束后 SessionService 调 `drain_next()`"（`queue.py:52`）从未接线 |
| steer **永不注入** | `MessageQueueManager.drain_steers`（`queue.py:134`）**零生产调用者**；`agent/runtime.py` 全文 **零** 处 steer/queue 引用；`steer/applied` 从未被写入（`queue.py:11` 的注释是空头承诺） |
| 前端**无法表达** | `Composer.tsx:76` `const locked = streaming \|\| approvalPending;`，`:112-123` textarea `disabled={locked}`——流式期间输入框禁用 |
| 用户可见后果 | 回答期间发出的消息被 ack 成 `queued/steered`、落一条 durable 事件，然后**永久静默**：界面不显示排队，run 结束后也不接力 |

### 1.3 物理与 API 边界（决定"能做到什么"，实现者不得越过）

1. **没有任何厂商支持改写已经在途的请求**（OpenAI / Anthropic / DeepSeek / Google 的 chat API 都不接受"替换上一轮 user 消息"）。一旦 messages 数组发出，那次调用的输入就固定。
2. **已经流出的 token 收不回来**。所以"边答边改"不可能表现为"同一段回答换了内容"。
3. 因此"让模型看到新输入"只有两条真实路径：
   - **(a) 同一 run 内的下一个边界**（下一次模型调用前）→ 本 ADR 称为 **steer**；
   - **(b) 下一个 run** → 本 ADR 称为 **queue**。
4. "把已经发出去的问句换成新问句"只能通过**改变派生（projection）**实现，不能通过改写历史事件实现（不变量 #3：append-only）。

> 实现者若想"直接改 JSONL 里那条 user/message 的内容"，立即停止：那会破坏 append-only、seq 对账与 resume。本 ADR §4.2 给出了唯一正确做法。

---

## 2. 术语表（全文唯一口径，前端文案也照此）

| 词 | 定义 | 投递边界 | 用户可见 |
| --- | --- | --- | --- |
| **queue（排队）** | `mode="queue"`，**默认**。消息进队列，等当前 run 结束后接力成下一个 run | 当前 run 的**终态之后** | 输入区下方出现队列条「待发送 N」；run 结束自动发出 |
| **steer（引导）** | `mode="steer"`，用户显式点「立即」。不排队，注入当前 run | 当前 run 的**下一个模型调用前**（= 循环头，§4.3） | 消息立刻进入会话流（带「引导」小标）；run 不被打断；**已输出内容不回滚** |
| **supersede（取代）** | 对**尚未定稿**的输入做编辑：新文本取代旧文本在投影中的位置 | 跟随其载体（queue 或 steer）的边界 | 界面上只出现新文本；被取代那一轮**整段消失**（§4.5） |
| **pause（暂停）** | 用户点停止按钮 → 在途 run 立即收口 | 立即 | **本 ADR 不新增**，是既有行为：终态为 `run/failed` + `data.reason ∈ {"cancelled","orphaned"}`（`runtime.py:227-249`），或崩溃恢复写的 `run/interrupted` |
| **被中断的轮** | 一个 run 没有正常完成：`run/failed`（reason=cancelled/orphaned）或 `run/interrupted` | — | 该轮回答标记为「已中断」（保留显示，见 §4.5.3） |

**"A+B 都要回答"的含义（用户原话裁定）**：用户在 A 的回答过程中又发了一条新输入 B，B **很可能是对 A 的补充**，所以模型必须同时看到 A 与 B（`query = A + B`），而不是"用 B 换掉 A"。这与 supersede 是**两件不同的事**：

- 发 **B**（不编辑 A）⇒ A 保留、B 追加 ⇒ 模型看到 A+B（steer 同理：A 在链上、B 追加在其后）。
- **编辑 A 成 B**（supersede）⇒ A 被取代 ⇒ 模型只看到 B，A 那一轮（问 + 答）整段从投影与界面消失。

判据只有一条：**用户有没有对那条已发出的问句执行编辑动作**。

---

## 3. 决策

| # | 决策 | 一句话 |
| --- | --- | --- |
| D1 | 两个通道、一套事实 | queue 与 steer 共用 `MessageQueueManager` 与同一组事件词汇；差别只在投递边界，不在存储 |
| D2 | steer 投递点 = run 循环头、`ContextBuilder.build()` 之前 | `runtime.py:592`；那是模型可见投影的唯一入口（`context/builder.py:73-75`） |
| D3 | append-only 不变，变的是派生 | 新增 `message/superseded` 事件；`derive_messages` 用与 compaction 相同的 shadow 机制跳过被取代区间 |
| D4 | queue 的驱动收敛到**一个**调用点 | run 终态后 `SessionService.on_run_terminal()`；Web / CLI 不各自实现 |
| D5 | 队列必须跨崩溃/重启存活 | durable 事实 = 事件流；内存队列只是缓存，启动时按事件流重建（新增 `queue/consumed` 事件补全"已消费"判据） |
| D6 | steer 消息**不得**复用 `injected_by` | 那是用户自己的话；标了会被记忆抽取静默丢弃（`memory/extractor.py:131-141`） |
| D7 | 未投递输入按**到达顺序**在下一次机会投递 | run 结束时既看 `message/queued` 也看未 apply 的 `steer/requested`，FIFO |
| D8 | 只有**最新一条** user 消息可被 supersede | 防止误删长历史；非最新 → 409 |
| D9 | 被中断轮的补齐指令 | 上一轮被中断且**未被取代**时，注入一次「先补齐被中断的问题」文案（ADR-0023 注册表） |
| D10 | 前端流式期间输入框**可用** | 解除 `disabled`，这是 #195/#196 的前置（用户明确要求"无论如何都要做"） |
| D11 | 队列/steer 状态必须**可见**且可操作 | 队列条 + 「立即」+「取消」+「编辑」；`steer` 落地后消息带来源标记 |
| D12 | 编辑已完成的问答轮也合法 | 与流式期间编辑同一机制（shadow 区间 + 新 run 回答），不额外开分支 |

---

## 4. 实现契约（逐条可直接编码）

### 4.1 事件（durable）

**新增 2 个类型**（必须同时加入 `session/event.py` 的常量与 `EVENT_TYPES` 白名单，否则 `Session.append` 拒绝）：

| 类型 | 写入者 | data 字段 | 含义 |
| --- | --- | --- | --- |
| `queue/consumed` | D4 驱动点 | `{queue_id: str, run_id: str}` | 某排队项已被消费成一次 run（重启重建时的"已消费"判据） |
| `message/superseded` | `SessionService`（编辑请求处理内，§4.4） | `{superseded_seq: int, carrier: "queue" \| "steer"}` | seq 为 `superseded_seq` 的 `user/message` 及其整轮被取代 |

**复用、不改 schema 的类型**：`message/queued`（已有 `queue_id`/`content`）、`queue/cancelled`（已有 `queue_id`）、`steer/requested`（已有 `steer_id`/`content`/`run_id`）、`steer/applied`。

`steer/applied` 的 data 统一为 `{steer_id: str, applied_seq: int | null, run_id: str | null}`，两个写入点：

| 写入点 | 场景 | 字段取值 |
| --- | --- | --- |
| runtime 循环头（D2） | steer 在 run 内被注入 | `applied_seq` = 新追加的 `user/message` 的 seq；`run_id` = 当前 run |
| D4 驱动点 | steer 在 run 结束前未被注入，被降级成一个新 run | `applied_seq = null`（它就是新 run 的首条 user 消息）；`run_id` = 新 run |

### 4.2 投影（`session/derive.py`）

1. **supersede 区间**：令被取代 seq 为 `s`。区间 = `[s, n)`，其中 `n` = `s` 之后**第一条未被取代的** `USER_MESSAGE` 的 seq；若不存在这样的消息则 `n = +∞`（一直到底）。
   - 投影跳过区间内**所有**事件（user / model / tool / delta 全部），即"被取代那一整轮问与答都不进模型可见上下文"。
   - 该区间与既有 `is_shadowed(seq)` 机制同构（`derive.py:99-113`）：实现上把 supersede 区间并入 shadowed 集合即可，**不要**新写一套跳过逻辑。
2. **被取代消息的正文不再出现**：取代它的新消息本身是一条普通 `user/message`（seq 在 `s` 之后），自然进入投影——所以模型看到的是 B 而不是 A（用户裁定："模型可见上下文里，被替代的旧 query 就是变成新 query 了"）。
3. **不变量**：`derive_messages` 仍是纯函数；投影结果与事件到达顺序无关（区间由 seq 决定）；若同一 seq 被取代两次，以**最早**的一条 `message/superseded` 为准（幂等）。
4. **dangling 注入**（`derive.py:161+`）必须在 shadow 之后执行：被取代轮里的 tool_call/tool_result 一律不参与 dangling 计算，不得为它们合成 ToolMessage。

### 4.3 steer 注入点（`agent/runtime.py`）

```
run 循环头（runtime.py:592，`while True:` 之后、ContextBuilder.build() 之前）：
  1. steers = await steer_source.drain_steers(session_id, run_id)   # 仅当注入了 port
  2. for s in steers:
         ev = session.append(USER_MESSAGE, {"content": s.content, "steer_id": s.steer_id},
                             run_id=run_id, step_id=step_base + steps)
         yield to_agent_event(ev)
         applied = session.append(STEER_APPLIED,
                                  {"steer_id": s.steer_id, "applied_seq": ev.seq, "run_id": run_id},
                                  run_id=run_id, step_id=step_base + steps)
         yield to_agent_event(applied)
  3. 继续原有流程：messages = await self._context_builder.build(session)
```

硬性要求：

| 要求 | 理由 |
| --- | --- |
| 追加必须用**本 run 自己的 `session` 实例**（就是循环里的那个） | 两个 `Session` 实例各自推算 seq 会撞号，写出重复 seq 让会话不可 resume（`service.py:657-664` 记录了这个坑） |
| 新增 `steer_id` 字段，**不要**设 `injected_by` | `memory/extractor.py:131-141` 按 `injected_by` 排除记忆抽取；用户的真实发言被排除 = 长期记忆静默丢失。另外 `session/session.py:357-367` 的 `user_turn_count` 也会漏计这一轮 |
| 注入顺序 = 队列内 FIFO（`drain_steers` 返回顺序） | 与"按到达顺序投递"（D7）一致 |
| 一次循环头注满、不设条数上限 | 输入来自人；人为上限只会制造"我发的消息被吞了"的静默行为。多个 steer 追加为多条连续 `HumanMessage`（见 §8 T7 的冒烟要求） |
| port 缺席（CLI / 测试）时跳过整段 | `steer_source: SteerSource \| None = None`，默认 None ⇒ 现有行为逐字不变 |

`SteerSource` 端口（新，放在 `session/` 或 `agent/` 侧定义 Protocol，由 `assembly.build_runtime` 注入 `MessageQueueManager`）：

```python
class SteerSource(Protocol):
    async def drain_steers(self, session_id: str, run_id: str) -> list[SteerRequest]: ...
```

> `MessageQueueManager.drain_steers(session_id)` 现在的签名就够了，**不要**为了 run_id 改它：runtime 侧拿到结果后自己筛 `run_id` 不匹配的项（理论上不该有）并保留？——不，指定做法：**直接调用现有签名**，把 `run_id` 写入 `steer/applied`；若某条 `SteerRequest.run_id` 与当前 run 不同（陈旧请求），runtime **丢弃并记结构化日志**，不注入（避免给错误的 run 注入）。

### 4.4 supersede 的写入路径（`SessionService`）

编辑请求（`SendMessageRequest` 增两字段，见 §4.6）处理顺序**固定**：

```
1. 校验：目标必须是本 session 的 user/message、非注入（无 injected_by）、
   未被取代过、且是**最新一条** user 消息 → 否则抛 SupersedeTargetInvalid（409）
2. 按 mode 投递 B（同 §4.6）：
     mode="steer" → register_steer + append STEER_REQUESTED
     mode="queue" → 入队 + append MESSAGE_QUEUED（若带 queue_id 见下）
3. append MESSAGE_SUPERSEDED {superseded_seq, carrier}
```

顺序必须"先 B 后 supersede"（第 2 步在第 3 步之前）：若在第 2、3 步之间崩溃，最坏结果是"B 未投递、A 已被取代"⇒ 该轮为空；反过来（先 supersede 后 B）会得到"A 被取代但 B 从未被接受"，用户无从恢复。**实现必须照此顺序**并在测试里锁住。

**编辑排队项**（还没成为 user/message 的排队消息）：请求带 `queue_id`，语义 = "用新内容替换这个排队项"：

```
1. cancel_queue(queue_id) → append QUEUE_CANCELLED {queue_id}
2. 按 mode 重新投递（同 §4.6 第 2 步，可同时带 supersedes_seq）
```

⇒ 排队项的编辑**不需要**新事件类型：`queue/cancelled`（旧）+ `message/queued`（新）已经足够表达，前端按 queue_id 集合过滤即可。

### 4.5 可见语义（用户逐条裁定，实现者不得自由发挥）

#### 4.5.1 流式期间编辑（A 在途 → 用户改成 B）

| 面 | 行为 |
| --- | --- |
| 模型可见 | 只看到 B。A 那一轮被 shadow 掉（§4.2） |
| 会话界面 | **只显示 B**。A 的问与答**整段删除、不保留、不加任何"已改写"标记**（用户裁定："旧回答段直接删掉也不要显示了，只显示新query的回答"） |
| 已流出的字节 | 立即停止渲染（已到达的 delta 不显示、后续 delta 也不再进入该段）；**不**回滚网络、不伪造"已撤回"提示 |
| 新回答 | 模型在下一个边界拿到 B，产生的新内容成为该轮唯一可见回答 |

#### 4.5.2 发新输入 B（不编辑 A：A 在途未完成）

| 面 | 行为 |
| --- | --- |
| 模型可见 | A + B（A 仍在链上，B 追加其后）⇒ 模型同时回答两者（用户裁定 `query = A+B`） |
| 会话界面 | A 的问句保留、B 的问句显示在它后面；A 未完成的回答按 §4.5.3 显示 |
| 投递 | 默认 queue（run 结束后新 run）；点「立即」= steer（下一个边界同 run 内注入） |

#### 4.5.3 被中断轮的回答显示（用户裁定："被打断的那版回答按 ZCode 的方式显示（保留 + 标记中断）"）

- 保留已流出的内容，在段尾显示**中断标记**（文案：「已中断」，配停止图标；不写"失败"，因为这不是错误）。
- **例外**：若该轮随后被 supersede（§4.5.1），则整段连标记一起删除——被取代的问题不存在了，它的回答没有保留理由。
- 中断判据（前后端同一口径，见 §2 术语表）：该 run 的终态是 `run/failed`（`data.reason ∈ {cancelled, orphaned}`）或 `run/interrupted`。

#### 4.5.4 上一轮被中断时先补齐（D9，用户明确要求新增）

触发条件（三个都要满足，缺一不可）：

1. 会话中**最近一个终态 run** 是被中断轮（§2 判据）；
2. 该轮的问题**没有被 supersede**（被取代就没有"待补齐的问题"，此时注入会与删除语义直接矛盾）；
3. 当前是**新一轮 run 的第一次循环迭代**（`steps == 0` 且本 run 尚未注入过）。

注入方式：在循环头（同 §4.3 位置）追加一条

```python
session.append(USER_MESSAGE, {
    "content": DEFAULT_REGISTRY.assemble("steer:resume_interrupted_turn").fragment_text,
    "injected_by": "interrupted_turn_resume",   # ← 这里**必须**标，它不是用户发言
}, run_id=run_id, step_id=step_base)
```

- 文案正文进 ADR-0023 的 prompt 注册表（新增 `steer:resume_interrupted_turn`），**不得**内联在 runtime 里。
- 文案内容（措辞可润色，语义不得改）：**"注意：你上一轮的回答被用户中断了，它针对的问题尚未回答完。请先补齐那个被中断的问题，再回答用户最新的一条输入。"**
- 位置：注入发生在 B 之后（因为 B 是新 run 的首条 user 消息，早已在链上）⇒ 投影尾部是 `[..., A, B, 指令]`。**指定这样做**：指令作为最后一条消息更可靠地被遵循，且不隐藏 B。
- 该消息标 `injected_by` ⇒ 记忆抽取会正确排除它（它是 runtime 文案），且不计入 `user_turn_count`。

> 与 steer 的交互：**steer 不触发** D9。用户主动"边答边补"的情况下 A 就在链上、模型自然会同时处理 A+B（§4.5.2），再加一条"先补齐"指令纯属噪音。

#### 4.5.5 未投递输入按到达顺序（D7）

run 终态后的驱动顺序 = 按**请求到达顺序**（事件 seq），而不是"先队列后 steer"：`message/queued` 与未被 `steer/applied` 收口的 `steer/requested` 一起排序，取 **1 条**：

- 取 `message/queued` 项 ⇒ 调 `resume_and_launch(task=content)`，成功后写 `queue/consumed {queue_id, run_id}`。
- 取未被 apply 的 `steer/requested` ⇒ 调 `resume_and_launch(task=content)`，成功后写 `steer/applied {steer_id, applied_seq: null, run_id}`（该 steer 的 supersede 语义不受影响，`message/superseded` 独立存在）。
- 一次只投递**一条**：投递会开新 run，后续输入在下一个终态继续接力（避免"一次开 N 个 run"）。

### 4.6 HTTP 契约

`SendMessageRequest`（`web/app.py:276-294`）新增两个**可选**字段（默认 `None` ⇒ 现有行为逐字不变）：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `supersedes_seq` | `int \| None` | 取代 seq 为它的那条 `user/message`（及其整轮）。与 `mode` 正交 |
| `queue_id` | `str \| None` | 本条消息**替换**某条排队项（该排队项被取消） |

新增 2 个读侧端点（前端可见性所需，D11）：

| 方法/路径 | 返回 | 用途 |
| --- | --- | --- |
| `GET /api/sessions/{sid}/queue` | `{"items": [{"queue_id", "content", "created_at"}], "steers": [{"steer_id", "content", "created_at"}]}` | 队列条渲染；重启后也能列出待发送项 |
| `POST /api/sessions/{sid}/queue/flush` | 空队列 → `{"status": "idle"}`；有 → 同 `/messages` 的 SSE 流（`launched` 语义） | ① run 终态后的**同一段代码**给前端复用（如前端想在会话恢复时立刻投递）② 重启后手动投递 |

错误码（沿用现有 `http_error` 映射风格）：

| 场景 | 码 | 异常 |
| --- | --- | --- |
| `supersedes_seq` 指向不存在/非 user/注入/已取代/非最新的消息 | 409 | `SupersedeTargetInvalid` |
| `supersedes_seq` 指向的消息不属于该 session | 404 | `SessionNotFound`？——**不**：用 409 `SupersedeTargetInvalid`，避免把"目标不对"谎报成"会话不存在" |
| `mode="steer"` 且无在途 run | 409 | 既有 `SteerTargetNotFound`（不变） |
| `queue_id` 不存在/已消费/已取消 | 404 | 既有 `QueueItemNotFound`（不变） |

### 4.7 queue 驱动点（D4 唯一实现）

```python
# SessionService（新增）
async def on_run_terminal(self, session_id: str) -> None:
    """run 终态之后消费下一条未投递输入。幂等：无输入 / 已有在途 run 时 no-op。"""
    if self._state.run_manager.get_active(session_id) is not None:
        return                      # 用户已手动开新 run：不要双驱
    nxt = await self._next_undelivered_input(session_id)   # §4.5.5 FIFO
    if nxt is None:
        return
    try:
        launched = await self.resume_and_launch(session_id=session_id, task=nxt.content, ...)
    except ActiveRunConflict:
        # 竞态：另一个入口刚拉起 run。不丢事实——输入仍在事件流里（D5），
        # 下一个终态会再次尝试；内存队列项需放回队首（drain_next 已弹出）。
        await self._requeue_front(nxt)
        return
    self._append_session_event(session_id, <QUEUE_CONSUMED | STEER_APPLIED>, ...)
```

**唯一调用点**（不得有第二处）：`RunManager` 的 `ManagedRun._drive` 完成路径所在模块
**2026-09-22 由 `web/runmanager.py` 迁至 `session/runmanager.py`（纯移位，见 ADR-0040 §4 R1）**，
按旧路径搜索会零命中。原写的行号 `:182` **从来不是有效读数**（复验 findings 的 N1 实测：该参数
在最近若干版一直落在 `:228`，更早一版在 `:225`；`web/runmanager.py:182` 在任何一版都是别的内容）
——现址 `:228` 已核。接线方式：

- `RunManager.__init__(..., on_run_terminal: Callable[[str], Awaitable[None]] | None = None)`（现 `session/runmanager.py:228`，默认 None ⇒ 测试与既有调用零改动）
- `_drive` 的 `finally`：先写终态、`run.finish()` 之后再 `await self._on_run_terminal(session_id)`；异常吞掉并记结构化日志（收尾失败不得影响 run 的终态事实）
- `web/app.py` 启动时注入 `on_run_terminal=SessionService(app.state.agent).on_run_terminal`

新 run 由 `RunManager.launch` 用 `asyncio.create_task` 驱动（`runmanager.py:186-203`），因此**不依赖任何 SSE 消费者**即可跑完；事件经 session 订阅扇出（`web/app.py:1560` 的既有注释已经假定"前端订阅既有 SSE/WS"）。⚠ 注意 orphan 回收（`runmanager.py:140-165`，宽限 300s）：前端若完全没有订阅，接力 run 会在宽限期后被回收——这是既有行为，本 ADR **不修改**，只在 §8 T9 要求一条"终态后接力"的集成测试用真实订阅验证。

### 4.8 重启后重建（D5）

`SessionService.scan_interrupted()`（`service.py:1137`）之后新增一步 `rebuild_message_queues()`：

```
对每个 session：
  已消费 = {queue/consumed.queue_id} ∪ {steer/applied.steer_id}
  待投递 = 按 seq 升序的
             message/queued 中 未被 QUEUE_CANCELLED 取消、不在已消费集合的项
           ∪ steer/requested 中 未被 steer/applied 收口的项
  ⇒ 重建 MessageQueueManager 的内存队列（只重建，不自动启动 run）
```

- **为什么不自动启动 run**：进程刚起、无客户端订阅，起了也会被 orphan 回收（§4.7 ⚠），且用户不在场时自动跑 token 是不可接受的。
- 用户回到会话后：前端调 `GET /queue` 显示「待发送 N」，用户点「立即发送」→ `POST /queue/flush`。
- 重建是**幂等**的：重复执行得到同一集合；重建只在启动时跑一次。
- 尚未被消费的 `message/queued` 已经是 durable 事实，所以"队列在崩溃/重启后还在"由事件流保证，不依赖任何内存快照。

---

## 5. 前端契约（#194 / #195 的实现依据）

### 5.1 流式期间输入可用（D10）

- `Composer.tsx:76` 的 `locked = streaming || approvalPending` 必须拆开：
  - `approvalPending` 仍禁用输入（等待审批时输入无意义，且与审批 UI 竞争）；
  - `streaming` **不再**禁用输入。停止按钮（`:170-190`）与发送按钮并存。
- 发送键语义（与上游一致，见 §6）：`Enter` = 默认模式（queue）；`Ctrl/Cmd+Enter` = steer（同一份输入，立即投递）。

### 5.2 队列条与 steer 状态可见（D11）

- 位置：Composer 输入框**正上方**（不与 `.composer-esc-hint` 争左下角，那是 #194 的病灶）。
- 每个排队项显示：内容摘要（1 行截断）+ 三个动作：**编辑** / **立即** / **取消**。
  - 「编辑」= 就地编辑该排队项 → `POST /messages {mode, content, queue_id}`（§4.4 编辑排队项）
  - 「立即」= 升级为 steer → `POST /messages {mode:"steer", content, queue_id}`（先取消原排队项）
  - 「取消」= `POST /sessions/{sid}/queue/{queue_id}/cancel`
- 空队列 ⇒ 队列条**不渲染**（不占位、不闪烁）。
- 数据来源：`GET /queue` + 事件流里的 `message/queued` / `queue/cancelled` / `steer/requested` / `queue/consumed`（实时增量）。
- 状态源优先级：**事件流是唯一事实**，`GET /queue` 只用于首屏/重连补齐（避免两个真相）。

### 5.3 用户消息动作行（#195）

在每条**用户消息**上显示动作行：**复制 / 编辑 / 分叉**（对齐 Codex 的三图标）。约束：

| 约束 | 说明 |
| --- | --- |
| `分叉` 保留既有行为与守卫（`Conversation.tsx:450-465`，`turn.status !== 'streaming'`） | 不改语义 |
| `编辑` 在**最新一条**用户消息上可用；其余消息上禁用（置灰 + `title` 说明），因为 supersede 只允许最新一条（D8） | 禁用而不是隐藏：让用户知道能力存在 |
| `编辑` 的目标是"被取代的那条" ⇒ 请求带 `supersedes_seq` | §4.6 |
| 编辑态是**原地输入**（该条消息变成 textarea + 保存/取消），保存即发请求 | 不弹模态 |
| 编辑成功后：该轮整段从界面消失（§4.5.1） | 由 `message/superseded` 驱动，不靠前端猜 |
| `复制` 复制消息正文；键盘可达、`aria-label` 中文 | 无障碍 |

### 5.4 前端投影规则（必须与后端 §4.2 同构）

前端收到 `message/superseded {superseded_seq}` 时，按**同一区间规则**（`[superseded_seq, 下一条未被取代的 user 消息)`）把该区间内已渲染的事件（含流式 delta 段）从视图中移除。实现落点：`web/src/lib/projection.ts`（与后端 `derive.py` 的区间语义保持同名同义），并在 e2e 里锁一条"编辑后旧回答段消失"。

---

## 6. 上游对照（为什么这样选）

| 产品 | 机制 | 与本 ADR 的关系 |
| --- | --- | --- |
| Codex CLI | `Tab` = 排队下一轮；`Enter` = 注入当前轮 | D1 双通道的直接来源 |
| Cursor | `Cmd+Enter` 在 agent 下一次工具调用时投递 | steer 的投递边界 = "下一个工具/模型调用边界"（用户裁定） |
| Claude Code | 排队消息在工具调用结束后、同一轮内交给模型 | 同 steer 边界 |
| Gemini CLI | model steering（实验），下一轮注入 | 边界更保守；本 ADR 取"下一模型调用前" |
| OpenAI `response.steer`（WebSocket） | 原响应以 `response.incomplete`（reason=`steered`）收口，新建后继响应 | 印证"已流出内容不可回滚 + 后继回答"的形状 |
| ChatGPT / Claude.ai 的"编辑消息" | 消息分支（branching），旧分支保留 | 与本 ADR 的 supersede 不同：本仓选择"取代 + 旧轮不显示"（用户裁定），不是分支 |
| pi / oh-my-pi | `peekSteeringQueue` / `popLastSteer` | 已有的 steering 队列原语，命名与本 ADR 的 queue/steer 对齐 |

---

## 7. 不变量、风险与手工验证

### 7.1 触碰的不变量（自检表）

| 不变量 | 本 ADR 如何守住 |
| --- | --- |
| #3 append-only typed SessionEvent | 只新增事件，从不改写/删除既有事件；取代只影响派生 |
| #4 Event ≠ Diagnostic Log | `queue/consumed` / `message/superseded` 都是运行事实（可对账），不是诊断日志 |
| #7 Complete save ≠ complete inject | supersede 正是这条不变量的正面使用：事件全留，投影剔除 |
| #22 Web UI 不维护第二套不可对账的真相 | 前端队列状态以事件流为唯一事实，`GET /queue` 只做补齐 |

### 7.2 风险与对策

| 风险 | 对策 |
| --- | --- |
| 旁路追加撞 seq（会话不可 resume） | 所有 service 侧追加只走 `_append_session_event`（内部优先 `_live_session`，`service.py:657-678`）；runtime 侧用自己那个 `session` |
| steer 消息被记忆抽取丢弃 | 禁止复用 `injected_by`（D6）；测试锁住"steer 消息计入 `user_turn_count` 且参与抽取" |
| supersede 区间把 tool_call 切出 dangling | 区间边界固定为"到下一条未被取代的 user 消息"，一轮内的 tool_call/result 必然同进同出；dangling 计算在 shadow 之后（§4.2.4） |
| 连续多条 steer 造成连续 `HumanMessage`，某 provider 拒绝 | §8 T7：对**真实** provider 冒烟一次（deepseek/qwen 任一）；若被拒，投影侧合并为一条（在实现 PR 里写回结论） |
| 终态驱动与用户手动发送竞态 | `on_run_terminal` 先查 `get_active`，捕获 `ActiveRunConflict` 后把输入放回队首 + 依赖事件流兜底（§4.7） |
| 接力 run 无订阅被 orphan 回收 | 既有行为（宽限 300s），不在本 ADR 修；T9 用真实订阅验证 |
| 前端与后端 shadow 规则漂移 | 同一区间定义、同一命名；后端单测 + 前端 e2e 各锁一条 |

### 7.3 需要人工/真实环境验证的项（写进实现 PR 的"验证记录"）

1. 连续 steer 在真实 provider 上是否被接受（§8 T7）。
2. 编辑在途问句后，模型新回答确实只针对 B（人工点一次，附会话 JSONL 路径）。
3. 重启后 `GET /queue` 能列出待发送项（手工 kill + 重启一次）。

---

## 8. 测试要求（本 ADR 的验收清单，逐条必须有对应用例）

> 遵循既有测试哲学：只测外部行为（事件流 + 投影 + HTTP 响应），不测内部实现细节。先红后绿。

| # | 用例 | 层级 | 断言 |
| --- | --- | --- | --- |
| T1 | queue 自动接力 | 集成（FakeModel） | run 1 在途时 `mode=queue` 发消息 → 事件流有 `message/queued`；run 1 终态后**无任何客户端动作**，run 2 自动出现且其投影含该消息；事件流有 `queue/consumed` |
| T2 | steer 在同 run 内注入 | 集成 | run 在途时 `mode=steer` → `steer/requested`；下一循环头出现 `user/message{steer_id}` + `steer/applied{applied_seq}`；run **未**新开（run_id 不变） |
| T3 | A+B 都在链上 | 单测（derive + 集成） | steer 后投影尾部为 `[..., Human(A), Human(B)]`（A 未被剔除） |
| T4 | supersede 剔除整轮 | 单测 `derive_messages` | `[A][AI(A答)][B]` + `message/superseded{A}` ⇒ 投影 = `[B]`；区间内 tool_call/result 也不出现，且不产生合成 ToolMessage |
| T5 | supersede 只允许最新 | HTTP | 对非最新 user 消息 supersede → 409 `SupersedeTargetInvalid`；对 `injected_by` 消息 → 409；重复 supersede 同一 seq → 幂等（不报错、以最早一条为准） |
| T6 | 中断补齐指令 | 集成 | 上一 run `run/failed{reason:"cancelled"}` + 新输入 → 投影出现 `injected_by="interrupted_turn_resume"` 的消息且位于最后；若该轮已被 supersede ⇒ **不出现** |
| T7 | 真实 provider 冒烟 | 手工/可选 | 两条连续 steer → provider 接受；结论写回实现 PR |
| T8 | 队列表与取消 | HTTP | `GET /queue` 返回待发送项；`cancel` → `queue/cancelled` 且后续不再投递；编辑排队项 → 旧项 cancelled + 新项 queued |
| T9 | 重启重建 | 集成 | 构造含未消费 `message/queued` 的事件流 → 启动 → `GET /queue` 列出该项；不自动开 run |
| T10 | steer 的记忆抽取 | 单测 | steer 消息**参与**记忆抽取（`injected_by` 为空）；`interrupted_turn_resume` 消息**不参与** |
| T11 | 前端：编辑后旧段消失 | e2e | 流式中编辑 → 旧回答段从 DOM 移除、新问句可见、新回答追加 |
| T12 | 前端：队列条可见可操作 | e2e | 排队项显示；「立即」触发 steer；「取消」移除；空队列不渲染 |

**回归护栏**：`ruff check` + 全量 `pytest` 必须绿；`tests/test_structured_logging.py:95-133`（usage 三字段）、`tests/web/test_web_api.py:297-304`（空 task → 422）等既有断言不得被本改动改掉（除非本 ADR 明确要求）。

---

## 9. 分期与 issue 映射

| 阶段 | 内容 | 票 |
| --- | --- | --- |
| P0（前置，必须先做） | 前端解除流式禁用 + Esc 提示不再压档位控件 | #194、D10 |
| P1（后端核心） | 事件 + 投影 shadow + steer 注入 port + `on_run_terminal` 驱动 + `GET/flush` 端点 + 重启重建 | #196、§4 |
| P2（前端可见面） | 队列条 / 立即 / 取消 / 编辑 + steer 来源标记 + 用户消息动作行 + shadow 渲染 | #195、§5 |
| P3（补齐指令） | prompt 注册表 `steer:resume_interrupted_turn` + 注入条件 | D9、§4.5.4 |

P1 与 P2 可并行，但 **P1 的 `GET /queue` 契约必须先冻结**（P2 依赖它）。

---

## 10. Out of scope（明确不做，防止顺手扩散）

- 消息分支（branch）/ 树状会话视图——本仓选择"取代"，不引入分支模型。
- 编辑**别人的**历史轮（只允许最新一条，D8）。
- 排队项的持久化优先级/重排（只 FIFO）。
- 多客户端并发编辑同一条消息的冲突解决（沿用现有 `ActiveRunConflict` / 409 语义）。
- 修改 orphan 回收策略（§4.7 ⚠）。
- steer 的"部分回滚/撤回已输出内容"（物理不可能，见 §1.3）。

---

## 11. Consequences

**正面**：queue/steer 从"契约在、实现不在"变成可对账的闭环；队列跨重启存活；编辑语义与界面一致且可测；模型可见投影与 append-only 日志解耦（#7 的正面用法）。

**代价**：新增 2 个 durable 事件类型；`derive_messages` 多一条区间规则（与 compaction 同构，成本可控）；前端需要实现与后端同构的 shadow 渲染；`RunManager` 多一个回调注入点。

**未决（实现时必须回填本文件）**：

1. 连续多条 steer 在真实 provider 上的接受度（T7 结论）。
2. 接力 run 在"前端无任何订阅"场景下的实际行为（T9 的观察结果 → 是否需要在 §4.7 补一条订阅保持策略）。

---

## 12. #213 决议：不引入增量重写计数器（对照 DSH `replaceGeneration` / `contentGeneration`）

**背景**：#213 源自与 DeepSeek Harness 的对照——DSH 用一对服务端单调计数器，让增量消费者区分
"纯尾部增长"与"中间被改写"。票面写明这是**设计票**（先决定再改）：① 改契约要连
`SPEC_ROOT/03_SESSION_EVENT_MODEL.md` 与 ADR 一起改；② 计数器必须服务端单一 owner，不得引入
第二套序号语义；③ 无用户可见缺陷支撑，优先级待定。

**决议（2026-09-17）：不新增任何计数字段、不改契约。** 票面的前提在本项目不成立：

1. **重写已经是显式事件，不需要"靠序号与内容推断"**。`message/superseded`（§4.1 / §4.2）
   带自己的 `seq`，其 `superseded_seq` 指向被取代的问句，服务端单一 owner（`session/service.py`
   §4.6 分支写入，先投递 carrier B 再写本事件）。DSH 之所以需要计数器，前提是"内容面被原地
   改写、没有对应事件"；本项目没有可原地改写的消息对象，只有 append-only 事件（不变量 #3）。
2. **计数器会成为同一事实的第二套序号语义**（票面 ② 明确禁止）：该事件的 `seq` 本身已单调、
   已持久、已重放；再加一个计数等于给同一事实两套真相。
3. **取代事实不可能被静默跳过**（决定性实测结论）：客户端续传游标是**它自己的水位线**
   （"≤ 此值都已应用"：`lastAppliedSeqRef`，即**本地已应用事件的最大 seq**，与 run 是以哪条
   分支启动无关；游标从不取自服务端声明），所以它缺的任何事件其
   `seq` 必 > 游标 ⇒ 必落在 `seq > after_seq` 的重放窗口内；同时跳号帧按契约 T4 **不投影**、
   直接丢弃并重连（`useSession.ts` `isSeqGap` 分支），不会"跳过丢帧继续往前"。两者叠加 ⇒
   取代帧只有"已应用"与"会被重放"两态，没有可以丢掉的第三态。取代帧一到，前端按 §5.4
   由**全量事件日志**重算区间，被取代的整轮立刻整段移除。
4. **没有可观察的失效面**（票面 ③）：项目内不存在以序列号为键的增量缓存消费方——派生快照
   （`session/derive.py`）每次请求现算，前端投影是纯折叠函数，故不存在"重写后需要失效的缓存"。

**锁定方式**：`web/e2e/supersede-gap-recovery.spec.ts` 是本决议的证伪点。它造出客户端侧最坏
形态——**取代帧本身在 live 通道被丢掉**，客户端先吃到一段连续增长、再撞上一条跳号的尾帧
（与"尾部增长"同形），且该帧只能由重放窗口送回来（夹具按 `seq > after_seq` 真裁）——断言：
① 撞上跳号那次重连的游标 = **丢帧前最后一条已应用事件**（重放窗口必含该帧）；② 旧轮问 + 答
整段消失、改写后的问题与其回答各恰好一次；③ 该帧确实进了本地事件日志（Inspector 可见）；
④ **只发生一次重连、且不出现 give-up 条**——这条是判定性的：视图正确本身不构成证据，因为
durable 日志里该帧还在，"重试 3 次耗尽 → 全量回读"同样能把视图兜对。

**夹具保真度（勿当成后端实证）**：真后端写 `message/superseded` 的时机是**登记 carrier 之后
立刻**（`service.py` §4.4 第 3 步），即落在新一轮流的**前段**；夹具把它放到流尾，因为本用例
要丢的**必须就是它**。该形态是**合成**的客户端最坏情形，不是后端产得出来的形态——后端真会
丢帧的位置是 `runmanager.py` 的有界队列，满时丢**最旧**那条（ADR-0016），方向相反。服务端
那半的重放语义由 `tests/web/test_web_ws_relay.py` 锁，本用例只锁客户端这半。

**红证（2026-09-17 实测，两次）**：①把该帧**同时**从所有通道剔除 ⇒ 旧轮留在视图
（`原始问题` 计数 1）；②把重放窗口置空（等价"游标越过丢帧处"）⇒ `subscribes=[12,12,12]`
（重试 3 次耗尽）、give-up 条出现、视图由两次 `/events` 全量回读兜对，断言 ④ 变红
（实测 `Expected 1 / Received 3`；正常形态下为 `[12]`、give-up 条 0）⇒ 四条断言都有判别力，
不是"视图恰好这么渲染"。**断言 ④ 必须排在视图断言之后**：兜底路径要先耗尽重试才会把视图
修对，"视图已对"在坏形态里恰好意味着 3 次订阅已发完；上移到视图断言之前即退化为竞态。

**本决议失效（应重开并升级为契约提案）的条件**：

- 出现"服务端在不产生事件的情况下改写已下发内容"的路径（例如原地修改流式块的 carrier 或顺序）；
- 出现以序列号为键的增量缓存消费方（前端或第三方）；
- 客户端改为从"服务端声明的游标"而非自身水位线续传——那会把第 3 条不变量交出去。

---

## 13. `/messages` 的响应窗口与结局分派（#221）

**问题**：交付层会把正常流式的响应**整体攒包**到 run 结束才下发（实测响应头
44.2s 才到；见 §4 与 `docs/BACKEND_CONTRACT_STREAMING_UI.md`），而 4xx/422 是
**完整且极短**的 JSON。于是前端用一条短窗判别（`useSession.ts::raceEarlyResponse`，
`EARLY_RESPONSE_WINDOW_MS = 1200`）分两支：窗内落定 = 真失败（要读 status /
detail），窗外 = 判为「正常流式」，此时 sid 已知，改走 WS 接流。

**#221 的缺陷**：窗外那一支**只挂了 rejection 通道**（`pending.catch`）。响应已落定
却非 2xx（409/404/422）时既不是 rejection、也走不到窗内的分派 ⇒ 不报错、消息没被
受理、而 Composer 早已清空输入框（它的 submit 末尾无条件 `setValue('')`）。用户视角
是「按了发送、输入框空了、什么都没发生」。这是既有缺口（T8 #138 / #205 时期就有），
不是 #219 引入的——#219 的 steer→queue 回退也只是让它在慢链路上更常被撞到。

**决策**：

1. **一张表**：`decideFollowUpOutcome({status, hasBody, contentType, mode, detail,
   hasQueueId}) → stream | ack | retry-queue | fail{text}`（纯函数，导出以便单测）。
   窗内与窗外**必须**走同一张表——两支各写一份判据正是缺陷的成因。
2. **窗外也要消费落定**：`pending.then(...)` 把迟到的落定交给同一个 `settle`，
   而不是只接 rejection。
3. **`retry-queue`（409 + steer 打空）在窗外同样生效**：去掉 `queue_id` 重投
   （理由见 §5.2 与 `service.py` 先 `cancel_queue` 再判在途 run 的顺序）。递归深度恒为
   1：重投的 `mode` 是 `queue`，而这张表只在 `mode === 'steer'` 时给出 `retry-queue`。
4. **纠正「接错流」必须推进代际**：窗外既然已按「launched」接过一条 WS 流，迟到落定
   一旦证明判错（非事件流），除了 `cancel()` 那条流还**必须**推进
   `streamGenRef.current`——只 cancel 停不掉它已经排定的重连定时器，那三个退避会跑完并
   弹一条假的「连接中断」，把真正的原因盖掉（`flushQueue` 早就是这么做的，见其
   `lateOutcome`）。代际按**投递尝试**计（不是按一次 `sendFollowUp` 计），否则回退重投
   会被自己的推进作废。
5. **只有 409 读 body**：它是「同码不同因」的唯一区分依据（§5.1 与 T8 #138 的人工裁决
   支），也是要转述给用户的那句话；404/422/其余状态各有固定文案，不必为不读的 body
   等一次 I/O。
6. **接流前要有代际守卫**：`await raceEarlyResponse` 之后、写会话级游标与流引用之前，
   必须先判「我是否还是权威消费者」（`submitTask` / `resumeLiveStream` 在同一位置都有
   这道闸）——否则一条失效的流会被挂到新会话身上，连它的 cancel 句柄与停摆检查一起
   被顶掉。

**验收（`web/e2e/multiturn-queue.spec.ts` T12k–T12q）**：四类状态码各一条（迟到 409
steer 打空 → 改投 queue；迟到 409 人工裁决 → 原样转述 detail；迟到 404 → 会话没了；
迟到 422 → 参数无效），外加三条：**迟到 2xx JSON 收据**（T12o，锁第 4 点——纠正后必须
**没有**假「连接中断」）、**迟到事件流**（T12p：判对则不许动，WS 继续收该 run 的输出）、
**纠正之后的回退重投仍失败**（T12q：它必须把原因报出来——报错守卫若读 `sendFollowUp`
入口的代际而不是**本次尝试**的代际，纠正自增出来的那个代际就会被误读成「用户换了会话」，
重投的失败被静默丢弃，回到「消息已被拒、输入框已清空、界面一个字都不说」的原症状）。

红证：把窗外那支还原成只接 rejection ⇒ T12k–T12n 全红（其中迟到 422 那条的实测症状正是
假「连接中断…重试 3 次未成功」盖掉了真原因）；把迟到链的报错守卫换成入口代际 ⇒
T12l / T12q 全红。

**不在本决议内**（各自独立）：`model/failed.message` 的呈现（ADR-0033 §3）；
`submitTask` 的窗外落定（`POST /api/sessions` 走的是「认领新会话」另一套机制，其迟到
非 2xx 会把「认领超时」当成原因报出来——**本 ADR 不覆盖**，残留登记见
`docs/PHASE_STATUS.md` 的 #221 条目）。
