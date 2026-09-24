# ADR-0044 — 长任务执行边界：分层预算、同 run 暂停/恢复、stuck 检测与可靠完成判定

- **Status**: Accepted（契约已冻结；实现分票 T2–T12 尚未落地）
- **Date**: 2026-09-25
- **Deciders**: 用户（#305 的产品与架构裁决，2026-09-23 冻结）+ 本 Agent（规格写入与口径组织）
- **Related**:
  - Issue **#305**（父 PRD，「Agent 长任务分层预算、暂停恢复与可靠完成判定」）与 **#306**（本票：把 FIXED 决策写进正式规格与本 ADR）
  - `docs/PRD_AGENT_RUN_BUDGET_PAUSE_RESUME.md` —— #305 正文的**仓库内副本**（38,483 字符 /
    41,041 字节，LF；逐行与正文一致，**唯一**差异是正文末尾多一个换行，本文件以单个换行收尾）
  - `docs/research/2026-09-23-agent-tool-loop-termination-benchmark.md` —— 阈值与设计取舍的逐家对照与 caveat
  - 受本 ADR 改口径的正式规格节：`02_AGENT_RUNTIME.md` §5/§6/§5.1–§5.4、`03_SESSION_EVENT_MODEL.md` §3.4/§5/§6/§7、`04_TOOL_RUNTIME.md` §9.1/§6、`10_MULTI_AGENT_DELEGATION.md` §5.1/§12、`11_STREAMING_API_WEB_UI.md` §6.1、`12_OBSERVABILITY_EVALUATION.md` §9.1
  - **ADR-0014**（完全相同工具名+参数的重复失败熔断）——本 ADR 把它的**责任域扩展**为多模式 stuck 检测，不新增第二个 guard
  - ADR-0039（ToolExecutor 独占绝对 deadline 责任）、ADR-0004（Operation Ledger 与 Checkpoint 的分层）、ADR-0037（投影引用稳定性）、ADR-0029（会话硬删除）、ADR-0017（Resume/Replay/Fork）
- **Refines**: ADR-0014（同一责任域，从「同指纹连续失败」扩展为五模式 stuck 检测）
- **Supersedes**: **无**（不删除任何既有决策；`max_steps` 走 expand–migrate–contract，退役归 #320）

---

## 1. Context

### 1.1 为什么需要这份 ADR

正式规格此前把 `max_steps` 写成「Agent Loop 的硬兜底」，而实现把它当成了**主要终止条件**：
默认值散落在多个入口，低位（10 / 20）且互不一致。用户可观察后果是稍复杂的任务在尚未完成时
被截断，而截断既不表达「没预算」也不表达「卡住」，更无法区分「已完成」「存在待 reconcile 副作用」。

#305 已把新的产品与架构裁决冻结为 35 条 User Stories + 12 条 Contracts + 16 条 Implementation
Decisions + AC-1..AC-19。（#305 正文的 FIXED 段自述「All 30 user decisions」，与其 35 条 User
Stories 不一致——本票只如实登记该上游不一致，**不**修改 #305 正文，见 §4.1。）按项目优先级（正式规格
高于 GitHub Issue），**先写规格再写代码**：否则实现者会同时面对旧规格与新 Issue，无法判断哪一套语义有效。

### 1.2 现状实测（本 ADR 写作时的读数，非推断）

| 事实 | 证据 |
| --- | --- |
| 低位默认值分散且不一致 | `cli.py:207` = 10；`profiles.py:45` 默认 20，`main`=20 / `coding`=10 / `research_review`=10（`profiles.py:97/106/113`） |
| 无预算模型 | `src/` 全目录 grep 不到 `max_agent_turns` / `RunBudget` / `run/paused` |
| 现有护栏只认同指纹 | `agent/guards.py`（ADR-0014）：指纹 = `(tool_name, canonical args)`，连续 3 次同指纹失败软熔断、再 3 次硬熔断 |
| 委派配额已有运行期 owner | `multiagent/depth.py`（#286）用 ContextVar 持有「剩余深度」，`multiagent/tools.py` 持有 `tree_id` 与 `max_delegations`（默认 8） |
| 完成判定只看最新模型消息 | 单 Runtime loop 序列化「模型决策 → 工具批次」，但 Approval / Child / Operation Ledger / reconcile 属跨组件在途状态 |

### 1.3 本 ADR 的范围

**在**：分层预算与 counter 口径、暂停/恢复状态机与并发控制、stuck 检测阈值与恢复前置条件、
完成判定（Quiescence + CompletionPolicy）、委派树预算的 owner 归属、`max_steps` 迁移序列、
投影与接口语义、Live Gate 证据口径，以及**决策覆盖表**（§3）。

**不在**：实现与测试（T2–T12）、UI 视觉与文案、新的上游框架引入。阈值、默认值与事件语义
**一个数字都不新增**——全部照抄 #305，写入正式规格章节（§3 覆盖表逐项指认落点）。

---

## 2. Decision

### D1 — 三种控制不是同一计数器，且配置只能收窄

三种控制（Local AgentRuntime fuse / RunBudget / SessionBudget）的**定义、作用域、默认值与优先级**
是契约，权威表在 `02 §5.1`——本 ADR **不复制**该表（同一事实两处写全会漂移）。本节只固定**理由与边界**：

- 三者**不可互相替代**：local fuse 管「单个 Runtime 实例不许无限转」，RunBudget 管「一次逻辑 run
  （含它创建的子孙）花了多少」，SessionBudget 管「会话及其跨 run 的委派后代累计花了多少」。
  把任意两者并成一个计数器就会开出漏洞：换新 run 即刷新额度，或一个子 Agent 拖垮整棵树。
- 配置只能**收窄**，不能放大：越权（试图越过生效上层的 ceiling）在**任何工作开始前**被拒绝（判据与
  状态码见 D9 与 `11 §6.1`）。
- local fuse **不跨兄弟池化**：父级额度更大 MUST NOT 让子 Agent 继承一个更大的本地保险丝。
  正因为 RunBudget / SessionBudget 允许「无显式 ceiling」，这条保险丝才是最后一道防线。

### D2 — counter 语义：七个定义各有**唯一计数点**

七个 counter（`agent_turns` / `model_requests` / `tool_calls` / `tool_attempts` / `total_tokens` /
`cost_usd` / `delegations`）的定义与计数点是**契约**，权威表在 `02 §5.1`——本 ADR **不复制**该表
（同一事实两处写全会漂移）。本节只固定三条最容易混淆、且直接决定计数正确性判据的边界：

1. **`tool_calls` ≠ `tool_attempts`**：一次 ToolExecutor retry 仍是一个逻辑调用；接纳点**之前**被拒的
   调用不消耗配额，但拒绝理由必须可审计。
2. **`model_requests` 独立于 `agent_turns`**：primary / fallback / closeout / 子 Agent 的**每一次**
   实际 Provider 请求各自计数；被拒或传输失败的请求**不**增 `agent_turns`。
3. **`cost_usd` 只在 Provider 给出可靠归属值时累计**：不可得记 **unavailable**，不编价、不记 0。

### D3 — 暂停/恢复是 durable 生命周期，不是终态

- 新增两类**持久化**事件（名字与载荷的权威枚举仍只有 `docs/EVENT_VOCABULARY.md`，本 ADR 只定格契约语义）：
  - `run/paused`：`reason ∈ {budget_exhausted, deadline, stuck}` + `trigger_dimension` + `budget_version`
    + `consumed` / `limits` 快照 + `continuation`（已完成 / 剩余 / 阻塞 / 下一步安全动作）
    + `closeout_source ∈ {model, deterministic}` + `resume_requirements`（预算与 deadline 暂停为空）。
    **非终态**：停止活动执行，但不关闭逻辑 `run_id`。
  - `run/resumed`：`from_pause_seq` + `previous_budget_version` + 新 `budget_version` + 更新后的 `limits`
    + `consumed`（**等于**暂停快照，直到产生新工作）+ `resume_basis ∈ {budget_increase, relevant_steer, environment_change, policy_change}`。
- **同一 `run_id`**：恢复绝不新建 run_id，绝不重置任何 counter 或 stuck 指纹。
- **CAS**：恢复请求带 `expected_version` 与**绝对 ceiling**（不是增量）。版本过期、把 ceiling 降到
  已消耗之下、缺少必要变更依据、存在未 reconcile 副作用 ⇒ **409**，且**不启动**任何 model/tool/child 工作。
  并发提交同一版本 ⇒ 至多一个成功。
- **closeout 预留**：暂停前在适用预算内**预留** closeout 容量，给模型一次**有界**机会产出 continuation；
  模型 closeout 不可用或产出无效时，落**确定性** continuation（**只**用已持久化事实，不伪造进展/成功/工具结果）。
- `paused` 与 `completed` / `failed` / `interrupted` / `needs_reconcile` 在**所有客户端**上必须可区分；
  六值 Run 状态集合（含 `active`）与「显式取消不是 `paused`」的读法权威在 `03 §5`。

### D4 — deadline 与 cancel 是两种语义；不确定副作用不得盲重跑

- `deadline_at` 是 RFC 3339 UTC **绝对**截止；检查点在**每次接纳新工作之前**：Provider 请求、
  ToolCall、子 Agent。
- 到点后**不启动任何新工作**；已在途的工具/操作按其**既有** ToolExecutor timeout/cancel 与
  Operation Ledger 语义收尾（ADR-0039 的责任域不变），随后在**下一个稳定边界**落
  `run/paused(reason=deadline)`。
- 若某个 mutating 操作**无法证明已完成或未开始** ⇒ 转 `NEED_RECONCILE`（**不得**落一个暗示可安全续跑的暂停），
  并在 reconcile 解除前**拒绝恢复**；**绝不自动重跑** UNKNOWN 高风险工具（AGENTS §7 不变量 #14）。
- 显式用户 **cancel** 保持既有立即语义，**不**被改写成预算暂停，两者在投影上可区分。

### D5 — stuck 检测：扩展既有 guard 责任域，一次 replan，恢复需真实变更

五个模式的阈值是**契约**，权威表（含 3 / 4 / 3 / 6 / 4 与项目级阈值的 provenance）在 `02 §5.3`
——本 ADR **不复制**该表；本节固定其规则与理由：

- **责任域唯一**：扩展 `agent/guards.py`（ADR-0014）这一处护栏，**不新增第二个 loop guard**。
- 阈值首达 ⇒ 发一条结构化 guard 事件 + 允许**恰好一次**纠正性 replan；同一模式在 replan 后
  仍持续且无进展证据 ⇒ `run/paused(reason=stuck)`。
- 指纹必须**规范化**（call id、格式、等价参数抖动、修饰性文字**不**构成进展），并**脱敏**后才可
  指纹化与持久化；指纹跨进程重启与同 run 恢复**保持**。
- 恢复前置：**相关** steer 事件、**比暂停快照更新**的工作区/环境 revision、或**比暂停快照更新**的
  policy/profile 版本，三者其一；缺依据的 plain continue 与无关变更 ⇒ **409**。
- 只有出现相关进展时才复位**受影响的那一个**模式。

### D6 — 完成判定：Quiescence 强制、CompletionPolicy 可插拔

`run/completed` 之前**必须**先证明 Runtime Quiescence（六条，缺一不可）：

1. 没有已接纳但缺结果或恢复分类的工具调用；
2. 没有未决的 ApprovalRequest；
3. 没有活动或待恢复的子 Agent；
4. 没有 pending / unknown 且未定 reconcile 状态的 Operation Ledger 记录；
5. 没有 pending 的 reconcile 操作；
6. 最新被接纳的模型决策不再请求新的工具调用。

Core 提供**一个**可插拔 `CompletionPolicy` seam，位置在既有的最高完成边界上。默认通用策略 =
静止后接受最终模型响应；域策略可以**增加**完成证据，但**不得**绕过 quiescence、权限或账本检查。
不静止时**不得**落 `run/completed`，应保持未解 owner 活动或进入准确的暂停/恢复/reconcile 状态；
未解工作结清后**重入同一完成闸门**。终态事件仍然**恰好一个**，既有 finalizer 与事件顺序契约不变。

### D7 — 委派树共享预算的唯一 owner 是 #287 的 tree context

- 「谁还能再委派、还能再往下几层、树内已消耗多少」这套运行期事实的 owner 是既有 runtime 侧 tree
  context：**#286** 落地了运行期的深度配额与收窄语义（`multiagent/depth.py` 的 ContextVar 作用域，
  `AgentSpec.max_depth` 只能收窄不能抬高），**#287** 落地了树级默认配额与**持久化**树账本
  （`multiagent/tools.py` 的 `tree_id` / `max_delegations`，持久化计数由 provider 的共享 tree ledger
  负责）。本特性**扩展**它们，**不得**另建平行的第二棵树账本。
- 树内 `max_delegations` 默认 **8**：整棵 SessionBudget 树最多 8 次被接纳的 `delegate`，
  第 9 次在**子 Agent 执行前**被拒。
- 根/子/孙与并发兄弟对共享额度的更新必须原子；创建、重试、恢复、重启子 Agent **都不能**
  重置或放大剩余深度、counter、ceiling 或 stuck 指纹。子 `AgentSpec` / profile 只能**收窄**授权。

### D8 — `max_steps` 走 expand–migrate–contract

- 新公开字段 `budget.local.max_agent_turns`；`max_steps` **保留为过渡 alias**：
  只发 `max_steps` ⇒ 解释为根 AgentRuntime 的 local fuse；两者同时出现且**相等** ⇒ 接受；
  **不等** ⇒ 422（**在任何工作开始前**）。任何超过生效上层 ceiling 的配置 ⇒ **拒绝，不静默截断**。
- 仓库内调用方（Web / CLI / SessionService / AgentProfile / Multi-Agent 创建 / 测试）随 T3 迁移；
  **删除** alias 是 contract 阶段（`#320`），前置条件是**证明零剩余调用方**（#305 AC-15）——
  五场景真实 Live Gate 3/3（D10）是**本特性**的集成门禁，不是删除动作的额外前置。
- 迁移期间**不得**改变既有 step/event 身份语义（step_id 与事件序列 identity 不因改名而变）。

### D9 — 投影与接口语义

- **422**（形状非法、别名冲突、越权 ceiling、无法强制执行的显式 token/cost ceiling）与
  **409**（版本过期、ceiling 低于已消耗、活动冲突、stuck 缺变更依据、未 reconcile）**都在开工前**判定，
  被拒请求**不**启动 model/tool/child 工作，**不**写消耗预算的事件。其中「越权 ceiling ⇒ 422」
  是本 ADR 对 #305 「rejected before work starts」的**补码裁决**（#305 §9 的 422 清单未点名该项，
  语义上它与其余 422 同类：输入不可接受且未开工），MUST NOT 被读成 #305 原有条款。
- 投影字段（会话与活动/暂停的 run，按作用域）：不可变身份、当前 `version`、配置的绝对 ceiling、
  已消耗 counter、有 ceiling 时的 remaining、token/cost 维度的**可执行性**、当前 deadline、
  暂停原因与 continuation 引用。**Provider 账目缺失 = unavailable，永不记 0**。
- SSE/WS：`run/paused` / `run/resumed` 走**既有**信封与顺序保证；重连/回放从 append-only 存储取回同样事件，
  **不**合成第二套客户端状态；暂停后当前直播流**干净收束**，恢复时以**同一 `run_id`** 接回正常流；
  重复帧按既有 seq 幂等处理。
- CLI 与 Web：状态**只**来自 SessionEvent/投影（进程本地内存与客户端本地暂停态**都不**是权威）；
  刷新与重连后必须重建出同样的暂停原因、continuation、已消耗/上限与版本；stuck 暂停**不提供**
  无依据的一键重试。

### D10 — Live Gate 是强制证据，且与无凭证套件分离

- 五类场景（照抄 #305）：① 真实模型 + 生产工具完成**超过旧 10 轮/10 工具**的实际任务；
  ② 低显式预算 ⇒ `run/paused` ⇒ 提高绝对 ceiling 后**同 run_id** 恢复并完成、消耗不重置；
  ③ 真实重复工具失败 ⇒ 恰好一次 replan ⇒ 同模式持续则 `PAUSED_STUCK`；
  ④ 真实 deadline 且存在在途 mutating 工具 ⇒ 不启动新工作，安全收尾或 `NEED_RECONCILE`；
  ⑤ 真实父子委派树共享预算并强制 `max_delegations=8`。
- 每个场景在**同一** commit/tree/配置上**连跑三次**，**3/3** 才算通过；**所有失败尝试必须保留**
  （禁止重跑后只留成功者）；任何代码变更即作废先前的矩阵。
- 证据必须含：`schema_version`、`scenario_id` / `scenario_version`、`sha`、`tree`、Provider/model
  标识（**无** token/密钥/授权头）、Sandbox 类型与一次性工作区身份、三次尝试的起止与状态、
  Session/Run 身份、事件/trace 引用、`verdict ∈ {PASS, FAIL, BLOCKED, SKIPPED}`。
- 缺凭据、Provider/Sandbox 不可用、场景未执行 ⇒ **BLOCKED / SKIPPED，永不 PASS**；受影响的
  implementation issue **不得关单**。Live Gate 是**专用可复跑命令**，与默认无凭证测试套件分离。
- **凭证零泄漏**：不打印、不持久化、不提交任何凭证值；`.env` 只可列 key 名。

### D11 — 与既有不变量的关系（逐条不改）

- 仍然**只有一条** Tool 执行路径、**一个** retry 责任域（ToolExecutor）、**一个** loop guard 责任域（D5）；
- Model fallback 与 Tool retry **继续分离**；本特性不改变 Provider 选择策略；
- 高风险 UNKNOWN 操作**不盲重跑**（D4）；Operation Ledger 仍是副作用账本，checkpoint 仍不等于副作用恢复；
- Optional capability（Langfuse / Memory / MCP / Web）故障**不能**拖垮 Core；
- replay **不**执行模型/工具、**不**消耗预算；fork 得到**新的** SessionBudget 身份 + 父快照与 lineage。

---

## 3. 决策覆盖表（#305 → 落地位置）

用法：左边是 #305 的编号（该文件是仓库内逐字节副本），右边是**生效落点**。**判据是右列可逐条打开**。
凡 #305 的 BOUNDED / FREE 项，此处只登记「实现自由」的边界，不额外约定实现形状。

### 3.1 Contracts（#305 第 240–453 行）

| #305 | 主题 | 落点 |
| --- | --- | --- |
| §1 | counter 语义（7 个） | 本 ADR **D2** + `02 §5.1` |
| §2 | 三种控制 + 优先级 | 本 ADR **D1** + `02 §5.1` |
| §3 | 预算输入契约（`budget` 对象、校验规则、`max_steps` alias、绝对 ceiling） | `02 §5.1`、`11 §6.1`（对象形状与校验规则）、`04 §9.1`（工具配额）、`10 §5.1`（session 作用域）、本 ADR **D1/D8** |
| §4 | 投影契约 | 本 ADR **D9** + `11 §6.1` |
| §5 | SessionEvent 契约（`run/paused` / `run/resumed` / 账目事件可由实现选择表示） | `03 §3.4` + 本 ADR **D3** |
| §6 | Run 状态契约（六值集合；含 `needs_reconcile` 优先于恢复、cancel 不变） | `03 §5`（状态集合与 cancel/interrupted 读法）+ `03 §3.4`（事件）+ 本 ADR **D3/D4** |
| §7 | CompletionPolicy + Quiescence 六条 | `02 §5.4` + 本 ADR **D6** |
| §8 | stuck 阈值表与五条规则 | `02 §5.3` + 本 ADR **D5** |
| §9 | REST 行为（422 / 409 / 不开工） | `11 §6.1` + 本 ADR **D9** |
| §10 | SSE / WebSocket 行为 | `11 §6.1` + 本 ADR **D9** |
| §11 | CLI 行为 | `11 §3`/`§6.1` + 本 ADR **D9** |
| §12 | Web UI 行为 | `11 §6.1` + 本 ADR **D9** |

### 3.2 Implementation Decisions（#305 第 455–472 行）

| # | 决策 | 落点 |
| --- | --- | --- |
| 1 | Runtime 仍项目自有、Python、async-first | 不变（`02 §1`） |
| 2 | 旧低 step 上限被**分层预算**取代，不是换个低位数字 | `02 §5`、本 ADR **D1** |
| 3 | 默认 local fuse 500；Deployment 可降，请求不得越权 | 本 ADR **D1**、`02 §5.1` |
| 4 | RunBudget 与 SessionBudget 是**两个** durable 账本，各自覆盖其作用域的委派树 | 本 ADR **D1/D7**、`10 §5.1` |
| 5 | SessionBudget 无默认 ceiling（`max_delegations` 除外），但必须累计并投影 | `10 §5.1`、`11 §6.1` |
| 6 | `max_delegations=8` 是本 PRD 唯一新增的工具级默认配额 | `10 §5.1` + 本 ADR **D7** |
| 7 | closeout 容量在适用预算内**预留**，不是额外不记账的工作 | 本 ADR **D3**、`02 §5.2` |
| 8 | 模型 closeout 不可用 ⇒ 确定性 continuation 只用已持久化事实 | 本 ADR **D3**、`02 §5.2` |
| 9 | deadline 在稳定边界协作式生效；显式 cancel 保持立即语义 | 本 ADR **D4**、`04 §9.1` |
| 10 | 恢复用绝对 ceiling + 乐观并发，**绝不**重置已消耗 | 本 ADR **D3**、`03 §5` |
| 11 | fork 新建 SessionBudget 并记录父快照；replay 零消耗 | `03 §6/§7` + 本 ADR **D11** |
| 12 | 同名前参失败检测**扩展**为多模式 stuck，仍只有一个 guard 责任域 | 本 ADR **D5**（Refines ADR-0014） |
| 13 | CompletionPolicy 可选可插拔；Quiescence 强制不可绕过 | 本 ADR **D6**、`02 §5.4` |
| 14 | `max_steps` expand–contract；删除是后续 contract 票 | 本 ADR **D8** |
| 15 | **#287** 拥有 durable 树级委派预算/指纹机制，本特性扩展而非另建 | 本 ADR **D7**、`10 §5.1` |
| 16 | Web UI 仍是 SessionEvent 与服务端状态的投影 | `11 §6`/`§6.1` |

### 3.3 Expected Behavior 与 Acceptance Criteria 的落点

| #305 | 落点 |
| --- | --- |
| EB-1..EB-13 | 分散契约化：EB-1/EB-2 → `02 §5`；EB-3/EB-4 → `02 §5.2` + `03 §5`；EB-5 → `11 §6.1`；EB-6 → `02 §5.1`；EB-7 → `02 §5.3`；EB-8 → `04 §9.1`（deadline 接纳边界）；EB-9 → `02 §5.4`；EB-10 → `10 §5.1`；EB-11 → `03 §6/§7`；EB-12 → `02 §5.1`（alias）；EB-13 → `11 §6.1` |
| AC-1 | `12 §9.1` 场景 ① + `02 §5`（旧限制退役） |
| AC-2 | `02 §5.1` 默认值 + `11 §6.1` 投影 |
| AC-3 | 本 ADR **D2**（唯一计数点） |
| AC-4 / AC-5 | 本 ADR **D1/D9**（越权与不可强制执行的显式 ceiling 在开工前拒绝） |
| AC-6 / AC-7 / AC-8 | 本 ADR **D3** + `03 §3.4/§5` |
| AC-9 | `03 §5`（**Crash durability** 段：真实 kill 后重建同 limits/consumed/version/continuation/指纹） |
| AC-10 | `02 §5.3` |
| AC-11 | 本 ADR **D4** + `04 §9.1` |
| AC-12 | `02 §5.4` |
| AC-13 | `10 §5.1` |
| AC-14 | `03 §6/§7` |
| AC-15 | 本 ADR **D8** |
| AC-16 / AC-17 | `11 §6.1` |
| AC-18 | `12 §9.1` |
| AC-19 | 项目既有门禁（`AGENTS §14.10`、协议 §7 第 6/8 条）——本票按纯文档面执行 |

### 3.4 FIXED / BOUNDED / FREE 的边界（#305 第 474–505 行）

- **FIXED** 全量落在：本 ADR D1–D11 + 上表引用的规格章节。逐条可判：counter（D2）、默认值（D1）、
  暂停/恢复语义（D3）、同 `run_id`（D3）、append-only 事件事实源（`03 §1`，不变）、
  单一 Runtime / 单一 ToolExecutor（D11）、权限与 Sandbox 边界不靠 prompt（不变）、
  retry 归 ToolExecutor 且与 fallback 分离（D11）、Operation Ledger reconcile（D4）、
  `max_steps` 迁移序列（D8）、真实 Live Gate（D10）、凭证零泄漏（D10）。
- **BOUNDED**：账本内部表示可由实现选择（但必须原子可重建、可审计）；为暴露预算/完成 seam 的必要
  重构允许，无关行为不得改变；UI 布局与文案自由但固定状态与动作必须可区分；指纹可复用既有
  规范化但必须识别等价抖动并脱敏；Live Gate 可用一次性工作区，但必须用配置的真实 Provider 与生产工具。
- **FREE**：内部命名、模块组织、值对象技巧、非契约 UI 细节、索引优化、测试辅助组织。

---

## 4. 边界与明确不做

- 不用 LangGraph / LangChain 接管 Agent Loop；不新增第二条 Tool 执行路径；不新增第二个 loop guard。
- 不把 Git / pytest / Todo / 具体 Coding Tool 的完成规则硬编码进 Core（CompletionPolicy 是 seam，不是特判）。
- 不做跨主机分布式预算协调；不做计费/开票/自动购额；不做通用 workflow DAG 引擎。
- 不为 read / edit / bash / web / MCP 设任意低默认配额（唯一新增默认配额是 `max_delegations=8`）。
- 不保证模型能完成本身不可完成、权限不足或外部依赖缺失的任务。
- 不用 Mock/Fake 的成功替代真实模型与生产工具的交付证据；不静默自动恢复。
- 不新建平行 Engineering Specification 或平行 Roadmap（本 ADR 只做**增量对齐**）。

### 4.1 已登记的上游不一致与待同步面（本票**不改**，如实登记）

1. **#305 正文的决策计数自相矛盾**：FIXED 段写 "All 30 user decisions"，而正文 User Stories
   实际是 35 条（本票实测计数）。修改 #305 正文属票面变更（AGENTS §9.1.1），本票只登记、不代改。
2. **`max_steps` 的失败归因面是「前向失配」，不是当前缺陷**：
   `docs/adr/0033-run-failure-attribution-surface.md`（`max_steps_exceeded` 作为 `run/failed` 载荷）
   与 `docs/BACKEND_CONTRACT_STREAMING_UI.md`（同）描述的是**当前实现**，在实现未改前**仍然为真**；
   但 T3/T4 落地后命中 local fuse MUST 落 `run/paused`（`02 §5.1–§5.2` 已冻结），届时这两份文档
   若不随合同更新，前端会照**相反**合同实现——2026-09-17 的 #222 审查（台账
   `docs/review_ledger.d/031-1a4c2fb-864fb15.tsv`）已经发生过一次同类问题，那一次是当场同步。
   **归属未定**（候选：`#320` 的 contract 面，或在实现票票面明确），按 §9.1.1 本票既不擅自改这两份
   文档、也不替别的票派活，交用户裁决后登记。
3. **两个事件名尚未进入机器可读枚举**：`run/paused` / `run/resumed` 的枚举条目要等实现票把常量加入
   `session/event.py` 并重新生成 `docs/EVENT_VOCABULARY.md` 才出现；在那之前 `03 §3.4` 是它们的权威定义。

---

## 5. 本票（#306）的证据与门禁

- **交付面**：本 ADR + `02/03/04/10/11/12` 正式规格章节 + `README.md` 索引口径 + `14_IMPLEMENTATION_ROADMAP.md`
  依赖指针 + 两笔前置摄入（#305 正文副本、调研报告）。**零** `src/**` / `tests/**` / `web/**` 改动。
- **门禁**：`git diff --check`、`ruff check .`、覆盖闸门 `scripts/check_review_coverage.py` exit 0、
  `scripts/gate0.py` 裸全量落盘 `docs/gate/<sha>.json`；规格/链接/Markdown 校验器**仓库内不存在**
  （实测：`scripts/` 无此类脚本），故该项以「逐文件相对链接与锚点核对」+ `git diff --check` 兑现并如实登记。
- **审查**：独立 Standards 轴 + Spec 轴（Spec 轴按票面要求核「#305 与正式规格语义等价 + 正式规格
  优先级 + #287 复用边界 + 凭证零泄漏」）。审查范围、结论与 findings 处置写在台账行与 tracker 段。

---

## 6. References

- `docs/PRD_AGENT_RUN_BUDGET_PAUSE_RESUME.md`（= #305 正文）
- `docs/research/2026-09-23-agent-tool-loop-termination-benchmark.md`
- 正式规格：`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/{02,03,04,10,11,12}.md`、`README.md`、`14_IMPLEMENTATION_ROADMAP.md`
- 既有 ADR：ADR-0004（存储分层）、ADR-0014（同指纹护栏）、ADR-0017（Resume/Replay/Fork）、ADR-0039（ToolExecutor 绝对 deadline）
- 事件名的唯一事实源与生成物：`src/agent_harness/session/event.py` → `docs/EVENT_VOCABULARY.md` / `web/src/generated/event-types.ts`
