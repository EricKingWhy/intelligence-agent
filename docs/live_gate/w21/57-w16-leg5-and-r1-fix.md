# W-16 腿⑤（本机干净环境替代腿）+ #901 R1 修复（2026-10-09）

父票 #365 [W-21]。**本文件是这两项的唯一正本**，回填状态：

- 腿⑤：**已执行**（按用户指令以本机替代干净 VM）；真 VM 复跑**仍未做**（用户已豁免，见 §1 用户指令）。
- R1（#901）：代码修复链已落地并冻结；**轮5 窄闭合复验判 FAIL（P0=0 P1=1 P2=1 P3=6，R1–R8）** ⇒ 按 SDD 协议 §8.3 第 4 条末段**停止修复、登记残余**（R1–R8 已登记进 #905，见 §2.6）；AC2 机制偏离经用户追认（2026-10-09）。#365 的关单口径见 §3。

## 1. 腿⑤：干净环境冷启动冒烟（本机替代 VM）

**用户指令（2026-10-09，原文）**：「1.不需要vm，直接用我电脑Windows跑就行了。」⇒ 原票面「干净 Windows x64
VM 复跑」改为**本机干净环境**：新 profile 目录 + 最小环境变量，不改机器其余状态。

**方式**：`D:\w21-work\leg5-clean\profile` 作为新 `APPDATA`/`LOCALAPPDATA`；子进程环境 =
`PATH='C:\Windows\system32;C:\Windows'`，无 `PYTHONPATH` / `MODEL_*` / `AGENT_*` / `.env`（转写第 14 行逐字记录）；
被驱对象是已安装产物 `C:\Users\王浩宇\AppData\Local\Programs\Intelligence Agent`。开跑前先记录用户自有实例的
pid 集，收尾后逐字复核。

**读数**（证据：`D:\w21-work\evidence\w16-legs\leg5-transcript.txt` 8 段 + `leg5-window.png` 107,315 B）：

- 桌面腿：`ready after 11s`；`windowTitle='Agent Harness Inspector'`；`/api/health` =
  `{"status":"ok","checkpoint_save_failures":0,"protocol_version":1,"version":"1.0.0","capabilities":[],"auth_required":true}`。
- 新数据根完整建出：`workspace\harness.db` 167,936 B、`workspace\.host-service.json` = `pid 12188 / port 62597 /
  owner ia-clean / protocol_version 1`、`host-credentials.json` 257 B、`.instance.lock` 138 B（另有 Electron
  缓存族，属正常首跑）。
- 进程树 = 壳 + 服务 `python.exe`（产物内 `resources\python`）+ 两个渲染进程 + `conhost`；收尾优雅关闭后再强杀，
  用户自有实例 5 个 pid（12120/7104/39600/21140/37260）**前后逐字相同**；真实数据根 18 个 session **未被触碰**。
- TUI 腿（同一新 profile）：`ia-tui.cmd --check` **rc=0 / 6 s**，起服务 pid 38372/port 62265，`GET /api/sessions`
  带凭据 **200** / 不带 **401**；服务进程路径在安装目录内（owner 由端点文件核对）。字节级捕获（`leg5c`）：
  stdout 369 B、stderr 0 B。⚠ 控制台直接读时中文行出现乱码（codepage），同一行在 `leg5c` 的文件重定向里按
  UTF-8 读**逐字正常** ⇒ 乱码是控制台解码现象，不是产品输出缺陷。
- 收尾：`DONE wall_seconds=23`。

**残余（如实登记）**：① 真 VM 复跑未做（用户豁免；环境因素如「未装 VC++ 运行库/杀软拦截」因此未覆盖）；
② 「托盘 → quit」显式退出路径仍未实测（与 `43`/`51` 同缺口）；③ 本腿未驱动真实模型（额度口径见 §3）。

## 2. #901 R1：更新时旧版备份删不掉且指针被清

### 2.1 根因（实测）

`desktop/installer/installer-directories.nsh` 的 promote 阶段用 `RMDir /r "\\?$iaBackupDirectory"` 删上一版
备份。**NSIS 字符串没有反斜杠转义**，所以该字面量拼出的是 `\\?C:\…`（`?` 后缺分隔反斜杠）——Win32 要求
`\\?\` 后紧跟分隔符，这个路径**不可解析**，删除必然失败；而 `RMDir` 后面没有 `${Errors}` 检查，紧接着
`iaClearBackupDir` 无条件把指向备份的注册表指针清掉 ⇒ **每次成功更新都静默留下约 0.7 GB 无主目录**，
且没有任何引用再指向它。本机实测 **5 个产品件孤儿 ≈ 3.50 GiB**（未删除，属用户数据目录，见 §2.5 残余）。

### 2.2 修复链（本分支 `docs/w21-365-residual-correction`）

| 笔 | 内容 |
| --- | --- |
| `bcda1acd` | （R1）删除改用合法长路径前缀 `\\?\`，失败可发现 |
| `04c46641` | 守卫改 fail-closed、引号扫描按 makensis 实测改正、残留记录随目录存在性清理 |
| `7b3a5d73` | 残留记录探测改用长路径前缀；守卫窗口补 `!macroend` 边界 |
| `d200a081` | 记录去留改为**两种探测形态都答「已消失」才删**；守卫补死分支/嵌套/记录值三项 |
| `d084240f` | 处置批复验 11 条 findings 的范围内收敛 |
| `2b77d19d` | 轮5 findings F1–F7 收敛（绝对深度 / open-above / 大小写 / 完整 closers / `#` 注释 / 记录键钉住 / 块内 `ClearErrors` 窗口） |

产品面只动 `desktop/installer/installer-directories.nsh`（另加 `desktop/installer/README.md` 的语义说明）；
守卫与夹具在 `desktop/scripts/build-windows-installer.mjs` 与 `desktop/test/installer-scripts.test.mjs`。
shipped `installer-directories.nsh` 的 blob 自 `d084240f` 起未再变（`52f8aa7b…`），轮5 只改守卫与测试。

### 2.3 行为语义（修后）

promote：`ClearErrors` → 删除（合法 `\\?\` 形态）→ `${Errors}`：

- 删除成功 ⇒ 清指针，安装完成；
- 删除失败 ⇒ **指针照样清空**，但把备份路径写进 `HKCU\<INSTALL_REGISTRY_KEY>\IaLeftoverDir`、`DetailPrint`、
  并弹一次用户可见提示（静默安装 `/S` 下自动确认）；下一次 promote 读回该记录，**只有两种探测形态
  （先无前缀、后带 `\\?\`）都看不见那个目录**时才把记录清掉。
- 指针为什么必须清：`.onGUIEnd` / `iaRollbackApplication` 把「`IaBackupDir` 非空」读作**安装段没跑完** ⇒
  留着指针会把一次**成功的更新**回滚掉。⇒ 与 #901 的 AC2 字面（「不清指针」）**不一致**，见 §2.6。

### 2.4 验证（全部在冻结树上，证据在仓库外）

- **单测**：`desktop` `node --test test/installer-scripts.test.mjs` ⇒ **57 / 57**（含守卫 40+ 条反例夹具）。
- **牙齿检查**（仓外 harness，原地变异后逐字节还原并核对）：
  - nsh 形状变异 **6/6 变红 / 0 存活**（`evidence/teeth-check-round5.log`；含 F1 构造：整段受守卫 body
    包进 `${If} 1 == 0`，形状测试与守卫双红）；
  - 守卫规则变异 **10/10 变红 / 0 存活**（`evidence/teeth-check-guards-round5.log`）。
- **真机端到端**（shipped blob `52f8aa7b…` 的安装件，脚本 `drive-r1-update.ps1`）：
  - success 腿 **16 PASS / 0 FAIL**（`evidence/final4-success-fixed.log`，`assertions_failed=0`）；
  - failure 腿 **24 PASS / 0 FAIL**（`evidence/final4-failure-fixed.log`）：`pointer_cleared IaBackupDir=`、
    `leftover_recorded IaLeftoverDir=` **等于真实残留目录** `.old-{691E3EBA-…}`、`record_kept_while_dir_exists`、
    解锁后 `stale_record_cleared IaLeftoverDir=`。
- **新守卫规则接受 shipped nsh**：`evidence/build-final5.log`（两个 `.nsh` blob 与 final4 相同；
  守卫在生产路径 `createWindowsInstallerConfig → validateInstallerScripts`（`:1163`）上运行）。
- **轮5 复验独立复核**（独立只读子代理；三文件 blob 与 `2b77d19d` 相同：守卫 `6f9822dc…` / 测试 `efdff888…` / nsh `52f8aa7b…`）：57/57；`teeth-check-round5.log` 6/6、`teeth-check-guards-round5.log` 10/10 均全红 0 存活且逐字节还原。nsh blob 自 final4 未变 ⇒ 上面两条真机腿读数仍归属本修复。
- **台账行**：`docs/review_ledger.d/t365-w21-r1-*.tsv` 四行（窄闭合复验 / 处置批复验 / 处置批 findings / 轮5 复验）。

### 2.5 审查轮次与结论

| 轮 | 对象 | 结论 |
| --- | --- | --- |
| 轴 A / 轴 B 窄闭合复验 | `854a0bdd..7b3a5d73` | PASS-WITH-FINDINGS、**P0=P1=0**；findings 全处置 |
| 单代理窄复验 | `7b3a5d73..d200a081` | PASS-WITH-FINDINGS、P2=1 + P3=10；7 修 / 4 写成守卫边界 + 登记 #905 |
| 处置批复验 | `d200a081..d084240f` | **FAIL**：P0=0 **P1=1** P2=3 P3=3（F1–F7）⇒ 在轮5（`2b77d19d`）收敛 |
| 轮5 窄闭合复验 | `d084240f..2b77d19d` | **FAIL**：P0=0 P1=1 P2=1 P3=6（R1–R8）；F1 命名构造 / F3 / F4 / F6 闭、F2 五个正则行为正确、F5 前三例与 makensis 一致、F7 委托两例闭合，残余见 §2.6 ⇒ 按 §8.3 第 4 条末段**停止修复、登记残余**（非自动再修） |

### 2.6 待裁决与残余

- **AC2 偏离已获用户追认（2026-10-09）**：AC2 字面「删除失败不清指针」由实现改为「照样清指针 + `IaLeftoverDir`
  记录 + DetailPrint + 用户可见提示」；理由见 §2.3（`.onGUIEnd` / `iaRollbackApplication` 把非空 `IaBackupDir`
  读作安装段未完成，留着会把一次成功更新回滚掉）经用户裁决采纳 ⇒ #901 按实现口径关单；若日后要求回到字面，最小
  替代 = 引入独立的「删除失败」标志位，让回滚判定不再依赖 `IaBackupDir` 是否非空（属新机制，需单独立项）。
- **轮5 复验残余 R1–R8（登记不修，转 #905）**：R1（P1）块计数正则与测试本地清单无 /i，混合大小写
  `${iF}` / `${eNdIf}` 使 F1 类死分支以合法拼写复活（删除+记录+清理全部不执行而守卫返回空表、套件全绿）；
  R2（P2）clear 移进兄弟 `${Else}` 分支后相对深度规则看不见；R3–R8（P3）= 记录/读取死分支、宏定义内
  ClearErrors 计入、紧贴删除点的死分支 ClearErrors、`CLOSES_FUNCTION` 无夹具、紧贴字符串的 `#` 注释、
  测试侧 closer 清单缺三项。**处置依据**：R1/R2 落在轮5 修复新引入的代码面 ⇒ SDD §8.3 第 4 条末段 /
  §8.8.5 的出口 = 停止修复 + 登记 + 交用户裁决（非自动再修）；本票守卫硬化链已超每轴 1 轮预算。
  **残余风险 = 低**：守卫是构建期绊线，同一失效模式由 §2.4 的真机 success / failure 两腿兜底。用户可另行指示开修复票。
- **残留 ①（#904）**：R1 同族「静默孤儿」路径（回滚失败留目录无记录、指针覆写丢上一条备份、卸载删记录留目录、
  `RMDir /r` 跟随 junction、注册表可写值驱动递归删除未做形状校验、UNC 安装根、记录单槽）。
- **残留 ②（#905）**：守卫文本扫描边界（`${Errors}` 计入字符串字面量、单引号/反引号实参、`!include` 与
  未插入宏内的删除点不可见等；本轮 R1–R8 的复现构造已以评论登记进该票）。
- **本机 3.50 GiB 无主备份**：用户裁决**保留**（2026-10-09；属用户数据目录，安装器此后不再引用）。

## 3. #365 关单口径（本文件写作时的状态）

用户 2026-10-09 指令：「4.功能齐全就行的口径以自动门禁 + 廉价冒烟收口」+「真实模型不能耗尽我额度…简单测试一下
就行了，功能齐全就行了」⇒ 票面原「两次独立完整通过（真实模型）」**不再重跑**；关单依据改为
**自动门禁（全量 13 车道 + Gate-0 + coverage） + 廉价冒烟（腿⑤ 干净环境 + R1 真机两腿）**。R1 按用户指令
「关单前修好」——代码已修并冻结；AC2 口径已获用户追认（§2.6），轮5 复验残余 R1–R8 按协议登记（§2.6 / #905）。

## 4. 第 7 次先回后正同步（2026-10-09）

- `origin/main` 自 `3191ab71` 进 25 笔 / 42 文件到 `f769ad4d`（代码面 = `tui/**` 图片粘贴族 + `src/agent_harness/skills/inspection.py`，与 R1 面零交集）。
- 冲突恰 2 文件（`tests/evaluation/test_smoke.py`、`tests/test_cli_stuck_resume.py`）：两侧各自独立修同一处 #865 红；blob 级 diff 证明 main 版 = 我方新增内容的逐字超集（差异仅注释措辞与 import 位置）⇒ 按 §14.7 报告后经用户批准「采 main 版」。
- 合并 `b4c72bc6`（父 `f8dd6e73` + `f769ad4d`）：解决后两文件 blob 与 `origin/main` 逐字相同；`git diff-tree --cc -r -p` 仅提交头 ⇒ 零新增组合内容 ⇒ 机械归属行 `docs/review_ledger.d/t365-w21-sync-merge7-f8dd6e73-b4c72bc6.tsv`。
- 定向复验：`tests/evaluation/test_smoke.py` + `tests/test_cli_stuck_resume.py` + `tests/model/test_reasoning_effort.py` **32 passed**；desktop 守卫套件 **57/57**。
