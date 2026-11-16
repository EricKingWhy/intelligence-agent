# GUI Round-5 修复批次（PR #436 + #437，票 #420–#428）独立深度检查报告

- 检查者：独立深度检查者（非本批实现者）
- 检查日期：2026-09-30
- 被检对象：`git log 8db02e83..367378dd`（本批代码 = `20e272c3` + `ce08cc8c` + `497b5952`；集成 = merge `5728d94e`）
- 当前树：`2c2e7b48`（tree `8bd136c0af57`）——本批之后只进了 docs-only 提交
  （`git diff --stat 367378dd..HEAD`：AGENTS.md + 7 份 docs，无代码/测试）
- 底账与台账按「声明性输入」处理：以下每条结论都有我自己产出的证据（命令输出 / `file:line` / 复跑读数）。
  底账为 `R5_DEEP_CHECK_BRIEF_2026-09-30.md`，台账为 `docs/review_ledger.d/*.tsv`。

---

## 0. 总裁决（先给结论）

**逐票功能判定**：#422 / #423 / #424 / #427 / #428 达成；#421 功能达成（批准/拒绝/幂等路径
实测仍工作），但它的**票面验收承载面（自动化 e2e）已失效**；#420 的 AC2 机制成立，
**AC1 只覆盖了一半接流点、AC3 带一处未经裁定的语义收窄**。

**放行判定：不能算「干净放行」。**

- 代码层我没有找到会损坏用户数据或破坏不变量的缺陷；修复方向正确，
  新增测试经 5 组变异实验全部转红（有判别力）。
- 但本批把 `main` 的**一条必需门禁车道留在红色状态**：
  `npx playwright test` 全量 **432 passed / 28 failed**，其中 **14 条由本批直接造成**
  （批前基线同批 spec 是 15 failed，其中 14 条与本批无关、批前既有）。
- 而本批的门禁读数**完全没有提这条车道**：台账行与 commit message 只列
  pytest / vitest / tsc / oxlint（+ 一次人工真浏览器抽查），protocol 要求的
  `playwright` 与 `vite build` 两条都不在其中（`vite build` 我复跑是绿的）。
- 因此：**在把这两条 e2e spec 更新掉、并把 AC1 的 4 处接线补齐之前，
  任何「本批门禁全绿」的说法都不成立**。修法明确、工作量小，
  建议按 AGENTS.md §14.4 走「补一次审查 → PR」收口（main 受保护，见 §14.4）。

---

## 1. 检查方法与环境（可复现）

| 项 | 我的做法 |
| --- | --- |
| 声明对账 | `git show 20e272c3 --stat`（29 文件 +889/−121）、`git show ce08cc8c`（7 文件 +436/−0）逐项比对台账行；`git diff --stat 8db02e83 367378dd -- web/e2e/`（空） |
| 门禁复跑 | `gate0.py` / `check_review_coverage.py` / `ruff`（经 gate0）/ `oxlint` / `tsc -b` / `vite build` / `vitest run` / `pytest`（全量） / `playwright test --workers=2` |
| 解释器纪律 | 后端一律 `.venv/Scripts/python.exe`；前端 `./node_modules/.bin/tsc`、`cd web` 后跑 vitest |
| e2e 车道 | 本仓 5173 被实现者留的 vite 占着（原配置 `assertPortFree` 会整批判失败）⇒ 我用**临时同参配置**换端口（5273）复跑；跑完已删 |
| e2e 归因 | 用 `git archive 8db02e83`（**只读**，不建 worktree、不动本仓）把**批前 main** 展开到 `web/.r5baseline/`，在同一批 spec 上复跑取基线；跑完已删 |
| 变异实验 | 5 组：改坏被测逻辑 → 断言必须转红 → 回滚 → `git status` 验证树干净 |
| 真浏览器 | 未做人工联调（见 §5「未覆盖」）。替代证据 = headless Chromium 的 e2e 车道 + 失败行号定位 |

环境事实（影响读数解释，值得记账）：

- 检查期间**另一条线在 `D:\intelligence-agent-backend` 跑全量 pytest**
  （PID 27132/33584，12:39 起）→ 我的重车道读数是在并发负载下取得的；
  e2e 非审批族我另做了隔离复跑，结论不变。
- 本仓 **8001 端口的 uvicorn 自 07:04 起常驻**（PID 29732，`agent_harness.web.app:create_prod_app`），
  它持有 `.agent/workspace/.instance.lock` —— 这是后端全量 pytest 一条红的直接成因（见 §3.7）。
  8000 是用户的 Docker Desktop，全程未触碰。

---

## 2. Findings

### P1 — e2e（`playwright`）必需车道在 `main` 上是红的；本批贡献 14 条，且本批门禁读数里没有这条车道

**证据**

1. 协议要求前端门禁五条：`npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build`
   （`docs/SDD_WORKFLOW_PROTOCOL.md:174`，同义表在 `:131`）。
2. 本批台账行 `docs/review_ledger.d/420-ff64228d-20e272c3.tsv` 的门禁证据只列
   「pytest 全量 / vitest / tsc / oxlint / `git diff --check`」+「真浏览器 e2e（IAB）」
   —— 那是**人工抽查**，不是 `playwright` 车道；commit `20e272c3` 的 message 同。
3. 我的全量复跑（workers=2）：**432 passed / 28 failed（13.8m）**。按 spec 分族：

   | spec | 失败数 | 归因 |
   | --- | --- | --- |
   | `n-approval-card.spec.ts` | 12（6 用例 × 2 视口） | **本批** |
   | `x-permission-section.spec.ts` | 2 | **本批** |
   | `workspace-modes.spec.ts` | 5 | 批前既有 |
   | `r-project-groups.spec.ts` | 4 | 批前既有 |
   | `multiturn-queue.spec.ts` | 2 | 批前既有 |
   | `control-row.spec.ts` | 2 | 批前既有（成因已定位，见下） |
   | `o-wait-hint.spec.ts` | 1 | 批前既有 + 负载敏感 |

4. **归因实验**：把批前 main（`8db02e83`）展开后跑同一批非审批 spec（5 个文件）：
   **批前 15 failed / 97 passed**，当前树同五个文件 **16 failed / 96 passed**。
   两侧失败的是**同一批用例族**（`workspace-modes` 4 条 / `r-project-groups` 2 条 /
   `control-row` 1 条 / `o-wait-hint` 1 条，各 ×2 视口），计数 ±1 来自这几族里的负载敏感用例
   （`o-wait-hint:91`、`workspace-modes:130` 在不同运行里时红时绿，两棵树都能红）⇒
   这 14 条**不是本批造成的**，而是批前 main 上既有的红。
5. 本批那 14 条的根因：`web/e2e/` 在本批零改动，而
   - `.approval-title` 文案由「已批准 / 已拒绝」改成「决策已提交」（`web/src/components/ApprovalCard.tsx:159-161`）；
   - 首个**非失效**待决审批从时间线移进 Radix 模态（`web/src/components/Conversation.tsx:445-450`，
     内联位渲染 `null`；模态内容在 body 末尾的 portal 里）。
6. **失败行号定位**（单跑 `n-approval-card` 抓 `at …spec.ts:N`）：6 条里 **5 条只败在文案断言**
   （`:123` / `:138` / `:239` / `:254` / `:347`）——即**批准、拒绝、409 幂等、按钮消失这些功能路径仍然工作**；
   1 条（`:159`）败在 `cards.first()).toBeFocused()`：DOM 序变成「内联卡在前、portal 模态在后」，
   `cards.first()` 已不是第一张活审批。
7. 没有任何机器会报这条红：`.github/workflows/` 只有 `gate0.yml`，其 6 条机械车道不含
   playwright/vitest（`scripts/gate0.py:6` 自己写明「完整门禁一次是跑不完的」）。
   批前那 15 条长期红着就是实例。

**为什么是问题**

- `main` 现在这条必需车道是红的，而它是唯一自动化覆盖「审批键盘闭环 / 焦点 / 幂等 409 /
  composer 解锁」的面；#421 票面 AC 明写「真鼠标 e2e：点击批准与拒绝各一路」，
  承载它的自动化面已经失真。
- 「必跑车道没跑 + 没报」这个组合比红本身更危险：它让下一批也不敢相信门禁读数。

**建议修法**

1. 更新两条 spec：文案断言改「决策已提交」；焦点/点击的定位改成**模态作用域**
   （例如 `page.locator('.approval-modal-content .approval-card')`）；
   `x-permission-section:60` 的「点 Inspector 再跳审批」改为在审批出现前打开 Inspector，
   或改成断言模态常驻可见（模态本身就是「审批面」）。
2. 顺手清批前 14 条：`control-row.spec.ts:289` 的「12/17」是**陈旧期望**——
   `web/src/lib/agentProfileScope.test.ts:47` 已是「13/18」，
   `be73a2d0 test(#201): sync profile scope counts with fixture` 只同步了单测没同步 e2e（该提交在批前 main 上）。
3. 把 e2e 变成**可发现**的门禁（进 PR 检查清单或夜间工作流）；否则它永远只是"谁想起来谁跑"。

---

### P2 — #420 AC1 的活性回调只接了一半接流点，且整条 SSE 路径没接

**证据**

1. `markStreamFrame`（`web/src/hooks/useSession.ts:394-395`）是 `lastFrameAtRef` 的写入器之一；
   WS 侧的唯一喂点在新加的 `onLiveness`（`web/src/lib/wsStream.ts:222-225`，放在 `switch` 之前，
   **任何解析成功的服务帧都算**——这一点确认无误）；另一处是 `onEvent`
   （`useSession.ts:628`，只认**真事件**）。
2. **已接** 4 处：`:803`（重连）、`:835`（截断重建）、`:945`（submitTask）、`:1013`（resumeLiveStream）。
3. **未接** 4 处（`wsStreamResponse` 第三参缺省 ⇒ 回调为 `undefined`）：
   - `:1136` —— `sendFollowUp` 的**排队/steer 受理回执**分支（注释原文「消息已受理、**当前 run 仍在跑**」）；
   - `:1191` —— `sendFollowUp` 的 launched 分支；
   - `:1502` —— `resumePausedRun` 的 launched 分支；
   - `:1621` —— `flushQueue` 的 launched 分支。
4. 停摆判据：`useSession.ts:849-856`（阈值 `RECONNECT_STALL_MS = 10_000`，`web/src/lib/reconnect.ts:52`），
   由 10s 心跳（`:550`）+ `visibilitychange`（`:561`）触发；额度 `MAX_RECONNECT_ATTEMPTS = 3`（`reconnect.ts:32`）。
   这四处挂的流在**审批等待期没有事件**（服务端只有心跳）⇒ `lastFrameAtRef` 变陈旧 ⇒ 必然假停摆。
5. **SSE 路径同样没接**：WS 不可用时走 `fallbackToSse`（`wsStream.ts:150-182`），
   该分支只 `emitRaw(value)`，**从不调用 `onLiveness`**；而后端 SSE 心跳是注释帧
   （`: ping - <ts>`，2s，`src/agent_harness/web/app.py:935-948`），客户端 `parseFrame` 只取 `data:` 行
   （`web/src/lib/sse.ts:59`）⇒ 注释帧不成事件 ⇒ 该路径在审批等待期也没有活性信号。
6. 由此产生一处**文档与实现相反**：`app.py:946-947` 明确写
   「停摆检测（`RECONNECT_STALL_MS`）看的是真实帧，**不会被 keepalive 喂假进展**」，
   而本批在 WS 侧把心跳改成了活性证据（`wsStream.ts:223-225`）。两者未统一。
   同时 issue #420 的披露写着「SSE 降级路径…该路径无心跳可数」——事实上 SSE 有 2s keepalive，
   只是注释帧不构成事件：**这句披露的事实面不成立**。

**为什么是问题**

- 这四条正是「run 在跑、随时会停在审批上」的路径。排队一条消息后 run 继续 → 需要审批 →
  等待期静默 → 10s 后**无谓重连**（resubscribe + 可能亮出「连接中断」条）。
- SSE 路径更重：重连额度靠"新 seq"重置（`observeProgress`），等待期没有新 seq ⇒
  3 次耗尽后 give-up，`useSession.ts:780` 报「连接中断（connection stalled）：重试 3 次未成功」，
  模式态落 `viewing` —— 这正是 #420 要消灭的形态，只是换了通道。

**建议修法**

1. 四处补第三参 `markStreamFrame`（各一行）；
2. SSE 读循环里也喂活性（`reader.read()` 成功即算，或 `emitRaw` 处统一记账），
   并把 `app.py:946-947` 的口径按「心跳 = 链路活性」改写，避免两处文档相反；
3. 补一条针对性测试：**审批等待期不得发生停摆重连**（可用假时钟 + 无事件流）。

---

### P3 — `TopBar` 的 `awaitingApproval` 与投影既有判据不一致（把 stale 也算成"在等"）

**证据**：`web/src/components/TopBar.tsx:85`
`const awaitingApproval = (conversation?.pending_approvals.length ?? 0) > 0;`
而同一仓已有正统判据 `web/src/lib/projection.ts:1643-1651` 的
`awaitingApproval(approvals) = approvals.some(a => !a.stale)`（注释明说算进 stale 会「永久锁死」），
且 `web/src/App.tsx:1227` 已经用它锁 composer。`markPendingApprovalsStale`（`projection.ts:1663-1668`）
只把 `stale` 置真、**不移出数组** ⇒ 长度大于 0 不等于有事在等。

**可达场景**：会话里留着上一条 run 的孤儿审批（stale），用户发起新 run（脉冲=thinking），
空闲 > 30s ⇒ 顶栏写「正在等待你的审批决定」，而实际没有任何东西在等决策。

**修法**：TopBar 改吃 `awaitingApproval(conversation.pending_approvals)`（同名函数已有，直接 import）。

---

### P3 — `ApprovalCard` 可能同时显示「审批已失效」与「决策已提交，等待后端确认」

**证据**：`web/src/components/ApprovalCard.tsx:159-161` 的标题是三态互斥的（`invalid` > `submitted` > 需要审批），
但 `:223-225` 的 submitted 说明**没有** `!invalid` 门。可达路径：POST 成功（含 409）置 `submitted`，
其后该审批被判失效（stale 或 404-gone）而 `permission/resolved` 尚未到达。

**修法**：submitted 说明加 `!invalid` 门，或让 `invalid` 分支优先吞掉它。

---

### P3 — 模态没有逃生口（票面授权的形态，但 POST 持续失败时是死路）

**证据**：`web/src/components/ApprovalModal.tsx:42`（`open` 恒真、`onOpenChange` 空实现）、
`:47-49`（ESC / 外部点击 / InteractOutside 全 `preventDefault`），无任何 Close。
另一方面 `Conversation.tsx:443-446` 明确「失效卡绝不进模态」⇒ **APR-01 的关闭路径保留**，
brief §5 的那个疑问我核实为**否**（失效路径没有被锁死）。

**判定**：模态 + 背景 inert 是 issue #421 正文亲口要的形态
（「活跃审批卡迁移到 Radix Dialog…背景 inert（WAI-ARIA Dialog 模式）」），**不是越权**。
残余风险只有一条：决策 POST 若持续失败（网络异常，非 404/409），
用户被困在不可关的浮层里，且看不到正文/Inspector 来解释这次审批的上下文
（`x-permission-section` 想做的正是"看上下文"这件事，它现在被挡，见 P1）。

**修法**：要么在票面/ADR 里把"不可关"确认成接受的取舍，要么给一个「稍后决定」
（保持 run 阻塞、释放 UI）出口。这是产品取舍，不是代码缺陷。

---

### P3 — CLI 对**不存在的会话**给出误导性报错（#424 引入）

**证据**（我用 `.venv/Scripts/python.exe` 直连 `_resolve_fork_ordinal` 实测）：

```
missing-session -> ForkBoundaryError | --from-message 1 超出范围：父会话 'nope' 只有 0 个可作分叉锚点的用户消息（事件 seq: []）
empty           -> ForkBoundaryError | --from-message 1 超出范围：父会话 'empty' 只有 0 个可作分叉锚点的用户消息（事件 seq: []）
boundaries: [1, 4] | N=2 -> 4
N=3 -> --from-message 3 超出范围：父会话 'two' 只有 2 个可作分叉锚点的用户消息（事件 seq: [1, 4]）
N=0 -> --from-message 0 超出范围：父会话 'two' 只有 2 个可作分叉锚点的用户消息（事件 seq: [1, 4]）
```

`store.read_events` 对不存在的会话返回 `[]`（`src/agent_harness/session/store.py:261-263`），
而解析器在 `fork_session` **之前**跑（`src/agent_harness/cli.py:1119-1121`），
于是 `fork.py:191-194` 原有的那句「Session 'x' 不存在或事件日志为空」**不再可达**。
退出码不变（同为 `ForkBoundaryError`），只是把"会话不存在"说成了"它有 0 条消息"。

**修法**：解析器里先判 `read_events` 为空 → 报「会话不存在或事件日志为空」，再走序数范围检查。

---

### P3 — #420 AC3 的「本观看窗」锚定是正文之外的语义收窄，披露只到一半

**证据**：`web/src/lib/approvalEcho.ts` 用渲染期登记的 `seen` 集合把回显限制在
「本次观看窗内见过它挂起」；`Conversation.approvalEcho.test.tsx` 明确锁了
「首屏直接挂载已决会话 → 无回显」「换会话 → 不回显」。机制本身自洽：
`approval_id` 是 `uuid.uuid4().hex`（`src/agent_harness/tooling/approval_queue.py:55`）⇒ 无同 id 复用，
登记幂等（StrictMode 双渲染安全），消费的是投影 `approval_decisions`（不变量 #22 单一事实源）。
**但**：issue #420 的评论只声明了「呈现方式与正文有偏差（改为 submitted 态 + 由投影卸载）」，
**没有**声明「只回显本观看窗见过的审批」；台账行 `420-20e272c3-ce08cc8c.tsv` 记的是
「用户决议「#420回显需接投影 props」」，没有任何用户对"观看窗"这条语义的裁定痕迹。

**判定**：不算缺陷（它是更保守的选择），但是**审计上说不清授权来源的语义扩张**。
**修法**：在 issue #420 / 台账里补一句「只回显本观看窗内见过的审批，历史决策按原样留在时间线」，
或在有用户裁定时再改回"全量回显"。

---

### P3 — #426 两条 issue 评论对同一条链的验证口径不一致

**证据**（同一作者，相隔 4.5 小时）：

- 2026-09-29T22:54:18Z：「预算到顶触发暂停的完整链路由后端既有用例覆盖（**本分支未另做长跑 e2e**）」；
- 2026-09-30T03:22:30Z：「纯 UI 全链（设预算 → 多 turn 任务 → 暂停 → PausedPanel 恢复）**已真浏览器验证**」。

**判定**：一维交付（turns 一维 + 设计票 #435 + 票保持 open）本身**正确**，符合 §14.12 的
「部分交付不关单」；不一致的是"暂停那一段到底有没有真机验证"的表述。
**修法**：关票前二选一改到与事实一致。

---

### P3 — #422 参数化用例里 `max_cost_usd` 那一维不具判别力

**证据**：把新加的 `launch=false × budget.run` 检查临时关掉后复跑参数化用例，
`max_cost_usd` 那条**仍然 422**——它的拒绝来自既有 `validate_ceiling_enforceability`
（「当前 Provider 集成不自报归属成本」），与新检查无关。其余五维（turns / requests / tokens /
deadline / tool_limits）在关掉新检查后转绿 ⇒ 有判别力。

**修法**：`max_cost_usd` 那一维的期望改为断言"拒绝理由"（或换一个自报成本的 provider 夹具），
否则这条用例无法证明 #422 的检查存在。

---

## 3. 被证实的声明（我独立复现）

1. **范围与规模对账**：`20e272c3` = 29 文件 +889/−121、`ce08cc8c` = 7 文件 +436/−0，
   与台账行逐字相符；`#428` 的显示分支在声明范围内；CHANGELOG 的 `[Unreleased]` #423 条目确实存在
   （`CHANGELOG.md` 新增 8 行）；**未发现夹带**（无顺手重构、无无关格式化、无静默语义变更）。
2. **#423 判定链自洽**：`src/agent_harness/session/service.py:901-907`（创建路径条件）+
   `:1312+`（续聊路径 `interactive=True`）+ `approval.py` 三分支 `build_approval_callback` +
   `assembly.py:334-341` 的 deny 兜底用户向文案 + CHANGELOG；
   `create_and_launch` 的唯一调用方 (`web/app.py:1390-1403`) 确实传了显式旗标；
   全库已无 `not yet wired` 残留。
3. **#424 语义与边界**（见 P3 那节的实测输出）：序数解析正确，恰好 N 返回对应 seq，
   越界报错同时给序数范围与 seq 清单——设计意图（用户按序数提问、按 seq 对账）达成。
4. **#428**：显示分支与后端 `storage/artifact.py` 的 `truncated = char_truncated or len(filtered) > max_lines`
   语义一致（"行数没少但内容被截"与"行数真的少了"分开措辞）。
5. **#427 文案分支**：`web/src/lib/runState.ts:203-206` + `:180-187`；
   `awaitingApproval` 为假时行为与批前**逐字相同**（差分确认）。
6. **测试判别力**：5 组变异实验（序数解析 / service 创建条件 / WS 心跳活性 / echo seen 锚 / 422 判定）
   **全部转红**，回滚后工作树干净 ⇒ 新增测试不是"假覆盖"。
7. **门禁复跑读数**（我的，非手抄）：

   | 车道 | 读数 | 备注 |
   | --- | --- | --- |
   | `gate0.py` | **6/6 PASS**，25.39s | 我自己的读数落盘 `docs/gate/2c2e7b481828f4d638865fd2c2664257baa6ca1d.json`（tip `2c2e7b48`，tree `8bd136c0af57`） |
   | `check_review_coverage.py` | exit 0 | 「089524a~1..HEAD 每条 commit 均有归属」 |
   | `tsc -b` | exit 0 | |
   | `oxlint` | 41 warnings / 0 errors | 与「基线持平」相符 |
   | `vite build` | exit 0 | 4.83s（**本批从未报过这条**） |
   | `vitest run` | 1189 passed / 1190 | 唯一 failed = `StepDetail.window.test.tsx`（已知负载 flake，隔离复跑 6/6 过） |
   | `pytest`（全量） | **4838 passed**, 1 failed, 2 skipped | 唯一 failed 见下 |
   | `playwright test --workers=2` | 432 passed / **28 failed** | 见 P1 |

8. **后端唯一那条红的归因（台账声明证实）**：`tests/observability/test_flush_lifecycle.py::test_web_lifespan_flushes_on_shutdown`
   失败原因是 `InstanceLockError`，锁文件 `.agent/workspace/.instance.lock`，
   占用者 **pid=29732 / root=D:\intelligence-agent\.agent\workspace** —— 正是常驻 8001 的本项目 uvicorn。
   用官方逃生门复跑该文件：`ALLOW_SHARED_ROOT=1 … → 3 passed in 10.90s`。
   ⇒ 台账「归因 dev 后端占锁、非代码回归」**成立**（我没有杀任何进程，只用逃生门证明）。
9. **安全红线**：我对 5 份 GUI 证据 md 跑了凭证模式扫描（`sk-` / `api_key=` / `Bearer` / `PASSWORD=`）
   —— **零命中**；`.env` 本批零改动（`git diff --stat 8db02e83 367378dd -- .env` 空）。
10. **关票纪律**：#420/#421/#422/#423/#424/#427/#428 **CLOSED**，#426 **OPEN**（部分交付）——
    符合 §14.12。

## 4. 被推翻的声明

- **无整条被推翻**。最接近的两处：
  1. **「AC1 已修」的完整性**：实现声明本身只写了 `wsStream` 的实现（那部分成立），
     并未声称覆盖全部接流点 ⇒ 记为「**声明不完整**」（P2）。
  2. **issue #420 评论里「SSE 降级路径…该路径无心跳可数」**：与代码事实不符
     —— SSE 有 2s keepalive（`app.py:935-948`），只是注释帧不成事件。这是一处**被推翻的小事实**。

## 5. 我未覆盖 / 无法验证的部分（如实列出）

1. **人工真浏览器联调的读数**（approve/deny/300s fail-closed 全链、模态保持 36s+ 连接不断、
   后端 `seq8→seq9` 间隔 300.01s、`budget.run.max_agent_turns_total=50` 原样落库）：
   只有台账与 issue 评论，**我没有做真机 IAB 联调**，无法独立复现这几条读数。
   我的替代证据（headless Chromium 跑同一套 UI 代码）能证明**功能路径仍工作**（见 P1 第 6 条行号定位），
   但不能证明那几条真机读数。
2. **7 张 R4/R5 截图的内容**：只做了"二进制、无文本泄漏面"的判断，未逐张肉眼核对。
3. **批前 14 条 e2e 红的引入时间**：我证明了它们**在批前 main 上就红**，但没有 bisect 到是哪一笔
   （没有任何 CI/台账记录显示这条车道曾经绿过——所以也无法判断它是长期状态还是某次提交引入）。
4. **`web/.r5baseline` 与临时 playwright 配置**：属于我自己的检查工具，跑完已删；
   工作树只剩 3 个批前就存在的未跟踪文件 + 我这次 gate0 的读数
   `docs/gate/2c2e7b481828f4d638865fd2c2664257baa6ca1d.json`（未跟踪，留作证据，是否入库由用户定）。

## 6. 建议的收口顺序（按性价比）

1. **补 AC1 接线的 4 处 + SSE 活性**（4 行 + 1 处；配上"审批等待期不得重连"的测试）。
2. **更新两条 e2e spec**（文案 + 模态作用域定位 + 焦点断言），跑一次全量 `playwright test --workers=2`
   把本批那 14 条清零——这是 #421 票面 AC 的承载面。
3. **把 e2e 纳入可发现门禁**（PR 检查清单/夜间工作流），并顺手清批前 14 条
   （`control-row:289` 的 12/17 改 13/18 即可，其余三条 family 我已给出证据但未逐条定位根因）。
4. P3 清账：TopBar 判据、ApprovalCard submitted 提示门、CLI 缺会话报错、#426 评论口径、
   #422 `max_cost_usd` 期望。
5. 收口时按 §14.4：代码提交必须**补一次审查**写入台账，使 `check_review_coverage.py` 保持 exit 0；
   main 受保护 ⇒ 走「推分支 → PR → gate0 绿 → 合并」（两步各需单独批准）。

---

### 附：本次检查未做（明确排除）

- 未修改任何被检代码 / 测试 / 台账 / tracker；
- 未杀任何进程（含 8001 的 uvicorn、8000 的 Docker Desktop）；
- 未改全局 provider / 凭据配置；未打印任何 `.env` 值；
- 未对 `docs/SDD_TICKET_TRACKER.md`、`docs/SDD_WORKFLOW_PROTOCOL.md` 等受控文件落盘
  （issue 评论与台账记账由用户定）。
