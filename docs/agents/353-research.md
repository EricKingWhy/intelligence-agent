# #353 [W-09] 任务 diff 与证据审阅界面 —— 成熟产品调研报告

> 阶段：研究（只调研、不施工）。工作区 `~/workspace/intelligence-agent-wt-353`，分支
> `codebuddy/353-w09-review-ui`（基线 `e165d5ec`，工作树干净）。本报告是 `AGENTS.md`
> §6.1 / `docs/SDD_WORKFLOW_PROTOCOL.md` §1.3 要求的「方案依据」块 + 票面成立性结论。
> **本报告不施工、不改任何产品代码。**

## 0. 启动检查表（AGENTS.md §3，如实）

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | `goal/.../00_PROJECT_VISION.md` §2.2 Observable / §2.5 Reuse First / §3 冻结原则（1/3/5/6/8/9/14/17/20/22） | READY |
| 当前任务规格 | `goal/.../11_STREAMING_API_WEB_UI.md` §5/§6/§6.1/§6.2/§9；`gh issue view 353` 全文；`docs/tickets/workbench-2026-09-27/W-09-review-ui.md` 全文；`docs/PRD_RECOVERABLE_REVIEWABLE_AGENT_WORKBENCH.md` §3/§4.2/§4.4/§5 | READY |
| Reuse 相关判定 | `goal/.../13_OPEN_SOURCE_REUSE_MATRIX.md` §2/§3/§5/§7 | READY |
| Phase 依据 | issue #353（父 #344，P1，in-progress；blocked-by #352 已关）+ workbench 票系。注：`14_IMPLEMENTATION_ROADMAP.md` **无 W-* 章节**（grep 实测），Phase 依据以 workbench 票系 + 父 issue 为准，如实记录 | READY |
| 本任务触发细则 | `docs/SDD_WORKFLOW_PROTOCOL.md` §1.3 / §8.9；`AGENTS.md` §3/§6/§6.1/§7/§9.1.1/§16.2；`docs/agents/reference-sources.md` §2；`CONTEXT.md`「Streaming / Web UI 层」「Web UI 层（Phase 9-10）」；`docs/adr/0014-web-ui-redesign-architecture.md`（D1/D8/不变量守卫）；`docs/agents/implementation-discipline.md`（§9.5 梯子边界）。本任务纯调研、无代码施工，不触发 §4.2 Debug / §4.1 Review playbook 正文 | READY |

---

## 1. 票面摘要与本仓现状核实

### 1.1 票面（复核用，以票面为准）

- **六件事**：① 原目标/约束授权/验收项（点开可见原事件来源）；② Agent 现在执行/暂停/待
  reconcile/结束；③ 哪些项通过/失败/缺证据/已过期；④ 改了哪些文件、当前 diff 与测试时快照
  是否一致；⑤ 失败尝试/UNKNOWN Tool/下一步；⑥ 接受/带原因接受/释放目录三种操作分别会发生什么。
- **工作指令**：服务端 Task/Evidence 投影驱动；状态文案固定「待验证/可交付/已接受」，验证与
  接受各有标签不同绿勾；工具卡链接 Artifact/Operation/SessionEvent；diff 显示 `agent-progress/`，
  暂存/提交只由用户显式调用原有能力（首版可仅外部打开）；刷新/隐藏再显示/重连不丢当前 Task，
  不以本地缓存推断通过；审批控件复用 ApprovalCard。
- **依赖**：W-08 服务端契约（#352，已关）。**不做** IDE 编辑器、多用户协作、前端私存业务事实。

### 1.2 本仓现状（施工前置事实，均为本次实读）

**服务端投影已就绪（W-07/G1-3 都已有）**

- Task 三轴：`src/agent_harness/session/task.py`——`PRODUCT_STATES = ("executing",
  "pending_verification", "deliverable", "accepted")`（`task.py:65`）、六态
  `VERIFICATION_VALUES`（`task.py:52-59`）、`product_state` 纯投影（`task.py:126-140`）、
  `derive_task_state`（`task.py:252-344`）；REST `GET /api/sessions/{id}/task` 与
  `POST /task/definition|acceptance-revision|verification|acceptance|acceptance/release`
  （`src/agent_harness/web/task_delivery.py:98-203`）。
- Evidence 投影：`src/agent_harness/session/evidence.py`——14 字段闭合 DTO、三态
  `pass|fail|blocked`（`evidence.py:42`）、`derive_evidence_state` 纯投影（`evidence.py:373-396`）、
  `evaluate_evidence_freshness` fail-closed（`evidence.py:442-482`）；REST
  `GET/POST /api/sessions/{id}/evidence`（`src/agent_harness/web/evidence_delivery.py:65-114`）。
  **关键**：`GET /evidence` 每条记录都带服务端算好的 `freshness: {status, reasons}`
  （`src/agent_harness/session/service.py:4085-4100`），客户端**不需要**自己算陈旧。
- 目录租约：`src/agent_harness/web/task_lease.py:84-156`（`GET /task/lease`、
  `POST /task/lease/acquire|release|queue/cancel`）——即票面第 ⑥ 项的「释放目录」已有端点。

**前端积木已就绪（React，`web/src`）**

- `components/ApprovalCard.tsx`（Tool 权限审批卡，`POST /approve`）、`components/DiffBlock.tsx`
  （before/after 双栏 diff，复用 ArtifactViewer）、`components/ChangesPanel.tsx`（按文件聚合的
  改动面）、`lib/reconnect.ts`（重连状态机）、`hooks/useSession.ts`（历史 + live SSE 重建）、
  `lib/projection.ts`（事件 → 渲染模型纯 reducer）。
- `workspace_files.py:530/563` 已有 `GET .../workspace/git/status` 与 `.../workspace/git/diff`。

**前端尚未接线**：`web/src/lib/projection.ts:1503-1532` 对 `TASK_*` 与 `EVIDENCE_RECORDED`
只登记 `noopProjection` 并标注「权威投影在后端 `derive_task_state` / `derive_evidence_state`
（单源）」。即：审阅页的服务端数据源已存在，页面本身未建。

---

## 2. 方案依据（SDD §1.3 五字段）

### 2.1 来源（3 个独立来源，均一手核实；读取日期 2026-10-06）

| # | 来源 | 读取方式与日期 | 形态 |
| --- | --- | --- | --- |
| 1 | OpenAI Codex app 介绍页 <https://openai.com/index/introducing-the-codex-app/> | 2026-10-06 一手整页读取（WebFetch，正文自述发布 2026-02-02、2026-03-04 增补 Windows） | 公开产品页，非代码 |
| 2 | DeepSeek Harness `apps/desktop/README.md` + `packages/client/ui-*` 包 README | 2026-10-06 一手读取 raw.githubusercontent（master 解析到 commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`）；另读 `LICENSE` | 代码仓库文档（MIT） |
| 3 | Pi（`earendil-works/pi`，更名前 `badlogic/pi-mono`） | 2026-10-06 本地浅克隆 `/home/hatch/reference/pi` @ `28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9`（`Add [Unreleased] section for next cycle`），一手 grep + 读码 + 读 README | 代码仓库（MIT） |

> 辅助（未作为独立方案依据，仅作交叉参考）：Claude Code 官方 Quickstart 与 Permission modes
> 两页 2026-10-06 一手读取——该两页**不含** diff 审阅面板与「接受/拒绝」编辑提示的细节，故
> 本报告不据其下机制结论（诚实记录，不以二手转述补位）。

### 2.2 机制摘要（每个来源怎么解这个问题，不是「它也有这功能」）

#### 来源 1 · OpenAI Codex app：隔离副本上的「线程内 diff + 人审 checkout」

- **线程 = 组织单位**：「Agents run in separate threads organized by projects, so you can
  seamlessly switch between tasks without losing context.」——变更与审阅都**嵌在线程内**，不跳
  独立审阅页。
- **变更以 diff 呈现，就地评论/外部打开**：「The app lets you review the agent's changes in the
  thread, comment on the diff, and even open it in your editor to make manual changes.」
- **采纳动作 = 本地 checkout，而非 UI 上的「通过」按钮**：「you can check out changes locally or
  let it continue making progress without touching your local git state.」页面**没有**显式
  accept/reject 按钮语义。
- **隔离靠 git worktree**：「built-in support for worktrees … Each agent works on an isolated copy
  of your code」。这是它敢让执行与采纳分离的前提。
- **后台结果进 review queue**：Automations 结果「land in a review queue so you can jump back in
  and continue working」。

#### 来源 2 · DeepSeek Harness：Host 拥有全部投影，桌面端只是同一 Web UI 的壳

> 票面只引了 `apps/desktop/README.md`，但该文件只讲**打包/生命周期**，不含 diff/审阅机制。
> 本报告据同仓库 `packages/client/ui-*` 包 README 补齐真正相关的机制（同 commit `5badb15`）。

- **桌面端 = 薄壳，复用同一 Web 应用**：「The desktop application is an Electron shell around the
  complete dsh Web application.」加载打包的 Web 入口 `dsh-app://app/`，Host 提供 boot
  injections 与已鉴权 API；「No renderer receives filesystem access, raw Electron IPC, a shell,
  or arbitrary pnpm arguments.」→ **桌面不维护第二套 UI，也不维护第二套真相**。
- **任务/运行状态来自 Host**：`ui-session`——「Running status comes from Host list baselines or
  status events.」；「Pending interactions are process-local projections — the owning Remote
  waterfall **must replay an outstanding request after a browser reconnect**.」（重连回放）。
- **运行中任务面板**：`ui-jobs`——`ctx.jobs` 是「the single roster … There is no second roster to
  join」，行内带 lifecycle/duration/progress/terminal detail；停止是「two-press stop control」
  且「the model is told the user stopped its task rather than left to infer it」。
- **变更/审阅**：`ui-deliverables`——每轮收尾渲染 changed-files 卡，「each opening the turn's
  **review tab** on that file」；review tab 支持 unified/side-by-side、逐 hunk 行号高亮；
  **「The comparison is the turn's snapshot of the file, not its current content.」**；路径与内容
  「come from the recorded summary, successful mutations, and explicit deliveries, **never from
  the prose**」，summary / comparison 都由 Host 经已鉴权路由提供服务。**快照 vs 当前内容被显式
  分开**——正对应票面第 ④ 项「当前 diff 与测试时快照是否一致」。
- **审批**：`ui-approval`——浏览器审批呈现 Host permission 请求，**只暴露瞬态决定**「supports
  allow-once and reject; persistent permission policy remains owned by **Host-side** approval
  packages」，且「a withdrawn or replaced request cannot accept another answer」（一次性锁）。
- **计划/审阅裁决**：`ui-plan`——提交的计划在右栏打开审阅，**「The review buttons alone decide
  whether implementation may begin.」**；「browser reload restores the document from Session
  history.」（刷新从 Session 历史恢复，不靠本地缓存）。
- **投影写法**：`ui-goal`——`useProjection('goal')` 由 history tail 播种 + `session/projection`
  帧更新；「The strip **owns no domain store or cross-plugin cache**」；变更用 CAS ref 防陈旧。
  `ui-conversation` 直接消费 `SessionEventLikeEntry` 事件流，单一 registry。

#### 来源 3 · Pi：committed state 即 UI 状态；task graph 供任务面板

- **UI 只读已提交状态**：`packages/durable/README.md:277`「**Everything a UI needs is committed
  state.** `viewState()` returns the conversation's structural view as a read-only Chord state,
  updated after every commit that touches it.」DTO 结构见
  `packages/durable/src/harness/view.ts:26`（`ConversationView` = active entries + docs
  `pi.live`/`pi.inbox`/`pi.usage`/`pi.agent`/`pi.provider`）。
- **watch 传增量、慢消费者合并、迟到者从当前视图起**：`README.md:293-310`——`root.watch()`
  交付每次提交的精确 ops；「A slow watch keeps at most 100 undelivered frames … replaced by one
  frame holding the whole newest view. A client that joins late or reconnects starts from the
  **current view; nothing is replayed**.」
- **任务面板直接由 task graph 驱动**：`README.md:487-501`——`harness.taskGraph()`「shows every
  live task … as one Chord state, **for a task panel**」，每节点带 owner 边、status
  （`pending`/`running`/`waiting`/`completing`）、background/abort 标记、其拥有的会话；
  `watchTaskGraph()` 同构。
- **Task = 每步 checkpoint 的 durable 状态机**：`README.md:85`；崩溃后 pending 重跑，
  submission 带 `requestId` 幂等。
- **无验收/证据实体，也无代码 review/接受 UI**：diff 只在 TUI 内按 edit 工具结果渲染
  （`packages/coding-agent/src/core/tools/renderers/edit.ts:10,110` 的 `renderDiff`）；
  `--approve`（`packages/coding-agent/src/cli/args.ts:334`）是「信任项目本地文件」，与变更审阅
  无关。

### 2.3 契合点（AGENTS.md §7 不变量逐条点名）

**强契合（调研加固票面方向）**

- **#22 Web UI 不维护第二套不可对账真相**：三个来源**全部**把「UI 状态 = 服务端/已提交投影」
  作为第一原则——DSH「Host 拥有 running status / 投影 / 审批策略」「no second roster」「owns no
  domain store」且桌面端复用同一 Web；Pi「Everything a UI needs is committed state」+ 迟到者
  从当前视图起、不 replay 本地缓存。票面「以服务端 Task/Evidence 投影驱动」「不以本地缓存推断
  通过」与之一致，**且被三来源一致背书**。
- **#3 append-only typed SessionEvent / #4 Event ≠ Log**：Pi 的 entries 不可变、documents 随
  commit 原子更新；DSH 从 SessionEvent 事件流投影。本仓 W-07/W-08 已是「新 append-only 事件 +
  纯 derive」，方向同源。
- **#13 Operation Ledger 支持 reconcile / #14 UNKNOWN 高风险 Tool 不盲重跑**：三来源均未在审阅
  页把「不确定副作用」做成自动通过——DSH `ui-approval` 只给瞬态 allow-once、持久策略留 Host；
  本仓 reconcile 属 Ledger/Runtime 域，UI 只能**展示**不得改写。
- **#15 Artifact 大内容只给 summary + ref**：票面「工具卡链接现有 Artifact」+ DSH review tab
  「由 Host 经已鉴权路由提供」同向；本仓 ArtifactViewer/可回读端点已具备。

**冲突/边界（照搬会破坏不变量）**

- **Codex 的 git worktree 隔离**：本仓 V1 显式非目标「自动 Git worktree」（Vision §5），且 PRD
  用**持久写入租约**（W-10）替代隔离副本。→ 隔离机制 **DEFER**，只借「执行与采纳分离」思想。
- **Codex/DSH 的「评论 diff」协作**：多用户协作是本票**不做**项，且评论需要新的可变存储 →
  与 #22 与 Scope Lock 冲突，**不做**。
- **Pi 的 durable TS runtime / DSH 的 Cordis**：复用矩阵既定 `DEFER`，不嵌入 Python Core。
- **把 ApprovalCard 当成「接受/带原因接受」控件**：见 §5 调整项 F——ApprovalCard 是权限审批
  （allow-once/deny，`ApprovalCard.tsx:1-16`），与 task acceptance（CAS 裁决、可带缺项 reason、
  可 release）是**不同语义**；DSH 同样把 `ui-approval`（权限）与 `ui-plan` 的 review 按钮分开。

**未被触及**：#1 Python/Async、#8 Tool Retry、#9 Fallback/Retry 分离、#10 并发依赖、#12
Checkpoint≠副作用恢复、#18 Capability、#19 SubAgent、#20 LangGraph、#21 Optional 故障隔离——
三来源无对应机制，本票不应据此改动。

### 2.4 判定（REUSE / ADAPT / PORT DESIGN / BUILD / DEFER + 理由）

| 子机制 | 判定 | 理由 |
| --- | --- | --- |
| 页面以服务端 Task/Evidence 投影驱动、刷新/重连同源 | **REUSE**（本仓既有）+ **PORT DESIGN**（DSH/Pi 纪律背书） | `GET /task`、`GET /evidence`（含服务端 freshness）、`useSession`/`reconnect`/`projection` 已就绪（§1.2）；DSH「Host 拥有投影、重连回放」与 Pi「committed state = UI state」直接背书，不需要新真相 |
| 变更展示：changed-files 卡 + 逐文件 diff + **快照 vs 当前**并陈 | **PORT DESIGN**（交互）+ **REUSE**（渲染/端点） | 借 DSH `ui-deliverables` 的「turn snapshot ≠ current content」分离；渲染复用 `DiffBlock`/`ChangesPanel`，数据走 `workspace/git/diff`（`workspace_files.py:563`） |
| 运行/任务状态面板（多任务、owner 边、状态枚举） | **PORT DESIGN** | 借 Pi `taskGraph`/`watchTaskGraph`「for a task panel」+ DSH `ui-jobs` 单 roster；本仓只做单 Task 审阅，裁剪到 Run 态 + 产品态 |
| 审批/裁决交互 | **REUSE 外壳 + 分离语义** | 复用 `ApprovalCard` 的视觉/一次性锁/错误态骨架；acceptance 走既有 `task/accepted`(CAS) 与 `task/acceptance/release`，**不复用** permission allow-once 语义（§5-F） |
| 接受 / 带原因接受 / 释放目录 三操作 | **REUSE**（既有端点） | `task_delivery.py:170-203`（acceptance、acceptance/release）+ `task_lease.py:136-145`（lease release）已存在；UI 只需薄封装 |
| 「可交付」是否含证据新鲜度 | **BUILD/接线（服务端）或 ADAPT（展示连接）** | 见 §5-C：`product_state` 不含 freshness，需明确由谁合并 |
| Codex worktree 隔离副本 | **DEFER** | Vision §5 非目标 + PRD 用租约替代；本票不引入 |
| Pi durable TS runtime / DSH Cordis | **DEFER** | 复用矩阵既定 |
| 编辑器内联编辑 / 多用户评论 | **DEFER（本票不做）** | 票面明确不做；且需新可变存储，违反 #22 |

### 2.5 License 结论

- 来源 1（Codex app 页）：公开产品页，**零代码复制**，只做思想级借鉴 → 无 License 传导义务。
- 来源 2（DSH）：`LICENSE` 一手核实为 **MIT**（Copyright (c) 2026 DeepSeek）——本次只读调研、
  无复制；将来若实质 Port 交互/代码须保留 MIT 声明与来源。
- 来源 3（Pi）：`/home/hatch/reference/pi/LICENSE` 一手核实为 **MIT**（Copyright (c) 2025 Mario
  Zechner）——同上。
- 本报告只借鉴**交互/数据流设计**，不搬任何上游代码；如 W-09 施工阶段要复制片段，须按复用
  矩阵 §1 第 3–6 条重新核查并保留来源。

---

## 3. 对比表：成熟产品做法 vs 票面六件事

| 票面六件事 | Codex app | DeepSeek Harness | Pi | 本仓落点（判定） |
| --- | --- | --- | --- | --- |
| ① 原目标/约束授权/验收项 + 点开见原事件来源 | 线程承载上下文，无「验收项」实体 | goal strip（`useProjection('goal')`）+ Session 历史；无 criterion 实体 | entries 不可变、原文永存；无验收项实体 | **REUSE**：`GET /task`（criteria 带 `item_id/origin/confirmed`）+ 事件 seq 跳转；本仓独有，三来源无对应物 |
| ② Agent 执行/暂停/待 reconcile/结束 | 线程 + review queue 隐含状态 | `ui-session`：running status 来自 Host status events；暂停/审批等待入 active-task 清单 | taskGraph status：`pending/running/waiting/completing` | **PORT DESIGN**：Run 态来自 SessionEvent（`open_run_ids`/run 终态）+ Ledger reconcile；**交付态**另轴（见 §5-B） |
| ③ 通过/失败/缺证据/已过期 | 无对应（无证据实体） | review tab「snapshot ≠ current」是**快照陈旧**的雏形 | 无 | **BUILD/接线**：`GET /evidence` 逐条 `result(pass\|fail\|blocked)` + 服务端 `freshness(fresh\|stale+reasons)`；「缺证据」= criterion 无 evidence 记录（§5-C） |
| ④ 改了哪些文件 + 当前 diff 与测试时快照一致 | 线程内 diff、可评论/外部打开 | changed-files 卡 → `changes-review` tab；**snapshot vs current 显式分离**；Host 供给 summary | 仅按 edit 工具结果渲染 diff | **PORT DESIGN + REUSE**：借「快照 vs 当前」分离；渲染复用 `DiffBlock`，数据走 `workspace/git/diff` |
| ⑤ 失败尝试/UNKNOWN Tool/下一步 | 无显式 | `ui-jobs`（含 settled/失败）、`ui-approval`（pending） | 失败 task/子任务状态可见 | **REUSE/接线**：失败尝试来自事件流；**UNKNOWN/reconcile 属 Ledger（#13/#14）**，见 §5-E |
| ⑥ 接受/带原因接受/释放目录三操作后果 | 采纳 = 本地 checkout（无按钮） | `ui-plan`：review 按钮决定是否开工；审批 allow-once | 无 | **REUSE**：`task/accepted`(CAS, `accepted_with_gaps` 必带 reason) + `task/acceptance/release` + `task/lease/release`，**三操作语义需分别明示**（§5-D） |

**一句话**：三来源**都没有**「验收项 ↔ 结构化证据 + 过期语义」的实体（W-08 是本仓独有，与
#352 调研一致）；但它们**一致**给出了本票真正该借的东西——**UI 只呈现服务端/已提交投影**
（DSH/Pi）、**快照与当前内容显式分离**（DSH）、**运行态与裁决分离且裁决由明确按钮触达**
（DSH `ui-plan`）、**迟到/重连从当前权威状态重建**（Pi/DSH）。

---

## 4. 本仓可复用核实（AGENTS.md §9.5 懒惰阶梯）

1. **页面骨架/渲染**：`components/`（ApprovalCard、DiffBlock、ChangesPanel、ArtifactViewer、
   JsonTree、Timeline 等）+ `lib/projection.ts` + `lib/reconnect.ts` + `hooks/useSession.ts`——
   全部已在，**不重造**。
2. **服务端投影**：`GET /task`、`GET /evidence`（含 freshness）——页面只做渲染，不在前端算
   业务事实。
3. **diff 数据**：`workspace_files.py:530/563` 的 `git/status`、`git/diff`；`agent-progress/`
   作为普通工作区文件出现在 diff（PRD §4.2），不自动暂存。
4. **三操作端点**：`task_delivery.py:170-203`、`task_lease.py:136-145`——UI 薄封装即可。
5. **审计/工具卡跳转**：Artifact/Operation/SessionEvent 已有 Inspector 定位能力
   （`lib/inspectorPanel.ts` + ADR-0014 D5 Main↔Inspector 联动）。
6. **缺口（须新建，最小）**：前端 task/evidence 接入（`projection.ts` 目前 `noop`）；「审阅六问」
   的**合并视图**（§5-C 是唯一需要决策的架构点）。

---

## 5. 票面成立性结论

### 结论：② 票面**部分成立**——方向正确（服务端投影唯一源、刷新/重连同源、不做 IDE/协作），
但 framing 有 **6 处需要调整**；其中 C/E 属架构级，需用户决策。

**票面成立的部分（调研加固，不改）**

1. 「以服务端 Task/Evidence 投影驱动页面」——被三来源一致背书（§2.3 #22）。
2. 「验证与接受分开、不混绿勾」「不以本地缓存推断通过」——与 DSH「review 按钮独立于运行态」
   「Host 拥有投影」同向。
3. 「diff 显示 agent-progress/、暂存/提交只由用户显式调用」——与 PRD §4.2 一致，且复用既有
   `git/diff` 端点。
4. 「刷新/窗口隐藏再显示/断线重连不丢当前 Task」——spec 11 §6.2 + 既有 `reconnect.ts`/`useSession`。
5. 「工具卡链接现有 Artifact/Operation/SessionEvent」——复用既有 Inspector。

**需要调整的 6 处（附证据）**

- **A. 交付态文案缺「执行中」**。票面固定「待验证/可交付/已接受」三词，但服务端
  `PRODUCT_STATES` 是**四态** `executing/pending_verification/deliverable/accepted`
  （`task.py:65`），PRD §3 的「交付展示」也是**四态** `执行中/待验证/可交付/已接受`。
  → 调整：UI 交付态用**四态**（补「执行中」），三词只作为 PRD 指定的子集，不可当作全集。

- **B. 第 ② 项把两个状态轴混在一起**。第 ② 项「执行/暂停/待 reconcile/结束」是 **Runtime
  Run / Operation Ledger 轴**；而「待验证/可交付/已接受」是 **Task 交付轴**
  （`product_state`）。PRD §3 明确把「Runtime Run 状态」「验证状态」「接受状态」「交付展示」
  列为**不同唯一真相**。DSH 亦然（running status 与 approval/plan 裁决分离）。
  → 调整：页面至少分两轴呈现（Run/操作态 vs 交付态），文案不得用一个「Agent 状态」囊括。

- **C. 「可交付」的服务端依据不足（架构级）**。`product_state == "deliverable"` **只**要求
  每个 criterion 的 `verification.value == "passed"`（`task.py:134-139`），**完全不看** W-08 的
  结构化 evidence 与 `freshness`。而 PRD §3 定义「可交付」= 「所有必需验收项的**真实证据与当前
  代码版本匹配**、没有未决副作用」，§4.2 要求「证据与当前文件摘要不一致时显示『已过期』，不得
  沿用绿勾」。因此：服务端 `GET /task` 说 `deliverable` 时，其证据可能**全 stale 或根本没有**
  （`verification` 可只用自由文本 `evidence: str|None` 置 passed，不产生任何 evidence 记录）。
  → 这使「以服务端 Task/Evidence 投影驱动」出现一个真空：**谁把 verification + freshness 合并成
  PRD 意义的「可交付」？** 若由前端拼两个投影得出绿标，则「可交付」这一**业务结论**首次在前端
  产生（#22 风险）；若不会合，则页面可能显示「可交付」+「证据已过期」自相矛盾。→ **需用户决策**
  （见下「推荐选项」）。

- **D. 「释放目录」与「撤销接受」是两个不同 release，票面第 ⑥ 项易混**。既有两个语义不同的
  端点：`POST /task/acceptance/release`（撤销**接受裁决**，CAS，`task_delivery.py:189-203`）与
  `POST /task/lease/release`（释放**目录写租约**，`task_lease.py:136-145`）。PRD §3「用户接受、
  归档或明确释放后释放（租约）」说明：**接受**本身也会**联动**释放租约，二者是不同事实。
  → 调整：第 ⑥ 项 UI 必须把「接受 / 带原因接受 / 释放目录（租约）」三种操作的**各自后果**
  分别说明，且「撤销接受」与「释放租约」不共用文案与按钮。

- **E. 第 ⑤ 项「UNKNOWN Tool」超出 W-08 投影范围（架构级/依赖）**。UNKNOWN 高风险 Tool / reconcile
  是 **Operation Ledger** 域（不变量 #13/#14），其 UI 归 W-13（restart-reconcile-ui）；本票依赖
  只列 W-08。W-09 **不能**自己从事件流「推断」某个 Tool 是 UNKNOWN——那正是 #14 禁止的猜测。
  → 调整：明确 W-09 首版若 Ledger reconcile 投影面未就绪，第 ⑤ 项只能展示**已由服务端投影出的**
  失败尝试/下一步；UNKNOWN 状态**留槽并显示"需 reconcile（来源未接入）"**，不得伪造。

- **F. 「复用 ApprovalCard」用于接受/带原因接受是语义错配（次要）**。`ApprovalCard` 是**权限审批**
  （allow-once/deny，`ApprovalCard.tsx:1-16`，成功后写 `permission/resolved`）；task acceptance 是
  **CAS 裁决**（`accepted`/`accepted_with_gaps` 必带 reason、可 `acceptance/release`）。DSH 把
  `ui-approval`（权限）与 `ui-plan`（review 裁决）做成不同组件。
  → 调整：复用 ApprovalCard 的**视觉外壳**（pending 一次性锁、错误态、就地卡片），但**不复用**
  permission allow-once 语义；acceptance 控件是独立语义，走 `task/accepted` 端点。

### 推荐选项（2–3 个，针对 C/E，其余为小改）

- **选项 1 — W-09 首版「只呈现不合并」（最小、零推断、Scope Lock）**
  UI 并列展示两个**原始服务端投影**：交付态 chip 取自 `GET /task` 的 `product_state`（四态），
  证据区逐条显示 `result` 与服务端 `freshness`；**当存在 stale 证据或 criterion 无证据时，仅
  叠加醒目的「证据已过期/缺证据」警示，不把 chip 文案改成前端自造的「可交付」**。失败尝试展示
  事件流既有事实，UNKNOWN 留槽不推断。
  取舍：不改后端、不越 W-09 范围、零 #22 风险；代价是可能出现「可交付 + 证据已过期」并存，需靠
  警示文案消歧，页面偏「原始」。

- **选项 2 — 增一个服务端 review 投影（推荐目标形态，需票面变更）**
  在服务端把 Task 三轴 + Evidence freshness（+ 必要时的 Run/Ledger 状态）合并成一个**单一可
  渲染投影**（扩展 `GET /task`，或新增 `GET /review`），由服务端给出每个 criterion 的
  `pass/fail/missing/stale` 汇总与 PRD 意义的「可交付」结论。
  取舍：最符合 #22 与 DSH「Host 拥有投影」范式，页面最干净；但**超出 W-09「纯前端」范围**，
  需按 AGENTS.md §9.1.1 请求用户批准改票面/拆票。

- **选项 3 — 分两票：W-09 出「原始投影页」，合并结论另立服务端票**
  W-09 按选项 1 交付页面骨架 + 三操作 + 同源刷新；把「freshness 合并进交付态」单列一张服务端票
  （承接 W-07/W-08 交集）。取舍：Scope 最清晰、可独立验收；代价是首版页面不含「一键可交付」结论，
  用户需自行在两组事实间判断（但这恰是 PRD「证据可审阅」的本意）。

> 我的建议：**选项 3 落地 W-09（= 选项 1 的首版）+ 选项 2 立服务端票**。理由：C 是真正的架构
> 缺口而非纯前端问题，硬塞进 W-09 会逼前端做业务合并（违反 #22），单独立票又能补齐 PRD §3/§4.2
> 的「可交付」定义。

### 待用户决策事项（研究阶段不决断、不施工）

1. **C**：采用哪个选项（1/2/3）？特别是「PRD 意义的『可交付』由服务端算还是前端展示层连接」。
2. **E**：W-09 首版对 UNKNOWN/reconcile 是「留槽不推断」还是「等 W-13 后再做第 ⑤ 项」。
3. **A/B**：交付态采用四态（补「执行中」）并显式分两轴，是否按本报告调整（与 PRD §3 一致）。
4. **F**：接受/带原因接受控件的落位与文案（复用 ApprovalCard 视觉、独立语义）。
5. 票面 text 修订（A/B/C/D/E/F）是否走 §9.1.1 改 issue #353 或拆子票。

---

## 6. 证据指针

- 票面：`gh issue view 353`；`docs/tickets/workbench-2026-09-27/W-09-review-ui.md`。
- 规格/PRD：`goal/.../11_STREAMING_API_WEB_UI.md` §5/§6/§6.1/§6.2/§9；
  `docs/PRD_RECOVERABLE_REVIEWABLE_AGENT_WORKBENCH.md` §3/§4.2/§4.4/§5；
  `goal/.../13_OPEN_SOURCE_REUSE_MATRIX.md` §2/§3；`CONTEXT.md`「Streaming / Web UI 层」；
  `docs/adr/0014-web-ui-redesign-architecture.md`（D1/D8）。
- 本仓实现：`src/agent_harness/session/task.py:52-65,126-161,252-344`；
  `src/agent_harness/session/evidence.py:39-42,140-148,373-396,442-482`；
  `src/agent_harness/session/service.py:3956-4102`；
  `src/agent_harness/web/task_delivery.py:98-203`；`src/agent_harness/web/evidence_delivery.py:65-114`；
  `src/agent_harness/web/task_lease.py:84-156`；`src/agent_harness/web/workspace_files.py:530/563`；
  `web/src/lib/projection.ts:1503-1532`；`web/src/components/{ApprovalCard,DiffBlock,ChangesPanel}.tsx`；
  `web/src/lib/reconnect.ts`；`web/src/hooks/useSession.ts`。
- 一手来源：Codex app 页（2026-10-06 读取）；DSH `apps/desktop/README.md` +
  `packages/client/ui-{deliverables,approval,plan,jobs,session,goal,conversation}/README.md`
  @ `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（MIT）；Pi `/home/hatch/reference/pi` @
  `28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9`（MIT，`packages/durable/README.md:85,277,293-310,487-501`、
  `packages/durable/src/harness/view.ts:26`、`packages/coding-agent/src/core/tools/renderers/edit.ts:10,110`）。

---

*本报告只含调研结论与调整建议，无实现代码。任何施工需另行立项并按 AGENTS.md §9.1.1 走票面细化与用户批准。*
