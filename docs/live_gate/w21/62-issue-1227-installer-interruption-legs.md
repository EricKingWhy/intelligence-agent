# #1227 安装器中断腿（运行中更新 / 服务暂停失败 / 安装目录原子交换中断）

> 承接 #1226 §6「诚实缺口」里登记的三条**未执行**项：`运行中更新安全暂停`、`服务暂停失败 / 迁移中断`、
> 以及 `51-w16-remaining-legs.md` §5 的安装目录原子交换中断。本文件在**同一台干净 Windows 真机**上
> （#1226 用的就是本机），用**本仓源码重新构建的同一安装件**（sha256 `6341b7c2…`）真实构造三种中断，
> 把读数与 2026-10-16 19:10 用户裁决的三条判据逐条对照。
>
> **本文件不宣布任何 Gate 通过、不 merge、不关单。** 失败/偏差如实记录。

---

## 0 执行器与环境

| 项 | 值 |
| --- | --- |
| 机器 | 腾讯云 Windows Server 2022 Datacenter，build 20348，x64，主机名 `10_1_0_15` |
| 执行方式 | 真机上的 CodeBuddy CLI（`codebuddy --model deepseek-v4.1-flash`）经 SSH 驱动，**会话 0**（`qwinsta`：`>services`） |
| 源码 | `C:\ia`（**origin/main 打包传输，非 git 工作副本**；本机无 `.git`，提交 / 开 PR 在 Linux 侧做） |
| 安装件 | `desktop/dist-installer/Intelligence-Agent-Setup-0.1.0.exe`，bytes **213,265,521**，sha256 `6341b7c23a1dc7ec8c884205ed370c676b72eb8e2e940ee3755472d68216aeca`（**与 #1226 逐字节相同**） |
| 安装目录 | `%LOCALAPPDATA%\Programs\Intelligence Agent`（per-user） |
| 用户数据 | `%APPDATA%\intelligence-agent` |

**安装件在腿执行前的前态**（2026-10-16 20:04）：安装目录 **不存在**、`HKCU\…\Uninstall` 无 Intelligence 项、
无同名服务、无产品进程；`%APPDATA%\intelligence-agent` **已存在**（#1226 留下的 Electron 缓存 +
`workspace\{sessions,workspaces,harness.db,.host-service.json}`、`host-credentials.json`、`smoke-marker.txt`）。

> **闸门语义（本文件对照的唯一实现）**：`desktop/installer/installer-directories.nsh`
> `iaClassifyRunningProduct` / `iaRequireNoRunningProduct`（114–199）——分类答案 `0` 放行、`11`（只有 bundled
> runtime）先停再问（有界 8×250 ms）、`12`（客户端等）与**任何不可证答案**都 `SetErrorLevel 2` + `Quit`；
> 闸门在 `iaStageApplication` 内、`Rename $INSTDIR` **之前**（首轮 :405、#1066 二次 :458）。
> 安装件为**静默**安装（`/S`），无 UI，`DetailPrint` 不落盘 ⇒ 闸门的**内部分类答案与重试计数无直接读数**，
> 只能由分支可观察结果（exit 码 / 进程 / 目录 / 墙钟）推断，下文逐处标注。

---

## 1 腿 1 — 运行中更新（判决已按实现修正）

判据（裁决 1）：`/S` 静默重装时——**服务在场 ⇒ 强杀服务后放行（安装继续）**；**服务杀不掉 ⇒ exit 2 拒绝**；
**客户端（文件占用者）在场 ⇒ 拒绝**（Electron 句柄挡 rename，闸门先拦）。

### 1.1 基线安装（步骤 1）

| 项 | 读数 |
| --- | --- |
| `/S` exit | **0** |
| 墙钟 | **198.3 s** |
| 安装目录 | 存在，**13,641** 文件 / **785,152,496** B，清单 sha256 `5d2f41f3cd3e4a62a69c2112f7f144d03c35ea507b4e0a3577b9914260532aac` |
| app exe | `Intelligence Agent.exe` sha256 `db4127ca748ca9660632e4675440f88b92b84619962b9ddcf9af8348cc28dc98`，247,033,344 B，产品版本 `0.1.0.0` |
| 关键资源 | `resources\{python,node,web,tui,mxc}` ✓、`resources\python\python.exe` ✓、`resources\web\index.html` ✓、`ia-tui.cmd` ✓ |
| 卸载注册表项 | 1 条：`HKCU\…\Uninstall\ad4c8292-94ef-5e7e-addf-1a5d7241abe1`，`DisplayVersion=0.1.0` |
| `.old-*` 残留 | 0 |
| 用户数据 | 安装未触碰（无新文件） |

> 旁证：进程运行期 bundled Python 会在 `resources\python\Lib\**\__pycache__\` 落 `.pyc`（本次实测
> 基线 13,641 → 客户端运行后 13,642+），故「安装目录清单 sha」在**产品运行期不稳定**；判定旧版本完整性
> 以 **app exe sha256 + `.old` 完整性**为准，不以运行期目录清单为准。

### 1.2 客户端在场 → 重装（步骤 2、3）

启动 `Intelligence Agent.exe`（会话 0 无可见窗口），实测产品进程 **4 个**：3× `Intelligence Agent.exe`
（Electron）+ 1× `resources\python\python.exe`（后端服务，**ppid = Electron 主进程**）；端点
`workspace\.host-service.json` 在位。

| 项 | 读数 |
| --- | --- |
| `/S` 重装（客户端在场）exit | **2** |
| 墙钟 | **11.1 s** |
| 实际分支 | **拒绝**（`$(iaAppRunning)`；分类答案 `12`，可推 = 客户端路径在 `$INSTDIR\` 下且不属 `resources\python\`） |
| 旧版本完整性 | **未动**：app exe sha 不变（`db4127ca…`）、`.old-*` 0、客户端进程重装后仍在 |
| 用户数据 | harness.db / host-credentials.json 不变 |

### 1.3 仅服务在场 → 重装（步骤 4、5）

先按**路径精确**结束全部产品进程（4 个，均以 `$INSTDIR\` 前缀核对）。然后单独起服务——**用桌面端的
忠实调用形态**（`desktop/src/service-host.ts:31-33` + `main.ts:145-149` 实测得出）：
`resources\python\python.exe -m agent_harness.cli serve --host 127.0.0.1`，cwd = 数据根，env =
`WORKSPACE_DIR=<root>` + `AGENT_HARNESS_HOST_CREDENTIALS=file:<root>\host-credentials.json` + `WEB_DIST_DIR=<install>\resources\web`。

| 项 | 读数 |
| --- | --- |
| 单独服务进程 | 存活 **1 个**（pid 5004，ppid 2788=驱动器）；`HOST_SERVICE_READY http://127.0.0.1:63996` |
| `/S` 重装（仅服务在场）exit | **0** |
| 墙钟 | **186.3 s** |
| 实际分支 | **闸门→强杀服务→放行**（分类答案 `11` ⇒ `Stop-Process -Force` ⇒ 复问 `0` ⇒ 继续） |
| 重装后 | 产品进程 **0**（服务被闸门结束）、`.old-*` 0、安装目录回到干净清单 `5d2f41f3…`、app exe sha 不变 |
| 用户数据 | harness.db 不变；`host-credentials.json` / `.host-service.json` 因**服务自身**运行而变（非安装所致） |

### 1.4 环境观测（非产品缺陷，如实登记）

**第一次尝试**（未带桌面 env）直接 `python -m agent_harness.cli serve`——服务**在 15 s 内崩溃**：
`keyring` 走 OS 凭据库 `CredRead` ⇒ `WinError 1312 指定的登录会话不存在`（会话 0 无交互登录会话）。
桌面端**总是**注入 `AGENT_HARNESS_HOST_CREDENTIALS=file:…` 走文件后端，故正常用户会话不受影响；
本条只在「绕过桌面、裸起服务」时出现，登记为**环境事实**而非缺陷。

**腿 1 判定：通过**（服务在场→放行 exit 0；客户端在场→拒绝 exit 2；旧版本均未动）。

---

## 2 腿 2 — 服务暂停失败（杀不掉的服务）

判据（裁决 3 腿②）：长跑服务 + **看门狗 250 ms 重拉**（构造「杀不掉」）⇒ `/S` 有界重试后 **exit 2**，
**旧版本未动**。

### 2.1 构造

看门狗脚本（`.scratch/evidence/w1227/20-leg2-watchdog.ps1`）：每 250 ms 检查 bundled 服务是否存活，
死了立刻重起（同一忠实 env）。实测存活采样（16 × 500 ms）**全为 1**（服务始终在场）。

### 2.2 读数

| 项 | 读数 |
| --- | --- |
| `/S` 重装（杀不掉的服务）exit | **2** |
| 墙钟 | **23.0 s** |
| 看门狗日志 | `20:18:26.476 respawn #1 pid=2132`（初始）→ **`20:18:49.726 respawn #2 pid=4536`** |
| 时间线 | 安装开始 20:18:39.2 → 闸门 `Stop-Process` 杀服务（**≈20:18:48.9**，由 respawn #2 反推）→ 复问循环 → **20:19:02.3 拒绝** |
| 旧版本完整性 | **未动**：app exe sha 不变（`db4127ca…`）、`.old-*` 0 |
| 清理后产品进程 | 0 |

**「有界重试 = 8 次」的读法（诚实说明）**：静默安装无 UI ⇒ 闸门内部计数 $iaRuntimeExitWaits **无直接读数**。
可观察的支撑：① 代码界 = `installer-directories.nsh:174-181`（`$iaRuntimeExitWaits > 8` 才 `ExitDo`）；
② 对比「单次分类即拒绝」的腿 1.2（11.1 s）与本次 23.0 s，差值 ≈ 12 s ≈ 8 ×（Sleep 250 ms + 一次 PowerShell/CIM 分类）
⇒ 与「跑了有界复问循环」一致。**不把推断写成已直接读到 8。**

**腿 2 判定：通过**（杀不掉的服务 ⇒ 有界重试后 fail-closed exit 2，旧版本未动）。

---

## 3 腿 3 — 安装目录原子交换中断

判据（裁决 2）：在安装器出现 `.old-{guid}` 备份目录的**窗口期** kill 安装进程，验证
**(a)** 安装目录要么完整旧版、要么完整新版（不允许半新半旧）；**(b)** 用户数据 `%APPDATA%\intelligence-agent`
**逐字节不可变**；**(c)** 重跑 `/S` 能**幂等恢复（exit 0）**。

用 `taskkill /F` 精确杀安装进程；**杀前逐个核对 pid + ExecutablePath（必须逐字等于安装件路径）**。
窗口期在被杀点上有两种可观察形态，**两种都跑**（均在 `.old` 已出现的窗口内）。

### 3.1 变体 A：`.old` 一出现即杀（live 目录尚空）

| 项 | 读数 |
| --- | --- |
| `.old` 出现 | 安装开始后 **7.0 s**：`Intelligence Agent.old-{9BB6CD76-3C5D-4C57-83F3-C0CE14C3C703}` |
| 杀点 | pid **1628**（ExecutablePath 逐字核对通过）→ `taskkill /F` 成功 |
| 杀时 live 目录 | 存在但 **0 文件**、app exe 缺失；`.old` 内 app exe 在位 |
| `.old` 完整性 | **13,644 文件 / 785,296,989 B**，清单 sha == 基线 `187ad61c…`，app exe == 基线 `db4127ca…` ⇒ **完整旧版** |
| (a) | **成立**：旧版**完整**保留在 `.old`；live 目录只可能含新载荷文件（先 rename 后新建），**无「同一目录内旧+新混合」** |
| (b) 用户数据 | **逐字节不变**：树 sha `50c3c485…`（98 文件 / 8,929,894 B）杀前=杀后 |
| (c) 重跑 `/S` | **exit 2 / 1.0 s** ⇒ **未达「幂等恢复 exit 0」** |

**(c) 的实际行为 = 设计内 fail-closed**：重跑时 `IaBackupDir` 已 armed、`$INSTDIR\Intelligence Agent.exe`
缺失 ⇒ `iaRecoverInterruptedInstall`（`installer-directories.nsh:245-277`）判定「中断待决」，
`IfSilent ⇒ SetErrorLevel 2 + Quit`（「重跑交互式以恢复或全新继续」）。**这是刻意的 fail-closed**
（silent 无法在「还原旧版」与「全新继续」之间替用户选择），`installer-directories.nsh:243-244` 与
`desktop/installer/README.md` 均如此声明。⇒ **变体 A 的 (c) 与票面预期不符，但不是产品缺陷；按 §9.1.1 登记待裁决。**

### 3.2 变体 B：等新 app exe 落盘后再杀（live 目录已半新）

先按观察恢复变体 A 的现场（删空的 live 目录 → 把 `.old` 改回 live 目录 → 清 `IaBackupDir`），复核回到
干净基线（清单 `187ad61c…`）。再重跑同一构造：

| 项 | 读数 |
| --- | --- |
| `.old` 出现 | 安装开始后 **6.85 s**：`Intelligence Agent.old-{BDFD48EF-4FA9-4944-8614-3B874D891BAA}` |
| 新 app exe 落盘 | **107.5 s**（live 目录此时 9 文件；capture 时 22 文件 / 340,982,611 B，`resources\*` 尚未落） |
| 杀点 | pid **2076**（ExecutablePath 逐字核对通过）→ `taskkill /F` 成功 |
| `.old` 完整性 | 13,644 文件，清单 sha == 基线 `187ad61c…`，app exe == 基线 ⇒ **完整旧版** |
| (a) | **成立**：live 目录 = **部分新版**（含 app exe，`resources\*` 未落），**不含任何旧文件**；旧版完整在 `.old` |
| (b) 用户数据 | **逐字节不变**：树 sha `50c3c485…` 杀前=杀后 |
| (c) 重跑 `/S` | **exit 0 / 182 s** ⇒ **幂等恢复成立** |
| 重跑后 | 安装目录**完整**：app exe ✓ + `resources\{python,node,web,tui,mxc}` ✓，清单 `5d2f41f3…`（干净新装）；`IaBackupDir` 已清 |
| 遗留 | 中断那次备份 `Intelligence Agent.old-{BDFD48EF…}`（完整旧版，785 MB）被记为 `IaLeftoverDir` 留盘 —— 这是 #904 的设计（由卸载器按名字形状 sweep，或手动删），非新缺陷 |

**腿 3 判定**：**(a) 成立、 (b) 成立**（两变体）；**(c)** 变体 B **通过**（exit 0 幂等恢复），
变体 A **偏差**（exit 2，设计内 fail-closed，与票面预期不符）⇒ 见 §5 待裁决。

---

## 4 判定汇总

| 腿 | 验收 | 判定 | 关键读数 |
| --- | --- | --- | --- |
| **1** 运行中更新 | 服务在场→放行；客户端在场→拒绝 | **通过** | 基线 install exit 0 / 198.3 s；客户端在场重装 **exit 2 / 11.1 s**（拒绝、旧版未动）；仅服务在场重装 **exit 0 / 186.3 s**（强杀服务后放行） |
| **2** 服务暂停失败 | 有界重试后 exit 2、旧版未动 | **通过** | 看门狗 250 ms 保活（采样 16/16=1）；重装 **exit 2 / 23.0 s**；respawn #2 证明闸门 Stop-Process 生效；旧版 app exe 未变、`.old-*`=0 |
| **3** 原子交换中断 | (a) 无半新半旧、(b) 数据逐字节不变、(c) 重跑 exit 0 | **部分通过（含一条偏差）** | 变体 A（`.old` 即杀）：(a)✓ (b)✓ **(c) exit 2 / 1 s ✗（fail-closed）**；变体 B（app exe 落盘后杀）：(a)✓ (b)✓ **(c) exit 0 / 182 s ✓** |

---

## 5 待裁决 / 待登记（另开 issue：先查重；本机无 gh，待 Linux 侧开单）

1. **[偏差，非缺陷，待裁决]** 腿 3 变体 A：中断点在「rename 已完成、新载荷尚未写入 app exe」时，
   静默重跑**不会**幂等恢复为 exit 0，而是 fail-closed **exit 2**（`iaRecoverInterruptedInstall` 的
   `IfSilent` 分支）。**与票面 (c) 的「幂等恢复 exit 0」预期不符**，但属设计内 fail-closed
   （`installer-directories.nsh:243-244/267-273`、`README.md`）。**作者未改任何代码**（本票纯证据）。
   交用户裁决：是接受「静默中断待决 = exit 2、交互式才可恢复」的口径，还是要求 silent 分支改为
   「备份完整即自动全新继续」（后者是行为变更，需另票）。
2. **[观测]** 中断恢复后，中断那次备份（完整旧版）会被记为 `IaLeftoverDir` 留盘（本机遗留
   `Intelligence Agent.old-{BDFD48EF…}`，785 MB），由卸载器名字 sweep 清理 —— 属 #904 既有设计，
   本票只登记读数。
3. **[环境]** 会话 0 下裸起 `python -m agent_harness.cli serve`（无桌面 env）会因 keyring
   `WinError 1312` 崩溃；带桌面 env（文件凭据后端）正常。非缺陷，登记为环境事实。

> 本机无 `gh` ⇒ 以上**未**在 GitHub 登记；待 Linux 侧查重后按需开单。

---

## 6 诚实缺口（仍未执行 / 本机不可执行）

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 「真实长跑 agent 任务」 | **未执行** | 干净机无 `MODEL_API_KEY`/`.env`（同 #1226 §6）。腿 1/2 的 fixture 用**客户端在场**与**bundled 服务进程**（+ 看门狗）替代；闸门的判别器（`$INSTDIR\resources\python\` 下进程的存在性与可杀性）已**完整**受测 |
| 闸门内部分类答案 / 重试计数的直接读数 | **未取到** | `/S` 静默安装无 UI、`DetailPrint` 不落盘；只能由分支结果 + 代码界推断（已逐处标注） |
| 交互式恢复页（`iaRecoveryPageCreate`）点击 | **未执行** | 会话 0 无交互桌面（#1226 同缺口） |
| 桌面窗口可见性 / 托盘 → quit | **未执行** | 同上 |
| `caRollbackApplication`「还原旧版本」路径的 GUI 触发 | **未执行** | 需交互页面 |

---

## 7 证据路径

- **本文件**（权威读数，入库）：`docs/live_gate/w21/62-issue-1227-installer-interruption-legs.md`
- 安装件：`desktop/dist-installer/Intelligence-Agent-Setup-0.1.0.exe`（sha256 `6341b7c2…`）
- 原始日志与驱动器（本机 scratch，**未入库**，作旁证）：
  - 驱动脚本：`.scratch/evidence/w1227/{common.ps1,capture.ps1,11-leg1-baseline.ps1,12-leg1-client-present.ps1,13-leg1-service-only.ps1,14-leg1-service-only-v2.ps1,20-leg2-watchdog.ps1,21-leg2.ps1,31-leg3-interrupt.ps1,32-leg3-midextract.ps1}`
  - 日志：`.scratch/logs/w1227/{leg1.log,leg1-client.log,leg1-service.log,leg2.log,leg2-watchdog.log,leg3.log,leg3b.log}`

---

## 8 逐节 V3.1-lite 合规声明

- **§1.1 接票 / §3 启动检查表**：任务来自 #1227 派工书（含 2026-10-16 19:10 三条裁决）。已实际读取：
  `AGENTS.md`、`docs/SDD_WORKFLOW_PROTOCOL.md`（**全文 929 行，首行至 EOF**）、`docs/SDD_TICKET_TRACKER.md`
  （当前态）、`docs/PHASE_STATUS.md`、`SPEC_ROOT/00_PROJECT_VISION.md`、`SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`、
  `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md`、`desktop/installer/{installer.nsh,installer-directories.nsh,README.md}`、
  `desktop/src/{service-host.ts,host-protocol.ts,host-client.ts,installer/paths.ts}` 相关段、
  `docs/live_gate/w21/58-issue-914-service-in-use-gate.md`、`61-issue-1226-clean-windows-vm-installer-rerun.md`、
  `.scratch/evidence/w16/*.ps1`。**如实缺口**：本机**无 `gh`** ⇒ `gh issue view 1227` **未执行**，票面以派工书为准。
- **§1.2 / §9（TDD/Tests）**：本票**不产出产品代码**（纯证据采集），无红→绿环；「验证」= 真实安装件上的真机中断链路。
- **§2（Code Review）**：无代码提交 ⇒ 无需 review；不触发台账行。
- **§3 / §7 / §8.1（冻结树·全量门禁）**：本票不改 `src/**`/`tests/**`/`web/**`/`scripts/**`/`desktop/**`（仅新增
  `docs/**` 一件证据 + gitignored `.scratch/**`），不产生新冻结树，不触发全量门禁与 `check_review_coverage.py`。
- **§4.4（施工授权）**：按派工书授权执行；仅写入 **gitignored** 的 `.scratch/**` 辅助脚本 / 日志、本证据文档，
  以及安装器自身的安装产物；**未修改任何产品源码、配置或锁文件**。中断场景涉及杀进程，**每次杀前逐个核对
  pid + ExecutablePath**（闸门自身只 `Stop-Process` `$INSTDIR\resources\python\` 下进程；驱动器只 kill 精确匹配
  的安装件进程），未误伤无关进程。
- **§5 / §16.1（落点）**：证据落在 `docs/live_gate/w21/`，编号 **62**（延续 `<NN>-issue-<num>-*.md` 体例）；
  **未改** Tracker / `PHASE_STATUS.md`（本票不关单、不集成，按派工书「只产出证据和报告」，与 #1226 同口径）。
- **§8.9（写盘纪律）**：本中文长文用 `Write` 工具直接落盘，未用 `python -c`/`bash` 内联承载正文；
  驱动器 `.ps1` 一律 ASCII，避免 5.1 无 BOM UTF-8 误读。
- **§9.1.1（新证据变更控制）**：腿 3 变体 A 的 (c) 与票面预期冲突 —— **停在报告、未改票面、未改代码**，
  按 §5 请求裁决。
- **§13 / §14（Git）**：本机**非 git 工作副本**，无任何 Git 写操作；提交 / 开 PR 按派工书在 **Linux 侧**做。
- **票面「不做什么」**：**未宣布任何 Gate 通过、未 merge、未关单**；失败/偏差如实记录（§5）。
