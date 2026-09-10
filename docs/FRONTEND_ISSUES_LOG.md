# 前端问题登记簿（实时更新）

> **用途**：真实浏览器点击测试 + 日常使用中发现的任何问题，实时登记在此。
> 修 bug 时照着本文档逐条处理；修完把状态改为 `已修复` 并补上 commit。
>
> 状态词汇：`未修复` / `修复中` / `已修复（commit）` / `不改（理由）`
> 严重级：`P0 功能不可用` / `P1 功能可用但体验破损` / `P2 边角 / 打磨`

---

## 问题清单

### BUG-001 分叉按钮 422：`turn.step_id` 不是后端要的用户消息 seq 【P0 · 已修复（8469a34）】

**发现时间**：2026-09-10 真实浏览器点击测试（会话 28eb3302，点第 2 轮的「分叉」）

**修复时间**：2026-09-10（commit `8469a34`）；回归锁与补强 2026-09-11

**现象**：点击用户消息上的「分叉」按钮 → `POST /api/sessions/{id}/forks` 返回 **422**，UI 无任何反馈（静默失败）。

**网络证据**：
- 请求体：`{"from_seq": 2}`
- 响应：`{"detail": "fork 边界 seq=2 不是父会话中的用户消息（可用边界: [1, 30, 46, 62, 199]）"}`

**根因**（`web/src/components/Conversation.tsx:290`）：
```tsx
onClick={() => onFork(turn.step_id)}
```
前端传的是 `turn.step_id`——这是投影层 `resolveStep` 为轮次分组**前端合成**的 step 值；而后端 fork 锚点要求的是**用户消息事件在 JSONL 里的 `seq`**（后端报错里列的可用边界 `[1, 30, 46, 62, 199]` 正是该会话 5 条 user/message 的 seq）。真实数据里 `user/message` 事件的 `step_id=None`，两个语义完全不同。

**修复方向**（最小方案）：
1. 投影层：`projectUserMessage` 已经拿得到 `event.seq`（user/message 的 seq 就是合法锚点），在 `Turn` 上记录 `user_message_seq: number`（per-turn 事实，与 T9 turn_index 落当轮同一模式）。
2. `Conversation.tsx:290`：`onFork(turn.user_message_seq ?? turn.step_id)` 改为只传 `user_message_seq`；没有该字段的历史 turn 显示分叉按钮但点击时给出提示，或对缺锚点的轮不渲染按钮（不造假入口）。
3. 错误反馈：`handleFork`（`web/src/App.tsx:312`）的 `catch {}` 是**空吞**——422/409 用户毫无感知。至少 `setError(...)` 显示后端 detail；409（在途 run）单独提示「等当前 run 结束再分叉」。

**实际落地**：
1. `Turn.user_message_seq: number | null`（`types.ts`）；`projectUserMessage` 仅在 `event.seq !== null` 时写入（`projection.ts`）——不伪造锚点。
2. 按钮只在 `onFork && status !== 'streaming' && user_message_seq !== null` 时渲染，点击传 `user_message_seq`；缺锚点不造假入口。
3. `forkSession`（`api.ts`）解析后端 `detail`；`handleFork` 的 catch 改为 `setForkError(\`分叉失败：${message}\`)`，渲染为 `role="alert"`。
4. 2026-09-11 code-review 补强：
   - 第 1 轮的按钮加 tooltip「本轮之前没有历史：child 会话将是空会话」——后端允许首个 user 消息当锚点，得到的 child 继承 model + 空 workspace，合法但反直觉，提前说清（交接手册 §B.3）。
   - `handleFork` 的结局（跳 child / 弹错误）落地前用 `selectedIdRef` 校验用户仍停在发起会话上，切走即丢弃（stale-write 纪律）；切会话的两条入口（会话栏选择 / 分叉跳 child）**同步**写该 ref，把「已切走但 effect 还没 flush」的窗口压到零。
   - `forkError` 带上发起它的 `sessionId`，只在与当前选中会话一致时渲染——**不是**在切会话时清空（少了 effect 与额外渲染）；代价是切回原会话仍会看到那条旧错误，符合「它确实是在那个会话上失败的」。
   - 分叉请求在途时忽略重复点击（`forkInFlightRef`）：按钮没有 pending 态，连点会在后端造出两个 child 会话。
   - 后端**未**新增端点——遵循交接手册 §B.4「建议不加」。

**回归**：
- `web/e2e/b-fork.spec.ts` B1 锁 `from_seq === 30`（user/message 的 seq，而非 turn 序号 2）；夹具刻意让 seq 与 turn 序号错开，且 `user/message` 不写 `step_id`（真实信封形状）。**变异测试**：把 `onFork(turn.user_message_seq!)` 改回 `onFork(turn.step_id)` → B1 变红；改回即绿。
- `web/src/lib/projection.test.ts`：新增两条——持久 seq 落到 `user_message_seq`（不写合成 step 号）、`seq === null` 不伪造锚点。
- `web/src/components/Conversation.test.tsx`：`TurnView — 分叉入口（BUG-001）` 6 条 SSR 契约。

---

### OBS-005 非默认 amend 档位会让模型调用 400【观察项 · 疑似后端/provider】

**发现时间**：2026-09-11 真实浏览器点击巡检

**现象**：把 Composer 控制行改成 `Agent Profile=编程` + `Reasoning Effort=深度` 后提交任务，连续 2 次都在 `seq=3` 落 `model/failed: "model call failed: BadRequestError"` → `run/failed`（会话 `ba4faa9a-…` / `70cca9c0-…`，`D:\intelligence-agent` 存储）。把两个档位都改回默认（`通用` / `标准`）后**同一任务立刻正常流式输出**。

**初步判断**：`agent_profile` / `reasoning_effort` 作为 amend 字段透传给 provider 时被拒（400）。前端只是把控制行选中的值放进 payload（`api.test.ts` 已锁 payload 形状），没有加工——**疑似后端/provider 侧对这些档位的处理**。需要后端确认这两个档位在 `senseaudio` 上是否受支持。

**前端表现（正常）**：run 失败被正确消费为「失败」run pulse + turn 渲染，无崩溃、无假成功。

---

### OBS-006 审批卡（#37）在本 UI 中不可达且无测试【观察项 · 覆盖缺口】

**发现时间**：2026-09-11 真实浏览器点击巡检（逐按钮清点）

**现象**：`ApprovalCard` 由 `Conversation` 按 `pending_approvals` 渲染，但：

1. **本 UI 无法触发**：`App.tsx` 提交任务时硬编码 `auto_approve: true`，所以从这里发起的 run 永远不会产生待审批项（只有「在别处以 `auto_approve=false` 发起的会话、再在本 UI 打开」才可能看到卡）。
2. **无测试**：`src/components/ApprovalCard.tsx` 没有单测文件，`e2e/` 也没有对应 spec——16 个 spec 里一个都没有审批场景。

**影响**：这个交互（批准/拒绝按钮 → 后端回调）目前既点不到也测不到，属未验证区域。本轮不做功能改动（超出交接手册 A–D 范围），仅登记。

---

### BUG-004 命令面板「Copy Run ID」复制的是 session id【P2 · 已修复（本次）】

**发现时间**：2026-09-11 真实浏览器点击巡检（Ctrl+K 面板逐条点击）

**现象**：命令面板执行「Copy Run ID」后，剪贴板得到的是**会话 id**，不是 run id。标签与动作不一致——两者都是 UUID，粘到日志查询/后端工单里只能用错地方才发现。

**根因**（`web/src/App.tsx` 创建命令处）：
```tsx
label: 'Copy Run ID',
hint: conversation ? conversation.session_id.slice(0, 12) : undefined,
run: () => conversation && copyText(conversation.session_id),
```
`ConversationState.run_id` 本来就存在（`types.ts`，注释写明「PRD §8.2 Inspector 头部 Run ID」），命令却抄了近处的 `session_id`。同区域的 `Copy Trace ID` / `Open Trace` 有 splice 兜底（值缺则命令移除），这条没有——`run_id` 为空时会留下一个点了没反应的假按钮。

**修复**：`run` 改为 `copyText(conversation.run_id)`；`run_id` 缺失时该命令整个不进入列表（条件展开生成，与 Trace 两条的「值缺则不出现」同一纪律）。

**回归**：`e2e/i-keyboard.spec.ts` 新增用例——夹具里 run id(`e2e-run-0002`) 与 session id(`e2e-session-0002`) 刻意不同，断言剪贴板等于 run id。**变异测试**：把 run 改回 `copyText(conversation.session_id)` → 断言变红（`Expected "e2e-run-0002" / Received "e2e-session-0002"`），改回即绿。

---

### BUG-003 工具输出面板内滚动会误触发对话「脱离跟随」【P1 · 已修复（本次）】

**发现时间**：2026-09-11 独立 code-review（Standards 轴，非用户报告）

**根因**：修 BUG 里的滚动问题时给 `.conversation-scroll` 加了 `wheel` 监听（用户上滚 → 同步脱离跟随）。但 `wheel` **冒泡**：工具输出面板 `.tool-out-body`（`max-height: 200px; overflow-y: auto`）与 reasoning 展开体本身就是独立滚动容器，在它们内部上滚同样会冒到会话容器，于是**对话根本没动**却脱离了跟随、弹出「↓ 最新」，之后所有 delta 都不再贴底。这是本次修复引入的回归（改动前的代码没有 wheel 监听）。

**修复**：`onWheel` 先沿 `e.target` 向上走到会话容器，把链上每个「可上滚的嵌套滚动容器」（`scrollHeight > clientHeight` 且 `overflow-y: auto|scroll`）的 `scrollTop` **累加**，再与本次上滚的位移比较：合计够 = 嵌套链吃下了这一下，忽略；不够 = 外层会被推动，释放跟随。累加（而不是「遇到第一个有余量的就忽略」）是必须的——浏览器按最内层→外层顺序分担，单看最内层会在内外层分担时误判「外层要动」，而「有余量就算」会在外层确实被推动时误判为不动（跟随仍为真、下一次 delta 把视口拽回，即原症状）。位移按 `deltaMode` 折算（像素 / ×16px 每行 / ×视口高每页）。算术收在 `lib/followLatest.ts` 的 `nestedChainAbsorbs` / `wheelDeltaPixels`。

另外补一条同类误报：容器已在顶部（`scrollTop <= 0`，内容没超视口或已滚到头）时上滚什么都不会动，不算「要离开底部」——`onWheel` 直接返回，否则浮标会为一次没发生的滚动弹出来。

**回归**：`lib/followLatest.test.ts` 锁累加语义、边界与 `deltaMode` 折算（含空链）；`e2e/j-scroll.spec.ts` 的浮标用例覆盖「对话容器内上滚」这一半；**按 DOM 走链收集余量那一半**（组件内 `parentElement` 循环）无自动化，属人工验证范围。

---

### BUG-002 交接手册 `HANDOFF_WORKBUDDY_FRONTEND.md` 关于 T9 的结论已过期 【P2 · 已过时，需勘误】

**发现时间**：2026-09-10 真实浏览器测试

**现象**：手册写「T9 未完成——TurnView 不渲染轮次标签」，但实际页面已真实渲染「第 1 轮」~「第 6 轮」。

**根因**：commit `cddea36`（T9 轮次标签 UI）已在当前 HEAD 里，手册是基于旧快照写的。**workbuddy 若照手册做会重复实现。**

**待办**：更新手册——T9 标记为已完成（commit `cddea36`），把「你需要做的工作」章节替换为指向 BUG-001（分叉 422）的修复任务。

---

### OBS-001 续聊 run 失败：`model call failed: BadRequestError` 【观察项 · 非前端 bug】

**发现时间**：2026-09-10 测试「会话级模型切换后续聊」

**现象**：切到 `glm-5.3-flash` 后发消息 → 后端 `model/failed`（`BadRequestError`）→ `run/failed`。

**前端表现**（正常）：header 显示「失败」、第 6 轮 turn 渲染出来、无崩溃。前端行为正确。

**初步判断**：后端模型调用问题（provider `senseaudio` 对该模型返回 400），属后端/环境问题。前端已正确消费 `model/failed` + `run/failed` 终态。

**附注**：这次失败同时验证了 T7 的链路真实可用——`model/changed` 事件（seq=319，`from qwen3.8-27b → to glm-5.3-flash`）落库，续聊请求带上了 amend 档位（`model`/`agent_profile`/`reasoning_effort`）。

---

### OBS-002 preset 任务按钮只在空态显示 【观察项 · 符合设计】

三个 preset 按钮（FizzBuzz / todo.md / 目录结构）只在未选会话的空态渲染，选中会话后消失。符合「空态引导」设计，非 bug，登记备查。

---

### OBS-003 恢复反馈一度会把「补齐 run 终态」谎报成「无可修复项」【已修复（本次）】

**发现时间**：2026-09-11 独立 code-review（Standards + Spec 双轴）

**现象**：`repaired` 只统计回填的 tool/result 条数。当后端只补上了缺失的 run 终态（没有 dangling 工具）时，`repaired === 0` 且入口消失 → 提示落到 `已恢复：无可修复项（会话事件已完整）`——**日志本就不完整、恢复确实修了它**，这句话是谎报。

**修复**：`hasUnterminatedRun(events)` 判定「最后一个 run 有头无尾」；recover 前后各算一次得到 `terminalRepaired`；文案收敛到纯函数 `recoverDoneMessage()`，各结局分别表述（回填 N 条 / 补齐终态 / 两者都有 / 后端没修完可重试 / 真无可修）。两个「没修完」的原因（缺终态 / 悬空工具调用）**分开**传入——`isRecoverableRun` 是 OR，压成一个布尔会在「终态已补、只剩悬空调用」时误报「仍缺 run 终态」。

**回归**：`runState.test.ts` 八条分支单测（含「repaired>0 但仍缺终态必须附可重试提示」「只剩悬空调用不得说仍缺终态」）+ `d-recover.spec.ts` 新增「只补终态」用例（断言不出现「无可修复项」/「已完整」）。

---

### OBS-004 `run/interrupted` 的 `step_id` 缺失：后端排查线索（交接手册 §D 的回执）【已确认 · 非前端 bug】

**发现时间**：2026-09-11

**排查方法**：扫两个 worktree 的真实 JSONL（**当日快照**：71 个会话；后续会话继续累计，比率是承重结论）。

**结果**：`run/interrupted` 共 **4 条**，其中 **2 条的 `step_id` 字段整个缺失**（不是显式 null）：

| session_id | run/started seq | run/interrupted seq | step_id |
| --- | --- | --- | --- |
| `c63ce4d3-3b26-40bb-8e8c-e3af8dd33035` | 2 | 3 | 缺失（`reason: process_restart`） |
| `2f2f3187-fccc-4704-b9a4-f259f82d153d` | 2 | 3 | 缺失（`reason: process_restart`） |
| `f181c5ce-7c84-43f9-b249-efa606293268` | 2 | 15 | 3 |
| `3b35b83d-dcae-476f-8343-9912e40e77d7` | — | 34 | 1 |

**语义**：这两条里 `run/started`（seq 2）之后**没有任何带步号的事件**就中断了（seq 3 就是 `run/interrupted`）——进程在该 run 的第一个步骤开始前死亡，检测器（`detect_unterminated_runs` → `recovery/scan._mark_interrupted`）无从沿用 step_id，于是信封不带该字段。

**前端处置**：`projectRunInterrupted` 把缺失强制为 `null`；横幅文案区分「首个步骤开始前中断」与「第 N 步中断」，不再渲染「第 ? 步」。e2e `d-recover.spec.ts` 两条锁死。

**后端待办**（交接手册 §D 请求的复现 id 即上表）：`detect_unterminated_runs` 的取值路径可对照这两条确认——是「沿用最后见到的 step_id」在无可用值时留空，还是另有分支。

---

### OBS-007 被中断的会话同时显示绿色「已完成」脉冲与「上次运行…中断」横幅【观察项 · 既有行为，未修（§8 范围外）】

**发现时间**：2026-09-11 独立 code-review（Spec 轴，非用户报告）

**现象**：`projectRunInterrupted`（`lib/projection.ts`）在写入 `run_interrupted` 之后调用 `finalizeRun(state, 'completed', …)`，于是 `run_status === 'completed'`；`deriveRunPulse` 只看 `run_status` → 渲染 `pulse-completed`（绿色 + `SquareCheckBig` + 文案「已完成」）。同时 `App.tsx` 的 `interrupt-banner` 因 `run_interrupted` 为真而渲染「上次运行在首个步骤开始前中断（原因：process_restart）」。**同一屏同时说「已完成」和「已中断」。**

**为什么当时这么写（可辩护的部分）**：冻结决策 69「interrupted ≠ error」——中断不是失败，所以不能走 `failed`。`finalizeRun` 只有 `completed | failed` 两档，作者取了「非失败」的那一档。

**为什么仍是问题**：`completed` 在 UI 上的语义是「跑完了」，与横幅文案直接冲突；用户看到绿色对勾会以为任务完成。

**未修的原因**：`projectRunInterrupted` 与 `PULSE_TABLE` 都不在本次交接手册 A/B/C/D 的范围内，且这是**改动前就存在**的行为（`git diff HEAD -- src/lib/projection.ts` 里本项目只新增了 `firstForkableTurnIndex`），按 `AGENTS.md` §8「Scope 外问题只报告，不顺手修」不在本次动手。

**建议修法**（留给集成决策）：给 `RunPulseDescriptor` 加第四态 `interrupted`（中性色 + 文案「已中断」），在 `deriveRunPulse` 里优先看 `conversation.run_interrupted`，或把 `finalizeRun` 的状态参数扩成三态。两条路都要同步 `runState.test.ts` 的 `deriveRunPulse` 用例与 `PULSE_TABLE`。

**验证**：`web/src/lib/runState.test.ts` 现有 `deriveRunPulse` 用例覆盖 completed/failed/cancelled/running/idle，不含 interrupted——即本组合无测试锁定。

---

## 已验证正常的交互（2026-09-10 真实点击 17 项 + 2026-09-11 新增第 18–20 项）

| # | 交互 | 结果 |
| --- | --- | --- |
| 1 | 会话列表点击选择会话 | ✓ 319 事件加载，header 显示会话 ID / 状态 / tok |
| 2 | T9 轮次标签 | ✓ 「第 1 轮」~「第 6 轮」per-turn 正确渲染（`cddea36`） |
| 3 | 折叠按钮 | ✓ 变「已折叠 · 0 个工具 · 1 轮 · 5.1s」，内容收起 |
| 4 | Timeline → Chat 反向联动 | ✓ 点 `user/message 什么是rag` 行，chat 滚到目标轮并进入视口 |
| 5 | Timeline 行 → 事件详情 | ✓ StepDetail 打开，显示 seq=199 的 USER/MESSAGE（seq/time/event_id） |
| 6 | 密度四档切换 | ✓ `data-density` 即时生效，localStorage `ahi.traceDensity` 持久 |
| 7 | 主题切换 | ✓ dark ↔ light，`data-theme` 属性正确翻转 |
| 8 | Inspector 收起/展开 | ✓ 收起后面板移除，展开恢复，aria-pressed 正确 |
| 9 | 模型选择器打开/选择 | ✓ dialog + cmdk 列表（默认链 + 5 个模型），选中后按钮文案更新 |
| 10 | **T7 会话级模型切换** | ✓ 已选会话中选模型 → `POST /model` 200 `{status:changed}`，本地状态用响应 `model_id` 更新 |
| 11 | 权限模式选择器 | ✓ 三档（只读/工作区写入/完全访问），描述文案齐全 |
| 12 | Agent Profile 选择器 | ✓ 三档（通用/编程/研究审查），选择生效 |
| 13 | Reasoning Effort 选择器 | ✓ 三档（轻量/标准/深度），选择生效 |
| 14 | 思考块展开 | ✓ aria-expanded 翻转，reasoning 全文展开 |
| 15 | 续聊发送 | ✓ `POST /messages` 200，请求体带 amend 档位（model/agent_profile/reasoning_effort）|
| 16 | run 失败的 UI 呈现 | ✓ header「失败」+ turn 渲染，无崩溃（内容见 OBS-001）|
| 17 | 分叉按钮 | ✓ 已修复 `8469a34`（from_seq = user/message seq，422 detail 可见）——e2e 回归锁 `b-fork.spec.ts` |
| 18 | 恢复会话按钮反馈 | ✓ 已修复——成功落 `done` 提示（在 `canRecover` 门外）；文案区分「回填 N 条 / 补齐 run 终态 / 后端没修完（附具体原因）可重试 / 真无可修」；e2e `d-recover.spec.ts` |
| 19 | 流式中上滚被顶回 | ✓ 已修复——复用 `followLatest` 原语 + 瞬时贴底 + `overflow-anchor: none`；e2e `j-scroll.spec.ts` 用真实 `page.mouse.wheel` 锁可观察契约（上滚→浮标出现→点浮标回底）；**竞态本身（delta 到达时是否拽回）无法自动化**，证据见下方真机埋点 |
| 20 | 工具输出面板内上滚 | ✓ 已修复 BUG-003（wheel 冒泡导致的误脱离）；`e2e/j-scroll.spec.ts` 覆盖对话容器侧，嵌套容器侧人工验证 |

### 2026-09-11 全量逐按钮巡检（真实浏览器 + 真实后端，density 详细档）

| # | 交互 | 结果 |
| --- | --- | --- |
| 21 | 密度四档 | ✓ 紧凑/均衡/详细/Raw 均生效，`data-density` + localStorage `ahi.traceDensity` 同步，`.density-btn.sel` 跟随 |
| 22 | 主题切换 | ✓ dark ↔ light；亮色下截图逐项检查，文字/边框/工具卡/Inspector 对比度正常（无 §15 token 漏覆盖） |
| 23 | Inspector 收起/展开 | ✓ `.step-detail` 常驻 DOM，收起时 `.app-regions.inspector-closed` 归零宽度，展开恢复 320px |
| 24 | Workspace 模式 Chat/Split/Preview | ✓ 三者互斥选中，`aria-pressed` 跟随 |
| 25 | 模型选择器 | ✓ 5 项（默认链 + 4 模型），短目录无搜索框，选中后 trigger 文案更新 |
| 26 | 权限模式选择器 | ✓ 3 档带描述，选中回填 trigger |
| 27 | Agent Profile 选择器 | ✓ 3 档，选中回填 trigger |
| 28 | Reasoning Effort 选择器 | ✓ 3 档，选中回填 trigger |
| 29 | Context Provider 选择器 | ✓ **正确地不渲染**——后端 `/api/context-providers` 返回空列表，零伪造 |
| 30 | 空态 preset chip | ✓ 点击填入 Composer 并启用发送 |
| 31 | 发送 / ⌘+Enter | ✓ 空内容禁用；有内容可发；真实 run 流式输出（caret + run pulse 思考中/执行工具） |
| 32 | 停止按钮 | ✓ 流式中出现 `.composer-stop`、控制行禁用；点击后 run pulse → **已取消**（中性通道，非红色失败），按钮复位 |
| 33 | 思考块展开 | ✓ `aria-expanded` false→true，正文 258 字符渲染 |
| 34 | 工具卡展开/折叠 | ✓ `.act-node` 本身为按钮，detailed 档 `aria-expanded` 跟随；折叠按钮「折叠 ↔ 已折叠 · N 个工具 · M 轮 · Xs」 |
| 35 | 复制回答 | ✓ `aria-label` 复制回答→已复制，**系统剪贴板实测拿到回答正文** |
| 36 | Timeline 行 → StepDetail | ✓ 点 `model/completed` 行 → Inspector 切到事件焦点（`返回 Timeline`），返回按钮复位到 Run Inspector |
| 37 | 工具卡 Inspect | ✓ Inspector 切到该工具（focusType=bash，Input/Output/Raw 分页） |
| 38 | 委派节点 | ✓ 展开 `委派 → research_review` + 结果摘要区；复制子会话 ID（剪贴板实测）；Inspect 子会话；**打开子会话** → 主窗口切到 child |
| 39 | Ctrl+K 命令面板 | ✓ 唤起/输入过滤/Enter 执行/Esc 关闭；执行 Toggle Theme、Copy Run ID 等；**发现并修复 BUG-004** |
| 40 | 分叉按钮 | ✓ 真实点击 → 后端建 child、主窗口切到 child、child 为空会话（与首轮 tooltip 承诺一致）；分叉错误可见 |
| 41 | 恢复会话 | ✓ 真实 dangling 会话（停止 run 留下）→ 点击 → **「已恢复：回填 1 条工具结果」**，入口与 hint 消失而成功提示保留（原症状已消除） |
| 42 | 中断横幅 | ✓ 真实会话 `c63ce4d3-…`（run/interrupted 无 step_id）显示「上次运行在首个步骤开始前中断（原因：process_restart）」 |
| 43 | 浮标 ↓ 最新 | ✓ 见第 19 项（真实 wheel 上滚 → 浮标 → 点回底） |
| 未及 | 审批卡（#37） | ⚠ 本 UI 不可达（硬编码 `auto_approve: true`）且无测试——见 OBS-006 |

**第 19 项的真机证据（2026-09-11，dev server + 真实后端）**：会话内提交 2500 行生成任务，流式中用真实 wheel 事件上滚并对 `scrollTop` 打点（临时埋点，验证后已移除）——

```
wheel 前: {following:true}
wheel:    {deltaY:-120, runActive:true}     → 同步脱离
随后 6×250ms 采样: dTop 全为 0，gap 7221→15502（内容在长），相邻 PIN 事件数 0（贴底已静默）
浮标:     streaming:true 期间恒为可见
点浮标后: gap→0、PIN 恢复（following:true）、浮标消失
流结束后: 5×350ms 采样均 pill:false / gap:0（settle 补底生效，无残留浮标）
```

**测试环境**：后端 `localhost:8000`（探测时 71 个真实会话）+ 前端 dev server `localhost:5173`，Chrome 经 CDP 驱动，点击 + 网络面板 + 埋点三重验证。
