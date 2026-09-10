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
| 后端交接手册 | `D:\intelligence-agent-backend\docs\HANDOFF_FRONTEND_T7_T9.md` |
| 集成交接提示词 | `docs/integration/FRONTEND_INTEGRATION_PROMPT.md`（已含深化批次 + 拓扑重测 + AGENTS.md 冲突分析） |

**禁止推送远程**（AGENTS.md §13.2/§14.4）：本地 commit 已完成，push 归集成 AI。

---

## Ticket 状态总览

| Ticket | 描述 | 状态 | Commit |
| --- | --- | --- | --- |
| FE-T7 | 会话级模型切换 + Fork UI（#137） | `done` | `71c01dd` + review 修复 `c6e6fab` |
| FE-T8 | 崩溃恢复 UI — run/interrupted + 409 守卫（#138） | `done` | `c137a23` |
| FE-T9 | 轮次标签（turn_index 显示）（#139） | **`partial`** | `d2bfbc8`（类型/投影层）+ `516e250`（UI 层） |
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
- **UI 层（`516e250`）**：`Turn.turn_index`（per-turn 事实）→ `TurnView` 显示「第 N 轮」。
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
