# W-21 Windows 实测结果（阶段四）

> 基线 `95f6c1cc`（= 取证时 `origin/main` tip）· 取证机 Windows 10.0.26200 x64 · 结论：**Gate NOT passed（fail）**
> 根因票：#809（6 个缺陷 + 抄作业修复方向）· 复跑脚本：`bash D:/w21-work/drive-windows-gate.sh`
> 纪律：#365 明文「不在本票修产品代码」「不得打印 `.env` 或其他凭证值」。本文档不含任何凭据值；未执行项一律标「未执行」，不用 fake/mock 冒充。

## 1. 结论：`20-blocked-items.md` 的前提被推翻

原判 B-1..B-5 = `blocked`，理由是「本机是 Linux 云电脑，无 Windows 执行环境」——即**缺环境**。

本轮 Windows 实测证明：**环境不缺，断的是产品**。

| 反面证据（环境具备） | 读数 |
| --- | --- |
| 真实模型通道 | CLI 侧一次真实 run 完成（真实 provider + write/read 工具，工作区 `D:/w21-work/smoke1`） |
| Python 服务在 Windows | `agent-harness serve --host 127.0.0.1` 健康并打印 `HOST_SERVICE_READY http://127.0.0.1:50311`（本轮脚本实跑）；`/api/health` 可达 |
| Electron | `electron.exe --version` → v44.5.1；能启动、能弹启动失败对话框、能执行到窗口创建前一行（D6） |
| 打包链 | electron-builder 26.15.3 完成 pack + 签名，仅在 makensis 阶段失败（D1） |
| NSIS | 3.0.4.1 可用；同一个 makensis 把 D1 精确报成「宏内 include 找不到文件」 |
| Node / Python | node v22.21.1；CPython 3.12.14 运行时按 `python-runtime.lock.json` 解包成功 |

→ B-1..B-5 由 `blocked` 改判 `fail`：产品链在 6 处断开，逐条可复现（§3）。

## 2. 判据状态表

| 判据 | 状态 | 依据 |
| --- | --- | --- |
| B-1 安装→启动→退出→更新→恢复（W-16 烟测） | **fail** | 安装包产不出来（D1，makensis 确定性失败）；`desktop/scripts/test-windows-installer.mjs` 无产物可测 |
| B-2 Run A（安装后桌面 + 真实模型长任务） | **fail（不可达）** | 桌面链断在 D2 → D4 → D6 → D3；窗口从未出现 |
| B-3 Run B（TUI 冷启动 + kill/reconcile） | **fail（不可达）** | 安装包内无 TUI（D5）；TUI 无端点发现/无冷启动/无凭据通道 |
| B-4 桌面/TUI 同时在场 + 单独退出 + Task/Event seq 一致 | **fail（不可达）** | 无双客户端可同时在场（D5） |
| B-5 两次独立完整通过 | **未达成** | 依赖 B-2/B-3；本轮不存在「一次通过」 |
| W-20 判定器 / 浏览器验证 / kill 语义（Linux 侧） | 沿用 `10-w20-deterministic-evidence.md`，**不可折抵**本表 | 交接手册与 #365 均已明文 |

## 3. 逐缺陷（现象 · 最小复现 · 观测 · 定位 · 影响）

### D1 安装包：NSIS 构建确定性失败

复现：`cd desktop && node scripts/build-windows-installer.mjs`

观测（`evidence/d1-installer-build.log`，sha256 `da8a0e096e377da32818c5ac2168037550bb2db9fff3289ed27ce0178ea34657`）：

```
!include: could not find: "D:\intelligence-agent-wt-w21\desktop\node_modules\app-builder-lib\templates\nsis\installer-directories.nsh"
Error in macro customHeader on macroline 3
Error in script "<stdin>" on line 101 -- aborting creation process
EXIT=1
```

定位：`desktop/installer/installer.nsh:31` 的 `!include "${__FILEDIR__}\installer-directories.nsh"` 位于 `!macro customHeader`（:28）内部。宏体是原文，预处理指令在 `!insertmacro` 时求值，`__FILEDIR__` 随插入上下文解析成 `<app-builder-lib>/templates/nsis`，而不是定义文件所在目录。

影响：没有安装包 → B-1 及之后所有 Windows 判据不可达。

### D2 打包运行时不含产品包

复现（即 W-16 `desktop/installer/README.md` 步骤 1–4 产出的 staging）：

```
desktop/installer/staging/python/python.exe -c "import agent_harness"
→ ModuleNotFoundError: No module named 'agent_harness'
…python.exe -m agent_harness.cli serve --host 127.0.0.1
→ Error while finding module specification for 'agent_harness.cli'
```

定位（结构性原因，不是取证方式的问题）：

- `desktop/installer/README.md` 的构建步骤只离线安装 69 个第三方 wheel；
- `desktop/installer/python-runtime.lock.json` 的 `wheels[]` 中没有产品包；
- `desktop/scripts/build-windows-installer.mjs:126-130` 只把 `dist/**/*`、`package.json` 与 `staging/python/` 放进产物；
- 全仓库没有任何步骤把 `src/agent_harness`、产品 wheel 或等效 `.pth` 放进 staging。

影响：安装后的应用无法启动服务（桌面表现为启动失败对话框）。

### D4 端点状态文件两侧路径不一致（桌面永远认不出服务）

复现（`evidence/windows-gate-drive.txt` STEP D4）：服务在 `D:/w21-work/d4-run/` 启动并打印 `HOST_SERVICE_READY http://127.0.0.1:50311`，随后——

```
服务写出：/d/w21-work/d4-run/.agent/workspace/.host-service.json   （存在，225 字节）
桌面读取：/d/w21-work/d4-run/.host-service.json                    （不存在）
```

定位：

- 服务侧：`src/agent_harness/config.py:148`（`workspace_dir` 默认 `.agent/workspace`）+ `src/agent_harness/host_service.py:87`（`ENDPOINT_FILENAME`）；
- 桌面侧：`desktop/src/host-client.ts:74` 读 `join(root, ENDPOINT_FILENAME)`，`root = process.cwd()`（`desktop/src/main.ts:64`）；
- 就绪循环 `desktop/src/service-host.ts:72-91` **只读这一个路径且无端口回退**，超时（20s）后抛 `Timed out waiting for the local service endpoint file`。

运行观测：把 staging 的模拟 `.pth` 就位（让子进程能 import，即越过 D2）后启动桌面，30s 内仍弹「Intelligence Agent 服务无法启动」且无任何窗口 → 就绪失败确实发生在**路径**，不是导入。

影响：桌面既认不出自己的子进程，也认不出外部已运行的服务。

### D6 ESM 主进程 `__dirname` 崩溃（窗口永不出现）

复现（前置：`<root>/.host-service.json` 存在且其后服务健康，使就绪通过；观测见 `evidence/d6-deterministic.log`，sha256 `95fd01a8e052c4bb782eb52e68d01788b57c9d86ae8b7885f4fbee10a0e67322`）：

```
(node:41952) UnhandledPromiseRejectionWarning: ReferenceError: __dirname is not defined
    at main (file:///D:/intelligence-agent-wt-w21/desktop/dist/src/main.js:90:26)
```

同期窗口枚举：无应用窗口（只有 GPU/IME 辅助窗口）。

定位：`desktop/package.json:5` `"type": "module"` + `desktop/tsconfig.json:5` `"module": "NodeNext"` ⇒ 产物是 ESM，`__dirname` 不存在；`desktop/src/main.ts:100`（编译为 `dist/src/main.js:90`）与 `desktop/src/main.ts:125` 各有一处。

历史记录：同一崩溃在 A3b 运行中也观测到（`evidence/a3b-desktop-launch.log`，sha256 `755f6ce3a83eeb00240c32403751b1063da3d9750f8d827e910e393a5b2d5889`）。

说明：本轮脚本化复跑（cwd=`desktop/`、未放端点文件）先停在 D4 的对话框、看不到该崩溃——与因果顺序一致（D6 在 D4 之后）。

### D3 渲染层：`ia-app://` 未注册 + 资产未打包

复现（STEP D3）：

```
desktop/src/ipc.ts:25    export const SCHEME = 'ia-app'
desktop/src/main.ts:121  await win.loadURL('ia-app://app/index.html')
grep -rE 'registerSchemesAsPrivileged|protocol\.handle|protocol\.register' desktop/src/  → 0 命中
git ls-files web/dist → 空 ； git ls-files desktop/installer/staging → 空
打包声明：files: ['dist/**/*','package.json']；extraResources 仅 staging/python/
实测产物：dist-installer/win-unpacked/resources/app/dist = { src, test }，全树无 index.html、无 tui
```

影响：即便 D2/D4/D6 修好，窗口也加载不到任何页面——自定义 scheme 没有处理器，磁盘上也没有被服务的资产。

### D5 安装包内无 TUI；TUI 无发现/无冷启动/无凭据

复现（STEP D5）：

```
grep -rE 'host-service|Authorization|Bearer|spawn|child_process' tui/src/  → 0 命中
node tui/dist/src/index.js                      → usage: ia-tui --session <id> [--server <url>]   [exit=2]
node tui/dist/src/index.js --session sess_probe → ia-tui 需要 TTY 运行（管道/重定向环境不支持交互界面）  [exit=2]
```

定位：`tui/src/index.ts:33-38`（`--session` 必填、非 TTY 直接 `exit 2`）；`tui/src/` 无任何端点发现、无子进程 spawn、无 `Authorization` 头。

对发布基线重要的补充：服务端在**未配置 `JWT_SECRET`** 时是本地信任（fail-open），见 `src/agent_harness/web/app.py:2014-2019`；一旦按发布基线配置密钥，客户端必须携带凭据，而 TUI 现在没有任何取用凭据的通道。

影响：B-3/B-4 没有可执行对象。

## 4. 未执行（不得折抵）

- Run A / Run B 本体（真实模型长任务、≥1 次压缩 + ≥1 次摘要失败、跨上下文窗口续接、DB 提交后 kill + reconcile + 人工确认 + 手动续跑）——**未执行**。
- W-20 的 DB/HTTP/UI/恢复/上下文/diff 断言在 Windows 上重跑——**未执行**（Linux 侧读数见 `10-w20-deterministic-evidence.md`）。
- W-16 烟测（安装→启动→退出→更新→恢复）——**未执行**（无产物）。
- 桌面/TUI 同时在场与单独退出、同一 Task 状态与 Event seq 一致——**未执行**。
- 补充观测（未深挖）：启动失败对话框提供「打开脱敏日志」，但 `%APPDATA%\intelligence-agent` 下没有任何日志文件（只有 GPU/Shader 缓存与 `Local State`）。

## 5. 方案依据（协议 §6.1：两个独立来源 + 机制/契合/复用判定）

来源（本地浅克隆；`file:line` 与 commit 均已实测）：

1. `D:\reference\PI-Desktop` @ `1e07bad33a298b7738e7abfaeefe528da6b4a378`（Electron 桌面 + Rust/Node sidecar；`docs/adr/0001-use-electron.md`）
2. `D:\reference\deepseek-harness` @ `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（Electron 桌面 + Web + CLI；本仓 `installer-directories.nsh` 的上游，provenance 同 commit）

| 缺陷 | 机制（来源 `file:line`） | 契合点 | 复用判定 |
| --- | --- | --- | --- |
| D1 | DSH `apps/desktop/scripts/installer.nsh:3-4,11-25,198-200`：顶层 `!define … "${__FILEDIR__}\…"`，include 放顶层，宏内只 include 已定义的绝对路径 | 与本仓同为 electron-builder 自定义 include + 目录换协议 | **PORT**（include 搬位；保留本仓回滚能力） |
| D2 | PI-Desktop `packages/agent-runtime/scripts/bundle.mjs:18-38`、`apps/desktop/package.json:122-143`、`apps/desktop/electron/main/agent-sidecar.ts:19-38`、契约测试 `apps/desktop/test/agent-runtime-bundle-package.test.mjs` | 同一失效模式：运行时在、产品代码不在 | **ADAPT**（Python 侧用 `.pth`/wheel/打包器等价实现 + 加契约测试） |
| D3 | PI-Desktop `apps/desktop/electron/main/bootstrap/window.ts:2045-2054`（生产 `loadFile`、开发 `ELECTRON_RENDERER_URL`）、`apps/desktop/electron/main/module-path.ts:12-16` | 同为 Electron 外壳 + 独立前端构建 | **PORT**（或保留 `ia-app://` 但注册协议并映射到打包目录） |
| D4 | PI-Desktop `packages/host-runtime/src/host-process.ts:101-108`（stdio 子进程 + 绝对 data dir）、`data-paths.ts:52-68`；DSH `apps/desktop-host/src/index.ts:30,103-104` + `apps/desktop/src/host-process.ts:206-213`（`--port 0` + IPC `ready`） | 本仓服务**已经**打印 `HOST_SERVICE_READY`，缺的是父进程把绝对路径告诉子进程（或直接消费 stdout） | **ADAPT** |
| D5 | PI-Desktop `apps/pi-host/src/cli.ts:36-40`（stdout ready + 一次性 pairing token）、`apps/pi-host/src/credentials.ts:10,21-26`（0600 + 原子写）；DSH `packages/client/connection/src/rpc-host.ts:104-107`（fail-closed 403/401） | 本仓缺「TUI 出件 + 客户端附着」 | **ADAPT** |
| D6 | PI-Desktop `electron.vite.config.ts:53-59`（`define: {"__dirname": "import.meta.dirname"}`）、`module-path.ts:1-16`、preload 单独打 CJS（`:84-99`） | 同一 ESM/`__dirname` 陷阱 | **PORT** |
| B-4 语义 | DSH `packages/session/session-persistence-jsonl/src/lease.ts:1-16`（跨进程单写者租约）；PI-Desktop `packages/agent-host/src/event-log.ts:176-184`、`packages/racp/src/server.ts:255-267`、`packages/agent-host/src/turn-queue.ts:44-46`（事件扇出 + 回合准入 + 断开只释放自己的订阅 + 恢复队列保持 held） | #365 的「同时在场/单独退出/seq 一致」需要明确语义 | **PORT DESIGN** |

不采用的部分：PI-Desktop 明确**未实现**版本回滚（`docs/adr/0022-application-update-delivery.md:64-65`）且**零手写 NSIS**；本仓已有更强的目录换 + 回滚能力（ADAPT 自 DSH）。修 D1 时应保留该能力，不为了「对齐上游」而删。

## 6. 本票自身改动与门禁

本次仅新增本文档（`docs/live_gate/w21/40-windows-gate-result.md`），属 DOC_PATTERN 范围：未改产品代码、未改规格、未改 tracker 之外的控制文件。全量门禁与双轴审查在本票的「失败交付」形态下如何安排，见 #809 与 #365 回填。

## 7. 证据清单（仓库外留存，sha256 供复核）

全套哈希清单：`D:/w21-work/evidence/SHA256SUMS.txt`。

| 文件 | 说明 | sha256（前 8 位） |
| --- | --- | --- |
| `evidence/d1-installer-build.log` | D1：makensis 完整输出 | `da8a0e09` |
| `evidence/windows-gate-drive.txt` | D2/D3/D4/D5 复跑全文（STEP 0–D6） | `d39d9264` |
| `evidence/d6-deterministic.log` | D6 确定性复现 | `95fd01a8` |
| `evidence/a3b-desktop-launch.log` | D6 首次观测 | `755f6ce3` |
| `evidence/a1-*.png`、`a2-dialog.png`、`a3a-dialog-service-healthy.png`、`a3b-nowindow.png`、`baseline.png` | 桌面启动链截图（a1/a2/a3a 的 `.log` 为空：当时未捕获 stderr；其证据为截图 + 窗口枚举读数 + 代码定位） | 见 `SHA256SUMS.txt` |
| `drive-windows-gate.sh` | 复跑脚本（只读仓库；唯一写入是取证用模拟 `.pth`，备份在 `D:/w21-work/w21-sim-harness.pth.bak`） | — |

PNG 等二进制不入仓库：`docs/**` 的 DOC_PATTERN 不含 `.png`，入库会把审查与覆盖率义务抬到二进制上。

## 8. 剩余与待批准

- **待批准**：本文件的 push / PR（§14.4 要求每次单独批准）。
- 修复：按 #809 的 6 条与抄作业方向拆票（建议顺序 D2/D4 → D6 → D3 → D1 → D5）。
- 重跑：修复后用**新样本**重跑本 Gate 的两次独立完整通过；W-20 断言在 Windows 上重跑后才谈 B-5。
- #365 保持 open（不关单）：本次为失败交付 + 根因票，AC 未满足。
