# R5 深检修复批次交接简报（2026-09-30）

> **本文用途**：R5 深度检查报告（`R5_DEEP_CHECK_REPORT_2026-09-30.md`）开出的 7 张票（#440–#446）
> 已由本会话修复并合并。用户将指派**另一个（能力更强的）模型做一次独立深度检查**来复核这批修复。
> 本文是给那位检查者的**被检方自述 + 证据索引**：我做了什么、证据在哪、我自知的薄弱点在哪。
> 检查者应当**独立复核一切，不信任本文的任何结论**——本文的价值是告诉你去哪看，不是让你信什么。
> 本文**未提交入库**（untracked，同 `R5_DEEP_CHECK_BRIEF_2026-09-30.md` 先例）；是否入库由用户决定。

---

## 1. 授权链（时间序）

1. 前一会话：本会话模型担任**独立深度检查者**，产出 `R5_DEEP_CHECK_REPORT_2026-09-30.md`（9 项发现：1×P1 / 1×P2 / 7×P3），并按用户选择开出 **#440–#446** 七张票（每票含修复依据）+ 三条归档评论（#420/#421/#426）。当时用户决策：**「只开 issue，代码先不动」**。
2. 本会话用户指令（**推翻上条并授权修复**）：「1.报告要入库。2.你可以修复，优先级是 #440（真缺陷）→ #441（车道清零，是 #421 票面 AC 的承载面）→ #442/#443/#444/#445/#446。3.起subagent帮你干活，不要省token，不许出现bug」。
3. main 受服务端保护（GH006，§14.4）⇒ 一切集成走「推分支 → PR → gate0 绿 → merge」通道；三次 PR 合并均在此授权链内执行。

## 2. 我做了什么（一票一行）

| 票 | commit | 修了什么（一句话） | 票面偏离 |
| --- | --- | --- | --- |
| 报告入库 | PR #453 | 报告 + 两笔 gate0 读数 + verification.map 登记行（guards 车道要求 tracked 文件全覆盖） | 无 |
| **#440** P2 | `e2fb7cc5` | AC1 活性回调补齐 4 个漏接点（sendFollowUp 排队回执/launched、resumePausedRun、flushQueue）⇒ `wsStreamResponse` 全仓 8/8 调用点带第三参；SSE 降级读循环 `reader.read()` 成功即喂 `onLiveness`（`done` 空 read 不算）；`app.py` **仅注释**统一「心跳/到达字节=链路活性」口径 | 无 |
| **#441** P1 | `df2a0e62` | 14 条审批 e2e 断言对齐 #421/#420 后语义：文案「已批准/已拒绝」→「决策已提交」、定位改模态作用域、焦点断言改指模态卡、x-permission 改两段式投递 | 无（防护断言零削弱，独立审查逐条核对） |
| **#442** P2 | `c690a7eb` | 四族批前 e2e 红修复：裸读竞逐改 `expect.poll`/`toHaveText`、T12m/T12n 加 bodies+settled 双锚、o-wait-hint 补 fastForward 前锚、control-row 12/17→13/18。**实现零改动**（诊断结论：四族全为测试侧缺陷） | 族2 有 2 处诊断未点名但同机制的加固（`:92-93`/`:288`，期望值原样）；缺省单 Chat 态 3 处**有意保留裸读**（逐点注释） |
| **#443** P3 | `5c47184b` | TopBar 判据改用 projection 既有 `awaitingApproval`（stale 审批不再算「在等」） | 无 |
| **#444** P3 | `e5697cba` | ApprovalCard submitted 说明加 `!invalid` 门（标题/说明不再同卡矛盾） | 无 |
| **#445** P3 | `d6b4bd0b` | CLI `_resolve_fork_ordinal` 先分「不存在/空日志」再判序数越界（措辞与 `fork_session` 同源）；越界/恰好N/N=0 语义零改动 | 无 |
| **#446** P3 | `55ec7fd4` | 参数化六维标明预期拒绝者：五维钉互斥文案；max_cost_usd 维改「规格内护栏二选一」断言 | **有，票面字面写法与正常态实测矛盾**（handler 级互斥检查 `app.py:1374` 先于 `validate_ceiling_enforceability` 触发 ⇒ 字面写法正常态必红）；commit message 与台账行均如实记录 |

关票：#440–#446 全部 CLOSED（各带证据评论）。遗留新票：**#456**（见 §5）。

## 3. 施工方式与过程异常（检查者应知道的实况）

- **subagent 分工**（用户要求「起 subagent、不省 token」）：#442 先派**只读诊断 agent**（route 延迟法逐字复现 workspace-modes/r-project-groups 症状；批前树 `git show 8db02e83:<path>` 逐字同源对照 ⇒ 判定四族全为测试侧缺陷、实现零回归），修复 agent 按诊断结论实施；其余票各派实现 agent（TDD：先写红测试看它失败，再修看它绿）；合并前派**独立审查 agent**（只读，逐 commit 两轴：票面对照 + 代码正确性）。
- **平台并发上限 2**：首波 6 派 4 拒；改为两波。**#445 agent 把活干完后被并发限制器截断、未交报告**——主会话对其工作树成果逐文件核对归属（确认零来历不明改动），并用单文件 `git stash` 复现红（2 failed）→ 还原绿（9 passed）补足红→绿证据后验收。⚠ 这意味着 **#445 的实现不是主会话亲眼逐行盯写的**，审查力度靠独立审查 agent + 主会话补验——值得重点复核。
- 台账：`docs/review_ledger.d/r5-deepcheck-fixes-e31cb654-c690a7eb.tsv`（7 行，一 commit 一行）+ 两笔先回后正合并审计行（`ci-02bd0b12-17df2b99`、`ci-93f591f5-7ef3e761`）。

## 4. 交付物索引

| 对象 | 位置 |
| --- | --- |
| 修复分支 | `fix/r5-deep-check-440-446`（基点 `e31cb654`），7 commit `d6b4bd0b`→`c690a7eb` + 台账 `93f591f5` + 先回后正 merge `7ef3e761` |
| 集成 | **PR #455 → main `361e00e5`**；报告 PR #453；记账 PR #457 → main `5b6af76a`（当前 main tip） |
| 台账 | `docs/review_ledger.d/r5-deepcheck-fixes-e31cb654-c690a7eb.tsv`（+2 笔 ci-* 行） |
| gate0 读数 | `docs/gate/07166334….json`（修复树 tip 6/6 PASS）、`docs/gate/b3b8f2e2….json`（记账树） |
| 记账 | tracker「GUI-R5 深检修复批次」段、2026-09 归档「R5 深检七票修复」小节、PHASE_STATUS 最近条目首行 |
| 新测试 | `web/src/hooks/useSession.liveness.test.tsx`（+`.sse.`）、`web/src/components/TopBar.approvalWaitHint.test.tsx`、`ApprovalCard.invalidSubmitted.test.tsx`、`tests/test_cli_fork.py` +2、`tests/web/test_budget_local_fuse_api.py` 改造、5 个 e2e spec 更新 |

## 5. 我自知的残余与薄弱点（检查者优先看这里）

1. **#456（已开票，未修）**：#440 实施中发现——停摆重连挂新流**不重置** `lastFrameAtRef` 基准 ⇒ 持续无帧时重连链以 10s 节奏耗额度。按 §9.1.1 开票不修。请复核该判定（「偏紧但合理、非功能缺陷」）是否成立。
2. **#441 覆盖缺口**：AC5 改写后「Inspector 待审批行点击 → 内联卡脉冲」反向联动（stale/echo 卡路径）失去唯一自动化覆盖。只在 #441 关票评论记录，未开票未补测。
3. **#442 的 3 处保留裸读**（`:67`/`:108`/`:152`）：诊断与修复方都判定「缺省单 Chat 态无竞逐可吃」。这是本批我最想被第二双眼睛复核的判断。
4. **#446 票面偏离**：二选一断言是否真的「不掩盖真回归」，独立审查只做了推理核验（两护栏同删→422 断言必红；互斥单删→五维转红），未做实测变异。
5. **#444 遗留观察**：invalid+submitted 并存时「决策无法再提交」措辞对已提交用户事后不精确（显示级，票面外，未动）。
6. **#442 族3 的 T12l**（迟到 409 人工裁决）与 T12m/T12n 同形但**未加固**（不在诊断点名清单，Scope Lock 未动）。
7. **pytest 9 假红的证伪逻辑**：全量跑在 `ALLOW_SHARED_ROOT=1` 下 9 个 lock 语义用例红（instance_lock ×4 + memory/v2 clean_slate_cutover ×5，含名字就叫「refuses shared root writer」的）——我用 `--lf` 双向复跑证伪为环境变量机制（无变量 9/9 绿）。请独立复核这个归因。
8. **已知环境 flake（非本批引入，勿误判）**：`web/src/components/StepDetail.window.test.tsx`「DIFFS/ARTIFACTS」负载敏感（本批两次全量各红一次，隔离复跑均绿）；stream-fallback / T12p / T12r 在满载+全量下偶红（诊断台账 B/C 有录）；`tests/model/test_stall_watchdog.py`（历史已知）。
9. 远端分支 `docs/r5-deep-check-report`、`fix/r5-deep-check-440-446`、`docs/r5-fix-batch-epilogue` 未删（§14.4 需用户单独批准）。

## 6. 环境备忘（踩过的坑）

- Python 一律 `.venv/Scripts/python.exe`（裸 `python` 解析到仓外 venv）。
- playwright 全量：5173 被用户 dev server 占用 ⇒ 临时配置换端口（我用的 5286：复制 `web/playwright.config.ts` 改 `PORT`，且 webServer.command 改 `npx vite --strictPort --port 5286`——只改 PORT 不改 command 会撞 `dev:e2e` 的脚本级 5173 预检）。用完删临时配置。
- vitest/tsc：`cd web && ./node_modules/.bin/tsc -b`；pytest 带实例锁语义时环境变量 `ALLOW_SHARED_ROOT=1` 是逃生门（但见 §5.7 的副作用）。
- 端口 8000 是用户 Docker Desktop **不许杀**；`.env` 值绝不打印；中文大文件用 Read/Grep 不用 cat/tail（GBK 乱码）。
- main 受保护：任何复核性改动也只能走 PR，不要试图直推。

## 7. 给深检模型的提示词（可直接粘贴）

> 你是独立深度检查者，对 `D:\intelligence-agent`（当前 main `5b6af76a`）刚合并的「R5 深检修复批次」做合并后独立复核。被检对象 = PR #453/#455/#457 的全部改动，核心是 PR #455 的 7 个 commit（`d6b4bd0b` #445、`55ec7fd4` #446、`df2a0e62` #441、`5c47184b` #443、`e5697cba` #444、`e2fb7cc5` #440、`c690a7eb` #442）。被检方自述与证据索引见仓库根 `R5_FIX_BATCH_BRIEF_2026-09-30.md`——**先完整读它**，但不信任其任何结论，它是供你定位与反驳的靶子。
>
> 【任务】逐票两轴检查：①代码正确性（自己读 diff 与实现，不信 commit message——例如 #440 要自己 grep `wsStreamResponse(` 核对接流点总数；#441/#442 要逐条对照组件事实核对断言强度，确认只改读法不削期望）；②票面一致性（`gh issue view 440`…`446` 对照票面与「修复依据」，票面外改动一律报告）。重点专项：(a) §5.3 三处保留裸读是否真的无竞逐；(b) §5.4 #446 偏离是否合理、二选一断言是否掩盖真回归（建议实测变异）；(c) §5.7 pytest 9 假红归因是否成立（可自己双向复跑）；(d) #445 实现未经主会话逐行盯写——全 diff 细读；(e) #442 实现零改动声明是否属实；(f) 新测试自身是否有假绿（对照组/vacuous green 设计是否有效）。
>
> 【验证要求】不要只做静态阅读：至少复跑 pytest 改动面、vitest 相关文件、tsc、以及受影响的 e2e spec；有条件时跑一次 playwright 全量（端口技巧见简报 §6）。环境红线：Python 用 `.venv/Scripts/python.exe`；端口 8000 是用户 Docker Desktop 不许杀；`.env` 的值绝不打印；只删你自己产生的临时文件；读中文大文件用 Read/Grep 不用 cat/tail；不改全局 model-providers.json。
>
> 【纪律】默认只读：发现问题记录，不改代码——修复是另一个授权。发现违反仓库不变量（AGENTS.md §7）或凭证泄漏面（§4.3）时最高优先报告。
>
> 【输出】写报告文件到仓库根 `R5_FIX_DEEP_CHECK_REPORT_<日期>.md`（未入库，等用户处置）：每条发现给 严重级(P0–P3)/证据(file:line 或命令读数)/为什么是问题/建议修法；区分「本批引入」与「批前既有」；对被检方自述的每条关键声明给出 确认/反驳/无法验证 三态结论；最后给总体判定（可接受 / 需修复清单）。报告里所有门禁读数必须来自你可复跑的命令，不许手抄简报里的数字。
