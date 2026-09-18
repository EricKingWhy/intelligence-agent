# ADR-0037 — 投影层引用稳定与 eventsVersion

**Status**: Proposed（待用户批准）
**Date**: 2026-09-18
**Related**: ADR-0016 §2（COW 纪律的**两处文档化例外**：**第一处** = `events` append-only 日志，**第二处** = `seenSeqs`）——**本 ADR 是它的补充，不覆盖它**；
ADR-0014-web-ui-redesign-architecture D7；`docs/HANDOFF_PERF_FRONTEND.md` §4.3 / §6 / §9 / §11；
本批 N2 子票（GitHub #271）
**真实证据**: `3344e34`（P0-1 实测数字）、`a78c322`（P0-2a + 合帧，本决策得以成立的前提）、
`f97f322`（Web UI Redesign Phase 1-9，消费端依赖格局在此改变）；`web/src/lib/projection.ts:1149-1180`

---

## Context

### 1. 一对**互相冲突**的诉求，它们都是真的

**诉求 A（渲染性能）**：`ConversationState.events` 必须是**引用稳定**的数组。
P0-1（`3344e34`）把原来「每事件整体克隆」的写法改成 append-only 共享数组后实测：

| 基准 | 改造前 | 改造后 |
| --- | --- | --- |
| `applyEvent` @20k 事件 | 240.9 µs/事件 | **0.2 µs/事件**（≈1200×；当次验收预算为 <10 µs/事件） |
| `projectHistory` 4650 事件 | 35.0 ms | **2.8 ms** |
| `projectHistory` 20k 事件 | 1536.9 ms | **7.2 ms** |

**诉求 B（渲染正确性 / 重算时机）**：消费端 `useMemo` / `useEffect` 需要一个**能感知「events 又追加了」的键**。
`React.memo` 与 `useMemo` 的比较语义是**引用相等**——引用刻意稳定时，依赖 `conversation.events`
的 memo 就**永远不会重算**。这不是"没优化"，是**陈旧渲染**。

两者在同一块代码上直接对立：A 要求引用不动，B 要求引用要动。

### 2. 为什么现有的两种「感知追加」手段都不成立

**(a) 把 `events` 改回「每次换引用」** —— 等于**回退 P0-1**。
代价就是 §1 那张表的右列全部退回左列：长会话（20k 事件级）的流式渲染与历史重建重新劣化到
**100 倍以上**量级。**不可接受**，且 `web/src/lib/projection.test.ts` 的引用稳定契约测试
（7 个）正是在守这件事。

**(b) 消费端一律改用 `events.length` 作键** —— 语义上**不封闭**：

- `web/src/lib/projection.ts:1174` 有**去重短路**
  （`if (event.seq !== null && state.seenSeqs.has(event.seq)) return state;`）——
  重复投递被整帧丢弃、**不 push**，长度自然不变——此时用长度当键**凑巧**与正确结果一致（渲染面确实没变），但那是**巧合而不是契约**：长度**无法区分「长度不变但内容改变」**，将来任何一种非纯追加的写入方式都会静默漏掉。
- `web/src/lib/projection.ts:1161-1164` 有 **quarantine 分支**：形状不可辨的帧进
  `state.events.push(quarantined)` 且同时写 `next.unknown_events` —— 这一路**确实改变了渲染面**。
- 更要紧的是：`events.length` 无法区分「长度不变但内容变」这一类将来可能出现的写入方式。
  用长度当版本号是**用巧合代替契约**。

### 3. 为什么现在必须把这件事写下来（三条 ADR 触发条件）

**(1) 难逆转**：`eventsVersion` 一旦进入 `ConversationState` 的形状，**所有**需要"追加后重算"的
`useMemo` / `useEffect` 都以它为键。要删掉它，必须逐处回改消费端，且回改后**必然重新引入陈旧 memo**。
它不是可以随手清理的局部实现细节。

**(2) 未来一定会困惑**：`ADR-0016 §2` 把 COW 纪律的**两处文档化例外**并列写下——
**第一处**是 `events`（append-only 日志，见 `HANDOFF_PERF_FRONTEND` §4.3/§6），**第二处**是 `seenSeqs`（`ADR-0016:33` 原文：「`ConversationState.seenSeqs`（Set，只增不改、跨快照共享引用）与 `events` 数组同构」）。
一个读者同时看到「**例外**」和「**版本号**」，会合理地推断其中一个是多余的、可以删掉。
没有这份 ADR，**下一次重构极可能把 `eventsVersion` 当死代码删掉**——删掉之后所有 `memo` 静默失效，
而**测试很可能仍然全绿**（陈旧渲染不抛异常）。

**(3) 真实权衡**：候选方案在 `Decision` 的结论表里**收敛为三个**（另有三个更细的变体列在 `Alternatives considered`），其中只有一个是可接受的，
而「唯一可接受」这个结论**不写在代码里**——代码只能表达"现在长什么样"，表达不了"为什么不能改成另外两种"。

---

## Decision

### D1：`events` 保持 append-only / 引用跨 state 稳定，**不回退**

`applyEvent` 与 `projectHistory` 继续以共享数组 + `push` 的方式维护 `events`；
既有条目**永不改写**、顺序**不变**。这是 P0-1 的成果（`3344e34`），
由 `web/src/lib/projection.test.ts` 的引用稳定契约测试锁定，**不得**为了让某个 memo 重算而回退。

### D2：新增 `eventsVersion`，作为**追加发生**的唯一精确信号

**确切语义（写死，后续不得扩大解释）**：

- 初值 `0`——`initConversation` 与 `projectHistory` 的产物都是 `0`。
- **每当 `state.events.push(...)` 成功执行一次，`next.eventsVersion = state.eventsVersion + 1`**。
- `eventsVersion` 只度量 events 数组的 append 次数，不是通用脏标记，也不进 SessionEvent。
- **按「是否真的 push」判定，不按「是否进入 `applyEvent`」判定**，两处边界写死：
  - `projection.ts:1174` 的**去重短路**：`return state`，**没有** push ⇒ **不递增**。
  - `projection.ts:1161-1163` 的**quarantine 分支**：它也 push 了 events ⇒ **递增**。
- 它**不**度量 `turns` / `tools` / `compactions` 的变化。那几类本来就有自己的引用变化
  （COW 深克隆被改写的那部分），**不需要** `eventsVersion`。
- 它**不是**「state 版本号」，**不得**被当作通用脏标记使用。

### D3：消费端契约——memo 键写 `eventsVersion`，**不要**写 `events`

需要「events 追加后重算」的 `useMemo` / `useEffect`，依赖项写
**`conversation?.eventsVersion`**，**不要**写 `conversation?.events`——
后者引用刻意稳定（D1），写了等于**永不重算**。

本批将修正的三处（归属 N2 / GitHub #271，**本 ADR 不改代码**）：

| 位置 | 现状 | 问题 |
| --- | --- | --- |
| `web/src/components/StepDetail.tsx:123` | `[conversation?.events]` | 追加后不重算（陈旧） |
| `web/src/components/StepDetail.tsx:944` | `[conversation.events]` | 同上 |
| `web/src/components/StepDetail.tsx:955` | `[conversation.events, selectedKey]` | 同上（`selectedKey` 变化才会重算） |

同一处 `web/src/lib/projection.ts:1149-1154` 的 COW docstring 末句
（「…**无消费者把 `events` 放进 memo/useEffect 依赖**，不受影响」）**已不属实**，
由 N2 在同一处一起修正（行为与注释必须同改，避免两个 commit 撞同一段）。
该断言与 `docs/HANDOFF_PERF_FRONTEND.md:230`（"全量核对无消费者…故未引入 eventsVersion"）
同源，两者**同时失效**（`f97f322` 晚于该回执所依据的 `a78c322`）。

### D4：与 `ADR-0016 §2` 的关系——**补充，不覆盖**

| | 记录什么 | 状态 |
| --- | --- | --- |
| `ADR-0016 §2` | 写下 COW 纪律的**两处**文档化例外：**第一处** = `events` append-only 日志（见 `HANDOFF_PERF_FRONTEND` §4.3/§6），**第二处** = `seenSeqs`：`events` 与 `seenSeqs` 同构，都是**追加型共享簿记**，不参与「被改写才深克隆」的规则 | **仍然有效，本 ADR 不改它、不覆盖它** |
| **本 ADR（0037）** | **在该例外之上**新加的**消费端契约**：正因为这个例外使引用稳定，消费端必须改用 `eventsVersion` 作 memo 键 | 新增 |

一句话：**ADR-0016 §2 说的是「events 为什么可以是共享的」；ADR-0037 说的是「既然它是共享的，
消费端该怎么正确重算」。** 前者因后者更完整，后者**依赖**前者成立。

### 候选方案对照（结论表，理由见 `Alternatives considered`）

| 方案 | 代价 | 是否选 |
| --- | --- | --- |
| ① 改回「每次换引用」（依赖引用相等感知追加） | 回退 P0-1：20k 事件重新劣化 ~100× | ❌ |
| ② 消费端改用 `events.length` 作键 | 语义不封闭（`:1174` 去重短路 / `:1161` quarantine 分支） | ❌ |
| ③ **新增 `eventsVersion`** | 状态形状多一个字段；消费端必须遵守新键纪律，漏用会**静默陈旧** | ✅ |

---

## Consequences

**正面**

- `memo` / `useMemo` 能**精确**感知「events 追加了」——粒度就是 append 次数，不多不少。
- 保住了 P0-1 的性能成果（D1 不动），同时修好了被它顺带打破的消费端重算时机（D3）。
- 语义可判定：`eventsVersion` 的值只由 `push` 次数决定，**不依赖消费端如何使用它**。

**负面 / 代价（必须承认）**

- `ConversationState` 形状**多一个字段**；所有构造 state 的地方（含测试夹具、合成事件 helper）
  都要带上它（初值 `0`）。
- 消费端**必须遵守新键纪律**——写 `events` 而不是 `eventsVersion` 的 memo 会
  **静默陈旧**：不报错、不告警、类型系统也拦不住（两者都是合法成员）。
  本 ADR 与 D3 的表是唯一的防线，因此**必须留在仓库里**。
- `eventsVersion` 是**投影实现细节**，不是持久事实——它**不进** `SessionEvent`、
  **不进** JSONL、**不参与** resume/replay/fork 的任何对账（见 Non-Goals）。

---

## Non-Goals

- **不改变 `events` 的 append-only 语义**（D1 明确保留；引用稳定的契约测试不动）。
- **不把 `eventsVersion` 当通用脏标记**，**不用它替代 `seq`**（`seq` 是 durable 事实序号）、
  不替代 `run_id`、不替代 `seenSeqs`（去重键）。
- **不引入第二个会话真相源**（架构不变量 #22）：`eventsVersion` 是**派生**的计数，
  不携带任何独立事实；丢了它只影响重算时机，不影响任何真相。
- **不借此改动 `projectHistory` 的输入契约**：输入**仍是完整历史**。
  （历史读取不裁剪是已冻结的决策，见本批索引 `docs/tickets/perf-interaction-smoothness-2026-09-18.md`
  的「已撤回项」表——`GET /events` 分批取已撤回，归属另一个批次的 T09 / #245。）
- **不改 `docs/adr/0016-*.md`**：`ADR-0016 §2` 逐字保留。
- **不新增第二套版本机制**（例如同时再引入 `eventsEpoch`）：一个就够，
  多余机制正是 §3(2) 说的"未来困惑"的来源。

---

## Alternatives considered

| 方案 | 为什么没选 |
| --- | --- |
| **① 把 `events` 改回「每次换引用」**（消费端继续依赖引用相等） | 这是**回退 P0-1**：`3344e34` 的实测（applyEvent @20k 240.9µs → 0.2µs（验收预算 <10µs）；`projectHistory` 4650 事件 35.0ms → 2.8ms；20k 事件 1536.9ms → 7.2ms）全部退回。而且 `web/src/lib/projection.test.ts` 的 7 个引用稳定契约测试正是为守住它而设——选这条路＝**为了让 memo 生效而牺牲 100× 量级的性能**，把"陈旧渲染"换成"整体变慢"，问题并没解决。 |
| **② 消费端改用 `events.length` 作键** | 语义**不封闭**。`web/src/lib/projection.ts:1174` 的**去重短路**（`…state.seenSeqs.has(event.seq)) return state;`）**不 push**，长度不变——用长度当键在"重复投递被丢弃"这一情形下**恰好**与"没发生任何事"不可区分；而 `web/src/lib/projection.ts:1161-1163` 的 **quarantine 分支**确实 push 了 events（`state.events.push(quarantined)`）并同时写 `next.unknown_events`，是真实渲染面变化。更根本的是：长度**无法区分「长度不变但内容变」**，将来任何非纯追加的写入方式都会静默漏掉；用长度当版本号是**用巧合代替契约**。 |
| **③（已选）新增 `eventsVersion`** | 唯一能把「append 发生」表达成**精确信号**的方案：只在 `push` 成功时递增（D2），去重短路不递增、quarantine 分支递增，两处边界都由"是否真的 push"这一条规则唯一确定。代价是形状多一字段 + 消费端键纪律（已列入 Consequences 与 D3）。 |
| **④ 给 `TurnView` / `StepDetail` 加自定义 `memo` 比较器，逐个绕开** | 治标且**不可持续**：每个新消费端都要重新决定"比较哪些字段"，比较器本身就是第二套（且更容易写错的）版本机制；一处写错就是静默陈旧。而且它**没有**解决"如何精确感知 append"这个问题，只是把它藏进比较器里。 |
| **⑤ 用 `seenSeqs` 的 size 当版本号** | `seenSeqs` 是**去重键集合**，与 `events` 数组**同构但不等同**（`ADR-0016 §2` 已把它们并列为两处例外）：两者在 quarantine 路径上会同时增长，但 `seenSeqs` 的增长语义由去重规则决定，未来任何对去重逻辑的调整都会**顺带**改变版本语义——把渲染时机耦合到去重实现上是错误的分层。 |
| **⑥ 什么都不做，接受陈旧渲染** | 不成立：`StepDetail.tsx:123` / `:944` / `:955` 的三处陈旧不是性能问题，是**正确性**问题——追加了事件而 memo 不重算，UI 会停在旧内容上。（这也是本批把它单列为 **N2**、与纯性能票 F1/F2 分开的原因。） |
