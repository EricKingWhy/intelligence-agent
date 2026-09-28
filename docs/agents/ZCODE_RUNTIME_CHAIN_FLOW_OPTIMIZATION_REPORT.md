# B 链（Runtime 链 `#306`–`#320`）执行流程优化报告

> **作者 / 读者**：ZCode（B 链 / `intelligence-agent-backend` 的当前主开发）写给**另一个 Coding Agent**看。
> **仓库 / 分支 / 基线**：`D:\intelligence-agent-backend`，`zcode/T313-model-request-budget`，分支基点 `0d03187`（= 当时的 `origin/main`）。
> **覆盖范围**：`#306`（T1）→ `#307`（T2）→ `#308`（T3）→ `#312`（T4）→ `#313`（T5，收尾中）的施工与集成过程。
>
> **定位声明（先读这一句）**：本文是**描述性报告 + 可移植做法清单**，**不是流程权威**。
> 流程权威是 `docs/SDD_WORKFLOW_PROTOCOL.md`（V3.1-lite）与 `AGENTS.md` §13/§14；
> 进度事实是 `docs/SDD_TICKET_TRACKER.md` 与 `docs/PHASE_STATUS.md`。
> 三者与本文冲突时，一律以它们为准；本文只回答「哪些做法真的压缩了周期、哪些坑不必再踩一次」。

---

## 0. 一句话结论

优化全部落在**等待与返工**上，**一条闸门都没削**：

1. 让每票的**证据与落点当场成型**（不攒到最后一次性补文档）；
2. 让审查**一次派清**（映射表 + 预算 + 强制结论行，避免二次追问与开放式返工）；
3. 让**读数机器可引用**（`docs/gate/<sha>.json` + `docs/live_gate/**`，人手抄数字这条路径删除）；
4. 让**集成小步快跑**（每票通过即合 `main` 并 push，不再攒批次）。

反过来：**真实模型的 3×3 次尝试、全量 pytest、前端三车道**这些墙钟时间**压不动**——
优化能省的只有「等人读懂、等人问清、等人返工」的那部分。

---

## 1. 八项优化（编号沿用当时的提案）与落地证据

| # | 内容 | 状态 | 证据（可直接打开核对） |
| --- | --- | --- | --- |
| O1 | 集成节奏改为「**每票审查通过后即合入 `main` 并 push**」，不再攒批次 | 已生效（用户 2026-09-25 常设授权） | 本文 §3 的时间账；`#312` 的合入与 push 已按此执行 |
| O2 | **票面简报 + 代码地图**：开工前把「读规格 + 设计」沉淀成文件，随分支走 | 已落地 | `docs/tickets/T313-model-request-budget-brief.md`、`docs/agents/runtime_budget_code_map.md`（commit `3443f06`） |
| O3 | **修 Live Gate 场景的取证缺陷**：终态读盘加「独立复读、逐条同序」的静止自证 | 已落地 | commit `d4de6c5`（`evaluation/live_gate/scenarios/smoke.py` / `long_task.py`）；原缺陷 `089de04` |
| O4 | **审查派发格式固定**：AC→文件映射 + 派发预算 + 强制结论行 + 有界修后重审 | 已落地 | `#313` 首审 `b17830c`、修后重审 `02daddd`；`#312` 台账行 `f25be96` |
| O5 | **落点纪律**：tracker / `PHASE_STATUS` 只写操作性事实 + 指针，机制叙述只在一处写全 | 已落地（依据 `AGENTS.md` §16.1） | `#312` 的 tracker 段（不放机制复述，改放 commit/闸门/指针） |
| O6 | ~~本地 commit 包装脚本~~ | **未做（用户不同意）** | 见 §5 |
| O7 | ~~并行 Agent 分担在途票~~ | **未做（用户不同意）** | 见 §5 |
| O8 | **下一票开工前先把 `main` 合回自己的分支**（§14.9 集成后回补，强制第一步） | 已落地 | commit `ee0251e`（`origin/main` `0d03187` → T5 分支） |

---

## 2. 三份可复用模板（真正省时间的东西）

### 2.1 票面简报（O2）——`docs/tickets/T<ID>-<slug>-brief.md`

**它解决什么问题**：每票开工前都要重读规格 + 重新 grep 一遍符号，两小时里有一小时在重建上下文；
而且这些重建结论**下一票的人还得再重建一次**。

固定五段（实例见 `docs/tickets/T313-model-request-budget-brief.md`）：

1. **契约在哪**：表格「内容 / 规格位置 / 读它干什么」——只给指针，**不复制规格正文**（§16.1）；
2. **本票的设计决策**：前置结论写死并编号（D1…Dn），附「为什么不是另一种做法」；
3. **AC → 落点 → 判据**：一行一条 AC，写清「改哪个文件 + 用什么测」；
4. **已踩过的坑**：每条都写「症状 + 原因 + 怎么躲」；
5. **交付清单**：与协议 §7/§8 的收尾顺序对齐。

两条使用纪律：

- **声明「行号会烂」**：文件里的 `file:line` 是当日实测值，动手前用符号名 `grep` 复核，别照抄行号下刀；
- **它随分支走**（进同一条 commit 序列），不写成 issue 评论——issue 会被后续讨论冲散，文件可 diff。

### 2.2 审查派单（O4）——派给独立审查子代理的固定形状

**它解决什么问题**：开放式「请审查这个分支」会得到开放式结论 —— 审查者去读整个仓库、报一堆
票外问题、你还要再问一轮「这条到底影响哪条 AC」。固定形状后**一次派清**：

1. **固定输入**：被审的**具体 commit 列表**（不是「这个分支」）+ 那份 AC→落点表 + diff 范围；
2. **两轴分开**：Standards（工程规范 / 可读性 / 一致性）∥ Correctness-Spec（对规格与 AC 的符合性），
   两个独立子代理，**同一 commit** 各自出结论；
3. **派发预算**：写明「最多读哪些文件、允许看多久、只报影响本票 AC 或信任边界的问题」，超范围只列不追；
4. **强制结论行**：每条 finding 必须给出 `P0/P1/P2/P3 + 文件:行 + 复现方式 + 影响哪条 AC + 建议动作`，
   没有复现方式的 finding 不接受（避免「感觉不太好」）；
5. **处置要么修、要么写下不修的理由**（写进台账行），不留「已阅」；
6. **修后重审是**有界的**：只复审「被改动的面 + 与它相邻的判据」，不重跑整轮两轴。

### 2.3 落账（O5）——tracker 段 + 台账行 + 归档明细

- **tracker 段**只写操作性事实：commit / 闸门读数 / 证据路径 / 残余项 + 指针，**不复制机制叙述**；
- **台账行**（`docs/review_ledger.tsv` / `docs/review_ledger.d/*.tsv`）一行一条审查，覆盖闸门
  `scripts/check_review_coverage.py` 的输入就是它；
- **当月归档**（`docs/phase_status/<年-月>.md`）放逐条明细，`PHASE_STATUS.md` 只留索引一行 + 行号指针。

**为什么这条能提速**：收尾不再需要「回忆 + 反查 + 重写」，因为施工过程中每一段事实都已经落在了
它唯一的归属文件里；收尾只剩「把数字填进索引」。

---

## 3. 实测：时间究竟花在哪里（数字全部来自入库证据，不是估计）

`docs/live_gate/<时间戳>-<sha>-<场景>/evidence.json` 里每次尝试都记了 `duration_ms`。
真实读数（PASS 的那几次）：

| 场景 | 尝试数 | 单次耗时（秒） | 一次 invocation 合计 |
| --- | --- | --- | --- |
| `smoke-production-tools` | 3 | 13 / 16 / 12 | ~41 s |
| `long-task-past-legacy-turn-limit` | 3 | 34 / 45 / 40 | ~119 s |
| `budget-pause-resume-same-run` | 3 | 137 / 76 / 97 | ~310 s |

两条结论：

1. **真实模型跑一次 PASS 矩阵是分钟级**（最贵的场景一次 5 分钟），`#319` 要五类场景各 3/3 ⇒
   单这一项就是**半小时以上的真实模型时间**，且必须串行（证据目录要干净提交、模型有速率约束）。
   这部分只能靠「先跑证据 → 立刻提交 → 再跑 Gate-0」的顺序纪律省掉**返工**，不能靠并行省掉时间。
2. **注入失败尝试几乎不花时间**：`--inject-failure attempt:2` 那几次的耗时是 `[6, 0, 8]` / `[9, 0, 22]`——
   中间那次 0 秒是因为 runner **合成**了失败、没有真的调场景。这与另一种注入的取舍要点见 §4.3。

项目已记录的全量 pytest 墙钟约 **20 分钟**（`AGENTS.md` §13.4 的既有记载），前端另有
`vitest` / `npm run build` / `oxlint` 三条车道。**四条重车道用后台并发跑 + 轮询进度**，
把 20 分钟的重叠成一次，是这轮里实际省下来的最大一块。

---

## 4. 事故与工具面换来的硬规则（照做，别重新发现）

### 4.1 一个 Agent 一个工作目录；绝不 `git add -A`；绝不在共用目录 `reset --hard`

本轮真实事故：另一路操作在**同一个工作树**里执行了 `git add -A` + 若干提交 + `git reset --hard 02daddd`，
一次性卷走了**未跟踪**的临时文件与 `.zcodeignore`，并把本 clone 的本地 `main` 分支删掉了。
未跟踪文件在硬重置下**没有回收站**。因此：

- 每个 Agent 用**自己的**目录 / worktree（`AGENTS.md` §13.2 已有此条，本轮是它的代价证明）；
- 提交 staged 集合**逐文件显式**列出，不用 `-A` / `-u`；
- 任何外部动作之后，核对三件事：**分支 tip 是否还在**、**工作树里未跟踪的证据是否还在**、
  `.zcodeignore` 的 blob 是否仍等于 `39371154b6f24e83d4f2d3d90e254466acdf7b3c`。

### 4.2 证据**先提交**，再跑 Gate-0

Live Gate 证据目录（`docs/live_gate/**`）以 JSON/JSONL 入库；它们在**未跟踪**状态下会让
`scripts/gate0.py` fail-closed（车道跑在工作树上，读数只能记 `HEAD` 的树）。
顺序固定：**跑 Live Gate → 提交证据 → 才跑 Gate-0**。这是 §4.1 的另一面：
未提交的证据既可能被硬重置卷走，也会让门禁拒绝落盘。

### 4.3 「受控失败」的证据面：走真实生产路径，且永不等于 PASS

需要「primary 失败 → fallback 接住」这种证据时，不要用「跳过场景、直接把这次尝试记 FAIL」的注入
（那只能证明 runner 会记 FAIL，证明不了产品会切换）。可行做法是**让场景自己消费注入标记**：

- runner 保留 `attempt:<n>` 语法（合成失败、不调场景）；
- **其它非空值**原样交给场景（`ScenarioContext.injected_failure`），场景据此只改**配置**
  （例：把 primary 的 `base_url` 指到 RFC 2606 的 `.invalid` 域 ⇒ 连接失败 ⇒ 属**瞬时**错误 ⇒
  真实配置的 fallback 接管），`provider` / `model` 标识仍是部署值；
- 这类证据在 schema 层**上限就是 FAIL**（`seams` 或 `injected_failure` 非空 ⇒ 不得 PASS），
  所以它**永远不能**混进 3/3 的过关样本里，只能当**附加证据面**。

顺带一条**分类**陷阱：404（模型名不存在）**不是**瞬时错误，不会触发 fallback；
要触发得用连接失败 / 超时 / 5xx / 429。

### 4.4 取证缺陷 ≠ 产品缺陷（O3 的由来）

Live Gate 场景如果**在执行侧收尾之前**读盘，会得到一份「写盘 N 行、轨迹实为 N+1 行」的证据，
validator 把它判 FAIL —— **看起来像产品 bug 的假 FAIL**。
修法不是放宽 validator，而是在场景里加**静止自证**：「独立复读一次，逐条同序才算读稳」。
凡是「失败了一次」先分辨它属于**取证/时序**还是**产品语义**，归因写进台账。

### 4.5 Windows 工具面（这一轮的实测）

- 中文大文件**只用** `Read`（含 `offset`/`limit`）与 `Grep`；`tail` / `sed` / `cat` / `head` 在
  GBK 控制台下会显示成乱码（本仓库历史上因此差点拿乱码当编辑锚点）；
- **不要** `| head` 收尾管道（提前退出会触发上游 SIGPIPE，行为随 `pipefail` 反转）；
- 前台命令约 120 s 被 SIGTERM ⇒ 长跑用 `nohup` + 进度文件轮询；
- Python 用项目 venv（`.venv/Scripts/python.exe`），系统解释器没有 pytest；
- 全量 pytest 走 `scripts/run_tests_clean.sh tests/`（它会清 `PYTHONPATH` 绕开安全删除 shim）；
- 变异 / 红证只能在**主工作树之外的副本**里做，且副本里必须显式
  `PYTHONPATH=<副本>/src`（`.pth` 指向主仓 `src`，不设会出现「测试生效、`src` 静默无效」的假绿）；
  改副本前先探行尾（`core.autocrlf=true` ⇒ 副本内是 CRLF），`.replace()` 必须断言命中数 `== 1`。

### 4.6 前端车道的必跑项：`npm run build`

`vitest` **不做类型检查**。本轮 `vitest` 全绿的同时，`npm run build` 用 4 条 TypeScript 错误
（事件语义表缺新键、可空字段窄化、两个测试夹具的空值形状）说明了这一点。
前端重车道固定为 **`vitest` ∥ `npm run build` ∥ `oxlint`** 三条，缺一不可。

### 4.7 生成物不许手改

`web/src/generated/event-types.ts`（`scripts/gen_event_types.py`）与 `docs/EVENT_VOCABULARY.md`
（`scripts/gen_event_vocabulary.py`）是生成物，`gate0.py` 有同步守卫，`tests/test_event_vocabulary_generated.py`
也会红。改源 → 重新生成 → 连生成物一起提交。

### 4.8 读数只引用机器落盘

`docs/gate/<sha>.json`（`scripts/gate0.py` 每次裸全量写出）是**六条机械车道**的唯一读数来源；
重车道（pytest / vitest / build / e2e / live）的读数必须来自**可复跑的命令**，并把命令与树写进落点记录。
**「人手抄数字」这条路径已被删除**——历史上正是因为抄错一格而静默豁免了一票。

---

## 5. 明确**没做**、也**不建议**再提的两项

| 编号 | 提案 | 为什么不做 |
| --- | --- | --- |
| O6 | 本地 commit 包装脚本（自动 stage + 规范 message） | 用户明确不同意。真实收益很小（本项目提交频率不高），而它会把「哪些文件进本次提交」从**显式列表**变成脚本推断——§4.1 的事故正是 stage 集合失控的代价 |
| O7 | 拉第二个 Agent 并行分担在途票 | 用户明确不同意，且技术上不成立：`#314`–`#320` 是**同一条文件流水线**（`run_budget.py` / `runtime.py` / `session/service.py` / `web/app.py` / `cli.py` / `runBudget.ts` / `PausedPanel.tsx` / golden），并行只会撞同一批文件。**要提速只能按角色分**（实现 ∥ 独立审查），而审查本身按协议本来就必须独立 |

红线（用户原话）：**「不要为了提速去削 Live Gate 3/3 或两轴审查。」**
任何「因为慢所以把 3/3 改成 1/1」「因为赶所以省掉一轮复审」的提议都不要再提。

---

## 6. 给另一条线的可抄清单

**开工前**
1. `git merge-base --is-ancestor main HEAD` —— 非 0 就是落后，先把 `main` 合回来再动手（O8）；
2. 写票面简报（§2.1），把 AC→落点表填满；行号一律标注「当日实测，动手前 grep 复核」；
3. 确认重车道与 Live Gate 的**顺序**：实现 → 测试 → 红证（副本）→ 两轴审查 → 门禁 → Live Gate → 提交证据 → Gate-0。

**施工中**
4. 一次只改本票 AC 覆盖的文件（`AGENTS.md` §8 Scope Lock）；发现票外问题只报告；
5. 有「受控失败」需求时按 §4.3 走场景消费型注入，不用 runner 跳过；
6. 每完成一个可验证单元就提交（未提交的成果会被硬重置卷走，§4.1）。

**收尾**
7. 两轴审查（同一 commit、两轴独立、带派发预算）→ 处置（修或写明不修）→ **有界**修后重审；
8. 四条重车道并发跑，读数写进落点记录并写明命令与树；
9. 台账行 + tracker 段（操作性事实 + 指针）+ `PHASE_STATUS` 索引 + 当月归档；
10. `python scripts/check_review_coverage.py` exit 0 → 比 `HEAD^{tree}` → 合 `main` → push → 通知另一条线回补。

---

## 7. 当前状态指针（不复制事实，只给入口）

| 想知道什么 | 去哪看 |
| --- | --- |
| 当前在途 ticket / 门禁读数 / 残余问题 | `docs/SDD_TICKET_TRACKER.md` |
| Phase 进度与当前焦点 | `docs/PHASE_STATUS.md`（明细在 `docs/phase_status/<年-月>.md`） |
| 本票的 AC→落点映射与坑 | `docs/tickets/T313-model-request-budget-brief.md` |
| 运行预算链的代码地图 | `docs/agents/runtime_budget_code_map.md` |
| Live Gate 真实证据（含每次尝试耗时） | `docs/live_gate/<时间戳>-<sha>-<场景>/evidence.json` |
| 六条机械车道的机器读数 | `docs/gate/<sha>.json` |
| 流程权威 | `docs/SDD_WORKFLOW_PROTOCOL.md`（V3.1-lite）+ `AGENTS.md` §13/§14 |
