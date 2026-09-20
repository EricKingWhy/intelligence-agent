# SDD 工作流协议（V3-lite：按风险审查）

> ## ⚠️ 自愈条款（最高优先级，先读这段）
>
> **任何时候你发现自己**：
> （a）不确定当前 Ticket / 验收条件 / 工作状态；
> （b）不记得当前 review 覆盖到哪个 commit；
> （c）上下文刚被压缩 / 摘要过；
>
> **第一动作 = 重读 `docs/SDD_WORKFLOW_PROTOCOL.md` + `docs/SDD_TICKET_TRACKER.md`，并核对 Git 状态。
> 按记录恢复，不凭记忆猜流程或 Ticket 状态。**

> 本文件是当前 SDD 执行流程的**唯一权威**；`AGENTS.md` §16 与 `CLAUDE.md` 只提供入口，
> `docs/SDD_TICKET_TRACKER.md` 记录事实，不定义流程。进入新 context window 或摘要后，
> 先读本文件和 Tracker，再按本文件恢复当前工作。
>
> **协议版本：V3-lite（2026-09-20）**。V3-lite 在 V2 的基础上保留 Ticket / Spec 对齐、focused 验证、风险审查、独立 review 证据、review ledger 和最终集成闸门；
> 移除固定的“每 2–3 个 Ticket 审一次”节奏和不可用 Skill 的强制要求。V1/V2 流程记录仅作历史，不再是执行要求。

---

## 1. Ticket 与实施

### 1.1 开始一个 Ticket

1. 读取当前 Issue / Ticket 的验收条件、相关 Engineering Specification 和 Reuse Matrix；核对代码、测试、分支与工作树。
2. 把目标写成可验证结果并锁定范围。发现验收条件实质冲突、新证据证明目标不可实现或需要改变架构时，先报告证据与最小替代方案，再请求用户决定；未经批准不改 Issue、Ticket 或冻结规格。
3. 选择当前环境实际可用的工具和 Skills。`/implement`、`/ask-matt` 等未枚举的命令不是强制步骤，也不得假设存在。

### 1.2 实施与验证

1. 按最小垂直切片实现；修 Bug 或新增行为时先建立能命中真实症状的测试，再修复并复跑。
2. 每个 Ticket 跑与改动相称的 focused tests、lint / type check 和必要的集成测试；**不要求每张票都重跑全量测试**。高风险跨模块改动可在自然边界提前跑更广门禁。
3. 相关验证通过后再提交，commit message 描述实际工程事实；更新 `docs/SDD_TICKET_TRACKER.md` 的状态、提交、门禁证据和残余问题。`uv run` 后检查 `uv.lock`，处理规则见 §7 第 6 条。

## 2. 按风险安排 Code Review

V3-lite **不设固定 Ticket 数量或日历节奏**。在变更风险高、边界清楚且 review 能显著降低风险时审查；具体时点由实现证据决定。

以下改动默认值得独立 review：

- 凭证、权限、Sandbox / Host 边界或外部副作用；
- 删除、迁移、持久化、SessionEvent、Checkpoint、Recovery 或 Operation Ledger；
- 并发、取消、流式生命周期、共享进程状态；
- 公共 API / Contract、Provider 边界、跨模块或大范围改动；
- 测试结果与票面 / 规格不一致，或新证据改变原假设。

低风险文档、纯测试和局部机械改动可合并到最近的自然 review 边界。review 按项目需要覆盖 Standards 与 Correctness / Spec；审查范围必须准确记录实际读过的 base..tip。发现的问题按影响修复：疑难、间歇或并发 Bug 使用 `diagnosing-bugs` 建立可复现反馈环；其余做最小修复并跑对应测试。

**集成前覆盖要求保持不变**：每个代码提交都必须落在真实 review ledger 审查范围内；此前未审的低风险代码在集成前补一次最小范围 review。已审范围没有后续代码变化时，不重复跑相同的全量 review。Review 修复提交也必须被后续 review 范围覆盖；代码提交不得用 whitelist 放行。

## 3. 完成与集成

1. 更新 Tracker、`PHASE_STATUS.md` 当前焦点 / 索引和当月归档；每条事实只写全一次，其余位置给指针。
2. 集成前按 `AGENTS.md` §14.10 通过全量测试、lint、type check（如有）、`git diff --check`、审查覆盖闸门及工作树检查；比较 tree，确认集成内容与已验证内容一致。
3. Branch merge、push、issue close 和跨仓库同步遵守 `AGENTS.md` §13–14。前一阶段的授权不自动扩大到下一种 Git 写操作；冲突按 §14.7 分析并取得批准。

### 3.1 不确定与票面变化

不要用猜测代替规格或用户决定。若验收条件不可实现、证据推翻原假设，或存在会改变架构 / 范围 / 数据迁移的选择，先停止相关实现，提交可复现证据、影响和最小替代方案，请用户批准后再改票面或计划。普通实现细节自行判断，不频繁打断用户。

### 3.2 Git 与推送

- 实现线默认只做本地 commit，不在 feature branch 上 push。
- 集成与 `push origin main` 的授权分类、前置条件和通知要求统一见 `AGENTS.md` §14.4、§14.9、§14.10；本文件不重复定义。

---

## 4. Context Window 恢复协议

任何新 context window 启动时，按以下顺序恢复：

0. **先执行上面的自愈条款**（不确定就重读，不猜）
1. 读 `docs/SDD_WORKFLOW_PROTOCOL.md`（本文件）
2. 读 `docs/SDD_TICKET_TRACKER.md`（当前在途 Ticket、验证证据与 review 覆盖状态）
3. 核对 `git status`、分支与 HEAD；确认无用户改动被覆盖
4. 根据 Tracker 与当前用户授权继续工作

### 4.1 恢复时的自检清单

- [ ] 当前在哪个 worktree？（`git worktree list`）
- [ ] 当前在哪个分支？（`git branch --show-current`）
- [ ] HEAD 是哪个 commit？（`git log --oneline -1`）
- [ ] 工作树是否干净？（`git status --short`）
- [ ] 当前在途 / 上一个完成的 Ticket 是哪个？（查 Tracker）
- [ ] 当前 HEAD 与工作树是否符合 Tracker 记录？
- [ ] 哪些代码 commit 已有 review 覆盖，哪些尚待 review？（查 Tracker + `review_ledger.tsv`）
- [ ] 是否有未修完的 review finding 或待用户决定事项？

---

## 5. Ticket Tracker 与进度记录

`docs/SDD_TICKET_TRACKER.md` 记录每个 ticket 的状态：

| 字段 | 说明 |
| --- | --- |
| Ticket ID | 如 FE-T7 |
| 描述 | 一句话说明 |
| 状态 | 记录项目当前使用的 Ticket 状态；`batched` 等旧批次状态只保留为历史 |
| 实现方式 | 用了什么 skill、改了哪些文件 |
| Commit SHA | 完成时的 commit hash |
| 门禁结果 | 前端：tsc/vitest/oxlint/playwright/build；后端：ruff/pytest（+ 真机 gate/变异，若有） |

历史批次记录（V1/V2）：

| 字段 | 说明 |
| --- | --- |
| 批次号 | 如 `B1`、`B2` |
| 本批 tickets | 这批包含哪几个 ticket |
| fixed point | 本批累计 diff 的起点 commit（= 上一批审查结束时的 commit） |
| 审查结论 | 轮次 + findings 分级 + 是否零 finding |
| 修复 commit | 当时 findings 修完后的 commit；该字段保留历史事实，不定义 V3-lite 审查节奏 |

⚠ **表格里的范围数字只是人读的索引；交付前"哪些 commit 真的被审过"以机读台账
`docs/review_ledger.tsv` 为准**（覆盖对账见 §7 第 8 条——手抄的 review 范围错一格就是
#213 那次静默豁免）。审查行的 tip 必须 = **审查实际读到的末条 commit**：审查之后才创建的
修复提交**不在**那个范围内，必须由另一条真实 review 行覆盖。

---

## 6. Ticket 来源

**不在此维护静态清单**——它会在每个 ticket 完成后立刻过期（本文件 2026-09-10 曾在此写下
FE-T7/T8/T9 三张票，该阶段早已结束，而清单留在这里一直被当成"当前剩余工作"）。

当前剩余工作唯一来源：

- `docs/SDD_TICKET_TRACKER.md` —— 在途 ticket、验证证据、review 状态与残余问题；
- `docs/PHASE_STATUS.md` —— Phase 状态与集成证据；
- GitHub Issues（`EricKingWhy/intelligence-agent`）—— 票面与验收标准。

---

## 7. 执行边界与集成闸门

1. **按风险安排 review**：不设固定 Ticket 数量节奏。集成前每个代码 commit 都必须有真实 review ledger 覆盖；审查范围缺口在集成前补齐。
2. **使用实际可用的 Skills / 命令**：按任务需要选择；不要求不存在的 `/implement`、`/ask-matt` 或其他工具。
3. **feature branch 不 push**：本地 commit 可以；push 与集成授权按 `AGENTS.md` §14.4 执行。
4. **规格外架构决策先问用户**：按 `AGENTS.md` §9.1 / §9.1.1 处理新证据、验收条件和范围变化。
5. **禁止跨仓库无授权写入**：`D:\intelligence-agent`（main）、`D:\intelligence-agent-backend`、
   `D:\intelligence-agent-frontend` 是**三个独立 clone**（不是 worktree），各有自己的工作树与分支。
   用户可以授权任意一条线做另一端的活，但**动手前必须先读 `AGENTS.md` §13 的仓库模型**——
   "三方一致"靠"谁集成谁通知、另一条线开工前先把 main 合回来"维持，不靠目录名分工。
6. **按改动选择逐票验证，集成前通过完整门禁**：逐票跑相关 focused tests、lint / type check 和必要的集成测试；不要求每张票都重跑全量。集成前完整门禁见 `AGENTS.md` §14.10：后端 `ruff check` + 全量 `pytest`；前端（在 `web/` 下）
   `npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build`。
   e2e 必须 `--workers=2`（4 worker 全量并行存在资源竞争型抖动）。
   后端门禁用 `uv run`，而 `uv run` 会在锁过期时**静默重写 `uv.lock`**。所以跑完先看
   `git status`：若出现 `uv.lock` 改动，说明提交的锁与 `pyproject.toml` 不同步（#216）。
   处置：**立刻以独立的 chore commit 落地**（`chore(#216): uv.lock 同步`）——不要用
   `git add -A` 把它混进功能票，也不要为了"工作树干净"顺手 `git checkout -- uv.lock`
   （那只是把不同步藏起来，下一次 `uv run` 还会再改一次）。§14.10 要求的"工作树 clean"
   指的是**进入集成之前**：这份改动同样要有归属的 commit，不能带着未提交的锁进集成。
7. **禁止凭记忆猜流程**：不确定就执行自愈条款（重读本文件 + Tracker）
8. **禁止未审查的代码 commit 进集成**：交付前跑 `scripts/check_review_coverage.sh`（台账
   `docs/review_ledger.tsv`）。它机械地对账「`<最早台账 base>..HEAD` 的每条 commit 是否有
   台账归属」，**例外两类**：
   - `[whitelist]` 段的 **docs-only** commit（脚本自己校验：改动文件全部命中文档扩展名；
     `docs/` 下只认 .md/.txt/.rst/.tsv/.json/.yaml —— 仓库里就有 `docs/integration/*.sh`，
     按 `docs/` 前缀放行会把可执行脚本当散文，2026-09-17 两轴审查 P1 实测复现）；
   - **恰好只改台账文件本身**的记账提交（机械可验、藏不了代码，自动放行——"把记账这件事
     记进台账"再要求记账是**死循环**，实测绕了三轮）。

   **代码提交永远不能靠白名单放行，必须落在真实 review 行中**。为什么需要这条（2026-09-17 实测
   两个案例，都是复验才发现）：
   - **#213 从未被任何 review 读过**：旧 V2 把 fixed point 作为批次边界，
     但那个数字是**手抄进 tracker 的**，抄过一格（把 fixed point 设在该票自己的末条提交上）
     就静默豁免一票，没有任何东西会报错；
   - **修复提交结构性免疫**：旧 V2 表格写着「修复 commit = 下一批的 fixed point」，于是交付
     周期里**最后一次审查的修复提交没有下一批**。实测 `9f2a8f8` 就是这样进的 main，而补审它
     时发现它带着一个真 bug（凭据删除 fail-open）。

   **信任边界（如实写，别把它读成"审查真的发生过"）**：台账是**声明式输入**，审查行的真实性
   由人对账——脚本只能证明"每条 commit 都有归属"，不能证明审查内容真实或完整。三点已知限制：
   ① 审计窗口的**左端由台账自己决定**（删掉最早那行即可整体缩短窗口，脚本无外部锚点）；
   ② 台账从**工作树**读，不读 HEAD 版 ⇒ 必须在**干净检出**上跑；
   ③ 闸门**目前只手动跑**（无 CI、无 hook），靠流程纪律而不是机器强制。
   新增 review 后：把准确范围写进台账新行，并更新 Tracker 的 review 状态与证据指针。**不要改台账蒙过去**。

   顺带（同一批实测得出）：**集成后不必重复跑全量门禁——先比 `HEAD^{tree}`**。两个 clone 的
   tree 相同即证明"跑过门禁的那棵树 = 被集成的这棵树"（2026-09-17 实测同为 `e63c202…`，
   省掉一次 ~20 分钟的 前后端全量）；不等才在集成 clone 里跑（细节见 `AGENTS.md` §13.4）。
