# #1251 静默重跑自动恢复中断安装（真机验证）

> Issue #1251：静默 `/S` 重跑检测到被中断的安装（`.old-*` 备份存在）时，应**自动回滚 + 全新安装（exit 0）**，
> 而非 exit 2 fail-closed。修复位置：`desktop/installer/installer-directories.nsh` 的
> `iaRecoverInterruptedInstall` 宏 `iaInterruptedSilent` 分支。
>
> 本文件在**一台 Windows 真机**上，用**本仓 `C:\ia-1251` 源码重新构建的安装件**复跑两条判据：
> **场景 A**（中断后静默重跑应 exit 0）、**场景 B**（备份缺失应 exit 2 fail-closed）。
>
> **本文件不宣布任何 Gate 通过、不 merge、不关单。** 失败/偏差如实记录。

---

## 0 执行器与环境

| 项 | 值 |
| --- | --- |
| 机器 | 腾讯云 Windows Server 2022 Datacenter，build **20348**，version 10.0.20348，x64，主机名 `10_1_0_15` |
| 执行方式 | 真机上的 CodeBuddy CLI（`codebuddy --model deepseek-v4.1-flash`）经 SSH 驱动；`qwinsta` 显示当前在 **会话 0**（`>services`，断开），console 会话 1 已连接但无登录用户 ⇒ **无可用交互桌面** |
| PowerShell | **7.2.2** |
| 源码 | `C:\ia-1251`（**tar 包解包，非 git 工作副本**；本机无 `.git`，提交 / 开 PR 在 Linux 侧做） |
| 工具链 | node v22.23.3 / npm 10.9.9 / pnpm 12.11.2（corepack）/ host python 3.13.7 / uv 0.13.1 |
| 安装目录 | `%LOCALAPPDATA%\Programs\Intelligence Agent`（per-user） |
| 用户数据 | `%APPDATA%\intelligence-agent` |
| 安装记录键 | `HKCU\Software\ad4c8292-94ef-5e7e-addf-1a5d7241abe1`（存放 `IaBackupDir` / `IaLeftoverDir`） |

### 对照基线（前序证据）

`docs/live_gate/w21/62-issue-1227-installer-interruption-legs.md` §3.1 **变体 A** 记录的正是同一状态
（`.old` 一出现即杀、live 目录空、`IaBackupDir` armed、live app exe 缺失），当时**修复前**的读数 =

> `/S` 重跑 **exit 2 / 1.0 s**（`iaRecoverInterruptedInstall` 的 `IfSilent ⇒ SetErrorLevel 2 + Quit`）。

本文档即在该状态的同一台机器上，用 #1251 修复后的安装件复跑，判据为 **exit 0**。

---

## 1 源码与 #1251 修复在场

`C:\ia-1251\desktop\installer\installer-directories.nsh` `iaRecoverInterruptedInstall` 宏
`iaInterruptedSilent` 分支（272–285 行）为修复版：

```
iaInterruptedSilent:
  ; #1251: silent mode auto-recovers instead of requiring interactive
  ; recovery. Roll back to the backup, then fall through so
  ; iaStageApplication does a fresh install. Only fail closed when the
  ; rollback itself cannot be proven safe (backup shape/missing already
  ; checked above; iaRollbackApplication refuses the rest).
  DetailPrint "iaRecoverInterruptedInstall: silent auto-rollback to $iaBackupDirectory"
  Call iaRollbackApplication
  ${If} $iaRollbackStatus != "restored"
    DetailPrint "iaRecoverInterruptedInstall: silent auto-rollback failed"
    SetErrorLevel 2
    Quit
  ${EndIf}
  ; Rollback succeeded: backup cleared, $INSTDIR points at the restored
  ; directory. Fall through to iaStageApplication for a fresh install.
  Goto iaInterruptedDone
```

判据（派工书）：搜索串 **`iaRecoverInterruptedInstall: silent auto-rollback`** —— **命中**
（`installer-directories.nsh:276`）。

调用链核对（`installer.nsh:124-139`）：`customInit` 先 `!insertmacro iaRecoverInterruptedInstall`，随后
`${If} $iaRecoveryPending != "1"` 时 `!insertmacro iaStageApplication`。修复分支**不置** `$iaRecoveryPending`
（只有交互分支 `268` 行 `StrCpy $iaRecoveryPending "1"` 才置位）⇒ 回滚成功后**确实落回**
`iaStageApplication` 走全新安装。

---

## 2 构建安装件

按 #1226 的成功路径构建（`desktop/` 下）：

```
python desktop/scripts/prepare_node_runtime.py
python desktop/scripts/prepare_python_runtime.py
cd web    && corepack pnpm run build        # tsc -b && vite build
cd tui    && npm run build                  # tsc -p tsconfig.json
cd desktop && npm run build                 # tsc -p tsconfig.json
cd desktop && node scripts/build-windows-installer.mjs
```

**为规避慢网/镜像差异所做的最小前置（脚本预置，未改源码，与 #1226 §0 同法）**：

- 下载件按 lockfile 的 SHA-256 预置进 gitignored 的 `.scratch/python-runtime-cache/{downloads,wheelhouse}` 与
  `.scratch/node-runtime-cache/downloads`（脚本随后打印 `[skip] … (sha256 ok)`）；
- `desktop/`、`web/`、`tui/` 的 `node_modules` 复用自同机 `C:\ia`（**只读复制**）——三份依赖清单
  （`package.json` / `package-lock.json` / `pnpm-lock.yaml` / `pyproject.toml` / 两个运行时 lock）**经 `diff -q`
  逐字节相同**，故依赖树等价；
- electron / electron-builder / 7zip / nsis 二进制命中**全局缓存** `%LOCALAPPDATA%\electron\Cache` 与
  `%LOCALAPPDATA%\electron-builder\Cache`（非仓库内）；
- `electron-builder@26.15.3` 随 `C:\ia\desktop\node_modules` 一并可用（**未**改 `desktop/package.json`）。

> **未修改 `C:\ia`**：仅从中**读取**（复制出）依赖树与缓存。本票对 `C:\ia-1251` 的写入仅限 gitignored 的
> `.scratch/**`、安装器自身构建产物、本证据文档。

**产品载荷确为本仓新源码**：`prepare_python_runtime.py` 以 `uv build --wheel` 从 `C:\ia-1251` checkout
重建产品 wheel，`sha256 = 6b1b250182109a2d3802f4581a8b90a8b4ac6ff16347d8c25663fce4ae9d8f88`（与 #1226 的
`8275e9e1…` **不同**），且 `install_product` 走 `--force-reinstall`；`installer-build.json.productCodeSha256`
= `01488260…`（#1226 为 `7e2237a5…`）——证明安装件携带的是本仓产品代码，而非 `C:\ia` 的旧载荷。

构建期自检（脚本内）全部通过并通过 electron-builder 打包（NSIS/`makensis`）。产物：
`desktop/dist-installer/Intelligence-Agent-Setup-0.1.0.exe`。

> 说明：`Intelligence Agent.exe` 自身的 sha256（`db4127ca…`）与 #1226/#1227 相同——该 exe 是 Electron
> 外壳存根，产品代码在 `resources/app`（`productCodeSha256` 与产品 wheel sha 已证明其为新源码），故外壳
> 一致属预期，不作为载荷陈旧的反证。

---

## 3 构建读数

| 字段 | 值 |
| --- | --- |
| 产物路径 | `desktop/dist-installer/Intelligence-Agent-Setup-0.1.0.exe` |
| 字节数 | **213,389,835** |
| sha256 | **ae52282f1eeece70d90095a898bd31934c9b0c7069ba15af6293d447e1983879** |
| 版本 / appId | `0.1.0` / `com.intelligence-agent.desktop` |
| `lockfileSha256` | `644fa21e0dfd2c0c37b30289852225345aa90cc08c51989a2946f18f7efc0197` |
| `nodeLockfileSha256` | `1a2e7800b68eef1b6fedd777094d596213c8df56698ab7b4532258790ecd5142` |
| `productCodeSha256` | `01488260dfaf793c2037a3fc270744b41d9a2c7cc7879b2873d45655832d464a` |
| `builtAt` | `2026-10-16T13:21:06.681Z` |
| 产品 wheel sha256 | `6b1b250182109a2d3802f4581a8b90a8b4ac6ff16347d8c25663fce4ae9d8f88` |

> `SHA256SUMS.txt` 与实测 `sha256sum` **一致**（`ae52282f…`）。

---

## 4 测试环境前态（基线清理）

机器上留有 **#1227 测试遗留**：一个完整 0.1.0 安装 + 一个 `.old-{BDFD48EF…}`（完整旧版，785 MB）+
`IaLeftoverDir` 记录。为使场景 A 的「全新安装」有意义，先把机器置于干净前态：

| 步骤 | 命令 | 读数 |
| --- | --- | --- |
| 前态 | `Get-OldDirs` / `Test-Path $InstallDir` / 注册表 | 安装目录**存在**（13,641 文件 / 785,152,496 B，exe `db4127ca…`）；`.old-{BDFD48EF…}` **1 个**；`IaLeftoverDir` 已设 |
| 卸载 | `& "$InstallDir\Uninstall Intelligence Agent.exe" /S` | **exit 0**；安装目录消失；`.old-*` 被**名字 sweep** 清空（count 0）；`HKCU\Software\ad4c8292-…` 键被移除 |
| 用户数据 | `Get-DataFingerprint` | 前后同为 `52bb24da138b169e056c1e088085bab4e91def7895b6dc56c192e5b5ab035164 (files=98)` ⇒ **未变** |

> 清理脚本：`.scratch/evidence/w1251/00-baseline.ps1`；日志 `.scratch/logs/w1251/00-baseline.log`。

---

## 5 场景 A — 中断后静默重跑应 exit 0

驱动脚本：`.scratch/evidence/w1251/{common.ps1,10-scenarioA1-fresh.ps1,11-scenarioA2-interrupt.ps1,12-scenarioA3-recover.ps1}`；
日志同名于 `.scratch/logs/w1251/`。

### A1 `/S` 全新安装

命令：`Start-Process -FilePath $Installer -ArgumentList '/S' -PassThru -Wait`

| 项 | 读数 |
| --- | --- |
| 安装器 exit | **0** |
| 墙钟 | **191.7 s** |
| 安装目录 | 存在，**13,655** 文件 / **785,460,990** B |
| app exe sha256 | `db4127ca748ca9660632e4675440f88b92b84619962b9ddcf9af8348cc28dc98` |
| 关键资源 | `resources\{web,python,node,tui}` ✓、`ia-tui.cmd` ✓ |
| `.old-*` | 0 |
| 用户数据 | 指纹不变（`52bb24da…`，98 文件） |

> 判定：**通过**（全新安装基线成立）。

### A2 `/S` 再次安装 + 在 `.old-*` 出现后精确 kill

- 启动安装器 `pid=416`，`ExecutablePath` = `C:\ia-1251\desktop\dist-installer\Intelligence-Agent-Setup-0.1.0.exe`。
- 轮询（100 ms）等待**新** `.old-*` 出现：**7.57 s** 出现
  `Intelligence Agent.old-{D3B496A3-FB58-42CE-8D6E-CC915CB649DC}`。
- **kill 前核对**：候选进程 `pid=416` 的 `Path` **逐字等于**安装件路径 → `taskkill /F /PID 416`（成功）。

| 项 | 读数 |
| --- | --- |
| `.old` 出现时刻 | 安装开始后 **7.57 s** |
| 杀点 | pid **416**（ExecutablePath 逐字核对通过） |
| 杀后安装器进程 | 0 |
| 备份完整性 | **13,655** 文件 / **785,460,990** B，app exe sha == 基线 `db4127ca…` ⇒ **完整旧版** |
| kill 时 live 目录 | 存在但 **0 文件**、app exe 缺失 |
| `IaBackupDir` | = `.old-{D3B496A3…}`（**armed**） |
| 用户数据 | 未动 |

> 判定：**中断态构造成功**，与 #1227 变体 A 同形（rename 已完成、新载荷尚未写入 app exe）。

### A3 `/S` 重跑（判据）

命令：`Start-Process -FilePath $Installer -ArgumentList '/S' -PassThru -Wait`

| 项 | 读数 |
| --- | --- |
| **重跑 exit** | **0** ✅（判据：exit 0） |
| 墙钟 | **171.1 s** |
| 重跑后安装目录 | 存在，**13,655** 文件 / **785,460,990** B，app exe sha `db4127ca…`，`resources\{web,python,node,tui}` ✓、`ia-tui.cmd` ✓ ⇒ **完整新版** |
| `.old-*` | **0**（已清理） |
| `IaBackupDir` / `IaLeftoverDir` | 均已清（`<none>`） |
| 用户数据 | 指纹 **不变**（`52bb24da…`，98 文件） |

> **场景 A 判定：通过。** 中断态（live app exe 缺失 + `IaBackupDir` armed + 完整备份）下，静默重跑
> **exit 0**，且落为完整新装、备份清理、用户数据逐字节不变。
>
> **归因说明（诚实）**：`/S` 静默安装无 UI，`DetailPrint` 不落盘 ⇒ 无法直接读到
> `…silent auto-rollback to …` 文本。归因依据为：① 该状态**唯一**进入 `iaRecoverInterruptedInstall` 的
> exe-missing 分支（`IaBackupDir` 非空 **且** `$INSTDIR\Intelligence Agent.exe` 缺失）；② 备份完整时该分支
> 唯一的 exit-0 出口即 #1251 的「回滚 → fall through 全新安装」；③ **同一状态在修复前（#1227 变体 A）实测为
> exit 2 / 1.0 s**；④ 终态（`.old` 清 0 + 完整新装 + `IaBackupDir` 清空）与「回滚还原 → 再次 staging →
> promote 删备份」一致。不作为「已直接读到 DetailPrint」宣称。

---

## 6 场景 B — 备份缺失应 exit 2 fail-closed

驱动脚本：`.scratch/evidence/w1251/{20-scenarioB1-interrupt.ps1,21-scenarioB2-delete-backup.ps1,22-scenarioB3-rerun.ps1}`。

### B1 构造中断态（同 A2）

| 项 | 读数 |
| --- | --- |
| `.old` 出现时刻 | 安装开始后 **7.45 s**：`Intelligence Agent.old-{7F4B8A59-79EB-4950-B308-741046D000C9}` |
| 杀点 | pid **4196**（ExecutablePath 逐字核对通过）→ `taskkill /F` 成功 |
| 杀后状态 | live 目录 0 文件（app exe 缺失）；`IaBackupDir` = `.old-{7F4B8A59…}` **armed** |

### B2 删除备份（模拟「备份缺失」）

| 项 | 读数 |
| --- | --- |
| 操作 | `Remove-Item` 删除 `.old-{7F4B8A59…}` |
| 删除后 `.old-*` | **0** |
| `IaBackupDir` | 仍 = `.old-{7F4B8A59…}`（**指向已不存在的目录**） |
| live app exe | **缺失**（`Test-Path` = False） |

> 状态即派工书要求的「安装目录不完整但无 `.old-*` 备份」；`IaBackupDir` 仍 armed，故命中
> `iaRecoverInterruptedInstall` 的「备份缺失」分支（`installer-directories.nsh:262-266`）。

### B3 `/S` 重跑（判据）

| 项 | 读数 |
| --- | --- |
| **重跑 exit** | **2** ✅（判据：exit 2 fail-closed） |
| 墙钟 | **1.1 s** |
| 重跑后状态 | 与重跑前**一致**：live 目录仍 0 文件、app exe 仍缺失、`.old-*` = 0、`IaBackupDir` 仍 armed |
| 用户数据 | 指纹不变 |

> **场景 B 判定：通过。** 备份缺失时静默重跑 **exit 2**，fail-closed 语义保留（未误删/误写、未假装成功）。
> 1.1 s 的短墙钟与「形状/存在性检查即拒绝、未进入安装」一致（同 #1227 变体 A 的 1.0 s 量级）。

---

## 7 判定汇总

| 场景 | 判据 | 判定 | 关键读数 |
| --- | --- | --- | --- |
| **A** 中断后静默重跑 | **exit 0** + 完整新装 + 备份清理 + 数据不变 | **通过** | 中断态：`.old` 7.57 s 出现、pid 416 核对后 kill、live 0 文件、`IaBackupDir` armed ⇒ 重跑 **exit 0 / 171.1 s**；终态 13,655 文件 / 785,460,990 B、`.old`=0、`IaBackupDir` 清、用户数据指纹不变 |
| **B** 备份缺失 | **exit 2** fail-closed | **通过** | `.old` 7.45 s、pid 4196 核对后 kill；删备份后 `IaBackupDir` 指向缺失目录 ⇒ 重跑 **exit 2 / 1.1 s**，状态未变 |

**对照（修复前，`62-issue-1227…` §3.1 变体 A）**：同一中断状态、修复前的安装件 = **exit 2 / 1.0 s**。
本票用修复后安装件复跑同一状态 = **exit 0** ⇒ #1251 修复在真机生效，且未破坏 fail-closed 的缺失备份分支。

---

## 8 诚实缺口

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 安装器内部分支 / `DetailPrint` 文本的**直接读数** | **未取到** | `/S` 静默安装无 UI、无 detail 日志；只能由「状态 ⇒ 唯一分支」映射 + 修复前对照推断（§5 已标注） |
| 交互式恢复页（`iaRecoveryPageCreate`）点击 | **未执行** | 会话 0 无交互桌面（同 #1226/#1227） |
| `iaRollbackApplication`「失败」子分支 | **未构造** | 需备份还原 rename 失败（锁定文件）方可；本票只覆盖「备份完整」与「备份缺失」两态 |
| 「备份形状不合法」分支 | **未构造** | 需写入非法 `IaBackupDir`（非 `.old-{guid}` 形状）；该分支非本次判据 |
| 仓内安装器烟测 `test-windows-installer.mjs` | **未运行** | 本票判据为静默自动恢复两条腿；烟测属 #1226/§W-16 范围，非本票要求 |
| 桌面窗口 / GUI 交互 | **未执行** | 会话 0 无交互桌面 |

> 以上缺口**不由本文件补齐**；本文件只就派工书两条判据（A: exit 0 / B: exit 2）给出真机读数。

---

## 9 证据路径

- **本文件**（权威读数，入库）：`docs/live_gate/w21/63-issue-1251-silent-autorecovery.md`
- 安装件：`desktop/dist-installer/Intelligence-Agent-Setup-0.1.0.exe`
  （sha256 `ae52282f…`，213,389,835 B）
- 产品 wheel：`desktop/installer/staging/python`（由 `C:\ia-1251` checkout 的 `uv build` 产出，
  sha256 `6b1b2501…`）
- 驱动脚本（本机 scratch，**未入库**，作旁证）：
  `.scratch/evidence/w1251/{common.ps1,00-baseline.ps1,10-scenarioA1-fresh.ps1,11-scenarioA2-interrupt.ps1,12-scenarioA3-recover.ps1,20-scenarioB1-interrupt.ps1,21-scenarioB2-delete-backup.ps1,22-scenarioB3-rerun.ps1,99-cleanup.ps1}`
- 日志（本机 scratch，**未入库**）：`.scratch/logs/w1251/{00-baseline,10-scenarioA1,11-scenarioA2,12-scenarioA3,20-scenarioB1,21-scenarioB2,22-scenarioB3,99-cleanup}.log`

---

## 10 逐节 V3.1-lite 合规声明

- **§1.1 / §3 启动检查表**：任务来自 #1251 派工书。已实际读取：`AGENTS.md`、`desktop/installer/installer-directories.nsh`
  （`iaRecoverInterruptedInstall` + `iaStageApplication` + `iaRollbackApplication` 相关段）、`desktop/installer/installer.nsh`
  （`customInit` 调用链）、`desktop/installer/README.md`、`desktop/scripts/build-windows-installer.mjs`、
  `desktop/scripts/prepare_python_runtime.py`、`docs/live_gate/w21/{61-issue-1226-clean-windows-vm-installer-rerun.md,62-issue-1227-installer-interruption-legs.md}`。
  **如实缺口**：本机**无 `gh`**，`gh issue view 1251` **未执行**，票面以派工书为准。
- **§1.2 / §9（TDD/Tests）**：本票**不产出产品代码**（纯证据采集），无红→绿环；「验证」= 真机安装件上的两条中断腿。
- **§2（Code Review）**：无代码提交 ⇒ 无需 review；不触发台账行。
- **§3 / §7 / §8.1（冻结树·全量门禁）**：本票不改 `src/**`/`tests/**`/`web/**`/`scripts/**`/`desktop/**`（仅新增
  `docs/**` 一件证据 + gitignored `.scratch/**` 与构建产物），不产生新冻结树，不触发全量门禁与
  `check_review_coverage.py`。
- **§4.4（施工授权）**：按派工书授权执行；仅写入 gitignored 的 `.scratch/**`、安装器构建产物、本证据文档；
  **未修改任何产品源码、配置或锁文件**。中断场景涉及杀进程，**每次杀前逐个核对 pid + ExecutablePath**（两个杀点
  416/4196 均逐字核对通过），未误伤无关进程。
- **§5 / §16.1（落点）**：证据落在 `docs/live_gate/w21/`，编号 **63**（延续 `<NN>-issue-<num>-*.md` 体例）；
  **未改** Tracker / `PHASE_STATUS.md`（本票不关单、不集成，按派工书「只测试并报告」）。
- **§6（Reuse）**：本次**非新方案/新集成**，无选型；仅按 #1226 既有成功路径构建（复用已装依赖与全局缓存），
  不改架构。
- **§8.9（写盘纪律）**：本中文长文用 `Write` 工具直接落盘，未用 `python -c`/`bash` 内联承载正文；驱动脚本 `.ps1`
  一律 ASCII。
- **§13 / §14（Git）**：本机**非 git 工作副本**，无任何 Git 写操作；提交 / 开 PR 按派工书在 **Linux 侧**做。
- **派工书「诚实红线」**：**未编造、未 waive 无证据项**（缺失项见 §8）；杀进程前均核对 pid + 路径；
  **未 merge、未关单**；修复**已生效**，**未改任何代码**。
- **票面判据对账**：场景 A **通过**（exit 0）；场景 B **通过**（exit 2）；构建读数见 §3；证据文件 = 本文件。

---

*生成于 2026-10-16（本机时间）；执行者：CodeBuddy CLI / deepseek-v4.1-flash；授权范围：仅本证据文档、gitignored scratch 与安装器产物。*
