# Issue #521 设计方案：事件干预语义（IMP-02）与 MCP Elicitation（IMP-10）

- 状态：Design / 待裁决（不含施工授权）
- 关联规格：`SPEC_ROOT/04_TOOL_RUNTIME.md`、`SPEC_ROOT/09_MCP_SKILLS_KNOWLEDGE_WEB.md`、`SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`
- 关联 ADR：`docs/adr/0012-mcp-client-integration.md`（D1）、`docs/adr/0011-skills-progressive-disclosure.md`（装配期边界）
- 关联票：#447（notification-only 事件缝）、#447 依赖链见 §7
- 冻结不变量：本设计只在 #7 / #10 / #11 / #18 / #21 约束内成立，越界处显式降级或 DEFER

---

## 1. 背景与问题

**IMP-02（事件无干预语义，只读总线）**：本仓的 `SessionEvent` 是 append-only 事实，运行期只被投影/订阅消费用于**观察**（JSONL、SSE、Web 投影），Capability 也只在**装配期**注入工具；换言之，运行期“看得见”一次 `tool_call`，却没有任何受支持的位置去**干预**它（拒绝 / 改写参数）——可观测，但不可干预。

**IMP-10（MCP 无 elicitation）**：MCP Client 只实现了 `tools` 原语，服务端在工具调用过程中**无法**向用户请求结构化补充输入；`mcp/client.py` 中不存在 `elicit`/`sampling` 路径，官方 spec 的 elicitation 能力完全缺席。

一句话对照：IMP-02 是**本仓自身的运行期缺口**（缺少受控干预缝），IMP-10 是**协议能力缺口**（MCP 侧缺 elicitation），两者共用同一个“在运行中把人/外部决策接进来”的语义，因此可设计为**同一决策通道**，但必须与只读观测严格分管道。

---

## 2. 事实依据（实码核实）

| # | 事实 | 证据（路径:行号） |
| --- | --- | --- |
| F1 | 统一 Tool 路径已冻结：`Contract → Registry → Validation → Permission → Scheduler → ToolExecutor → Ledger → ToolResult → SessionEvent`，任何 Tool 不得绕过 | `goal/.../04_TOOL_RUNTIME.md:7-22`（“Local / Knowledge / MCP / Web / SubAgent Tool 均不得绕过”在 :22） |
| F2 | Executor 已把三阶段固化并有唯一接纳点：lookup(:320-333) → validation(:338-355) → deadline(:366-390) → quota(:392-411) → approval(:413-422) → **接纳点**(:424-431) → Ledger(:433-474) | `src/agent_harness/tooling/executor.py:282`（`execute` 定义）、`:298-305`、`:320-333`、`:338-355`、`:413-422`、`:424-431` |
| F3 | 权限/审批已在 Executor 内必经；无回调默认拒绝（安全默认） | `executor.py:1051-1106`（`_check_approval`）、`:1093`（`await self._approval_callback(request)`）、`:1081-1091`（无回调 → `PERMISSION_DENIED`） |
| F4 | `ApprovalCallback` **已是 awaitable**（Phase 5 async 化的现成决策缝） | `src/agent_harness/tooling/approval.py:77`（`ApprovalCallback = Callable[[ApprovalRequest], Awaitable[ApprovalResponse]]`） |
| F5 | 当前存在两条独立的 async 审批等待实现，未被统一成“通用决策通道” | `src/agent_harness/session/approval.py`、`src/agent_harness/tooling/approval_queue.py`（同一 `ApprovalCallback` 的两个消费端） |
| F6 | MCP Client 只做 tools：无 `elicit` / `sampling` | `src/agent_harness/mcp/client.py:98-360`（类内仅 `connect`/`list_tools`:264/`call_tool`:283/`aclose`）；读循环对 server notification 原样忽略（`:269-281`） |
| F7 | MCP 接入 = Capability Provider，工具经 `ContributesTools` 进统一 Registry（零旁路） | `src/agent_harness/mcp/capability.py:35`、`:51`（`build_mcp_capability`）；`docs/adr/0012-mcp-client-integration.md:16`（D3） |
| F8 | ADR-0012 D1 冻结：V1 只做 tools，`resources/prompts/elicitation/roots` 全部 **DEFER** | `docs/adr/0012-mcp-client-integration.md:14` |
| F9 | 无 `EventBus` 类；Capability 只在**装配期**接线，运行期无事件订阅缝 | 全仓 `grep class EventBus|EventBus` 零命中；`src/agent_harness/capability/wiring.py:640`（`wire_capabilities` 装配期）、`:615-621`（静态 wiring 表） |
| F10 | MCP SDK 复用裁决 = `REUSE + ADAPT`（transport/protocol/discovery 归 SDK，Adapter 自研） | `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md:83-87`；`SPEC_ROOT/09_...md:19-24`（“不自研 wire protocol”） |
| F11 | #521 的既有审计结论：readonly 观测总线 ≠ 缺 mandatory 拦截；notification(#447) 与 decision hook 分开；启用 elicitation 需先改 ADR-0012 D1 | `docs/research/2026-10-03-open-issues-audit.md:154` |
| F12 | #447 范围被明确限定为 **notification-only**，不纳入 #521 决策 hook / 权限改写 | `docs/research/2026-10-03-open-issues-audit.md:158` |
| F13 | Pi 的现成机制：两管道判据“Agent 要不要读返回值”——决策钩子 await+读返回值，通知钩子同步广播不收集；`emitToolCall` 唯一不包 try-catch（扩展抛错即 block，fail-closed） | `docs/agents/pi-research-2026-09/synthesis-report.md:50-59`、`:73,75`；`a1-chapters-1-5.md:250-251`；`a2-chapters-6-10.md:60`（`runner.ts:1233-1251`） |

---

## 3. 方案依据（§6.1）

设计前已按 §6.1 核对 `docs/agents/reference-sources.md` 与审计来源表，采用**两个独立成熟来源** + 一个内部同类参考。判定与引用：

### 来源 A — Claude Code Hooks（外部一手文档，机制来源）

- 引用：`docs/agents/reference-sources.md:54-56`（ZCode/CodeBuddy Hooks 同族规范列于此）；审计来源表 `docs/research/2026-10-03-open-issues-audit.md:102`（`hooks` 条目）、`:154`。
- 可借鉴机制：**`PreToolUse` 可拦截**——可 deny / ask / allow，并可**改写工具参数**；**`PostToolUse` 仅反馈**（工具已执行，不能回滚、不能改变已发生的结果）。
- 契合点：与本仓“干预只能在执行**前**、`after` 必须只读”的既定边界一致；参数改写后必须重新过校验与权限，避免“钩子授予越权”。
- 复用判定：**PORT DESIGN**（借机制，不引依赖）。理由：本仓权限已是 Executor 必经路径（F2/F3），Hook 只作为 Executor 内的可选**前置缝**，不做第二权限路径。

### 来源 B — MCP 2025-06-18 Elicitation 官方 spec（外部一手规范）

- 引用：审计来源表 `docs/research/2026-10-03-open-issues-audit.md:288-294`（`mcp_elicitation`，`DEFER`，“启用前须决策”）；规范地址 `https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation`。
- 可借鉴机制：**能力协商**（客户端显式声明才可用）+ **三态响应 `accept` / `decline` / `cancel`**，由服务端发起、客户端将请求转交用户并回传结构化数据。
- 契合点：三态天然是“人/外部决策”的输入，可复用本设计 §4 的统一决策通道；且 elicitation 的返回值**不构成副作用证据**，不能用于 Recovery 判定。
- 复用判定：**REUSE + ADAPT**（复用官方 MCP Python SDK 的协议实现，仅 ADAPT 一个路由到决策通道的回调；**不自研协议**，F10）。当前状态维持 **DEFER**，启用须先改 F8。

### 来源 C — Pi 运行期钩子（内部同类参考，非独立外部来源）

- 引用：`docs/agents/pi-research-2026-09/synthesis-report.md:50-59`；`runner.ts:1233-1251`（经 `a2-chapters-6-10.md:60` 核实）。
- 结论：两管道按“是否读返回值”分界是本设计的直接对照；Pi 的 fail-closed（扩展抛错即 block）在本仓是**可配置选项**而非默认，且本仓权限不应退回“扩展可装可不装”。

> 说明：来源 A/B 为两个**独立**来源（外部产品文档 + 外部规范），满足 §6.1“至少 2 个独立来源”；来源 C 仅作对照不单独计入。未使用仓库外未核实笔记。

---

## 4. 事件干预语义设计

### 4.1 两条管道严格分离

| 维度 | notification 管道 | decision hook 管道 |
| --- | --- | --- |
| 定位 | 只读观测总线（订阅事件） | 执行前受控干预（allow/deny/rewrite） |
| 调用语义 | fire-and-forget，**同步广播、不收集返回值** | **awaitable，读返回值**，串行等待 |
| 返回值 | 无（返回值被丢弃） | `ALLOW` / `DENY(reason)` / `REWRITE(args)` |
| 失败策略 | 隔离，绝不影响 Tool 结果与 Core | 超时/异常按 fail-open 或 fail_closed（见 4.5） |
| 影响工具结果 | 否（只读） | `DENY`/`REWRITE` 可改变是否执行/入参 |
| 覆盖事件 | 全部 SessionEvent + 运行期通知（含 after-tool 反馈） | 仅 `tool_call` 前置 |
| 归属票 | #447（子集） | 本设计新增，独立边界 |

**铁律**：`after`（工具已执行后）**只能走 notification 管道**，永远不得进入 decision 管道——工具结果一旦落盘即为 append-only 事实，不接受回写或拦截（对照来源 A 的 `PostToolUse` 仅反馈）。

### 4.2 干预缝位置（tool_call 前置）

在统一路径（F1/F2）内，新增**唯一一个** decision 缝，位置：

```text
Registry.get(name)  →  [ BEFORE-TOOL DECISION HOOK ]  →  Pydantic Validation
   (executor.py:320)        (allow/deny/rewrite)          (executor.py:338)
                          →  deadline  →  quota  →  Permission/Approval  →  接纳点 → execute
                             (:366)      (:392)      (:413 / :1051)        (:424)   (:532)
```

- 选在 **lookup 之后、Validation 之前**：Hook 需要拿到已解析的 `Tool`（`side_effect` / `permission` / `resource_keys` 元数据），但**必须在 Validation 之前**，这样改写后的参数才能被重新校验。
- `DENY` 出口与既有“准入前拒绝”同族：`execute` 次数 = 0、不占配额、`budget_delta` 记 0（沿用 `executor.py:_rejected_delta` 与 `04 §9.1`），结果 `tool_call_id` 与 `tool/result` 正常配对。
- `ALLOW` = 原样放行，继续 Validation → Permission。
- `after` 只通过 notification 管道广播，不产生第二个 Executor 阶段。

### 4.3 参数改写：必须重走 Validation → Permission → Scheduler（不变量 #7）

- `REWRITE(new_args)` 后**不从 lookup 起点重来**，而是回到 **Validation**重新走：`Validation → deadline → quota → Permission/Approval → 接纳点`，即改写后的参数必须重新过 `args_schema.model_validate`（`executor.py:338-355`）与 `_check_approval`（`:1051-1106`）。改写不合法 → `INVALID_ARGUMENT`，`execute` 次数 = 0。
- **rewrite 不得提升权限**：Hook 返回值**只能携带新的 args**，不能设置/覆盖 `permission`、`side_effect`、`policy`。权限判定仍由 `_check_approval` 依据**工具静态元数据 + Session policy**计算；Hook 无法凭返回值抬高授权级别（对照来源 A 的 allow/ask 边界，及 F3 安全默认）。
- **Scheduler（批次并发决策）**：`execute_batch` 的并发/串行判定基于 `tool.side_effect` 静态枚举（`executor.py:_decide_mode`，:1028-1049），改写不改变工具静态分类，故批次模式不变；但若改写了会影响 `resource_keys` 的参数，则必须**重新推导资源键**，在无法证明“无资源冲突”时保守回退串行——守住不变量 #10（并发基于显式依赖与资源冲突）。V1 最小实现：只要存在 rewrite 型 Hook，批次层对该工具保守串行。

### 4.4 单一执行路径（不变量 #7 / #18）

- decision hook 是 **Executor 内的可选前置阶段**，不是新的审批/权限层，也不在 Agent Loop 内做特判（不变量 #18：MCP/Knowledge/Web/Coding 仍是 Capability/Tool）。
- MCP / Local / Coding 等所有工具**共用同一缝**；Hook 不区分工具来源，避免出现第二条隐藏执行路径。
- 审批（`ApprovalCallback`）职责不变，仍在 Hook 之后、接纳点之前执行；Hook 与审批是**两个不同问题**：Hook 是“是否允许这次形态的调用”，审批是“当前 policy 下高危工具是否放行”。

### 4.5 超时与异常 fail 策略

- decision hook 受**有界超时**约束（可配置，默认取保守小值；具体数值待实现期定，不在本设计冻结）。
- **默认 fail-open**：Hook 超时或抛异常 → 视为 `ALLOW`，按**原始参数**继续（改写未完成即不应用，绝不部分应用）。理由：Hook 是可选能力，故障不得拖垮 Core（不变量 #21）。
- **可选 fail_closed**（逐 Hook 配置）：超时/异常 → 视为 `DENY`（准入前拒绝、零执行）。用于安全敏感部署，对照来源 C 的 fail-closed 例外，但在本仓**显式化、非默认**。
- 未注册任何 decision hook 时：**无 Hook 语义**，行为与现状逐字节一致。

### 4.6 性能预算（未注册 Hook 零开销）

- Executor 持有一个可为 `None` 的 decision hook 引用；`if self._decision_hook is None:` 直接跳过，**零 await、零分配、零事件**，热路径只多一次判空。
- notification 管道无订阅者时，`emit` 为 O(1) no-op；订阅者存在时用**有界队列**，慢订阅者不阻塞产生方（背压/丢弃策略需可观察计数），不产生无界积压（不变量 #21）。
- 性能回归需有基准断言：未注册 Hook 的 `execute` 路径相对基线无新增 await 点（见 §8 AC-1）。

---

## 5. MCP Elicitation 设计

### 5.1 启用条件（前置门）

- MCP elicitation 当前被 **ADR-0012 D1 明确 DEFER**（F8）。**启用前必须先修订 ADR-0012 D1**，并取得用户明确批准（AGENTS.md §9.1.1：变更已批准决策需用户决策）。本设计**不自动解除 DEFER**。
- 即使批准启用，也遵守 §7 不变量与下面的复用边界。

### 5.2 复用判定：REUSE + ADAPT 官方 MCP SDK，不自建协议

- 复用官方 MCP Python SDK 的 elicitation 协议实现（能力协商、请求/响应消息、三态映射），本仓只 ADAPT：
  - 一个 **client 侧 elicitation 回调**，把 SDK 收到的 elicitation 请求路由到 §4 的**统一决策通道**；
  - SDK 侧 `accept` 数据经 `jsonschema` 校验后再回传（对齐 ADR-0012 实现注记：MCP 任意 JSON Schema 用现有 jsonschema 适配，不硬转 Pydantic）。
- **不自研 wire protocol**（对齐 SPEC 09:20-21、F10）。

### 5.3 elicitation 复用 IMP-02 决策通道

- MCP server 的 elicitation 请求 = 一次 awaitable 决策请求，投递到与 `ApprovalCallback`（F4）**同族的决策通道**；用户/前端 `accept` 回传结构化数据，`decline` / `cancel` 分别映射为 SDK 三态。
- **三态不是副作用证据**：elicitation 的 accept/decline/cancel 只能说明“用户说了什么”，**不能**用于 Recovery/Reconcile 对副作用是否发生的判定（对齐审计 `mcp_elicitation` 结论）。
- 不新增第二套 Session 真相：elicitation 请求/响应作为 SessionEvent 落盘、可投影，但不得成为独立状态库（不变量 #22 精神）。

### 5.4 能力协商与默认关闭

- 仅当决策通道已配置且 elicitation 开关启用时，才在初始化握手声明 elicitation 能力；否则**不声明**（server 不会发 elicitation 请求）。
- 默认关闭 = 现网行为零变化。

### 5.5 sampling 保持 DEFER

- MCP `sampling`（服务端反向请求模型补全）**本设计不启用、不设计落地**，维持 DEFER。理由：它把模型调用主权交给第三方 server，涉及预算与权限面，须独立评估，不在 #521 范围。

---

## 6. 待裁决项（需用户拍板）

### (a) 只读观测总线是否需要 mandatory 拦截能力（notification 与 decision hook 分开）

| 选项 | 内容 | 评价 |
| --- | --- | --- |
| A（推荐） | notification 总线保持**纯只读**；decision hook 作为**独立管道/独立票**，仅限 tool_call 前置 | 与来源 A/B、审计 F11 一致；不把拦截塞进只读总线，避免重造权限第二路径 |
| B | 在 notification 总线内建 mandatory 拦截（一个管道可读返回值、可 block） | 破坏“只读”语义与不变量 #7/#11，观测者故障会变执行路径故障 |
| C | 同一 seam 同时提供两种能力，仅靠调用约定区分 | 约定不构成边界，易被误用为权限旁路，不推荐 |

**推荐 A**，理由：审计已判定“readonly 观测总线 ≠ 缺 mandatory 拦截”，把两者混成一个可写管道会破坏单一执行路径与运行时权限边界。

### (b) MCP elicitation 是否现在启用

| 选项 | 内容 | 评价 |
| --- | --- | --- |
| A（推荐） | 维持 DEFER，本次只交付设计；待 ADR-0012 D1 修订获批后再启用 | 尊重已冻结决策；启用需先改 ADR（§9.1.1） |
| B | 立即启用（先改 ADR-0012 D1 并获批） | 改动已批准决策，须用户明确授权；协议与安全面需单独验收 |
| C | 永久 DEFER，MCP 只保留 tools | 放弃官方能力，但无技术必要性，不推荐 |

**推荐 A**，理由：#521 是 P0 但 elicitation 的启用是**决策变更**而非实现缺口，先设计、后按流程解锁，避免绕过冻结 ADR。

---

## 7. 与 #447 的边界

- **#447 是 notification 管道的子集**：#447 的“Capability 运行期事件缝（先限通知型订阅）”= 本设计 §4.1 中 notification 管道的只读、fire-and-forget 部分；#447 明确**不纳入** decision hook / 权限改写（F12）。
- 本设计对 #447 的补充仅是**同管道边界内的约束**：fire-and-forget 仍需强引用、有界队列、卸载清理、异常隔离，失败不得拖垮 Core（与 F12 一致）。决策 hook 是本设计新增的**独立管道**，不与 #447 合票。

**不变量 #7（统一执行路径）不被破坏的论证**：
- decision hook 只作为 Executor 内的一个前置阶段（§4.2），所有 Tool 仍走同一条 `Validation → Permission → 接纳点 → execute → Ledger → ToolResult → SessionEvent`；不存在第二条调用路径。
- rewrite 后**重走 Validation → Permission → Scheduler**（§4.3），且 Hook 不能设置权限/副作用字段，权限仍由 `_check_approval`（F3）唯一裁决。
- after 干预被禁止进入 decision 管道（§4.1），杜绝“对已执行结果回写”的旁路。

**不变量 #18（Capability/Tool 不写进 Agent Loop 特判）不被破坏的论证**：
- Hook 缝位于 **Tool Runtime / Executor** 层，MCP/Knowledge/Web/Coding 仍作为 Capability/Tool 接入，Agent Loop 不感知 Hook 存在，也不为某类工具加特判。

（附：不变量 #21 由 notification 的故障隔离与 decision 的默认 fail-open 共同保证。）

---

## 8. 验收标准（可测断言式）

**A. 事件干预语义**

- AC-1（零开销）：未注册 decision hook 时，`ToolExecutor.execute` 行为与基线逐字节一致；基准/结构断言证明热路径未新增 await 点与分配（对照 §4.6）。
- AC-2（deny 拦截）：注册返回 `DENY` 的 before-hook 后，被拒调用 `tool.execute` 调用次数 = 0、无 Ledger `RUNNING`、`budget_delta.tool_calls == 0`、结果与 `tool_call_id` 正确配对、错误码确定（复用准入前拒绝族）。
- AC-3（rewrite 重校验）：`REWRITE(new_args)` 后参数经 `args_schema` 重新校验；非法改写 → `INVALID_ARGUMENT` 且 `execute` 次数 = 0。
- AC-4（rewrite 不提权）：hook 无法通过返回值提高 `permission`/`side_effect`/`policy`；改写为更高危参数后，审批关卡仍按 Session policy 对**改写后参数**裁决（可构造“改写前可过、改写后需审批”的用例证明关卡生效）。
- AC-5（after 只读）：after/工具结果不得被 notification 订阅者或任何 Hook 修改；已落盘 `TOOL_RESULT` 逐字不变。
- AC-6（fail 策略）：decision hook 超时/抛异常时，默认 fail-open → 按原始参数继续；配置 fail_closed → 准入前拒绝、零执行；rewrite 未完成绝不部分应用。
- AC-7（notification 隔离与有界）：订阅者抛异常/挂起/缓慢时，Tool 结果与 Core 不受影响；有界队列不无界增长；卸载订阅后无强引用泄漏（可测对象计数/引用回收）。
- AC-8（唯一路径）：同一 MCP/Coding 工具在启用 Hook 后，仍可观察到完整 `Validation → Permission → Ledger → ToolResult` 链（不出现旁路证据）。

**B. MCP elicitation（仅在其启用获批后生效）**

- AC-9（协商）：仅当开关启用且决策通道配置时，握手声明 elicitation 能力；默认关闭时**不声明**、行为与现状一致。
- AC-10（三态映射）：`accept` 返回的数据经 `jsonschema` 校验后回传；`decline` / `cancel` 正确映射；非法 `accept` 数据被拒绝且不导致 run 崩溃。
- AC-11（非副作用证据）：elicitation 三态在任何 Recovery/Reconcile 判定中**不**被当作副作用发生与否的证据（负控用例钉死）。
- AC-12（失败隔离）：elicitation 通道故障不拖垮基础 Agent（不变量 #21），单 server 故障按 OPTIONAL_RUNTIME 降级。
- AC-13（sampling 未启用）：`sampling` 仍 DEFER，无新代码路径被触发。

**C. 流程/决策**

- AC-14（决策门）：MCP elicitation 启用前，ADR-0012 D1 的修订已落盘并获用户明确批准；否则保持 DEFER。

---

## 9. 范围与非目标

### 范围（本设计覆盖）

- IMP-02：明确 notification 与 decision 两条管道的语义、干预缝位置、改写后重走校验/权限、fail 策略与性能预算。
- IMP-10：MCP elicitation 的**设计**（启用条件、REUSE + ADAPT、复用 §4 决策通道）。
- 与 #447 的边界划分。

### 非目标（本设计明确不做）

- 不授权、不实施任何代码改动；本文件为设计方案（施工须另按 ticket 授权，§4.4）。
- 不在 Agent Loop 内做 Capability 特判；不新增第二套权限/审批路径；不引入 EventBus 之外的第二套 Session 真相。
- 不自研 MCP wire protocol；不实现 MCP `sampling`；不实现 `resources`/`prompts`（仍在 ADR-0012 D1 DEFER）。
- 不做运行期工具表动态增删（对齐 Manus “mask, don't remove” 与 `synthesis-report.md:54`）。
- 不改动 ADR-0012 D1（是否启用 elicitation 归用户裁决，见 §6b）。
- 不为 `after` 阶段提供任何可改变结果的能力（只读反馈）。
- 不扩大 V1 干预范围：decision hook 仅覆盖 `tool_call` 前置，不覆盖 prompt 干预（prompt 干预若需，另立项并先过 §7 不变量审查）。

---

以上为 #521 设计方案全文。落盘后建议：先就 §6(a)(b) 两项请求裁决；两项 decision 的结论将决定本设计是拆为“notification（#447）/ decision hook（新票）”两条施工线，还是继续停在设计阶段。
