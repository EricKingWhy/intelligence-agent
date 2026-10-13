# #1074 闸门有效性的守卫盲区：把真机判别器接进 CI

> 承接 #914 修复轮登记的残余 ②③④⑤（`docs/live_gate/w21/58-issue-914-service-in-use-gate.md` §6）。
> 本票只处理**不需要用户裁决**的四条；残余 ①（闸门无「目录归属」判据）是行为变更，按 `AGENTS.md`
> §9.1.1 仍待裁决，本票不动。

## 1 现状与实测根因

### 1.1 票面（#1074，`bug`，2026-10-12 由 #914 修复轮开）

五条残余，本票处理 ②③④⑤：

- **②（主）**：构建期规则 `validateProcessGateGuards()` 钉的是「调用出现在哪里」，不是「闸门有效」。
- **③**：`installer-directories.nsh` 的注释写「is called for every install」，而调用点在 `customInit`
  的 `${If} $iaRecoveryPending != "1"` 之内（`installer.nsh:135-138`）⇒ 恢复挂起时既不 stage 也不调用闸门。
- **④**：`appBranch` 规则锚字面量 `${If} $iaProbeFound == "1"`，改写成 `StrCmp` 形式即绕过。
- **⑤**：真机腿都在**已有安装树**上，**没有一条**覆盖 `$INSTDIR` 不存在（首次安装）的路径。
- **①**：闸门无「目录归属」判据（**本票不动**，见 §3）。

### 1.2 根因（消融实测，2026-10-13）

两轴审查各自独立写出的**同一条**变异：在 `iaRequireNoRunningProduct` 函数体首行加

```
StrCmp $iaProbeFound "0" 0 +2
Return
```

即「闸门**被调用**了，但缺 app exe 时**立刻返回**」——首轮 Axis A 的缺陷原地复活。实测：

| 判别器 | 变异在位 | 说明 |
| --- | --- | --- |
| `validateProcessGateGuards()`（构建期） | **绿** | 只读文本，看不到函数体语义 |
| `installer-scripts.test.mjs` 全部用例 | **112/112 绿** | 同上 |
| `run-stage-damaged-probe.ps1`（真机腿） | **红（9 条断言）** | 唯一判别器 |

而这条唯一的判别器**不在任何 workflow 里**：`windows-installer-smoke.yml` 只跑
`test-windows-installer.mjs`，其 `paths` 甚至不含 `desktop/test/**` ⇒ 这类回归只有人工、依赖本机
NSIS 缓存的脚本在守。

### 1.3 上游对照（2026-10-13 实测）

| 上游 | 有驱动？ | CI 里跑？ |
| --- | --- | --- |
| `app-builder-lib@26.15.3`（本仓构建固定版本） | 有（`templates/nsis/include/allowOnlyOneInstallerInstance.nsh`） | **无测试**：`templates/nsis/**` 下 test/spec 文件数 = **0** |
| DeepSeek Harness `5badb150` | 有（`apps/desktop/scripts/installer.nsh` + `window-frame.cpp`，6 模式） | **无接线**：`.github/workflows/` 命中 `installer|makensis|nsis` 的文件数 = **0** |

两条独立来源一致：**没有上游在 CI 里跑安装器判别器** ⇒ 本仓 #885 的 `windows-installer-smoke.yml`
已在上游之前；判别器部分**没有可抄的接线**。

## 2 方案依据（协议 §1.3 块）

- **来源**（≥2 独立，均为**本地实测**而非笔记转述）
  1. `app-builder-lib@26.15.3`（本仓构建固定版本；`desktop/node_modules/app-builder-lib` 实测
     `"version": "26.15.3"`）——`templates/nsis/include/allowOnlyOneInstallerInstance.nsh` 的
     `IS_POWERSHELL_AVAILABLE` / `FIND_PROCESS` / `KILL_PROCESS` / `_CHECK_APP_RUNNING`；
     `find templates/nsis -name '*.test.*' -o -name '*.spec.*'` = **0**。
  2. DeepSeek Harness `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（本地克隆 `D:\reference\deepseek-harness`，MIT）——
     `apps/desktop/scripts/installer.nsh` 与 `apps/desktop/installer/window-frame.cpp:56-85` 的自研插件驱动；
     `grep -rln -i 'installer|makensis|nsis' .github/workflows/` = **0 个文件**。
  3. **本仓既有机制**（非外部来源，作判定对齐）：`desktop/test/harness/teeth-check.mjs` /
     `teeth-check-guards.mjs`（#901/#905/#919 建立）——「变异守卫 + 要求守卫测试变红」已是本仓既有约定。
- **机制摘要**
  - 静态规则 `validateProcessGateGuards()` 读的是**文本**，最多钉住「调用出现在哪里」；「被调用的函数还在不在分类」
    是**语义**，文本规则看不到（消融实测：变异在位 ⇒ 112/112 构建期用例绿，§1.2）。
  - 所以本票**不新造静态规则**，而是把**已经存在、已被 #914 审查过**的真机判别器
    `run-stage-damaged-probe.ps1` 接进 CI，让它**真的被执行**；并把它覆盖不到的地方**写进守卫自己的
    docstring**（与残余 ④ 的「规则范围被高估」同型）。
  - 两条上游都没有 CI 接线 ⇒ 接线部分无上游可抄，取最小实现：一条 step + 一条 `paths` 过滤项。
- **契合点**（`AGENTS.md` §7）
  - 不变量 11（Sandbox / Permission 是 Runtime 边界，不靠 Prompt）：闸门与判别器都在安装器 runtime 边界，不改产品协议；
  - 不变量 21（Optional capability 故障不拖垮 Core）：闸门查不清时**中止安装器**而非放行；
  - 不新增依赖：探针要的 makensis（electron-builder 缓存）与 node 由**既有烟测步骤**建立，step 只排在其后。
- **判定**：`REUSE`（探针脚本、真机腿机制、变异判据全部复用 #914 已审查过的资产）+ `BUILD`（最小：一条 CI step
  + 一条 path 过滤项）。不引入新 Provider / 依赖 / 框架。
- **License**：不新增第三方代码。`app-builder-lib` MIT（**构建期工具**，非声明依赖，见 #914 §2 的 License 段）。
- **来源披露（§3.1）**：本领域已在 `docs/agents/reference-sources.md`「Windows 安装器 / 在占用进程处理」
  补录（#914 批次），本票不新增领域、不新增来源条目；两条来源与 #914 同源（同一领域）。本环境
  `WebFetch` 不可用（实测三次 `Model request failed`），故**未引任何网络来源**，也不编造读取日期。

## 3 决策与范围

- **D1**：采用**候选①（把真机判别器接进 CI）**，不采用候选②（再加一条静态规则）：静态规则本质是形状匹配，
  加规则只能覆盖它看得见的形状；而消融已证明「语义」这一类它看不见（§1.2）。
- **D2**：**不新增静态规则**；只把既有规则的锚从「分支的一种拼写」改成「变量本身」（残余 ④），
  并把规则的**覆盖边界**写进它自己的 docstring。
- **非目标（本票不动）**：残余 **①**（闸门无「目录归属」判据）——`iaProbeFound == "1"` 曾是的唯一归属信号
  随裁决 D6 一起消失，在一台「本机无安装、但 `$INSTDIR` 下恰好有 `resources\python\` 布局且有进程在跑」的机器上，
  答案 `11` 会 `Stop-Process -Force` 掉那些进程。这是**行为变更**，按 `AGENTS.md` §9.1.1 待用户裁决。
- **非目标（另开票）**：`customInit` 的 `${If} $iaRecoveryPending != "1"` 包装本身没有任何守卫（§6）。

## 4 实现

| 提交 | 文件 | 做了什么 | 依据 |
| --- | --- | --- | --- |
| `b352b106` | `.github/workflows/windows-installer-smoke.yml` | 新增真机腿 step（排在两条烟测之后，`if: !inputs.compile_only`）+ `paths` 增加 `desktop/test/harness/r1-drivers/**` | 残余 ②；§2 判定 |
| `b352b106` | `desktop/scripts/build-windows-installer.mjs` | `appBranch` 锚从字面量分支改为变量 `$iaProbeFound`；docstring 写明规则**看不见**什么 | 残余 ④ |
| `b352b106` | `desktop/test/installer-scripts.test.mjs` | 新增 Failure 用例：`StrCmp` 形式重拼的分支把调用移到其后 ⇒ 必须红 | 残余 ④ |
| `b352b106` | `desktop/test/harness/teeth-check-guards.mjs` | 变异锚随规则改；`GUARD_BASELINE_SHA256` 同笔更新 | 该 harness 自身要求 |
| `b352b106` | `desktop/installer/installer-directories.nsh` | 注释更正：闸门跑在**与它守的 move 同一条路径**上（`customInit` 的 `${If}` 同时跳过两者） | 残余 ③ |
| `b352b106` | `run-stage-damaged-probe.ps1` + `stage-damaged-probe.nsi` | 新增 `first-install` 腿（`$INSTDIR` 不存在） | 残余 ⑤ |
| `ed9f1e0a` | `run-stage-damaged-probe.ps1` | 新增 `damaged-mixed` 腿：**运行时 + 客户端同时在跑** ⇒ 必须拒绝且**一个都不杀** | 首轮 Axis A P3-1（§5.6） |
| `ed9f1e0a` | `desktop/scripts/build-windows-installer.mjs` | docstring 的「does NOT cover」列全三种规则接受、但会退回缺陷的形状，并写明各自被哪一层兜住；规则内联注释不再声称覆盖「每一种分支形式」 | 首轮 Axis A P4-1 / Axis B P4 |
| `ed9f1e0a` | `stage-damaged-probe.nsi` | 头注释改为「one or two real Windows processes」（`damaged-mixed` 腿留下两个进程） | 首轮 Axis A P3-1 |
| `ed9f1e0a` | `desktop/test/harness/teeth-check-guards.mjs` | `GUARD_BASELINE_SHA256` 随规则文本改而同步（`11e9d28f…` → `8f7bbb05…`） | 该 harness 自身要求（改了守卫文本就要同步基线） |
| `ed9f1e0a` | `.github/workflows/windows-installer-smoke.yml` | 去掉**两处**硬编码用例数（step 注释与 `paths` 注释）；**第三处**（「为什么必须在 CI 里跑」段里引 §1.2 消融读数的 `112/112 绿`）**保留**为读数记录——它是测量结果不是判据；该处的漂移风险按 §5.8 登记 | 首轮 Axis B P4 |

## 5 验证读数

### 5.1 冻结与 blob（协议 §8.1 第 1 条）

- base `42bb109ae91d9352c047f51dda834568bb1e826d`（tree `d514e1359a9f33b4d8694ec8e36f249ccb605a60`，= 当时的 `origin/main`）。
- 首轮冻结 tip `b352b1060d2c1361f71f250e0c5b4d20daa1c070` / tree `b0beba482ea2304121ff45f667729d0a306e2d2a`。
- 修复轮冻结 tip `ed9f1e0a75511f9ceae060e911b31c8d44420404` / tree `5ff56b5e5fbdfc6d2e8e6fb32e5a19787d3b9e7d`。
- 逐文件 blob（修复轮 tip；首轮与之不同的标在括号里）：
  `.github/workflows/windows-installer-smoke.yml` `c480a100`（首轮 `0bdc7872`）、
  `desktop/installer/installer-directories.nsh` `fc7fc0cf`、
  `desktop/scripts/build-windows-installer.mjs` `19332cfa`（首轮 `35d93450`）、
  `run-stage-damaged-probe.ps1` `73c1913e`（首轮 `8869c5d7`）、
  `stage-damaged-probe.nsi` `2558e601`（首轮 `24ac59ab`）、
  `teeth-check-guards.mjs` `66da5b47`（首轮 `cc976480`）、
  `installer-scripts.test.mjs` `5714bd72`。

### 5.2 消融红证 / 变异（`run-stage-damaged-probe.ps1 -DirectoriesSource`，生产宏原文注入）

在**主工作树之外**的副本（`C:\ia-probe-1074\abl`，`git clone --no-hardlinks`）里造变异，
用 `-DirectoriesSource` 指向副本的 `installer-directories.nsh`；主工作树的 `.nsh` 全程未被改动。

| 变异 | 构建期套件 | 真机腿（6 腿） |
| --- | --- | --- |
| M1 闸门体内提前返回（残余 ②） | **112/112 绿** | **红 9 条**（含 `damaged-mixed`、`damaged-runtime` 进程存活、三条 `answer=` 为空） |
| M2 分类器 12→11 塌缩（`-eq $iaAll.Count` → `-ge 1`） | **112/112 绿** | **红 1 条**：仅 `[damaged-mixed] the bundled runtime … was stopped` |
| M3 同上取恒真的 `-ge 0` | **112/112 绿** | **红 1 条**：同上 |

M2/M3 只有 `damaged-mixed` 腿看得见——单进程腿两种答案的退出码相同（都拒绝），分辨不出来。
这正是否掉首轮 Axis A P3-1 的读数（§5.7）。修复前（5 腿）M1 是 **7 条**断言红；加 `damaged-mixed` 后 **9 条**。

### 5.3 focused / 单测 / 真机腿

| 车道 | 命令 | 读数 |
| --- | --- | --- |
| 构建期规则套件 | `node --test test/installer-scripts.test.mjs`（`desktop/`） | **112 tests / 112 passed / 0 failed** |
| 守卫牙齿（变异） | `node test/harness/teeth-check-guards.mjs` | **存活变异 = 0**，逐字节还原 `identical=true` |
| 真机腿（装机文本） | `run-stage-damaged-probe.ps1` | 六腿全绿：`intact-clean exit=0 answer=0 probe=1`（被 rename 成 `.old-{guid}`）、`first-install exit=0 answer=0 probe=0`（不 stage、不建目录）、`damaged-runtime exit=0 answer=0 probe=0`（进程 `gone`）、`damaged-mixed exit=2`（两进程 `alive`）、`damaged-rootproc exit=2`（进程 `alive`）、`damaged-unknown exit=2` |
| 工作流语法 | `yaml.safe_load(...)` | 解析成功；`jobs.installer-smoke.steps` = 9；**制表符 0 个** |

### 5.4 Gate-0（隔离副本 `C:\ia-probe-914\gate-tree`）

- 首轮冻结树 `b352b106`：**5/6**，❌ 仅 `coverage`，其 ❌ 集合**恰为该笔**；收据
  `docs/gate/b352b1060d2c1361f71f250e0c5b4d20daa1c070.json`（`tracked_matches_head=true`、`untracked_total=0`）。
- 修复轮冻结树 `ed9f1e0a`：**5/6**，❌ 仅 `coverage`，❌ 集合 = `{b352b106, ed9f1e0a}`（**恰为本批两笔代码提交**）；
  收据 `docs/gate/ed9f1e0a75511f9ceae060e911b31c8d44420404.json`。
- **收尾树 `7abac831`（记账笔：证据 + 台账 4 行 + tracker / PHASE_STATUS / 月档）：6/6 PASS**——`coverage` 随台账行落盘转绿；
  收据 `docs/gate/7abac8310628b1670ac7d8f04a469eaf3acc80ce.json`（`tracked_matches_head=true`、`untracked_total=0`，墙钟 50.9s）。
- 满足协议 §8.2 第 4 条的「除 `coverage` 外全绿 + ❌ 集合 ⊆ 本批代码笔」判据 ⇒ 机械面清零后才派两轴。
- **为何在隔离副本里跑**：主工作树含另一条线的未提交改动（` M dev.sh`），`gate0.py` 的落盘前置会 fail-closed
  拒绝落盘（§8.7 第 3 条），而其处置建议明写「**不要** `git stash`」。副本为真仓 `git bundle` 克隆 + CI 同形依赖，
  `.pth` 指向副本自己的 `src`。**未触碰 `dev.sh`。**

### 5.5 全量门禁（冻结树 `ed9f1e0a`，隔离副本，单跑无并发）

单飞顺序跑（不并发：`#1069` 记录过后端套件与 playwright 同机并跑造成负载型假红）。

| 车道 | 命令 | 读数 |
| --- | --- | --- |
| 后端全量 | `.venv/Scripts/python.exe -m pytest -q -p no:randomly` | **7775 passed / 43 skipped / 51 deselected / 1 failed**（2037.90s） |
| desktop | `npm test`（`node --test`） | **288 tests / 286 passed / 2 skipped / 0 failed** |
| 前端单测 | `npx vitest run` | **119 files / 1681 passed / rc=0**（隔离复跑） |
| 前端构建 | `npx vite build` | **rc=0**，`✓ built in 47.11s`（只有 chunk > 500 kB 的既有告警） |
| E2E | `npx playwright test --workers=2` | **NOT_RUN**（见下） |

- **后端唯一红 = 台账机械瞬态**：`tests/tooling/test_review_coverage_immutable_ref.py::test_real_ledger_passes_after_the_431_fix`
  —— 该用例断言「**真实台账**跑覆盖闸门 exit 0」，本批台账行落盘前必然红（其 docstring 自述
  「修复笔钉死 431 行后，闸门必须绿」）。**与 `#914` 同一形状**（「唯一红 = 台账未落盘的机械瞬态」）。
  本批台账行落盘后此例转绿（§5.4 的 ❌ 集合 = `{b352b106, ed9f1e0a}`，恰为本批两笔代码提交）。
- **前端单测首轮 rc=1 是负载型假红**：首轮 84 files / 1442 passed + **35 个 `Unhandled Error`**，
  全部是 `[vitest-pool]: Failed to start forks worker … Timeout waiting for worker to respond`
  —— 即 **35 个测试文件根本没起来**（84 + 35 = 119）。隔离复跑 **119 files / 1681 passed / rc=0**，
  与 `#914` / `#1025` 记录的 1681/119 基线一致 ⇒ 首轮红是 worker 启动超时，不是用例失败。
- **E2E 为何 NOT_RUN**：`playwright.config.ts` 的端口是**单一字面量** `const PORT = 5173`
  且 `reuseExistingServer: false`；`preflight-port.mjs` 实测 5173 被**另一个 clone 的常驻
  vite dev server** 占用（PID 9620，仓库根 `…\.codex\worktrees\issue-729-windows-sandbox\intelligence-agent`），
  按 `#209` **拒绝复用**、e2e 未启动。端口无 env 覆盖 ⇒ 不能换端口。本批 `web/` 与 base
  **逐字节相同**（`git diff --stat 42bb109a..ed9f1e0a -- web/` 输出为空），该车道对本 diff 无信号；
  故如实记 NOT_RUN，不写成「绿」。

### 5.6 首轮独立审查（两轴，范围 `42bb109a..b352b106`）

两轴均为**非作者只读**子代理，各自独立、互不可见；自写变异都在主工作树**之外**的副本里做。

- **Axis A（Correctness/Spec）：FAIL，P0-P3 = 0/0/0/2 + P4×2**
  - **P3-1**：修复轮声称的判别器**仍分辨不出**分类器答案 12 与 11——五条腿里每条至多一个进程在安装根下，
    两种答案都以「拒绝」收场 ⇒ 把 12 弱化成 11 的变异（`-eq $iaAll.Count` → `-ge 1` / `-ge 0`）活过
    构建期规则、全部 112 条用例**和**五腿探针；生产上它的后果是「客户端 + 运行时同时在跑」时**先杀掉 bundled
    运行时再拒绝**，而设计是**拒绝且一个都不杀**（裁决 D1/D4）。
  - **P3-2**：`customInit` 的 `${If} $iaRecoveryPending != "1"` 包装本身**没有任何守卫**——把它取反
    （正常安装从此不 stage、不调闸门）能通过构建期规则、`installer-nsis.test.mjs` 与探针（探针只注入
    `installer-directories.nsh`）。**属既有缺口，非本 diff 引入**；登记为新残余（§6）。
  - **P4-1**：规则 docstring 的覆盖声明仍偏宽：实测两条「规则绿」的放置变异（把调用移到
    `StrCpy $iaFinalDirectory $INSTDIR` 之前；把分支改写成 `${FileExists}` 形式并把调用移进去）。
  - **P4-2**：无「方案依据」块（票面 AC 点名要求）。
- **Axis B（Standards/仓规）：FAIL，P0-P3 = 0/0/1/1 + P4×2**
  - **P2**：无「方案依据」块（票面 AC 明写「施工前按 §1.3 先做方案依据调研并落『方案依据』块」）。
  - **P3**：本 tip 尚无交付/覆盖记录（台账行、`docs/gate/<sha>.json`、tracker / PHASE_STATUS / 月档）——
    **属交付债，非缺陷**；并实测该 tip 的状态**如实**（提交信息未声称任何门禁 / 审查结论）。
  - **P4**：硬编码「112」两处会漂；docstring 写「reads」而机制是「首次文本出现（读或写）」。
- **两轴都取到结论行**，未使用 §8.3 第 4 / 5 条的替代通道。
- 两轴独立复核并确认：`GUARD_BASELINE_SHA256` 与守卫文本相符；首轮 `.nsh` 注释更正**逐字准确**
  （与 `installer.nsh:135-138` 一致）；`first-install` 腿**非空转**（结果文件缺失会失败，`answer=0` 只能来自
  `Pop $iaRunningProduct`）；CI step 不会静默跳过或错序；`main` 的保护只有 `gate0` 一项必需检查
  （`gh api .../branches/main/protection` 实测），故工作流头注释「不是必需状态检查」为真；
  （**§5.8 F4 更正**：本项首轮曾写「`r1-drivers/` worktree 为 LF、**统一**」，实测为混合）`r1-drivers/`
  目录的 **index 全为 LF**（`git ls-files --eol` 实测 10 个文件全 `i/lf`），worktree 是**混合**
  状态（6 个 `w/crlf` + 4 个 `w/lf`）——这是既有状态、非本笔引入（本笔改动的两个文件
  `run-stage-damaged-probe.ps1`、`stage-damaged-probe.nsi` 在 base 与 tip 均为 `w/lf`，blob 均 LF，
  无整文件 EOL 重写）。

### 5.7 修复轮（`ed9f1e0a`）：处置与红证

| 首轮 finding | 处置 |
| --- | --- |
| Axis A P3-1（12/11 分辨不出） | **加 `damaged-mixed` 腿**；M2/M3 实测：构建期 112/112 绿、**只有该腿**红（§5.2） |
| Axis A P4-1（docstring 覆盖声明偏宽） | docstring 的「does NOT cover」列全三种形状 + 各自兜底层；规则内联注释不再声称覆盖「每一种分支形式」 |
| Axis B P4（硬编码 112 / 「reads」措辞） | 去掉**两处**用例数（第三处保留为读数记录，§4）；「reads」措辞**登记不修**——修它要引入修后重审之后的代码提交，需独立审查轮（§5.8 末段） |
| Axis B P2 + Axis A P4-2（无方案依据块） | 本文件 §2 |
| Axis B P3（交付记录） | 最终记账笔（台账行 + 收据 + tracker / PHASE_STATUS / 月档） |
| Axis A P3-2（`customInit` 包装无守卫） | **本票不修**（非本 diff 引入、且要改 `installer.nsh`）；登记为残余并另开票（§6） |
| 残余 ①（无目录归属判据） | **本票不动**（行为变更，待 §9.1.1 裁决） |

### 5.8 修复轮修后重审（两轴，范围 `b352b106..46e150b0`）

按 §8.3 第 4 条：修后重审每轴 1 轮，两轴都必须关掉既有 finding、且不得零发现。派发预算（§8.3 第 1 条）：
目标 tip `46e150b0`、树 `88d4ff3f…`、每轴报告上限 12 条、墙钟 60 min、变异上限 5 条/轴、**无论如何必须给结论行**。
只读（§8.3 第 2 条）：变异一律在**主 worktree 之外**做副本，收尾报 `git status --short` + `git hash-object`。

- **Axis A（正确性 / 规格一致）：`APPROVE_WITH_FINDINGS`，P0–P3 = 0/0/0/0 + P4×1**
  - 四条首轮 finding 的处置**逐条实测复验成立**（不是采信自述）：
    - **P3-1**（分类器分辨不出 12 与 11）：自写变异 M2（`-eq $iaAll.Count` → `-ge 1`）⇒ 构建期规则**绿**、
      真机腿**只有 1 条红**（`[damaged-mixed]`），其余五腿全绿 ⇒ 生产后果（先杀运行时再拒绝）确被该腿唯一捕获，修复成立。
    - **P4-1**（docstring 覆盖声明）：逐形状实测——shape2（分支改写成 `${FileExists}` 且调用移入）⇒ 规则**绿**、
      `#1045` 语句表**红**、真机腿 **10 条红**；shape3（调用移到 `StrCpy $iaFinalDirectory` 之前）⇒ 规则**绿**、真机腿 **7 条红**。
      两条「规则绿」的形状确被声明的兜底层兜住 ⇒ 声明准确。
    - **P4-2 / Axis B P2**（方案依据）：§2 的 ≥2 来源**独立复核相符**（`app-builder-lib@26.15.3` 的
      `allowOnlyOneInstallerInstance.nsh` 存在且 `templates/nsis` 下 test/spec = 0；DSH `5badb150` 的
      `.github/workflows` 命中 `installer|makensis|nsis` = 0）。
    - **P3-2**（`customInit` 包装无守卫）：「**非本 diff 引入**」成立——`git diff --name-only b352b106~1 46e150b0
      -- desktop/installer/installer.nsh` 为空，`:136-138` blame 到 `387948595`（#915/#1023/#1034，早于本批）
      ⇒ 登记为残余 + 另开票成立。
  - 自写变异 4 条，**失败集合互不相同**（满足 §8.3 第 4 条「覆盖不同检查点且失败集合不同」）：M1 闸门体内提前返回
    ⇒ 真机腿 **9 红**；M2 12→11 塌缩 ⇒ 仅 **1 红**；shape2 ⇒ 规则绿 / `#1045` 表红 / 腿 **10 红**；shape3 ⇒ 腿 **7 红**。
  - 基线六腿全绿，与 §5.3 一致。
  - **新 finding（P4）**：`windows-installer-smoke.yml` 的「为什么必须在 CI 里跑」段仍硬编码「112/112 绿」，
    与本笔「去掉硬编码用例数」的自述不一致（同 Axis B F2）。
- **Axis B（仓规 / 标准）：`APPROVE_WITH_FINDINGS`，P0–P3 = 0/0/0/1 + P4×3**
  - **F1（P3，交付债、非缺陷）**：本 tip 尚无台账行 / tracker / PHASE_STATUS / 月档条目——已在 §4 / §5.7 声明为
    「最终记账笔」，属已声明的顺序；台账未落盘前 `test_real_ledger_passes_after_the_431_fix` 仍红，与 §5.5 一致。
    **本轮末尾的记账笔即其处置**。
  - **F2（P4）**：`windows-installer-smoke.yml` 仍有一处硬编码「112」（基线共 3 处，修复只清 2 处）⇒ 与 §4 / §5.7 自述不一致。
  - **F3（P4）**：规则内联注释未写明「锚是**首次文本出现**」；抛出的错误信息仍写 `reads $iaProbeFound`（机制是「首次文本出现（读或写）」）。
  - **F4（P4）**：§5.6 的 EOL 断言不准确（首轮写「worktree 为 LF、统一」，实测混合）——**已就地更正**（§5.6 已加标记）。
  - 自写变异 3 条：M1 把 `probeUse` 锚退回字面量 `${If} $iaProbeFound == "1"` ⇒ 套件 **111/112，唯一红是新用例**
    「refuses a gate call that a re-spelled app-executable branch moves behind it」⇒ **新用例对锚点改动有鉴别力、
    首轮所述 `StrCmp` 绕过已被堵住**；M2 在副本 `.nsh` 的函数体首行插入提前返回 ⇒ 构建期 **112/112 全绿**（独立复现 §1.2）；
    M3 `teeth-check-guards.mjs` ⇒ 存活变异 0、`identical=true`。
  - Scope Lock：本批仅动 7 个代码文件 + docs；`dev.sh` 未被本批触碰（`git log/diff 42bb109a..HEAD -- dev.sh` 为空）；无顺手重构。
    EOL/BOM：全部改动文件 `i/lf`、无 BOM、无整文件 EOL 重写。`DOC_PATTERN`：新增 docs 仅 `.json` / `.md`。
    License：未引入第三方代码；`app-builder-lib` 实测 `26.15.3` / MIT / 不在 `desktop/package.json`、不在 `THIRD_PARTY_NOTICES.md`
    ⇒ §2 说法准确。提交卫生：`46e150b0` 如实记 e2e `NOT_RUN` 并留 §5.8 / §6 占位。
- **两轴都取到结论行**，未使用 §8.3 第 4 / 5 条的替代通道（不自行判通过）。
- **F2 / F3 的处置（登记不修）**：两条均为 P4（注释 / 文案）。修它们会引入**修后重审之后的代码提交**；该提交在覆盖闸门里
  必须由一次新审查归属（`scripts/check_review_coverage.py` 的 docs-only 只认 `docs/**` 等路径，`.github/workflows/*.yml`
  与 `desktop/**` 都不算），而 §8.3 第 4 条把修后重审封顶在每轴 1 轮 ⇒ 为两处 P4 文案花掉「一票一次」的发现阶段补审配额
  不合比例（§9.2）。故**如实登记、本票不改**；§4 / §5.7 的相关自述已改为逐处准确（两处已去、第三处保留为读数记录）。

## 6 残余与后续

| # | 残余 | 归属 | 状态 |
| --- | --- | --- | --- |
| ① | 闸门无「目录归属」判据：`iaProbeFound == "1"` 曾是唯一归属信号；在一台「本机无安装、但 `$INSTDIR` 下恰好有 `resources\python\` 布局且有进程在跑」的机器上，答案 `11` 会 `Stop-Process -Force` 掉那些进程 | #914 修复轮登记，**本票不动**（行为变更） | **待用户裁决（§9.1.1）** |
| ② | `customInit` 的 `${If} $iaRecoveryPending != "1"` 包装本身无守卫：取反它（正常安装从此不 stage、不调闸门）能通过构建期规则、`installer-nsis.test.mjs` 与探针（探针只注入 `installer-directories.nsh`） | 首轮 Axis A P3-2；**非本 diff 引入**（`installer.nsh:136-138` blame 到 `387948595`） | **已另开票 [#1078](https://github.com/EricKingWhy/intelligence-agent/issues/1078)** |
| ③ | 修后重审 F2（P4）：`windows-installer-smoke.yml` 保留一处硬编码「112」（读数记录） | 本轮修后重审 Axis A P4 / Axis B F2 | **登记不修**（理由见 §5.8 末段） |
| ④ | 修后重审 F3（P4）：规则内联注释 / 错误信息未写明锚是「首次文本出现」 | 本轮修后重审 Axis B F3 | **登记不修**（同上） |

E2E 车道 NOT_RUN（§5.5）——端口 5173 被另一 clone 的常驻 dev server 占用；本批 `web/` 与 base 逐字节相同，
故该车道对本 diff 无信号。集成后由 CI 的 `windows-installer-smoke`（PR 上跑）覆盖真机腿。
