# SDD Ticket Tracker

> **持久化活文档** — 跨 context window 追踪 SDD 循环进度。
> 每次进入新 context window 时，先读本文件恢复状态。

---

## 当前状态

| 项 | 值 |
| --- | --- |
| Worktree | `D:\intelligence-agent-frontend` |
| Branch | `feat/frontend` |
| 协议版本 | `docs/SDD_WORKFLOW_PROTOCOL.md` v1 |
| 后端交接手册 | 本轮：`D:\intelligence-agent-backend\docs\HANDOFF_FRONTEND_RECOVER_FORK_SCROLL.md`（A/B/C/D） |
| 集成交接提示词 | 本轮：`docs/integration/FRONTEND_REFRESH_PERSIST_INTEGRATION_PROMPT.md`（**集成 AI 的唯一入口**，§0 是可执行摘要）；上一批：`docs/integration/FRONTEND_RECOVER_FORK_SCROLL_INTEGRATION_PROMPT.md` |
| 本批交接手册 | `docs/HANDOFF_APPROVAL_CARD_COVERAGE.md`（做了什么 + 8 个坑点 + 未决项 + 复核命令） |
| 下一批提示词 | `docs/PROMPT_FRONTEND_NEXT_BATCH.md`（可直接复制给前端 Agent：OBS-015 修复为主） |

**禁止推送远程**（AGENTS.md §13.2/§14.4）：本地 commit 已完成，push 归集成 AI。

### 最近一批：OBS-016 前端同步——超长单行标记文案（2026-09-11）

| 项 | 值 |
| --- | --- |
| 本批 commit | `1fac807` |
| 门禁 | tsc ✓ / vitest **502 passed**（28 文件）/ oxlint **35w 0e**（基线持平）/ playwright **118 passed**（`--workers=2`）/ vite build ✓ |
| 交付 | 纯跨端同步，**解析逻辑零改动**（`LINE_TRUNCATED_RE` 的 `[^\]]*` 本就吞尾部）。① `web/src/lib/toolShapes.test.ts`：新增「新文案（OBS-016）」用例；原用例改标「旧文案（历史会话已落盘）」并**保留**——历史 JSONL 事件仍是旧文案，两种都要能解。② `docs/HANDOFF_FRONTEND_SYNC.md` §1.3：订正为「形状契约 + 措辞可变 + 历史文案兼容」。 |
| 变异验证 | 把 `LINE_TRUNCATED_RE` 改成仅匹配旧文案（追加 `\. Use bash`）→「新文案」用例变红、「旧文案」用例仍绿（已还原）。证明新增用例非空转，且旧用例仍锁住向后兼容。 |
| 跨端配对 | 后端半在 `D:\intelligence-agent-backend` `feat/backend`：`aa29562`（`read.py` 正文改点名真实工具标识符 bash/grep）。本 clone 是独立 clone，`web/` 与 `docs/HANDOFF_FRONTEND_SYNC.md` 相对 `origin/main` **零漂移**，故本批**未做 merge**（`feat/frontend` @`274afcf` 是 `origin/main` @`63db650` 的严格祖先，如需同步可 ff）。 |
| code-review | 本批为测试/文档同步，无解析逻辑改动；后端半的两轴 review 已发现并修复初版「the shell tool」指向不存在工具的问题。 |

### 上一批：OBS-015 修复——审批卡区分幂等已决(409)与真失败(5xx)（2026-09-11）

| 项 | 值 |
| --- | --- |
| 本批 commit | `cb0e008` |
| 门禁 | tsc ✓ / vitest **501 passed**（28 文件）/ oxlint **35w 0e**（基线持平）/ playwright **116 passed**（`--workers=2`）/ vite build ✓ |
| 交付 | ① `api.ts` 新增 `AlreadyResolvedError`；`postApproval` 在 HTTP 409 时抛它，其它非 ok 抛普通 `Error`。② `ApprovalCard.tsx` 修复 catch：`AlreadyResolvedError`(409) → 幂等成功翻卡片；其它错误 → 保持 pending + 显示可见错误(`role="alert"`) + 按钮重新可用可重试。③ 回归锁 `e2e/n-approval-card.spec.ts` +2 用例 ×2 视口 = 4 例。④ `.approval-error` CSS 规则（danger 淡染底 + 左侧 2px 实条）。⑤ 单测 `api.test.ts` +4 例（200 ok / 409 AlreadyResolvedError / 500 plain Error / 422 plain Error）。 |
| 变异验证 | 两处全部生效：① 还原 ApprovalCard 旧行为（任何错误都翻卡片）→ POST 500 用例变红（`Expected: "需要审批" / Received: "已批准"`）。② 禁用 AlreadyResolvedError 分支 → POST 409 用例变红（卡片不再翻「已批准」）。 |
| code-review | Standards 轴 0 hard violations、2 minor smells（均 acceptable）。Spec 轴发现 4 项：① 404 幂等语义未处理 → 经核实后端契约 404 = approval 不存在（不是「已解析」），409 才是幂等已决，当前代码正确。② 失败文案需更明确 → 已在 error message 中体现。③ `.approval-error` 无 CSS → 已补。④ tracker 未更新 → 本批更新。 |

#### 补记（2026-09-11 收尾）：404 语义订正 + 注释与代码对齐 + 404 fail-safe 锁

OBS-015 的既有 code-review 已判出「404 ≠ 幂等已决，当前代码正确」，但**只改了 tracker**，遗留了三处与代码矛盾的载体。本次收尾（**零产品行为改动**）：

| 项 | 内容 |
| --- | --- |
| 订正 1 | `web/src/lib/api.ts` 的 `postApproval` docstring 原写「404 with "already resolved" detail = same semantics → AlreadyResolvedError」，与代码（只有 409 抛 `AlreadyResolvedError`）相反，且会把「决策没生效」误显示成「已批准」。已按后端真实语义改写：404 的四个来源（session 不存在 / 审批队列缺失 / `approval_id` 不在队列 / 事件过期，`web/app.py:1157-1166`）无法与「已解析且已出队」区分 → 必须保持 pending；真已决由 `permission/resolved` 投影事件兜底。 |
| 订正 2 | `docs/PROMPT_FRONTEND_NEXT_BATCH.md` 原把「409/404 幂等语义走已决」写进任务步骤与验收标准（**该前提本身是错的**，会诱导后人实现 404-as-success）。已加 2026-09-11 订正块：保留原文 + 明确 **404 不走已决**。 |
| 新增锁 | `web/e2e/n-approval-card.spec.ts` +1 用例 ×2 视口：**POST 404 → 保持「需要审批」+ 错误文案含 404 + 按钮仍可重试**（此前该路径零覆盖）。 |
| 变异验证 | 注入「旧提示词推荐的错误实现」（`if (404) throw AlreadyResolvedError`）→ 新 404 用例**两视口变红**（2 failed / 12 passed）→ 还原后全绿。证明新锁非空洞。 |
| 未改 | `docs/FRONTEND_ISSUES_LOG.md` 的 OBS-015 条（证据记录，按 HANDOFF §8 不重写）；`ApprovalCard.tsx` / `api.ts` 的运行时行为零改动。 |
| 门禁 | tsc ✓ / vitest **501 passed**（28 文件）/ oxlint **35w 0e**（基线持平）/ playwright **118 passed**（`--workers=2`，116 + 新 404 用例 2）/ vite build ✓ |

### 上一批：瞬态三键 + 401 缝 + 审批卡两键（覆盖账目收口到 45/45，2026-09-11）

| 项 | 值 |
| --- | --- |
| 本批 commit | `35cd0a1`（401 缝单测 + `l-auth-banner.spec.ts`）、`8ed86f0`（瞬态三键 `m-stream-affordances.spec.ts`）、`c9dcf2a`（审批卡 `n-approval-card.spec.ts` + 联调车道） |
| 门禁 | tsc ✓ / vitest **497 passed**（28 文件）/ oxlint **35w 0e**（基线持平）/ playwright **112 passed**（`--workers=2`；104 + 审批卡 8）/ vite build ✓ |
| 交付 | ① 三个瞬态按钮（`tool-out-wrap-btn` / `tool-out-jump` / `reasoning-jump`）用 mock 流钉住窗口后**真实点击**；② 401 缝补 3 例单测 + 横幅 e2e；③ 审批卡「批准」「拒绝」**真机点击**（真实后端 + 真实模型） |
| 覆盖账目 | **45/45 全部已被点击**：38 真机 + 3 mock 流 + 1 mock 401 + 1 e2e 内激活 + **2 审批卡真机点击**（原记「产品不可达」已证伪） |
| 原「残余不可达」 | ~~审批卡「批准」「拒绝」：`auto_approve` 硬编码 → 永不渲染~~ **已证伪**：门是 `session/service.py:348` 的 `permission_mode_explicit`（显式选权限档位即开启交互式审批），与 `auto_approve` 无关。两键已真机点击，后端 JSONL 落库 `permission/resolved`（由测试自身轮询断言）；回归锁 `web/e2e/n-approval-card.spec.ts`；真机脚本走独立联调车道 `web/e2e-live/` + `playwright.live.config.ts` |
| 新登记问题 | **OBS-015（P2，前端，预存在，需产品决策）**：`ApprovalCard.tsx:26-33` 的 `catch` 对任何错误都翻成「已批准/已拒绝」，与注释「其它错误保持 pending」相反 → 审批 POST 失败时是乐观假象。本轮未改代码（§8） |
| 审查 | 两轮独立审查。`m-stream-affordances` **approve**（0 P0/P1/P2、6 项 P3 全修）；审批卡一轮发现 **2 项 P2**（URL 会话 id 未断言、联调车道分不清「后端已决」与「乐观 UI」）**均已修 + 变异验证**，P3 三项处置 |
| 自曝缺陷 | 新建联调车道时 `vitest.config.ts` 的 `exclude` 漏了 `e2e-live/**` → 单测车道被 Playwright 用例污染而变红；已修，并写入 HANDOFF §6 警示 |
| 关单 | 不适用（缺陷/覆盖批次，非 ticket 交付） |

### 最近一批：刷新一致性 BUG-005 / BUG-006 + 第二轮逐按钮巡检（2026-09-11）

| 项 | 值 |
| --- | --- |
| 起始 commit | `cf8f3a7` |
| 本批 commit | `138b056`（+ 后续小提交回填本 hash） |
| 门禁 | tsc ✓ / vitest **490 passed**（28 文件）/ oxlint **35w 0e**（基线持平）/ playwright **96 passed**（86 → +10）/ vite build ✓ |
| 交付 | BUG-005 刷新恢复选中会话；BUG-006 流式中刷新 → `?after_seq=` 接回流继续收事件；新增 `lib/sessionRestore.ts`(+test)、`api.ts` `NotFoundError`、`e2e/k-refresh-restore.spec.ts` |
| 真机验证 | BUG-006 **决定性取证**：真实后端 run 在途时 F5 → `GET /stream?after_seq=2 [200]`，零交互下事件 3 → 54 条直到 `run/completed`；与后端真值 54 条 / 0 重复 / 0 空洞。BUG-005 刷新前后正文指纹 `-271347586` / 4102 字符逐项一致 |
| 巡检 | 第二轮 66 行逐按钮表（密度/主题/Inspector 收起/Workspace/四选择器/五 Tab/命令面板/委派节点/分叉/令牌弹窗/preset chip/发送禁用/停止/请求量）全部通过 |
| 新发现 | BUG-007（命令面板 label 全英文，中文查询零命中）——**已修复**（label 本地化 + `keywords` 别名，英文仍可搜）；OBS-008（`glm-5.3-flash` `model/failed: RuntimeError`）、OBS-009（bash 工具 10s 超时且 `retryable:false`）均为**后端/provider**问题，前端渲染忠实 |
| 审查 | 第一轮 6 findings（3×P2 + 3×P3）：4 修 + 2 说明理由不改；第二轮见集成提示词 |
| 关单 | 不适用（缺陷修复批次，非 ticket 交付） |

### 上一批：恢复/分叉/滚动 三缺陷 + 真实浏览器逐按钮巡检（2026-09-11）

| 项 | 值 |
| --- | --- |
| 起始 commit | `8469a34`（另一 Agent 的 BUG-001 修复） |
| 本批 commit | `32356f4`（缺陷修复 + 巡检）、`0d6b82d`（架构扫描低风险项） |
| 门禁 | tsc ✓ / vitest **472 passed**（27 文件）/ oxlint **35w 0e** / playwright **86 passed** / vite build ✓ |
| 交付 | 交接手册 A/B/C/D；额外 BUG-004（Copy Run ID）+ 分叉 30s 超时反馈 |
| 真机验证 | A/B/D 三项在真实浏览器 + 真实后端复验（回执见 `FRONTEND_ISSUES_LOG.md` OBS-003/OBS-004）；43 行逐按钮巡检表 |
| 遗留 | OBS-007（中断会话绿色「已完成」脉冲与中断横幅矛盾，**预存在、故意未修**，§8）；覆盖缺口清单见集成提示词 §4 |
| 关单 | 不适用（缺陷修复批次，非 ticket 交付） |
| 架构扫描 | `/improve-codebase-architecture` 已完成。已修：候选 3（Inspector Run 摘要收归 `runState`，`0d6b82d`）+ 候选 4 字段级文档。**未做（按扫描结论 + §8）**：候选 1 `StreamOrchestrator`（最热路径，需监督 + 测试先行）、候选 2 `useFollowLatest`（代码库已显式推迟，ADR-0016）。报告：`%TEMP%rchitecture-review-20260911-0345.html` |

---

## Ticket 状态总览

| Ticket | 描述 | 状态 | Commit |
| --- | --- | --- | --- |
| FE-T7 | 会话级模型切换 + Fork UI（#137） | `done` | `71c01dd` + review 修复 `c6e6fab` |
| FE-T8 | 崩溃恢复 UI — run/interrupted + 409 守卫（#138） | `done` | `c137a23` |
| FE-T9 | 轮次标签（turn_index 显示）（#139） | `done` | `d2bfbc8`（类型/投影层）+ `cddea36`（UI 层） |
| 前置 | 重新生成 event-types（MODEL_CHANGED + RUN_INTERRUPTED） | `done` | `6012414` |
| 深化 C4 | api 层成为唯一归一化点 | `done` | `9b2234f` |
| 深化 C3 | Composer 档位 → 提交字段的单一构造器 | `done` | `9b2234f` |
| 深化 C2 | projection 事件语义注册表（编译期穷尽） | `done` | `f481ea5` |
| 深化 C1 | StreamOrchestrator 流式编排深化 | **`partial`** | `ae341e2`（第一刀） |
| 深化 review 修复 | 字段表编译期锁 + 时间参数集中 + 记录修订 | `done` | `2735422` |
| 深化 C5 | ConversationState 拆分 | `rejected` | —（YAGNI + 参考实现反证，见 `docs/ARCHITECTURE_REVIEW.md`） |

### 全部完成后的步骤

| 步骤 | 状态 |
| --- | --- |
| /improve-codebase-architecture | `done` → `docs/ARCHITECTURE_REVIEW.md`（`3e71b33`） |
| 深化批次实施 | `partial`（C1 剩余部分见下） |
| 写集成 AI 交接提示词 | `done`（`25917c1` 初版 → 本轮补深化批次 + 拓扑重测 + AGENTS.md 冲突预判） |

---

## 门禁基线（本轮全绿）

| 门禁 | 结果 |
| --- | --- |
| `npx tsc -b` | 0 error |
| `npx vitest run` | 408 passed / 27 files |
| `npx oxlint` | 0 error / 35 warning（全部既有，非本批引入） |
| `npx playwright test --workers=2` | 46 passed |
| `npx vite build` | OK（仅既有 chunk-size 提示） |

---

## 已完成批次：FE-T7 / T8 / T9（#137–#139）

### FE-T7 会话级模型切换 + Fork UI

- API：`changeSessionModel(sessionId, provider, modelId)` → `POST /api/sessions/{id}/model`；
  `forkSession(sessionId, fromSeq)` → `POST /api/sessions/{id}/forks`。
- 投影：`model/changed` → 更新 `conversation.model`。
- UI：ModelPicker 走 POST 并以**响应回传的规范 model_id**更新本地状态（不回显请求值）；
  用户消息上的「分叉」入口（`Conversation.tsx` + `.fork-btn`）。
- review 修复（`c6e6fab`）：CSS 变量改正、响应 model_id 采用、移除越出 T8 范围的改动。

### FE-T8 崩溃恢复 UI

- 投影：`run/interrupted` → `finalizeRun` + `ConversationState.run_interrupted`
  `{ step_id, interrupted_seq, reason }`。
- `sendFollowUp` 捕获 **409**：解析 detail，抛「存在需要人工裁决的高风险操作」，
  **不伪造结果继续**（后端硬拒绝，不变量 #14）。
- UI：中断横幅（`.interrupt-banner`）。

### FE-T9 轮次标签

- **数据层（`d2bfbc8`）**：`run/started` 提取 `data.turn_index`（后端 `session.begin_run`
  定义为「该 session 第几个 run，1-based」，每次 run 各自携带）→
  `ConversationState.turn_index`（会话级，供 Langfuse/turn 元数据）。
- **UI 层（`cddea36`）**：`Turn.turn_index`（per-turn 事实）→ `TurnView` 显示「第 N 轮」。
  ⚠️ 修正：原计划复用会话级 `state.turn_index` 传入 `TurnView`，但该字段被最新 run
  覆盖 → 所有历史轮次会显示同一个数字。改为把 RUN_STARTED 的值落到**当轮 turn** 上。

---

## 深化批次（架构评审 → 实施）

评审产物：`docs/ARCHITECTURE_REVIEW.md`（5 个候选 + 「选择的最佳路径」+ 「交付状态」+ 「已披露的行为变化」）。

**执行顺序（先低风险后深水）**：C4 → C3 → C2 → C1；C5 已否决。

### C4 + C3（`9b2234f`，一个 SDD 循环覆盖两个 candidate——偏离「每 candidate 一轮」，已记录）

- 新增 `lib/amend.ts`：`toAmendFields` / `toCreateControls`——Composer 档位（camelCase）
  → 契约字段名的**单一映射点**；明确**不做**空值丢弃（丢弃归 api 层）。
- `lib/api.ts`：`startSession` 成为与 `sendMessage` 同款的「有值才带键」执行点；
  空值 / 空数组不发键 = 后端默认。
- 新增 `lib/amend.test.ts`（5 例）锁字段集边界与「映射层不判空」契约。

### C2（`f481ea5`）事件语义注册表

- `applyEvent` 与 `summarizeEvent` 两个并行 switch 收敛为
  `EVENT_SEMANTICS: Record<EventTypeValue, EventSemantics>`（`{ apply, summarize }`）。
- **穷尽性经实验证伪**：注入 `FUTURE_THING` → `tsc` 报 TS2741；
  还原后 `git diff --stat` 干净。生成物新增事件类型而忘记登记 → 编译失败。
- 词汇表内但前端零处理的 7 个类型（`artifact/externalized`、`context/compaction_start|end`、
  `message/queued`、`queue/cancelled`、`steer/requested|applied`）**显式登记**为
  `unhandledProjection`（保持既有兜底行为进 `unknown_events`），使缺口可见而非静默。

### C1（`ae341e2`）第一刀——`ReconnectController`

- 新增 `lib/reconnect.ts`：**无 React / 无定时器 / 无 I/O** 的重连策略状态机。
  调用方拿 `decision` + `delayMs` 后自行调度（参考 deepseek-harness `BlockStreamer`
  的 injectable clock、pi-mono `lane.ts` 把 operation 生命周期从编排循环剥出）。
- 契约原语一并迁入 `decideStreamEnd` / `reconnectDelayMs` / `MAX_RECONNECT_ATTEMPTS`；
  时间常量 `RECONNECT_STALL_MS` / `RECONNECT_BANNER_DELAY_MS` 在 review 修复 `2735422` 随迁
  （评审「速度」目标：退避 / 停摆阈值 / banner 延迟集中为一处）。`useSession.ts` 以 re-export
  保持既有导入路径不破。
- 行为逐点对齐旧闭包：准入即占单飞并递增额度；`observeProgress` 只在 seq **严格超过**
  重连起点游标时复位额度（重放旧帧不是真进展，否则额度永不耗尽 → `give-up` 不可达 →
  悬空 run 无限重连）；`release` 放单飞但不动额度；`hold` 是 truncated 全量重建的占位；
  `reset` 是流的生命周期边界。
- 新增 `lib/reconnect.test.ts`（19 例）锁此前无测试的状态迁移。

---

## 剩余工作

### 1. C1 深水部分：`StreamOrchestrator`（未做，风险最高，需完整回归）

`attachLiveStream` 仍是约 200 行嵌套闭包，以下四类降级路径与合帧提交仍在 hook 内：

| 路径 | 现状位置 |
| --- | --- |
| 合帧批量提交（性能正向路径） | `coalescer` / `coalescerRef` |
| 停摆心跳 + visibilitychange | `stallCheckRef` / `stallCheck` |
| truncated 全量重建 | `doTruncatedRebuild` |
| seq-gap 分流 | `onEvent` 内 `isSeqGap` 分支 |

目标形状（评审「主路径」）：`lib/stream-orchestrator.ts`，构造注入
`fetchStream` / `scheduleTimer` / `clock`，内部按 pi-mono `drive/` 的状态分派
拆成 recovery / rebuild / stall 子模块；生产（SSE 解析）与消费（投影应用）分离
（pi-mono `EventStream<T,R>`）。

**为何本轮不做**：该段处于每帧热路径与全部降级路径的交汇处，无监督长会话收尾阶段
风险过高。先剥出重连策略状态机并锁单测，是后续提取的安全网前置条件。

### 2. 已登记待定项（不在本批范围）

- `session/forked`：已知类型但 Timeline 摘要仍落「未知事件」文案（pre-existing，
  文案变更未经确认）。C2 注册表按等价迁移保留，已登记。
- 上述 7 个未接线类型：需产品确认是否显示。
- `api.ts` 中 `START_SESSION_FIELDS` 与 `SEND_MESSAGE_FIELDS` 的 amend 四项各自登记
  （编译期各自穷尽，但同一字段集两处书写）——是否抽公共表待定，当前按
  「简单优先」保留显式重复。

### 3. 集成交接提示词（已更新）

`docs/integration/FRONTEND_INTEGRATION_PROMPT.md` 已重写：14 个 commit 清单、
新 HEAD `d5a8dca`、深化批次新增模块、两处已披露行为变化、C1 未完成范围，
以及**重测后的拓扑与冲突预判**。

#### ⚠ 拓扑已变，冲突预判与初版相反

初版写「ahead/behind 6/0，预期 merge 干净」。重测：merge-base `5c07fff`，
`main` `c5149ad`，**14 / 13**（main 在 merge-base 后又前进 13 个 commit）。

`git merge-tree --write-tree --name-only main HEAD` → **`AGENTS.md` 必然冲突**：
两侧都在 §15 之后追加同号 §16（`main` 版是后端/Primary 口径，本分支版是前端口径）。
按 §14.7 已给出 9 项分析与推荐统一语义（保留 main 版为唯一 §16，前端内容折叠为
`## 16.6 前端 worktree 补充`）——**留给集成 AI + 用户决策，本分支不自行解决**。

`web/src/generated/event-types.ts` 两侧都改过但**逐字节相同**，auto-merge 零差异。
其余 `main` 侧变更全在 `src/**` / `tests/**` / `docs/**`，与 `web/**` 无交集。

#### 已识别但未做的协议项

`main` 的 AGENTS.md §16.4 要求「每个 ticket 完成后更新 `docs/PHASE_STATUS.md`」。
本分支按前端协议记在 `docs/SDD_TICKET_TRACKER.md`，**未写 PHASE_STATUS.md**——该文件既有
条目均为「合入 main 后回填」，故在交接触提示词 §6 提供了建议条目文本，由集成 AI 在
merge 后追加。这是本批的协议偏离，记录在案。

---

## 4. 真机验收批次（2026-09-11）：逐按钮巡检 + 刷新一致性

用户要求「用真实浏览器把每个功能按钮都点一遍，问题实时写进文档，并检查刷新后会话是否与刷新前一致」。本批不新增 ticket，交付物是**问题登记簿 + 修复 + 回归锁**。

- 登记簿（单一事实源）：`docs/FRONTEND_ISSUES_LOG.md`——含 74 行逐按钮巡检表、BUG-005/006/007、OBS-007～010、前后端归因、三轮 code-review 处置。
- 交接提示词：`docs/integration/FRONTEND_REFRESH_PERSIST_INTEGRATION_PROMPT.md`（§1 改了什么 / §2 真机取证 / §8 OBS-007）。

| commit | 内容 |
| --- | --- |
| `138b056` | BUG-005 刷新恢复选中会话 + BUG-006 在途 run 接回流（`after_seq=N` 重放+续流） |
| `21fb004` | 文档回填 138b056 |
| `03d6a70` | BUG-007 命令面板本地化 + 可搜索英文别名 |
| `b4181ad` | OBS-007 中断脉冲第四态 + 清 `run_interrupted` 标记（含审查 P3 处置） |

**已修**：BUG-005、BUG-006、BUG-007、OBS-007。
**判定为后端/非前端**（仅记录，未改）：OBS-008（`glm-5.3-flash` 工具成功后 `model/failed`）、OBS-009（bash 工具 10s 超时上限与 `retryable` 语义）、OBS-010（`GET /api/sessions` 的 `trace_id` 恒为 `null`，但会话详情事件里的 `trace_id` 正常，故 UI 的 Trace 命令实际可用——**原登记曾误判为「命令不出现」，已订正**）。
**已知覆盖缺口**：~~OBS-006 审批卡不可达（`auto_approve` 硬编码）~~ **已证伪并闭合（第五轮真机点击）**；`已中断` 脉冲态真实语料不可达（仅单测）；`pulse-interrupted` 类名字符串与 CSS 选择器无测试绑定。

**子会话刷新一致性（追加真机验证）**：委派 child `2515a128`（列表点击 / 「打开子会话」两条入口）与分叉 child `1fdac9b9`（410 事件）刷新前后正文指纹**逐字节相同**（日志见登记簿对应章节）。新增回归锁 1 例（×2 视口）——首版播种式被变异验证证伪（只覆盖读路径），已改为真实点击写入路径 + 按 id 区分事件。

**第三轮控制面清点（可核对方法）**：从源码枚举全部 **45 个 `<button>`**（17 文件）逐个核对。初版**关键词比对**不可靠（两个方向都会错：`保存`/`清除` 命中的是无关散文 → 令牌弹窗两键实际没点过却判 OK；`滚动到最新`/`恢复会话` 其实有覆盖却判缺失），**故改为逐个真机点击**。最终 **45 = 38 点过 + 1 补 e2e + 3 按设计不可达 + 3 瞬态窗口不可达**：

- **38 个真机点击通过**（本轮新验含：Inspector 5 tab、加载更早 200→410、时间线行跳转、终端行→工具焦点、io-tabs ×4、JSON 展开、返回父会话、代码块换行、**推理块展开**、**Inspect chip**、**Inspector 工具行**、**令牌保存/清除**、空态示例 chip）；
- `auth-banner-close` 本地不可达（后端仅配 `jwt_secret` 时 401）→ 新增 `web/e2e/l-auth-banner.spec.ts`（含变异验证）；
- **按设计不可达 3**：审批卡「批准」「拒绝」（`auto_approve` 硬编码，OBS-006）、`ContextProviderPicker`（本部署后端目录为空 → 正确不渲染）；
- **瞬态窗口 3 已在第四轮补齐**：`tool-out-wrap-btn`、`tool-out-jump`、`reasoning-jump` 原需「流式中 + 用户上滚」才渲染（cmd 缓冲输出使尾窗仅存毫秒级），已用 **mock 流钉住窗口**（不发 `tool/result` / `reasoning/completed` → 投影状态恒为 running/streaming）后**真实点击**并逐一变异验证，见 `web/e2e/m-stream-affordances.spec.ts`。

**最终覆盖账目（第五轮后）**：**45/45 全部已被点击**，其中审批卡「批准」「拒绝」为**真机点击**（真实后端 + 真实模型）。原记的「2 产品不可达」是**误判**并被证伪——审批卡由 `tool/approval-requested` 事件驱动（`projection.ts:514`），门在 `session/service.py:348`（`permission_mode_explicit`），与 `auto_approve` 无关；显式选「只读」后任何 workspace-write 工具都会触发。真机证据 + 回归锁见登记簿 OBS-006 订正条。**不再有「未验证」按钮。**

**新发现的后端问题（含根因行号，需后端修复）**：OBS-011 子进程输出按 UTF-8 解码而 cmd.exe 输出 GBK → **乱码固化进 JSONL**（`sandbox/local.py:166-167`，铁证：原始字节中 U+FFFD 与侥幸合法的 GBK 双字节混杂）；OBS-012 `bash` 工具在 Windows 实为 cmd.exe（`shell=True`，`local.py:161`）→ bash 语法 41ms 失败；OBS-013 provider 退化重复（2,868 delta / 186,507 字符的同句循环，3.5 分钟无工具调用，另见多次 `model/fallback … InternalServerError`）；OBS-014 bash 工具 10.0s 硬超时且 `retryable:false`。

**本批最终门禁（实跑）**：tsc ✓ · vitest **497 passed**（28 文件）· oxlint **35 warnings / 0 errors** · playwright **112 passed**（`--workers=2`：瞬态三键 4 例 + 审批卡 8 例）· vite build ✓。联调车道（`e2e-live/`）用例不计入主车道（已核验计数 0）。
**第四轮新 spec 的独立审查**：0 个 P0/P1/P2，6 项 P3 **全部已修**（头注释挂载条件、合成滚动划界、显式 `aria-expanded`、数值化可滚动断言、`toHaveCSS` 断生效样式、作废指针），并按修改后版本**重跑三处变异**（均红）。详见登记簿「第四轮收尾」。

**本轮审查（`l-auth-banner.spec.ts`）**：0 个 P0/P1，1 项 **P2** + 4 项 P3，**全部已处置**。P2 是**注释谎报覆盖**——我写「`api.test.ts` 测 401 分类」，实则全 `src` 测试树零个 401 引用（该缝当时**无单测**）。已把谎报改成事实：`api.test.ts` 新增 3 例（401→`UnauthorizedError`、广播 detail、**body 非 JSON 的回退文案**）。P3 中一项揭示了**真实行为被我注释说反**：关闭**不是**永久忽略（`App.tsx:148` 每次广播都会重新显示），故 e2e 改为走「配置令牌」真实路径断言**横幅重新出现**（变异验证：删掉 `refreshSessions()` → 两视口都红）。
