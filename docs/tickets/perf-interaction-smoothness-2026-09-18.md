# 性能与交互流畅度硬化 Tickets — 本地执行索引

> 日期：2026-09-18
> 来源审查：一次全栈（前端 React 19 + 后端 FastAPI）「响应速度 / 交互流畅度」只读审查，
> 16 项候选（前端 F1–F8、后端 B1–B8），每项带 `文件:行号` 证据。
> 用途：本批 14 张可由独立 Coding Agent 领取的本地执行票；对应 GitHub Issues **#268–#281**，
> 父票（umbrella）为 **#267**。
> 本文是执行索引，**不替代** Engineering Specification、ADR 或 GitHub issue。
> **裁决规则**：GitHub issue 正文是每票 Scope/AC 的权威；本文只补充证据、依赖、串行约束与批次。
> 若本文与对应 issue 冲突，**以 issue 为准**；本文的额外建议不构成关单条件。

---

## 0.1 与并行批次的边界（**最高优先约束，2026-09-18 追加**）

同一仓库同时有**另一批进行中的架构整改**，由另一个 Agent 执行：

- 索引：`docs/tickets/architecture-audit-remediation-2026-09-18.md`
- 来源审计：`docs/research/2026-09-18-full-codebase-architecture-quality-audit.md`
- GitHub：父票 **#237–#248**（T01–T12），子票 **#249–#266**（18 张）
- 内容：**全部是 Python 后端 / 架构轴**（统一 implementation、收敛入口、冻结契约）

本批是**性能与感知轴**（消除无谓重渲染、动画上合成层、过渡连续、离屏不渲染）。
两批**目标函数不同**，不得混为一谈，也不得互相代替。

### 硬规则

1. **不得**修改 #237–#266 的任何候选文件（清单见对方文档每节的 `### 候选文件`）。
2. **不得**修改 #237–#248 的 issue、**不得**修改那 18 张子票、**不得**改写对方那份索引文档。
3. **不得**新建与那 18 张票同主题的 issue；**不得**新建平行 Roadmap（`AGENTS.md §5`）。
4. **不得**为了「顺手统一」而改动对方文件里的同类代码。
5. 若发现同文件有在飞改动 → **停止并报告**，不得自行 rebase / merge / 抢改。

### 本批与对方批次的文件交集

- **9 张前端票**（F1–F8 + N2）只改 `web/src/**`，与对方批次（全 Python）**零文件交集**。
- **2 张 docs 票**只新增/追加文档，零交集。
- **B6**（`src/agent_harness/storage/sqlite.py`）与 **B8**（`src/agent_harness/agent/runtime.py`）
  有**受控重叠**，各自带**硬 `Blocked by`**（#242 / #247），且票面写死了允许改动的具体行区间。
- **B7** 只改 `web/websocket.py` + `storage/local_artifact.py`，两者都不在对方候选清单里。

### 本批因冲突而**撤回**的项（不要重新提议）

| 曾评估项 | 归属 | 处理 |
|---|---|---|
| `has_session` 全量读判存在 | T09 / #245 | 撤回 |
| `list_sessions` 逐会话串行读摘要 | T09 / #245 | 撤回（T09 的 typed reader 是更根本的解法） |
| `GET /events` 分批取 | T09 / #245（已冻结「full/recovery 读取仍保留完整历史」） | 撤回 |
| 每事件 fsync 组提交 / 写后 `path.stat` | T05 / #241 + `docs/DEFER_ROUND2.md` R7-2 | 撤回（R7-2 重启条件未满足） |
| 启动扫描的**读取**部分 | T09 / #245 | 撤回 |
| 启动扫描按「未终态 run」**筛选**（原 B5b） | 落在 `recovery/scan.py` + `web/app.py`，两者都是 #237–#266 的文件 | **撤回**，不另开票 |

---

## 0.2 已冻结的跨票决策（2026-09-18 grilling）

后续 Agent **不得重新猜测**；若实现发现与 Engineering Specification 冲突，必须停止并报告。

- **G1 形态修正**——本批识别出 **5 处刻意设计**，**不得当成垃圾删掉**：
  1. **F8**：`web/src/lib/wsStream.ts:26-28` 明写「刻意把 WS 帧重新编码成 SSE 文本喂给既有
     `consumeSSE`，因此 `attachLiveStream` / 合帧器 / `seenSeqs` 去重门 / 重连调度全部零改动」
     ⇒ **保留同形契约**，只优化 `sse.ts` 的分帧扫描。**不得**拆成两个 source。
  2. **F4**：`web/src/App.tsx:879-899` 明写候选表要跟「最新事件」（Search Runtime Events / PRD §15）
     ⇒ **不得收窄** `conversation` 依赖，只做「关闭时不构建」。
  3. **F7**：`web/src/styles/app.css:63-72` + `docs/BENCHMARK_SYNTHESIS.md:69` +
     `web/e2e/y-inspector-peek.spec.ts:139-140` ⇒ **保持挂载**（DSH semantics）且
     **保留 `visibility: hidden` 语义**。
  4. **B3**（已撤回）：`src/agent_harness/session/service.py:417-421` +
     `web/src/lib/projection.test.ts` 的同构契约 ⇒ 已撤回（见上表）。
  5. **F6**：`web/src/styles/app.css:185-187` **逐字**写着「胶囊面积小，`box-shadow` 动画 repaint
     **可忽略**；reduced-motion 由全局块关闭」⇒ 这是**已记录的有意判断**。F6 的形态因此是
     **「用测量复核该判断」**：测得可忽略 ⇒ **不改代码**、只追加复核行；测得不可忽略 ⇒
     才改成合成层等价实现。
- **G2**：N2 采用 `eventsVersion`（用户裁定），**不采用**「memo key 换成 `events.length`」。
- **G3 基线硬前置**：任何性能票**没落可复核基线数字不许动代码**；基线落
  `docs/PERF_BASELINE.md`（本次新建）。**不要**把性能数字写进 `docs/PHASE_STATUS.md`
  （那是 Phase 进度表，混入即污染）。
- **G4 验收口径 A+C**：可自动化部分用测试断言；观感部分用浏览器 Performance 录制的
  **long task 数 + 最长单帧**（**不是** FPS 均值）判定，并把录制结果作为交付物存档。
  **不引入** Playwright 帧率自动采集（本项目 E2E 有收尾挂死的历史问题，成本 > 收益）。
- **G5 分支与提交**：在**当前 worktree** 开短分支，命名沿用仓库现有形态
  （`fix/web-…`、`perf/web-…`、`perf/…`）。**不得**使用 `T<编号>-` 前缀
  （那是另一个项目 deepsearch 的规范，本仓库无此先例）。集成由当前主开发执行
  （`AGENTS.md §14.4`）；本批执行者**不碰 main、不 push main**。
- **G6**：勘误走「在原文档新增勘误小节，**既有回执正文一字不改**」。
- **G7**：ADR-0037 新建，显式声明与 `ADR-0016 §2` 的关系（**补充，不覆盖**）。
- **G8 串行约束**：**B8 硬 blocked by #247**（同改 `agent/runtime.py` 同一函数体）；
  **B6 硬 blocked by #242**（`#242` 候选文件含 `storage/sqlite.py`）。

---

## 0.3 子票与依赖（严格 blockers-first）

| 本地票 | GitHub | 内容 | Blocked by |
|---|---:|---|---|
| 勘误 | **#268** | `HANDOFF_PERF_FRONTEND.md` 补 §11 勘误小节（docs-only） | 无 |
| ADR-0037 | **#269** | 投影层引用稳定与 `eventsVersion`（docs-only） | 无 |
| F1 | **#270** | 稳定 `disclosure` 引用，接回被折断的 memo 链 | 无 |
| N2 | **#271** | 引入 `eventsVersion`，修 StepDetail 三处陈旧 memo（**正确性缺陷**） | 无 |
| F2 | **#272** | `Conversation` / `StepDetail` 补齐 memo 与 props 收敛 | #270 + #271 |
| F4 | **#273** | 命令面板按打开态门控构建 | 无 |
| B6 | **#274** | Operation Ledger 单动作三连连接收敛 | **#242** |
| B7 | **#275** | 事件循环内同步重活搬线程（WS 快照 + 本地 artifact IO） | 无 |
| F3 | **#276** | 流式贴底读写收进单帧 | #272 |
| F6 | **#277** | 呼吸辉光改走合成层（或按基线证明可忽略并关单） | 无 |
| F7 | **#278** | Inspector 与工作区面板的过渡编排 | #273 + #277 |
| F5 | **#279** | Inspector 三个长列表离屏跳过 | #272 |
| F8 | **#280** | SSE 分帧改游标扫描 | 无 |
| B8 | **#281** | 流式 chunk 聚合去 O(n²) | **#247** |

> 原 15 张子票中的 **B5b（启动扫描按「未终态 run」筛选）已撤回**，不另开票（见 §0.1）。

### 文件所有权矩阵（**同文件同一时刻只允许一张票在飞**）

| 文件 | 占用票（按先后） | 说明 |
|---|---|---|
| `web/src/lib/disclosure.ts` | F1 | 只此一张 |
| `web/src/lib/projection.ts` | N2 | 只此一张 |
| `web/src/components/Conversation.tsx` | F1 → F2 → F3 | 三张串行 |
| `web/src/components/StepDetail.tsx` | N2 → F2 → F5 | 三张串行 |
| `web/src/App.tsx` | F4 → F7 | 两张串行 |
| `web/src/styles/app.css` | F6 → F7 | 两张串行 |
| `web/src/lib/sse.ts` | F8 | 只此一张 |
| `src/agent_harness/storage/sqlite.py` | **#242** → B6 | 撞 #242 候选文件 |
| `src/agent_harness/web/websocket.py` | B7 | 只此一张 |
| `src/agent_harness/storage/local_artifact.py` | B7 | 只此一张 |
| `src/agent_harness/agent/runtime.py` | **#247** → B8 | 撞 #247 同函数体 |
| `docs/HANDOFF_PERF_FRONTEND.md` | 勘误 | 只加 §11，既有回执一字不改 |
| `docs/adr/0037-*.md` | ADR-0037 | 新文件；开工前先确认 0037 未被占用 |
| `docs/PERF_BASELINE.md` | **所有性能票追加** | 只允许**追加**，不得改写他人已落的行 |
| `docs/SDD_TICKET_TRACKER.md` | **所有票追加**（**且与并行批次共享尾部**） | ⚠ 见下方「跨批共享的追加目标」 |
| `docs/review_ledger.tsv` | 落点提交追加 `[whitelist]` 行 | 需**真实 SHA**，故只能在 commit **之后**补 |
| `docs/phase_status/2026-09.md` | 落点提交追加索引行 | 只加索引；性能数字**不进**此文件 |

> ⚠ **跨批共享的追加目标（覆盖缺口，2026-09-18 补充）**：上表前面各行是「本批内部」的串行约束
> （本批自制机制，`AGENTS.md §5` 之外），但下面三个文件**同时是另一批（#237–#266）的写入目标**，
> 「同一文件同一时刻只允许一张票在飞」在这里**无从落地**——两批互相看不见对方的在飞状态：
>
> - `docs/SDD_TICKET_TRACKER.md` —— 两批都在**文末追加**新 section；
> - `docs/review_ledger.tsv` —— 两批都在 `[whitelist]` 段追加行；
> - `docs/phase_status/2026-09.md` —— 两批都追加索引行。
>
> **处置（不是「谁先谁赢」，而是让冲突可机械解决）**：本批的追加一律
> ① 落在**文末**；② 用 HTML 注释包裹出**显式边界**
> （tracker 用 `<!-- ===== 批 P1（#267）性能与交互流畅度硬化 —— 本批台账起点（2026-09-18） ===== -->`）。
> 这样尾部双追加冲突的解法**恒为「两边都保留」**，不需要任何内容判断。
> ⚠ **顺序代价（如实写明）**：同一分支上若两批的追加提交交错，`merge` 会要求人工解一次尾部冲突，
> 因此**落点提交应与实现提交同批推送**，不要长期悬着。

---

## 0.4 建议批次

1. **批 1 — 缺陷优先**：勘误(#268) → ADR-0037(#269) → F1(#270) → N2(#271) → F2(#272)
   这五张是一条链：勘误让后续 Agent 不再被误导，ADR 冻结语义，F1 接回 memo 链，
   N2 修正确性缺陷，F2 才谈得上验收。
2. **批 2 — 低风险纯优化**：F4(#273) → B6(#274) → B7(#275)
   ⚠ B6 硬 blocked by #242；若 #242 未合并，跳过 B6 先做 F4 / B7。
3. **批 3 — 流畅度与长尾**：F3(#276) → F6(#277) → F7(#278) → F5(#279) → F8(#280) → B8(#281)
   ⚠ B8 硬 blocked by #247；若 #247 未合并，B8 顺延到最后。

---

## 0.5 统一执行协议

每张票按以下顺序执行：

1. 读相关 Engineering Specification、本批 issue 正文中的证据与候选文件。
2. **先跑「并行批次避让」的开工前自检**（`git status --short` +
   `git log --oneline -8 -- <本票文件>`），确认无同文件在飞改动。
3. **先写/确认红证**：必须能在基线证明缺口或漂移；纯结构票先建立行为 golden。
   性能票**先落 `docs/PERF_BASELINE.md` 的前置数字**（G3），否则不许动代码。
4. 最小修改；**不顺手改相邻热点**（`AGENTS.md §8 Scope Lock`）。
5. 跑票面专项测试 + 相关目录测试 + `ruff`（前端为 `tsc -b` + `oxlint` + `vitest`）。
6. `git diff --check`；确认只改票面范围（`git diff --stat` 对照 issue 的 `## Scope lock`）。
7. DoD 证据记录：命令、结果、关键文件、已披露的行为变化、`docs/PERF_BASELINE.md` 的前后数字。
8. 记 `docs/SDD_TICKET_TRACKER.md` 的对应行。
9. 按 `docs/SDD_WORKFLOW_PROTOCOL.md` **每 2–3 票**对累计 diff 跑一次两轴 review。

> ⚠ **禁止 `git stash`**。本机实测一次 `git stash -u` 会清掉 `.git/refs` 使仓库不可用。
> 需要旧版本时用 `git show <sha>:<path>` 导出到临时目录做 A/B。
> ⚠ 本环境 bash shim **缺 coreutils**（`ls` / `cat` / `head` / `dirname` 不可用）。
> 列目录/读文件用 Glob / Read / Grep；聚合统计用 `python -c`。

---

## 0.6 全局完成定义

全部子票完成 ≠ 关掉父票 #267。父票关单还必须同时满足：

- **不变量 #22**（Web 不维护第二套 Session 真相）仍成立。
- **引用稳定契约**（`web/src/lib/projection.test.ts` 的 COW 引用稳定测试）原样全绿——
  除 N2(#271) 票明确说明的新增断言外**不得改写**。
- **每一处改动都有前后数字**落在 `docs/PERF_BASELINE.md`。
- **5 处刻意设计**全部原样保留（见 G1）。
- **未撤回任何已冻结决策**；若确需改动，必须新 ADR 并与本索引显式对齐。
- 专项测试、`ruff`、`tsc -b`、`oxlint`、全量测试与 `scripts/check_review_coverage.sh`
  都有**可复制的**关单证据。
- 每个「判定为无需处理」的项都有**测量数字**（不允许无数字的 wontfix）。
