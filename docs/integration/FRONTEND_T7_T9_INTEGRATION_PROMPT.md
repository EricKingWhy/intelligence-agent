# 集成 AI 提示词 — feat/frontend → main

> **给集成 AI（Git Integrator）的执行提示词。**
> 按 AGENTS.md §14 集成规则执行；merge / push 需用户明确批准。
>
> **本文件已更新**：原版只覆盖 T7–T9，且拓扑已过期（见 §1、§2）。当前 HEAD 为 `d5a8dca`。

---

## 0. 任务

将 `feat/frontend` 分支合入 `main`。

**内容**：T7 #137 + T8 #138 + T9 #139 前端实现，加架构深化批次（C1–C4）+ SDD 防漂移协议文档。

### 0.1 Commit 清单（相对 merge-base `5c07fff`，共 14 个）

| commit | 内容 |
| --- | --- |
| `b9b9c88` | docs: SDD 工作流协议 + Ticket Tracker（防指令漂移） |
| `6012414` | chore(web): 重新生成 event-types.ts——新增 MODEL_CHANGED + RUN_INTERRUPTED |
| `71c01dd` | feat(web): T7 会话级模型切换 + Fork UI（#137） |
| `c6e6fab` | fix(web): code-review 修复——CSS 变量 + 响应 model_id + 移除 T8 范围 |
| `c137a23` | feat(web): T8 崩溃恢复 UI — run/interrupted + 409 守卫（#138） |
| `ff458f2` | docs: 更新 SDD Ticket Tracker——FE-T7 完成 |
| `d2bfbc8` | feat(web): T9 轮次标签——turn_index 显示（#139） |
| `3e71b33` | docs: 架构评审——5 个深化候选 + 选择的最佳路径 |
| `25917c1` | docs(integration): feat/frontend T7+T8+T9 集成交接提示词 |
| `9b2234f` | refactor(web): 架构深化 C3+C4——Composer 档位映射 + 归一化归属 api 层 |
| `f481ea5` | refactor(web): 架构深化 C2——事件语义注册表（编译期穷尽性） |
| `ae341e2` | refactor(web): 架构深化 C1 第一刀——ReconnectController 重连策略状态机 |
| `2735422` | fix(web): 深化批次 code-review 修复——字段表编译期锁 + 时间参数集中 |
| `d5a8dca` | fix(web): code-review 二轮——契约键类型锁 + 文档/追踪表勘误 |

---

## 1. Git 拓扑（**已重测，与初版不同**）

| 项 | 值 |
| --- | --- |
| Worktree | `D:\intelligence-agent-frontend` |
| Branch | `feat/frontend` |
| HEAD | `d5a8dca` |
| `main` | `c5149ad` |
| merge-base(`HEAD`, `main`) | `5c07fff` |
| ahead / behind | **14 / 13** |

> ⚠ 初版提示词写的是 `6 / 0`。**`main` 已在 merge-base 之后前进 13 个 commit**
> （含后端 T7/T8/T9 合入、#136 审批超时、多份 PRD/调研文档、`AGENTS.md` §14.12 + §16）。
> 按 AGENTS.md §14.9，此前的冲突判断**全部过期**，本文件的预判是重测结果。

---

## 2. 冲突预判：**`AGENTS.md` 必然冲突**（§14.7 分析，**请勿机械解决**）

`git merge-tree --write-tree --name-only main HEAD` 输出：

```
Auto-merging AGENTS.md
CONFLICT (content): Merge conflict in AGENTS.md
```

这是**唯一**冲突文件。按 §14.7 逐项分析：

| # | 项 | 结论 |
| --- | --- | --- |
| 1 | `main` 改了什么 | 在 §15 之后追加 `# 16. SDD 长任务工作流协议（防指令漂移）`，含 16.1 单 Ticket SDD 循环 / 16.2 Bug 处理协议 / 16.3 全部完成后 / 16.4 防漂移纪律 / 16.5 进度追踪；另在 §14 后追加 §14.12 Ticket 关单纪律 |
| 2 | `feat/frontend` 改了什么 | 同样在 §15 之后追加 `## 16. SDD 工作流协议（防指令漂移）`——短版，指向 `docs/SDD_WORKFLOW_PROTOCOL.md` + `docs/SDD_TICKET_TRACKER.md` |
| 3 | 为何冲突 | 两侧都在**同一位置**（文件末尾）追加同号 §16，锚点相同、内容不同 |
| 4 | 两边能否同时保留 | **能**，且应当——两者互补而非互斥：`main` 版是 Primary/后端口径（ruff + pytest + `gh issue close` + `PHASE_STATUS.md`），前端版是 Secondary/前端口径（tsc + vitest + oxlint + playwright + build，独立协议文件 + tracker）。真正重复的是「循环骨架」，真正分歧的是「门禁工具链」与「进度落哪个文件」 |
| 5 | 推荐的最终语义 | **保留 `main` 版为唯一 §16 正文**，把前端内容折叠为一个小节（如 `## 16.6 前端 worktree 补充`）：前端门禁为 `tsc -b` / `vitest run` / `oxlint` / `playwright test --workers=2` / `vite build`；前端在途进度记 `docs/SDD_TICKET_TRACKER.md`，整合后的进度仍记 `docs/PHASE_STATUS.md`；并保留 `docs/SDD_WORKFLOW_PROTOCOL.md` 作为前端细化版。**不要**保留两个 §16 标题 |
| 6 | 影响 Contract | 无（纯文档） |
| 7 | 影响 Runtime | 无 |
| 8 | 影响 Test | 无 |
| 9 | 风险等级 | **低**（文档），但用 `ours`/`theirs` 机械解决会**静默丢掉一侧的防漂移协议**——正是 §14.7 明令禁止的 |

### 其余路径：无冲突

`web/src/generated/event-types.ts` 两侧都改过，但**内容逐字节相同**：
`git diff main:web/src/generated/event-types.ts HEAD:web/src/generated/event-types.ts` 为空
（同为后端 `session/event.py` 的生成物），auto-merge 后零差异。`main` 侧其余变更全在
`src/**` / `tests/**` / `docs/**`（后端域），与前端 `web/**` 无交集。

### 本项目新增文档（`main` 上不存在，纯新增，无冲突）

`docs/SDD_WORKFLOW_PROTOCOL.md`、`docs/SDD_TICKET_TRACKER.md`、`docs/ARCHITECTURE_REVIEW.md`、
`docs/integration/FRONTEND_T7_T9_INTEGRATION_PROMPT.md`。

---

## 3. 门禁证据（在 `d5a8dca` 复跑）

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| Type check | `npx tsc -b` | exit 0，无输出 |
| 单元测试 | `npx vitest run` | **27 files / 408 tests passed** |
| Lint | `npx oxlint` | **0 errors / 35 warnings**（全部既有，非本批引入） |
| e2e | `npx playwright test --workers=2` | **46 passed** |
| 生产构建 | `npx vite build` | ✓ built（仅既有 chunk-size 提示） |

---

## 4. 契约接触面

### 4.1 新增 API 函数（`web/src/lib/api.ts`）

```typescript
// POST /api/sessions/{id}/model
changeSessionModel(sessionId, provider, modelId): Promise<ModelChangeResult>

// POST /api/sessions/{id}/forks
forkSession(sessionId, fromSeq): Promise<ForkResult>
```

### 4.2 新增事件投影（`web/src/lib/projection.ts`）

| 事件 | 投影行为 |
| --- | --- |
| `model/changed` | 更新 `conversation.model` 为 `data.to_model_id` |
| `run/interrupted` | `finalizeRun` 标记为终态；存 `run_interrupted` |
| `run/started` | 提取 `data.turn_index` → `ConversationState.turn_index` |

### 4.3 深化批次新增模块（**新的对外接口面**）

| 文件 | 内容 |
| --- | --- |
| `web/src/lib/amend.ts` | `toAmendFields` / `toCreateControls`——Composer 档位 → 契约字段名的单一映射点（不判空） |
| `web/src/lib/reconnect.ts` | `ReconnectController` 状态机 + `decideStreamEnd` / `reconnectDelayMs` / `MAX_RECONNECT_ATTEMPTS` / `RECONNECT_STALL_MS` / `RECONNECT_BANNER_DELAY_MS` |
| `web/src/lib/projection.ts` | `EVENT_SEMANTICS: Record<EventTypeValue, EventSemantics>` 穷尽注册表取代两个并行 switch |
| `web/src/lib/api.ts` | `BodyFields<T>` 字段表取代手写请求体（payload 新增字段未登记 → tsc 失败） |

### 4.4 新增 UI

- ModelPicker 选择后调 `POST /api/sessions/{id}/model`（而非仅本地状态更新），并以**响应回传的规范 `model_id`** 更新本地状态（不回显请求值）
- 历史用户消息上的 fork 入口 → `POST /api/sessions/{id}/forks` → 跳转 child session
- 中断横幅：「上次运行在第 N 步中断」
- 轮次标签：TurnView 显示「第 N 轮」

### 4.5 ⚠ 已披露的行为变化（合并影响评估用）

深化批次以「行为保持」为约束，但有两处**有意**改变，已登记在 `docs/ARCHITECTURE_REVIEW.md`
「已披露的行为变化」：

1. **Timeline 摘要**（`f481ea5`）：`run/interrupted` 由「未知事件 · …」变为「第 N 步中断」
   （`step_id` 缺失时「运行中断」）；`model/changed` 由「未知事件 · …」变为「模型 → X」
   （`to_model_id` 缺失时「模型已切换」）。旧行为把**已处理**的类型渲染成「未知事件」，
   与 projection 内「未知兜底只留给真正未知类型」的既定注释自相矛盾。
2. **create 请求体**（`9b2234f`）：`context_providers: []` 不再发键（此前会发）。这是
   「有值才带键」的既定语义，与 `SendMessagePayload` 的已知 Gap 一致；后端区分 None / `[]`，
   前端选择器当前无法表达「零个」——如需 `[]` 语义请先定契约（见
   `docs/integration/CONTRACT_CONTEXT_PROVIDERS_EMPTY.md`）。

### 4.6 明确未动的部分

- SSE 帧形状 / seq 投影 / 消费机器**零改动**（`consumeSSE` 未改）
- 未迁 WebSocket
- `permission_mode` 不在 `/messages` 的 amend 契约内，未传

---

## 5. 集成步骤

```text
1. 前置检查
   git worktree list --porcelain
   git -C D:/intelligence-agent-frontend status --short
   git -C D:/intelligence-agent-frontend log --oneline -1   # 应为 d5a8dca

2. 先回后正（§14.6，需用户批准）
   git -C D:/intelligence-agent-frontend fetch origin --prune
   git -C D:/intelligence-agent-frontend merge main
   # AGENTS.md 会冲突 → 停止，按 §2 的推荐语义统一为一个 §16，再请用户确认

3. 在 feat/frontend 上复跑门禁（§3 五条命令）

4. 合入 main（§14.4，需用户批准）
   git -C D:/intelligence-agent merge feat/frontend

5. main 上验证
   复跑门禁 + 在 D:\intelligence-agent 起完整项目做前后端联调

6. 追加 PHASE_STATUS.md 记录（§6，纯新增条目，不会冲突）

7. push（§14.4，需用户批准）
   git -C D:/intelligence-agent push origin main
```

**禁止**：`git pull`、在 dirty worktree 上 merge、`reset --hard`、`rebase`、`push --force`、
未经批准删分支/worktree。

---

## 6. `docs/PHASE_STATUS.md` 待追加条目（建议文本）

本分支按前端协议记在 `docs/SDD_TICKET_TRACKER.md`，**未**写 `main` 的 `PHASE_STATUS.md`
（该文件的既有条目均为「合入 main 后回填」，故留给你在 §5 步骤 6 追加）：

```markdown
- 2026-09-10：**集成记录：feat/frontend T7+T8+T9 + 架构深化批次 → main**。14 commits
  `b9b9c88..d5a8dca`（含 4 个 code-review 修复 commit）。交付：①T7 #137 会话级模型切换 +
  Fork UI（`changeSessionModel`/`forkSession` + `model/changed` 投影 + 用户消息 fork 入口）；
  ②T8 #138 崩溃恢复 UI（`run/interrupted` 投影 + `/messages` 409 人工裁决守卫，不伪造结果继续）；
  ③T9 #139 轮次标签（`RUN_STARTED.data.turn_index` → TurnView）；④架构深化 C1–C4
  （`lib/amend.ts` 单一映射点、`lib/reconnect.ts` 重连状态机、projection 事件语义穷尽注册表、
  api 请求体字段表编译期锁）；⑤SDD 防漂移协议（`docs/SDD_WORKFLOW_PROTOCOL.md` +
  `docs/SDD_TICKET_TRACKER.md` + AGENTS.md §16）。**冲突**：仅 `AGENTS.md`（两侧都追加 §16，
  已按统一语义合并为单节）；`web/src/generated/event-types.ts` 两侧改动逐字节相同，零差异。
  门禁：tsc 0 + vitest 27 files/408 passed + oxlint 0 error/35 既有 warning + playwright 46 passed
  （--workers=2）+ vite build OK。**未完成**：C1 深水部分 `StreamOrchestrator`（`attachLiveStream`
  仍是约 200 行嵌套闭包，coalescer/stallCheck/doTruncatedRebuild/seq-gap 未集中）——已在
  `docs/ARCHITECTURE_REVIEW.md`「C1 未完成部分」声明范围与不做的风险理由，留作后续 ticket。
```

---

## 7. 未完成项 & 后续 ticket

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| T7 前端 | ✅ 完成 | 模型选择器、fork 入口、`model/changed` 监听 |
| T8 前端 | ✅ 完成 | 中断横幅、409 人工裁决守卫 |
| T9 前端 | ✅ 完成 | 轮次标签 |
| C2 / C3 / C4 深化 | ✅ 完成 | 见 §4.3；C2 穷尽性已实验证伪 |
| **C1 深化（StreamOrchestrator）** | ⚠ **部分交付** | 仅第一刀 `ReconnectController`。剩余：`coalescer`（合帧提交，性能正向路径）、`stallCheck`（停摆心跳）、`doTruncatedRebuild`（全量重建）、seq-gap 分流仍在 hook 内。目标形状 `lib/stream-orchestrator.ts`，构造注入 `fetchStream`/`scheduleTimer`/`clock`（参考 deepseek-harness `BlockStreamer`、pi-mono `lane.ts`+`drive/`、`EventStream<T,R>`）。**本轮不做**：处于每帧热路径与全部降级路径的交汇处，无监督收尾阶段风险过高 |
| C5（ConversationState 拆分） | ❌ 已否决 | YAGNI + 参考实现反证（pi-mono `Session` 与 deepseek-harness `SessionEvent` 均扁平） |
| `session/forked` 摘要文案 | 📋 待定 | 已知类型但 Timeline 仍落「未知事件」文案（pre-existing，变更未经确认） |
| 7 个未接线事件类型 | 📋 待定 | `artifact/externalized`、`context/compaction_start\|end`、`message/queued`、`queue/cancelled`、`steer/requested\|applied` 已在注册表显式登记为 `unhandledProjection`（保持既有兜底），缺口已可见，需产品确认是否显示 |

---

## 8. 后端依赖确认

| 后端功能 | 后端 commit | 前端消费方式 |
| --- | --- | --- |
| `POST /api/sessions/{id}/model` | `ae553ad` (T7) | `changeSessionModel()` |
| `POST /api/sessions/{id}/forks` | `ae553ad` (T7) | `forkSession()` |
| `model/changed` 事件 | `ae553ad` (T7) | projection `MODEL_CHANGED` |
| `run/interrupted` 事件 | `ccebf9a` (T8) | projection `RUN_INTERRUPTED` |
| `RUN_STARTED.data.turn_index` | `c438a1e` (T9) | projection `RUN_STARTED` |

以上均已在 `main`（`c5149ad`）中。

---

## 9. 集成 AI 注意事项

1. **`AGENTS.md` 的 §16 冲突是本次唯一的语义决策点**——不要机械 `ours`/`theirs`（§14.7），
   按 §2 表格第 5 行的统一语义处理，并请用户确认。
2. **`web/src/generated/event-types.ts` 是生成物**——本分支版本与 `main` 逐字节相同，
   合入后无需再生成；若后端 `session/event.py` 后续再变，用
   `uv run python scripts/gen_event_types.py`（在**后端** worktree 跑）后同步。
3. **`AGENTS.md` §16 是本次新增的防指令漂移机制**，请勿删除。
4. **未推送远程**——本分支全部为本地 commit，push 归你执行。
5. **工作区无未提交改动**（除 `test-results/` 与两份历史 untracked 文档，均非本批产物）。
