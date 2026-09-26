# ADR-0045 — 工具调用计数与 per-tool 配额：接纳点、两个 counter 与上界

- **Status**: Accepted（实现随 T6/#314 落地）
- **Date**: 2026-09-26
- **Deciders**: 用户（#305 的产品与架构裁决，2026-09-23 冻结）+ 本 Agent（票面实现口径）
- **Related**:
  - 父 PRD **#305**、本票 **#314**（T6/12：「Account logical calls and enforce explicit tool quotas」）、
    上游 **#312**（T4：非终态 `run/paused` + 同 run CAS 恢复）
  - **ADR-0044**（长任务执行边界：分层预算、暂停/恢复、stuck、完成判定）——本 ADR 是它在
    **工具调用这一维**的展开：四维的临界点由 D2/D3 的原文冻结，per-tool 配额是**第五类**动态维度
  - **ADR-0039**（ToolExecutor 独占绝对 deadline）、**ADR-0004**（Operation Ledger 与 Checkpoint 分层）、
    **ADR-0014**（同指纹重复失败熔断）——本 ADR 不动这三者的责任域
  - 受本 ADR 约束的正式规格节：`04_TOOL_RUNTIME.md` §9.1、`02_AGENT_RUNTIME.md` §5.1/§5.2、
    `11_STREAMING_API_WEB_UI.md` §6.1、`03_SESSION_EVENT_MODEL.md` §3.4
  - 代码落点：`agent/run_budget.py`、`tooling/executor.py`、`tooling/quota.py`、`agent/runtime.py`、
    `assembly.py`、`web/app.py`、`session/service.py`、`cli.py`、`web/src/lib/runBudget.ts`
- **Refines**: ADR-0044（新增一类 run 维度，不新增生命周期）
- **Supersedes**: 无

---

## 1. Context

### 1.1 票面要解决的问题

`04 §9.1` 与 `02 §5.1` 冻结了两件此前只存在于规格、没有实现的事：

1. **归一化逻辑调用 ≠ 执行尝试。** 一次模型决策可以返回多条 `ToolCall`；ToolExecutor 可以对
   一条逻辑调用重试。`tool_calls` 数「被接纳的逻辑调用」，`tool_attempts` 数「每一次真实的
   `tool.execute`」，两者**不是别名**，`02 §5.1` 明文禁止混同。
2. **显式的 per-tool 绝对配额。** operator 可以对**已注册的工具名**给出正整数绝对 ceiling；
   用尽时不许静默失败，要给一个可审计的事实（并且与四维共用同一套暂停/恢复生命周期）。

票面同时钉死了两条否定式约束（Must Not Do）：**不许**造第二个执行器/重试环/权限路径；
**不许**为普通工具（bash / read / edit / MCP / web…）预设低位默认配额。

### 1.2 现状实测（本 ADR 写作时的读数）

| 事实 | 证据 |
| --- | --- |
| 计数点此前不存在 | `run_budget.BudgetConsumed` 只有四维；`tool/result` 载荷没有预算键 |
| 唯一执行路径已在位 | `ToolExecutor.execute` / `execute_batch` 是全部工具执行的入口（MCP / 内建 / 子 agent 工具都经它） |
| 重试已有责任域 | `ToolExecutor` 自己跑 retry 循环，`result.metadata["attempt"]` 记录尝试序号 |
| 暂停/恢复生命周期已在位 | T4（#312）落 `run/paused` / `run/resumed`，CAS 版本 + 绝对 ceiling 恢复 |
| 注册表在装配期才最终 | `assembly.build_runtime` 先建 registry、再按 agent profile 过滤 tool scope |

### 1.3 本 ADR 的范围

**在**：计数点（接纳点）的位置与它覆盖/不覆盖的四类结果、durable 载体形状、批次内并发窗口、
per-tool 临界点与恢复判据、上界配置的形状校验与注册名校验的位置及其**如实边界**、动态触发
维度命名与判定顺序、客户端（CLI/Web）展示口径、可观测性边界。

**不在**：实现与测试（见 §5 证据）、`max_tool_calls`（工具调用**总数**的 ceiling——规格只冻结
"按名字的显式配额"，总数只观测不设限）、T10 的 `max_delegations=8` 与委派树原子记账（属 #287/#318
的责任域）、优先级/抢占语义（不存在）。

---

## 2. Decision

### D1 — 接纳点是**唯一**计数点，准入前被拒的调用记**显式 0**

计数写在 `ToolExecutor.execute` 内部、四处早退拒绝（工具未注册 / 参数非法 / 配额已尽 / 审批被拒）
**之后**、Operation Ledger 之前的那一个位置。沿途四道门与它们的记录形状：

| 结果 | `error_code` | `budget_delta` | 是否占配额 |
| --- | --- | --- | --- |
| 工具未注册 | `TOOL_NOT_FOUND` | `{tool_calls: 0, tool_attempts: 0}` | 否 |
| 参数不合法 | `INVALID_ARGUMENT` | 同上 | 否 |
| 本 run 配额已尽 | `BUDGET_EXHAUSTED`（本票新增码） | 同上 | 否 |
| 审批被拒 | 审批结果自己的码 | 同上 | 否 |
| 批次前序失败 / 外部取消 | `CANCELLED` | 同上 | 否 |
| 被接纳（含执行失败、含 retry） | 无（成功）或工具自报码 | `{tool_calls: 1, tool_attempts: n}` | 是（1） |

**为什么"被拒"要记 0 而不是不记键**：票面要求"拒绝理由必须可审计"。缺键与记 0 在可观测面上
是两件不同的事——缺键读起来是"这个结果没参与记账"（未知），0 是"参与记账了，贡献是 0"。
悬空修复（reconcile）合成的结果走 `budget_delta=None`（缺键），因为那**不是**接纳点产物；
两者因此可以逐条区分。

**推论（有意接受的）**：一次被拒的调用会在账上留下一个 0 贡献的记录，因此"`tool_calls` 表里
出现过这个工具名"**不**等于"它被调用过"。这是刻意的：读表的人要看的是"谁被拒过、为什么"，
而配额判定只读**求和结果**（0 不改变任何 ceiling 判定）。

### D2 — durable 载体：`tool/result.data.budget_delta`

```jsonc
{"tool_call_id": "...", "content": "<ToolResult JSON>",
 "budget_delta": {"tool_name": "glob", "tool_calls": 1, "tool_attempts": 3}}
```

- **写者只有一个**：`ToolExecutor` 的接纳点（`_admitted_delta` / `_rejected_delta`）。
  Runtime 只把这个值搬进 `emit_result_event(budget_delta=...)`，不重算。
- **`tool_attempts` 取自 `result.metadata["attempt"]`**（ToolExecutor 重试环写的尝试序号）：
  在结果被 overflow 截断（`model_copy`）后仍然存活，所以它是"这一次真实执行了几回"的权威读数。
  形状不是正整数时记 0（宁少不多）。
- **run 账本按 delta 求和**：`consumed_from_events` 的 TOOL_RESULT 分支把它们折进
  `tool_calls_by_tool` / `tool_attempts_by_tool`（`_add_tool_delta`，0 = no-op），
  **总数是两张表的和**（`BudgetConsumed.tool_calls` / `tool_attempts` 是派生属性）——
  不另立总数字段，两个字段就能不一致。

被否掉的三个替代方案（写下理由，避免后来者重新发明）：

1. **新事件类型 `tool/attempt`**：为一次重试再加一条 durable 事件，等于给"重试"造第二个账本
   写入点；且它与 `tool/result` 之间存在部分写入窗口（事件多、原子性更差）。
2. **ToolExecutor 直接写 run 账本**：执行器会因此知道 run 预算的内部形状（分层被打破），
   而账本现在的唯一真相来源是**事件**——直接写会让"重放事件重建账本"不再成立（不变量 #3/#22）。
3. **从 `tool/call` 计数**：`tool/call` 在**准入之前**落盘（顺序知识需要它先于 `artifact/created`），
   用它当"已接纳"会把被拒的调用也算进去——正是票面禁止的误计。

### D3 — 批次内并发窗口 `ToolQuotaWindow`：预留、释放与"未执行不占位"

`execute_batch` 里的多条同工具调用是**并行**（`asyncio.gather`）执行的，各自读 run 账本时
看不到兄弟调用**本次**即将占用的额度。所以 Runtime 在每批开始时构造一个窗口
（`run 账本快照 + 批内预留`），批次内所有调用共用它：

- **检查与预留同步完成**（`take(name)`）：判断与占位之间没有 `await`，因此不存在竞态窗口。
- **预留发生在审批门之前**，审批拒绝时 `release(name)` 归还。这是必须的：若不归还，一个
  "审批被拒"的批次会让窗口认为额度已用掉，而账本读数是 0——两个陈述（"被拒"与"配额用尽"）
  会与账本互相矛盾，而**暂停判定读的是账本**。
- **取消 / 从未执行的兄弟调用不占位**（它们记 0）。
- 空窗口（没配任何 per-tool 配额）恒不阻塞；`limits` 或 `consumed` **未知**（`None`）时
  Runtime **不构造**窗口（而不是把未知当 0——那会过度接纳）。

### D4 — per-tool 临界点与恢复判据

- **暂停点**：`used >= ceiling`（`_dimension_reached` 的第三类）。**不**预留 closeout 容量：
  closeout 是一次模型请求、**不调用任何工具**，给它留一格会让"配额 = 3"实际允许 4 次调用。
  四维中可数的 turns / requests 之所以预留，是因为 closeout 自己要占它们的额度。
- **恢复判据**：`resume_headroom_ok` 要求 `ceiling > used`（严格大于）——恰好等于已消耗的
  ceiling 会被 409 拒，避免"恢复成功但立刻再次暂停"的假象。前端据此给出"至少 `calls + 1`"
  的输入提示（**提示**，判定权威在后端）。
- 恢复**绝不重置** counter（D6）。

### D5 — 上界配置 `budget.run.tool_call_limits`：两道校验，位置与边界如实登记

| 层 | 判据 | 拒绝 | 落点 |
| --- | --- | --- | --- |
| 形状（领域） | `parse_tool_call_limits`：必须是映射；工具名非空、无首尾空白；值必须是 `int`（拒 `bool`）、`>= 1` | 422 | `run_budget.py`，经 `session/service._run_limits` 到所有入口 |
| 注册名（装配） | `validate_tool_call_limits_registered`：名字必须在**profile 过滤后**的注册表里 | 422（列出未注册名 + 已注册清单） | `assembly.build_runtime` |

**位置的理由**：注册表只有在 profile 收窄之后才是"这次 run 真正能用的工具集合"，而收窄发生在
`build_runtime` 内部——所以这一判只能在那一行做。要把它提前到"构造模型对象之前"，就得把注册表
组合**复制**一份到端点层（第二份真相），或先建一个沙箱再判（副作用更大）。

**因此它满足的边界是**（不是更强的那条）：`11 §6.1` 的"**无 Provider 请求、无工具执行、
无子 agent 工作、无消耗预算的事件**"。它**不**保证"未构造模型对象、未创建 workspace 目录"
——这两件事发生在这一行之前。这条边界是实测出来的（web 契约用例原先断言"零副作用 = 没有模型
对象"，实现后无法成立），如实登记而非放宽表述。

**未配置 = 不限，但照样计数**：`{}` / 缺键都表示"没有 per-tool ceiling"，四维之外的普通工具
**没有**任何默认配额（票面的 Must Not Do）。

### D6 — 恢复是**逐键**合并，不是整表替换

`resume_limits` 对 `tool_call_limits` 做 `{**paused, **request}`：点名哪个工具就抬哪个，
未点名的**沿用**（删不掉）。整表替换会静默撤掉 operator 起的其他工具配额——那是"借着一次抬高
顺手放大授权"，与 ADR-0044 D1「配置只能收窄」相反。四个 `max_*` 维沿用 ADR-0044 的 `_pick`
（`None` = 没点名）。

### D7 — 动态维度命名与判定顺序

- 每个配了配额的工具名占一维：`run.tool_call_limits.<tool_name>`（`TOOL_DIMENSION_PREFIX`）。
  它是**动态**的，因此不在 `TRIGGER_ORDER` 这个定长元组里，由 `tool_dimensions()` 按名字
  **排序**给出——顺序必须确定，否则"同时到顶报哪一个"会随 JSON 键序漂移，暂停载荷就不可复现。
- `pause_trigger` 的判定顺序：四维（`TRIGGER_ORDER`，turns 最先）→ per-tool 维（按名排序）
  → local fuse。工具配额与 local fuse 是**两层不同**的控制（`02 §5.1`），不合并成一个计数器。

### D8 — 客户端同源（CLI 与 Web 显示同一份事实）

| 面 | 落点 | 口径 |
| --- | --- | --- |
| CLI 配置 | `--run-tool-limit NAME=N`（可重复，`NAME=` 与值在**同一个** argv 里） | argv 层只 reject 明显畸形（缺 `=` / 重名 / 非整数）；形状规则仍在领域层 |
| CLI 呈现 | `_tool_dimension_lines`：`tool <name>: consumed X calls / Y attempts / limit Z (remaining R)` | 两个 counter **分列**；`unavailable` / `unlimited` 永不写成 0 |
| CLI 恢复提示 | `_resume_command_tail` / `resume_hint` 给 `--run-tool-limit <name>=N` | 提示必须是**能照抄执行**的真实 argv 形状 |
| Web | `web/src/lib/runBudget.ts` 的 `ToolQuotaFacts` / `PausedPanel` | 与 CLI 同序同类；恢复输入抬的是**那个工具**（`tool_call_limits.<name>`），草稿默认 `calls + 1` |

Web 侧一处刻意的**逐字镜像**：配了配额但从未调用过的工具，其读数是 `unavailable` 而**不是** 0
（CLI 也是 `calls.get(name)` → `None`）。两块屏幕显示同一份事实是票面 AC 的原话；账本自己的
`calls_for()` 读 0 是另一层口径（`/api/sessions/{id}/budget` 投影用它）。

---

## 3. 不变量与边界（本 ADR 之后仍然成立）

1. 工具执行**只有一条**路径：per-tool 配额是准入前置判定，不新建第二个执行器/重试环/权限路径。
2. retry 仍归 ToolExecutor（`tool_attempts` 只是它的**读数**，不是新 owner）。
3. 权限 / 沙箱 / Operation Ledger / reconcile 语义逐字不变；配额不改变审批结果。
4. `tool_calls` / `tool_attempts` 的**唯一**增量来源是 `tool/result.data.budget_delta`。
5. 配额不能靠改 `tool_call_id` 或参数格式绕过：计数与 `tool_call_id` **无关**（同一次接纳无论
   何种 id 都记 1），配额判定只看 `tool_name` 与账本。
6. 未配置 per-tool 配额时行为与 T5 逐字相同（不限、照样计数）。
7. 可选能力（Langfuse 等）不影响本维度的判定。

---

## 4. 后果

- **好的**：`tool_calls` / `tool_attempts` 可分别观测与对账；per-tool 配额与四维共用同一套
  暂停/恢复/CAS/投影生命周期（没有第二套状态机）；被拒调用的理由durable 可审计。
- **代价**：每次 `tool/result` 多一个兄弟键（事件体积增加 O(1)）；批次内多一个窗口对象的
  生命周期责任（`take` 必须与 `release()` 或真实接纳成对——由 `tooling/quota.py` 的
  `__slots__` 小对象 + 单批作用域封住）。
- **覆盖到的调用**：**包括**失败与取消的**已接纳**调用（它们确实发生过，是真实尝试）；
  **不包括**准入前被拒的调用（记 0，理由在 D1）。

---

## 5. 证据（实现与验证）

- 单测：`tests/tooling/test_tool_quota.py`（接纳/拒绝/批次窗口/取消不占位/retry 只记一次逻辑调用）、
  `tests/agent/test_run_pause_resume.py`（配额用尽 → 暂停 → 抬高 → 同 run 完成）、
  `tests/web/test_run_pause_resume_api.py`、`tests/web/test_budget_local_fuse_api.py`（注册名 422）、
  `tests/test_cli_run_pause_resume.py`（真工具 `glob` 的端到端：暂停 → `--run-tool-limit` → 完成）。
- 前端：`web/src/lib/runBudget.test.ts`、`web/src/lib/projection.pause.test.ts`、
  `web/src/components/PausedPanel.test.tsx`、`web/src/lib/api.test.ts`。
- Live Gate：`evaluation/live_gate/scenarios/long_task.py`（v3 起把实现身份 + 两套计数 +
  显式 ceiling 纳入断言面）在**真实模型 + 生产工具**上三连跑（同 SHA/树）。
  真实 3/3 证据（**最终**）：`docs/live_gate/20260926T020152-462c5bd31cb8-long-task-past-legacy-turn-limit/`
  ——绑定 sha `462c5bd3` / tree `ddaa252c`（`tracked_matches_head=True`），3 次尝试 `steps=15/16/16`
  （均越过旧上限 10）、逻辑调用 `15/17/16`（第 2 次是 16 个决策对 17 条调用：一条消息带两条 `read`）、
  `tool_calls == tool_attempts`（本 run 无 retry）、per-tool 表 `bash=12/12/12`、请求的 ceiling
  `{'bash': 24}` 与 `run/started` 落盘快照一致；`validate --require-pass` 复核 24/24 条 0 FAIL（exit 0）。
  ⚠ 首版证据（sha `317cf2cb` / tree `2b906371`，目录 `…20260926T012418-…`）在 `RUF023` 处置动了
  `src/**` 之后**不再覆盖本树** ⇒ 按 T5 同例**保留入库作原始依据**，并由本行替换其指针。
  ⚠ 本 ADR 写于证据首次产出之前，当时的指针**指错文件**（写成
  `tests/live_gate/test_pause_resume_scenario.py`，那是 T4 的场景测试）且**提前宣告
  证据存在**——两轴审查的 Standards 面按 P1 拒收，此处于证据入库时按实测订正。

---

## 6. 未决与后续

- **`max_tool_calls`（工具调用总数上限）**：规格只冻结"按名字的显式配额"，总数保持只观测；
  若产品要总数上限，它是另一票（当前请求体显式 422 拒绝该字段，不静默忽略）。
- **per-tool 配额的默认值**：本票**不**给任何普通工具默认配额；委派树默认 `max_delegations=8`
  属 T10 / #287。
- **`tool/attempt` 独立事件**：不建（理由见 D2）；若未来观测面需要每次尝试的时间线，
  应先评估 `tool/result` 里的 `attempt` 计数是否已够。

### 6.1 已知边界（本票如实登记，未修或刻意不改）

- **HARD 熔断优先于配额暂停（重叠区）**：单条 assistant 消息里，若**真实执行**的同指纹
  失败已攒够 `soft+hard`（默认 3+3），护栏的 HARD 在工具批次末尾收 `run/failed`（终态），
  而配额暂停判定在循环顶 ⇒ 一批**同时**满足两者时终态胜出、`run/paused` 不发生。
  **这是刻意的**：模型真在死循环里撞墙时 `#69` 的终态是正确归宿（恢复只会把它放回同一个
  死循环），配额耗尽只是同时出现的第二个信号。修复笔 `b9f9bbd` 只让**配额拒绝本身**不
  喂护栏（那才是"虚构的失败"，复现用例见 §5），不改变"真实失败照数"。若产品要"配额耗尽
  一律可恢复"，那是优先级规则的另一票。
- **畸形计数条目两端显示不同**：`consumed.tool_calls_by_tool.<name>` 的值若不是非负整数，
  CLI 报 `unavailable`（不印值、不当 0），Web 报 `0`（解析器把畸形条目整条丢掉之后按
  "表在、名字不在"读）。**不可达输入**——唯一写入者是接纳点的 `budget_delta`（恒为非负
  整数），且 `tool_call_limits` 在入口已 422 ——故本票只登记，不改前端解析器的类型。
- **`take()` 之后的抛点**：配置错误已移到配额闸门之前（消除一条泄漏路径，用例见
  `tests/tooling/test_tool_quota.py::test_config_error_does_not_leak_a_reserved_slot`），
  但 `take()` 之后仍有可抛点（Operation Ledger 的 await、`kill_hook` 注入）。当前调用链让
  异常穿出整批、窗口随批次对象一起消失 ⇒ 不会出现"槽位泄漏后窗口被复用"。若未来出现
  "捕获后复用同一窗口"的调用方，这条约束需要重新审视。
- **崩溃 + 恢复的记账缺口**：恢复时**合成**的 `tool/result`（`recovery/coordinator.py`）
  不带 `budget_delta` ⇒ 崩溃前已接纳、且已写下 PENDING/RUNNING 的那次调用在账上记 0
  （少记，方向=放宽）。它属 `#315` 的副作用 reconcile 面，本票不改；影响仅限终态 run 的
  投影（那个 run 已不可恢复，丢失的计数进不了任何后续准入判定）。
- **`limits.run.deadline_at` 的形状不由显示层复判（`#315` 增补）**：值若不是"非空且无
  首尾空白"的文本（数 / 布尔 / 空串 / 带首尾空白）⇒ CLI 与 Web **都**读作"这一维没有
  时刻"（`unlimited` / `null`），与后端事件回读 `agent/run_budget._deadline_or_none`
  的同名判据一致。**唯一的分叉**是"非 ISO 的**文本**"（如 `garbage`）：两端照契约原样
  显示成时刻，而 `_deadline_or_none` 对同一份事件判"没配"。**不可达输入**——唯一写入者
  是 `RunLimits.as_projection` 的 `_deadline_text`（恒为 `…Z` 收尾的 RFC 3339 文本）
  ——故只登记，不在显示层写第二份 RFC 3339 解析（带时区 / 严格未来的形状判据属恢复草稿
  那一关：`deadlineDraftError` → `parseInstant`，与后端 `parse_deadline_at` 同口径）。
