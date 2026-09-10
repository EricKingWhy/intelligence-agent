# 前端集成交接提示词 —— 刷新一致性（BUG-005 / BUG-006）

> **分支**：`feat/frontend` @ `D:\intelligence-agent-frontend`
> **起始 commit**：`cf8f3a7`
> **本批 commit**：`138b056`（代码 + 文档 + 测试同批）
> **门禁**：tsc ✓ · vitest **487 passed**（28 文件）· oxlint **35 warnings / 0 errors**（基线持平）· playwright **96 passed** · vite build ✓
> **禁止推送远程**：本分支只做本地 commit，`git push` / merge 由集成 AI 执行（AGENTS.md §13.2 / §14.4）

---

## 1. 本批改了什么

用户要求：「检查页面刷新后会话内容还在不在——刷新后的会话必须和刷新前一致」。实测发现两处缺口，均已修复并用真实浏览器 + 真实后端取证。

| 缺陷 | 症状 | 根因 | 修复 |
| --- | --- | --- | --- |
| **BUG-005** 刷新丢失选中会话 | F5 后回到空态：8 轮 → 0 轮、正文 4102 字符 → 0；会话列表 67 条还在，只是没人记得用户选的是哪条 | `useSession` 的 `mode` 初始态恒为 `idle`，会话选择**没有任何持久化**（全仓只有 theme / density / apiToken 三处 localStorage） | 新增 `lib/sessionRestore.ts`：`ahi.selectedSession` 键 + `readStoredSessionId` / `writeStoredSessionId`（storage 不可用静默降级）。`mode` 改为**惰性初始化**；持久化挂在 `mode` 上（`idle` ⇒ 删键） |
| **BUG-006** 流式中刷新后停在假快照 | run 仍在服务端跑（ADR-0016 detached-run），刷新后的 UI 只显示一次历史快照，后续事件**再也进不来**，看起来像已完成 | `viewing` 分支只 `GET /events` 取一次就收工，没有任何「接回实时流」的通路 | 历史装载后若 `hasUnterminatedRun(events)` → `resumeLiveStream(sid, maxEventSeq(events))` → `GET /stream?after_seq=N`（后端 T4 #97 的重放+续流契约），不重不漏 |

### 文件清单

| 文件 | 变更 |
| --- | --- |
| `web/src/lib/sessionRestore.ts` | **新增**（无 React 依赖）：storage 读写 + `maxEventSeq` + `nextResumeAttempt` + `forgetResumeAttempt`（+ `ResumeAttempts` 类型） |
| `web/src/lib/sessionRestore.test.ts` | **新增** 12 例 |
| `web/src/lib/api.ts` | 新增 `NotFoundError`；`getSessionEvents` 遇 404 抛它（区别于真故障） |
| `web/src/lib/api.test.ts` | +3 例（404/500/200 三态归类） |
| `web/src/hooks/useSession.ts` | 主改动：惰性 `mode`、持久化 effect、`resumeLiveStream`、`attachLiveStream` 的 `opts.resume` + 零帧兜底、`selectSession` 的重新武装 |
| `web/e2e/k-refresh-restore.spec.ts` | **新增** 5 用例（× 2 视口 = 10 例） |
| `docs/FRONTEND_ISSUES_LOG.md` | BUG-005/006/007 + OBS-008/009 + 第二轮 66 行巡检表 + 审查处置 |
| `docs/SDD_TICKET_TRACKER.md` | 本批进度 |

---

## 2. 真机取证（这是本批最需要集成 AI 知道的部分）

**BUG-006 决定性取证**（真实后端 `127.0.0.1:8000` + 真实浏览器 CDP 驱动）：

1. 新会话提交一个 ~30s 任务（三次 `sleep`），5s 后确认 run **确在途**（脉冲「思考中 · 15s」，事件仅到 `run/started`）。
2. **此刻 F5**。刷新后 3s：选中行已恢复、用户消息在、**无重连横幅、无空态**；Inspector 已渲染事件 0–6。
3. **网络层铁证**：`GET /api/sessions/affd6084-…/stream?after_seq=2 [200]`。
   刷新时 `/events` 快照只到 **seq 2** → `maxEventSeq` = 2 → 以 `after_seq=2` 接流；**事件 3–6 是经这条流重放+续送达的**，不是历史快照。
4. **零交互前进**：此后不碰页面等 16s，事件 **3 → 54** 条，末条 `53 run/completed`，脉冲「已完成 · 15,306 tok」。静态快照不可能自增。
5. **与后端真值对账**：服务端 54 条 / seq 0–53 / **重复 0、空洞 0**；UI 显示 54 条 / 末条 53 → 「无缝无重复」在 UI 侧成立。

**BUG-005 取证**：刷新前后逐项对照——选中行、`.turn` 轮次（8）、正文指纹 `-271347586` / 4102 字符、「已完成 · 4,339 tok」脉冲、右栏 Inspector 全部一致；且是**零点击**（不点任何东西）。

**回归锁**（`e2e/k-refresh-restore.spec.ts`，10 例全绿）：
- 刷新后零点击恢复选中 + 内容（含 `page.reload()` 后复验）；
- **写入路径**：从空态出发、真实点击会话行 → 断言键被写下 → **不重新播种**刷新 → 仍恢复。这条是第二轮审查补的：其余用例用 `addInitScript` 播种，而 `addInitScript` 每次导航（含 reload）都会重跑，所以只能证明**读**路径——删掉持久化 effect 它们依然全绿。补测后做过**变异验证**：临时停用写入 effect → 该用例在两个视口都变红（`Expected: "e2e-session-0001" / Received: null`），已还原；
- 接流游标恰为 `after_seq=4`，且**刷新后**才产生的标记文本由接流送达（首屏流给不同标记，只有真正接流才能通过断言）；
- 零帧空流（run 已在刷新窗口内收口）→ 静默停在历史 + **只发一次请求**（死循环断点）；
- 陈旧 id 404 → 清键 + 静默回空态。

---

## 3. 需要集成 AI 注意的行为变化

1. **新增 localStorage 键 `ahi.selectedSession`**。刷新后 UI 会恢复上次选中的会话（含分叉 child、委派 child、`打开子会话` 的目标）。显式点「新建会话」会删键，不会复活旧会话。
2. **刷新后可能自动发起一条 `GET /stream?after_seq=N`**（仅当该会话最后一个 run 无终态）。对已收口的会话不会发（或发一条立即 200+空 body 的，静默回落 viewing，不报错、不弹重连横幅）。
3. **`getSessionEvents` 的 404 现在是 `NotFoundError`**（新增导出），与 500 等真故障区分——UI 对前者静默回空态，对后者照旧显示错误横幅。`useChildConversation.ts` 把它当普通 `Error` 读 `.message`，仅文案更友好，无回归。
4. 无 CSS / 主题改动（§15 不涉及）；无新依赖。

---

## 4. 未修 / 遗留（明确披露，勿误判为完成）

| 项 | 级别 | 说明 |
| --- | --- | --- |
| **BUG-007** 命令面板 label 全英文 | P2 | 面板 50 条命令的 `label`/`hint` 全英文（`Toggle Theme` / `Copy Run ID`…），中文查询（「主题」「复制」）**零命中**；过滤逻辑本身正确（fuzzy 子序列，已实测）。**只登记未改**——文案语言属产品决定，且超出 BUG-005/006 范围（§8）。建议改 `lib/commands.ts` 一处，`i-keyboard.spec.ts` 有回归锁 |
| 零帧回落全静默 | P3 | 若后端某天在 run 活着时返回 200+空 body（契约破坏），用户会盯着半截会话且无信号。按**已实测契约**（空闲会话 → 立即 200+空 body；活着的 run 不会零帧 EOF）这不构成缺陷；加延迟复查会在回调里引入新重入面，收益不抵复杂度。风险已登记 |
| OBS-008 `glm-5.3-flash` model/failed | 后端 | 工具成功后 `model/failed: "model call failed: RuntimeError"` → `run/failed`。**非前端**，与既有 OBS-001 同族（同一模型）。建议后端把原始异常落进日志/JSONL |
| OBS-009 bash 工具 10s 上限 | 后端 | `sleep 10` 贴边越界 → `TIMEOUT`，且 `retryable:false`。前端渲染忠实（工具失败 ≠ run 失败，模型自行改用 `sleep 1` 后收口成功）。建议后端确认超时上限与 `retryable` 语义 |
| OBS-006 审批卡不可达 | 覆盖缺口 | 硬编码 `auto_approve: true`，UI 不可达且无测试（预存在，上批已登记） |
| `test-results/.last-run.json` | 卫生 | 工作区根目录的 Playwright 残留（**Sep 9，非本批产生**），未纳入提交。`web/.gitignore` 已忽略 `web/test-results`，根目录这份是历史遗留 |

---

## 5. 代码审查

第一轮（两轴：Standards + Spec/不变量）**6 findings，无 P0 / 无 P1**（3×P2 + 3×P3）：

| # | 级别 | 处置 |
| --- | --- | --- |
| 1 | P2 | **已修**——resume-404 分支的 `writeStoredSessionId(null)` 是死写（后面 `setMode(viewing)` 会让持久化 effect 把 id 写回去）。删除，并把「真已删除」交给紧随其后的历史重跑（404 → `NotFoundError` → 清键 + 回 idle，那次落得住） |
| 2 | P2 | **已修**——`resumeAttemptedRef: Set<string>` 只按 sid 记账，会连**正当的再次恢复**一起禁掉（A 接流成功 → 续聊 → 切 B 再切回 A 且 run 在跑 → 被拒 → 冻结）。改为按 **(sid, 游标)** 记账的 `nextResumeAttempt` 纯函数：死循环断点仍在（零帧不带来新事件 → 游标不变 → 拦下），有新事件时游标变大 → 允许再接 |
| 3 | P2 | **已修**——新增 8 例 e2e + 5 例 `nextResumeAttempt` 单测 + 3 例 `NotFoundError` 单测 |
| 4 | P3 | **接受的取舍**（零帧静默），理由见 §4 |
| 5 | P3 | **不改 + 补注释**（`scheduleReconnect` 404 同一 tick 内自愈，单独清是死写） |
| 6 | P3 | **已修**（多余空行） |

审查确认正确的部分（覆盖凭据）：死循环断点充分、StrictMode 双调用已被 `cancelled` 守卫挡住、游标数学不重不漏、`framesSeen` 计数位置正确、`terminalSeenRef` 每流重置、`streamGenRef` 代际守卫两路复检、不变量 #22 未破坏（复用同一 `projectHistory` 与唯一 `ConversationState`）。

第二轮审查结论：见本文件 §6（本轮追加）。

---

## 6. 第二轮审查结论

第二轮（对修复后的 diff 复审）**6 项一审 findings 全部 RESOLVED**（其中 #3 从 PARTIALLY 补齐为 RESOLVED，见下）；**新发现 4 项（1×P2 + 3×P3），无 P0/P1**。

| 新 # | 级别 | 内容 | 处置 |
| --- | --- | --- | --- |
| A | P2 | **BUG-005 的写入路径零自动化覆盖**——所有 e2e 都用 `addInitScript` 播种，而它在每次导航（含 reload）都重跑，于是只能证明读路径；删掉持久化 effect 全套仍绿 | **已修**：新增「真实点击 → 断言写键 → 不重新播种刷新」用例，并做**变异验证**确认它真的会红（见 §2） |
| B | P3 | `useSession.ts:288` 注释声称 ref 在 render 期赋值，与实现（effect 内）矛盾，会诱导后人改回去 | **已修**：改为「在 effect 里镜像」并注明「曾据此改过一次，别改回去」 |
| C | P3 | 失败尝试在 `await` **之前**就记账，会吃掉该游标的一次自动重试 | **不改 + 补注释说明理由**：这正是刻意的——失败路径同样 `setMode(live)`→`setMode(viewing)`，mode 每变一次历史 effect 就重跑，若失败不记账会变成**无上限重试**（每轮两条请求）。代价是失败后不自动重试（错误横幅已告知），用户重点一次该会话行即经 `selectSession → forgetResumeAttempt` 重新武装 |
| D | P3 | 本交接文档在定稿前已过期（测试数 9 vs 12、缺 `forgetResumeAttempt`） | **已修**：本文件即修正后版本 |

**第二轮额外确认（作为「无 P0/P1」的依据）**：`NotFoundError` 三个调用点①历史 effect 已处理②`doTruncatedRebuild` 走重连自愈③`useChildConversation` 仅读 `.message`（文案更友好，无回归）；`tsconfig` 目标 `es2023` 故 `class extends Error` 原型链完好，`toBeInstanceOf` 可靠；`maxEventSeq` 返回 `-1` 不会与 `hasUnterminatedRun` 同时成立（`run/started` 必带数字 seq），首帧不会被误判为 gap；`resumeAttemptedRef` 只按「本页会话内选中过的不同会话数」增长，刷新即清，非泄漏；§8 Scope Lock 与 §15 CSS 规则均未被触碰。

**第二轮后的门禁**：tsc ✓ · vitest **487 passed**（+3 例 `forgetResumeAttempt`）· oxlint **35w 0e** · playwright **96 passed**（+2 例写入路径）· vite build ✓。

---

## 7. 建议的合并顺序与验证

`feat/frontend` 落在 `web/**` 与 `docs/**`，与后端 `src/**` / `tests/**` 无交集。合并前按 §14.10 走：

```bash
git -C D:/intelligence-agent fetch origin
git -C D:/intelligence-agent diff --stat main...feat/frontend
git -C D:/intelligence-agent merge feat/frontend      # 需用户批准
```

合并后建议在 `D:\intelligence-agent` 起真实前后端，手工复验两条：

1. 选中一个真实会话 → **F5** → 会话与内容应完全一致（零点击）。
2. 提交一个 ≥20s 的任务 → 流式中 **F5** → 应看到「思考中/执行工具」继续前进，直到终态；**不应**出现「连接中断，正在重连…」或停在半截。

`docs/PHASE_STATUS.md` 按本分支协议未改（前端进度记 `docs/SDD_TICKET_TRACKER.md`），merge 后由集成 AI 追加一条。
