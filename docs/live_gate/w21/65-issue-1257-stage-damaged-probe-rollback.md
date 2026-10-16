# #1257 真机腿修复：stage-damaged-probe 注入 iaRollbackApplication（CI 修复）

> **PR #1257**（分支 `codebuddy/1251-silent-autorecovery`，承载 #1251 静默自动恢复）的
> `windows-installer-smoke` 红项修复（用户亲口裁决：**方案 A**）。
>
> 本文件记录：根因、改动集、本地 makensis 红→绿（A/B）、六套守卫套件读数（本机 + 沙箱，均含 pristine 对照）、
> 两轴独立审查结论与 P4 处置、以及 gate0 收据指针。**本文件不宣布 Gate 通过、不 merge。**

---

## 0 执行器与锚点

| 项 | 值 |
| --- | --- |
| 仓库 / 分支 | `/home/hatch/workspace/intelligence-agent-wt/1251-silent-autorecovery` / `codebuddy/1251-silent-autorecovery` |
| 施工前 tip | `853e7ff6d64ba48572f2a2544f6c2258400a823d`（= origin/main `781733e8` + 已做的 sync merge `651a4bf3` 之后） |
| 修复提交 | `a96ffb052a20c17d647fb7d072762230b2ff41f7`（父 = `853e7ff6`） |
| 审查范围 | `853e7ff6..a96ffb05`（仅 1 笔提交，只改 2 个 harness 文件） |
| 被审 blob | `stage-damaged-probe.nsi` = `5a4ef51237836fb4ab6f578026188d68092d8497`；`run-stage-damaged-probe.ps1` = `376ee34f2790ba4e3aa96f0b97564408754ffb8b` |
| 本机工具 | `makensis` `/usr/bin/makensis` **v3.09**；node v24.20.0；**无 pwsh**（runner 是真机腿，本机跑不了） |
| 沙箱 | 云端 Docker `shell/ia-test`（node v22.22.1 / python 3.14），**Agent 不入箱** |

---

## 1 根因（三方已交叉验证，本次仅本地复现确认）

`desktop/installer/installer-directories.nsh` 的 `!macro iaRecoverInterruptedInstall` 静默分支
（`iaInterruptedSilent:`，由 #1251 引入）现在 `Call iaRollbackApplication`：

```
iaInterruptedSilent:
  DetailPrint "…silent auto-rollback to $iaBackupDirectory"
  Call iaRollbackApplication
  ${If} $iaRollbackStatus != "restored"
    … SetErrorLevel 2 / Quit
  ${EndIf}
  Goto iaInterruptedDone
```

`desktop/test/harness/r1-drivers/stage-damaged-probe.nsi` 经标记 `@@PRODUCTION_IA_RECOVERY@@`
**逐字注入**该 macro，但模板里**没有** `Function iaRollbackApplication` 定义 ⇒ `makensis`：

```
warning 6000: unknown variable/constant "iaRollbackStatus" detected, ignoring
Error: resolving install function "iaRollbackApplication" in unnamed install section (0)
Error - aborting creation process
```

⇒ `windows-installer-smoke` 的「真机腿」（`run-stage-damaged-probe.ps1`）整条编译失败。
**跨文件语义冲突，非 Git 文本冲突**（sync merge `651a4bf3` 零冲突）。

---

## 2 改动集（方案 A，测试 harness 专用，产品代码零改动）

`git diff 853e7ff6..a96ffb05` = **2 文件 / 8+ / 1-**：

1. `stage-damaged-probe.nsi`：`Var iaPowerShellPath` 后加 `Var iaRollbackStatus`、`Var iaLeftoverDirectory`；
   `@@PRODUCTION_IA_REQUIRE@@` 后加标记 `@@PRODUCTION_IA_ROLLBACK@@`。
2. `run-stage-damaged-probe.ps1`：`$backupMacros` 追加 `!macro iaReadLeftoverDir` 提取；
   `$replacements` 增加 `'@@PRODUCTION_IA_ROLLBACK@@' = Get-ProductionBlock … 'Function iaRollbackApplication' 'FunctionEnd' …`。

**严格复用**既有机制（同一 `Get-ProductionBlock` / `$replacements` ordered dict / `$backupMacros` 拼接 /
`@@PRODUCTION_IA_*@@` 约定 / `[regex]::Matches(...).Count -ne 1` 断言），与先例
`run-rollback-gate-probe.ps1:106-120`（同一函数、同一四宏清单）逐行同构。

---

## 3 本地 makensis 红 → 绿（A/B）

本机无 pwsh，runner 的替换逻辑（纯字符串）在临时目录忠实复刻（脚本 `/tmp/ia1257-probe/build-probe.py`，
逐行对照 `run-stage-damaged-probe.ps1:115-151` 的 `Get-ProductionBlock` 与 `$replacements`），
`makensis -V2` 编译（BOM 写入与 runner 一致：`[System.Text.UTF8Encoding]::new($true)`）：

| 变体 | 源码 | 读数 |
| --- | --- | --- |
| 修前（`853e7ff6` 模板 + 旧替换集，无 `@@PRODUCTION_IA_ROLLBACK@@`） | — | **exit 1**：`Error: resolving install function "iaRollbackApplication"` + `warning 6000: …"iaRollbackStatus"`（**缺陷复现**） |
| 修后（`a96ffb05` 模板 + 新替换集） | HEAD | **exit 0**，产出 `stage-damaged-probe.exe` |
| `-Expect1165Regression` 路径 | 历史基线 `5497abdd`（`PRE_1165_BASE_SHA`） | **exit 0**（基线含 `Function iaRollbackApplication`:484 与 `!macro iaReadLeftoverDir`:237 ⇒ 不触发 runner 的 `throw`），仅多 `warning 6010: install function "iaRollbackApplication" not referenced`（warning 非 error，runner 用 `/V2` 非 `/WX`） |

> 残余 warning（`7025 LANG_ENGLISH`、`6001 iaDeleteBase`）为**既有**（修前模板同样命中），非本次引入。

**两轴审查各自独立复核了本表**（Correctness 轴复刻后自跑得到同形读数；Standards 轴用自写变异证明
两个新增承重：M1 不 harvest `iaReadLeftoverDir` ⇒ `!insertmacro: macro named "iaReadLeftoverDir" not found!`；
M2 删两行 `Var` ⇒ `Usage: StrCpy $(user_var: output)…` 硬错）。

---

## 4 受影响测试读数（六套 desktop 守卫套件）

本改动落在 `desktop/test/**`（`verification.map.tsv` 的 `desktop/` 行面：`diff-check|coverage|guards`，
该行「focused 为空」并注明改动 `desktop/` 时须手动跑 `npx tsc --noEmit` 与 `npm test`；本机/沙箱均缺
`desktop/node_modules`，故以 `node --test` 直跑六套套件）。

| 环境 | 总数 | pass | fail | skipped | 失败项 |
| --- | --- | --- | --- | --- | --- |
| 本机 node v24 | 203 | **195** | 5 | 3 | `installer-tui`×4 + `installer-web-assets`×1（Windows 路径语义） |
| 沙箱 node v22（修复树） | 191 | 181 | 7 | 3 | `installer-node-runtime`（整文件）+ `runtime lockfile product pin` + `installer-tui`×1 + `installer-web-assets`×1 |
| 沙箱 node v22（**pristine `HEAD`** 对照） | 191 | 181 | 7 | 3 | **失败集合逐条相同** |

**受影响套件单独读数**：`installer-nsis.test.mjs` **13/13 pass**、`installer-scripts.test.mjs` **131/131 pass**。

**这不是本次改动引入的红（隔离证据 = A/B 对照）**：
- 本机：`git stash` 掉两文件（pristine）复跑 `installer-tui` + `installer-web-assets` ⇒ **同样 15 pass / 5 fail**。
- 沙箱：`git archive HEAD desktop` 快照复跑六套 ⇒ **191/181/7/3，失败集合与修复树逐条相同**。
- 归因：`assertTuiRuntimeClosure` / `assertBundledWebAssets` / node-runtime 锁文件断言均为 Windows 路径
  语义与缺构建产物（`desktop/dist`、`installer/staging`）所致；与 `AGENTS.md` 末条（沙箱需 CI 同形依赖）
  及 tracker #1165 记录（「五个 Windows 路径失败在 pristine main 同样复现」）一致。CI 跑在 `windows-latest`，
  这几条按设计通过/跳过。

---

## 5 两轴独立审查（实现者不自审；各起 fresh 会话）

- 执行器：CodeBuddy CLI / `deepseek-v4.1-flash`，`HOME=/home/hatch/workspace/system/cb-isolation/1257-merge-mamaA`
  （隔离 HOME，妈妈A 凭据；探针「回一个字：好」无 429；日志 uid `38916e1e-c8f1-443d-9ada-9353c238deda` /
  phone `13651179675` 与 `map.tsv` 妈妈A 登记**逐字符一致**）。
- 两轴上下文互不污染（独立进程、独立会话文件）。收尾主工作树 `git status --short` **空**（未污染）。

| 轴 | 结论 | P0 | P1 | P2 | P3 | P4 |
| --- | --- | --- | --- | --- | --- | --- |
| Correctness | `PASS-WITH-FINDINGS` | 0 | 0 | 0 | 0 | 2 |
| Standards | `PASS-WITH-FINDINGS` | 0 | 0 | 0 | 0 | 2 |

### P4 处置（逐条，如实）

| 编号 | 轴 | 要点 | 处置 |
| --- | --- | --- | --- |
| P4-1 | Correctness | 新注入在本探针里是**编译期**覆盖：没有任何腿 arm 一个**存在且合法**的备份 + 损坏安装 ⇒ `Call iaRollbackApplication` 与 `$iaRollbackStatus != "restored"` 分支解析得到但**运行期从不执行**。该腿能抓 #1251 的**符号/编译**缺陷，抓不了**运行期**缺陷 | **report-only（未改）**：审查者自述「No change needed for this commit's goal; noted for #1251 evidence completeness」。补一条运行期腿属**新范围**（改腿的运行行为、且只能 Windows 验证），按 §9.1.1 不擅自扩范围；登记为 **#1251 的 CI 覆盖待办**（候选新票） |
| P4-2 | Correctness | `run-stage-damaged-probe.ps1:134,142` **无条件**从 `-DirectoriesSource` 提取 `iaReadLeftoverDir` / `iaRollbackApplication`；若某历史修订早于 #361/#901 会 `throw`（而非产出红证） | **report-only（未改）**：审查者自述「No live breakage：两个已记录的红证修订都含二者（1105ebd1:214/439；5497abdd:237/484）」；且该 `throw` 与**相邻 3 个备份宏及另 5 个替换**的既有 fail-loud 形状一致，只给新的两条加 presence 门会引入不一致（§9.3/§9.5）；无实际破损 |
| F1 | Standards | 两份 runner 的备份宏**定义顺序**不同（stage: Clear/Read/Stash/ReadLeftover；rollback: Stash/Clear/Read/ReadLeftover） | **report-only（未改）**：审查者自述「建议最小修法：**不改**（改顺序本身是无关改动），勿重排」；`!macro` 定义顺序对 `makensis` 无影响（两轴均实测），且该差异部分**先于本提交**（stage 原本即 Clear/Read/Stash） |
| F2 | Standards | 模板注释枚举未含新注入的 rollback 函数（`stage-damaged-probe.nsi:75-76`） | **report-only（未改）**：审查者自述「改动**前已非穷尽**（更早注入的 `iaRecoverInterruptedInstall` 同样未列），本提交沿袭而非引入」，且建议为「可选」。为守 §9.3（每行可追溯、不改无关注释）与避免为一个**先于本提交**的注释摘要再开一轮 review，本次不改 |

> **总结**：两轴 **P0–P3 全 0**；四条 P4 **全部**为审查者自评「no change needed / 可选 / 不改」，
> 且均**先于或超出**本提交范围（F1/F2 先于；P4-1/P4-2 属新范围/一致性）。故四条**全部 report-only**，
> 逐条给出理由与出处，未以任何形式 waive。

---

## 6 冻结读数可传递性与门禁

- 代码面冻结 = `a96ffb05`（tree 见 `docs/gate/<sha>.json`）。其后只追加 `docs/**`（本证据 + 台账 + gate 收据）。
- gate0（本机 6 条机械车道）在沙箱 `shell/ia-test` 以 **CI 同形**依赖跑（`git bundle` 真仓克隆 +
  `uv sync --frozen` + `pnpm install --frozen-lockfile`），读数落盘 `docs/gate/<sha>.json`。
- `scripts/check_review_coverage.py`：本笔 `a96ffb05` 的归属行见 `docs/review_ledger.d/1257-*`（两轴各一行）。

---

## 7 诚实缺口

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 真机腿的**运行期**读数（各腿 exit code / 结果文件） | **本机未取到** | 需 Windows + electron-builder 缓存的 makensis + 真进程语义；本机仅 makensis 3.09 编译，运行期留 **CI `windows-installer-smoke`** 判定 |
| `windows-installer-smoke` 全绿 | **待 CI** | push 后在 PR #1257 见；本文件不预先宣布 |
| #1251 静默自动回滚分支的**运行期** CI 覆盖 | **缺**（P4-1） | 该探针无 arm 合法备份的腿；真机验证见 `63-issue-1251-silent-autorecovery.md`（人工）。登记为 #1251 待办 |
| Tracker / `PHASE_STATUS.md` 更新 | **未做** | 本波为协调员管理的合并波，本线按分支约定只落证据 + 台账；集成阶段由协调员回写 |

---

## 8 证据路径

- 本文件（权威读数，入库）：`docs/live_gate/w21/65-issue-1257-stage-damaged-probe-rollback.md`
- 修复提交：`a96ffb05`
- 归属行：`docs/review_ledger.d/1257-stage-probe-rollback-axis-a-correctness-853e7ff6-a96ffb05.tsv`、
  `docs/review_ledger.d/1257-stage-probe-rollback-axis-b-standards-853e7ff6-a96ffb05.tsv`
- gate0 收据：`docs/gate/<a96ffb05 的 HEAD sha>.json`（push 前落盘）
- 本地 scratch（**未入库**，旁证）：`/tmp/ia1257-probe/{build-probe.py,probe-before.nsi,probe-after.nsi,guards-*.log,review-*.out}`

---

## 9 逐节 V3.1-lite 合规声明

- **§1.1 / §3 启动检查表**：任务来自派工书（六 PR 合并波 · #1257 修复）。已实际读取 `AGENTS.md`、
  `docs/SDD_WORKFLOW_PROTOCOL.md`（全文 929 行）、`docs/SDD_TICKET_TRACKER.md`（当前态检索）、
  `SPEC_ROOT/00_PROJECT_VISION.md`、`SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`、
  `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md`、`docs/agents/{implementation-discipline,review-debug-playbook,issue-tracker}.md`、
  `desktop/installer/installer-directories.nsh`、`desktop/test/harness/r1-drivers/{stage-damaged-probe.nsi,run-stage-damaged-probe.ps1,run-rollback-gate-probe.ps1}`、
  `.github/workflows/windows-installer-smoke.yml`。**如实缺口**：本机无 `gh` 凭据，`gh issue view 1257` 未执行，票面以派工书为准。
- **§1.2 / §9（TDD/Tests）**：本票为**测试 harness 缺陷修复**（产品零改动）。红→绿 = §3 的 makensis A/B；
  承重 = 两轴各自独立复核（含 Standards 轴 M1/M2 变异）。
- **§2 / §8.2–§8.3（双轴审查）**：两轴 fresh 会话，实现者不自审，P0–P3=0，P4 全 report-only（逐条出处见 §5）。
- **§4.4（施工授权）**：仅改派工书点名的 2 个 harness 文件；产品 / 配置 / 锁文件零改动。
- **§8.1 第 6 条（造红隔离）**：所有变异 / 复刻编译在**主工作树之外**（`/tmp/ia1257-probe`、审查者各自的 `/tmp/*`）；
  收尾主工作树 `git status --short` 空。
- **§8.9（写盘纪律）**：本中文长文用 `Write` 工具直接落盘，未用 `python -c`/`bash` 内联承载正文。
- **派工书「诚实红线」**：未编造、未 waive；四条 P4 全部给出处置与出处，未假装「零 finding」。

---

*生成于 2026-10-16/17（本机时间）；执行者：CodeBuddy CLI / deepseek-v4.1-flash；授权范围：`#1257` 真机腿修复 + 验证 + 台账。*
