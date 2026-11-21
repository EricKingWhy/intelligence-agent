# Flake 台账（flaky 测试的唯一登记处）

> 归属：issue **#1338 改动 2**。规则正文（死规矩）落在 `docs/agents/verification.md` §4.1；
> 本文件是**数据**：每一条 flaky 用例在这里登记一次，准入判据 F1–F5 缺一不放行。
>
> 与本文件互补的票：**#1292**（e2e 既有红跟踪票）。#1292 的条目在本台账建成后**可迁移**进来，
> 迁移时按 F1–F5 补齐证据；两票互相引用。

---

## 0. 用途

一条测试**偶发红**时，人的默认反应是「复跑一下」——复跑转绿就当作没发生。
这个习惯是错的：它既掩盖了真 bug，也把 flake 永久留在必需路径上反复消耗每一张票的时间。

本台账把这件事变成**可核对的登记**：flake 必须具名、带签名、带读数、带机械证明，
然后才允许从「阻塞合并」降级为「只进 nightly」。**没有登记就不是 flake**——
未登记的偶发红按真 bug 处理。

---

## 1. 准入标准 F1–F5（逐字取自 #1338，缺一不放行，不许合并）

| 编号 | 判据 |
| --- | --- |
| **F1** | 逐条**具名** + 失败签名 + 触发命令 + 树 SHA，**读数落盘**。 |
| **F2** | **同树**隔离重跑 **≥2 次全绿**，读数落盘。 |
| **F3** | `git diff` 与失败 spec **零文件交集**（机械证明）。 |
| **F4** | **基线对照**——基线同签名红 ⇒ 走「既有红」waive + 开票，**不是 flake**。 |
| **F5** | **首次即登记**；同签名累计 **≥2 次**未修 ⇒ **移出必需路径**、开票指派 owner。 |

关于 F5 的「移出必需路径」载体（#1338 原文）：

> 在台账登记 + `verification.md` 车道清单标注该用例走 nightly，由执行器在**台账 PR 里一并改**。

`gate-nightly.yml` 已覆盖全量测试，**本票不改该工作流**；「移出必需路径」只是把该用例从
**PR 必需路径**上摘下来，nightly 仍会跑它。

---

## 2. 必须按真 bug 立案的情形（不是 flake）

出现下列任一情形，**不得**登记为 flake：

1. 同签名**同树 3 次内再现 ≥2 次**；
2. **隔离重跑仍红**；
3. 失败落在 **PR 改动文件**上；
4. **数据丢失 / 权限安全 / 数值钉死断言**类失败；
5. **缺 F1/F2 证据**（没有落盘读数、没有同树重跑记录）。

第 5 条最常被忽略：**「我觉得它是 flake」不是证据**。

---

## 3. 两条死规矩（权威正文见 `docs/agents/verification.md` §4.1）

1. **已登记 flake 不阻塞合并，只进 nightly**（nightly 已覆盖全量，`gate-nightly.yml` 不改）。
2. **「复跑转绿」四字不许作为唯一归因**。#1298 的 5 条正是因此被复核打回——
   复跑转绿只能说明「这次没复现」，说明不了「这不是本次改动引入的」；
   归因必须落在 F1–F5 的证据上。

---

## 4. 成熟产品依据

Kubernetes SIG Testing 的 flaky-tests 政策：**零容忍 + 隔离（quarantine）+ 禁止自动重试**。

<https://github.com/kubernetes/community/blob/master/contributors/devel/sig-testing/flaky-tests.md>

本仓 `web/playwright.config.ts` 保持**无 retries**，与 K8s 同款口径一致（#1338「不做事项」）。

---

## 5. 格式示例（**FORMAT EXAMPLE**，不是已准入的登记）

> ⚠ 下面这条是**格式示例**，取自真实历史事故，但**证据链不完整**。
> 它示范字段怎么写，**不构成准入**：F1–F5 全部以落盘读数补齐之前，它不能算 flake。

### 示例：vitest `StepDetail.window.test.tsx` 超时（B-29）

- **F1 具名 / 签名 / 触发命令 / 树 SHA**
  - 名称：`web/src/components/StepDetail.window.test.tsx`（B-29 已知 flake）
  - 失败签名：**恰好 1 例**超时，约 **6224ms**；跨次运行**非确定性**
    （同一次运行里其余用例全绿，下一次可能整轮全绿）
  - 触发命令（`web/`）：`node node_modules/vitest/vitest.mjs run src`
  - 树 SHA：`docs/agents/verification.md` §2 ⑭ 记录的读数为
    `HEAD=65f8d1f0a455 / tree=3cbc12dd63f2`；但两次结果不同的运行只被描述为
    「同一类干净树」，**两次运行各自的 SHA 未被逐次钉死** ⇒ **此处不完整**
  - 读数落盘：**只有 `verification.md` §2 ⑭ 的散文记录**（血证 ②），**没有原始日志文件**
    ⇒ **此处不完整**
- **F2 同树隔离重跑 ≥2 次全绿**
  - 历史记录：同类干净树两次读数 —— 运行 A：67 文件 1067 例，**1 例超时**；
    运行 B：67 文件 1067 例，**全绿**。
  - ⇒ 只有 **1 次**绿，**F2 未满足**（要求同树 ≥2 次全绿且读数落盘）
- **F3 `git diff` 与失败 spec 零文件交集**
  - 历史记录给出的是**结论**（「前端红结构上无法归因到本次改动」），**未留 `git diff` 机械证明**
    ⇒ **此处不完整**
- **F4 基线对照**
  - 历史记录**未做**基线同签名对照 ⇒ **此处不完整**
- **F5 首次即登记 / 累计 ≥2 次未修则移出必需路径 + 开票**
  - 历史上只在 `verification.md` 里被记为 B-29 已知 flake，**未进本台账、未开票指派 owner**
    ⇒ **此处不完整**
- **状态**：**FORMAT EXAMPLE**（未准入）。真要登记，必须把 F1–F5 全部补齐、读数落盘后方可计入。

---

## 6. 空白模板（复制后逐项填写；缺项即不许合并）

```markdown
### <用例 id / 路径>（<票号或别名>）

- **F1 具名 / 签名 / 触发命令 / 树 SHA**
  - 名称：
  - 失败签名（精确到「哪一例、什么断言/超时、多少 ms、出现频次」）：
  - 触发命令（含 cwd）：
  - 树 SHA（`git rev-parse HEAD` + `git rev-parse HEAD^{tree}`）：
  - 读数落盘位置（原始日志路径，入库或可复核的绝对路径）：
- **F2 同树隔离重跑 ≥2 次全绿**
  - 第 1 次：<结果>（日志：<路径>）
  - 第 2 次：<结果>（日志：<路径>）
- **F3 机械证明**
  - `git diff --name-status <base>..<tip>` 输出：
  - 与失败 spec 的交集：**空**（逐行核对：<粘贴命令与输出>）
- **F4 基线对照**
  - 基线树：<sha>；同签名红？<是/否>；读数：<路径>
  - 若为「是」⇒ 本条目改为**既有红 waive + 开票**，不登记为 flake
- **F5 登记与 owner**
  - 首次观测日期：
  - 累计同签名次数：
  - 是否已移出必需路径（`verification.md` 车道清单标注 nightly）：
  - 指派 owner / 跟踪票：
- **状态**：<已准入 / 待补证据 / 已移出必需路径>
```

---

## 7. 既有红观测记录（**不是 flake 登记**）

> 本节的条目**不构成准入**：它们是 F4 判为「基线同签名红」的观测，按 §2 第 1 条应走
> 「既有红 waive + 开票」，而**开票不在执行器权限内**（#1500 任务书第 3 步：开票走双路票面审查）。
> 放在这里只为把读数落盘、让协调员按 F1–F5 的形状接手；**不得**把本节读成「已登记 flake、
> 可放上必需路径」。两条 spec 均**不在** PR 车道（`docs/agents/e2e_scope_map.tsv` 未收）。
> **读数与探针落盘位置**：`~/1500-evidence/`（在协作机 home 下，**不入库**）—— 原始 JSON
> `e2e-base-1500.json` / `1500-e2e-full2.json`、隔离复跑 `iso-1500.log` / `iso2.log`、
> 探针 `1500-probe.log` + `1500-probe.spec.ts`（Probe A/B 各 6 次）、
> 截图 `probe-smem-z.png`。正文提到的路径都指这个目录。

### 7.1 `web/e2e/design-tokens.spec.ts:127`（焦点环宽度量到 3px）

- **F1 名称 / 签名 / 触发命令 / 树**
  - 名称：`design-tokens.spec.ts` → `#1364 截图：会话列表 + 顶栏（dark/light）` 的焦点环断言（`:127`）。
  - 签名：`expect(ring.width).toBe('2px')` 实得 `"3px"`，同一次失败里 `outline-color` 与
    `--focus-ring` 都正确（`[dark] focus ring = 3px solid rgb(154, 139, 255)`）；**非确定性**。
  - 触发命令（`web/`）：`node node_modules/@playwright/test/cli.js test design-tokens.spec.ts --workers=2`
  - 树：`a5250089f`（tree `5c8ca801`）与旧绿 nightly 树 `e9a259f7`（tree `8843b789`）。
  - 读数：基线全量 2026-11-18T02:25Z 红 1 次；同树隔离复跑 6 次红 1 次
    （`~/1500-evidence/iso2.log` 段 D：run 3 红，其余 6 passed）；旧树 `e9a259f7` 3 次复跑
    **3 次全红**（`~/1500-evidence/iso-1500.log` 段 C）。
- **根因（机械，不是猜测）**：`web/src/index.css:592–598` 的
  `@media (prefers-reduced-motion: reduce)` 给 `*` 加 `transition-duration: 0.01ms !important`，
  而 `transition-property` 初始值就是 `all` ⇒ `outline-width` / `outline-offset` / `outline-color`
  从聚焦前的值开始过渡。页内 rAF 时间线（`~/1500-evidence/1500-probe.log` Probe A，6 次重放）：
  **5/6 次首帧（t=4~21ms）读到 `w:"3px", off:"0px"`**（第 5 次首帧 t=19ms 已是 `2px/2px`）；
  其中 **4 次**（第 1、2、4、6 次）首帧同帧挂着 `outline-*:running` 过渡，第 3 次首帧（t=4ms）
  过渡尚未注册、第二帧（t≈23ms）才出现。**各次最迟在 t≈44ms 收敛到 `w:"2px", off:"2px"` 且
  无过渡**（收敛帧：第 1/2/4/6 次为第二帧 t=33~41ms，第 3 次为第三帧 t=44ms，第 5 次首帧
  t=19ms 即已收敛 —— 不是「第二帧起恒为 2px」）。
  spec 在**首帧**取 `getComputedStyle` ⇒ 读到过渡起点。产品的稳态值确实是 2px
  （`:focus-visible { outline: 2px solid var(--focus-ring) }`）。
- **F2 同树隔离重跑**：tree `5c8ca801` 上 6 次里 5 次全绿（每次 6 passed）⇒ 有 ≥2 次全绿读数，
  但**同签名在同树再现 2 次**（基线 + 第 3 次复跑）⇒ 按 §2 第 1 条**不得**登记为 flake。
- **F3 零文件交集**：`git diff --name-only a5250089f..cc4cee6aa`（**钉住实测时点的分支 tip
  `cc4cee6aa`**；此前写作「8 个文件」是 `2ad973f32` 时点的数）实查 **13 个文件**，逐名：
  `.github/workflows/e2e-smoke.yml`、`docs/agents/e2e_scope_map.tsv`、
  `docs/agents/verification.md`、`docs/flake-ledger.md`、`docs/gate/35643a797….json`、
  `docs/gate/dfc8f9ffe….json`、`docs/gate/f453bfd71….json`、
  `docs/review_ledger.d/t1500-e2e-scope-expansion-mechanical.tsv`、
  `docs/review_ledger.d/t1500-r1-fix-mechanical.tsv`、
  `docs/review_ledger.d/t1500-r1-pin-mechanical.tsv`、`scripts/e2e_scope.py`、`scripts/gate0.py`、
  `tests/tooling/test_e2e_scope.py`。与 `web/e2e/design-tokens.spec.ts` 及
  `web/src/index.css` **交集为空**。其后的收据提交只加 `docs/gate/*.json`，不改变零交集结论。
- **F4 基线对照**：**同签名红**（旧树 3/3、当前树 2/7）⇒ 这是**既有红**，不是本分支引入。
- **F5 处置（待协调员）**：未开票（执行器无开票授权）；该 spec 未进 PR 车道，故不阻塞任何 PR。
  建议：修 spec 的采样时机（等 `transitionend` / `expect.poll` 到 2px），**不要**改断言值、
  也不要动 `prefers-reduced-motion` 块（那是无障碍必需项）。

### 7.2 `web/e2e/s-memories.spec.ts:309`（关掉命令面板后 `.palette-input` 仍在）

- **F1 名称 / 签名 / 触发命令 / 树**
  - 名称：`s-memories.spec.ts` → `focus: selecting an already open panel does not leak its opener to the next panel`（`:309`，失败断言在 `:328`）。
  - 签名：`expect(page.locator('.palette-input')).toHaveCount(0)` 实得 **1**（命令面板没关）。
  - 触发命令（`web/`）：`node node_modules/@playwright/test/cli.js test s-memories.spec.ts --workers=1 --project=chromium-1920`
  - 树：`a5250089f`（tree `5c8ca801`）。
  - 读数：整文件 3 次里红 2 次（每次 `29 passed / 1 failed`，`~/1500-evidence/iso-1500.log` 段 A）；
    单用例 6 次里红 1 次（同文件段 B）；`~/1500-evidence/iso2.log` 段 E 单用例 6 次里红 1 次。
    早期两次「整页卡住」探针（12 次里 4 次、14 次里第 2 次）**过程日志未落盘**，故只当过程观察；
    可复现的机械证据是下面 Probe B 的 6 次重放。
- **根因（机械，指向产品面）**：`.palette-content` 与 `.memory-panel` **同为 `position: fixed; z-index: 51`**，
  且都是 Radix portal 追加到 `body`；记忆面板的 portal 更晚建立 ⇒ **记忆面板盖在命令面板上**。
  页内实测（`~/1500-evidence/1500-probe.log` Probe B，6 次重放）：
  `document.elementFromPoint(命令项中心)` **6/6 次**恒为 `DIV.project-dialog-head`（记忆面板头部）；
  不加 `force` 的真实点击 **6/6 次被 2s 超时拦下**，此后 `.palette-input` 仍为 1（面板没关）。
  spec 用 `page.locator('.palette-item').click({ force: true })` ⇒ 点击被派发给覆盖元素，
  命令面板的 `onClick` 不触发、面板不关 —— 卡住与否取决于命中测试的时序竞争。
- **F2/F4**：同签名同树再现（2/3 整文件）⇒ §2 第 1 条排除 flake；且基线树即红 ⇒ 既有红。
- **F3 零文件交集**：同上（`a5250089f..cc4cee6aa` 的 13 个文件均与该 spec、与 `App.tsx` /
  `CommandPalette.tsx` 零交集；其后的收据提交只加 `docs/gate/*.json`，不改变该结论）。
- **F5 处置（以下为 #1706 修复前的历史记录）**：**按 #1500 任务书第 3 步「隔离证据指向真产品缺陷
  ⇒ 停下报协调员、不许自己开票」处理**：未开票、未登记为 flake；该 spec 已从 PR 车道映射表排除
  （见 §7.2 的 `e2e_scope_map.tsv` 说明）。
  建议方向（留给协调员/后续票）：命令面板与记忆面板同时打开时的层级/互斥（打开面板时关闭面板，
  或给命令面板更高层级），并去掉 spec 里的 `force: true`（它掩盖了「用户点不到」这一事实）。
  > 上面这段记录了**当时的**处置与建议；其中「已从 PR 车道排除」「建议去掉 `force: true`」
  > 两条在 #1706 之后都不成立，现状见下一条。
- **2026-11-21 关闭（#1706）**
  - **根因（两层）**：① DOM 顺序——`.palette-content` 与 `.memory-panel` 同为 fixed/51，Radix
    portal 都 append 到 body，退出动画（`--dur-out` 150ms）未结束时重开使 `Presence` 复用旧节点
    ⇒ 命令面板仍排在记忆面板之前；② Radix `DismissableLayer` 按**图层注册顺序**（插入序 Set）
    判顶层 ⇒ 旧节点让记忆面板仍被视为顶层模态，把「点在命令面板里」判成 outside 而**关掉自己**。
  - **修法**：`CommandPalette` 按打开周期重挂载（换 `Dialog.Root` key，节点重新 append、图层
    重新入栈）；仅本组件的 Overlay/Content 加修饰类抬到 52/53（裸 `.palette-overlay` 被全仓
    13+ 处 Dialog 共用，不动其数值——#203）。
  - **`force: true` 已删除**（`s-memories.spec.ts` 的 `:309` 用例改为真实点击）。
  - **`s-memories.spec.ts` 已进 PR 车道映射表**（`docs/agents/e2e_scope_map.tsv` 新增
    `CommandPalette.tsx` 与 `s-memories.spec.ts` 两行，撤销排除项 e.）。
  - **读数**：`:309` 单跑 **6/6** 通过；`s-memories.spec.ts` 两视口（chromium-1280 + 1920、
    `--workers=2`）**40/40** 通过。
  - **base 反向对照**（产品代码回退到 `d554cbf28`）：三条缺陷时序用例 **4/4 红**——
    AC1① 命中 `DIV.project-dialog-head`、AC1② `.palette-input` 计数 1≠0、
    AC1③ 与 AC4③ 面板被自关（`element(s) not found`）。
    读数：`~/workspace/ticket-takeover-drafts/evidence-1706/spec-defect-timing-*.log`。
