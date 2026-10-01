# R5 修复批次合并后独立深度复核报告（2026-09-30）

> **被检对象**：`D:\intelligence-agent`（main `5b6af76a`）刚合并的「R5 深检修复批次」——
> PR #453（报告入库）、PR #455（7 个修复 commit）、PR #457（记账）。
> **被检方自述**：`R5_FIX_BATCH_BRIEF_2026-09-30.md`（untracked；本报告逐条对账但不采信其结论）。
> **本报告未入库**（untracked，等用户处置）。
> **纪律**：全程默认只读；仅有的两处写操作是两个**突变实验**（随后 `git checkout` 还原，
> blob 哈希逐字节校验一致）与我自己运行的 gate0 落盘读数；临时 playwright 配置与 `tmp` 日志已删。
> 未打印任何 `.env` 值；未触碰端口 8000 / 全局 `model-providers.json`。

---

## 0. 方法、范围与独立性限制（先说清可信度）

**逐票两轴**：① 代码正确性（自读 diff 与实现，不信 commit message）；② 票面一致性
（7 张票的票面与「修复依据」对照实际交付）。

**本报告全部门禁读数来自本深查自己复跑的命令**（见 §4 逐条命令与读数），不手抄被检方任何数字。

**独立性限制（如实声明）**：

1. 本深查由**同一模型会话**在其修复工作之后执行——属于「自查」，不是不同主体的独立审查。
   缓解手段是：对抗性检查清单（原计划由只读子代理执行）+ 全部断言**实测复跑**（含两个突变实验），
   凡是能机械判定的结论都不依赖自述。
2. 原计划的两个对抗性独立子代理（agent 9444a616 / agent 60fe23bb）**均因基础设施失败未交付**
   （`Upstream stream disconnected` / `Model request failed`）。失败后未再派发第三次，改由主 agent
   按同一清单直接完成 C1–C6 复核。**这削弱了「第二双眼睛」的独立性**，请在采信本报告时计入此限。

**过程事实澄清**：被检方声明的「平台并发上限 2」「#445 实现 agent 被截断后由主会话补证」在本深查中
得到印证（本会话派发 subagent 同样受限）；#445 的补证方式（单文件 stash 红→绿）经本深查复核成立，
且本次复跑其全部 9 用例绿（§4）。

---

## 1. 发现清单（每条：严重级 / 证据 / 为什么是问题 / 建议修法 / 归属）

### F1 — playwright 全量首轮复跑 459/1：`r-project-groups` AC4 负载窗口 flake【P3，批前既有 flake 家族成员】

- **证据（本深查复跑三组读数）**：
  - 首轮全量（运行期间我并发跑了 3 个 pytest，构成人为负载）：`459 passed / 1 failed`，唯一失败 = `e2e/r-project-groups.spec.ts:141:1 › AC4`；
  - 隔离复跑该 spec：`22 passed (1.2m)` 全绿；
  - 干净全量（机器无并发负载）：**`460 passed / 0 failed (11.6m)`, `PW_EXIT=0`**。
- **为什么是问题**：AC4 恰是 #442 的加固对象（`:144` 断言改为 `expect.poll`），加固后在全量负载下
  仍红了一次——说明 #442 的 poll 化**没有完全消除**该用例的负载间歇。该用例自身注释记载了历史
  （「本用例曾在全量并行下偶发 30s 超时，页面快照却显示改名已经成功」），红点与 #442 改动无直接因果
  （失败发生在 poll 断言之外的重命名/键盘段），故**不构成批次缺陷**，但意味着 #442 之后仍有残余 flake 面。
- **建议**：不修（属既有 flake 家族）。若后续复现，优先把 AC4 的 `page.keyboard.press('Enter')` 后
  `toBeVisible` 段改为与 `:144` 同机制的 `expect.poll` 断言，或对该用例做负载隔离标记。
- **归属**：批前既有（AC4 用例与 flake 史均早于本批；本批只改了其中一条断言读法）。

### F2 — vitest 全量复跑 1201/1：`StepDetail.window.test.tsx` 已知 flake【P3，批前既有】

- **证据（本深查复跑）**：全量 `1201 passed / 1 failed (1202)`，唯一失败 = `src/components/StepDetail.window.test.tsx:132`
  「DIFFS / ARTIFACTS：默认先裁、点出口才涨，最终行数 = N」（5s 默认超时）；隔离复跑该文件 **`6 passed (7.29s)`** 全绿。
- **为什么是问题**：非缺陷，是与本批零文件交集的负载敏感用例；被检方简报 §5.8 已如实登记，
  本深查**确认**其归因（隔离必绿、失败信息为超时而非断言值错）。
- **建议**：不修（既有 flake）。
- **归属**：批前既有。

### F3 — `docs/gate/` 残留 7 份未跟踪读数；最终 main 树读数存在于工作树但未入库【P3，本批引入（书证卫生）】

- **证据**：
  - `git status` 未跟踪 7 份 `docs/gate/*.json`（本批 6 + 批前 1：`98f8241d`）；
  - 其中 **`b3b8f2e2….json` 的 tree = `baa2064b59c6…` = 最终 main（`5b6af76a`）的树**，6/6 PASS——
    即当前 main 的机械门禁读数只存在于工作树，**仓库内不可复核**；
  - 已入库的两份参照读数均正确可查：`docs/gate/07166334….json`（6/6 PASS，17.55s，tree 与合并后代码树相同）
    与 `docs/gate/2c2e7b48….json`（6/6 PASS）；
  - 三份 FAIL 存证均为忠实记录、且与台账时序吻合：`17df2b99` 5/6（coverage FAIL → 后由台账行
    `ci-02bd0b12-17df2b99.tsv` 补归因）、`db41a766` 5/6（guards FAIL → 后由 `02bd0b12` 补 verification.map 行）、
    `98f8241d` 5/6（批前，coverage FAIL 存证）。
- **为什么是问题**：§14.10 要求门禁读数「来自机器落盘」且可引用。本批正式引用（`07166334`）没问题，
  但终态读数的缺失属于书证完整性降级——未来审计无法从仓库直接取到「最终 main 树的 6/6」这一读数。
- **建议**：把 `b3b8f2e2….json` 随下一次 docs 提交入库（或明确删除未跟踪残留并接受「以引用读数为准」）；
  FAIL 存证三份建议保留（它们是修复链的机械证据）。属可选的卫生动作，不阻塞。
- **归属**：本批引入（残留产生于本批的运行节奏；`98f8241d` 为批前先例）。

### F4 — #441 后「Inspector 待审批行 → 内联卡脉冲」反向联动零自动化覆盖【P3，本批引入（覆盖缺口，已知并自述）】

- **证据**：`grep -rn "stream-jump-pulse" web/src web/e2e` 现只命中实现与 CSS
  （`Conversation.tsx:147-150`、`app.css:2401`），**无任何测试引用**；该断言的引入者是 `3a579242`（#184），
  被 `df2a0e62`（#441）改为模态可见断言后，这条路径失去唯一自动化覆盖。
- **为什么是问题**：不是缺陷（`Conversation.tsx` 的脉冲实现未改），是覆盖缺口——#421 起模态候选使
  「点行→脉冲」成为设计内 no-op，e2e 无法再走原路径。被检方已自述（简报 §5.2）、关票评论有记录，
  但**未开票**。
- **建议**：开一张 P3 覆盖票：为「无模态候选（stale/echo 内联卡）时的行点击→脉冲」补组件级测试
  （jsdom 绕开 inert 限制），或在 e2e 中构造仅内联卡在场的窗口。
- **归属**：本批引入（覆盖被本批改写移除；缺口成因是 #421 的设计，登记责任在本批）。

### F5 — `invalid + submitted` 并存时「决策无法再提交」措辞对已提交用户不精确【P3，批前状态残留】

- **证据**：`ApprovalCard.invalidSubmitted.test.tsx` 断言了「失效卡不再说『决策已提交』」，
  但该卡在 invalid 态仍渲染「无法再提交」一类说明（台账行 7 的遗留观察：「invalid+submitted 并存时
  『决策无法再提交』措辞对已提交用户事后不精确」）；本深查读组件实现确认该文案分支存在于 invalid 态。
- **为什么是问题**：显示级不精确（用户明明已提交过）。#444 修的是「同卡自相矛盾」，这条是修复后暴露的
  残留边界，被检方已记录且**有意未动**（Scope Lock）。
- **建议**：后续批把已提交分支的 invalid 文案改成「决策已提交，该审批已失效」类表述；纯文案改动，1 行。
- **归属**：批前状态残留（组合可达性先于本批；本批使矛盾消失，瑕疵仍在）。

### F6 — 两个已知 flake 登记未在本深查复现；T12l 未加固属 Scope Lock【信息，不评级】

- **证据**：本深查三次全量（pytest 1 次、playwright 2 次、vitest 1 次）均未触发
  `stream-fallback` / `T12p` / `T12r` / `tests/model/test_stall_watchdog.py`；`multiturn-queue` diff 确认
  只动 T12m/T12n，T12l 未动（简报 §5.6 属实）。
- **为什么记录**：被检方把这些列为「已知 flake，勿误判」。本深查**无法证伪**其存在（未复现≠不存在），
  但确认本批改动**没有触碰**这些用例（diff 文件清单可查），归因链成立。
- **归属**：批前既有。

### F7 — 独立性受限（见 §0）【信息】

- 两个对抗性只读子代理均基础设施失败；本报告由修复方同模型完成「自查 + 对抗清单 + 实测复跑」。
  这是本报告最重要的已知偏置。

**未发现**：P0 / P1 / P2 级问题；凭证面；§7 架构不变量破坏；Scope 外实现改动（7 个 commit 的文件清单
与票面逐一吻合，见 §2 表）；`.env` / 全局配置 / 端口越界动作。

---

## 2. 被检方关键声明 × 三态结论（确认 / 反驳 / 无法验证）

| # | 声明（简报/commit message 原文要点） | 结论 | 本深查证据 |
| --- | --- | --- | --- |
| 1 | #440：`wsStreamResponse` 全仓 **8/8** 调用点带第三参 | **确认** | `grep -n wsStreamResponse( web/src/hooks/useSession.ts` → 恰 8 处（:806/:838/:948/:1016/:1139/:1194/:1505/:1624），全部 `markStreamFrame` 第三参；diff 显示 :1139/:1194/:1505/:1624 为本批补齐，其余 4 处本就有 |
| 2 | #440：SSE 降级读循环「读到字节才喂活性，done 不算」 | **确认** | `wsStream.ts:176-185`：`if (done) break;` 在前、`if (value) { onLiveness?.(); emitRaw(value); }` 在后；单测锁 1→2→close 后恒 2（`wsStream.test.ts` 新增用例） |
| 3 | #440：`app.py` **仅注释** | **确认** | `git show --stat e2fb7cc5` app.py 8 行；diff 全为 `#:` 注释块；`SSE_PING_INTERVAL_SECONDS=2` 经 `_sse_response` → `EventSourceResponse(ping=...)` 接线未动（app.py:954/971） |
| 4 | #441：防护断言零削弱（POST 次数/请求体/无第二请求/500/409/composer 解锁原样） | **确认** | n-approval-card 全 diff 逐条核对：仅「已批准/已拒绝」→「决策已提交」改标 + 定位改模态作用域；`POST count=1`、`NO_SECOND_REQUEST_WAIT_MS`、500/409 路径、actions=0 均保留 |
| 5 | #441：AC5 改写导致脉冲反向联动覆盖缺口（自述） | **确认** | 全仓 `stream-jump-pulse` 无测试引用（见 F4） |
| 6 | #442：**实现零改动**（四族全为测试侧缺陷） | **确认** | `git show --stat c690a7eb`：仅 5 个 spec 共 +60/−28；无 `src/` / `web/src/` 文件 |
| 7 | #442：三处保留裸读「缺省单 Chat 态无竞逐」（简报点名的薄弱点） | **确认** | `workspace-modes.spec.ts:67`（capabilities=[] 全空态）、`:108`（单 Chat 缺省态）、`:152`（404 降级态）——三处期望值均为「fetch 落地前后同值」，逐点注释属实 |
| 8 | #446：二选一断言**不掩盖真回归** | **确认（实测升级）** | 单突变（杀互斥检查）：**6 红**（5 互斥维 + `loud` 用例），max_cost_usd 走第二护栏绿——与自述逐字吻合；**双突变**（互斥 + enforceability 同杀）：**2 红**（loud + max_cost_usd）⇒ 全删必被捕获 |
| 9 | #445：`_resolve_fork_ordinal` 先分「不存在/空日志」再判越界；措辞与 `fork_session` 同源 | **确认** | `cli.py:1076-1078` 读 `read_events` 后空即 `ForkBoundaryError("…不存在或事件日志为空")`；两条新测试在 pre-fix 实现下必红（pre-fix 报的是「--from-message 超出范围…0 个锚点」措辞，match 不上） |
| 10 | pytest 9 条全量红 = `ALLOW_SHARED_ROOT` 环境机制，非回归 | **确认（双向复跑）** | 带 env：**9 failed**（45.45s）；不带 env：**9 passed**（27.54s）；全量读数自洽（见 §4.7） |
| 11 | playwright 全量「首次全绿 460/0」 | **确认（干净复跑）** | 本深查干净全量 **460 passed / 0 failed (11.6m), PW_EXIT=0**；负载轮 459/1 见 F1 |
| 12 | vitest 全量 1201/1202，唯一败 = 已知 StepDetail flake | **确认** | 复跑同数（1201/1）；隔离 6/6 绿（见 F2） |
| 13 | 全量 pytest（修复树）4842 passed / 2 skipped | **确认（换算自洽）** | 本深查终态树复跑：**4851 passed / 2 skipped / 51 deselected / EXIT 0**（833.29s）；4851 − 4842 = 9 = 环境变量敏感集，恰为声明中「在 env 下失败」的那 9 条 |
| 14 | 覆盖闸门 exit 0；Gate-0 6/6 | **确认** | 自跑 `check_review_coverage.py` → exit 0（`089524a~1..HEAD` 全部归属）；自跑裸全量 `gate0.py` → **6/6 PASS（18.3s）**，读数落盘 `docs/gate/5b6af76a….json` |
| 15 | #456 判定：「重连挂新流不重置停摆基准——偏紧但合理、非功能缺陷」 | **无法验证（静态一致）** | 复核 `reconnect.ts`（`RECONNECT_STALL_MS=10_000`、`MAX_RECONNECT_ATTEMPTS=3`）：逻辑上确为「持续无帧时以 10s 节奏耗额度」的偏紧行为而非错误；未做行为级实测，维持「合理、可后续批」的判断 |
| 16 | #442 T12l 未加固属 Scope Lock | **确认** | `multiturn-queue` diff 只含 T12m/T12n |
| 17 | 并发上限 2；#445 agent 截断后主会话补证（stash 红→绿） | **确认** | 本会话派发同样受限；`test_cli_fork.py` 现 9 用例复跑全绿（38 passed 含之） |
| 18 | 台账 7 行 + 2 笔合并审计行 | **确认** | `docs/review_ledger.d/r5-deepcheck-fixes-e31cb654-c690a7eb.tsv` 7 行逐一核对（含 #445 截断补证记录、#446 偏离记录）；`ci-02bd0b12-17df2b99.tsv` / `ci-93f591f5-7ef3e761.tsv` 内容与合并事实一致 |
| 19 | 新测试「先红后绿」判别力（TDD 真证） | **确认（逐文件分析）** | 见 §3 |
| 20 | 7 个 commit 文件清单与票面对应 | **确认** | `git show --stat` 逐 commit 核对（#445: cli.py+tests；#446: 1 测试文件；#441: 2 spec；#443: TopBar+测试；#444: ApprovalCard+测试；#440: app.py+useSession+wsStream+tests；#442: 5 spec） |

---

## 3. 新测试判别力逐文件核验（「修复被回退必红吗」）

| 测试文件 | 判别机制 | 判读 |
| --- | --- | --- |
| `useSession.liveness.test.tsx` | mock `wsStreamResponse` 捕获三参；对照组（无心跳）必须 give-up（calls=4）、绿组心跳不断供必须 calls=1；断言 `onLiveness` 是 function（pre-fix 该位置不传 ⇒ `undefined` ⇒ 必红） | **真判别**（对照组证明夹具对停摆敏感，非 vacuous） |
| `useSession.liveness.sse.test.tsx` | **不 mock wsStream**：真链条（FakeWebSocket 零帧死 → 降级 fetch → 可投喂 SSE 流）；对照组「字节断供也必判停」（calls=4）守边界 | **真判别**（端到端，pre-fix 假停摆 → 70s 后 give-up） |
| `wsStream.test.ts` 新用例 | liveness 计数 1→2→`close()` 后恒 2（done 不刷） | **真判别** |
| `TopBar.approvalWaitHint.test.tsx` | 真投影造 stale/live 两态 + 假计时器过 30s 阈值；stale 断言**不出现**审批主语**且**出现「仍在等待模型」；live 断言反之（防修过头） | **真判别**（pre-fix `length>0` ⇒ stale 会说审批主语 ⇒ 必红） |
| `ApprovalCard.invalidSubmitted.test.tsx` | 真驱动：mock `postApproval` 成功 → 点击 → 前置断言咬住 submitted 态 → 同根重渲染下发 invalid | **真判别**（pre-fix 两文案同卡并存 ⇒ 末条断言必红） |
| `tests/test_cli_fork.py` +2 | 不存在会话 / 空 events.jsonl 两分支均 `match="不存在或事件日志为空"` | **真判别**（pre-fix 两分支都落「序数越界」措辞） |
| `test_budget_local_fuse_api.py` 改造 | 五维钉互斥 `detail`；max_cost_usd 二选一 | **真判别**（§4.8 双突变实测 6 红 / 2 红） |

---

## 4. 门禁读数（全部来自本深查复跑的命令；树 = `5b6af76a` / tree `baa2064b59c6`，另有注明者除外）

1. **pytest 受影响作用域**：`tests/test_cli_fork.py` + `tests/web/test_budget_local_fuse_api.py`
   → **38 passed，41.27s**（9 + 29）。
2. **9 节点双向证伪**（`test_clean_slate_cutover.py` ×5 + `test_instance_lock.py` ×4）：
   带 `ALLOW_SHARED_ROOT=1` → **9 failed，45.45s**；不带 → **9 passed，27.54s**。
3. **playwright 全量**（临时配置 port 5286，`npx vite --strictPort --port 5286`）：
   干净轮 **460 passed / 0 failed，11.6m，PW_EXIT=0**；负载轮 459/1（F1）；隔离
   `r-project-groups.spec.ts` **22 passed，1.2m**。
4. **vitest 全量**：**1201 passed / 1 failed (1202)，38.65s**；隔离 StepDetail 文件 **6 passed，7.29s**。
5. **tsc**：`./node_modules/.bin/tsc -b` → **exit 0**。
6. **build**：`npm run build` → **exit 0**（1.97s；chunk-size 警告为既有）。
7. **pytest 全量**：`PYTHONPATH= .venv/Scripts/python.exe -m pytest tests/ -q --no-header -p no:cacheprovider`
   → **4851 passed / 2 skipped / 51 deselected / 15 warnings / 833.29s / EXIT 0**。
   （与「4842」差 9 = 环境变量敏感集，换算自洽；**零 failed**——即无任何本批相关红。）
8. **Gate-0 裸全量**：`.venv/Scripts/python.exe scripts/gate0.py` → **6/6 PASS，18.3s**，
   读数落盘 `docs/gate/5b6af76acdcbf1bd6ca7cbd1be870ac2fcf8034d.json`（本深查产生的读数，未入库）。
9. **覆盖闸门**：`scripts/check_review_coverage.py` → **exit 0**（`089524a~1..HEAD` 每条 commit 均有归属；
   白名单冗余条目为 advisory warning）。
10. **突变实验（两轮，均已还原）**：
    - 单突变 `app.py:1374` 互斥检查失效 → `test_budget_local_fuse_api.py` **6 failed / 23 passed**
      （5 互斥维 + loud；max_cost_usd 走第二护栏保持绿——与文档化偏差逐字吻合）；
    - 双突变（+ `run_budget.py:1515` enforceability 失效）→ `-k "max_cost_usd or loud"` **2 failed**
      （loud + max_cost_usd）⇒「两护栏同删必红」实测成立；
    - 还原校验：`app.py` blob `20a3a97adc9bd2e2d485870e7af94b04994720ed` 与 `HEAD` 一致；
      `run_budget.py` blob `71ca9503999798fd6f09c75fa4b476454a3b37b7` 与 `HEAD` 一致；`git status` 两文件干净。

---

## 5. 总体判定

**可接受（无 P0–P2；批次可以维持合并态）。**

- 7 张票的修复在**正确性**与**票面一致性**两轴全部通过：无一票存在「测试空洞」「实现不符声明」
  「Scope 外改动」；两个最关键的判别力问题（#446 二选一、#440 活性接线）经**实测突变**与**全仓计数**
  独立证实。
- 被检方自述的四处薄弱点（§5.1–§5.4）逐一复核：§5.2/§5.3/§5.4 从「自述」升级为「实测确认」；
  §5.1（#456）维持「偏紧但合理」的静态判断（无法验证项）。
- **需修复清单：无。** 待办建议（均 P3、不阻塞，建议随后续批处理）：
  1. F3：把最终 main 树读数 `docs/gate/b3b8f2e2….json` 入库或清理残留（书证卫生）；
  2. F4：为「内联卡脉冲反向联动」补一条组件级测试（覆盖缺口，建议开 P3 票）；
  3. F5：invalid+submitted 的「无法再提交」措辞（1 行文案）。
- 残留风险登记：F1/F2 两类负载 flake 与 #456 偏紧语义已在案；本深查未引入任何仓库改动
  （全部写操作已还原/清理，工作树仅剩旧有 untracked 与新报告文件）。

---

## 附：本深查的环境动作与清理

- 临时 playwright 配置 `web/playwright.chk.config.ts`（port 5286 + `--strictPort`）——已删除；
- `/d/tmp/chk_pw_full.log` / `chk_pw_full2.log` / `chk_pytest_full.log`——已删除；
- `docs/gate/5b6af76a….json`——保留（本深查读数，报告 §4.8 引用；未入库）；
- 突变涉及的两文件——`git checkout` 还原并 blob 校验一致；
- 未触碰：`.env`、`model-providers.json`、端口 8000、任何他人未提交工作。
