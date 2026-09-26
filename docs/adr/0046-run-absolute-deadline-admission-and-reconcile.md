# ADR-0046 — run 绝对 deadline：接纳边界、到点收口与对账耦合

- **Status**: Accepted（实现随 T7/#315 落地）
- **Date**: 2026-09-26
- **Deciders**: 用户（#305 的产品与架构裁决，2026-09-23 冻结）+ 本 Agent（票面实现口径）
- **Related**:
  - 父 PRD **#305**、本票 **#315**（T7/12：「Honor an absolute run deadline」）、
    上游 **#312**（T4：非终态 `run/paused` + 同 run CAS 恢复）、
    **#313**（T5：四维 run ceiling）、**#314**（T6：per-tool 配额，与本票共用"准入前被拒"这一族语义）
  - **ADR-0044 D4** —— 本 ADR 是它在 **deadline 这一维**的展开与实现口径（原文冻结：
    「`deadline_at` 是 RFC 3339 UTC 绝对截止；检查点在每次接纳新工作之前：Provider 请求、
    ToolCall、子 Agent」；「到点后不启动任何新工作；已在途的按其既有 ToolExecutor timeout/cancel
    与 Operation Ledger 语义收尾」；「无法证明 ⇒ `NEED_RECONCILE`，**不得**落一个暗示可安全续跑的
    暂停，并在 reconcile 解除前拒绝恢复」；「显式 cancel 保持既有立即语义」）
  - **ADR-0039**（ToolExecutor 独占**每次尝试**的绝对 deadline）——**另一个层**：0039 管
    "这一次 `tool.execute` 最多跑多久"（`tooling/deadline.py` 的 contextvar），本 ADR 管
    "整个 run 还能不能**开始**新工作"。两者同时生效、互不替代（`02 §5.1` 三层控制）
  - **ADR-0004**（Operation Ledger 与 Checkpoint 分层）、**ADR-0045**（工具调用计数与 per-tool 配额）
  - **ADR-0015**（Phase 13 委派）与 **ADR-0044 D7**（委派树共享预算的唯一 owner 是 #287 的 tree
    context）——本 ADR 只**准入面**管到委派（`delegate` 是一次 ToolCall），树内记账与 deadline
    的传播不在这里（§6.1 第 2 条）
  - 受本 ADR 约束的正式规格节：`04_TOOL_RUNTIME.md` §9.1、`02_AGENT_RUNTIME.md` §5.1/§5.2、
    `03_SESSION_EVENT_MODEL.md` §3.4/§5、`07_STORAGE_PERSISTENCE_RECOVERY.md` §4/§6/§7/§8/§9、
    `11_STREAMING_API_WEB_UI.md` §6.1
  - 代码落点：`agent/run_budget.py`、`agent/runtime.py`、`tooling/executor.py`、`tooling/result.py`、
    `storage/operation.py`、`recovery/coordinator.py`、`session/service.py`、`web/app.py`、`cli.py`、
    `web/src/lib/runBudget.ts`、`web/src/components/PausedPanel.tsx`
- **Refines**: ADR-0044 D4
- **Supersedes**: 无

---

## 1. Context

### 1.1 票面要解决的问题

`04 §9.1` 与 `ADR-0044 D4` 冻结了"run 有一个绝对截止时刻"这件事的语义，但没有实现：

1. **deadline 是绝对时刻，不是第五个 ceiling。** 判据是"现在与截止时刻的先后"，不是
   "consumed 与 ceiling 比大小"——所以它没有 consumed 读数，也不能被"抬高"（只能换一个新的未来时刻）。
2. **接纳边界。** `deadline_at` 过后 MUST NOT 再接纳新的 Provider 请求 / ToolCall / 子 Agent；
   **已在途**的按其**既有** timeout / cancel / Ledger 语义收尾，**不是**被杀掉。
3. **对账耦合。** 到点收尾时若某个 mutating 操作"无法证明已完成或未开始" ⇒ `NEED_RECONCILE`，
   且**不得**落一个暗示可安全续跑的暂停；reconcile 解除前**拒绝恢复**；绝不自动重跑
   （`03 §5` 对账优先于恢复；不变量 #14）。
4. **与 cancel 可区分。** 显式取消保持既有立即语义，MUST NOT 被改写成 `paused`。

票面同时钉死的否定式约束：**不许**造第二个执行器 / 重试环 / 权限路径；**不许**把"到点"实现成
"打断在途调用"——那恰好会制造出无法证明的副作用，与第 3 条自相矛盾。

### 1.2 现状实测（本 ADR 写作时的读数）

| 事实 | 证据 |
| --- | --- |
| 非终态暂停 + 同 run CAS 恢复已在位 | `#312` 落 `run/paused` / `run/resumed`（`agent/run_budget.py`、`session/service.py`） |
| 四维 run ceiling 与 per-tool 配额已在位 | `#313` / `#314`；`RunLimits` 已是"可扩展的多维 + 动态维"形状 |
| Ledger 状态机与两步链已在位 | `07 §4`；`storage/sqlite.py` 的迁移表（`RUNNING → UNKNOWN → NEED_RECONCILE`） |
| reconcile 裁决链已在位但**只有悬空调用**看得见 | `recovery/coordinator.py`：旧收集面只查 `detect_dangling` 的 tool_call_id |
| deadline 此前只声明、不受理 | `web/app.py::RunBudgetRequest._reject_unimplemented_dimensions`（`deadline_at` 非空 ⇒ 422） |
| 每次尝试的绝对 deadline 已有唯一 owner | ADR-0039 / `tooling/deadline.py`、`ToolExecutor._execute_with_retry` |
| 子 run 不继承父的 launch 预算 | `multiagent/provider.py::_run_child` 调 `child_runtime.run(...)`，**不**传 `LaunchRunBudget`（§6.1 残余） |

### 1.3 本 ADR 的范围

**在**：deadline 的语义与命名；三处准入面与唯一决定点；被拒调用的形状（配额 / Ledger / 事件）；
到点后的收口顺序；"副作用未证"的 durable 载体与三类判据；reconcile 优先于恢复在**两个入口**
（resume / budget 投影）的落点；恢复契约（新未来时刻 + CAS）；422 / 409 分界与投影键名；
客户端同源（CLI / Web）与**模型可见**的拒绝文案；Live Gate 的场景与判定口径。

**不在**：stuck（`#317`）；委派树共享预算 / 原子记账（`#318`）；reconcile **裁决的提交面**
（服务端尚无入口，作为已知边界登记在 §6.1）；per-attempt deadline（ADR-0039 的责任域，本 ADR 不动）；
"deadline 到点后如何选新时刻"的策略（不做策略：客户端给绝对时刻）。

---

## 2. Decision

### D1 — deadline 是**绝对时刻**，命名 `run.deadline_at`，`reason=deadline`

- wire 形状：RFC 3339 **UTC** 文本（`Z` 或带偏移，解析后一律归一化成 UTC）或 `null`
  （= 本 run 不设 deadline）。**不收**"剩余秒数"：倒计时是客户端从绝对时刻自己算的派生量，
  存进事件流会让"重启后能否重建同一个 ceiling"依赖落盘那一刻的时钟（`03 §3.4`）。
- 维度名 `run.deadline_at`（`TRIGGER_RUN_DEADLINE`）**故意不在** `RUN_DIMENSIONS` /
  `TRIGGER_ORDER` 里：那两张表是"consumed 与 ceiling 比大小"的维度清单，而 deadline 比的是
  两个**时刻**。硬塞进去会让投影与前端各自编出一个 `unavailable / unlimited` 的读数行。
- `run/paused.data.reason = "deadline"`（`REASON_DEADLINE`，`03 §3.4` 三值里的第二个；
  `stuck` 属 `#317`）。

### D2 — 准入边界：**唯一决定点**在 loop 顶部，三处准入面各自把闸

deadline 的判定函数只有一处（`run_budget.pause_trigger`，deadline **先判**且判据不同：
`now >= deadline_at` 即到点——到点那一刻就停，不留"正好等于还有一次机会"的缝）；三处准入面
各自调用它：

| 准入面 | 闸门位置 | 到点后的行为 |
| --- | --- | --- |
| Provider 请求（primary / fallback） | loop 顶部 `AgentRuntime._pause_trigger`（`_drive` 每轮准入前） | 不进这一轮；直接走暂停收口 |
| closeout 那次 Provider 请求 | `closeout_capacity(now=…)` | 容量恒为 `false` ⇒ **确定性** continuation（不发现场模型请求） |
| ToolCall | `ToolExecutor.execute` **阶段 2.35** | 该调用被拒：`ErrorCode.DEADLINE_EXCEEDED`、`retryable=False` |
| 子 Agent | 委派是一次 `delegate` **ToolCall**（`DelegateTool`），走同一条 executor 路径 | 同一道闸门：到点后不再签发新 child |

`ToolExecutor` 里那道闸门的位置是**契约**，不是口味：

- 在 **approval 之前**：一个注定不被接纳的调用不该先弹一次人工审批（审批可能等一个回合，
  而 deadline 已经过去了）；
- 在 per-tool 配额的 `take()` **之前**、**Ledger 建行之前**：所以被拒的调用**不占配额**
  （没有配对的 `release()` 义务）、**没有 Ledger 行**（"从没开始过"不需要对账）、
  `budget_delta` 记显式 0（与 `#314` 的四类早退同一族）。

**与 ADR-0039 的关系**：这里的 `run_deadline` 是一个**准入判据**，不产生第二个 per-attempt
deadline——`tool.execute` 的超时边界仍由 executor 自己建立（`t0 + tool.timeout_seconds`
进 contextvar），到点判定不影响它。规格里"ToolExecutor 是绝对 deadline 的唯一责任域"落在
这一层。

### D3 — 到点后的稳定边界收口：顺序固定，恰好一条 `run/paused`

`AgentRuntime._terminal_paused`（`reason=deadline` 时）依次做四件事：

1. **对账闸门**（见 D4）：把本 run 里"副作用未证"的 Operation 提升到 `NEED_RECONCILE` 并落
   `operation/reconcile-required`。位置在 continuation **之前**——"存在未结清的副作用"是
   continuation 必须如实写出的事实（它要进 `blockers`、并把 `next_safe_action` 从"抬高 ceiling
   后恢复"换成"先 reconcile"）。
2. **closeout**：有容量 ⇒ 一次**有界**模型调用产出 continuation；到点后容量恒为没有 ⇒
   **确定性** continuation（只用已持久化事实组装，不伪造进展 / 成功 / 工具结果）。
3. 落**恰好一条** `run/paused`（durable、**非终态**）并镜像给流消费者。
4. 本次执行到此收口：**不**落 `run/completed` / `run/failed`，**不**做记忆形成，
   **不**新增 Checkpoint 边界。

镜像顺序 = 落盘顺序（`reconcile-required` → closeout 的 `model/request` → `run/paused`）：
"流帧必须是落盘日志的前缀"是 golden 钉住的不变量，而顺序本身也有语义——客户端先读到
"某个操作进入 NEED_RECONCILE"，再读到"本次执行在 deadline 上暂停"，与 `03 §5` 同向。

### D4 — "副作用未证"的 durable 载体：`OperationState.UNKNOWN` + `reconcile_meta` 标记

判据只在一处（`storage.needs_reconcile`）：

- 状态 ∈ {`RUNNING`, `UNKNOWN`, `NEED_RECONCILE`}；**`PENDING` 不算**（`07 §6` 明文：
  能证明尚未启动 ⇒ 可按策略重执行，不是未证）；
- **或** 行上带"副作用未证"标记（`reconcile_meta` 里的
  `{"unproven_side_effect": true, …}`，JSON 文本）——这条覆盖"尝试本身已结束、但世界状态未知"
  的情形。

写入者是 `ToolExecutor._settle_state`：**MUTATING + TIMEOUT** ⇒ `UNKNOWN` + 标记。
理由：`_ToolFailure.from_timeout` 已经写明"命令可能仍在运行，或已部分生效"，即这次尝试
**给不出**"没落地"的证据；落 `FAILED` 等于替工具断言它没生效（`07 §7` 禁止）。
**为什么不是直接 `NEED_RECONCILE`**：状态机只允许两步链，"要不要现在就对账"是**稳定边界**
（deadline）或崩溃恢复的决定，不是执行域能替上层拍的板。

裁决落地时用裁决内容**覆盖**这一格（`_commit_reconcile`）——覆盖即"疑问已解除"，
所以不需要第二个"清除"标记。读法**宽容**：`reconcile_meta` 缺失 / 非 JSON / 形状不对 ⇒ `False`
（腐烂的历史数据只让这一项失效，不让整个判定抛错）。

**三个读者读同一份落盘事实**（谁都不另存内存标记）：`AgentRuntime._raise_deadline_reconcile`
（deadline 稳定边界）、`SessionService._unreconciled_tool_calls`（恢复闸门 + 投影）、
`RecoveryCoordinator.recover` 的裁决收集面。第三处本票同时修了一个**收集面缺口**：
非悬空但未证的行（tool/call 与 tool/result 都齐、只是那条 result 说"状态未知"）
旧判据永远看不到，而 resume 闸门读的正是这一份裁决要求。配套地，裁决回填
**只补缺的那一半** `tool/result`——同一 `tool_call_id` 两条结果会破坏 `derive_messages`
依赖的 1:1 配对（`07 §8`）。

### D5 — reconcile 优先于恢复：两个入口同一把尺子

`03 §5` 的"对账优先于恢复"落成两处，**判据同一个函数**：

- **恢复入口**（`SessionService.resume`）：开工前置条件从"事件面悬空"扩成
  `detect_dangling(existing) or detect_unterminated_runs(existing) or await
  self._has_unreconciled_operations(session_id)` ⇒ 先 `recover()`。生产装配的
  `RecoveryCoordinator` **不带** `ReconcileCallback` ⇒ 安全拒绝（`RecoveryConflict` → **409**
  「存在未 reconcile 的副作用」），零副作用。这正是"不盲目重跑高风险副作用"要的结果。
- **投影**（`project_budget(..., reconcile_pending=…)`）：欠账非空且 run **未终态** ⇒
  `state` 报 `needs_reconcile`（`03 §5` 词表原词）并附 `reconcile` 子对象。这是**覆盖**而非替换：
  `reason` / `trigger_dimension` / `continuation` 照旧在（"为什么停"与"停了之后欠了什么"
  是两件事）。覆盖的理由是客户端只看 `state` 就会把这条 run 当普通暂停，给出一个点了必然 409
  的恢复入口。run **已终态** ⇒ `state` 保持终态名（`completed` 是既成事实，不能因为账本上另有
  一笔欠账就报成没跑完），`reconcile` 子对象照旧出现。

投影的 `reconcile_pending` 由**调用方**查账本得出（`SessionService.budget_projection` 是唯一
读 Ledger 的一处）——`project_budget` 是纯派生函数，不碰存储（与 `local_fuse` 同一分工）。

### D6 — 恢复契约：**点名一个新的未来时刻**，CAS，同一 `run_id`

- deadline 暂停的恢复依据就是**新时刻**：`resume_headroom_ok` 里这一维的判据是**严格在未来**
  （`limits.deadline_at > now`）。没点名 ⇒ 沿用暂停快照里那个（已过去）⇒ 同样被拒。
  于是"恢复一个到点的 run 必须换一个新的未来时刻"是**流程的必然结果**，不是额外规则。
- 走 T4 已有的同 run CAS 契约：`expected_version` + 被暂停的 `run_id`；成功后服务层在任何工作
  之前落 `run/resumed`（含 `from_pause_seq` / `previous_budget_version` / `budget_version`），
  version 1 → 2；**不**新建 run_id、**不**重放历史、**不**重置 counter。
- 复用 `budget_increase` 这条恢复依据路径（deadline 的"变更"就是换了一个时刻），
  不为它新造第三种 resume basis。

### D7 — 422 / 409 分界与投影键名

| 输入 | 结论 | 落点 |
| --- | --- | --- |
| 朴素时间（无时区）/ 空串 / 布尔 / 非字符串 / 不可解析 | **422**（形状） | `parse_deadline_at` |
| 开工时给一个**已过去**的时刻 | **合法**，等于立刻到点（即时暂停） | 没有形状问题 |
| 恢复时给一个**已过去**（含"没点名 ⇒ 沿用旧时刻"） | **409**（状态） | `validate_resume` / `resume_headroom_ok` |
| 版本过期 / run_id 不是被暂停的那个 / ceiling 没真提高 | **409** | 同上 |

"已过去"不是形状错误——这一点是本票明确的分界（`11 §6.1`：422 = 形状，409 = 状态），
它的后果是**开工路径与恢复路径对同一个值给出不同结论**，这是规格要的，不是不一致。

投影键名冻结：`limits.run.deadline_at`（RFC 3339 UTC 文本或 `null`）与
`reconcile` 子对象 `{"state": "needs_reconcile", "tool_call_ids": [...]}`（`11 §6.1`）。
**空列表不落键**（同本函数对"缺席 vs 空值"的一贯口径：没有欠账与欠账为空不是同一件事）。

### D8 — 客户端同源，且**模型可见**的拒绝文案是恢复行为的输入

- **CLI**：`--run-deadline`（`run` 与 `resume` 两侧）；恢复提示按维度给**真实的 argv 形状**
  （`--run-deadline 2026-09-26T04:30:00Z` 而不是 `--run-deadline N`——给 `N` 会让人抄一条
  必然被形状校验拒掉的命令），规则行写"给的是**新的未来时刻**，沿用已到点的时刻会被拒（409）"，
  不写"必须高于 consumed"（deadline 没有 consumed）。
- **Web**：`runBudget.ts` 的 `DEADLINE_DIMENSION` / `DEADLINE_EXAMPLE`（与 CLI 的
  `_DEADLINE_PLACEHOLDER` 逐字一致）/ `deadlineDraftError`（**只为省一次必然拒绝的往返**，
  不是规则来源；它自己判时区，因为 `Date.parse` 会把朴素时间当本地时间收下，
  于是前端放行、后端 422——一次必然失败且提示词还是错的往返）；
  `PausedPanel` 的标题报**时刻本身**与到点后的准入边界，恢复输入框换成 RFC 3339 形状。
- **模型可见文案**（本票的实测教训）：`DEADLINE_EXCEEDED` 的拒绝消息是模型在暂停前读到的
  **最后**一段文字，它直接决定模型恢复后的意愿。旧文案只写"不要重复提交本调用"，
  真实运行里模型的原话是 *"The error says deadline not solved by retry. I should report
  status briefly."* ——恢复后那一轮零工具调用、run 直接 `run/completed`。现在文案明确写出
  "到点暂停**不是任务结束**：本 run 以新的未来时刻恢复后，从暂停前的进度继续原任务"。
  这条文案与 `TASK` 的任务描述同源（两侧都提），由 `tests/tooling/test_deadline_admission.py`
  守。

### D9 — Live Gate 口径：两种**安全**结局都接受，各自必须完整成立

真实模型在一次 run 有两个合法走向：**(a)** 恢复后再次到点 ⇒ 第二条 `run/paused(reason=deadline)`
（version 2，非终态）；**(b)** 恢复后模型自己收尾 ⇒ `run/completed`。
第三种（`run/failed` / `run/interrupted`）是**不安全**的，判红。

判据本身必须是"**准入**被重新打开"而不是"模型必须再调工具"：`resumed_leg_admitted_new_work`
= 恢复后至少一次 Provider 请求或 ToolCall。理由是"模型有权选择收尾"是产品事实（它读得到
自己的上下文），把它写成红会让判据在模型理性行为上误伤；而"恢复请求真的重新打开了准入"
才是产品契约。实测：3/3 的运行里 (a) 与 (b) **各自自然出现过**（attempt-1/3 是 (a)、
attempt-2 是 (b)），这正是"两种都接受"这条设计被验证的方式。

---

## 3. 不变量与边界（本 ADR 之后仍然成立）

1. **Tool 只有一条执行路径**（不变量 #7）：deadline 闸门长在 `ToolExecutor.execute` 里，
   MCP / 内建 / 委派工具一律经它；没有第二条"绕开 deadline 的快速通道"。
2. **ADR-0039 的责任域不变**：per-attempt 绝对 deadline 仍由 ToolExecutor 独占；
   本 ADR 的 run deadline 是**准入判据**，不进 `tool_execution_deadline_var`。
3. **UNKNOWN 高风险 Tool 不盲重跑**（不变量 #14）：到点收尾不产生任何自动重跑；
   未证 ⇒ `NEED_RECONCILE` ⇒ 恢复被拒。
4. **Event ≠ Diagnostic Log**（不变量 #4）：`run/paused` / `operation/reconcile-required` 都是
   typed durable 事实，不是日志行。
5. **`paused` 是非终态**（`03 §3.4`）：到点不落 `run/completed` / `run/failed`；
   显式 cancel 仍走 `run/failed(reason=cancelled)` 的既有面，两者可区分。
6. **Checkpoint ≠ 副作用恢复**（不变量 #12）：到点收口不新增 Checkpoint 边界。
7. **Optional 能力故障不拖垮 Core**（不变量 #21）：closeout 模型调用失败 ⇒ 回落确定性
   continuation，暂停照常成立。

---

## 4. 后果

**正面**

- "长任务到点收口"从一个语义描述变成了可观测、可恢复、可对账的流程：到点在事件流里是一条
  `run/paused`，欠账在 Ledger 与投影里各有一处读数，恢复入口对"还能不能恢复"给的是 409 而不是
  一个点进去必失败的按钮。
- 与 `#314` 共用"准入前被拒"这一族的形状（`error_code` + `budget_delta` 记 0 + 不占配额 + 无 Ledger 行），
  客户端不需要为 deadline 学第二套拒绝语义。
- 未证副作用的标记让"这次尝试给不出结论"第一次成为 durable 事实，而不只是执行域的一句日志。

**代价 / 需要接受的**

- 投影接口多了一个**覆盖** `state` 的规则（`needs_reconcile` 压过 `paused`）：读投影的人必须
  知道 `state` 是"当前该不该让你恢复"的刹车灯，而不是"最后一条生命周期事件的名字"。
- deadline 到点后的收口是**确定性**的（不发 closeout 模型请求）⇒ continuation 文本质量低于
  预算暂停那条路径。这是"到点后不启动任何新工作"的直接代价，不是实现偷懒。
- 恢复必须点名一个新的未来时刻 ⇒ 客户端要自己决定"再给多久"（本 ADR 刻意不做策略）。

---

## 5. 证据（实现与验证）

### 5.1 用例（本票新增 / 扩写；数字为 `--collect-only` 的实测条数）

| 用例文件 | 条数 | 钉住的事实 |
| --- | ---: | --- |
| `tests/tooling/test_deadline_admission.py` | 8 | 到点在 approval / 配额 `take()` / Ledger **之前**被拒；不烧 per-tool 配额；未来 deadline 不改变任何行为；批次层同一条闸门；`_settle_state` 对 MUTATING+超时打"未证"标记、对确定性失败与只读超时不打 |
| `tests/agent/test_run_budget.py` | 79 | `parse_deadline_at` 的形状（归一化 / 朴素时间 / 畸形值）；deadline 先判且映射到自己的 reason；**同一瞬**不停（`now >= deadline_at` 才停）；到点后 `closeout_capacity` 恒 false；恢复要求严格未来；未点名 ⇒ 沿用旧时刻被拒；continuation 点名新时刻与边界；`blocked_by` 把 reconcile 顶到预算动作之前；投影的 `reconcile` 键（非空落键 / 空不落键） |
| `tests/agent/test_run_pause_resume.py` | 17 | 稳定边界收口顺序、版本 1→2、快照含 closeout、非终态 |
| `tests/recovery/test_deadline_restart.py` | 2 | **kill / restart**：已知结果的 mutating 不欠账也不重跑；未知结果的 mutating 拒绝恢复且**永不**重跑（真子进程） |
| `tests/recovery/test_reconcile.py` | 26 | 裁决链（含"非悬空但未证"的收集面与"只补缺的那一半 `tool/result`"） |
| `tests/web/test_run_pause_resume_api.py` | 15 | HTTP 面：422（形状）/ 409（状态）分界、`reconcile` 投影、resume 契约 |
| `tests/test_cli_run_pause_resume.py` | 25 | CLI 两条命令 + 暂停摘要（deadline 行、真实 argv 形状的恢复提示、过去时刻在 `run` 合法 / 在 `resume` 被拒） |
| `tests/live_gate/test_deadline_scenario.py` | 41 | 场景的判定本身（含三条**判红**用例：读错投影键、恢复后零准入、恢复后的不安全收尾） |
| `tests/agent/test_event_sequence_golden.py` | 243（本票只改 4 行） | golden：deadline 只新增事实，既有会话的序列逐字不变 |

前端：`web/src/lib/runBudget.test.ts`（+97 行）、`web/src/components/PausedPanel.test.tsx`（+60 行）。

### 5.2 Live Gate（票面 AC 的强制证据）

场景 `run-deadline-boundary`，真实 Provider + 真实 mutating 工具（生产默认装配、local sandbox）：

- **PASS 3/3**，运行树 `dd23306441721b1347fa8ee167d8c8393f2eda99`（sha `3cd8253f…`），
  证据 `docs/live_gate/20260926T100829-3cd8253f0625-run-deadline-boundary/`，
  `worktree_proof.tracked_matches_head: true`、无 seam、无注入失败；
  独立复核 `scripts/live_gate.py validate <evidence.json>` → 声明 PASS 且证据自洽，24 条断言 0 FAIL。
- 三次 attempt 的收尾形状：`paused@v2 + 5 次 tool/call` / `completed + 0` / `paused@v2 + 4 次`——
  **两种合法结局在同一次 3/3 内各出现过**（D9 的实测依据）。
- 被拒的两次运行**同样入库**（不删）：`9496226e7518`（投影键读错 ⇒ TypeError 3/3）、
  `1a88b799bed6`（恢复面判据写成"必须再调工具" ⇒ 2/3）。

### 5.3 门禁读数

- Gate-0 六条机械车道：`docs/gate/<sha>.json`（本票集成时那次裸全量运行的落盘读数）。
- 审查覆盖闸门：`scripts/check_review_coverage.py` 退出 0。

---

## 6. 未决与后续

### 6.1 已知边界（本票如实登记，未修或刻意不改）

1. **reconcile 裁决没有提交面。** `ReconcileCallback` 只在进程内可注入，
   生产装配的 `SessionService.recover()` **不传**它 ⇒ `POST /recover` 遇到第一行
   RUNNING/UNKNOWN 就 409。后果：deadline 暂停若同时升了 `NEED_RECONCILE`，
   对 HTTP 客户端是**硬停**——`resume` 409、`recover` 也 409，而"我查过了：它确实落地了 /
   确实没落地"这句话**没有地方可以说**。拒绝本身是规格要的（`03 §5`、不变量 #14），
   缺的是裁决入口；本票不新增（属对账链的后续票）。
2. **已签发的 child 不继承父的 deadline。** `multiagent/provider.py::_run_child` 不向 child
   传 launch 预算，child 在自己的作用域里跑（`LaunchRunBudget()` 默认无 deadline）
   ⇒ 父到点后，一个**在途** child 仍会发出它自己的 Provider 请求。按 ADR-0044 D4 的字面，
   它是"已在途的操作"，按自己的 timeout（`delegate` 工具 1800s）收尾，父在这一段被阻塞、
   随后在下一个稳定边界暂停——语义自洽；但"委派树共享 deadline"**没有实现**，
   归 `#318`（ADR-0044 D7 指认的 tree context 责任域）。
3. **deadline 路径上没有"模型可见的恢复标记"。** `run/resumed` 是 durable 但**不进消息投影**
   ——恢复后模型看不到"你被恢复了"。产品侧的补偿是 D8 那段拒绝文案（"到点不是任务结束"）；
   残余风险是模型若在暂停前没读到该文案（例如到点发生在两轮之间、没有工具被拒），
   恢复后的收尾意愿只能靠 `TASK` 里的那一句。这条在 Live Gate 场景的 docstring 里带实测证据，
   不在这里重复叙述。
4. **`max_tool_calls`（工具调用**总数**的 ceiling）仍声明不受理**（非空 ⇒ 422）：
   规格只冻结"按名字的显式配额"，总数只观测不设限（ADR-0045 §6.1 同一条登记）。
