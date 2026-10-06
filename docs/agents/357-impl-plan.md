# W-13（#357）施工计划（修订 A · 纯规划稿）

- **分支**：`codebuddy/357-w13-recovery-reconcile`（worktree `/home/hatch/workspace/intelligence-agent-wt-357`），HEAD `3d3a1056`，工作树干净。
- **日期**：2026-10-06。
- **性质**：**只做计划**。本阶段不写产品代码、不跑测试、不关单；本阶段唯一落盘文件 = 本文件。
- **设计权威**：`docs/agents/357-research.md` §9（修订 A，supersede 第一轮 A/B/C）；§8 为 UNKNOWN 七家深挖证据。用户 2026-10-06 亲口批准**修订 A**。
- **合同基线**：#547（后端裁决合同，已合入 main，`RecoverRequest.decisions` + `DecisionsReconcileCallback` + token-CAS）**复用、只做加法扩展，不造第二套**。
- **写盘纪律**：全文先在内存构好，一次写盘（`.tmp` → 原子替换），不分段 append（SDD §8.9）。

---

## 0. 五项启动检查表（本 Task 实读证据）

| 前置项 | 实际读取依据（本次实读） | 状态 |
| --- | --- | --- |
| Vision 相关原则 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md` §2.3 Recoverable（L28–46，含 L46「Tool 状态无法确定 MUST 进入 NEED_RECONCILE/人工决策，不允许盲目重放」） | READY |
| 当前任务规格 | `07_STORAGE_PERSISTENCE_RECOVERY.md` §6 Reconcile（L97–112）、§7 Tool-specific Recovery（L114–131）、§8 Message/Event Consistency（L133–142）、§9 Resume 顺序（L144–155）、§10 Acceptance（L157–172）；`03_SESSION_EVENT_MODEL.md` §5 Resume（L158–195，L193「对账优先于恢复」）与 §3.1 L78 Ledger 分层；`web/PRODUCT.md` 全文（视觉/不变量 #22 权威）；`gh issue view 357` 全文（AC + 2026-10-03 审计节） | READY |
| Reuse 相关判定 | `13_OPEN_SOURCE_REUSE_MATRIX.md` §5 允许 BUILD（L118–136，点名 Operation Ledger + reconcile policy 属 BUILD）、§6 禁止复制方式（L138–144）；`docs/agents/357-research.md` §2/§8（七家 PORT DESIGN/ADAPT/BUILD 判定与 License）与 §9 | READY |
| Phase 依据 | `14_IMPLEMENTATION_ROADMAP.md` Phase 14「Resume / Replay / Fork 完整化」（L226–239）；`docs/PHASE_STATUS.md` 当前焦点（W 批 frontier，L43）；`docs/SDD_TICKET_TRACKER.md` L6919（#547 关单裁决「页面 UX 归 #357 W-13」）。#357 依赖 #337/#356/#350 均已 CLOSED（research §1 已核实） | READY |
| 本任务触发细则 | `AGENTS.md` 全文（543 行）；`docs/SDD_WORKFLOW_PROTOCOL.md` §8.9 写盘纪律（L828–849）；`docs/agents/implementation-discipline.md` 全文；`docs/agents/reference-sources.md`（reuse 调研已由 `357-research.md` §2/§8 按 §1.3 落块，本轮引用不重做）；本票为**规划任务**，不触发 §4.1 审查 / §4.2 Debug playbook 正文 | READY |

**结论**：五项全 READY，无必读阻塞。

---

## 1. 现状核实（只读锚点，行号为 HEAD `3d3a1056`）

### 1.1 #547 合同链路（复用对象，逐点核实）

| 环节 | 位置 | 现状 |
| --- | --- | --- |
| HTTP 请求体 | `src/agent_harness/web/app.py:597` `RecoverDecisionRequest{tool_call_id,verdict}`；`:608` `RecoverRequest{decisions:list}` | decisions 可选、空=纯恢复尝试 |
| 端点 | `app.py:2677` `recover_session` → `service.recover(session_id, decisions=...)` | 409→`http_error`；`domain_errors.py:423-435` 把 `pending_decisions` 升级进 `detail` 对象 |
| 领域入口 | `session/service.py:3392` `recover()` | 预检（非法值/重复/非目标→`InvalidDecision` 422；未覆盖全→`RecoveryConflict` 409 带 `pending_decisions`；被拒零写入）→ 构造 coordinator |
| 待裁决清单 | `session/service.py:3490` `_reconcile_pending()` | 字段仅 `{tool_call_id,tool_name,state}`；判据 = 悬空只认 RUNNING/UNKNOWN/NEED_RECONCILE + 非悬空按 `storage.needs_reconcile` 全量 |
| 回调桥 | `recovery/coordinator.py:174` `DecisionsReconcileCallback.resolve` | 返回 `ReconcileVerdict`；缺裁决→`ReconcileRequired`（fail-closed） |
| 裁决词表 | `recovery/reconcile.py:60` `ReconcileVerdict` | 四值：CONFIRM_SUCCESS/CONFIRM_FAILURE/RETRY/ABANDON |
| 提交链 | `coordinator.py:500-517` 锁外 `resolve` → 重锁 → `token.matches(current)` CAS → `_commit_reconcile`（`:753`） | `reconcile_meta={"verdict","reconciled_at"}` + `operation/reconciled` 审计事件 + 合成 tool/result |
| 裁决映射 | `coordinator.py:877` `_verdict_outcome` | 4 路穷尽（末路=ABANDON→CANCELLED） |
| 传输模型 | `session/service.py:564` `ReconcileDecision{tool_call_id,verdict}`（frozen dataclass） | verdict 用 str，合法性在预检 |

### 1.2 Operation Ledger / 扫描 / 进度

- `storage/operation.py:12` `OperationState` 词表：PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED/UNKNOWN/NEED_RECONCILE（**无 DEFERRED**）；`:94` `needs_reconcile()`；`:62` `unproven_meta()`。
- `storage/sqlite.py:132` `_ALLOWED_TRANSITIONS`：`NEED_RECONCILE → {SUCCEEDED,FAILED,CANCELLED}`（**无自环**）。
- `recovery/scan.py:84` `InterruptionScanResult{session_id,interrupted,recovery∈{recovered,needs_manual_reconcile,failed},detail}`；`:98` `scan_interrupted_sessions()`——**它会写盘**（append `run/interrupted` + 跑 reconcile），不是只读查询。
- 启动扫描在 `app.py:1560` lifespan 内执行，结果只 `logging.warning`，**未进任何 state / 未暴露端点**（`app.py:1546-1566`）。
- `session/service.py:3522` `scan_interrupted()` 返回扫描结果但同样仅日志消费。
- `access` **进度文件**：`session/progress.py:67` `PROGRESS_SCHEMA_VERSION="1"`；`ProgressDocument`（`:120`，含 `schema_version/session_id/source_event_seq/goal/acceptance/...`）；`progress_paths(root,session_id)`（`:106`）；`verify_progress_file`（`:947`，schema_version 不匹配→`invalid_schema`）。版本读取的单源就是头部 `schema_version` + `source_event_seq`。

### 1.3 ReconcileHint / probe 机制（关键发现）

- 生产者：多个 Tool 覆写 `reconcile_hint`（`tools/edit.py:70`、`glob.py:55`、`grep.py:70`、`git.py:168/231`、`read.py`、`inspect_artifact.py`、`read_artifact.py`、`knowledge/tools.py:82/156/237`、`memory/v2/tools.py:171/283`、`multiagent/tools.py:187`、`mcp/adapter.py:184`）。形状（`tooling/reconcile.py:17`）：`ReconcileHint{verifiable:bool=False, suggested_action:str|None}`——**纯建议数据，不是可执行的机器 probe**。
- 唯一消费者：`recovery/coordinator.py:695` `_hint_for()`，只在 `resolve(operation, hint)` 调用点（`:501-503`）使用。
- **关键发现（待补）**：生产路径的两处 `RecoveryCoordinator(...)` 构造（`service.py:3475`、`scan.py:155`）**都未传 `tool_registry`**，故 `_hint_for` 在生产恒返回默认 `ReconcileHint(verifiable=False)`。也就是说 `reconcile_hint` 目前只在测试里被消费——**合同 6「先查外部事实」要落地，必须最小化接线让 service 拿到工具 hint/replay_safe**（见 §2.4）。
- 消费面另有一处只读投影：`web/src/lib/projection.ts:976` 把 `operation/reconcile-required` 事件入 `reconcile_queue`，`components/StepDetail.tsx:781` 只读展示（`tool_name/args_identity/state`），**无提交 UI**。

### 1.4 前端锚点

- `web/src/lib/api.ts:65` `PendingDecision{tool_call_id,tool_name,state}`；`:75` `parsePendingDecisions()`（严格：缺键/类型不对/state 越三态→整组回落 `undefined`）；`:1239` `RecoverError`；`:1252` `recoverSession(sessionId)`——**不发送 decisions**，409 只解析 `pendingDecisions`。
- `web/src/hooks/useSession.ts:63` `RecoverState`；`:1493` `recover()`——409 只 `setRecoverState({conflict:true,message})`，**无处提交裁决**（`:432` 注释明文「本期只展示原因，不做裁决」）。
- `web/src/App.tsx:1138-1160` 恢复入口：单个「恢复会话」按钮（`.recover-btn`）+ 409 时 `.recover-conflict` 展示文本，无裁决表单。
- 组件目录 `web/src/components/` 已有 `PausedPanel`、`ApprovalCard`、`SessionList`、`MemoryPanel` 等面板先例；`App.tsx` 无 router，靠 `SessionMode` + 面板挂载/布尔开关组织。
- `web/e2e/d-recover.spec.ts` 已有恢复链路 e2e（含 409「需人工裁决」单独成形用例）；验收车道前提见 `docs/ACCEPTANCE_LANE_ENV.md`（`:5173` dev + `:8000` 后端，/api/capabilities 判据）。
- `#353`（W-09，OPEN）同改 `web/src/App.tsx` / `api.ts` / projection / reconnect——**同文件冲突风险**（见 §8）。

---

## 2. 修订 A 七条契约 → 代码改动映射

### 2.1 契约 1：默认安全动作先行，不问人

- **默认动作定义**（后端算、前端呈现）：
  - `replay_safe=True`（低风险、可安全重放）⇒ `default_action = "RETRY"`（安全重试；对齐 Pi「双 safe 才重跑」）。
  - `replay_safe=False`（高风险 UNKNOWN）⇒ `default_action = "DEFER"`（先跳过/稍后；对齐 #14、Codex「扣住不重发」、Trigger.dev「Crashed 不重试」）。
- **落点**：`session/service.py::_reconcile_pending`（:3490）为每条 pending 增 `default_action` / `risk_level`（见 §2.4 的 hint 端口）。后端不替用户裁决；`default_action` 只是前端「默认选中项」与「一键按默认处理」的驱动值。
- **不改** `_verdict_outcome` 的既有 4 路；DEFER 是新第 5 路（§3）。

### 2.2 契约 2：绝不做开放式技术问答

- **前端**（见 §5）：每张卡片标题用**后果语言**（抄 DSH `"Running tasks will be interrupted."` 形态，默认方向按 #14 重定为安全侧）；三选一单选（已生效/安全重做/先跳过），**默认选中最安全项**；批量「全部按默认安全动作处理」。
- **禁用文案**：绝不出现「这个工具调用到底生没生效？」（§8.5 Codex #50118 教训）。
- **后端**：为卡片提供机器可读的呈现字段（`pending_decisions` 增 `default_action/risk_level/probe`，只读展示，不改裁决语义）。

### 2.3 契约 3：裁决留痕 + 来源

- `session/service.py:564` `ReconcileDecision` 增 `source: str | None = None`（frozen dataclass 带默认值 ⇒ 向后兼容）。
- `recover()`（:3392）预检后把 `source` 与 `verdict` 一起交给回调桥；调用 `CONFIRM_SUCCESS` 时来源随裁决落 `reconcile_meta`（**不伪造自动验证**，第一轮 §3(c) 维持）。
- `coordinator.py::_commit_reconcile`（:753）在 `reconcile_meta` 里追加 `source`（有值时）。`ReconcileCallback` 契约**保持 `resolve()` 签名不变**（守 #547 核心链路），新增**非抽象**可选项 `source_for(operation) -> str | None`（默认 `None`），`DecisionsReconcileCallback` 覆写返回；协调器在 CAS 复核后用其写 meta。既有实现零迁移。
- 来源采集（前端）：可选项（如「我刚才亲眼看到结果了」/「我查了外部系统」/「查了文件/命令输出」）+ 自定义输入，降低小白门槛（§9.4-3）。

### 2.4 契约 6：先查外部事实（probe 展示）+ 接线

- **probe 数据源** = 既有 `Tool.reconcile_hint`（`verifiable` + `suggested_action`），**不新增 probe 算法**（issue「不做」：不重做 Tool-specific reconcile 算法）。
- **必要接线（最小）**：仿既有零副作用端口模式（`assembly.root_registry_tool_names` + `web/app.py:1199 _registered_tool_names_provider`），新增**零副作用投影** `root_registry_reconcile_info(...) -> dict[str, ReconcileInfo]`（读 `tool_cls(None).name` / `.reconcile_hint` / `.replay_safe`，**不实例化 sandbox、不 mkdir**），经新领域端口注入 `SessionService`（同 `registered_tool_names` 落位），供 `_reconcile_pending` 产出 `probe{verifiable,suggested_action}` 与 `default_action/risk_level`。该投影与 `build_runtime`/`_build_tooling` 同源，由既有 `tests/test_assembly_root_registry_names.py` 同型对账钉住。
- **诚实边界**：probe 是**建议**（「建议这样核对：…」），后端**无法**凭空知道「已查到/未查到」；UI 的「已查到/未查到」是用户核对后的如实自陈，**不伪造 probe 结论**（§9.4-6）。

### 2.5 契约 5：恢复列表

- **端点**：新增**只读** `GET /api/recovery/interrupted`（命名待定，见 §8），返回 lifespan 启动扫描的**快照**（`app.py:1560` 已拿到 `scan_results`，本轮在 lifespan 存入 `app.state`；**端点只读快照，绝不重跑 `scan_interrupted()`**——后者会写 `run/interrupted` + 跑 reconcile，违反「零写副作用」）。
- **四要素**（每行）：上次 Task（`derive_task_state`/`SessionSummary.first_user_message` 口径）、Run（无终态 run_id）、工作目录（workspace 锚）、进度文件版本（`progress.py` 头部 `schema_version` + `source_event_seq`）。四要素形态抄 Claude resume picker（时间/分支/标题/大小）。
- **加载门控**：严格 Spec 07 §9 顺序（events→workspace/sandbox→Ledger→reconcile→tool pair→context）加载完成后，才出现「继续」动作；未完成/版本不匹配时禁止启动模型。
- **fail-safe**：状态不可读时**默认亮 Resume**（抄 Cline `sdk-task-control-coordinator.ts:281-285`）；后端快照缺失/不可读 → 该行标记 `resume_available=true`（安全侧 affordance）。
- **#22 合规**：列表与卡片全部由后端端点驱动，前端不缓存第二套真相。

### 2.6 契约 4 + 7：前端提交 UI + UNKNOWN 一等 + replay_safe

- 契约 4 见 §5；契约 7 见 §4（`Tool.replay_safe` 默认 `False`；`NEED_RECONCILE` 已是一等状态）。

---

## 3. #547 合同「只做加法」边界论证（含 DEFER）

### 3.1 五处加法，逐一论证「同一合同」

| # | 加法 | 复用同一… | 为什么不是第二套 |
| --- | --- | --- | --- |
| A | `ReconcileVerdict.DEFER` 第 5 值（`reconcile.py:60`） | 同一端点 `POST /recover`、同一 `DecisionsReconcileCallback`、同一 token-CAS 链、同一 `reconcile_meta`/`operation/reconciled` 审计 | 只是既有词表加成员；四值语义逐字不变；预检 `ReconcileVerdict(decision.verdict)` 天然接纳 |
| B | `ReconcileDecision.source`（`service.py:564`，带默认） | 同一裁决入口 | frozen dataclass 加**带默认值**字段 ⇒ 旧构造点/旧请求零迁移 |
| C | `pending_decisions` 增 `default_action/risk_level/probe` | 同一 409 载荷 | **只读展示字段**，不改变任何一个 verdict 的语义；前端解析对缺省宽容 |
| D | `ReconcileCallback.source_for()`（非抽象、默认 None） | 同一回调契约 | 不破坏 `resolve()` 签名；既有实现零迁移 |
| E | 只读 `GET /api/recovery/interrupted` | 复用 `InterruptionScanResult` + lifespan 快照 | 纯读、零写；不新增恢复编排 |

### 3.2 DEFER 的语义（**本票首要未决点**）

修订 A §9.4-3：DEFER = 「先跳过，稍后再说」；**Ledger 记 pending、可审计、可稍后处理**（Temporal/Codex 式「标记待查稍后」）。

**语义边界（DEFER vs ABANDON）**：

- `ABANDON` = 用户放弃：Ledger→`CANCELLED`（终态），合成明确取消结果，调用已结清、不再出现在待裁决清单。
- `DEFER` = 用户暂缓：**不结束调用**，Ledger **保持 `NEED_RECONCILE`**（记为 pending），落 `reconcile_meta={verdict:"DEFER",source,reconciled_at}` + `operation/reconciled` 审计；**仍在 `pending_decisions` 中**（可稍后重新裁决）。

**机制前提（须实现）**：

1. `_verdict_outcome` 增 DEFER 路：返回「结果仍不确定，用户选择稍后处理（DEFER）」的非重试结果语义，**Ledger 目标状态 = `NEED_RECONCILE`（不推进终态）**。
2. `storage/sqlite.py:132 _ALLOWED_TRANSITIONS` 需为 `NEED_RECONCILE` **增加自环**（`NEED_RECONCILE → NEED_RECONCILE`），否则无法在保持 pending 的同时写 `reconcile_meta`（当前表无自环）。
3. `_commit_reconcile` 对 DEFER 的**事件配对**取舍（见下）。

**未决（需实现前定夺，登记为风险 R1）**：DEFER 是否合成 `tool/result`？
- **推荐 D1（audit-only，保持 pending）**：不合成终态 `tool/result`，悬空调用**有意保留**直至用户最终裁决。理由：DEFER 的本义是「先不决定」；强行补一条结果会把「未定」伪装成「已定」。代价：与 `07 §8`「不能留下 dangling call」字面冲突 ⇒ 需在实现/审查中**明示豁免**（悬空 = DEFER 的诚实表示，非缺陷），不得静默。
- **备选 D2（abandon-like pairing）**：像 ABANDON 一样补一条非重试取消结果（消除悬空），但在 `reconcile_meta` 记 `DEFER` 并靠单独谓词维持 pending。代价：事件流「已取消」与 Ledger「pending」分歧，模型可能把 DEFER 读成取消。
- **推荐 D1**；D1/D2 的取舍在施工前与用户/设计确认（AGENTS §9.1.1 精神：不硬顶、不伪完成）。

---

## 4. Tool Contract 加 `replay_safe`（契约 7）

- `src/agent_harness/tooling/contract.py` `Tool`（:173，可选元数据区 :227 之后）新增属性：
  ```python
  @property
  def replay_safe(self) -> bool:
      """崩溃恢复时本工具被中断后「盲目重跑」是否安全。默认 False（unsafe）。
      形制取自 Pi durable `replay:"safe"`（双 safe 才重跑）；语义锚 #14
      （UNKNOWN 高风险不盲重跑）。reconcile_hint 只影响「可否外部核验」，
      本属性只影响「可否安全重放」，两者正交。
      """
      return False
  ```
- **判据**（`replay_safe=True` 的必要且充分条件）：`side_effect == READ_ONLY` **且**重执行幂等、无任何外部副作用（含远程），**且**不依赖会变的输入之外的状态。`reconcile_hint.verifiable` 不参与本判据。
- **声明为 `True` 的内置工具**（全为只读）：
  `read`（`tools/read.py`）、`grep`（`tools/grep.py`）、`glob`（`tools/glob.py`）、`git_status`/`git_diff`（`tools/git.py:137/199`）、`inspect_artifact`（`tools/inspect_artifact.py`）、`read_artifact`（`tools/read_artifact.py`）、`retrieve_knowledge`/`read_knowledge_source`（`knowledge/tools.py:46/124`）、`retrieve_memory` v2（`memory/v2/search_tool.py:37`）。
- **保持默认 `False`**：`write`/`edit`/`apply_patch`（MUTATING）、`bash`（副作用不可统一判定，`07 §7` 明文）、`update_plan`（写会话状态）、`remember`/`forget` memory（MUTATING）、`ingest_document`（MUTATING）、`MCPTool`（**即便 `_read_only=True` 也不声明**——远程副作用不可证，保守）、`DelegateTool`（多 Agent 副作用不可控）。
- **消费点**：§2.4 的零副作用投影读 `.replay_safe`，供 `default_action/risk_level`；`RecoveryCoordinator` 不自动重跑（#14 不变），`replay_safe` 仅驱动**用户可选**的默认方向与 UI 呈现。

---

## 5. 前端改动（最小化 App.tsx churn）

| 文件 | 改动 |
| --- | --- |
| `web/src/lib/api.ts` | `PendingDecision` 增可选 `args_identity?/default_action?/risk_level?/probe?`；`parsePendingDecisions` 对**新增可选键**容忍（缺省→undefined），`tool_call_id/tool_name/state` 仍严格；`recoverSession(sessionId, decisions?: RecoverDecisionInput[])` 发送 `{decisions:[{tool_call_id,verdict,source?}]}`；新增 `getInterruptedRecoveries()`（只读列表端点） |
| `web/src/hooks/useSession.ts` | `RecoverState` 增 `pendingDecisions: PendingDecision[] | null`；`recover()` 在 409 落地 pendingDecisions；新增 `submitRecoverDecisions(sid, decisions)`（复用 `shouldApplyRecoverResult` 守护，成功后整表重建=既有 200 路径）；新增 `loadRecoveries()`（只读列表） |
| `web/src/components/RecoveryDecisionPanel.tsx`（新） | 逐条裁决卡片：后果语言标题（非开放问答）、工具名/参数摘要/最后已知状态/probe「已查到·未查到」、**三选一单选默认选中最安全项**（DEFER for unsafe / RETRY for replay_safe）、来源可选项+自定义输入、「全部按默认安全动作处理」；提交 → `submitRecoverDecisions` |
| `web/src/components/RecoveryListPanel.tsx`（新） | 恢复列表：Task/Run/工作目录/进度文件版本四要素；严格 07 §9 顺序加载门控「继续」按钮；状态不可读默认亮 Resume；入口调 `loadRecoveries` |
| `web/src/App.tsx` | **只加最小入口**：一个打开「恢复」面板的按钮/菜单项（形制照 `MemoryPanel`/`ApprovePolicyPanel` 的面板挂载），并把 409 时的 `.recover-conflict` 文本升级为挂载 `RecoveryDecisionPanel`。尽量不整文件改动以降低与 #353 冲突面 |
| CSS（`web/src/index.css` / `web/src/styles/app.css`） | 新增 token（若有）必须**同时**改暗色 `:root` 与亮色 `:root[data-theme='light']`（AGENTS §15 / PRODUCT.md）；对比度 ≥4.5、字号地板 12px（`web/e2e/t-contrast.spec.ts`） |
| `web/e2e/d-recover.spec.ts` | 扩展：409→裁决卡片→提交→成功；默认选中项断言；列表页四要素 + Resume fail-safe |

**#22 合规**：面板只消费后端端点/409 载荷，不维护本地裁决真相；提交后以 200 返回的全量事件重建（既有 `projectHistory` 管线）。

---

## 6. TDD 红测清单（先失败 → 后通过）

方法：每例先写**会失败**的测试（红），确认失败因「新面未实现/旧行为存在」，再最小实现转绿（`AGENTS.md §9.4`）。

### 6.1 后端（`tests/recovery/`、`tests/web/`）

| # | 用例 | 红（未实现时如何失败） | 绿（实现后断言） | 依据 |
| --- | --- | --- | --- | --- |
| R1 | `Tool.replay_safe` 默认 False | `AttributeError` / 默认值缺失 | `Tool` 子类默认 `replay_safe is False` | 契约 7；#14 |
| R2 | 只读工具声明 `replay_safe=True` | 属性不存在 → 红 | read/grep/glob/git_status/git_diff/inspect_artifact/read_artifact/retrieve_knowledge/read_knowledge_source/retrieve_memory 全部 `True` | 契约 7；Pi 双 safe |
| R3 | 高危/MCP/delegate 保持 False | 误声明为 True → 红 | write/edit/apply_patch/bash/update_plan/memory 写/ingest/MCP（含 `_read_only`）/delegate 全 `False` | 契约 7；07 §7 |
| R4 | `ReconcileVerdict.DEFER` 被预检接纳 | `ValueError`（枚举无 DEFER）→ 422 红 | `recover(decisions=[{...,verdict:"DEFER"}])` 不抛 `InvalidDecision` | 契约 1；修订 A §9.4-3 |
| R5 | DEFER 落 pending + 留痕 | 无 DEFER 路 → 裁决被拒/状态错 | Ledger 仍 `NEED_RECONCILE`；`reconcile_meta` 含 `verdict=="DEFER"` 且 `source` 在；`operation/reconciled` 审计事件在 | 契约 1/3；Codex anomaly 机制 |
| R6 | DEFER 后仍在 `pending_decisions` | 若被当终态消除 → 红 | 再次 `recover()` → 409 且该 `tool_call_id` 仍在清单（可稍后处理） | 修订 A §9.4-3 |
| R7 | 全 DEFER 的 recover 返回 200 | 若仍 409 → 红 | 覆盖全部 pending 后返回事件数组、不 409（决策已记账） | 契约 1「不阻塞」 |
| R8 | `source` 向后兼容 | 未加字段/字段进签名 → 旧构造点报错红 | `ReconcileDecision("c1","ABANDON")` 合法；`source` 默认 None | 契约 3；#547 加法 |
| R9 | `reconcile_meta` 记 source | 无 source 落盘 → 红 | CONFIRM_SUCCESS+source → meta `verdict` 与 `source` 都在，**不伪造**自动验证字段 | 契约 3；第一轮 §3(c) |
| R10 | 回调 `source_for` 默认 None | 抽象化致既有实现破坏 → 红 | 旧 `ReconcileCallback` 子类不实现 `source_for` 仍可用 | 契约 3；#547 加法 |
| R11 | HTTP `RecoverDecisionRequest.source` 可选 | 形状校验拒未知字段/缺省 → 红 | 省略=合法；超上限（若采纳 2000 字符 cap）=422 | 契约 3；传输边界 |
| R12 | `pending_decisions` 增展示字段 | 字段缺失 → 红 | 每条含 `default_action/risk_level/probe` | 契约 2/6 |
| R13 | 默认动作由 `replay_safe` 决定 | 恒 DEFER 或恒 RETRY → 红 | replay_safe 工具→`RETRY`；unsafe→`DEFER` | 契约 1/7 |
| R14 | probe 只来自 `ReconcileHint` | 伪造「已成功」→ 红 | `probe.verifiable/suggested_action` 与工具声明逐字一致；无 hint→默认 `false/null` | 契约 6；§9.4-6 |
| R15 | 只读列表端点零写副作用 | 端点缺失/复跑扫描写盘 → 红 | `GET /api/recovery/interrupted` 返回快照；调用前后事件数不变、无新增 `run/interrupted` | 契约 5；扫描会写盘的反证 |
| R16 | 状态不可读默认亮 Resume | 不可读→隐藏 Resume → 红 | 快照缺失/读失败→该行 `resume_available=true` | 契约 5；Cline fail-safe |
| R17 | 重复提交仍 422（回归） | 新改动放宽 → 红 | 同 `tool_call_id` 两条裁决→`InvalidDecision` 422，零写入 | #547 回归护栏 |
| R18 | token-CAS 仍拒绝 stale（回归） | 新改动绕过 CAS → 红 | 等待期 Operation 变更→`RecoveryError`(409) | #547 回归护栏 |
| R19 | 真实 Kill：SQLite 写入成功后、ToolResult append 前 → CONFIRM_SUCCESS 无重复副作用 | 基线已有 `test_kill_after_terminal_write_recovers_without_duplicate_side_effect`（`tests/integration/test_kill_resume.py:124`）；扩展 decisions 路径 | kill 后重启：DB 仅一份副作用；经 `decisions=[CONFIRM_SUCCESS]` 恢复、配对恰一次 | 契约 1/3；Spec 07 §10；issue AC |
| R20 | 真实 Kill → 409 `pending_decisions` → 决策 → 成功（全环） | decisions 未接线 → 红 | 端到端：kill→recover 409（含新字段）→带 decisions 重发→200，`Operation ID`/`seq`/DB 状态可核 | 契约 4；issue AC |

### 6.2 前端（vitest）

| # | 用例 | 红 | 绿 | 依据 |
| --- | --- | --- | --- | --- |
| F1 | `parsePendingDecisions` 容忍新增可选键 | 新字段致整组回落 `undefined` → 红 | 含 `default_action/probe` 正常解析；缺省→默认；required 仍严格 | 契约 2/6 |
| F2 | 裁决卡片默认选中最安全项 | 默认落中间/最危险 → 红 | unsafe→DEFER 选中；replay_safe→RETRY 选中 | 契约 2/1 |
| F3 | `recoverSession` 发送 decisions | 请求体无 decisions → 红 | body 含 `{decisions:[{tool_call_id,verdict,source?}]}` | 契约 4 |
| F4 | `useSession` 暴露 pendingDecisions + 提交动作 | 状态里无 pendingDecisions → 红 | 409 后持有清单；`submitRecoverDecisions` 调 API 并走 200 重建 | 契约 4 |
| F5 | 恢复列表四要素 + 门控 + fail-safe | 无列表/无门控 → 红 | 显示 Task/Run/cwd/进度版本；加载未完不亮「继续」；不可读→亮 Resume | 契约 5 |

**合计**：后端 20 + 前端 5 = **25 条**（先红后绿）。

---

## 7. 验证计划（§14.10；本轮只列步骤，不执行）

1. **focused**：`tests/recovery`、`tests/web/test_run_pause_resume_api.py`、`tests/session/test_client_exit.py` 相关、新增用例；`ruff check .`；`tsc` + vitest（src 层）+ Playwright（`web/e2e/d-recover.spec.ts`）。
2. **重复提交 / token-CAS / 重启幂等**：R17/R18/R19/R20 覆盖；另跑「同一 session 连续两次全量 recover」确认幂等（事件配对自然跳过已修复项）。
3. **真实 Kill Test**：kill 点 = **SQLite Ledger 终态写入成功后、`tool/result` append 前**（复用 `tests/integration/test_kill_resume.py::test_kill_after_terminal_write_recovers_without_duplicate_side_effect` 的 `kill_stage="terminal"` 探针）。证据含：**SessionEvent seq**、**Operation ID**、**DB 状态**（`operations` 行 state + `reconcile_meta`）。重启后先查 DB：仅一份副作用。
4. **UNKNOWN 前 model/tool calls = 0**：未带 decisions 的 recover 报 409 时，断言零模型/零工具准入（issue AC）。
5. **实际系统核查**：`GET /api/recovery/interrupted` 前后事件数不变（证明只读）；`scan_interrupted()` 仅在 lifespan 跑一次。
6. **UI 真机验收车道**（步骤，不执行）：按 `docs/ACCEPTANCE_LANE_ENV.md` §5 三检查（`:8000` 是本分支后端 / `/api/capabilities` 期望 / 语料可触发）→ 截图恢复列表、裁决卡片（默认选中项）、probe 诚实展示、提交后成功态。**UI 截图走真机验收车道**。
7. **门禁**：冻结树全量 `run_tests_clean.sh` + `ruff` + `tsc` + vitest + `git diff --check` exit 0；`docs/review_ledger.tsv` 双读覆盖闸门；Gate-0 收据 `docs/gate/<sha>.json`。CI gate0 绿后再谈 PR merge（逐项 §14.4 批准）。

---

## 8. 风险 / 未决项

- **R1（最高）DEFER 语义未定**：D1（保持悬空、audit-only，推荐）vs D2（abandon-like 配对）。D1 与 `07 §8`「无 dangling」字面冲突，需明示豁免；D2 有事件/Ledger 分歧。**施工前须定夺**；同时需 `_ALLOWED_TRANSITIONS` 增 `NEED_RECONCILE→NEED_RECONCILE` 自环（现无）。另需确认 DEFER 后 run 是否可继续（#14 仍拦截盲跑，倾向「recover 200 但该 op 仍 pending」）。
- **R2 `source` 是否进 HTTP 传输模型**：推荐**进**（`RecoverDecisionRequest.source` 可选），因为来源采集在前端；替代方案是后端生成占位来源（不可，会伪造）。若进，需定**长度上限**（建议 2000 字符，与 `ApproveRequest.reason` 同口径）防放大；不上限则登记为残余。
- **R3 App.tsx × #353 冲突**：两者同改 `App.tsx`/`api.ts`。预案：本票只在 `App.tsx` 加**最小挂载点**（一个入口 + 409 分支挂面板），大改放新组件；施工前按 §14.13 检查对侧在途，集成前先回后正、按 §14.7 报告。
- **R4 probe 接线范围**：`_hint_for` 生产未供 registry（§1.3）。新增零副作用投影 + 领域端口属**必要接线**；若审查认为超出票面，则合同 6 降级为「仅展示工具名/参数，不展示 probe」（需用户确认）。
- **R5 只读列表端点命名/形状**：`GET /api/recovery/interrupted` 命名与载荷（是否含四要素全量 vs 仅 id+状态）未定；须零写且与 `/api/sessions` 不重复真相（#22）。
- **R6 进度文件版本口径**：用 `schema_version`+`source_event_seq`（`verify_progress_file` 同源）；文件缺失/损坏→如实标（`missing/unreadable/invalid_schema`），不伪造「最新」。
- **R7 `replay_safe` on read-only MCP**：保持 False（远程不可证）；如未来要求 read-only MCP 声明 safe，另立票。

---

## 9. 交付边界（不做）

- 不碰禁区：`src/agent_harness/context/`、`session/fork.py`（#527）、`approve_policy`、skill promote、delegation。
- 不重写 #305 通用 pause 状态机；不新增第二套 RecoveryCoordinator；不做 Tool-specific reconcile **算法**（issue「不做」）。
- 不复制 Inngest 代码（SSPL，只借概念）。
- 不新建 OperationState 词（DEFER 复用 `NEED_RECONCILE` 记 pending）。
- 规格冻结：`07`/`03` 正文不改；若需修订（如 07 §8 对 DEFER 悬空的豁免）按漂移声明流程、用户另批。
- Scope Lock：只动本票范围；不整文件 ruff format；`git diff --check` 干净。
