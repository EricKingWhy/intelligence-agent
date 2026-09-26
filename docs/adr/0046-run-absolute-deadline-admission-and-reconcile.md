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

deadline 的判定只有一种语义（`now >= deadline_at` 即到点——到点那一刻就停，不留"正好等于
还有一次机会"的缝），落成一个共用函数 `run_budget.deadline_reached(deadline_at, now=…)`，
agent 侧三处消费者共用它（`pause_trigger` / `closeout_capacity` / `resume_headroom_ok`）：

| 准入面 | 闸门位置 | 到点后的行为 |
| --- | --- | --- |
| Provider 请求（primary / fallback） | loop 顶部 `AgentRuntime._pause_trigger`（`_drive` 每轮准入前） | 不进这一轮；直接走暂停收口 |
| closeout 那次 Provider 请求 | `closeout_capacity(now=…)` | 容量恒为 `false` ⇒ **确定性** continuation（不发现场模型请求） |
| ToolCall | `ToolExecutor.execute` **阶段 2.35** | 该调用被拒：`ErrorCode.DEADLINE_EXCEEDED`、`retryable=False` |
| 子 Agent | 委派是一次 `delegate` **ToolCall**（`DelegateTool`），走同一条 executor 路径 | 同一道闸门：到点后不再签发新 child |

**跨层例外（如实登记）**：`ToolExecutor` 那道闸门**不用** `deadline_reached`，而是拿裸时刻
（`run_deadline`）做同一个比较——`tooling/**` 不依赖 `agent/**`（工具运行时不该认识 run 预算，
分层实测确认这条依赖方向不存在）。所以"只有一个判据函数"这句话的准确边界是：**agent 侧
唯一**，执行域那处是同一语义的第二个实现，两边的一致性由各自用例维持（`#315` 的两组
用例：`tests/tooling/test_deadline_admission.py` 与 `tests/agent/test_run_pause_resume.py`）。

`ToolExecutor` 里那道闸门的位置是**契约**，不是口味：

- 在 **approval 之前**：一个注定不被接纳的调用不该先弹一次人工审批（审批可能等一个回合，
  而 deadline 已经过去了）；判据是"审批回调有没有被调用"，不是最终错误码——错误码只能
  说明报了哪个错，说明不了审批有没有先跑一轮；
- 在 per-tool 配额的 `take()` **之前**、**Ledger 建行之前**：所以被拒的调用**不占配额**
  （没有配对的 `release()` 义务）、**没有 Ledger 行**（"从没开始过"不需要对账）、
  `budget_delta` 记显式 0（与 `#314` 的四类早退同一族）。

上述顺序**逐条**有用例钉住（`tests/tooling/test_deadline_admission.py`：到点拒收零副作用 /
不占配额 / 审批未被询问，外加"未到点时审批照常发生"的反例）。

**与 ADR-0039 的关系**：这里的 `run_deadline` 是一个**准入判据**，不产生第二个 per-attempt
deadline——`tool.execute` 的超时边界仍由 executor 自己建立（`t0 + tool.timeout_seconds`
进 contextvar），到点判定不影响它。规格里"ToolExecutor 是绝对 deadline 的唯一责任域"落在
这一层。

### D3 — 到点后的稳定边界收口：顺序固定，恰好一条 `run/paused`

`AgentRuntime._terminal_paused`（**任何原因的暂停**都走这一段）依次做四件事：

1. **对账闸门**（见 D4）：把本 run 里"副作用未证"的 Operation 提升到 `NEED_RECONCILE` 并落
   `operation/reconcile-required`。位置在 continuation **之前**——"存在未结清的副作用"是
   continuation 必须如实写出的事实（它要进 `blockers`、并把 `next_safe_action` 从"抬高 ceiling
   后恢复"换成"先 reconcile"）。
   **闸门不以暂停原因为条件**（⚠ 这条是两轴审查那个 P1 的整改点）：恢复闸门是 **session 级**的，
   它读 Ledger 上有没有未结清的行，不读 `reason`。所以"预算暂停 + 一条 MUTATING 超时留下的
   未证行"同样必须被点名——否则那次暂停会落成暗示可安全续跑的形态（载荷里写着"升高 ceiling
   后恢复"），而恢复必被 409 挡死，正是 `03 §5` / ADR-0044 D4 禁止的。判据只看
   `storage.needs_reconcile`，不看 reason。
2. **closeout**：有容量 ⇒ 一次**有界**模型调用产出 continuation；到点后容量恒为没有 ⇒
   **确定性** continuation（只用已持久化事实组装，不伪造进展 / 成功 / 工具结果）。
   `blocked_by` 的改写对**两个来源都生效**（`run_budget.apply_blocked_by`，⚠ 同一条 P1 的
   后半）：模型写的那份 continuation 也要被追加 blockers 并改写 `next_safe_action`。deadline
   暂停没有容量 ⇒ 恒走确定性那一支，只看 deadline 用例看不出这处缺口；预算暂停走的是
   `CLOSEOUT_MODEL` 那一支。
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
**为什么不是直接 `NEED_RECONCILE`**：状态机只允许两步链（`RUNNING → UNKNOWN →
NEED_RECONCILE`），"要不要现在就对账"是**稳定边界**（暂停收口）或崩溃恢复的决定，不是
执行域能替上层拍的板。

裁决落地时用裁决内容**覆盖**这一格（`_commit_reconcile`）——覆盖即"疑问已解除"，
所以不需要第二个"清除"标记。读法**宽容**：`reconcile_meta` 缺失 / 非 JSON / 形状不对 ⇒ `False`
（腐烂的历史数据只让这一项失效，不让整个判定抛错）。

**三个读者读同一份落盘事实**（谁都不另存内存标记）：`AgentRuntime._raise_reconcile_required`
（暂停收口，**与 reason 无关**）、`SessionService._unreconciled_tool_calls`（恢复闸门 + 投影）、
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
- **投影**（`project_budget(..., reconcile_pending=…)`）：欠账非空且 run **处于暂停** ⇒
  `state` 报 `needs_reconcile`（`03 §5` 词表原词）并附 `reconcile` 子对象。这是**覆盖**而非替换：
  `reason` / `trigger_dimension` / `continuation` 照旧在（"为什么停"与"停了之后欠了什么"
  是两件事）。覆盖的理由是客户端只看 `state` 就会把这条 run 当普通暂停，给出一个点了必然 409
  的恢复入口。run **在途（active）** ⇒ `state` 仍是 `active`（它确实还在跑——一条未证行不改变
  这件事），欠账由 `reconcile` 子对象表达；run **已终态** ⇒ `state` 保持终态名（`completed`
  是既成事实，不能因为账本上另有一笔欠账就报成没跑完），`reconcile` 子对象照旧出现。
  ⚠ 覆盖条件写成"非终态"曾把在途 run 误报成 `needs_reconcile`（两轴审查的 P3）——"能恢复"
  这件事只对**暂停**成立，所以覆盖也只对暂停成立；补审后判据收紧到 `resumable`
  （暂停**且**非终态），与这句理由逐字对齐（"有暂停记录但已终态"这一形状不可派生，见 §6.1）。

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
| `tests/tooling/test_deadline_admission.py` | 11 | 到点在 approval / 配额 `take()` / Ledger **之前**被拒（含"到点时审批回调根本不被调用"与"未到点时它照常被调用"的正反两条）；**`now == deadline_at` 那一刻就算到点**（把"现在"钉死，带"差一微秒照常接纳"的反例）；不烧 per-tool 配额；未来 deadline 不改变任何行为；批次层同一条闸门；`_settle_state` 对 MUTATING+超时打"未证"标记、对确定性失败与只读超时不打 |
| `tests/agent/test_run_budget.py` | 80 | `parse_deadline_at` 的形状（归一化 / 朴素时间 / 畸形值）；deadline 先判且映射到自己的 reason；**同一瞬**不停（`now >= deadline_at` 才停）；到点后 `closeout_capacity` 恒 false；恢复要求严格未来；未点名 ⇒ 沿用旧时刻被拒；continuation 点名新时刻与边界；`blocked_by` 把 reconcile 顶到预算动作之前；投影的 `reconcile` 键（暂停态覆盖 / 在途态不覆盖 / 空不落键） |
| `tests/agent/test_run_pause_resume.py` | 19 | 稳定边界收口顺序、版本 1→2、快照含 closeout、非终态；deadline 与**预算**两条暂停各自把未证行升到 `NEED_RECONCILE` 并改写 continuation（后者走 `CLOSEOUT_MODEL` 分支） |
| `tests/recovery/test_deadline_restart.py` | 2 | **kill / restart**：已知结果的 mutating 不欠账也不重跑；未知结果的 mutating 拒绝恢复且**永不**重跑（真子进程） |
| `tests/recovery/test_reconcile.py` | 26 | 裁决链（含"非悬空但未证"的收集面与"只补缺的那一半 `tool/result`"） |
| `tests/web/test_run_pause_resume_api.py` | 15 | HTTP 面：422（形状）/ 409（状态）分界、`reconcile` 投影、resume 契约 |
| `tests/test_cli_run_pause_resume.py` | 27 | CLI 两条命令 + 暂停摘要（deadline 行、真实 argv 形状的恢复提示、过去时刻在 `run` 合法 / 在 `resume` 被拒）；**形状认不出不编时刻**与**键缺席不印一片 `unavailable`** 两条由 `c6d9cd6` / `cf1879d` 补（前者同时是"恢复块给的是**新**时刻"那条断言的前提） |
| `tests/live_gate/test_deadline_scenario.py` | 43 | 场景的判定本身（含 25 条 `test_red_*`：读错投影键、空账本不得读成干净、欠账必须**逐条**被点名、恢复后零准入、恢复后的不安全收尾 …） |
| `tests/agent/test_event_sequence_golden.py` | 243（本票只改 4 行） | golden：deadline 只新增事实，既有会话的序列逐字不变 |

前端三处：`web/src/lib/runBudget.test.ts`（+117 行 / 30 条，含"小写 `z` 必须被拒"这一格）、
`web/src/components/PausedPanel.test.tsx`（+64 行 / 17 条）、`web/src/lib/projection.pause.test.ts`
（+108 行 / 21 条；`#315` 那一段钉在**投影**上——既有用例手工构造 `RunPausedInfo`、绕过了投影解析器，
`71f19f8` 抓回的缺陷正落在那个缝里）。

### 5.2 Live Gate（票面 AC 的强制证据）

场景 `run-deadline-boundary`，真实 Provider + 真实 mutating 工具（生产默认装配、local sandbox）。
**最终**证据绑定 sha `cf1879d3` / tree `99a8b465`：

| 证据目录 | 绑定树 | 判定 | 读数 |
| --- | --- | --- | --- |
| `20260926T171722-cf1879d3cf32-run-deadline-boundary`（**最终**） | `99a8b465` | PASS 3/3 | 68.4s / 66.4s / 52.1s，每次 **23/23 断言**；收尾 **paused / paused / completed** |
| `20260926T153033-98d56d6149f8-run-deadline-boundary` | `9c037d91` | PASS 3/3 | 70.6s / 66.7s / 71.2s，每次 23/23 断言；其后 CLI 与前端投影又动过 ⇒ 由本份取代，保留作原始依据 |
| `20260926T100829-3cd8253f0625-run-deadline-boundary` | `dd233064` | PASS 3/3 | 修复批次**之前**的场景口径 ⇒ 保留作原始依据 |
| `20260926T095353-1a88b799bed6-run-deadline-boundary` | `9496226e` 系 | **FAIL 3/3**（每次恰 1 条红：`resumed_leg_did_new_work`） | 恢复面判据原写成"必须再调工具"，收窄成"**接纳**"（`509a229`） |
| `20260926T083245-9496226e7518-run-deadline-boundary` | `9496226e` | **FAIL 3/3**（`TypeError: 'NoneType' object is not iterable`，0 条断言产生） | 场景读错投影键（`0285418`） |

**最终那份的机器读数**：`sha cf1879d3` / `tree 99a8b465`、`worktree.tracked_matches_head=true`、
未跟踪清单只有 `.zcodeignore`；独立复核 `scripts/live_gate.py validate <evidence.json> --require-pass`
⇒ 声明 PASS 且由 attempts **重算一致**、**24 条检查 0 FAIL、exit 0**。三次收尾形状
**paused / paused / completed** ⇒ **D9 的两种安全结局在同一次 3/3 内各出现**（validator 对两次暂停结局给的是
结构性 ⚠️「轨迹里没有该 run 的终态事件」而不是 FAIL：暂停本就是非终态收尾）。每次 attempt 内 **23/23 断言**：
`deadline_pause_snapshot`（`reason=deadline` / `trigger=run.deadline_at` / `closeout=deterministic` /
`resume_requirements=[]`）、`deadline_pause_precedes_any_terminal`、`real_work_admitted_before_the_deadline`
（真实 Provider 请求 3 / 5 / 6 轮 + 真实 BashTool，`chain-steps.txt` 只跑到 7/40、10/40、4/40 ⇒ 结构上跑不完）、
`deadline_outcome_is_safe_or_needs_reconcile`（Arm A 与 Arm B **各自完整成立**）、
`uncertain_mutation_recorded_as_unproven`（生产 BashTool MUTATING 超时 ⇒ `TIMEOUT` + `UNKNOWN` +
`needs_reconcile=True`）、`no_new_admission_after_the_deadline`（`DEADLINE_EXCEEDED` + **账上不留行** +
副作用计数不动）、`unreconciled_debt_blocks_recovery`（生产恢复入口 `RecoveryConflict`、拒绝不顺手改账、
**没有盲重跑**）、`resume_contract_holds`、`durable_replay_matches_live`、`no_fuse_trip` …；
`seams` 空、`secret_scan` 0 命中、`sandbox.deleted=true`（一次性工作区已核实销毁）、
`scope.does_not_cover` 如实列出三条未覆盖面。

被拒的两次运行**同样入库**（不删）——它们的读数正是"判据太窄"与"读错键"这两个真实缺陷的证据。
**证据的取代链如实记**：`dd233064` →（场景口径两处收紧：Arm A 要求账本**非空**、Arm B 的 blockers 判据
`any`→`all`）→ `9c037d91` →（CLI 与投影又变）→ `99a8b465`（最终）；旧 PASS 的结论方向不受影响
（收紧的是"空账本 / 部分点名也会绿"这两条**空泛通道**）。

### 5.3 门禁读数

- Gate-0 六条机械车道：读数落盘 `docs/gate/28995ce96dcf4da87054f1bc605e1023e205a6c3.json` ——
  **6/6 PASS**，墙钟 **41.05s**（diff-check 0.04 / ruff 0.87 / oxlint 0.62 / tsc 33.64 / guards 4.25 /
  coverage 1.62），tip `28995ce` / tree `215e16e2f244`、`tracked_matches_head=true`、未跟踪清单只
  `.zcodeignore`。bare 运行不带 `--since` ⇒ 车道 ① 只查工作树，另跑 `git diff --check f13ed0d..HEAD`
  **exit 0** 补上「已提交未推送」那 17 笔的范围（§14.10 不手抄读数：以上数字全部取自该 json）。
- **集成候选树的复跑**（先回后正把 `origin/main` 合并进来之后；合并内容为 docs 与 `docs/gate/**`，
  与本票改动面只在 `docs/SDD_TICKET_TRACKER.md` 相交、无冲突）：tip `72541ee` / tree `9a834ae55551` 上
  **6/6 PASS**，墙钟 **22.66s**，读数落盘 `docs/gate/72541eed2909c54c1be2e15571be9bde690786ca.json`。
  重车道**未**重跑（合并对 pytest / vitest / e2e 的输入面零改动）⇒ 重车道读数以**代码冻结树 `cf1879d`** 为准。
- 审查覆盖闸门：`scripts/check_review_coverage.py` **exit 0**（本票台账行 327–334；集成前的**前置条件**）。
- 重车道（全量 pytest / vitest / oxlint / vite build / playwright e2e）的读数同样来自**可复跑的命令 + 写进
  集成记录**（`docs/SDD_TICKET_TRACKER.md` 的 `## T7` 段），不在本文内手抄。

### 5.4 作者红证与变异证据（`#315` 的审查修复轮）

- **红证（变更前的树上）**：`tests/tooling/test_deadline_admission.py` 收集期 ImportError
  （`has_unproven_side_effect` 不存在）、`tests/recovery/test_deadline_restart.py` 收集期
  ImportError（`REASON_DEADLINE` 不存在）、前端新增用例中 6 条在旧实现上失败。
- **变异（隔离副本 `PYTHONPATH=<copy>/src`，主工作树不动）**：6 个变异各自有**不同的失败集**
  （位置契约 / 标记判据 / 恢复严格未来 / 投影覆盖 / 前端草稿校验 / 预算暂停的闸门条件），
  其中 M1a 的失败集是 M1b 的真子集（位置契约由"少了就有一格被烧配额"这一条单点钉住），
  如实登记而不是当成两组独立证据。
- **修后补审**：两个轴在同一冻结 sha/tree（`24a1acc` / `7371656`）上复审修复批次本身
  （互不可替换），结论各为 **PASS-WITH-FINDINGS、0 P0 / 0 P1**（Standards 1×P2 + 5×P3；
  Correctness 1×P2 + 2×P3）。两轴各自独立复核了上面那两条 P1 的两半都已闭合，并各自跑出
  **互不相同**的变异失败集。处置见 §6.1 与台账；其中三条值得单独记：
  - **两条 P2 都是"证据面松"而不是"行为错"**：① 执行域的 `>=` / `>` 等值边界**零鉴别力**
    ——把闸门写成 `>` 时 68 条 deadline 用例全绿（根因是闸门内联 `datetime.now(UTC)`，
    时刻不可注入）。处置：执行域收出自己的 `utc_now()` 单点（跨层例外见 D2），
    新增"`now == deadline_at` 就算到点 + 差一微秒照常接纳"的正反用例，修后同一变异转红；
    ② 场景 Arm B 的点名判据原先接受"**工具名**命中"，而名字不唯一、`apply_blocked_by`
    又只追加不校验 ⇒ 收紧成**只认 id**（文案里 id 一定在，名字那一支纯是松的一格）。
  - **投影 `state` 的覆盖条件**由"有暂停记录"收紧为 `resumable`（暂停**且**非终态）——
    D5 的原话是"'能恢复'这件事只对暂停成立"，而"能恢复"的准确判据就是 `resumable`。
  - 其余为口径/文字类：`operation.py` 的"三处读者"补齐第三处、`runtime.py` 一处过期
    deadline 注释、ADR 本节的 `--collect-only` 条数与实测对齐、前端一处 JSDoc 换行。
- **权威前端车道（`tsc -b`）抓回的真缺陷（`71f19f8`，来源是 Gate-0 车道 ④，不是审查轮）**：
  本票给 `RunLimitsFacts` 加了必需键 `deadline_at`，而投影解析器 `parseRunLimitFacts` 没读它
  ⇒ 生产路径上 `run_limits.deadline_at` 恒 `undefined`，暂停面板「绝对截止时刻」那一行**没有时刻**
  （rc=2 / 9 处 TS2741）。既有用例看不见这一格：它们手工构造 `RunPausedInfo`、绕过了投影解析器。
  处置 = 新增 `dimensionInstantText` + 补读该键 + 3 条钉在**投影**上的用例（删那一行 ⇒ 3 红）。
  与 T5 / T6 同一形态：**每个文件单测都绿、合起来才看得见的缺口**由权威车道抓回。
- **定向两轴审查（冻结 sha `71f19f8`，读范围 `0697ec4..71f19f8`；各一独立只读子代理）**：
  两轴各 **0 P0 / 0 P1**，合计 10 条（含 **1 条 P2**：CLI 上票面 Must Do 的「Show deadline … CLI」
  名存实亡——暂停摘要只打 `dimension=run.deadline_at`，**时刻从不出现**，而 Web 面板与投影都打得出）。
  其余为可读性/口径/夹具类。处置 `c6d9cd6`：CLI 新增 `_deadline_dimension_lines`（暂停块与恢复块都打
  `deadline: <时刻>`；没配 ⇒ `unlimited`、形状认不出 ⇒ `unavailable`、**键缺席 ⇒ 零行**）、
  `dimensionInstantText` 收严（带首尾空白的文本当场收下会与后端**事件回读** `_deadline_or_none` 分叉）、
  `summarizeRunPaused` 加 deadline 分支（Timeline 报**那个时刻**，不再拿 turns 的 `2/8` 冒充）。
- **修后重审（冻结 sha `c6d9cd6`，读范围 `71f19f8..c6d9cd6`）**：两轴各 0 P0 / 0 P1，8 条 P3 于 `cf1879d`
  全部收尾——只动测试断言、注释与登记文本，**无生产判据变更**；其中"恢复块那一行原先零覆盖"一条带机械证据
  （删掉它，27 条 CLI 用例全绿）。本轮处置不含新生产代码面 ⇒ 按 §8.3 第 8 条不触发补审。
- **本段变异（7 个，仓库外副本，失败集互不相同）**：删 `parseRunLimitFacts` 那一行 ⇒ 3 红
  （新用例进来后同一变异 ⇒ **4 红**）；投影退回**宽容读法**（带空白那一格）⇒ 1 红；删 Timeline 的
  deadline 分支 ⇒ 1 红（`run.deadline_at · 2/8`）；CLI helper 返回零行 ⇒ 2 红；CLI 照单全收（印 `None`）
  ⇒ 1 红；恢复块丢行 ⇒ 1 红；恢复块改打旧暂停时刻 ⇒ 1 红。

---

## 6. 未决与后续

### 6.1 已知边界（本票如实登记，未修或刻意不改）

1. **reconcile 裁决没有提交面。** `ReconcileCallback` 只在进程内可注入，
   生产装配的 `SessionService.recover()` **不传**它 ⇒ `POST /recover` 遇到第一行
   RUNNING/UNKNOWN 就 409。后果：一次暂停若同时升了 `NEED_RECONCILE`（deadline 或预算
   都算，见 D3），对 HTTP 客户端是**硬停**——`resume` 409、`recover` 也 409，而"我查过了：
   它确实落地了 / 确实没落地"这句话**没有地方可以说**。拒绝本身是规格要的（`03 §5`、
   不变量 #14），缺的是裁决入口；本票不新增（属对账链的后续票）。
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
5. **Web 的暂停呈现读事件流，不读预算投影。** `runState.ts` 的脉冲与 `PausedPanel` 的数据
   都来自 `run/paused` 事件（以及 `operation/reconcile-required` 入队的
   `reconcile_queue`），而 `state=needs_reconcile` 与 `reconcile` 子对象是**投影**这一侧的
   事实（`GET /api/sessions/{id}/budget`）。后果：一条"暂停 + 欠对账"的 run 在 UI 上仍显示
   "已暂停"并给出恢复输入，点了必被 409 拒（`03 §5`）。后端那一侧是对的（投影与恢复闸门
   都按同一判据），缺的是前端把投影当作刹车灯——属前端票，本票只登记。
6. **模型 closeout 的 `blockers` 只被追加，不被校验。** `apply_blocked_by` 保证"已确证的
   阻塞项一定在列表里"，但模型仍可能写别的（或写空）——产品侧不替它判断"这条算不算阻塞"
   （`02 §5.2`：不许伪造进展，也不许替模型编它的自述）。
7. **对账闸门的作用域：暂停收口是 run 级，另两个读者是 session 级。** D4 说"三个读者读
   同一份落盘事实"，指的是**判据**（`storage.needs_reconcile`）同源，**不**指作用域相同：
   `AgentRuntime._raise_reconcile_required` 过滤 `operation.run_id == run_id`（它产出的是
   **本 run** 暂停载荷的 blockers，点名别的 run 的欠账没有意义），而
   `SessionService._unreconciled_tool_calls`（恢复闸门 + 投影）与 `RecoveryCoordinator.recover`
   是 session 级（它们问的是"这个会话还欠不欠"）。这个分叉**今天不可达**：会话里只要有一条
   未结清的行，任何新 run 的开工就进不来（`resume_and_launch` 的
   `_has_unreconciled_operations` 分支 → `recover()` 无 `ReconcileCallback` ⇒ **409**），
   ⇒ "本 run 带着**别的 run** 的欠账暂停"这个形状造不出来。**不改**：去掉 run 过滤会让本
   run 的暂停点名别人的账，语义更差。口径以本条为准（2026-09-26 补审两轴各报一次）。
8. **投影的第一个分支不看终态。** `project_budget` 按"有暂停记录 ⇒ 用暂停那支投影
   （键集是超集）"分流；`state` 的 `needs_reconcile` 覆盖已按 `resumable`（暂停**且**非终态）
   收紧——但"暂停 **且** 终态"同时成立时它仍走暂停那一支。该形状**不可由事件派生**：
   `derive_run_budget` 在终态事件处清 `paused`（同函数注释：终态压过暂停），
   手工构造 `RunBudgetState` 的地方只有 `SessionService.budget_projection` 的一处空会话默认值。
   登记而不改第一分支：它定的是投影的**键集**，动它等于同时改暂停载荷的形状，而收益只在
   一个造不出来的状态上（2026-09-26 补审 P3）。
9. **`limits.run.deadline_at` 的形状不由显示层复判。** 三面读的是**同一份 durable 事件**，但"形状认不出"
   时**用词**两端不同：CLI 对显式 `null` 报 `unlimited`（与同块 `limit unlimited` 同词）、
   对数 / 布尔 / 空串 / 带首尾空白报 `unavailable`，Web 侧一概 `null`（面板显示 `unavailable`）。
   剩下的分叉是"非空、无首尾空白、但后端**事件回读** `_deadline_or_none` 收不下的**文本**"
   （非 ISO / 无时区朴素 ISO / 小写 `z`）——**不可达输入**（唯一写入者 `RunLimits.as_projection`
   的 `_deadline_text` 恒为 `…Z` 收尾的 RFC 3339）。完整登记与逐格判据在 **ADR-0045 §6.1**
   （同一事实只在一处写全，这里只留指针）。
