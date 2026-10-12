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
- **License**：`app-builder-lib` MIT——但它是**构建期工具，不是运行时依赖、也不是声明依赖**（`desktop/package.json` 无 `dependencies`，devDependencies 只有 `@types/node`/`electron`/`typescript`；CI 在 `windows-installer-smoke.yml:111` 以 `npm install --no-save … electron-builder@26.15.3` 临时装上，该处是版本唯一钉点）。**`desktop/THIRD_PARTY_NOTICES.md` 不含 app-builder-lib 条目（实测零命中）** ⇒ 本票的 MIT 义务不靠该台账，而靠：只复用两行谓词与既有 LangString、不整段复制文件。DSH MIT（`installer-directories.nsh` 头部既有 provenance）。

## 3 决策与范围

- **D1**（裁决 1）：采用方向 2，最小替代；不做方向 1（列在途 session → client-exit → 拒绝/未知中止）。
- **D2**（裁决 2）：兼容性判据不加产品版本/构建号；`protocol_version` 唯一；另加测试钉住「破坏协议必须 bump `protocol_version`」。
- **D3**（裁决 3）：中止条件覆盖服务进程。
- **D4**：交互客户端在场 ⇒ 中止并提示（保留腿①实测行为，也对齐 ②④ 的「不默认强关 GUI」）。
- **D5**：闸门放在 `iaStageApplication` 的 `customInit` 路径上、**rename 与 `iaStashBackupDir` 之前**：所有中止都「旧版本原样」，无需依赖回滚（腿③的回滚链保持只服务中断场景）。
- **D6**（用户裁决 2026-10-12，修复轮）：把闸门调用**移出** `${If} $iaProbeFound == "1"` 分支 —— 即首轮 Axis A 的 P3。**这是行为变更**：缺 app exe 的「修复安装」路径、以及**本机尚无安装的首次安装**路径，从此也会经过闸门。用户明示批准（原话：「我同意了#914 症状的一条边角路径。把闸门移出该分支是行为变更（会改掉「缺 exe 的修复安装」语义）」）。代价与新残余登记在 §6。
- **非目标**：`.old-*` 目录里孤儿服务的生命周期（属 #904 / R3 的 leftover 域，另开票）；`iaRollbackApplication`（恢复页「还原旧版本」）路径的同类盲区（只在已中断安装后出现，且其自带 fail-closed 分支，另记）；两个安装树并存时上游 `KILL_PROCESS` 前缀偏宽的误杀（上游行为，本票仅记录）。这三条与 §6 的窄竞态已合并登记为 **issue #1066**（只登记不修）。
- **已知窄竞态**：`customInit` 与安装 section 之间用户手动启动新客户端/服务（此时 rename 已完成、闸门已过）。触发需要用户在向导运行期间启动一个尚未落盘的 app，且后果可由下一次更新或用户重启消除 ⇒ 本票不引入 section 期二次闸门（另记 §6）。

## 4 实现

### 4.1 改动清单（首轮 4 笔 `239750b8..4b704801` + 修复轮 1 笔 `0e42ae11`；逐文件 blob 见 §5.1 / §5.7）

| 提交 | 文件 | 做了什么 | 依据 |
| --- | --- | --- | --- |
| `e88d7f00` | `desktop/installer/installer-directories.nsh` | 新增 `iaClassifyRunningProduct`（115–119）与 `iaRequireNoRunningProduct`（131–176）；调用点落在 `!macro iaStageApplication` 内（378 行）、`iaStashBackupDir`/`iaReadBackupDir` 与 `Rename` **之前** | 裁决 D1/D3/D5；根因 A（§1.2） |
| `e88d7f00` | `desktop/installer/installer.nsh` | 把闸门挂到 `customInit`；新增 LangString `$(iaAppRunning)` / `$(iaProcessCheckFailed)` | 裁决 D4；② 的 fail-closed 口径 |
| `e88d7f00` | `desktop/scripts/build-windows-installer.mjs` | 构建期把**生产函数原文**注入 r1 探针（`@@PRODUCTION_IA_CLASSIFY@@` / `@@PRODUCTION_IA_REQUIRE@@`），让真机腿跑的是装机文本而非副本 | §9.4（假绿防线） |
| `b38c9195` | `desktop/test/installer-scripts.test.mjs` | 11 例判据规则（`@()` cast、分隔符前缀、答案字母表、未知即 exit 2、调用点先于 rename） | 票面 AC「单测（判据逻辑）」 |
| `b38c9195` | `desktop/test/harness/teeth-check-guards.mjs` | 变异检验：去掉 `@(` ⇒ 规则必须变红 | §9.4（守卫须有牙齿） |
| `b38c9195` | `desktop/test/harness/r1-drivers/process-gate-probe.nsi` + `run-process-gate-probe.ps1` | 真机四腿驱动（none / runtime / client / unknown），**逐腿同时打印 stock 谓词的读数**作对照 | 票面 AC「真机腿」 |
| `b38c9195` | `desktop/test/harness/README.md` | 登记两条驱动、可复制命令与两轮实测读数 | §16.1（进度落点） |
| `90fdc90c` | `desktop/src/host-protocol.ts` + `src/agent_harness/host_service.py` | 常量处写死规则：`protocol_version` 是**唯一**兼容性判据；破坏协议必须两端同时递增；产品版本/构建号永不参与 | 裁决 D2 |
| `90fdc90c` | `desktop/test/host-protocol.test.ts` | 5 例钉子：跨端数值一致、单端递增必红、`version` 串不参与判据、两端规则文本在位 | 裁决 D2「钉住这个不变量」 |
| `90fdc90c` | `docs/adr/0053-protocol-version-sole-compatibility-gate.md` | ADR：为何是唯一判据、代价与边界 | AGENTS §5（重要架构决定写 ADR） |
| `4b704801` | `docs/live_gate/w21/58-issue-914-service-in-use-gate.md`、`docs/agents/reference-sources.md` | 实测根因、方案依据块、决策与非目标；补录领域来源 | 协议 §1.3、AGENTS §6.1 |
| `0e42ae11` | `desktop/installer/installer-directories.nsh` | 闸门调用从 `${If} $iaProbeFound == "1"` 分支**移出**，改到该分支之前（仍在 `iaProbePath` 之后、备份指针与 `Rename` 之前）；头部注释与闸门散文同步更正 | 裁决 D6；首轮 Axis A P3 |
| `0e42ae11` | `desktop/scripts/build-windows-installer.mjs` | `validateProcessGateGuards` 新增 `appBranch` 规则：闸门调用落在该分支**之后**即构建期报错 | §9.4 编码成机制：让「移回分支内」无法静默复发 |
| `0e42ae11` | `desktop/test/installer-scripts.test.mjs` | 新增变异用例（把调用移回分支内 ⇒ 必须红）；`assertProbe` 的 `guard - call < 120` 字符上界换成**语句表断言**（新增的注释与调用本身把该上界顶破） | §9.4（守卫须有牙齿） |
| `0e42ae11` | `desktop/test/harness/teeth-check-guards.mjs` | 为新规则加一条变异；`GUARD_BASELINE_SHA256` 随守卫正文同笔更新 | 该 harness 自身注释要求「与守卫正文同笔更新」 |
| `0e42ae11` | `desktop/test/harness/r1-drivers/stage-damaged-probe.nsi` + `run-stage-damaged-probe.ps1` | 新增真机驱动：1 条对照腿（完整安装仍正常 stage）+ 3 条损坏腿（缺 exe + 运行时在跑 / 缺 exe + 根进程在跑 / 缺 exe + 分类不可证） | 票面 AC「真机腿」；首轮 Axis A P3 的判别器 |
| `0e42ae11` | `desktop/test/harness/README.md` | 登记新驱动、可复制命令与红/绿读数 | §16.1（进度落点） |

### 4.2 关键行 → 依据

- `@(Get-CimInstance …).Count` ← 根因 A：`.Count` 是 PowerShell 的**合成属性**，在单个 `CimInstance` 上不存在 ⇒ 旧写法在「恰好一个匹配」时恒判「未运行」（§1.2 四腿实测）。
- 前缀**带尾分隔符**（`$iaRoot` / `$iaRuntime` 各自以 `\` 结尾）← 上游 `StartsWith('$INSTDIR')` 会命中 `…Agent 2`、`…Agent.old-{guid}` 这类兄弟目录（§1.4）。
- 闸门位置（`customInit`、rename 与备份指针之前）← 裁决 D5：所有中止都「旧版本原样」，不依赖回滚链。
- 答案 `11` ⇒ 只停 bundled 运行时并**复问**（有界 8 次，每次 `Sleep 250`）；`12` ⇒ 弹窗中止；`0` ⇒ 放行；**其余任何答案 ⇒ `SetErrorLevel 2` + `Quit`** ← 裁决 D1/D3/D4 + ② 的 fail-closed。
- `protocol_version` 唯一 ← 裁决 D2 + ADR-0053；产品版本串只在 payload 里透传、不参与判据（`host-protocol.test.ts` 用 `version: '0.0.1-older-than-the-shell'` 作正控钉住）。

## 5 验证读数

### 5.1 冻结与 blob（协议 §8.1 第 1 条）

- 冻结 tip `4b704801aa58134c9cfc77e272e247c5d092bd05` / tree `80f8824fcc93a75e9cfa1308ce59e44f34ad1a0e`；base `239750b81bd1179e3057ad5a29280f46b81f8dcc`（= 当时的 `origin/main`）。
- 改动面逐文件 blob（冻结 tip）：`installer-directories.nsh` `1105ebd1`、`installer.nsh` `64282635`、`build-windows-installer.mjs` `1b85235f`、`host-protocol.ts` `68557aed`、`host-protocol.test.ts` `f813e386`、`installer-scripts.test.mjs` `66a05c41`、`teeth-check-guards.mjs` `d37baa2b`、`process-gate-probe.nsi` `ba943b8a`、`run-process-gate-probe.ps1` `08d37cda`、`host_service.py` `aa3f6df7`、ADR-0053 `1e20ab47`、本证据文件 `bef8f82e`。

### 5.2 focused / 单测 / 真机腿

| 车道 | 命令 | 读数 |
| --- | --- | --- |
| desktop 全套（含 11 例闸门规则 + 守卫） | `npm test`（`desktop/`） | **286 tests / 284 passed / 2 skipped / 0 failed**；其中 `installer-scripts.test.mjs` 110 passed（**首轮树 `4b704801` 读数**；修复轮 287 / 285P / 2S / 0F、`installer-scripts.test.mjs` 111 passed，见 §5.7） |
| D2 钉子 | 同上（`host-protocol.test.ts`） | **5 passed**（跨端数值一致、单端递增必红、`version` 串不参与判据） |
| 类型检查 | `npm run check`（`tsc --noEmit`） | 无输出、rc=0 |
| 守卫牙齿（变异） | `node test/harness/teeth-check-guards.mjs` | 去掉 `@(` ⇒ 规则红（守卫基线 sha 一致） |
| 真机四腿 | `run-process-gate-probe.ps1` | `none` exit 0 / `runtime` exit 0（PID gone）/ `client` exit 2（PID alive）/ `unknown` exit 2；**四腿 `stock=1`**；两轮逐字节一致（仅 PID 不同） |
| 语义等价 | `scripts/check_semantic_equiv.py --a 5412461d --b aa3f6df7` | 「等价」，归一化摘要两侧同为 `fb336f2e011edbf8`；两条负控（注释外改字面量 / 删一条真语句）判「不等价」⇒ 非空转 |

### 5.3 门禁

- **Gate-0（冻结树 `4b704801`，隔离副本，02:27:14）**：收据 `docs/gate/4b704801aa58134c9cfc77e272e247c5d092bd05.json` —— 5/6，❌ 仅 `coverage`，其 ❌ 集合恰为本批三笔代码提交（`e88d7f00` / `b38c9195` / `90fdc90c`），台账行尚未落盘。
- **Gate-0（记账树 `3bcd9f62`，隔离副本，03:16:12）**：收据 `docs/gate/3bcd9f62eba58ab12af5e1c3daefbe248931f27e.json` —— **6/6 PASS**（墙钟 38.5s；`worktree.tracked_matches_head=true`、`untracked_total=0`）。
- **覆盖闸门**：`python scripts/check_review_coverage.py` → exit 0（`089524a~1..HEAD` 每条 commit 均有归属）。
- **全量门禁（冻结树，隔离副本）**：后端 `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q -p no:randomly` → **7758 passed / 43 skipped / 51 deselected / 2 failed / 2586.92s（43:06）**（两条红见 §5.5）；web `vitest run` **119 files / 1681 passed**；`vite build` 成功；`playwright test --workers=2` **540 passed（19.8m，0 flaky）**；`diff-check` / `ruff` / `oxlint` / `tsc` / `guards` 由 Gate-0 覆盖且绿。
- **读数传递（协议 §8.1 第 3 条判据①，首轮：冻结 `4b704801` → 记账树 `3bcd9f62`）**：`git diff --name-status --no-renames 4b704801 3bcd9f62` = **9 条**（冻结 → 交付提交：`reference-sources.md`、两份 `docs/gate/*.json` 收据、两行台账、本证据文件、`SDD_TICKET_TRACKER.md`、`PHASE_STATUS.md`、`phase_status/2026-10.md`），状态仅 `A`/`M`、无 `D`/`R`/`C`/`T`，路径全命中 `DOC_PATTERN` ⇒ 冻结树读数对被交付的代码面有效。

### 5.4 Gate-0 为何在隔离副本里跑

主工作树含另一条线的未提交改动（` M dev.sh`），`gate0.py` 的落盘前置（协议 §8.7 第 3 条：工作树不得偏离 `HEAD`）会 fail-closed 拒绝落盘，而它自己的处置建议明写「**不要** `git stash`」。故按 §8.1 第 6 条的隔离原则建了独立副本（`C:\ia-probe-914\gate-tree`：真仓 `git bundle` 克隆 + CI 同形依赖 `uv sync --frozen --all-extras` / `pnpm install --frozen-lockfile` / `npm ci --ignore-scripts`），两份收据都产自该副本；副本的 `.pth` 指向副本 `src`（§8.1 第 6 条的「混合解析」陷阱已核对）。**未触碰 `dev.sh`。**

### 5.5 全量后端两条红的处置（如实记 2 failed，不写成「绿」）

1. `tests/live_gate/test_deadline_scenario.py::test_uncertain_script_lands_its_side_effect_before_hanging` —— **负载型时序假红**。该例以 `sandbox.exec(…, timeout=1.0)` 给一个 Python 子进程 **1 秒**硬预算，要求它在被杀之前落盘 `uncertain.txt`（`evaluation/live_gate/scenarios/deadline.py` 的 `UNCERTAIN_SLEEP_SECONDS=8.0` 只约束脚本自身的挂起时长，不约束启动）。全量那次与 playwright、两个审查子代理并发（整轮 43:06，按 2026-09-30 的 911.51s / 4849 例外推常态约 24 min ⇒ 本次约 1.8×）。证据：**同一冻结树**上单例复跑 **1 passed（22.9s）**、整文件复跑 **43 passed（22.13s）**；空闲时该解释器启动实测 66–94 ms。已另开票登记为 issue #1069（复跑读数、机理与三条候选修法都在票面）。
2. `tests/tooling/test_review_coverage_immutable_ref.py::test_real_ledger_passes_after_the_431_fix` —— **本批记账顺序的机械瞬态**：该例断言「真实台账在真树上 exit 0」，而冻结树的台账**尚无本批两行** ⇒ 三笔代码提交无归属 ⇒ exit 1。台账行落盘后**同一例在记账树上 4 passed**（rc=0）。仓内既有先例同型（台账自身记有「唯一红 = 真实台账测试的预期瞬态——本行入库即消」，2026-10-01 与 2026-10-03 两处）。

### 5.6 审查

两轴独立只读子代理（非作者）。**首派两轴均因上游 `Model request failed` 未产出任何结论行**（基础设施故障，§8.3 第 4 条定义内），按该条重派 1 次后两轴都取到结论；未使用「替代」通道。

- **Axis A（Correctness/Spec）FAIL，P0-P3 = 0/0/0/1**：唯一 P3 = 闸门落在 `${If} $iaProbeFound == "1"` 分支内 ⇒ 缺 app exe 而 bundled 运行时在跑的外部损坏安装不被分类。**该 P3 已在修复轮（`0e42ae11`）按用户裁决 D6 修掉**（首轮「本票不改，待用户裁决」的表述由 §5.8 与 §6 取代）。
- **Axis B（Standards/仓规）FAIL，P0-P3 = 0/0/2/4**：P2① 本文件 License 字段的 `THIRD_PARTY_NOTICES.md` 指针为假、P2② 仓库内无冻结树 Gate-0 收据；P3① `reference-sources.md` 误标「运行时依赖」、P3② §4-§6 占位（属「缺但已计划」）、P3③ 4 笔直接落在集成 clone 的 `main`、P3④ `reference-sources.md` 补录顺序偏差。逐条处置见两行台账。

两轴**各自独立**复跑并确认：`check_semantic_equiv.py`（`host_service.py` 等价）、`host-protocol.test.ts` 5/5、`installer-scripts.test.mjs` 110/110、真机四腿读数、以及 `coverage` 的 ❌ 集合恰为本批三笔。

### 5.7 修复轮：冻结、真机红证与全量门禁（`0e42ae11`）

- **冻结**：tip `0e42ae118211f48ed38e31045076ebd0464bdfe3` / tree `b7c60bf10953ca4f58e30a63116ac0aa8bee6116`（修复轮唯一代码笔 `0e42ae11`，叠在首轮两笔文档提交 `3bcd9f62` / `261a3188` 之上 —— 实测 `git log`：`3bcd9f62` 02:57 → `261a3188` 03:24 → `0e42ae11` 06:08；其后的提交才是本轮文档笔）。逐文件 blob：`installer-directories.nsh` `9ccc0231`、`build-windows-installer.mjs` `4472ad0d`、`installer-scripts.test.mjs` `c2b68954`、`teeth-check-guards.mjs` `9dd6e382`、`stage-damaged-probe.nsi` `d28c37cf`、`run-stage-damaged-probe.ps1` `521f0a9c`、`README.md` `4ab298a4`。
- **真机红证（`run-stage-damaged-probe.ps1`，生产宏原文注入）**：把驱动指向**修复前**的 include（`-DirectoriesSource`，blob `1105ebd1`）⇒ 对照腿 `intact-clean` 仍绿，三条损坏腿全红、**共 6 条断言失败**：`damaged-runtime` `exit=0` 而 `answer=` **为空**（闸门从未被调用）、`damaged-rootproc` `exit=0`（应为 2）、`damaged-unknown` `exit=0`（应为 2）。空的 `answer=` 是「宏没调用闸门」的直接证据。
- **修复后两轮读数（逐字一致）**：`intact-clean exit=0 stage_returned=1 answer=0 probe=1`（目录被 rename 成 `<dir>.old-{guid}` ⇒ 对照腿证「普通更新路径不回归」）；`damaged-runtime exit=0 answer=0 probe=0`（运行时进程 `gone`）；`damaged-rootproc exit=2 <无读数>`（进程 `alive`）；`damaged-unknown exit=2 <无读数>`。
- **单测 / 类型 / 牙齿**：`installer-scripts.test.mjs` **111 passed**（新增「把调用移回分支内 ⇒ 必须红」用例）；desktop 全套 **287 tests / 285 passed / 2 skipped / 0 failed**；`npm run check` rc=0；牙齿 harness **存活变异 = 0**，守卫逐字节还原（`restored sha256 = 143714f063906161b0b45c0a3ca0c1f988d913d262b8470f3adac14aa226feca`，`identical=true`）。
- **生产安装器编译（修复轮补做，取代首轮 §6 的「未做」）**：`npm install --no-save --no-package-lock --no-audit --no-fund electron-builder@26.15.3` + `node scripts/test-windows-installer.mjs --compile-only` ⇒ en_US / zh_CN 两个生产安装器**编译成功**（`succeeded=true`，rc=0），即改动后的宏在真实 NSIS 构建链里可编译。`desktop/package-lock.json` 与 `package.json` 未被改动（`git status` 实测）。**仍未做**完整安装 / 卸载 smoke（需真机交互），由 PR 的 CI 作业 `windows-installer-smoke` 承担。
- **Gate-0（冻结树 `0e42ae11`，隔离副本）**：收据 `docs/gate/0e42ae118211f48ed38e31045076ebd0464bdfe3.json` —— **5/6**，❌ 仅 `coverage`，且其 ❌ 集合**恰为本轮那一笔代码提交 `0e42ae11`**（台账行尚未落盘）。满足协议 §8.2 第 4 条的「除 `coverage` 外全绿 + ❌ 集合 ⊆ 本批代码笔」判据，机械面清零后才派两轴。
- **全量门禁（冻结树 `0e42ae11`，隔离副本，**不与任何重活并发**）**：后端 `PYTHONPATH= PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q -p no:randomly` ⇒ **7759 passed / 43 skipped / 51 deselected / 1 failed / 1412.74s（23:32）**；唯一红 = `tests/tooling/test_review_coverage_immutable_ref.py::test_real_ledger_passes_after_the_431_fix`，即**台账尚未落盘的机械瞬态**（与首轮 §5.5 第 2 条同型；台账行落盘后该例在记账树复跑转绿）；desktop `npm test` **287 / 285 passed / 2 skipped / 0 failed**；web `vitest run` **119 files / 1681 passed**；`vite build` 成功（10.33s）；`playwright test --workers=2` **540 passed（14.2m，0 flaky）**；`diff-check` / `ruff` / `oxlint` / `tsc` / `guards` 由 Gate-0 覆盖且绿。
- **首轮那条负载型假红未复现**：issue #1069 的 1 秒硬预算假红本轮**未出现** —— 本轮全量单跑、无并发重活，读数里没有它的签名。这与「负载诱发」的归因一致（首轮那次与 playwright、两个审查子代理并发）。
- **读数传递（协议 §8.1 第 3 条判据①，修复轮）**：`git diff --name-status --no-renames 0e42ae11 <交付提交>` = **<TRANSFER_COUNT> 条**，状态仅 `A`/`M`、无 `D`/`R`/`C`/`T`，路径全命中 `DOC_PATTERN` ⇒ 冻结树读数对被交付的代码面有效。

### 5.8 修复轮审查（两轴修后重审，各 1 轮；范围 `4b704801..0e42ae11`）

按协议 §8.8.5：闭合既有 findings + **在新 diff 上至少一次有限发现**（不得零发现）。两轴各自独立只读、互不可见；自写变异都在主工作树**之外**的副本里做，两轴报回的冻结树 `git status --short` 都只有 Gate-0 收据那一个未跟踪文件（无人改动被测树）。

- **Axis A（Correctness/Spec）：PASS，P0/P1/P2 = 0/0/0，P3×2 / P4×1**。首轮 P3 的闭合是**实测**（不是采信作者）：用作者驱动分别指向冻结 include 与修复前 include，得到 §5.7 的红 / 绿两组读数。**自写变异 3 条**，其中 M2（在闸门函数体内加 `StrCmp $iaProbeFound "0" 0 +2` / `Return`，即「被调用但在缺 exe 时立刻返回」）**活过全部 111 条构建期用例**，并可观测地重现首轮缺陷 ⇒ 非零发现。另外核对并确认：`IaBackupDir` 非空 = 「安装 section 已开始」的不变量**逐字保留**（闸门不写注册表，且先于 `iaReadBackupDir` / `iaStashBackupDir` / `Rename`；`iaProbeFound == "1"` 路径的顺序与修复前逐字节相同）；三条拒绝出口都不写记录、不 stage、不 rename；`GUARD_BASELINE_SHA256` 自算相符；`protocol_version` 仍是唯一兼容判据（修复 diff 里无版本 / 构建号判据）。
- **Axis B（Standards/仓规）：APPROVE_WITH_FINDINGS，P0/P1/P2 = 0/0/1，P3×4 / P4×3**。P2 = **交付记录与已交付代码互相矛盾**：本文件 §5.6 / §6、`SDD_TICKET_TRACKER.md`、`PHASE_STATUS.md`、`phase_status/2026-10.md` 四处仍写「本票不改、待用户裁决」并只钉到 `4b704801`（`grep -c 0e42ae11` 四处皆 0）⇒ 本轮的文档笔即该 P2 的处置。**自写变异 3 条**：M1 与 A 轴 M2 同形、**独立复现**「活过 111 条用例」（两轴各自独立撞到同一条，鉴别力最强）；M2（把调用移回分支内）被杀（`111 / 107 passed / 4 failed`，含 `not ok 9 - refuses a gate call that runs only when the app executable is present`）⇒ 新用例不是空转；M3（把调用移到宏首行）红，但红因是 fixture 锚点失配而非规则本身（P4，诊断力问题）。另确认：`.gitattributes` 的实际形态（无目录级规则；`*.ps1 eol=crlf`，`.nsi` 无属性、blob 同为 CRLF，两个新文件与同目录兄弟一致；无 BOM；`worktree == index == HEAD`）；台账行 775 / 778 字符 ≤ 800、`PHASE_STATUS` 行 1199 ≤ 2000，均未超上限。首轮 B 轴 P2① / P2② 与 P3① / P3② 逐条实测 **CLOSED**，且 `0e42ae11` 未触碰这些面。
- **残余处置**：A 轴 P3-1 与 B 轴 F4 是同一条（两轴各自独立复现）；与 A 轴 P3-2 / B 轴 F2（闸门无「目录归属」判据）、B 轴 F5（注释强于机制）、B 轴 P4（`appBranch` 可被改写分支条件绕过）一并登记为修复轮新残余（§6），**本票不修**：协议 §8.8.5 的修后重审预算（1 轮 / 轴）已用满，再改代码会产出**没有审查行覆盖**的代码提交，直接撞 §7 第 8 条的覆盖闸门 ⇒ 按用户「查到问题就另开票」的常设指令留给后续票处理（已开 **#1066** 注释承接同族）。P4 里两条（`.gitattributes` 前提更正、`#1045` 断言替换与失败模型差异）**无缺陷**，如实记为确认项。
- **两轴都取到了结论行**，未使用 §8.3 第 4 / 5 条的「有界收口替代」通道（首轮那次基础设施故障重派不适用于本轮）。

## 6 残余与后续

- **窄竞态 → issue #1066**：闸门通过之后、section `Rename` 之前用户手动启动服务/客户端时，rename 不拦 headless 服务 ⇒ 旧服务从 `.old-*` 继续应答。本票不做 section 期二次闸门（理由见 §3：触发需要用户在向导运行期间启动一个尚未落盘的 app，后果可由下一次更新或用户重启消除）。
- **同源盲区 → issue #1066**：`iaRollbackApplication`（恢复页「还原旧版本」，439–524）反向搬回旧版本时没有「谁在跑」的判据；卸载器 sweep 没有「先尝试停掉 bundled 运行时」的动作；上游 `KILL_PROCESS` 前缀偏宽（本仓只记录，不改上游行为）。
- **无 Windows PowerShell 的机器**：闸门解析不到 `$SYSDIR\WindowsPowerShell\v1.0\powershell.exe` 时分类不可证 ⇒ **中止更新**（exit 2），不再像 stock 那样退回 `tasklist` 并可能静默放行。这是有意的 fail-closed（② 的口径）；Windows PowerShell 5.1 自 Win7 SP1 起随系统预装，实际面很窄，但**语义变化是真的**，写在这里而不是留给用户去发现。 **修复轮起覆盖面变宽（裁决 D6）**：闸门调用移出 `${If}` 分支后，**首次安装**（本机尚无安装、`$INSTDIR` 里没有 app exe）也会走闸门 ⇒ 同样的「分类不可证」现在也会**中止一次全新安装**，而那里没有任何东西需要保护。首轮本段只写了「中止更新」，口径偏窄，此处更正。四条新真机腿都在**已有安装树**上，**没有一条覆盖 `$INSTDIR` 不存在的路径**（登记为本轮残余）。
- **安装路径含单引号**：分类脚本把 `$INSTDIR` 内插进 PowerShell 单引号字符串（**上游同形写法**），路径含 `'` 时分类不可证 ⇒ 中止（真机腿 `unknown` 实测 exit 2）。比 stock 的「静默判未运行」安全，但仍是用户可见限制；与 #1066 同族，一并记录。 同样地，修复轮起这条也覆盖**首次安装**：默认目录是 `%LOCALAPPDATA%\Programs\<product>`，Windows 用户名含 `'` 即命中。
- **完整安装包**：首轮**未做**（当时本机无 `desktop/node_modules`、无已构建安装器、无 staged 运行时 ⇒ 生产安装器跑不起来，故真机腿用 r1 驱动 + **生产函数原文注入**代替，与仓内既有约定一致）。**修复轮补做了编译面**：装上 `electron-builder@26.15.3` 后 `node scripts/test-windows-installer.mjs --compile-only` ⇒ en_US / zh_CN 两个生产安装器**编译成功**（rc=0），见 §5.7。**仍未做**完整安装 / 卸载 smoke（需真机交互），由 PR 的 CI 作业 `windows-installer-smoke` 承担。
- **外部损坏的安装目录（首轮 Axis A 的 P3）—— 已在修复轮修掉（裁决 D6）**：闸门调用原先落在 `iaStageApplication` 的 `${If} $iaProbeFound == "1"` 分支内（`installer-directories.nsh`），缺 exe 而 bundled 运行时在跑的损坏安装从不被分类，stock section 直接就地安装。`0e42ae11` 把调用移到该分支**之前**（仍在 `iaProbePath` 之后、备份指针与 `Rename` 之前），并补了构建期规则、变异用例与真机三腿。**行为变更已由用户明示批准**；它的代价与新残余见下。首轮「本票不改，待用户裁决」的表述由本条取代。
- **修复轮引入的新残余（登记，本票不修，同族交 #1066 / 后续票）**：
  ① **闸门没有「目录归属」判据**（A 轴 P3-2 / B 轴 F2）：`iaRequireNoRunningProduct` 只问「谁在 `$INSTDIR` 下跑」，不问「这个目录是不是本产品的安装」。调用点移出分支后，`iaProbeFound == "1"`（app exe 在位）曾是的唯一归属信号也没有了；在一台「本机无安装、但 `$INSTDIR` 下恰好有 `resources\python\` 布局且有进程在跑」的机器上，答案 `11` 会 `Stop-Process -Force` 掉那些进程。可达面窄（`RequestExecutionLevel user`，只及同用户进程、不提权），故 P3。
  ② **构建期规则钉的是「调用位置」，不是「闸门有效」**（A 轴 P3-1 / B 轴 F4，**两轴各自独立复现**）：两轴各自写出的变异（在闸门函数体内加 `StrCmp $iaProbeFound "0" 0 +2` / `Return`，即「被调用但在缺 exe 时立刻返回」）**活过全部 111 条构建期用例**，同时可观测地重现首轮 Axis A 的缺陷。当前唯一能杀它的判别器是 `run-stage-damaged-probe.ps1`，而该驱动**不在任何 workflow 里**（`windows-installer-smoke.yml` 只跑 `test-windows-installer.mjs`，其 `paths` 甚至不含 `desktop/test/**`）⇒ 这一类回归只有人工、依赖环境的脚本在守。
  ③ **注释强于机制**（B 轴 F5）：`installer-directories.nsh` 新增注释写「is called for every install」，而调用点在 `customInit` 的 `${If} $iaRecoveryPending != "1"` 之内（`installer.nsh:135-138`）⇒ 恢复挂起时既不 stage 也不调用闸门。读者 / 维护者口径问题，非运行期行为。
  ④ **`appBranch` 规则可被改写分支条件绕过**（B 轴 P4）：规则只锚字面量 `${If} $iaProbeFound == "1"`；把该分支改写成 `StrCmp` 形式即绕过它（此时 #1045 的语句表断言兜住）。属纵深防御的第二道，规则自身注释的范围被高估。
  ⑤ **首次安装路径无腿覆盖**（A 轴 P3-2 / B 轴 F3）：见上两条（PowerShell / 单引号）——「`$INSTDIR` 不存在」这一路径四条新腿一条都没盖。

- **同批发现的测试基建缺陷 → issue #1069**：§5.5 第 1 条那条负载型假红不是本票语义的问题，而是该用例把「Python 子进程启动 + 落盘」压在 1 秒硬预算里（预算按「脚本已在跑」估的）。按用户「查到问题就另开票」的常设指令开票，票面含全量 / 单例 / 整文件三组读数、机理与三条候选修法，留给独立一轮处理。
- **外来改动**：工作树里的 ` M dev.sh` 是另一条线的未提交工作，本票未触碰、未纳入任何提交；它也是 Gate-0 读数改在隔离副本里取的原因（§5.4）。
