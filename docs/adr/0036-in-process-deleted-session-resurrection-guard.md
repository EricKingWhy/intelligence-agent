# ADR-0036 — 硬删会话的进程内复活护栏（迟到的旁路写者不得重建日志）

**Status**: Accepted
**Date**: 2026-09-17
**Related**: #232（本决策的 ticket）、ADR-0029（会话硬删；**本 ADR 是它的实现补丁，不是修订**）、
ADR-0026（记忆硬删 + outbox）、ADR-0004（不做自动清理）
**真实证据**: `docs/LIVE_BROWSER_TEST_20260917.md` §9.4 F13（三轮独立复现）

---

## Context

### 1. 症状：删掉的会话自己回来了，而且日志被截断

真机实测（Round 3 巡检 3）：用户 `DELETE /api/sessions/{id}` 拿到 `200` + `events: N`
（回执**承诺删净 N 条**），20–30s 后该会话**自己回到列表**，且日志里只剩 **1 条**
`memory/degraded`——原 N 条事件随目录一起被删掉了，回来的是**重建出来的新文件**。

### 2. 根因：旁路写者在 ADR-0029 五道守卫的视野之外

`MemoryWriteback` 是 run 收尾后 **fire-and-forget** 的写者（`memory/writeback.py`）。
它完成时调 `session.append(...)`；若抽取超时，降级路径会写一条 `memory/degraded`。
它持有自己的 `Session` 对象，**与 ADR-0029 D4 的五道守卫无关**——守卫
「在途 run」读的是 `RunManager.is_busy()`，而写回任务不是 run。

于是时序是：`delete_session` 删掉目录 → 写回任务醒来 → `append_event` 见目录不存在，
**建目录 + 写一行** → 会话"复活"。ADR-0029 D1 承诺的"删了就删了"被静默撤销。

### 3. 为什么不能靠现有的两道锁

`delete_session` 原先只做 `rmtree` + 清进程内 seq 缓存；`append_event` 原先在
**取锁之前**就 `mkdir`。迟到写者可以在 `rmtree` 与"登记"之间挤进来：它重建目录、
删除方再把那个新目录删掉——文件没了，但进程内的 seq 缓存已被它改写，且"谁赢"
取决于线程调度。

---

## Decision

### D1：`append_event` 对本进程硬删过的 id **一律拒写**（`SessionNotFound`）

`JsonlSessionStore` 维护一个**进程内** `_deleted_ids` 集合；`delete_session` 登记，
`append_event` 在临界区内先查它。

抛异常而不是静默丢：**拒写是事实**，写者本就带着自己的降级兜底
（`MemoryWriteback` 的 `try/except` 会把 `SessionNotFound` 记成一次降级），
静默丢反而让写者以为事实已落盘。

### D2：删除与追加**共用一把会话写锁**

`delete_session` 的整个删除 + 登记都放进 `_lock_for(session_id)` 的临界区；
`append_event` 的"查已删集合 + 建目录 + 读写 seq"也在同一把锁内。两边的顺序恒为
**会话写锁 → `_state_guard`**（`_lock_for` 内部只短暂持 `_state_guard` 即返回，
不存在反向嵌套），故无死锁。

### D3：集合**不清除**，进程重启即归零——这不是 ADR-0029 D1 反对的"墓碑"

ADR-0029 D1 逐字写着「没有回收站、没有 undo、**没有 tombstone**」，其 Alternatives
明确否决「保留墓碑行」，理由是墓碑会引入一个「**已删但还在**」的半状态、每个读者
都要处理它（不变量 #22 警惕的第二真相）。

本集合与那个概念**不同类**，因此**不触及** D1：

| | ADR-0029 D1 反对的墓碑 | 本 ADR 的 `_deleted_ids` |
| --- | --- | --- |
| 落盘 | 持久行 | **进程内 set，重启即空** |
| 可恢复 | 是（读者可据此恢复） | **否**（只用于拒写） |
| 半状态 | 有（"已删但还在"） | **无**（删完就没了，集合只是护栏） |

不清除的理由：生产环境的会话 id 由 `uuid4` 生成（`service.py:478`、`cli.py:198`；
`Session.start` 的显式 id 只出现在测试与 fork 的可选参数），**同一 id 再次出现只可能
是迟到写者**。进程重启后归零，而那时也不存在在途的写回任务。

### D4：仅覆盖**同一进程内**——与 store 既有的边界口径一致

本层没有文件锁（见 `store.py::__init__` 的边界说明），跨进程的迟到写者不在本护栏
覆盖范围内。这与 `JsonlSessionStore` 对 seq 单调性的既有承诺范围相同，不是新增的
退缩。

---

## Consequences

- 进程内对同一 id 的"删除后重建"被**拒绝**——包括非恶意的复用（代价可接受，见 D3）。
- `_deleted_ids` 随进程内被删会话数增长；上界是"本进程删过的会话数"，量级与
  `_last_seq` / `_seq_locks` 同阶，不额外引入清理机制（§9.2 Simplicity First）。
- 跨进程迟到写者**仍会**复活会话（D4）；这是记录在案的已知边界。

## Non-Goals

- **持久化墓碑 / 回收站 / undo / 恢复入口**（ADR-0029 D1 明确反对的东西；本 ADR 不引入）。
- 跨进程护栏（需文件锁，另开票，且要与 seq 单调性的跨进程问题一起解决）。
- 级联删除 Memory / Artifact（ADR-0029 D6 的既有非目标）。
- 在 `MemoryWriteback` 里单独加过滤：其它旁路写者也要各自加一遍，而
  `append_event` 是**唯一的日志落笔点**，护栏放这里天然收口。

## Alternatives considered

| 方案 | 为什么没选 |
| --- | --- |
| **持久墓碑行** | 正是 ADR-0029 D1 否决的"已删但还在"半状态；审计需求已由 `session_delete` 结构化日志满足（ADR-0029 D7）。 |
| **只在 `MemoryWriteback` 里过滤** | 治标：任何未来的旁路写者都要重新踩一次；`append_event` 是唯一落笔点，护栏放那里才收口。 |
| **在 service 层给五道守卫加一条** | 守卫读 `RunManager` 状态，与 fire-and-forget 写回任务无关；扩守卫要重新定义"在途"，范围更大。 |
| **把 `mkdir` 留在 `append_event` 锁外** | 迟到写者能在 `rmtree` 与登记之间重建目录（Context §3）。 |
| **登记后即清除集合** | 那就等于没有护栏：迟到写者随后一次 `append_event` 就把会话写回来。 |
