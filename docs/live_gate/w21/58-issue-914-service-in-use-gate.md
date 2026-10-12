# #914 服务在场时的更新闸门：实测根因、方案依据与验证读数

- 票据：[#914](https://github.com/EricKingWhy/intelligence-agent/issues/914)（`bug` / P2，`in-progress`）；口径裁决见票面 comment（2026-10-11，王浩宇授权）
- 起于：#365 W-16 恢复腿的报告级候选 R2（`docs/live_gate/w21/56-w16-recovery-legs.md` §2.2 / §2.3 / §7；索引 `57` §2.6 / §3）
- 施工窗口：2026-10-12，本机 Windows 10.0.26200 + 本仓 clone `D:\intelligence-agent`
- 裁决口径（本文件全程以此为准）：安装器检测到服务在场 → **先尝试停止服务**；停不掉则 **fail-closed 中止更新**；`_CHECK_APP_RUNNING` 的中止条件应覆盖 `python.exe` 服务；复用判据不加入产品版本（`protocol_version` 仍是唯一兼容门，另用测试钉住「破坏协议必须 bump」）。

## 1 现状与实测根因

### 1.1 票面症状（2026-10-09 真机读数，`56` §2.2）

- 腿②（只有服务在场、无客户端）：启动更新**没有任何「仍在运行」中止**，逐页走完、exit 0（约 6 分 11 秒）；`agent.jsonl` 显示**新客户端由旧版服务进程应答**，该进程所在目录已被 rename 成 `.old-{4E897E42-…}`。
- 腿①（Electron 客户端在场）：rename 失败 → exit 2 + `$(iaAppRunning)`，旧版本不动。这是今天**正确**的行为，本票保留（见 §3 决策 D4）。

### 1.2 根因 A：stock 守卫的「单匹配盲区」（探针实测 2026-10-12）

`FIND_PROCESS` 的判据逐字为
`(Get-CimInstance -ClassName Win32_Process | ? { $_.Path -and $_.Path.StartsWith('$INSTDIR','CurrentCultureIgnoreCase') }).Count -gt 0`。
PowerShell 的合成属性 `Count` **不适用于单个 `Microsoft.Management.Infrastructure.CimInstance`**：恰有一个匹配时 `.Count` 是 `$null`，`$null -gt 0` 为假 ⇒ 守卫得出「没在运行」。

| 探针读数（`C:\ia-probe-914\probe`，makensis 编译 stock 宏逐字复现） | 结果 |
| --- | --- |
| 单个 CimInstance（过滤后 / 未过滤）`.Count` | `$null`（`is_null=True`） |
| 同一对象 `@(…).Count` | `1` |
| `if ($x.Count -gt 0)`：无 cast / 有 `@()` cast | NOT FOUND / found |
| 对照组：104 个匹配 `.Count` | `104` ⇒ found（≥2 匹配时守卫一切正常） |
| `Get-Process` / `Get-ChildItem` 的单个对象 `.Count` | `1`（只有 CIM 对象缺该合成属性） |
| stock 判据逐字复现、0 匹配 | 与模板语义一致 |
| `-C` 承载整条管线（长命令行）传递 | 逐字无损，无转义丢失 |
| 同型查询本机实测：4×`Intelligence Agent.exe` + 服务 `python.exe` | 多匹配 ⇒ 能命中（与票面「探针本机能命中」一致） |

⇒ 守卫**恰好**在「只有服务一个匹配」这一情形失效，即腿②；同一缺陷也让 `KILL_PROCESS` 分支失效，所以服务既没被拦、也没被杀。

### 1.3 根因 B：rename 不是「产品」的守卫（探针实测）

`desktop/installer/installer.nsh:20-21` 原注释断言「原子 rename 就是运行中守卫：Windows 不能 rename 含运行中可执行文件的目录」。实测（探针：先 rename 再查在场）：

| 读数 | 结果 |
| --- | --- |
| 服务（headless，从 `$INSTDIR\resources\python\python.exe` 启动）在场时 stock `FIND_PROCESS` 读数 | `stock_before_rename=1` = **「没在运行」**——盲区（0 才表示在运行，见模板 `${if} $R0 == 0`；服务确实在场） |
| 同一次测量里的 `Rename $INSTDIR` | `rename_errors=0`，**改名成功** → `C:\ia-probe-914\target-a.old-{2E9597F1-…}` |
| rename 之后再问一次 | `stock_after_rename=1`（同一盲区读数，服务仍在 ⇒ 判据从未看见它） |
| 强杀服务之后再问一次 | `stock_after_force_kill=1`（与在场时同值 ⇒ 反向印证盲区） |

exe 映像以 share-delete 打开，**headless 服务不阻止目录 rename**；只有 Electron 客户端的自有句柄挡得住（腿①）。⇒ 该注释与「rename 即守卫」的设计假设被证伪，本次一并更正。

### 1.4 根因 C：上游 `_CHECK_APP_RUNNING` 在更新路径的策略其实是「先停、停不掉才中止」

`_CHECK_APP_RUNNING`（模板 `:100-165`）在 `${isUpdated}` 为真时：`Sleep 300` → `FIND_PROCESS` → 命中则 `Sleep 1000` → `doStopProcess` → `KILL_PROCESS 0`（先温和）→ `Sleep 300` → 循环 `FIND_PROCESS` / `KILL_PROCESS 1`（强杀）→ 两次仍杀不掉 ⇒ `$(appCannotBeClosed)` RETRY/CANCEL，CANCEL 即 `Quit`（中止）。
即：**裁决要求的方向 2 策略上游本就实现**，失效点只在 1.2 的单匹配盲区（以及它所处位置在本仓流程里晚于 rename，见 §4.1）。

附带实测事实：

- `${isUpdated}` 在 `app-builder-lib@26.15.3` 里**只有使用、没有定义**（`out/` 426 个文件与 `templates/` 全树零定义），它由构建链在别处注入 ⇒ 本仓无法把该 macro 逐字抄进自己的 include 编译（抄了会 `macro not found`）。这排除「整段覆盖 `customCheckAppRunning`」的抄法。
- `FIND_PROCESS` / `KILL_PROCESS` 的前缀是 `StartsWith('$INSTDIR', …)`，**没有分隔符**：`C:\…\Intelligence Agent 2`、`C:\…\Intelligence Agent.old-{guid}` 这类兄弟目录都会被命中（上游偏宽；本仓新判据补分隔符，见 §4）。

## 2 方案依据（协议 §1.3 块）

- **来源**（≥2 独立，清单已按 §3.1 补录，见 `docs/agents/reference-sources.md`「Windows 安装器 / 在占用进程处理」）
  1. `app-builder-lib@26.15.3`（本仓构建固定版，`npm pack` 解包；`templates/nsis/include/allowOnlyOneInstallerInstance.nsh:45-165`）；
  2. DeepSeek Harness `5badb150`（MIT；`apps/desktop/scripts/installer.nsh:167-198` + `apps/desktop/installer/window-frame.cpp:56-85`）；
  3. Microsoft Restart Manager 官方文档（2026-10-12 读取）；
  4. Inno Setup `CloseApplications` 官方文档（2026-10-12 读取）。
- **机制摘要**
  - ① 上游守卫 = 用 PowerShell/CIM 扫「映像路径落在 `$INSTDIR` 下」的进程（PowerShell 不可用退 `tasklist`），更新流「宽限 → 先温和后强杀 → 杀不掉才中止」，交互流「OK/Cancel 后 kill」。我们**复用它的原语**（同一条 `Get-CimInstance` + `Path.StartsWith` + `Stop-Process`，本仓已装机、无需新依赖），但修掉其盲区（`@()` cast）并把判据放到 rename **之前**。
  - ② DSH 把 stock 探针整体换掉：全路径精确匹配（`_wcsicmp(path, expected)`，注释明写为了不误伤别目录同名应用），**答案未知（返回负值）即 fail-closed 中止**，且**更新时也不杀交互客户端**（10 s 宽限后弹窗中止）。
  - ③ RM 是平台级做法（MSI 自动走），停机顺序把「控制台应用 / 服务」排在 GUI 之后单独一类，并声明「应用与服务应准备好被关闭并保存状态」——即「停掉 headless 服务」属于成熟产品的标准动作；但 RM API 需要插件/内联 C#（新基建），且 RM 只会「请求所有持有者关闭」，无法表达本仓的口径「只停 headless 运行时、绝不动交互客户端」。
  - ④ Inno 用 RM 关闭占用者，但**不默认强关 GUI**（文档明写强关会丢失未保存工作）——与本仓「客户端在场 ⇒ 中止并提示用户自己关」一致。
- **契合点**（`AGENTS.md` §7）
  - 不变量 11（Sandbox/Permission 是 Runtime 边界，不靠 Prompt）：闸门是安装器 runtime 行为，不改产品协议；
  - 不变量 12/13/14（Checkpoint ≠ 副作用恢复、Ledger 可 reconcile、UNKNOWN 高风险不盲重跑）：停服务 = 一次进程级 crash，产品侧**已有**恢复路径（`src/agent_harness/recovery/coordinator.py` 的 `_prepare_reconcile` / `_commit_reconcile`，`session/service.py:1406` 的 `_has_unreconciled_operations`）；这正是票面否决「安全暂停 + Ledger 结清」preflight（方向 1）时不需新建机制的原因；
  - 不变量 21（Optional capability 故障不拖垮 Core）：闸门失败（查不清）时**中止安装器**而非放行，属安装器边界，不影响 Core；
  - 裁决 D1/D3 的「停服务 / 覆盖服务」= 复用 ① 的原语 + ② 的 fail-closed 与「不杀客户端」策略。
- **判定**：`REUSE`（原语与消息）+ `ADAPT`（判据位置、`@()` cast、分隔符前缀、fail-closed 未知分支）+ `DEFER`（Restart Manager：需要新插件/内联 C#，且语义不匹配，本票不做）。
- **License**：`app-builder-lib` MIT（本仓既有运行时依赖，`desktop/THIRD_PARTY_NOTICES.md` 已列）；DSH MIT（`installer-directories.nsh` 头部既有 provenance）；本票只复用两行谓词与既有 LangString，不整段复制文件，MIT 义务按既有台账满足。

## 3 决策与范围

- **D1**（裁决 1）：采用方向 2，最小替代；不做方向 1（列在途 session → client-exit → 拒绝/未知中止）。
- **D2**（裁决 2）：兼容性判据不加产品版本/构建号；`protocol_version` 唯一；另加测试钉住「破坏协议必须 bump `protocol_version`」。
- **D3**（裁决 3）：中止条件覆盖服务进程。
- **D4**：交互客户端在场 ⇒ 中止并提示（保留腿①实测行为，也对齐 ②④ 的「不默认强关 GUI」）。
- **D5**：闸门放在 `iaStageApplication` 的 `customInit` 路径上、**rename 与 `iaStashBackupDir` 之前**：所有中止都「旧版本原样」，无需依赖回滚（腿③的回滚链保持只服务中断场景）。
- **非目标**：`.old-*` 目录里孤儿服务的生命周期（属 #904 / R3 的 leftover 域，另开票）；`iaRollbackApplication`（恢复页「还原旧版本」）路径的同类盲区（只在已中断安装后出现，且其自带 fail-closed 分支，另记）；两个安装树并存时上游 `KILL_PROCESS` 前缀偏宽的误杀（上游行为，本票仅记录）。
- **已知窄竞态**：`customInit` 与安装 section 之间用户手动启动新客户端/服务（此时 rename 已完成、闸门已过）。触发需要用户在向导运行期间启动一个尚未落盘的 app，且后果可由下一次更新或用户重启消除 ⇒ 本票不引入 section 期二次闸门（另记 §6）。

## 4 实现

（施工后回填最终改动清单与逐行依据）

## 5 验证读数

（施工后回填：focused 测试、真机腿、门禁、审查）

## 6 残余与后续

（施工后回填）
