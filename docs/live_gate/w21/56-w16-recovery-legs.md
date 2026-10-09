# W-16（#361）恢复腿：真机实测读数（安装件 `9d359af5…`，2026-10-09）

- 场景：`51-w16-remaining-legs.md` §5 登记了五条**未执行**的 W-16 恢复腿。本文件在
  **#848 修 + 重打包后的安装件**上把其中可在本机执行的四条跑完并留读数，第五条（干净 VM）仍未执行。
- 安装件：`D:\w21-work\repack-1791521109\Intelligence-Agent-Setup-0.1.0.exe`，
  **209,366,585 B**，sha256 `9d359af5795ede5f2bd4ab3f15a5d2c2e9ea175f47a5745dda80bf5d352bd63f`，
  构建树 `bd560e3f`；已装到 `C:\Users\王浩宇\AppData\Local\Programs\Intelligence Agent`
  （`HKCU\Software\ad4c8292-94ef-5e7e-addf-1a5d7241abe1`，DisplayVersion `0.1.0`）。
- 破坏性中断腿（§3）用 **en_US 测试安装件**
  （`desktop\.desktop-build\installer-tests\run-nNsml7\en_US\installer-test.exe`，与产品件同一 NSIS
  模板与同一 `installer.nsh`，app id `84063fb8-f196-4cbc-a5d8-590a798a1d63`）在隔离的测试 app 上做，
  避免把本机产品安装推到「程序不可用」状态。
- **本机是开发机，不是干净 Windows x64 VM**（承 `43`/`51` 的口径，本条未变）。所有腿都在这台机器上，
  数据根 `%APPDATA%\intelligence-agent`（端点文件 `workspace\.host-service.json`）。
- UI 一律用 **computer-use**（辅助功能树 + 截图）驱动真实窗口；**操作者动作都逐条披露**
  （§2 里 `Stop-Process` 结束桌面客户端、§3 里 `taskkill /F` 硬杀安装器）。
- 本文件**只报告，不改产品代码**（#365 票面）；**不宣布 #365 Gate 通过**。

## 1. 腿① 运行中更新（桌面客户端在场）⇒ fail-closed 中止（实测通过）

- 驱动：`D:\w21-work\run-installer.ps1`（`Start-Process -PassThru` + `WaitForExit()` + 记 exit code）。
- 读数（证据 `D:\w21-work\evidence\w16-legs\`）：

| 项 | 读数 |
| --- | --- |
| 起止 | pid 4112，起 `14:32:24`，退 `14:32:41` |
| 退出码 | **exit code = 2**（`b1-exit.txt`） |
| 界面 | computer-use 读到的窗口 `Intelligence Agent 安装`，只有一条正文 + 「确定」：**「Intelligence Agent（或其后台进程）仍在运行。请关闭后重新运行安装程序——旧版本未被改动。」**（`b1-app-running-dialog.txt`/`.png`） |
| 旧版状态 | **中止瞬间我没有取目录/注册表快照**（快照从 14:36:51 的 A 起）；「旧版未被改动」由三条支持：①对话框文案本身、②机制（`Rename` 失败即 `Quit`，目录未被换走）、③**14:33 应用仍从安装目录运行**（`c0-desktop-before-quit.txt`：`…\Programs\Intelligence Agent\Intelligence Agent.exe` pid 42948，窗口 `Agent Harness Inspector`，404 行辅助功能树）——安装被毁则不可能还在跑 |

- 语义（对 `desktop/installer/installer-directories.nsh:49-71`）：中止发生在**换目录的原子 `Rename`** 上——
  Windows 不允许 rename 含正在运行 exe 的目录，失败即 `MessageBox` + `SetErrorLevel 2` + `Quit`，
  所以「客户端在场」这条腿是**失败即停、旧版未动**，没有部分安装。
- 这条腿同时**兑现了 `LEG-NOTE.txt` 第 3 条的待触发项**：`installer.nsh:64/70` 的自定义
  `iaAppRunning`（en/zh 各一条）由此触发并实测到渲染文案。

## 2. 腿② 仅服务在场 ⇒ 更新照做；**票面的「安全暂停」在发货路径上不存在**（实测 + 代码）

### 2.1 前置状态（据实标注）

- 桌面客户端用 `Stop-Process` 结束（**操作者动作**，非产品退出路径：产品的 × = 隐藏到托盘，见 `43`
  §关联观察 2）；产品**服务**仍在运行：pid 24844，exe = 安装目录 `resources\python\python.exe`。
- 该服务的来源可考：`%APPDATA%\intelligence-agent\logs\agent.jsonl` 在 **14:35:29** 有唯一一条服务启动
  记录（「未投递输入重建完成：扫描 18 个会话，命中 0 个，耗时 0.58s」），下一条同型记录要到 **14:45:24**
  （我把旧服务停掉、重启客户端后的新服务）。14:35:14 启动过 TUI 冷启动尝试
  （`tuic-console.txt`）。**诚实注记**：那次 TUI 驱动自身没拿到可读会话（`cmd_alive=false`、
  屏幕 0 字），所以「服务由 TUI 冷启动」这一点此处是**按启动时刻与唯一性推断**的，不是 TUI 会话读数；
  TUI 冷启动腿的正身读数仍以 `43` 的 C/E 腿（旧件 `ce5fd54a`）为准。

### 2.2 读数

| # | 动作 | 读数（证据） |
| --- | --- | --- |
| 1 | 服务在场时启动安装器（pid 30744，`14:36:15`） | **没有任何「仍在运行」中止**：向导正常走到第 1 页（`c1-service-only-dialog.txt`）；取消后 exit 1（`c1-service-only-exit.txt`） |
| 2 | 换目录是否成功（快照 A，向导期） | 安装目录 **0 项**、新备份 `Intelligence Agent.old-{AB32461F-…}` **20 项**、`IaBackupDir` 指向它 ⇒ **rename 成功**（服务不锁安装目录）（`c2-swap-snapshots.txt`） |
| 3 | 取消后的回滚（快照 B） | 安装目录复原 20 项、`AB32461F` 消失、`IaBackupDir` 清空、exit 1、服务 24844 仍在 ⇒ 优雅取消路径的**回滚按设计工作** |
| 4 | 完整更新（pid 8720，`14:37:31`） | 逐页走完向导（选项页 → 位置页 → 「正在安装」进度页 → 完成页「Intelligence Agent 已安装在你的系统。单击 [完成(F)] 关闭此向导。」），**exit 0**，`14:43:42` 结束（约 6 分 11 秒） |
| 5 | 更新后客户端（`c4-after-update-sessions.png`，14:44） | 侧栏 7 组 `run-a-sample-*` **旧会话可见**（43 / 5246 / 605 / 1131 / 519 / 1710 事件，相对时间 43 分钟前 ~ 1 天前，**均在本次更新之前**）⇒ 用户数据未被动过 |
| 6 | 更新后是哪个服务在应答 | `agent.jsonl`：**14:43:33** 有 `GET /api/models` 被应答（此刻唯一存活的服务是 14:35:29 启动的那个）；**14:45:24** 才出现新服务启动记录 ⇒ **新客户端由旧版服务进程应答**，而该进程的文件已被 rename 到 `.old-{4E897E42-…}` |
| 7 | 服务健康面（14:50 复核） | `GET http://127.0.0.1:58183/api/health` → **200** `{"status":"ok","checkpoint_save_failures":0,"protocol_version":1,"version":"1.0.0","capabilities":[],"auth_required":true}` |

### 2.3 代码侧核对（为什么没有「安全暂停」）

- 票面要求的是：**列在途 Task / 其他客户端 → 安全暂停 → Ledger 结清（失败则中止替换）**。
- `desktop/src/installer/preflight.ts:68` 的 `runUpdatePreflight` **正是这段设计**（逐 session
  `POST /api/sessions/{id}/client-exit`，拒绝或未知状态则 fail-closed 中止）。但全仓检索
  （`*.ts/*.mjs/*.js/*.md/*.json`，排除 `node_modules`）它只被
  `desktop/test/installer-preflight.test.ts`、`desktop/installer/README.md:57`、
  `desktop/THIRD_PARTY_NOTICES.md:99` 引用——**没有任何发货代码调用它**，属于**死代码**。
- 仓内也不存在 `autoUpdater` / `electron-updater`：更新就是「手动跑安装器」，安装器与产品之间**没有 HTTP**，
  唯一的中止闸门是 §1 的 rename 碰撞。
- 客户端复用服务的判据（`desktop/src/host-protocol.ts:96-106`）只看 **`protocol_version`**
  （现为 1，旧服务同为 1 ⇒ 判为兼容、直接复用），**不看产品版本**；所以「更新后旧版进程继续服务新客户端」
  在这个判据下是设计内行为，本文件按实测如实登记，不把它写成版本门缺失的缺陷。
- 安装器里跑的还是 electron-builder 原生 `_CHECK_APP_RUNNING`（`installer.nsh:20` 有注记）。
  实测其**探针查询本身是能命中的**——在本机跑同一形状的查询
  （`Get-CimInstance Win32_Process | Where Path.StartsWith($INSTDIR)`）命中 5 个进程：
  4 个 `Intelligence Agent.exe` + 服务 `python.exe`——但 §2.2 第 4 条的更新**没有被拦下**，
  且服务在更新前后是同一个 pid（14:35:29 的启动记录唯一）。**探针当时是否弹过框、我无证据**
  （14:37 的两张页截图与 14:38 的进度页截图上都没有弹框），所以这里只按结果说话：
  **服务在场时更新未被拦下、服务未被杀**。
- **命名失准的自我披露**：`evidence\w16-legs\c3-stock-probe-dialog.png` 这个名字是当时按计划起的，
  实际内容是**「正在安装」进度页**（已回看图像核对），本文件不按文件名当证据用。

## 3. 腿③ 迁移中断（硬杀）⇒ 程序不可用；重跑可恢复，但备份被永久遗留 + 指针被清（实测）

在 en_US 测试件上做（理由见文件头）。快照工具 `D:\w21-work\snap-swap.ps1`（列安装目录与所有
`.old-*`、扫 `HKCU:\Software` 找 `IaBackupDir`），读数 `d1-interrupt-snapshots.txt`。

| 步 | 动作 | 读数 |
| --- | --- | --- |
| 0 | 首次安装（pid 38436，14:45:39） | **exit 0**（14:46:07）；快照 C：安装目录 **4 项 + exe 在位** |
| 1 | 再跑安装器（pid 23468，14:46:39）触发换目录，**14:46:48 `taskkill /F`** | exit **-1**（被硬杀）；快照 D：安装目录 **0 项、exe 不在（程序不可用）**；备份 `…84063fb8.old-{30C6C427-…}` **4 项、exe 在**；`IaBackupDir` **已指向备份** |
| 2 | 重跑安装器（pid 15116，14:47:08），走完向导点「完成」 | **exit 0**（14:48:31）；快照 E(14:47:23)/F(14:48:35)：安装目录 **3 项 + exe 在位（程序恢复可用）**；备份 `30C6C427` **仍在（4 项）**；而 `IaBackupDir` **已空** |
| 3 | 收尾 | 测试 app 逐页/静默卸载（`e2-testapp-uninstall-exit.txt`，exit 0；安装目录与 `HKCU` 键消失；该 stub 从未建过用户数据目录 ⇒ 本腿对「卸载保留用户数据」**无断言力**，那条由 `43` 的 F 腿与 `51` §3 的 `userDataPreserved=true` 覆盖） |

- 语义结论：**硬中断（任务管理器结束 / 断电）不会触发 `.onGUIEnd`**（`installer-directories.nsh:119-126`），
  所以旧目录不会自动还原——用户此时看到的是一台「程序没了」的机器（安装目录空），
  恢复动作 = **再跑一次安装器**（本腿实测能恢复）。票面「迁移失败时恢复旧目录/旧程序可用」
  **只在优雅取消/提取失败路径成立**（§2.2 第 3 条），硬中断路径做不到自愈。
- 恢复的那次安装**不会**回收上一次中断留下的备份，反而把指针清掉（第 2 步读数）⇒ 备份变成**无主孤儿**，
  且此后任何一次安装都不会再碰它。

### 3.1 根因探针：`\\?` 前缀拼错 ⇒ promote 的「删备份」每次静默失败

- 代码：`desktop/installer/installer-directories.nsh:106-109`

  ```
  ${If} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
    ; Long-path prefix so deep runtime trees (node_modules-style) are removed.
    RMDir /r "\\?$iaBackupDirectory"
    !insertmacro iaClearBackupDir
  ```

  NSIS 字符串里**反斜杠不是转义字符**（转义是 `$` 系），所以 `"\\?$iaBackupDirectory"` 拼出来的是
  `\\?` + `C:\Users\…` = **`\\?C:\Users\…`**；合法的 Win32 长路径前缀是 `\\?\` + 路径
  （`?` 后必须还有**一个**分隔反斜杠）。而且 `RMDir` **没有 `ClearErrors` / `${Errors}` 检查**，
  紧跟着就把指针 `iaClearBackupDir` 清掉 ⇒ 失败完全静默。
- 探针：`D:\w21-work\probe-backup-path.ps1`，读数 `d3-backup-delete-probe.txt`（14:48:46）：

  | 检查 | 结果 |
  | --- | --- |
  | 6 个 `%LOCALAPPDATA%\Programs\*.old-*` 目录 `[\\?\ +path]`（合法形式） | 全部 **True** |
  | 同一批目录 `[\\?+path]`（**被拼出来的那一串**） | 全部 **False** ⇒ `RMDir` 的目标根本不解析 |
  | 占用检查（对目录内 `python.exe` / 首个文件独占打开 `ReadWrite + None`） | **全部未被占用**（6/6 unlocked） |
  | 对同一个目录做一次普通删除（`Remove-Item -Recurse -Force`） | **立即成功**（我删的是测试 app 那个 279,593 B 的孤儿，作为清理） |

  ⇒ 「删不掉」与文件锁**无关**；是**目标路径拼错 + 无错误检查**。注释里写的
  「长路径前缀让深目录树可删」在现网**从未生效**。

### 3.2 影响盘点：本机 6 个备份目录，产品件 3.50 GiB 无主副本

`d3-backup-delete-probe.txt`（14:48:46 实测）：

| 目录 | 字节 | 说明 |
| --- | --- | --- |
| `Intelligence Agent.old-{0C4DC4D6-…}` | 747,868,706 | 今天之前（2026-10-07/08 的本机安装/更新操作，本机是开发机） |
| `Intelligence Agent.old-{A761CF90-…}` | 747,871,079 | 同上 |
| `Intelligence Agent.old-{DFF3E06F-…}` | 747,169,324 | 同上 |
| `Intelligence Agent.old-{E0017DFB-…}` | 747,834,164 | 同上 |
| `Intelligence Agent.old-{4E897E42-…}` | 764,469,105 | **本文件 §2.2 第 4 条这次更新产生** |
| `IA Installer Test 84063fb8.old-{30C6C427-…}` | 279,593 | §3 的中断/恢复腿产生（**已作为测试残留删除**） |

产品件合计 **3,755,212,378 B ≈ 3.50 GiB**，全部**未被注册表跟踪**（`IaBackupDir` 全空）。
⇒ **每一次成功更新都会在用户机上永久留下约 0.7 GB 的旧版完整副本**，安装器既不回收也不报错。
**我没有删**这 5 个产品件孤儿（属用户数据目录下的删除，需用户决定）；只删了自己测试产生的那 1 个。

### 3.3 门禁前清场（顺带记录）

跑本文件之前清掉了一批**本车道自己留下的**残留进程（读数 `d4-stale-processes.txt`）：
两个 00:16 起的 `pytest`（CPU 6 秒内增量 0.000 s，已挂死）与 Run A pass4 attempt1（402 作废）
留下的 4 个 sample fixture 进程（13:59 起）。全部按 pid + 命令行核对过归属后才停；
用户自己的 `bosshunter` 进程与产品服务未动。

## 4. 腿④ 安装器/卸载器**逐页 UI 双语**（已执行）

- 完整逐页读数在仓库外：`D:\w21-work\evidence\w16-ui-pages\LEG-NOTE.txt`
  （**7,563 B**，sha256 `e86142356c2ea077632563678c6e896a2f27ffe053f35126ce10214285972581`）：en_US 安装 4 页 + 卸载 3 页、zh_CN 安装 4 页 + 卸载 3 页，
  每页都有**渲染文案**（`pageN.txt`）与截图（`pageN.png`，见 `en-install-cua/`、`en-uninstall-cua/`、
  `zh-install-cua/`、`zh-uninstall-cua/`），全程 computer-use 驱动、**不用 `/S`**。
- 判定：两套构建各自 4+3 页全部按构建语言渲染完整成句（不是空串/英文占位）。
  - 已披露的一处混排：zh_CN 安装第 2 页正文首词是字面量 `Setup`（消息模板未本地化）；
  - 四条自定义 `LangString` 中 **`iaAppRunning` 已由 §1 触发**；`iaPerUserOnly` / `iaUpdateFailed` /
    `iaRollbackFailed` 三条**本批仍未触发**（如实登记，`iaRollbackFailed` 需要构造「回滚 rename 失败」）。

## 5. 仍未执行 / 本文件不满足的（如实登记）

| 腿 | 状态 | 原因 |
| --- | --- | --- |
| 干净 Windows x64 VM 复跑 | **未执行** | 本机是开发机（有 `.env`、既有数据根、既有服务），无干净 VM 可用（与 `43`/`51` 同缺口，未变） |
| 运行中更新**安全暂停**（列在途 Task/客户端 → 安全暂停 → Ledger 结清 → 失败中止替换） | **票面语义未实现（实测）** | 发货路径无此实现（§2.3：`runUpdatePreflight` 死代码、无 autoUpdater、无安装器侧 HTTP）。本文件只测到两端：客户端在场 ⇒ 换目录失败即中止；仅服务在场 ⇒ 不中止、不暂停 |
| 服务暂停失败 | **无触发路径（实测）** | 没有暂停实现 ⇒ 没有「暂停失败」分支可触发。可测的两条失败分支是：①换目录 `Rename` 失败 ⇒ 中止（exit 2，§1）；②安装段中断 ⇒ 备份保留 + 指针/孤儿（§3）。`iaRollbackFailed`（回滚 rename 失败）分支**未构造** |
| 迁移中断里的「数据根 schema 迁移」部分 | **未覆盖** | 本安装件的「迁移」= 安装目录换目录（§3 已测）；产品自己的数据根 schema 迁移不在安装器路径上，本腿未构造其中断点 |
| 桌面客户端 托盘 → quit 的显式退出路径 | **未执行** | 承 `43` §关联观察 2；本腿的「客户端不在场」是 `Stop-Process` 造成的（操作者动作，已在 §2.1 披露） |
| 卸载保留真实产品件用户数据 | 本文件**不新增**读数 | 已由 `43` 的 F 腿与 `51` §3 的 `userDataPreserved=true` 覆盖；本文件只补了测试 stub 侧的收尾 |

## 6. 证据清单与复跑命令

- 证据根：`D:\w21-work\evidence\`（仓库外，与既有 W-21 证据同处）
  - `w16-legs\b1-exit.txt` / `b1-app-running-dialog.txt` / `b1-app-running-dialog.png`（§1）
  - `w16-legs\c0-desktop-before-quit.png|txt`、`c1-service-only-dialog.png|txt`、`c1-service-only-exit.txt`、
    `c2-swap-snapshots.txt`、`c2-swap-cancel-exit.txt`、`c3-page1-options.png`、`c3-page2-location.png`、
    `c3-page4-complete.png|txt`、`c3-fullupdate-exit.txt`、`c4-after-update-sessions.png`（§2）
  - `w16-legs\d0-testapp-install-exit.txt`、`d1-interrupt-exit.txt`、`d1-interrupt-snapshots.txt`、
    `d2-recovery-exit.txt`（§3）
  - `w16-legs\d3-backup-delete-probe.txt`（§3.1/§3.2 探针）、`d4-stale-processes.txt`（§3.3 清场）、
    `e2-testapp-uninstall-exit.txt`（§3 收尾）
  - `w16-ui-pages\LEG-NOTE.txt` + `en-install-cua/`、`en-uninstall-cua/`、`zh-install-cua/`、
    `zh-uninstall-cua/`（§4）
  - 产品侧观测：`%APPDATA%\intelligence-agent\logs\agent.jsonl`（14:35:29 / 14:43:33 / 14:45:24 三条）
- 复跑脚本（都在 `D:\w21-work\`）：
  - `run-installer.ps1 -Installer <setup.exe> -Out <file>`（起安装器 + 记 exit code）
  - `snap-swap.ps1 -Out <file> -Tag '<tag>'`（安装目录 / `.old-*` / `IaBackupDir` 快照）
  - `probe-backup-path.ps1`（`\\?\` 与 `\\?` 两种形态 + 占用检查 + 体积）
  - `run-uninstall-testapp.ps1`（测试 app 卸载 + 用户数据目录前后对照）

## 7. 结论与报告级缺陷候选（**不在本票修产品代码**）

1. `51` §5 的五条未执行腿，本轮**四条已在真机执行并留读数**：逐页 UI 双语 = 通过；
   运行中更新 = 两端读数（含「票面安全暂停语义未实现」）；服务暂停失败 = 无触发路径 + 两条失败分支；
   迁移中断 = 硬中断后程序不可用、重跑可恢复但备份永久遗留。**仅「干净 Windows x64 VM」仍未执行**。
2. 三条报告级候选（编号用 **R1/R2/R3**，避开 #809 已用的 D1–D11 口径；供用户裁决是否开票/是否在关单前修，按 §9.1.1）：
   - **R1（高）**：每次更新的 promote 阶段**静默**删不掉旧版备份（`installer-directories.nsh:108`
     的 `\\?` 前缀拼错 + 无错误检查），并把 `IaBackupDir` 清掉 ⇒ **每次更新留下约 0.7 GB 无主副本**
     （本机实测累计 3.50 GiB）。修法方向：写对长路径前缀或在删前 `ClearErrors` 并检查 `${Errors}`
     （失败则不 `iaClearBackupDir`）。
   - **R2（中）**：服务在场时更新不中止也不暂停；更新后旧版服务进程继续应答新客户端
     （复用判据只看 `protocol_version`，见 §2.3）——票面的「安全暂停 + Ledger 结清」在发货路径上
     由死代码 `runUpdatePreflight` 承担，**没有任何在途任务的列举或暂停**。
   - **R3（中）**：硬中断后程序不可用且**不会自愈**（`.onGUIEnd` 不跑），恢复靠「再跑一次安装器」；
     而这次恢复又会命中 R1 ⇒ 票面「迁移失败时恢复旧目录/旧程序可用」只在优雅取消路径成立。
3. 本文件**不构成 #365 的通过判据**：B-5（两次**独立完整通过**）需在**本安装件**上重新取得
   （Run A 桌面真实模型 + Run B kill/reconcile/续跑），那部分依赖真实模型档位，见 `52`/`49` 与
   档位阻塞记录；#365 保持 OPEN。
