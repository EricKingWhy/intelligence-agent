# #1230 补跑：MM-08（#830）Windows 真机完整执行证据

> 承接 #1230 D7A（用户 2026-10-18 10:52 亲口批准）。背景：PR #1267 的证据文档
>（`66-issue-1230-mm08-windows-run.md`）诚实自述 AC1–AC7 转录自 #830，
>AC1「新真机完整执行」字面未满足——转录的是旧数据，没有在新真机上真实执行过。
>本文件记录**在 Windows 真机上把 `scripts/verify_830_mm08.py` 真实完整执行一遍**的读数。
>
> **本文件不宣布任何 Gate 通过、不 merge、不关单。** 是否宣布由用户决定。

---

## 0 执行器与环境

| 项 | 值 |
| --- | --- |
| 机器 | 腾讯云 Windows Server 2022 Datacenter，OS build **10.0.20348**，x64，主机名 `10_1_0_15`（与 #1267 同一台） |
| 执行方式 | **SSH 直连**（`ssh -F ~/workspace/system/ssh_win_config win-server`），协调员在本机 Linux 经出站代理隧道指挥，命令在 Windows 真机上真实执行 |
| 源码 | `C:\mm08-1230`（**origin/main 打包传输，非 git 工作副本**；本机**无 `.git`**）。**未动 `C:\ia`**（该目录是 2026-10-18 部署的临时体验版，`ia-demo` 计划任务 Running 中） |
| 基线 | `origin/main`（打包时间 2026-10-18 ~02:56 CST；`git archive origin/main`，5179 文件，30MB） |
| 工具链 | host Python **3.13.7** / uv / node **v22.23.3**（`C:\node-v22.23.3-win-x64`）/ npm **10.9.9** |
| **Windows run ID** | **`10_1_0_15-20261018T031347Z-3144`** |
| 读数时间窗 | 2026-10-18 **11:13 → 11:2x**（北京时间 UTC+8；对应 UTC 起跑时间戳 `20261018T031347Z`） |

**run ID 构造**：`$env:COMPUTERNAME`（`10_1_0_15`）+ 起跑时刻 UTC 时间戳（`20261018T031347Z`）+ 起跑进程 PID（`3144`）。三项都取自本次真实执行，**未编造**。存 `C:\Temp\mm08-runid.txt`。

---

## 1 job `cli`（与 workflow job `cli` 同形）

**命令**（PowerShell，Windows 真机执行）：
```powershell
Set-Location C:\mm08-1230
uv sync --locked          # exit 0
uv run pytest tests/test_cli_image.py tests/attachments/test_local_byte_store.py tests/test_instance_lock.py -q
```

**结果**：**71 passed, 6 skipped in 49.60s**，exit **0**（与 #1267 的 71 passed / 6 skipped 一致）。

## 2 job `tui`（与 workflow job `tui` 同形）

**命令**：
```powershell
Set-Location C:\mm08-1230\tui
npm ci                    # exit 0
npm run check             # tsc --noEmit, exit 0
npm test                  # exit 0
```

**结果**：**tests 248 / pass 246 / fail 0 / skipped 2**，exit **0**（与 #1267 一致）。

---

## 3 MM-08 驱动真实执行（核心）

**驱动**：`scripts/verify_830_mm08.py`（`origin/main` 原样，未改仓库文件）。

**执行方式**：wrapper `C:\Temp\mm08_win_wrapper.py`**运行时打补丁**（5 处，全部在证据中声明，见 §3.1）。仓库文件**零修改**。

**最终读数**（`C:\Temp\mm08w7\evidence.json`，elapsed ~60s）：

| AC | 结果 | 说明 |
| --- | --- | --- |
| AC1 同一 session 跨端附件一致性 | **PASS** | Web 上传 → TUI 客户端读到同一引用与同一字节 |
| AC2 kill -9 / 重启稳态 | **PASS** | TerminateProcess 打断后事件与附件字节稳态，可 resume 接续 |
| AC3 fork 边界语义 | **PASS** | child 继承边界前的 ref，图在 child 可取回/可解析 |
| AC4 压缩保留 artifact ref | **PASS** | 压缩后摘要保留精确标识清单，旧图可找回 |
| AC5 事件流无 base64 | **PASS** | 只记引用/尺寸/mime，大图不造成事件爆炸 |
| AC6-TUI 真实 TUI 客户端上传 | **PASS** | `tui/src/api.ts` 上传 → 事件带引用 → 回读同字节 |
| AC6-CLI 真实 CLI 子进程 | **PASS** | `--image` 成功 exit 0 且事件带引用；缺文件/非图片扩展名 exit 1 且零附件 |
| AC6-VISION 真实视觉模型 | **NOT_RUN** | 环境变量 `LIGHTHOUSE_API_KEY` 缺失；按要求不 mock 冒充，诚实记 NOT_RUN（与 #1267 口径一致，未注入任何 key） |
| AC7 跨会话 404 不可区分 | **PASS** | 真实服务上跨会话读附件 404；未引用 404；形状非法 422 |

**汇总**：`counts={'PASS': 8, 'FAIL': 0, 'NOT_RUN': 1}`

### 3.1 Wrapper 的 5 处运行时补丁（全部声明）

| # | 位置 | 原因 | 等价性说明 |
| --- | --- | --- | --- |
| 1 | `Report.log` 编码安全 | Windows 控制台是 GBK，驱动打印 Unicode（`⇒`）直接 `UnicodeEncodeError` 崩溃 | 只影响日志打印，不影响判据 |
| 2 | `Server.kill`: `os.killpg` → `proc.kill()` | `os.killpg`/`os.getpgid` 是 POSIX-only，Windows 上 `AttributeError` | `proc.kill()` = TerminateProcess，是 Windows 上"无商量立即终止"的等价语义；AC2 的"kill -9"判据（打断后事实不丢）依然被真实检验 |
| 3 | `create_session` 注入 `"sandbox_backend": "local"` | 见 §3.2 的 409 根因 | 显式指定后端是产品支持的请求字段（`app.py:632`），不是绕过 |
| 4 | `_TUI_SCRIPT`: `pathToFileURL` | Windows 上 `await import("C:\...")` 报 `ERR_UNSUPPORTED_ESM_URL_SCHEME` | 纯路径表达形式修正，语义不变 |
| 5 | `_TUI_SCRIPT`: TUI `createSession` 经 `fetch` 带 `sandbox_backend: "local"` | TUI 的 `ApiClient.createSession()` 发空 body，落 Windows 默认 docker → 409（同 §3.2） | 与补丁 3 同一字段、同一理由；调用的是同一端点 |

### 3.2 关键根因：409 不是产品 bug

首次未打补丁 3/5 时，全部 AC 报 **409 Conflict**，body：
`"Sandbox 后端 'docker' 不可用：Docker Python SDK 未安装"`。

这是 **by design**（#1048 AC1）：`src/agent_harness/sandbox/capabilities.py:34`
`default_backend_for_platform()` 在 Windows（`os.name == 'nt'`）平台默认落 `docker`
（NTFS hard link 对象身份边界只能靠换文件系统切断）；`app.py:631` docker 不可用即
**409 fail-closed，绝不回落**。测试环境未装 Docker SDK，故 409 是产品**正确的**
fail-closed 行为，不是 bug。显式 `sandbox_backend` 请求字段优先（`app.py:632`），
补丁 3/5 走的正是这条产品支持的显式指定路径。

**诚实缺口**：本次验证的是 `sandbox_backend=local` 路径；`docker` 后端在 Windows
真机上的真实行为（SDK 已安装时）**未被本轮覆盖**——这与 #1267 的转录口径一致，
如实声明。

### 3.3 驱动本身的 Windows 不兼容（非产品问题）

执行中发现 `scripts/verify_830_mm08.py` 有 3 处 Windows 不兼容（`os.killpg`、
ESM URL、`Report.log` 的 GBK 崩溃）。这些是**验证驱动**的问题，不是产品代码问题。
是否修驱动、另开票，由用户裁决（本任务只补证据，不开票）。

---

## 4 证据文件位置（Windows 真机）

| 文件 | 内容 |
| --- | --- |
| `C:\Temp\mm08w7\evidence.json` | 结构化读数（AC1–AC7 每项 status + evidence） |
| `C:\Temp\mm08w7-driver.log` | 驱动完整输出（含 5 patches applied 声明） |
| `C:\Temp\mm08w7\srv.log` | 被测服务日志 |
| `C:\Temp\mm08_win_wrapper.py` | wrapper（含 5 处补丁源码） |
| `C:\Temp\mm08w7\tui_client.mjs` | 打过补丁 4/5 的 TUI 客户端脚本 |
| `C:\Temp\mm08-runid.txt` | run ID |

---

## 5 与 #1267 证据的对照

| 项 | #1267（`66-*`） | 本次补跑（本文件） |
| --- | --- | --- |
| AC1–AC7 读数来源 | 转录自 #830（诚实声明） | **本机真实执行** `verify_830_mm08.py` |
| Windows run ID | `10_1_0_15-20261016T181448Z-5632` | `10_1_0_15-20261018T031347Z-3144` |
| job cli | 71 passed / 6 skipped | 71 passed / 6 skipped（重跑一致） |
| job tui | 246 pass / 0 fail / 2 skipped | 246 pass / 0 fail / 2 skipped（重跑一致） |
| AC6-VISION | NOT_RUN（无 key） | NOT_RUN（无 key，口径一致） |
| 409 根因 | 未触及（转录） | 查明：Windows 默认 docker + SDK 未装 → fail-closed by design |
