# #1226 干净 Windows x64 真机复跑 W-16 安装器（安装 → 运行 → 卸载）

> 承接 #926 登记的 5 条未执行项中的**第 1 条**（`docs/live_gate/w21/51-w16-remaining-legs.md` §5：
> 「干净 Windows x64 VM 复跑 | 未执行 | 本机是开发机；无干净 VM 可用」）。本文件在**一台没有任何
> Intelligence Agent 安装/服务/用户数据的 Windows 真机**上，用**本仓源码重新构建的安装件**完整复跑
> W-16 的安装 → 运行 → 卸载链路，并把读数与 #926 / `43-w16-smoke-artifact.md` 的既有读数逐条对照。
>
> **本文件不宣布任何 Gate 通过、不 merge、不关单。**（票面 §「不做什么」）

---

## 0 执行器与环境

| 项 | 值 |
| --- | --- |
| 机器 | 腾讯云 Windows Server 2022 Datacenter，build **20348**，version 10.0.20348，x64，主机名 `10_1_0_15` |
| 执行方式 | Windows 真机上的 CodeBuddy CLI（`codebuddy --model deepseek-v4.1-flash -p`）经 SSH 驱动 |
| 源码 | `C:\ia`（**origin/main 打包传输，非 git 工作副本**；本机无 `.git`，提交/开 PR 在 Linux 侧做） |
| 工具链 | node v22.23.3 / npm 10.9.9 / pnpm 12.11.2（corepack）/ host python 3.13.7 / uv 0.13.1 |
| 构建期工具 | electron-builder **26.15.3**（仓内固定版）；electron 实际解析为 **44.8.0** |
| 内置运行时 | CPython **3.12.14**（`python-runtime.lock.json`）、Node **24.21.0**（`node-runtime.lock.json`） |

**镜像（GitHub 直连实测慢，按票面用镜像）**：本机实测 `github.com` ≈ **54 KB/s**、
`files.pythonhosted.org` ≈ **43 KB/s**；`registry.npmmirror.com` 二进制镜像 ≈ **8–13 MB/s**、
`pypi.tuna.tsinghua.edu.cn` ≈ **1.2 MB/s**。故：CPython 归档走 `registry.npmmirror.com/-/binary/
python-build-standalone/`、wheel 闭包走 `pypi.tuna.tsinghua.edu.cn`、Node 走
`registry.npmmirror.com/-/binary/node/`、electron / electron-builder-binaries 走
`registry.npmmirror.com/-/binary/`。

**关键约束（未改产品代码）**：`prepare_python_runtime.py` / `prepare_node_runtime.py` 按 lockfile 的
SHA-256 跳过已缓存文件，本身不读镜像环境变量。为不改源码，只把**已按 pin 校验过 SHA-256** 的下载件
预置进 gitignored 的 `.scratch/python-runtime-cache/{downloads,wheelhouse}` 与
`.scratch/node-runtime-cache/downloads`（脚本随后打印 `[skip] … (sha256 ok)`）。
`electron-builder` 不在 `desktop/package.json`（脚本 lazy-require，见 `build-windows-installer.mjs:12`），
按仓内既有约定（`windows-installer-smoke.yml:165`、`58-issue-914-service-in-use-gate.md` §5.7）以
`npm install --no-save --no-package-lock electron-builder@26.15.3` 临时安装；实测
`desktop/package.json` / `package-lock.json` **未被改动**（`sha256sum -c` 核对 OK）。

---

## 1 环境前态取证（安装前，2026-10-16 15:39）

| 断言 | 命令 | 读数 |
| --- | --- | --- |
| 无既有用户数据 | `Test-Path $env:APPDATA\intelligence-agent` | **False** |
| 无既有安装目录 | `Test-Path $env:LOCALAPPDATA\Programs\Intelligence Agent` | **False** |
| 无既有卸载注册表项 | `HKCU\…\Uninstall` × `DisplayName -like '*Intelligence*'` | **0 条** |
| 无相关服务 | `Get-Service` × Name/DisplayName | **0 条** |
| 无相关进程 | `Get-Process` × Name | **0 个**（仅腾讯云 `BaradAgent`/`sgagent`/`tat_agent` 监控代理，与本产品无关） |
| 交互桌面 | `qwinsta` | `services` 会话 0（当前 SSH 所在，**断开**）；`console` 会话 1 已连接但**无登录用户** ⇒ **无可用交互桌面** |

> 「干净」判据 = 上表**全部为无**。这是 #926 当年缺失的条件（其读数取自开发机，有 `.env`、既有用户
> 目录、既有服务，见 `43` §环境）。

---

## 2 构建安装器（本机源码 → 全新安装件）

命令（`desktop/` 下，均带镜像环境变量）：

```
python desktop/scripts/prepare_node_runtime.py          # → [done] v24.21.0 at …\staging\node\node.exe
python desktop/scripts/prepare_python_runtime.py        # → [done] …\staging\python\python.exe imports agent_harness (intelligence-agent==1.0.0)
cd web  && corepack pnpm run build                      # tsc -b && vite build  → web/dist/index.html
cd tui  && npm run build                                # tsc -p tsconfig.json → tui/dist/src
cd desktop && npm run build                             # tsc -p tsconfig.json → dist/src/main.js
cd desktop && node scripts/build-windows-installer.mjs  # electron-builder → dist-installer/
```

构建期自检（脚本内）全部通过：`lockfile OK: cpython 3.12.14 (70 pinned wheels) + product
intelligence-agent==1.0.0` / `node lockfile OK: 24.21.0` / `installer LangString guards OK` /
`installer inputs OK` / `staged product matches the checkout`。

**产物**：`desktop/dist-installer/Intelligence-Agent-Setup-0.1.0.exe`

| 字段 | 值 |
| --- | --- |
| 字节数 | **213,265,521** |
| sha256 | **6341b7c23a1dc7ec8c884205ed370c676b72eb8e2e940ee3755472d68216aeca** |
| 版本 | `0.1.0`（app exe 产品版本 `0.1.0.0`） |
| appId | `com.intelligence-agent.desktop` |
| `lockfileSha256` | `644fa21e0dfd2c0c37b30289852225345aa90cc08c51989a2946f18f7efc0197` |
| `nodeLockfileSha256` | `1a2e7800b68eef1b6fedd777094d596213c8df56698ab7b4532258790ecd5142` |
| `productCodeSha256` | `7e2237a565d9b2129e457b74406a7a4430b500d2f13e8b7120134cdf4f16bf96` |
| `builtAt` | `2026-10-16T07:38:55.353Z` |
| 产品 wheel | `intelligence_agent-1.0.0-py3-none-any.whl` sha256 `8275e9e16fb86fdb7704d6699b45bc27ea2b8a5c0a429166f39ee71aa2331967` |

> `installation-build.json` / `SHA256SUMS.txt` 与实测 `sha256sum` **一致**。

---

## 3 安装 → 运行 → 卸载读数（腿名沿用 `43-w16-smoke-artifact.md`）

驱动脚本（`.scratch/evidence/w16/`，gitignored scratch；**本节数字即权威读数**，日志路径为旁证）：
`install-and-verify.ps1`、`run-and-exit.ps1`、`window-probe.ps1`、`update-uninstall.ps1`。

### 腿 A/B — 静默安装（`…Setup-0.1.0.exe /S`）

| 项 | 读数 |
| --- | --- |
| 安装器 exit | **0** |
| 墙钟 | **248.3 s** |
| 安装目录 | `%LOCALAPPDATA%\Programs\Intelligence Agent`（per-user，无 UAC） |
| 关键文件 | `Intelligence Agent.exe` ✓、`ia-tui.cmd` ✓、`resources\{python,node,web,tui,mxc}` ✓、`resources\web\index.html` ✓、`resources\python\python.exe` ✓、`resources\node\node.exe` ✓ |
| 版本 | 产品版本 `0.1.0.0`（FileVersion `0.1.0`） |
| 卸载注册表项 | 1 条：key `ad4c8292-94ef-5e7e-addf-1a5d7241abe1`，`DisplayName=Intelligence Agent 0.1.0`，`DisplayVersion=0.1.0`，`InstallLocation` 正确 |
| 快捷方式 | 开始菜单 1 个 + 桌面 1 个（`Intelligence Agent.lnk`） |
| 安装对用户数据的影响 | **未创建** `%APPDATA%\intelligence-agent`（安装不碰用户数据） |

### 腿 C — 启动（桌面）

| 项 | 读数 |
| --- | --- |
| 进程 | Electron 进程树（4 → 稳定 3 个 `Intelligence Agent.exe`） |
| **主窗口可见** | **否**：进程全部运行在**会话 0**（SSH 所在），只创建了**隐藏**顶层窗口 `Chrome_WidgetWin_0`（`vis=False`）与 `Base_PowerMessageWindow`，**无可见窗口** |
| 后端服务 | `resources\python\python.exe` 起，**pid 6084 / port 57375**（首轮；重跑为 4996、5256/57529 等，每次不同） |
| 服务父子关系 | 服务 **ppid = Electron 主进程 pid**（实测 `service pid=4996 ppid=4312`） |
| 用户数据根 | `%APPDATA%\intelligence-agent` 创建：`workspace\{sessions,workspaces,harness.db,.host-service.json}`、`host-credentials.json` 均在 |
| 端点记录 | `.host-service.json`：`pid/port/protocol_version=1/auth_required=true/owner=administrator/service_uuid` 齐备 |
| 首启耗时 | 端点文件首次出现在**首次启动后 63.5 s**；二次启动即时出现（首启含一次性初始化） |
| 应用日志 | 出现 `error` `GET /api/models` → `agent_harness.model.config.ConfigError: provider 'deepseek' 缺少 API key，必须在 .env 中配置 MODEL_API_KEY`（**干净机无 `.env`**，见 §5 差异） |

### 腿 D — 退出

| 项 | 读数 |
| --- | --- |
| 桌面进程退出后 | `Intelligence Agent.exe` 计数 = **0** |
| 服务子进程 | **随之退出**（计数 = 0）——服务是桌面的**子进程**（ppid=Electron 主进程），强制结束桌面进程树时一并结束 |
| 端点记录 | `.host-service.json` **逐字段保留**（`pid 6084/port 57375` 原样，未清） |
| 用户数据 | 不变 |

### 腿 E — 就地更新（重装同一安装件）

| 场景 | exit | 墙钟 | 结果 |
| --- | --- | --- | --- |
| **产品未运行**（隔离复现，2026-10-16 15:52） | **0** | **205.1 s** | 关键文件齐备、无 `.old-{guid}` 残留、版本不变 |
| **产品仍在运行**（2026-10-16 15:47，桌面进程刚结束 ~6 s） | **2** | **9.1 s** | 旧版本**未被改动**（app exe 仍在且随后可正常启动）——与 `installer.nsh:92` 的 `iaAppRunning` 文案/进程闸门语义一致（`SetErrorLevel 2`，见 `installer.nsh:129/200/243`、`installer-directories.nsh:197`） |

更新前后用户数据指纹**逐字节不变**：`workspace\harness.db` = `a4ad0648…`、
`host-credentials.json` = `4752b048…`、`.host-service.json` = `505e4bce…`。

### 腿 F — 卸载（`Uninstall Intelligence Agent.exe /S`）

| 项 | 读数 |
| --- | --- |
| 卸载器 exit | **0** |
| 墙钟 | **14.5 s** |
| 安装目录 | **已完全消失**（`Test-Path` = False） |
| 卸载注册表项 | **0 条** |
| 快捷方式 | 开始菜单 **0**、桌面 **0** |
| **用户数据（票面核心断言）** | `%APPDATA%\intelligence-agent` **完整保留**：`workspace\harness.db` sha256 `a4ad0648…` **逐字节不变**、`host-credentials.json` sha256 `2d485261…` **逐字节不变**、共 89 个文件 / 7.4 MB |

> **一次隔离复现的旁证（不计入上表读数，据实登记）**：第一次卸载（紧接在强制结束刚运行的桌面进程后
> 约 4 s）exit 0、17.8 s，但残留了 **8 个空目录**（`resources\python\Lib\site-packages\openai\
> types\realtime\…`，0 字节）。**在上表这次「无进程、已 settle」的干净复跑中，同一卸载器 exit 0 且
> 安装目录完全消失** ⇒ 那次残留归因于**刚被强制结束的进程留下的文件句柄**（句柄未释放时 `RMDir` 失败），
> **非卸载器判据失效**。这是「环境/flake 需有隔离复现证据」的红线要求，故在此单独记录。

---

## 4 仓内安装器烟测（票面 §4）

在 `desktop/` 下（带镜像环境变量）：

| 命令 | 读数 | 判定 |
| --- | --- | --- |
| `node scripts/test-windows-installer.mjs` | `en_US` `INSTALL_OK …`、`zh_CN` `INSTALL_OK …`；`Installer test artifacts: … (succeeded=true)`；rc=**0** | 通过 |
| `node scripts/test-windows-installer.mjs --uninstall-only` | `en_US` `UNINSTALL_OK userDataPreserved=true`、`zh_CN` `UNINSTALL_OK userDataPreserved=true`；`(succeeded=true)`；rc=**0** | 通过 |
| `node scripts/test-windows-installer.mjs --compile-only` | `… (succeeded=true)`；rc=**0**（en_US + zh_CN 两个生产安装器均编译成功） | 通过 |

读数与 `51-w16-remaining-legs.md` §3 **逐字一致**（同两条 `UNINSTALL_OK userDataPreserved=true` 与
`succeeded=true`）。烟测后已用各自的卸载器清理临时安装（`IA Installer Test <id>`），
`Programs` 目录与注册表回到无项。

---

## 5 与 #926 读数的差异（票面 AC 2）

| 腿 | #926 / `43-w16-smoke-artifact.md`（开发机，安装件 `ce5fd54a…` 204,375,375 B） | **#1226（干净机，安装件 `6341b7c2…` 213,265,521 B）** | 差异性质 |
| --- | --- | --- | --- |
| B 静默安装 | exit 0，**299.2 s** | exit 0，**248.3 s** | 同向通过；耗时不同（载荷/机器不同） |
| C 启动 · 窗口 | **4.9 s 主窗口可见**（`Chrome_WidgetWin_1` / `Agent Harness Inspector`） | **无可见窗口**（会话 0，仅隐藏 `Chrome_WidgetWin_0`） | **环境差异**：干净机经 SSH 无交互桌面（§1），非产品缺陷 |
| C 启动 · 服务 | pid 7920 / port 58469 | pid 6084 / port 57375（首轮） | 同类读数，pid/port 每次不同 |
| D 退出 · 服务存活 | 服务**存活**（进程**由 TUI 冷启动**，与桌面无父子关系） | 服务**随之退出**（服务是**桌面**的子进程，ppid=Electron 主进程） | **路径差异**：本机由**桌面**拉起服务，强杀桌面进程树连带结束；#926 的服务由 TUI 独立拉起 |
| E 就地更新 | exit 0，**217.9 s** | exit 0，**205.1 s**（无进程）；exit 2 / 9.1 s（**有进程**） | 同向通过；补测了「有进程」触发闸门拒绝的分支 |
| F 卸载 | exit 0，**9.0 s**，数据保留 | exit 0，**14.5 s**，目录完全消失，数据逐字节保留 | 同向通过 |
| （#926 §1）旧会话/模型配置可见 | 通过（16 条旧会话 + 模型选择器 `glm-5.3-flash`） | **不适用**：干净机无任何历史会话、无 `.env`/模型配置 | 干净机**结构性不可复现**（该腿前置「先有会话」，本属 #365 的 Run A/B） |
| （#926 §4）磁盘满回退 | 仅逻辑/单测 | 未跑（同 #926 口径：不真填磁盘） | 未执行，如实登记 |

**差异根因说明（据实，不臆断）**

1. **无可见窗口** = 会话 0 无交互桌面。`window-probe.ps1` 用 `EnumWindows` 逐 pid 枚举，仅得两个
   **隐藏**顶层窗口；`qwinsta` 显示 `console` 会话 1 无登录用户 ⇒ 本机不存在可显示 GUI 的桌面会话。
   这**不是**产品缺陷，但确实是「#926 在开发机上拿到的窗口可见读数在干净机上不成立」的**如实差异**。
2. **服务随桌面退出** = 服务进程由桌面（Electron 主进程）spawn，Windows 下强杀进程树连带子进程。
   与「一个数据根一个服务、客户端从不杀服务」**不矛盾**（此处是**强制终止**，非产品退出路径）；
   但 #926 已登记的「托盘 → quit 的显式退出路径未实测」缺口在本机**同样未覆盖**。
3. **`/api/models` 报 `ConfigError`** = 干净机无 `.env`，缺 `MODEL_API_KEY`。此为**预期**：
   安装器不携带模型配置；#926 的开发机有既有配置。

---

## 6 诚实缺口（仍未执行 / 本机不可执行）

| 腿 | 状态 | 原因 |
| --- | --- | --- |
| 桌面窗口可见性 / GUI 交互 | **未执行** | 会话 0 无交互桌面（SSH 驱动） |
| 一次真实短任务（Run A / Run B） | **未执行** | 干净机无模型 API Key；无真实 LLM 凭据 |
| 旧会话与模型配置可见 | **不适用** | 干净机无历史会话/配置 |
| 运行中更新安全暂停（列在途 Task → 安全暂停 → Ledger 结清） | **未执行** | 需在途任务；本机「有进程」时触发的只是进程闸门**拒绝**分支，非安全暂停分支 |
| 服务暂停失败 / 迁移中断 / 磁盘满回退 | **未执行** | 与 #926 §5 一致（需构造故障组合；磁盘满按用户指令不真填） |
| 卸载器逐页 UI 双语点击 | **未执行** | 烟测走静默装/卸（同 #926 §3 注记） |
| 托盘 → quit 显式退出路径 | **未执行** | 需交互桌面 |

以上缺口**不由本文件补齐**；本文件只把 #926 §5 第 1 条（干净 VM 复跑）从「未执行」推进到
「**已在本干净机执行**」，其余仍按未执行登记。

---

## 7 证据路径

- **本文件**（权威读数，入库）：`docs/live_gate/w21/61-issue-1226-clean-windows-vm-installer-rerun.md`
- 构建产物：`desktop/dist-installer/`（`Intelligence-Agent-Setup-0.1.0.exe`、`SHA256SUMS.txt`、
  `installer-build.json`）
- 原始日志（本机 scratch，**未入库**，作旁证）：
  - 依赖/运行时：`.scratch/logs/seed.log`、`npm-desktop.log`、`npm-tui.log`、`pnpm-web.log`
  - 构建：`.scratch/logs/build-{web,tui,desktop,installer}.log`
  - W-16 腿：`.scratch/logs/w16/{install,run,window-probe,update-uninstall}.log`
  - 烟测：`.scratch/logs/smoke-{full,uninstall,compile}.log`
- 驱动脚本（本机 scratch，**未入库**）：`.scratch/evidence/w16/*.ps1`、`.scratch/seed_cache.py`

---

## 8 逐节 V3.1-lite 合规声明

- **§1.1 接票**：任务来自 #1226 派工单；已读 `AGENTS.md`、`docs/SDD_WORKFLOW_PROTOCOL.md`（全文）、
  `docs/live_gate/w21/51-w16-remaining-legs.md`、`43-w16-smoke-artifact.md`、`desktop/installer/README.md`、
  `build-windows-installer.mjs`、`test-windows-installer.mjs`、`prepare_*_runtime.py`、`windows-*-smoke.ps1`。
  **如实缺口**：本机**无 `gh`**，故 `gh issue view 1226/926` **未执行**；#926 的读数基数以仓内
  `43`/`51` 文档为准（派工单已指认其为对照基准）。
- **§1.2 / §9（TDD/Tests）**：本票**不产出产品代码**（纯证据采集），无红→绿环；「验证」= 真实安装件上的
  真机链路 + 仓内 `test-windows-installer.mjs` 三条模式。
- **§2（Code Review）**：无代码提交 ⇒ 无需 review；不触发台账行。
- **§3 / §7 / §8.1（冻结树·全量门禁）**：本票不改 `src/**`/`tests/**`/`web/**`/`scripts/**`（仅新增
  `docs/**` 一件证据），不产生新冻结树，不触发全量门禁与 `check_review_coverage.py`。
- **§4.4（施工授权）**：按派工单授权执行；仅新增 **gitignored** 的 `.scratch/**` 辅助脚本与
  **本证据文档**；**未修改任何产品源码、配置或锁文件**（`desktop/package.json`/`package-lock.json`
  经 `sha256sum -c` 核对未变）。
- **§5 / §16.1（落点）**：证据落在 `docs/live_gate/w21/`，编号 **61**（延续 w21 既有 `<NN>-issue-<num>-*.md`
  体例）；只留**一处**写全读数，其余为指针。**未改** Tracker / `PHASE_STATUS.md`（本票不关单、不集成，
  按派工单「只产出证据和报告」）。
- **§8.9（写盘纪律）**：本中文长文用 `Write` 工具直接落盘，未用 `python -c`/`bash` 内联承载正文。
- **§13 / §14（Git）**：本机**非 git 工作副本**，无任何 Git 写操作；提交 / 开 PR 按派工单在 **Linux 侧**做。
- **票面 AC 对账**：
  1. 「安装器在新真机上完整跑通（安装 → 运行 → 卸载全链路）」——**安装 ✓（exit 0 / 248.3 s）、运行 ✓
     （应用进程 + 后端服务 + 数据根 + 端点；窗口可见性因无桌面不可验，如实登记）、卸载 ✓（exit 0 /
     14.5 s / 目录消失 / 用户数据保留）**。
  2. 「读数与 #926 / 51 对照，差异如实记录」——**见 §5**。
  3. 「证据落盘进仓库」——**本文件**。
- **不宣布任何 Gate 通过 / 不 merge / 不关单** —— 遵守。

---

*生成于 2026-10-16（本机时间）；执行者：CodeBuddy CLI / deepseek-v4.1-flash；授权范围：仅本证据文档与 gitignored scratch。*
