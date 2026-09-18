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
| 流式提交一次（已完成段已渲染） | 1 个可见已完成段 | `renderMarkdown` 调用次数 / 每次父级提交 | **1**（= 可见已完成段数，与提交同频） | **0**（内容未变即命中，零解析） | `node node_modules/vitest/vitest.mjs run src/components/Conversation.render.test.tsx` | 2026-09-18 | <sha> |
| 同上，挂载即计数 | 1 个已完成轮 | `renderMarkdown` 调用次数 / 挂载（含 sessionKey effect 那一拍） | **2** | **1** | 同上 | 2026-09-18 | <sha> |
| 父级无关状态变化 | 1 个已完成轮 | `memo(TurnView)` render 次数 / 挂载 | **2**（memo 恒 miss） | **1** | 同上（以轮次时间戳计 `formatDuration` 调用） | 2026-09-18 | <sha> |
| 只改与工具卡无关的 prop | 1 个工具轮 | `memo(ToolCard)` render 次数 / 挂载 | **2**（`cycle` 闭包每次新建） | **1** | 同上（以工具时间戳计 `formatDuration` 调用） | 2026-09-18 | <sha> |
| 单次解析成本 | 2k 字长回答 | `renderMarkdown` 单次耗时中位数（n=200） | **0.692 ms**（p95 3.591 ms） | 同（单价不变，变的是调用次数） | 见下方「成本探针」命令 | 2026-09-18 | <sha> |
| 一次提交的被浪费工作 | 3 个可见已完成段 | 一轮全量重解析耗时中位数（n=200） | **1.320 ms**（p95 2.614 ms） | **0**（内容未变即命中，零解析） | 同下 | 2026-09-18 | <sha> |
| 折算每秒 | 合帧窗口 24ms ⇒ 40 次提交/秒 | 单核占用（40 × 1.320 ms） | **≈53 ms/s（≈5.3%）**，且随段数/长度线性增长 | ≈0 | 同上 | 2026-09-18 | <sha> |
| `deriveChain` 单次成本 | 3 model 段 + 2 工具 | 单次耗时中位数（n=200） | **0.002 ms** | 同 | 同下 | 2026-09-18 | <sha> |
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

**未闭合（G4 的观感口径）**：Chrome Performance 的 long task 数 / 最长单帧**本次未取得**——
本执行环境无 GUI 浏览器、且无真后端可驱动一场真实流式，产不出可信 trace（`PERF_BASELINE §2.1`
本身也明令**不引入** Playwright 帧率自动采集）。**解除条件**：在真机 Chrome Performance 面板按
固定场景录一次（同窗口尺寸 + 同一段长回答 + 同一操作序列：打开 → 流式 → 折叠/展开工具卡 →
切 density），把 trace 存档并把两列数字补进上表。在此之前，本票的收益证据只覆盖
「可自动化口径」（调用次数 / 次数 × 单价）。

要求指标：
- 「流式提交一次」时 `renderMarkdown` 的实际调用次数（改造前应按可见已完成段数增长）；
- `memo(TurnView)` / `memo(ToolCard)` 的 render 次数（render 计数 spy）；
- Chrome Performance：long task 数 + 最长单帧（长回答场景，录制存档）。

### N2 — `eventsVersion`（#271）
_待落基线。_
要求指标：
- **正确性数字**：`StepDetail` 的 run 分组在「追加事件后」是否更新（二值）；
- 投影层：`projectHistory` 4650 事件、`applyEvent` @20k 事件的耗时
  （**必须保持** `3344e34` 的水平：`<10µs/事件` / `2.8ms`——本票**不得**回退这两个数）；
- 新增字段后 `applyEvent` 的耗时增量（应可忽略）。

### F2 — memo 与 props 收敛（#272）
_待落基线。_
要求指标：
- Inspector **关闭**状态下追加 delta 时 `allTools` / `deriveAgentProfile` 的调用次数；
- `Conversation` / `StepDetail` 的 render 次数（父级无关状态变化时）；
- long task 数 + 最长单帧（Inspector 打开且长 run，录制存档）。

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
