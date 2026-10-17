# #1229 Windows Desktop/TUI 发布 Gate：两次独立完整运行（W-16 烟测 + W-20 断言 + 桌面/TUI 一致性 + V3.1-lite 门禁）

> 承接 #365。票面 #365 要求「用新样本跑两次独立完整通过」才谈 Gate passed。#365 已于
> 2026-10-09 按用户口径（自动门禁 + 廉价冒烟）收口关闭，**未声明** 「发布 Gate passed」
> （两次独立真实模型通过未取得，见 `docs/live_gate/w30/00-integration-contract.md` §1）。
> 本文件按 #1229 派工书，在**同一台腾讯云 Windows 真机**上跑**两次独立完整**的
> 安装→启动→真实模型任务→退出→更新→恢复 链路，每轮另取 W-20 pytest 子集、桌面/TUI
> 一致性与 V3.1-lite 门禁读数，并把两轮读数与 #365 判据逐条对照。
>
> **本文件不宣布任何 Gate 通过、不 merge、不关单。** 是否宣布由用户决定。

---

## 0 执行器与环境

| 项 | 值 |
| --- | --- |
| 机器 | 腾讯云 Windows Server 2022 Datacenter，主机名 `10_1_0_15` |
| 执行方式 | Windows 真机上的 CodeBuddy CLI（`codebuddy --model deepseek-v4.1-flash`）经 SSH 驱动 |
| 源码 | `C:\ia`（**origin/main 打包传输，非 git 工作副本**；本机无 `.git`）。派工书指认基线为 main `b7d99b0a`——本机**无法就地核对 commit**（无 `.git`），如实登记为不可验证项 |
| 工具链 | node v22.23.3 / npm 10.9.9 / pnpm 12.11.2（corepack）/ host python 3.13.7 / uv 0.13.1 / git 2.51.0 |
| 构建期工具 | electron-builder **26.15.3**（`--no-save --no-package-lock` 临时安装，见 §1）；electron 解析为 **44.5.1** |
| 内置运行时 | CPython **3.12.14**（`python-runtime.lock.json`，70 wheels）、Node **24.21.0**（`node-runtime.lock.json`） |
| 交互桌面 | `qwinsta`：`services` 会话 0（SSH 所在）、`console` 会话 1 无登录用户、会话 3 Administrator 已 `Disc` ⇒ **无可用交互桌面**（与 #1226 §1 一致） |
| docker | **未安装**（`Get-Command docker` 无结果）⇒ 真实任务必须显式 `sandbox_backend:"local"`（Windows 部署默认后端是 docker，见 `web/app.py`） |

**镜像（GitHub / npmjs 直连慢）**：CPython 归档与 Node 走 `registry.npmmirror.com`/`cdn.npmmirror.com`
binary 镜像，wheel 闭包走 `pypi.tuna.tsinghua.edu.cn`，npm 走 `registry.npmmirror.com`，
electron / electron-builder-binaries 走 `registry.npmmirror.com/-/binary/`，uv 的构建索引走
tuna。所有下載件都按 lockfile 的 SHA-256 校验后才进缓存。

---

## 1 前置状态取证（Round 1 安装前）

| 断言 | 读数 |
| --- | --- |
| 安装目录 `%LOCALAPPDATA%\Programs\Intelligence Agent` | **不存在**（True=缺） |
| 卸载注册表项（HKCU/HKLM Uninstall × `*Intelligence*`） | **0 条** |
| 相关进程（`Intelligence Agent.exe` / `ia-tui` / 产品服务 `python.exe`） | **0 个** |
| 快捷方式（开始菜单 / 桌面 × `*Intelligence*`） | **0 / 0** |
| `%APPDATA%\intelligence-agent\.env` | **存在**，134 B；键名 `MODEL_API_KEY` / `MODEL_BASE_URL` / `MODEL_NAME`（值**未读取打印**） |

> **诚实偏差（重要）**：派工书称「干净状态」。**安装面确为空**（上表），但 `%APPDATA%\intelligence-agent`
> **并非空目录**——它含 **2026-10-16** 一次先前运行留下的 Electron 缓存（`Cache`/`Code Cache`/`GPUCache`/…）、
> `workspace/`、`harness.db`(167936 B)、一条**过期的**端点记录、`.instance.lock`、
> `host-credentials.json`，以及 `smoke-marker.txt`（内容 `do-not-delete`）。
> **关键判据**：`workspace\sessions\` 在 Round 1 前为**空**（0 事件、0 会话）⇒ **没有历史会话/事件**可复用。
> 我**未删除**任何用户数据（`do-not-delete` 标记是被明确要求保留的），只如实登记。Round 1 的两条端点记录
> 是过期的（pid 4536/port 64120、pid 3076/port 63842，端口均无监听），首次启动时被新服务覆盖。

---

## 2 Round 1（安装件 `1723dc26…`）

### 2.A W-16 烟测（安装 → 启动 → 真实任务 → 退出 → 更新 → 恢复）

**A.1 构建安装器**（`desktop/` 流程，本机源码）：`prepare_node_runtime.py` → `prepare_python_runtime.py`
（含 `uv build --wheel` 重建产品 wheel 并 `--force-reinstall` 装入暂存运行时）→ `web build` → `tui build`
→ `desktop build` → `node scripts/build-windows-installer.mjs`。全部 0 退出。

| 产物 | 值 |
| --- | --- |
| 文件 | `desktop/dist-installer/Intelligence-Agent-Setup-0.1.0.exe` |
| 字节数 | **213,363,797** |
| sha256 | **1723dc265141b97b05555ffe2694cdbdff6d700b5a7cc0e4e7d2b721fc3f7cce** |
| 版本 / appId | `0.1.0`（app exe 产品版本 `0.1.0.0`）/ `com.intelligence-agent.desktop` |
| `lockfileSha256` | `644fa21e0dfd2c0c37b30289852225345aa90cc08c51989a2946f18f7efc0197`（与 #1226 一致） |
| `nodeLockfileSha256` | `1a2e7800b68eef1b6fedd777094d596213c8df56698ab7b4532258790ecd5142`（与 #1226 一致） |
| `builtAt` | `2026-10-16T16:57:28.790Z` |

> `desktop/package.json` / `package-lock.json` 未改（electron-builder 用 `--no-save --no-package-lock`）。

**A.2 静默安装**（`…Setup-0.1.0.exe /S`）

| 项 | 读数 |
| --- | --- |
| 安装器 exit / 墙钟 | **0** / **198.9 s** |
| 安装目录 | `%LOCALAPPDATA%\Programs\Intelligence Agent`（per-user，无 UAC），展开 **749.1 MB** |
| 关键文件 | `Intelligence Agent.exe` ✓、`ia-tui.cmd` ✓、`resources\web\index.html` ✓、`resources\python\python.exe` ✓、`resources\node\node.exe` ✓、`resources\tui\dist\src\index.js` ✓ |
| 版本 | `0.1.0.0`（FileVersion `0.1.0`） |
| 卸载注册表项 / 快捷方式 | 1 条（`Intelligence Agent 0.1.0`）/ 开始菜单 1 + 桌面 1 |
| 对用户数据影响 | 安装**未**触碰 `%APPDATA%\intelligence-agent` |

**A.3 启动（首次，未注入模型 env）**

| 项 | 读数 |
| --- | --- |
| 进程 | Electron 进程树（稳定 4 × `Intelligence Agent.exe`） |
| 主窗口可见 | **否**：全部运行在**会话 0**（SSH 所在），无交互桌面 ⇒ 无可见窗口（**已知环境限制**，非产品缺陷） |
| 后端服务 | `resources\python\python.exe -m agent_harness.cli serve --host 127.0.0.1`，**pid 1080 / port 54907**，**ppid = Electron 主进程 pid（6076）**（服务是桌面的子进程） |
| 端点记录 | `workspace\.host-service.json`：`pid/port/protocol_version=1/auth_required=true/owner/service_uuid` 齐备 |
| 首启耗时 | 新端点文件约 **90 s** 出现（冷启动，含一次性初始化；接近 90 s readiness 预算） |
| **POST /api/sessions 读数** | **HTTP 500** —— 服务日志 `ConfigError: provider 'deepseek' 缺少 API key，必须在 .env 中配置 MODEL_API_KEY` |

> **诚实发现（配置缺口）**：**打包安装的服务不读 `%APPDATA%\intelligence-agent\.env`**。`config.py`
> 把 `.env` 锚在 `<install>\resources\python\Lib\.env`（无此文件）⇒ 退化为**环境变量**配置。
> 这与 `docs/SDD_TICKET_TRACKER.md` 既有观察一致（「打包安装的模型配置锚 `<install>\…\Lib\.env`
> ⇒ Run A/Run B 与 W-16 烟测需在启动环境提供 `MODEL_API_KEY`」）。故真实任务前，**重启桌面并把
> `.env` 的 `MODEL_*` 注入启动环境**（只注入，未打印任何值）。这是环境事实，不是产品缺陷。

**A.3b 启动（注入 `MODEL_*` 后）**：服务 **pid 2404 / port 56169**，`/api/health` = `{"status":"ok",
"protocol_version":1,"version":"1.0.0",...}`。

**A.4 真实模型任务**（经运行中的服务 `POST /api/sessions?launch=true`，`auto_approve=true`、`sandbox_backend=local`）

| 项 | 读数 |
| --- | --- |
| HTTP | **200**，SSE 墙钟 **8.1 s**，21 个 `data:` 事件 |
| 会话 | `1b64b4f1-28bb-4a70-8bc5-449709e25bbe` |
| **模型** | **`hy4-preview-f`**（`run/started.model` 与 `model/request.model` 均为它）；✅ **真实模型，非 fake** |
| tool/call | `chatcmpl-tool-bbd08cd71fad450f`，`tool_name=bash`，`args={"command":"echo IA1229-R1"}`（`chatcmpl-` 前缀为真实 OpenAI 兼容网关生成的调用 id） |
| tool/result | `exit_code=0`，`stdout="IA1229-R1\n"`，`duration_ms=58.0`（与 tool/call **成对**，同 `tool_call_id`） |
| 终态 | `run/completed`，`final_text="IA1229-R1"`，`usage_total={prompt 10977, completion 62, total 11039, cached 5312}` |

> 任务文本（逐字）：`请调用 bash 工具执行命令 echo IA1229-R1，然后只回复该命令的 stdout 原文。不要做别的事。`
> **真实模型判据**：`run/started`/`model/request` 的 `model` 字段、真实 `chatcmpl-tool-*` 调用 id、
> 真实 `usage_total` token 计数、`tool/result` 的真实 stdout —— 四者都不是 fake/替身能生成的。

**A.5 退出**：强杀桌面进程树（**先核对 pid + path**，只杀 `…\Programs\Intelligence Agent\*`）后
`Intelligence Agent.exe` = **0**、产品 `python.exe` = **0**。端点记录**保留**（未清）。**注**：这是
**强制结束**，非托盘 → quit 的显式退出路径（同 #1226 口径）。

**A.6 更新（`…Setup-0.1.0.exe /S` 就地重装）**

| 场景 | exit | 墙钟 | 结果 |
| --- | --- | --- | --- |
| **产品仍在运行** | **2** | **10.1 s** | 拒绝；app exe 仍在、无 `.old-{guid}` 残留 —— 与 `installer.nsh` 的进程闸门语义一致 |
| **无进程** | **0** | **195.1 s** | 关键文件齐备、无 `.old-*` 残留、版本不变 |

**A.7 恢复（用户数据逐字节保留）**：更新前后指纹**全部相同**：
`env_file` ✓ / `harness_db_root` ✓ / `sessions/1b64b4f1-…`（2 文件）✓ / `workspaces/1b64b4f1-…`（4 文件）✓ / `host_credentials` ✓。

### 2.B W-20 断言（Windows cli 车道 pytest，#1225 口径）

命令（按 #1225 job `cli`，用仓库 `.venv`）：
`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_cli_image.py tests/attachments/test_local_byte_store.py tests/test_instance_lock.py -q -p no:cacheprovider`

| 读数 | 判定 |
| --- | --- |
| **`71 passed, 6 skipped in 50.91s`** | ✅ **0 failed / 0 error**，共 77 例 |

> 对照 #1225 同日读数 `1 failed, 70 passed, 6 skipped`（唯一红是 `test_instance_lock` 的已知 flake，约 13%
> 复现率）。本轮**未触发**该 flake ⇒ 0 failed。`tests/test_cli_image.py`（D2 判据）本轮 0 failed。

### 2.C 桌面 / TUI 一致性

| 项 | 读数 |
| --- | --- |
| 同一 Task | 会话 `1b64b4f1-…`，`run/completed`，**22 事件** |
| 桌面侧视图（desktop 拉起的服务，A.4 时） | `/api/sessions` 列出该会话，`event_count=22`；SSE 止于 `run/completed` |
| TUI 侧视图（`ia-tui --check` 冷启动/附着服务 pid 2756 / port 55749） | `--check` **rc=0 / 29.9 s**；带凭据 200、不带 401；`/api/sessions` 列出**同一**会话，`event_count=22` |
| Event seq | 服务 API `/api/sessions/{id}/events` = **22 事件，seq 0..21**，末条 `run/completed` @21；磁盘 `events.jsonl` = **22 行，seq 0..21**（一致） |

> **判据**：desktop 拉起的服务与 TUI 冷启动的服务读**同一数据根**，对**同一 Task**给出**相同状态与相同
> Event seq 区间**（0..21，终态 `run/completed`）。**缺口**：会话 0 无交互桌面 ⇒ **无法**在真实控制台里
> 同时开 GUI 窗口与 TUI 做「双客户端同场 + 单独退出」的人机交互实测（#365 B-4 的完整形态）。本节只给
> 「两客户端路径对同一 Task 一致」的服务/CLI/磁盘三位一体证据，**不冒充**交互式一致性。

### 2.D V3.1-lite 门禁（Windows 可跑部分）

| 车道 | 命令 | 读数 | 判定 |
| --- | --- | --- | --- |
| cli pytest | 同 §2.B | `71 passed, 6 skipped` | ✅ |
| tui tsc | `npm run check`（`tsc --noEmit`，`tui/`） | exit **0** | ✅ |
| tui npm test | `npm test`（`tui/`） | `tests 248 / pass 247 / fail 0 / skipped 1 / duration 47.2s` | ✅ `fail 0` |
| desktop npm test | `node --test test/*.test.ts test/*.test.mjs`（`desktop/`） | `tests 310 / pass 310 / fail 0 / skipped 0`，exit **0** | ✅ |

> tui `skipped 1` 为 win32-skip（`/dev/zero` 字符设备用例）。desktop 车道是本票**附加**项（#1225 只有 cli / tui 两 job）。

---

## 3 两轮之间的环境重置（步骤记录）

| 步 | 动作 | 读数 |
| --- | --- | --- |
| 0 | 先停残留进程（核对 pid + path） | IA=0 svc=0 |
| 1 | 卸载产品：`Uninstall Intelligence Agent.exe /S` | exit **0** / **12.7 s** |
| 2 | 确认安装目录消失 | `Test-Path` = **False** |
| 2 | 注册表清理 | 卸载项 **0** 条 |
| 2 | 快捷方式清理 | 开始菜单 **0** / 桌面 **0** |
| 3 | **保留** `%APPDATA%\intelligence-agent\.env` | **存在**，134 B（两轮共用；两轮指纹同值 `369c7c24…`） |
| 4 | **用户数据处置（我的选择，如实记录）** | **保留**（不清理）。理由：Round 2 会产生**全新**会话、另取全部读数，独立性由「新会话 id + 新安装件 + 新读数」保证；保留还能顺带验证 Round 2 的安装/更新**不破坏** Round 1 的既有用户数据。`smoke-marker.txt`(do-not-delete) 等前存文件一并保留 |
| 5 | 确认无残留进程 | IA=**0** / svc=**0** |

> Round 2 起点的用户数据**含 Round 1 的会话 `1b64b4f1-…`**（Round 2 会话列表里可见，见 §4.C）——
> 这是**保留**选择的直接后果，如实登记。

---

## 4 Round 2（安装件 `516af94b…`，独立重跑）

> 独立性声明：Round 2 **重新构建**安装件（新 sha256）、**重新安装**、跑**新会话**、重取**全部**读数；
> **没有任何一项复用 Round 1 的结果**。

### 4.A W-16 烟测

**A.1 重新构建**：同 §2.A 流程（源码未改）。

| 产物 | 值 |
| --- | --- |
| 字节数 | **213,363,798** |
| sha256 | **516af94b1e4192cc68bb37bc2edf4b440a2398de3102ae2fe284d1a3948c2b82** |
| `lockfileSha256` / `nodeLockfileSha256` | `644fa21e…` / `1a2e7800…`（与 Round 1 相同） |
| `productCodeSha256` | `01488260dfaf793c2037a3fc270744b41d9a2c7cc7879b2873d45655832d464a`（与 Round 1 相同）→ 同源输入、不同打包字节 |
| `builtAt` | `2026-10-16T17:28:15.096Z` |

**A.2 静默安装**：exit **0** / **185.4 s**；6 个关键文件齐备；卸载项 1；快捷方式 开始菜单 1 + 桌面 1；版本 `0.1.0.0`。

**A.3 启动（注入 `MODEL_*`）**：Electron 进程树 4 个；服务 **pid 5440 / port 56169**，
`/api/health` = `{"status":"ok","protocol_version":1,"version":"1.0.0",...}`；窗口**不可见**（会话 0，同 §2.A.3）。
**注**：Round 1 已实测「不注入模型 env → 500 ConfigError」，Round 2 直接注入，未重复该负例。

**A.4 真实模型任务**（同样经服务 `POST /api/sessions?launch=true`）

| 项 | 读数 |
| --- | --- |
| HTTP / SSE | **200** / **11.3 s**，27 个 `data:` 事件 |
| 会话 | **`39d321c7-aa5f-4473-8a1e-ece98c6adaa2`**（与 Round 1 不同的**新**会话） |
| **模型** | **`hy4-preview-f`**；✅ 真实模型 |
| tool/call | `chatcmpl-tool-9d90cefdbea2da43`，`bash`，`args={"command":"echo IA1229-R2"}` |
| tool/result | `exit_code=0`，`stdout="IA1229-R2\n"`，`duration_ms=51.9`（成对） |
| 终态 | `run/completed`，`final_text="IA1229-R2"`，`usage_total={prompt 10973, completion 113, total 11086}` |

**A.5 退出**：核对 pid + path 后强杀，IA=**0**、svc=**0**。

**A.6 更新**：**产品运行中** → exit **2** / **8.1 s**（拒绝）；**无进程** → exit **0** / **188.1 s**，
无 `.old-*` 残留、关键文件齐备。

**A.7 恢复**：更新前后指纹**全部相同**（`env_file` / `sessions/39d321c7-…` / `workspaces/39d321c7-…` / `host_credentials`）。

### 4.B W-20 断言

`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_cli_image.py tests/attachments/test_local_byte_store.py tests/test_instance_lock.py -q -p no:cacheprovider`

| 读数 | 判定 |
| --- | --- |
| **`71 passed, 6 skipped in 34.44s`** | ✅ **0 failed / 0 error**，共 77 例 |

### 4.C 桌面 / TUI 一致性

| 项 | 读数 |
| --- | --- |
| TUI `--check` | **rc=0 / 32.3 s**；服务 pid 3564 / port 56716；带凭据 200、不带 401 |
| 会话列表（TUI 侧服务） | **两条**：`39d321c7-…`（**28 事件**，本轮的 Task）+ `1b64b4f1-…`（22 事件，Round 1 的 Task，因保留用户数据而并存） |
| R2 Event seq | API `/api/sessions/39d321c7…/events` = **28 事件，seq 0..27**，末条 `run/completed` @27；磁盘 `events.jsonl` = **28 行，seq 0..27**（一致） |

> 同 §2.C：给出「两客户端路径对**本轮** Task 状态/Event seq 一致」的证据；**交互式双客户端同场**仍是缺口。

### 4.D V3.1-lite 门禁

| 车道 | 读数 | 判定 |
| --- | --- | --- |
| cli pytest | `71 passed, 6 skipped`（34.44 s） | ✅ |
| tui tsc（`npm run check`） | exit **0** | ✅ |
| tui npm test | `tests 248 / pass 247 / fail 0 / skipped 1` | ✅ |
| desktop npm test | `tests 310 / pass 310 / fail 0 / skipped 0` | ✅ |

---

## 5 两轮读数对照

| 项 | Round 1 | Round 2 | 判定 |
| --- | --- | --- | --- |
| 安装件 sha256 | `1723dc26…` | `516af94b…` | 各自独立构建 |
| 安装 exit / 墙钟 | 0 / 198.9 s | 0 / 185.4 s | 同向通过 |
| 服务 pid/port（注入 env 后） | 2404 / 55016 | 5440 / 56169 | 每次不同（正常） |
| **真实模型** | `hy4-preview-f` | `hy4-preview-f` | ✅ 两轮都是真实模型 |
| **会话 id** | `1b64b4f1-…` | `39d321c7-…` | **不同 ⇒ 独立** |
| tool calls | `bash echo IA1229-R1` → `IA1229-R1` | `bash echo IA1229-R2` → `IA1229-R2` | 逐字正确 |
| 终态 | `run/completed` | `run/completed` | ✅ |
| 退出后残留进程 | 0 | 0 | ✅ |
| 更新（运行中 / 无进程） | exit 2 (10.1 s) / exit 0 (195.1 s) | exit 2 (8.1 s) / exit 0 (188.1 s) | 两轮同向 |
| 恢复（用户数据指纹） | 全同 | 全同 | ✅ |
| cli pytest | 71p/6s/0f | 71p/6s/0f | ✅ |
| tui tsc / npm test | 0 / 248-247-0-1 | 0 / 248-247-0-1 | ✅ |
| desktop npm test | 310-310-0-0 | 310-310-0-0 | ✅ |
| 一致性（同一 Task seq） | 22 事件 seq 0..21 | 28 事件 seq 0..27 | ✅（服务/CLI/磁盘三方一致） |

---

## 6 与 #365 判据的对照

| #365 判据（按仓内可得口径归并） | 本文件证据 | 结论 |
| --- | --- | --- |
| 「两次独立完整通过」且**互相独立、不复用** | §2 / §4 各自独立构建+安装+新会话+新读数；会话 id 不同 | **达成**（就本票 A/B/C/D 范围而言） |
| 「**真实模型**，mock / 替身 / Fake Provider 不得计入 PASS（上限锁 FAIL）」 | 两轮均 `hy4-preview-f`，真实 `chatcmpl-tool-*` id、真实 token usage、真实 stdout（§2.A.4 / §4.A.4） | **达成**（非 fake） |
| W-16「安装→启动→退出→更新→恢复」链路 | 两轮：安装 0、服务起、真实任务、退出清、更新 0/2、恢复指纹全同 | **达成** |
| 「关键工程测试仍有阻断 ⇒ Gate 不可宣布发布完成」 | 本票可跑车道：cli pytest 0 failed、tui 0 failed、desktop 0 failed、tsc 0 —— **本票可跑面 0 阻断** | **本票可跑面无阻断**（见 §7 未覆盖面） |
| 「干净 Windows x64」前置 | 安装面为空；用户数据根**非空**（前次遗留，`sessions/` 为空）——如实登记（§1） | **部分达成**（安装面干净；用户数据非空，已披露） |
| 桌面/TUI 同一 Task 状态/Event seq 一致（#365 B-4） | 服务 API / TUI 服务 / 磁盘三方对同一 Task 的 seq 一致（§2.C / §4.C）；**交互式双客户端同场未做** | **部分达成**（一致性有证据；交互式缺口） |

> **本文件不宣布 Gate passed。** 上表只陈述「本票可跑范围内两轮读数与 #365 判据的对照」。是否宣布由用户决定。

---

## 7 诚实缺口清单（仍未执行 / 本机不可执行）

1. **交互式桌面 / GUI 行为**：会话 0 无交互桌面 ⇒ 主窗口**不可见**、无法做 GUI 点击、无法做
   「托盘 → quit 的显式退出路径」。本文件的「退出」是**强制结束进程**（同 #1226 口径）。
2. **桌面/TUI 交互式双客户端同场**（#365 B-4 完整形态）：无交互控制台 ⇒ 无法在真实 TUI 里键入任务、
   无法「同时在场 + 单独退出各实测一次」。只给了服务/CLI/磁盘三方一致证据。
3. **模型配置注入**：打包服务不读 `%APPDATA%\.env`，真实任务**必须在启动环境注入 `MODEL_*`**
   （Round 1 已实测不注入即 500）。本文件按此注入，**未**改写安装目录里的 `Lib\.env`。
4. **sandbox 后端**：本机无 docker，任务显式用 `sandbox_backend:"local"`；Windows 默认后端（docker）
   未实测。
5. **运行中更新安全暂停**（列在途 Task → 安全暂停 → Ledger 结清）：未执行（无在途任务；本票只测到
   进程闸门**拒绝**分支）。
6. **kills / reconcile 真机流程**（#365 Run B 的 kill→重开→查 DB/Ledger→裁决→续跑）：**未执行**，
   本票范围不含（那是 #365 Run B 的完整协议）。
7. **长任务 / 多轮 / 压缩**（#365 Run A 的完整形态）：未执行；本票的「真实短任务」是**单轮单工具调用**。
8. **源码 commit 无法核对**：`C:\ia` 无 `.git`，派工书指认的 `b7d99b0a` **本机不可验证**。
9. **卸载器 UI 双语点击 / 磁盘满回退 / 服务暂停失败 / 迁移中断**：未执行（同 #1226 §6 口径）。
10. **msi/签名**：安装件未签名（票面既有裁定）；本机 `signtool` 步骤是 electron-builder 的默认占位。

以上缺口**不由本文件补齐**；如实登记，未用 fake / 简化冒充。

---

## 8 逐节 V3.1-lite 合规声明

- **§1.1 接票**：任务来自 #1229 派工书；已读 `AGENTS.md`、`docs/SDD_WORKFLOW_PROTOCOL.md`（全文）、
  `docs/live_gate/w21/61-issue-1226-*`（W-16 模板）、`60-issue-1225-*`（Windows 车道）、`43-w16-smoke-artifact.md`、
  `10-w20-deterministic-evidence.md`、`30-gates.md`、`49-two-independent-passes.md`、`20-blocked-items.md`、
  `docs/live_gate/w30/00-integration-contract.md`、`desktop/installer/README.md`、构建/烟测脚本。
  **如实缺口**：本机**无 `gh`** ⇒ `gh issue view 365/1229` **未执行**；#365 判据以仓内文档为准（派工书已指认）。
- **§1.2 / §9（TDD/Tests）**：本票**不产出产品代码**（证据采集），无红→绿环；「验证」= 真实安装件上的
  真机链路 + 真实模型任务 + 仓内 pytest/tsc/npm test 读数。
- **§2（Code Review）**：无代码提交 ⇒ 无需 review；不触发台账行。
- **§3 / §7 / §8.1（冻结树·全量门禁）**：本票不改 `src/**`/`tests/**`/`web/**`/`tui/**`/`desktop/src/**`/`scripts/**`；
  仅新增 `docs/**` 一件证据与 gitignored 的 `.scratch/**` 辅助。`desktop/installer/staging/**` 与
  `web/dist`、`tui/dist`、`desktop/dist-installer` 均为 gitignored 构建产物（`pytest` 的 `addopts` 已 ignore 前两者）。
- **§4.4（施工授权）**：按派工书授权执行；未修改任何产品源码 / 配置 / 锁文件；`desktop/package.json` /
  `package-lock.json` 经 `--no-save --no-package-lock` 保持不变。
- **§8.9（写盘纪律）**：本中文长文与 `.scratch` 辅助脚本（`fetch_runtimes.py`）均用 `Write` 工具直接落盘，
  未用 `python -c`/`bash` 内联承载正文。
- **§13 / §14（Git）**：本机**非 git 工作副本**，无任何 Git 写操作；提交 / 开 PR 在 Linux 侧做。
- **凭据纪律**：全程**未打印** `.env` 内容或任何 key / host token 值；命令、日志、本文件中都不含凭据明文。
- **杀进程纪律**：每次 `Stop-Process` 前核对 **pid + path**（只杀 `…\Programs\Intelligence Agent\*`）。
- **不宣布 Gate passed / 不 merge / 不关单** —— 遵守。

---

## 9 证据路径

- **本文件**（权威读数，入库）：`docs/live_gate/w21/65-issue-1229-desktop-tui-gate.md`
- 构建产物：`desktop/dist-installer/`（`Intelligence-Agent-Setup-0.1.0.exe`、`SHA256SUMS.txt`、`installer-build.json`）
- 原始读数（本机 scratch，**未入库**，作旁证）：
  - `.scratch/evidence/w16/round1/`：`create-body.json`、`sse-round1.txt`、`events-api.json`、`fp-before-update.json`、`fp-after-update.json`
  - `.scratch/evidence/w16/round2/`：`create-body.json`、`sse-round2.txt`、`events-api.json`、`fp-before-update.json`
  - `.scratch/logs/fetch-runtimes.log`（运行时下载日志）
  - `.scratch/fetch_runtimes.py`（镜像下载辅助，gitignored）
- 会话事件（真机数据根，未入库）：`%APPDATA%\intelligence-agent\workspace\sessions\{1b64b4f1…,39d321c7…}\events.jsonl`

---

*生成于 2026-10-16/17（本机时间）；执行者：CodeBuddy CLI / deepseek-v4.1-flash；
授权范围：仅本证据文档与 gitignored scratch 与真机安装/运行/卸载动作。*
