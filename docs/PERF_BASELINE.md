# PERF_BASELINE — 性能基线台账

> 建立日期：2026-09-18（随 GitHub #267 批次建立）
> 用途：**性能票的前置基线与改造后对照数字的唯一落点**。
> 关联批次索引：`docs/tickets/perf-interaction-smoothness-2026-09-18.md`（父票 #267 / 子票 #268–#281）

---

## 0. 这个文件**不是**什么（避免污染既有台账）

- **不是** `docs/PHASE_STATUS.md` 的替代品。那份是 **Phase 进度表**（Phase 状态 + commit + Gate 证据），
  性能数字混进去即污染。**性能数字只落这里。**
- **不是** issue / ticket 的执行索引。索引在 `docs/tickets/perf-interaction-smoothness-2026-09-18.md`。
- **不是** ADR。语义决策（例如 `eventsVersion` 的语义）落 `docs/adr/`。

## 1. 硬规则

1. **G3 基线硬前置**：性能票**没落可复核基线数字之前不许动代码**。
   基线必须能在别人机器上按同样的命令复现。
2. **追加式**：本文件按票分节，**只允许在对应小节内追加**。
   **不得**改写、删除、重排他人已落的数字（历史数字是证据，不是待修正的错误）。
   若发现前人数字有误，**追加一条更正**并写明为什么，保留原行。
3. **每条数字必须可复核**：附「怎么测的」——命令、脚本路径、场景描述、数据规模。
   只有数字没有口径 = 无效。
4. **判定为「无需处理」也必须留数字**。本批明确**不允许无数字的 wontfix**。

## 2. 测量口径（G4，与批次决策一致）

### 2.1 观感类（前端）

- 工具：Chrome DevTools **Performance** 面板录制。
- 取数：**long task 数** + **最长单帧**（不是 FPS 均值）。
  理由：用户能感知的是「卡一下」，那是长帧，不是平均帧率。
- 场景固定：**同窗口尺寸 + 同会话内容 + 同一段操作序列**（打开 → 流式 → 交互 → 关闭）。
  前后对照**必须**是同一条操作序列。
- 录制结果（`.json` trace 或视频）作为**交付物存档**，路径写进该票 DoD。
- **不引入** Playwright 帧率自动采集（本项目 E2E 有收尾挂死历史，成本 > 收益）。

### 2.2 可自动化类（前端）

- 用 spy / 桩在 **vitest** 里计数（调用次数、节点数、节点查询数），
  或断言「同一输入放大 N 倍后耗时比 < 常数」的线性形态。
- 计数类断言比耗时断言更稳（CI 上耗时抖动大），**优先用计数**。

### 2.3 可自动化类（后端）

- 量「**事件循环上的最长单次同步占用**」，**不是**平均值。
  项目已有 `scripts/measure_sse_streaming.py` 可按此口径扩展。
- 也可用 `time.perf_counter` 在测试里量单次调用；但必须**同时记调用次数**
  （因为本批多处优化的是「次数」而不是「单次耗时」）。
- pytest 结果以 `--junitxml` 为准（本环境 stdout 可能被截断）。

## 3. 记录模板

每票一节，每行一条记录。示例：

```
### F1 — 稳定 disclosure 引用（#270）

| 场景 | 规模 | 指标 | 改造前 | 改造后 | 口径 / 命令 | 日期 | commit |
|---|---|---|---|---|---|---|---|
| 流式追加 delta，已完成段重解析 | 3 段 × 2k 字 | `renderMarkdown` 调用次数 / 24ms 提交 | N（= 可见已完成段数） | 0 | `npx vitest run src/components/Conversation.test.tsx` | 2026-09-18 | <sha> |
| 同上 | 同上 | long task 数 / 最长单帧 | … | … | Chrome Performance，录屏存档：`<路径>` | 2026-09-18 | <sha> |
```

**必填列**：场景、规模、指标、改造前、改造后、口径/命令、日期、commit。
**允许留空**的情形：改造后列在该票开工前为空——**但基线列必须有数字**。

---

## 4. 各票基线（按 #268–#281 顺序；开工时在对应小节追加）

### 勘误 #268 / ADR-0037 #269
> 无性能数字要求（docs-only）。**不在此文件记账。**

### F1 — 稳定 `disclosure` 引用（#270）

**基线（改造前，2026-09-18）**——全部为实测，非估算：

| 场景 | 规模 | 指标 | 改造前 | 改造后 | 口径 / 命令 | 日期 | commit |
|---|---|---|---|---|---|---|---|
| 流式提交一次（已完成段已渲染） | 1 个可见已完成段 | `renderMarkdown` 调用次数 / 每次父级提交 | **1**（= 可见已完成段数，与提交同频） | **0**（内容未变即命中，零解析） | `node node_modules/vitest/vitest.mjs run src/components/Conversation.render.test.tsx` | 2026-09-18 | bcdf4e4 |
| 同上，挂载即计数 | 1 个已完成轮 | `renderMarkdown` 调用次数 / 挂载（含 sessionKey effect 那一拍） | **2** | **1** | 同上 | 2026-09-18 | bcdf4e4 |
| 父级无关状态变化 | 1 个已完成轮 | `memo(TurnView)` render 次数 / 挂载 | **2**（memo 恒 miss） | **1** | 同上（以轮次时间戳计 `formatDuration` 调用） | 2026-09-18 | bcdf4e4 |
| 只改与工具卡无关的 prop | 1 个工具轮 | `memo(ToolCard)` render 次数 / 挂载 | **2**（`cycle` 闭包每次新建） | **1** | 同上（以工具时间戳计 `formatDuration` 调用） | 2026-09-18 | bcdf4e4 |
| 单次解析成本 | 2k 字长回答 | `renderMarkdown` 单次耗时中位数（n=200） | **0.692 ms**（p95 3.591 ms） | 同（单价不变，变的是调用次数） | 见下方「成本探针」命令 | 2026-09-18 | bcdf4e4 |
| 一次提交的被浪费工作 | 3 个可见已完成段 | 一轮全量重解析耗时中位数（n=200） | **1.320 ms**（p95 2.614 ms） | **0**（内容未变即命中，零解析） | 同下 | 2026-09-18 | bcdf4e4 |
| 折算每秒 | 合帧窗口 24ms ⇒ 40 次提交/秒 | 单核占用（40 × 1.320 ms） | **≈53 ms/s（≈5.3%）**，且随段数/长度线性增长 | ≈0 | 同上 | 2026-09-18 | bcdf4e4 |
| `deriveChain` 单次成本 | 3 model 段 + 2 工具 | 单次耗时中位数（n=200） | **0.002 ms** | 同 | 同下 | 2026-09-18 | bcdf4e4 |
| Chrome Performance（观感口径，G4） | 长回答流式 | long task 数 + 最长单帧 | **未取得** | **未取得** | 见「未闭合」 | 2026-09-18 | — |

**改造后数字的取证方式（可复核）**：上表前 4 行的「改造后」不是估算，是
`web/src/lib/disclosure.test.tsx` + `web/src/components/Conversation.render.test.tsx` 里
**直接断言掉的计数**（计数用的是 `formatDuration` / `renderMarkdown` 的 spy，见该文件头部注释）；
命令与上表「口径 / 命令」列一致，全绿即等于这几个数字成立。

**改造前的红证（同一条命令、同一批用例）**：把三个源文件临时换回 `HEAD` 版本
（`git show HEAD:<path>`，**禁用 `git stash`**）后重跑，14 条里 **6 条失败**：
`disclosure.test.tsx` R1 ×2（`expected 3 to be 2`）、AC3（`expected 2 to be 1`）、
AC4（`expected 2 to be 1`）、AC5 ×2（`expected "vi.fn()" to be called 1 times, but got 2 times`）。
改造后 14/14 全绿 —— 因果闭合。

**顺带记录（同一文件、非本票收益来源）**：`web/src/lib/disclosure.ts` 的
`react(set-state-in-effect)` 告警由 **2 → 0**（清空 override 的 effect 加了「挂载期跳过」守卫
后不再是无条件同步 setState）；全仓 oxlint 告警总数 **44 → 42**，**无新增**。


**成本探针（基线复现脚本，已随本票入库）**：`web/src/lib/f1-cost-probe.perf.test.ts`
（与仓内既有的 `projection.perf.test.ts` / `streaming.perf.test.ts` 同属 perf 车道；
`vitest.config.ts` 已排除 `*.perf.test.ts`，不进默认 `npm test`）：

```bash
cd web && node node_modules/vitest/vitest.mjs run -c vitest.perf.config.ts src/lib/f1-cost-probe.perf.test.ts
```

> **复跑波动（2026-09-18 当日第二次运行，追加记录，不改上表原值）**：`renderMarkdown(2k)`
> 中位 **0.752 ms**（p95 3.958）、三段合计 **1.484 ms**（p95 3.264）。与上表 0.692 / 1.320
> 同量级、差异属机器抖动——**结论不受影响**：本票的收益是「调用次数从 N 降到 0」，
> 单价量级只用于说明「浪费确实有成本」，不作为阈值。

> **注意别把功劳记错**：`deriveChain` 只有 0.002 ms，**本票真正的浪费全在 markdown 重解析**
> （0.692 ms/次）。`deriveChain` 那一行是「顺手测了一下、结论是可忽略」，不是收益来源。

**未闭合（G4 的观感口径）**：Chrome Performance 的 long task 数 / 最长单帧**本次未取得**。
⚠ **2026-09-18 更正理由**：原文写「本执行环境无 GUI 浏览器、且无真后端可驱动一场真实流式」——
**不属实**，真机 Chromium 一直可用（`web/e2e` 主车道用真机 Chromium，2026-09-18 实测 436 绿）。
真正缺的是**该口径的采集手段**：`PERF_BASELINE §2.1` 明令**不引入** Playwright 帧率自动采集，
而人工录制需要一个可判读的 trace 与固定场景。**解除条件**（二选一）：① 写一个 CDP
`PerformanceObserver('longtask')` 采集脚本（这是**可自动化**的，本批未做）；② 人工在 Performance
面板按固定场景录一次（同窗口尺寸 + 同一段长回答 + 同一操作序列：打开 → 流式 → 折叠/展开工具卡 →
切 density），把 trace 存档并把两列数字补进上表。在此之前，本票的收益证据只覆盖
「可自动化口径」（调用次数 / 次数 × 单价）。

<!-- PERF-LIVE-G4-CORRECTION-2026-09-18 -->
> **2026-09-18 追加更正（当日晚些时候，采集手段已补）**：上面解除条件的 ① **已完成** ——
> `web/scripts/perf-longtask-live.mjs` 用浏览器原生 `PerformanceObserver('longtask')`
> （阈值 50ms，与 Chrome Performance 面板同源判定）做采集，**可自动化、不需要 GUI 录制**。
> 但它**没能**补上本行的「长回答流式」场景：2026-09-18 真机联调时模型供应商账户被冻结，
> `api.senseaudio.cn` 对 `deepseek-v4-flash-0731` 返回
> `HTTP 400 {"code":"billing","message":"计费账户已被冻结","ref_code":400901}`，
> 任何依赖真模型往返的场景当前都跑不出流式（harness 侧表现为 `run/failed
> reason=provider_account_unavailable`）。该脚本覆盖的场景是**打开长会话 → 12 次 Inspector
> peek 切换 → 工作区面板往返**，即 F2（#272）的交互路径，数字见 F2 节
> 「真机 live 车道（2026-09-18）」。
> ⇒ **本行仍标记为「未取得」**；解除条件更新为：账户解冻后，用同一脚本指向一个流式会话采集，
> 或按 §2.1 人工在 Performance 面板按固定场景录一次并归档 trace。


要求指标：
- 「流式提交一次」时 `renderMarkdown` 的实际调用次数（改造前应按可见已完成段数增长）；
- `memo(TurnView)` / `memo(ToolCard)` 的 render 次数（render 计数 spy）；
- Chrome Performance：long task 数 + 最长单帧（长回答场景，录制存档）。

### N2 — `eventsVersion`（#271）

**基线（改造前，2026-09-18）**——全部为实测，非估算：

| 场景 | 规模 | 指标 | 改造前 | 改造后 | 口径 / 命令 | 日期 | commit |
|---|---|---|---|---|---|---|---|
| Timeline run 分组是否跟随 `events` 追加更新 | 1→2 个 run（4→6 事件） | 组头数 / 行数（**同一实例**再渲染后） | **1 / 4**（陈旧——派生值停在首帧，第 2 个 run 不出现） | 2 / 6 | `node node_modules/vitest/vitest.mjs run src/components/StepDetail.render.test.tsx` | 2026-09-18 | `40851f8` |
| 同上：已存在组的计数也陈旧 | 同 run 追加 1 事件（4→5） | 组头文案 | **「Run 1 已完成 4 事件」**（不涨） | 「5 事件」 | 同上 | 2026-09-18 | `40851f8` |
| `eventsVersion` 语义（AC1–AC5 + 引用稳定守卫） | 6 帧（含 1 重复 seq + 1 quarantine） | 断言通过数 | **0 / 6 通过**（字段不存在，6 条全红） | 6 / 6 | `node node_modules/vitest/vitest.mjs run src/lib/projection.test.ts` | 2026-09-18 | `40851f8` |
| `applyEvent` 单事件成本（**不得**回退 P0-1） | @1k / @5k / @20k | µs/事件 | **0.5 / 0.2 / 0.2** | 见下 | `node node_modules/vitest/vitest.mjs run -c vitest.perf.config.ts src/lib/projection.perf.test.ts` | 2026-09-18 | `40851f8` |
| `projectHistory` 历史重建（**不得**回退 P0-1） | 4650 / 20000 事件 | ms | **4.3 / 10.7** | 见下 | 同上 | 2026-09-18 | `40851f8` |

**红证（改造前，同一条命令的失败输出）**：`Tests 8 failed | 193 passed (201)`
——失败的正是本票新增的 8 条（投影 6 + 组件 2），**既有 193 条全部通过**（含
`applyEvent — 引用稳定性（流式渲染 memo 契约）` 的 7 条）。关键失败行：

```
AssertionError: expected [ 'Run 1已完成4 事件' ] to have a length of 2 but got 1
AssertionError: expected 'Run 1已完成4 事件' to contain '5 事件'
AssertionError: expected undefined to be +0      // AC1
AssertionError: expected undefined to be 1       // AC2 / AC3 / AC4
AssertionError: expected undefined to be 5       // AC5
TypeError: actual value must be number or bigint, received "undefined"  // 引用稳定守卫
```

> 「组头数 1、行数 4」这一条是**红证的主体**：`useMemo(..., [conversation.events])` 的
> 依赖是 P0-1 刻意固定的共享数组引用，父级提交后比较恒等 ⇒ 派生值停在首帧。
> 用例里用 `expect(second.events).toBe(first.events)` 同时钉住「新 state 对象 + 同一数组引用」
> 这一对条件，否则用例会退化成「测了一个不存在的场景」。

**改造前后对照（同上口径）**：见本小节末尾「改造后」。

要求指标：
- **正确性数字**：`StepDetail` 的 run 分组在「追加事件后」是否更新（二值）；
- 投影层：`projectHistory` 4650 事件、`applyEvent` @20k 事件的耗时
  （**必须保持** `3344e34` 的水平：`<10µs/事件` / `2.8ms`——本票**不得**回退这两个数）；
- 新增字段后 `applyEvent` 的耗时增量（应可忽略）。

---

**改造后（2026-09-18，同口径，追加记录——上表原值一字未改）**：

| 场景 | 规模 | 指标 | 改造前 → 改造后 | 口径 / 命令 |
|---|---|---|---|---|
| Timeline run 分组跟随追加更新 | 1→2 个 run（4→6 事件） | 组头数 / 行数 | **1 / 4 → 2 / 6** ✔ | `node node_modules/vitest/vitest.mjs run src/components/StepDetail.render.test.tsx` |
| 同 run 追加 1 事件（组内计数） | 4→5 事件 | 组头文案 | **「4 事件」→「5 事件」**、行 4→5 ✔ | 同上 |
| `eventsVersion` 语义（AC1–AC5 + 引用稳定守卫） | 6 帧（含 1 重复 seq + 1 quarantine） | 断言通过数 | **0 / 6 → 6 / 6** ✔ | `node node_modules/vitest/vitest.mjs run src/lib/projection.test.ts` |
| `applyEvent` 单事件成本（不得回退 P0-1） | @100 / @1k / @5k / @20k | µs/事件 | 0.4/0.5/0.2/0.2 → **0.7/0.3/0.1/0.2**（同车道；未回退） | `node node_modules/vitest/vitest.mjs run -c vitest.perf.config.ts src/lib/projection.perf.test.ts` |
| `projectHistory` 历史重建（不得回退 P0-1） | 4650 / 20000 事件 | ms | 4.3/10.7 → **3.4/12.5**（同量级，机器抖动内） | 同上 |
| **新增字段的耗时增量**（A/B，各 3 轮取中位） | @20k **真实 push 路径**（`seq: null`，不去重） | µs/事件 | 改造前 **0.696** → 改造后 **0.678** ⇒ **增量落在抖动内** | 见下「N2 成本探针」 |
| **本票唯一被改变的运行时行为**：`groupEventsByRun` 调用频率 | 从「永不重算」变成「每次 `events.push`」 | 单次成本 / 折算 | @20k 单次 **0.145 → 0.147 ms**（单次成本不变）⇒ 合帧 40 次/秒 ≈ **5.9 ms/s（≈0.6% 单核）** | 同上 |

**N2 成本探针（基线复现脚本，随本票入库）**：`web/src/lib/n2-cost-probe.perf.test.ts`
（与 `f1-cost-probe.perf.test.ts` 同属 perf 车道，不进默认 `npm test`）：

```bash
cd web && node node_modules/vitest/vitest.mjs run -c vitest.perf.config.ts src/lib/n2-cost-probe.perf.test.ts
```

> ⚠ **顺带查明的一个度量陷阱（不改动他人行，仅追加说明）**：`projection.perf.test.ts` 的
> `applyEvent @N` 基准把**同一个 `seq: 999999` 反复投递**——`seenSeqs` 是跨 state 共享的
> Set（append-only 簿记，ADR-0016 §2），第二轮起该 seq 已在集合里，于是每次都走
> **去重短路 `return state`**，测到的是短路路径（≈0.2µs），**不是真实事件要走的 push
> 路径**。实测对照：真实 push 路径 @20k ≈ **0.68µs**（本票探针，A/B 各 3 轮）。
> 影响面（**不在本票范围，仅登记**）：
> 1. `3344e34` 引用的「0.2µs/事件」实际是短路路径读数——P0-1 的收益方向不变（它消灭的是
>    `[...state.events, event]` 的整体克隆，那条路径在两个口径下都变快了），但**倍数**若要
>    对外引用，应以 push 路径重测为准；
> 2. 该文件的「O(N²) 回潮探测器」用的也是同一口径 ⇒ 它对 **push 路径**的回退是**瞎的**。
>    **解除条件**：把基准里的 delta 换成每次新 seq（一行改动）后重测，或由本票的
>    `n2-cost-probe` 承担 push 路径的预算断言（后者已带 `<50µs` 断言，**已覆盖**）。

**门禁（同一 commit 树，全绿）**：`tsc -b` 0 错误；`oxlint` **42 → 42**
（零新增，且**零顺带消失**——见下方说明）；`vitest` **60 文件 / 990 用例全绿**
（F1 时 59 / 982）；`vite build` 通过。

> **oxlint 零新增的实现方式（重要，供后续票复用）**：三处 `useMemo` 用 `eventsVersion`
> 作键会被 `exhaustive-deps` 判为「缺依赖 `conversation.events`」+「多余依赖 `eventsVersion`」
> （共 6 条）。豁免**只能用行内 `// eslint-disable-line react-hooks/exhaustive-deps`**：
> 本版 oxlint（1.79.0）下 `disable-next-line` 与块级 `disable`/`enable` 会让该函数**全部
> compiler 类规则一起跳过**——最小复现：同一份代码无豁免时 4 条告警（2×`react(refs)` +
> 2×`exhaustive-deps`），加 `disable-next-line` 后变成「无告警」，连无关真告警一起吞掉。
> 行内形式实测只吞目标告警：`oxlint` 总数 42 → 42，`StepDetail.tsx` 里那条既有的
> `react(refs)`（`visibleRef.current = visible`）**原样保留**。

### F2 — memo 与 props 收敛（#272）

**一句话**：本票的收益是「**提交频次 × 组件规模**」的一次性下降（与对话无关的提交、以及
「只动事件日志」的提交不再触发任何派生），**不是**单帧成本的下降。唯一需要真机录制的
门槛数字本机不可得，已如实登记（见文末）。

| 指标（票面要求） | 场景 | 口径 | 改造前 → 改造后 | 复核命令 |
| --- | --- | --- | --- | --- |
| Inspector **关闭**状态下追加 delta 时的派生调用次数 | 面板关闭后 5 次**与对话无关**的父级提交 | `allTools` / `deriveRunPulse` / `deriveAgentProfile` 各自 spy 调用数 | **5 / 5 / 5 → 0 / 0 / 0** | `node node_modules/vitest/vitest.mjs run src/components/StepDetail.memo.test.tsx` |
| `Conversation` 的 render 次数（父级无关状态变化） | 同上（6 次无关提交） | 函数组件体执行次数（虚拟化 hook 计数） | **6 → 0** | `node node_modules/vitest/vitest.mjs run src/components/Conversation.memo.test.tsx` |
| 追加**不改轮次**的事件（`SESSION_RESUMED`） | 追加 1 条 | spy 计数增量 | `allTools` **+1 → 0**、`deriveRunPulse` **+1 → 0**、`deriveAgentProfile` **+1 → +1（契约要求，非回归）** | 见下方「第三行为什么 +1」 |
| 键盘导航表随事件变化重建 | 事件 5 条 → 6 条后按 `↓` | 能否走到新增条目（peek 显示的 `event.type`） | **不能（原地不动，停在 `run/completed`）→ 能（`session/resumed`）** | 同上；变异检验见下 |
| long task 数 + 最长单帧（Inspector 打开且长 run，录制存档） | — | Chrome Performance | **未取得** | 见文末「未闭合项」 |

> **第三行为什么 `deriveAgentProfile` 仍然 +1**：票面 AC3 原文要求「Inspector 关闭态追加
> delta 时 `allTools` / `deriveAgentProfile` 调用次数**都不增长**」，这与 N2 的 `eventsVersion`
> 契约正面冲突——任何**真正追加成功**的事件都让 `eventsVersion` +1（`projection.ts:1171/1190`；
> 唯一不增的路径是去重短路 `return state`，它返回**同一个 state 对象**），而本票必做 2 又
> **指定** `deriveAgentProfile` 的键必须是 `eventsVersion`。两条不可能同时成立。
> 已交用户 2026-09-18 裁定，按「**收窄 AC3 + 补无关提交用例**」执行：断言**如实写 +1**，
> 不假装它没涨；收窄后成立的两条 = 上表第 1、2 行（无关提交零重算）与第 3 行的前两项
> （不改轮次的事件下 `tools` / `pulse` 零重算）。

**红证（改造前 → 改造后，同一条命令、同一批用例）**

```bash
cd web && node node_modules/vitest/vitest.mjs run src/components/Conversation.memo.test.tsx src/components/StepDetail.memo.test.tsx
```

做法：两个源文件临时换回 N2 tip `79f7f26`（`git show <sha>:<path>` 写回，**全程未用 `git stash`**），
跑完按字节还原并 sha256 对账（两文件 `same=True`）。

| 阶段 | 文件级 | 合计 |
| --- | --- | --- |
| 改造前（两个源文件 = `79f7f26`，新用例保留） | `Conversation.memo.test.tsx` **7 tests / 2 failed**；`StepDetail.memo.test.tsx` **11 tests / 5 failed** | **7 failed / 11 passed (18)** |
| 改造后（本票工作树） | 同两文件 | **18 passed (18)** |

关键失败断言（改造前）：`expected undefined to be Symbol(react.memo)`（AC1，两个组件各一条）、
`expected 6 to be 1`（Conversation 在 6 次无关提交下的 render 次数）、
`expected { all: 2, pulse: 2, profile: 2 } to deeply equal { all: 1, pulse: 1, profile: 1 }`
（AC3(a) / AC2 / 两条反例守卫）。

> **改造前就通过的那 11 条不计入红证**：含 AC6 两条与「输入变了必须重算」三条——它们是
> **不变式守卫**而不是新行为，改造前天然成立（改造前导航表本来就每次提交重建；没有 memo
> 当然也不会漏重算）。红证只认「改造前必红」的那 7 条。

**变异检验（钉住导航表的 `eventsVersion` 依赖确实是可载荷的）**：把 `listTargets` 依赖数组里的
`conversation?.eventsVersion` 摘掉（只此一处、只此一项），AC6 第二条立刻转红：

```
× 追加事件后 ↓ 能走到新增的那一条（陈旧导航表会原地不动、连回调都不发）
AssertionError: expected 'run/completed' to be 'session/resumed'
Tests  1 failed | 10 passed (11)
```

`StepDetail.tsx` sha256：变异前 `da9972e4d83c4b21…` → 变异后 `084ce979e4f12744…` → 还原后
**逐字节相同**；还原后复跑同一文件 **11/11 绿**。

**门禁（同一 commit 树，全绿）**：`tsc -b` rc=0（输出 0 字节）；`oxlint` **42 → 42**
（零新增、零顺带消失）；`vitest` **62 文件 / 1008 用例全绿**（N2 时 62 / 1006，+2 = 本票 AC6 两条）；
`vite build` rc=0。

**oxlint 零新增的实现方式 + 「指令挂在哪一行才生效」的 A/B（本轮实测，可复跑）**

本票新增的 6 处 `useMemo` 依赖被**刻意**写成字段级（`conversation?.turns` /
`conversation?.eventsVersion` / …）⇒ 会被 `exhaustive-deps` 判成「缺 `conversation`」+
「多余字段」；豁免**只能用行内 `// eslint-disable-line react-hooks/exhaustive-deps`**
（`disable-next-line` 与块级会把该函数全部 compiler 类规则一起吞掉——最小复现见 N2 节）。
`StepDetail.tsx` 最终保留 **9** 条行内指令，按**指令所在行**分两类：

| 变体 | 保留 | oxlint 总数 | `StepDetail.tsx` 本文件告警 |
| --- | --- | --- | --- |
| 基准 | 9 条全留 | **42** | 3（与 F1/N2 基线同数） |
| A | **只留依赖数组行**（`:153 :190 :192 :198 :246 :1051`），删掉回调行的（`:152 :1050`） | **42** | 3 ⇒ 少了回调行那两条**毫无变化** |
| B | **只留回调行**，删掉依赖数组行的 | **51** | 12 ⇒ 漏出 9 条（`:152` `:153` `:189` `:191` `:197` `:234` `:246` 等） |

⇒ **真正生效的是「挂在依赖数组那一行」的指令**；挂在回调行（`useMemo(() => …,` 那一行）的
指令是**装饰**。这与直觉相反（告警的标签行指向回调体里的 `conversation`），故写在此处供后续票复用。
`:1037` 是单行 `useMemo`（回调与依赖同处一行），两个变体都保留、不参与判定。

**真机 e2e（2026-09-18，AC8 缺口闭合；`fe96009` 同一工作树）**

主车道 `web/playwright.config.ts` 用**真机 Chromium**，但**不连真后端**——`e2e/fixtures.ts` 用
`page.route` mock SSE（帧形状 = `docs/BACKEND_CONTRACT_STREAMING_UI.md`）。命令（`npx` 在本环境
不可用，等价直调）：

```
cd web && node node_modules/@playwright/test/cli.js test --workers=2 --output=<新目录>
```

| 结果 | 数字 |
| --- | --- |
| 用例 | **436 passed / 0 failed** |
| 退出码 / 耗时 | **0** / 10.2 分钟（**无收尾挂死**） |
| project | `chromium-1280` + `chromium-1920`（各 218） |

与本票直接相关的 spec（**条数为每 project**）：`y-inspector-peek` **9**（AC1–AC9：点行预览 / ↑↓
移动 / Esc / Space 快按与按住 / 钉住跨会话 / 整页往返 / 拖宽夹取 / 头部不溢出 / 子会话头）、
`x-output-panel` 7、`z-artifact-content` 6、`i-keyboard` 3、`j-scroll` 3、`z-changes-panel` 3、
`a-reasoning` / `c-tool-output` / `h-density` 各 1。

**未闭合（本票）**

| 项 | 状态 | 解除条件 |
| --- | --- | --- |
| 主车道 e2e（AC8 的相关 e2e） | **已闭合**（2026-09-18，436 绿） | 无 |
| `playwright.live.config.ts` 联调车道（真模型 + 真后端 `127.0.0.1:8000`） | **未运行** | 按设计**不入标准门禁**；后端起在 8000 后跑 `--config playwright.live.config.ts --workers=1` |
| long task 数 + 最长单帧（上表最后一行） | **未取得** | 写 CDP `PerformanceObserver('longtask')` 采集脚本（可自动化，本批未做），或人工在 Performance 面板按固定场景录一次并归档。⚠ 理由更正：真机 Chromium 一直可用，**不是**「无 GUI 浏览器」 |

<!-- PERF-LIVE-F2-RAIL-2026-09-18 -->
#### 真机 live 车道（2026-09-18 追加）

**验收范围**：`web/playwright.live.config.ts`（**真后端** `127.0.0.1:8000` + **真模型**）。
上文 436 绿属**主车道 mock 车道**（`e2e/fixtures.ts` 用 `page.route` mock SSE，
帧形状照 `docs/BACKEND_CONTRACT_STREAMING_UI.md`）——两者不是一回事，别互相顶替。

| spec | 用例数 | 结果 | 归因 |
| --- | --- | --- | --- |
| `e2e-live/approval-live.spec.ts` | 2 | 修好选择器后**仍红** | 前端链路是对的：`session/started` 里 `permission_mode: 'read-only'` 证明档位选中；发出任务即 `run/failed reason=provider_account_unavailable` |
| `e2e-live/project-groups-live.spec.ts` | 1 | 红（**既存规格与代码冲突**） | `src/agent_harness/web/projects.py:249`「注册即归入 cwd 匹配的既有会话」（AC5 / #169，响应带 `sessions_attached`）与规格第 143 行「注册后应仍在未分组」正面冲突 |
| `web/scripts/perf-longtask-live.mjs`（采集器，非 e2e） | — | 绿 | 只读 + 只互动本地 UI，不依赖模型 |

- **`approval-live` 两层失败**：第一层已修（`pickControl(page,'权限模式', 0, '只读')` → **1**）。
  根因：`OptionPicker` 自 #201（`4ddec6b`，同时是 main HEAD 与本批 tip 的祖先 ⇒ **与 P1-B2 无关的
  既存漂移**）在目录首部恒插「默认（未选）」行，下压次数 = 条目下标 + 1；主车道
  `e2e/control-row.spec.ts` 里选「只读」的 4 处调用全是 1，只有本文件写 0，而 live 车道不入标准
  门禁 ⇒ 静默腐烂。**判别证据（真机 A/B，零副作用）**：`%TEMP%\wbi-probe-permpick.cjs`
  （只做「打开 → ↓N 次 → Enter」，**不点发送** ⇒ 不新增会话、不污染工作区）实测
  `目录前 5 行 = ["默认（未选）","只读","工作区写入","完全访问"]`；下压 **0** 次 ⇒ trigger 文案
  **`"权限"`**（= placeholder，证明 0 选中的是「默认（未选）」）、下压 **1** 次 ⇒ **`"只读"`**。
  另核：`4ddec6b`（#201）确为 main HEAD 与本批 tip 的共同祖先（`git merge-base --is-ancestor`
  两条均成立）⇒ 这是**既存漂移**，不是 P1-B2 引入的。
  第二层是**外部阻塞、本机不可解**：供应商账户冻结（证据同上），
  ⇒ **任何依赖真模型往返的用例（审批卡、流式长回答）当前不可能绿**。
- **`project-groups-live` 是决策票，不擅自修**：候选 A = 改规格承认 AC5 语义（有 `projects.py:249`
  的 docstring + AC5 背书，属纠正过期断言而非放宽校验）；候选 B = 台账登记「规格过期，待 WS-5/#155
  票主修」，live 车道保持红。已在台账登记，**未改规格**。
- ⚠ 该 spec 跑到一半失败会**污染真实数据**（把 `ws-delete-me` 注册进用户真实分组）。上次已用
  `DELETE /api/projects/<id>` 软删除还原（`sessions_detached=1`）。**重跑前先想清楚还原方式。**

**long task 数字（`perf-longtask-live.mjs`，生产口径 = 后端同源托管 `web/dist`）**

```bash
cd web
node scripts/perf-longtask-live.mjs                         # 生产口径（默认 http://127.0.0.1:8000）
node scripts/perf-longtask-live.mjs --base http://localhost:5173   # dev 口径（vite，未压缩）
```

场景：打开 **事件数最多** 的真实会话（本次 `40ee6420-…`，**1834 事件**，`timeline-row=200`）→
**12 次 Inspector peek 切换** → 工作区面板（输出 / 改动）往返。会话由脚本按 `event_count`
自动挑选，**不硬编码 id**；viewport 固定 1440×900。同轮顺带巡检不变量：
`detail-peek-kind` 12 次全部有值、`timeline-row[aria-current="true"]` 恒为 **1**
（F1 / N2 / F2 要保的在真机成立）。

北京时间 2026-09-18 同一台机器、同一后端、同一份 `web/dist` 上连跑四轮（`--base` 默认生产口径）：

| 阶段（脚本共 4 行 report：前 3 行**累计**口径，第 4 行 = 扣掉加载段） | 轮次 1 | 轮次 2 | 轮次 3 | 轮次 4 | 该行**新增那一段**内最坏单帧 |
| --- | --- | --- | --- | --- | --- |
| 加载 + 初始渲染 | 3 条 / 297ms | 4 条 / 333ms | 4 条 / 308ms | **0 条 / 0ms** | 156 / 102 / 83 / — ms |
| 打开长会话（1834 事件） | 6 条 / 1040ms | 5 条 / 669ms | 5 条 / 803ms | **1 条 / 272ms** | 513 / 336 / 495 / 272 ms |
| 再 +12 次 peek 切换与面板往返 | 10 条 / 1481ms | 7 条 / 909ms | 9 条 / 1331ms | **3 条 / 506ms** | 189 / 122 / 186 / 126 ms |
| 第 4 行（= 打开 + 交互，扣掉加载段） | 7 条 / 1184ms | 3 条 / 576ms | 5 条 / 1023ms | **3 条 / 506ms** | 513 / 336 / 495 / 272 ms |

> 读表须知：条数 / 总时长两列**累计**；最后一列是该行**新增那一段**里的最坏单帧
> （从 `run*.log` 的 `worst=[…]` 列表逐段切出来），**不是**累计最坏——轮次 1–3 的累计最坏
> 在第 2 行就已出现（513 / 336 / 495 ms），后面几段都没超过它。

证据（可直接复核）：日志 `%TEMP%\wbi-perf-longtask-run1.log` … `run4.log`；
截图 `web/gui-test-screenshots/perf-longtask/01-shell.png` … `04-panels.png`（gitignored）；
后端启动日志 `%TEMP%\wbi-perf-backend-20260918.log` 首行可证「用的是本 worktree 的 src」，且
`/` 返回的 `assets/index-DWcQjnCB.js` 与 disk 同文件（确为生产口径而非 dev）。

⚠ **轮次 4 推翻了本轮早先的「跨会话差异」定性（自我更正）**：轮次 1–3（23:26）加载段恒有
3–4 条 / ~300ms，而 23:50 的轮次 4 是 **0 条 / 0ms**，与上一会话（22:14）两次跑的
「加载 0 条 / 0ms、打开 1 条 / 257ms、全段 3 条 / 463ms」**同型**。⇒ 这不是「跨会话」差异，
而是**同一机器、同一会话、同一份 `web/dist` 上的双峰波动**（mode A：加载段有 3–4 条长任务、
全段 7–10 条；mode B：加载段 0 条、全段 1–3 条）。触发条件**未定位**（轮次 4 之前刚跑过一次
同页面的探针，怀疑与 OS 文件缓存 / 字体与主包解析冷热相关，**未证实**）。⇒ 结论不变但更强：
这批数字只能当**数量级基线**、**不得用作阈值断言**（与 `f1-cost-probe` 同规矩），且**单次
采样不足以支撑任何前后对照**——要用它做 A/B，必须同机、同口径、多轮取中位数并在文档里留下
全部轮次。dev 口径（vite 未压缩 + React DEV）系统性更高，两种口径**不可混用同一条基线**。

⚠ **口径边界（别把这当成替换）**：§2.1 禁的是 **Playwright 帧率（FPS）自动采集**；本脚本是
原生 long task 计数，**不与该禁令冲突**。但它给的是 **F2 交互路径**的数字，不是 F1 的
「长回答流式」——后者仍因账户冻结 **未取得**，F1 节那一行继续保留。


### F4 — 命令面板门控（#273）

要求指标：
- 面板**关闭**时连续 N 次提交下的 `summarizeEvent` 调用次数（改造前 ≈ N × 100）；
- 打开态一次构建的耗时（确认门控没有把成本挪成尖峰）。

**结论：第一项成立且为决定性证据；第二项在本环境（jsdom）不可测，故不作为证据。**详见下方「读数与边界」。

观测点：`summarizeEvent` 调用计数。用例 `web/src/App.test.tsx`（新增）注入 spy 包裹
`lib/projection.summarizeEvent`，并渲染**真 `App`**（只夹具化 `useSession` / `useProjects`，
`CommandPalette` 用转发包装捕获 `items` prop）。

> ⚠ **归因陷阱（实测踩过，务必保留此段）**：`summarizeEvent` 在 `web/src` 有**两处**调用点——
> `App.tsx:883`（本票对象）与 `StepDetail.tsx:1647`（时间线每帧对**全部**事件各调一次）。
> `App.tsx:1123` 无条件渲染 `StepDetail`，故不隔离时计数被污染成「100 + 事件总数」：
> 首次实测得到的是 `[223,224,225,226,227]` / 累计 **1125**（每次 +1 正是事件数在涨），
> 数字看着「很像那么回事」却完全不可归因。用例因此把 `StepDetail` 换成空壳
> （它只被 App 引用，grep 实证），观测面收敛为单点，AC1 期望值同时从「≈123」收紧为**严格 0**。
> **教训**：先确认「观测点是否单源」再取数，否则量到的是噪声之和。

| 场景 | 规模 | 指标 | 改造前 | 改造后 | 口径 / 命令 | 日期 | commit |
|---|---|---|---|---|---|---|---|
| 面板**关闭**，流式投影连续提交 5 次 | 事件窗口恒满 100 | `summarizeEvent` 调用次数 / **逐次**提交 | **[100,100,100,100,100]**（共 500；= 每次 1 次满窗构建） | **[0,0,0,0,0]**（关闭态构建体不执行） | `node node_modules/vitest/vitest.mjs run src/App.test.tsx`（cwd=`web/`） | 2026-09-19 | 28a1a34 |
| 面板**关闭**，投影提交 3 次 | 同上 | 各次渲染拿到的 `items` 是否同一引用 | **否**——每次换新数组（R3 违反） | **是**——同一模块级常量 `CLOSED_PALETTE_ITEMS` | 同上 | 2026-09-19 | 28a1a34 |
| 面板**打开**一次 | 同上 | 候选表 id / 顺序 / 分组（golden） | **110 项** = 6 `actions` + 4 `density` + 100 `events`（`event-121` → `event-22`） | **110 项，逐项一致**（门控未改变内容） | 同上（golden 断言） | 2026-09-19 | 28a1a34 |

**读数与边界**：

1. **决定性的那一项是「100 → 0」**：这是确定性计数，同一代码任意次运行都得同一个数，
   不存在噪声。它直接回答本票的问题——关闭态下那次满窗构建（100 条 × `summarizeEvent`）
   确实被省掉了，而流式期间每秒约 40 次提交，即每秒省掉约 4000 次调用。

2. **墙钟口径（jsdom）已实测两次，结论是不可用，故不落任何数字当证据。** 取证过程：
   用同一份改造前代码、同一探针（预热 3 拍 + 15 次取样 + 取中位数）先后跑两次，
   关闭态中位数得到 **27.41ms** 与 **14.23ms**（差近 2 倍）；
   改造后重跑，**打开态**中位数又从 33.78ms 掉到 20.97ms——而打开态那段代码路径
   **一个字都没改**。同一逻辑不可能凭空快 38%，只能是机器状态（JIT / GC / 后台负载）漂移主导。
   ⇒ 本环境下 jsdom 墙钟**不足以支撑「<5ms 级」的判定**，AC8 的「(若可测) 主线程占用」
   据此判定为**本环境不可测**，不编造数字。
   （AC8 的措辞本就带「若可测」，此判定不构成验收缺口；若后续要真数字，
   须走 §2.1 之外的浏览器车道——但那被账户冻结阻断，见 F1 节残留项。）

3. **因此「门控是否把成本挪成尖峰」由 golden（第 3 行）而非计时回答**：打开态候选表
   与改造前逐项一致，且打开那一拍必然重建（`paletteOpen` 进依赖）。逻辑上成本没有增加，
   只是从「每秒 40 次」减到「每次打开 1 次」。

### F6 — 呼吸辉光（#277）
_待落基线。_
要求指标：
- `pulse-thinking` / `pulse-tool` **可见**时 vs `prefers-reduced-motion: reduce` 的
  **A/B 两列** long task 数 + 最长单帧；A/B 差值即动画的真实成本；
- paint 时间占比（若可取得）。
> 若测得可忽略 ⇒ 本票**不改代码**，关单为「复核通过」；本文件必须留下这次测量的数字。

### B6 — Ledger 单动作连接收敛（#274）
_待落基线。_
要求指标：
- 一次 `update_state` 的 `_connect` 调用次数（改造前 **3**）；
- 一次工具调用在 Ledger 上的迁移次数 × 单次 `update_state` 耗时（改造前/后）。

### B7 — 事件循环重活搬线程（#275）

要求指标：
- WS 快照在 `STREAM_REPLAY_MAX_EVENTS` 允许的最大窗口下的序列化耗时
  （`to_dict()` × N + `json.dumps`）；
- 本地 artifact 大文件 `save` / `load` 的耗时（按 `ArtifactStore` 允许的典型上限）；
- **判定阈值**：某站点单次占用 **< 5ms** ⇒ 判定无需搬线程（并在此留数字）；
  **≥ 5ms** ⇒ 必须搬。

**量测脚本（随本票入库，可复跑）**：`scripts/measure_loop_blocking.py`

```bash
# 主仓库 venv + PYTHONPATH 指向本 worktree 的 src（worktree 的 .venv 无依赖）
cd <worktree>
PYTHONPATH=$PWD/src "D:/intelligence-agent-backend/.venv/Scripts/python.exe" \
  scripts/measure_loop_blocking.py \
  --sessions-root "D:/intelligence-agent-backend/.agent/workspace/sessions" \
  --repeats 300 --json /tmp/b7.json
```

口径（`PERF_BASELINE §2.3` 原话：「量事件循环上的**最长单次同步占用**，**不是**平均值」）：
**判定取 max**，中位数只作上下文；每轮带一条**与站点无关的控制列**看机器漂移。

#### 上界怎么来的（票面要求「读实现确认上限，不要凭空取值」）

| 站点 | 真实上界 | 依据 |
|---|---|---|
| **S1** WS 快照 | **1000 事件**（窗口条数） | `STREAM_REPLAY_MAX_EVENTS = 1000`（`web/app.py:629`）；脚本运行时在源码里核对字面量，常量一变就报 ❌ |
| **S2/S3** artifact 内容 | **2,000,000 字符** | 生产里唯一写入者 `tooling/overflow.py:88-94` 把超 `artifact_overflow_chars`（默认 2000）的**工具结果字段原样**交给 store；而字段本身的上界 = 沙箱单通道捕获上限 `LocalSandbox(max_capture_chars=2_000_000)`（`sandbox/local.py:121`），`tools/bash.py:124-132` 把 `result.stdout` 原样放进 `ToolResult.data` |

⚠ 两个上界卡的都是**条数 / 字符数**，不是字节数。所以每个站点都在**每种字节实现**下各测一遍，
**判定取最坏的那种**（契约不限制内容的语言：同一句 2M 字符可以是 2MB（ASCII）也可以是 6MB（CJK）；
本项目中文优先，CJK 是常态而非边角）。

#### 夹具（**真实生产会话**，不是合成数据）

跑 `--sessions-root` 时脚本自动挑**事件数最多**的真实会话，并取其中**序列化字节数最大的连续
1000 事件窗口**（滑窗，不是随便截一段）：

- 会话 `40ee6420-35de-4c25-a9f3-0e6bc99dab64`，**1834 事件**（本机事件数最多的一条）；
- 逐事件 `to_dict()` + `json.dumps(default=str)` 字节数：p50 **374** / p99 **2918** / max **9311**；
- 1000 事件窗口：最大 **430,858** 字节、中位 **418,990**、最小 **403,640**。

> 为什么帧比磁盘上的行大得多：`SessionStore` 落盘用 `ensure_ascii=False`（`store.py:180`），
> 而 WS 帧走 `json.dumps(payload, default=str)` 的默认 `ensure_ascii=True` ⇒ 中文被转义成
> `\uXXXX`。量测脚本按**帧**那一侧同参复刻（`_serialized_sizes` 的注释钉住了这一点）。

#### S1 读数（WS 快照，`[e.to_dict() for e in window]` + `json.dumps` 合成的**一整块**）

7 次独立运行，同一命令、同一夹具（数字为 `combined` 块的 **最长 / 中位**，单位 ms）：

| 运行 | `--repeats` | 最大窗口 | 中位窗口 | 最小窗口 | 控制列 最长 / 中位 |
|---|---|---|---|---|---|
| ① | 15 | **7.94** / 2.34 | — | — | 11.94 / 11.30 |
| ② | 50 | 2.66 / 1.91 | 3.62 / 1.90 | 2.79 / 1.86 | 14.56 / 12.06 |
| ③ | 50 | 2.46 / 1.77 | 3.74 / 1.77 | 2.22 / 1.69 | — |
| ④ | 200 | 12.35 / 5.49 | 10.22 / 5.67 | 10.33 / 4.26 | 42.46 / 12.06 |
| ⑤ | 300 | 10.51 / 3.71 | 9.23 / 3.67 | 14.58 / 3.69 | — |
| ⑥ | 300 | 7.61 / 4.91 | 7.34 / 3.72 | 256.11 / 5.34 | — |
| ⑦ | 300 | 32.04 / 4.73 | 9.29 / 4.35 | 9.59 / 3.53 | — |

分解（运行②，最大窗口，最长 / 中位 ms）：`to_dict() × 1000` **0.66 / 0.28**；
`json.dumps`（430KB，含中文转义）**1.96 / 1.61** ⇒ **dumps 是大头**。

**S1 判定：≥ 5ms ⇒ 必须搬线程（方案 A）。** 依据：

1. **在能分辨尾巴的样本量下，p90 稳定 ≥5ms**：运行④⑤⑥⑦共 9 次窗口测量（n=200/300），
   p90 全部落在 **5.6 – 11.9 ms**，最长单次 7.3 – 32.0 ms。票面判据是「最长单次」，
   这条已经越线一个量级。
2. 运行②③（n=50）那次「最长 2.2 – 3.7ms」**在 n≥200 未复现**。本机墙钟对 <5ms 级判定
   不可靠（F4 节同一结论），而 n=300 的样本量是 n=50 的 6 倍、且 9/9 一致 ⇒ 判定取它。

#### S1′ 单条事件帧（relay 路径 `websocket.py:303` 的 `_send_json`）

`combined (单帧)`：n=50 最长 **0.008 – 0.019** ms；n=200 最长 **0.145** ms、中位 **0.042** ms。
与 S1 相差 **3 个数量级** ⇒ **判定：不搬**（票面「未闭合项」原文：「单帧小，判定为可忽略」，
解除条件是「基线显示单帧 `dumps` 成为长帧主因」——**未成为主因**，故该项按票面留在原地）。

#### S2 / S3 读数（`LocalArtifactStore.save` / `load` **整个函数体**）

前提已运行时核对：两个函数体内**零 `await`**（`GET_AWAITABLE` 操作码探测；
`inspect.CO_AWAIT` 在现代 CPython 里不存在）⇒ 「最长单次同步占用」**就是**整个函数的墙钟。

| 规模 | save 最长 / 中位 | load 最长 / 中位 |
|---|---|---|
| 64 KiB 字符（read / MCP / skill 工具上限） | 14.04 / 8.18 | 3.92 / 1.37 |
| 300 KiB 字符（diff `before`+`after` 的 JSON） | 17.97 / 7.26 | 2.16 / 1.07 |
| 2M 字符 ASCII（2MB 落盘） | 22.50 / 10.24 | 14.16 / 5.88 |
| **2M 字符 CJK（6MB 落盘）← 判定级** | **73.86 / 36.95** | **61.46 / 37.75** |

分解（2M 字符 CJK，最长 / 中位 ms）：`encode` 19.5 / 10.2、`sha256` 27.8 / 12.4、
`_write_atomic`（一次）35.2 / 27.2、`read_bytes` 8.6 / 4.7、`read_bytes+decode` 32.6 / 17.3。

**S2 `save` 判定：≥ 5ms ⇒ 必须搬。S3 `load` 判定：≥ 5ms ⇒ 必须搬。**

> **`save` 的成本几乎与内容大小无关（实测定位，64 KiB，n=60 中位）**：
> `save` 组合 5.17ms；`save` 同步体克隆（去掉 `await`）4.51ms；**两次 `_write_atomic` 4.53ms**；
> 其中一次内容写 2.45ms、一次元数据写（~250 字节）≈2.1ms；`mkdir(exist_ok=True)` 0.11ms；
> `Artifact` + `sha256` + `model_dump` 0.05ms。
> ⇒ **`_write_atomic` 的单次固定成本 ≈1.5 – 2.5ms，与内容大小基本无关**（它每次都新建一个
> 随机名临时文件再 `os.replace`；本机 NTFS 元数据 + AV 实时扫描的开销）。
> `save` 一次调它**两次** ⇒ 哪怕 content 只有几百字节，`save` 也是 ~4 – 5ms。
> 这条同时解释了「分解项之和 < 组合项」——分解表里 `_write_atomic` 只列了**一次**。
> ⚠ 本机是 Windows + NTFS；Linux 上这个固定成本会低一个量级。**6MB 那一级的数字与平台无关**
> （内容是实打实要编码/哈希/写盘的），故判定对平台不敏感。

**环境边界（与 F4 节同一纪律）**：本机墙钟对 <5ms 级判定**不可靠**。控制列（整数循环，
~12ms）只能证明「机器稳态未变」——它在 7 次运行里中位恒为 11.3 – 12.1ms；但它的**尾巴**
在运行④飙到 42.5ms，且它对**内存/IO 密集**负载不敏感（`json.dumps` 430KB 恰好是这类），
所以它**控不住** S1 的漂移。判定因此一律取**保守侧**，并把全部轮次留在上表里。

### F3 — 贴底读写收进单帧（#276）
_待落基线。_
要求指标：
- 一个 rAF 帧内 `scrollHeight` 的读取次数（改造前为「每提交一次」）；
- long task 数 + 最长单帧（长会话 + 长回答，录制存档）。
> 若基线显示完全观测不到差异 ⇒ 允许关单为 wontfix，**但必须在此留下这次基线数字** +
> 一条「rAF 合并被测量否决」的记录。

### F7 — 过渡编排（#278）
_待落基线。_
要求指标：
- 打开 → 关闭 → 整页 → 退出整页 四段操作的 long task 数 + 最长单帧（改造前/后）；
- **必须明确写出**「过渡没有让最长单帧变差」；若变差，写明按实测收窄了哪些轨道。

### F5 — 长列表离屏跳过（#279）
_待落基线。_
要求指标：
- TOOLS / DIFFS / ARTIFACTS 三个列表在 **N = 50 / 200 / 500** 下的单次渲染耗时 + 节点数；
- **判定阈值**：某列表在 N ≥ 200 时单次渲染 **≥ 4ms**（与 `StepDetail.tsx:891-893` 注释里
  Timeline 当时的量级同级）⇒ 必须做尾窗；远小于 4ms ⇒ 判定无需处理（留数字）。
> 参考已有实测：`StepDetail.tsx:935` 记录「流式合帧 40fps 下 Timeline tab 每秒烧 14s CPU；
> 200 行窗口 ≈4ms」。本票沿用同一口径。

### F8 — SSE 分帧游标化（#280）
_待落基线。_
要求指标：
- **一帧超大**场景（`stream/truncated` 之后「整段 durable 日志一帧」的形态）下，
  N 个帧被切成 M 个 chunk 时的分帧总耗时（改造前应呈超线性）；
- 归一化/扫描处理的总字符数（应随输入线性）。

### B8 — 流式 chunk 聚合去 O(n²)（#281）
_待落基线。_
要求指标：
- N = **1k / 4k / 16k** chunk 的聚合耗时（改造前应呈超线性，改造后应线性；
  三点拟合，不要只测一点）；
- **同时必须记事件产出序列 golden 是否逐条不变**（这是正确性约束，不是性能数字，
  但要在同一节声明它通过了）。
