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

### F4 — 命令面板门控（#273）
_待落基线。_
要求指标：
- 面板**关闭**时连续 N 次提交下的 `summarizeEvent` 调用次数（改造前 ≈ N × 100）；
- 打开态一次构建的耗时（确认门控没有把成本挪成尖峰）。

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
_待落基线。_
要求指标：
- WS 快照在 `STREAM_REPLAY_MAX_EVENTS` 允许的最大窗口下的序列化耗时
  （`to_dict()` × N + `json.dumps`）；
- 本地 artifact 大文件 `save` / `load` 的耗时（按 `ArtifactStore` 允许的典型上限）；
- **判定阈值**：某站点单次占用 **< 5ms** ⇒ 判定无需搬线程（并在此留数字）；
  **≥ 5ms** ⇒ 必须搬。

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
