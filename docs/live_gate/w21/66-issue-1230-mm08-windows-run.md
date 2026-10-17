# #1230：MM-08（#830）Windows run ID 实测证据落盘

> 承接 #1230 派工书。背景：#935 要求把 MM-08（#830）的验证证据落盘，但 #830 关单时 0 评论、
> 证据包只落在特性分支（见 `docs/tickets/multimodal-2026-10-07/MM-08-verification-evidence.md` §14），
> 其「Windows run」很可能**从未真实存在**（#830 的 Windows 面是 GitHub Actions 近似验证，
> 见该包 §9）。本文件在**一台 Windows 真机**上，按 `mm08-d2d3-windows-verify.yml` 的两条判据
> **实际跑一遍**并留下**真实 run ID**、逐命令完整输出与退出码。
>
> **本文件不宣布任何 Gate 通过、不 merge、不关单。** 是否宣布由用户决定。

---

## 0 执行器与环境

| 项 | 值 |
| --- | --- |
| 机器 | 腾讯云 Windows Server 2022 Datacenter，OS build **10.0.20348**，x64，主机名 `10_1_0_15` |
| 执行方式 | **Windows 真机上的 CodeBuddy CLI**（`codebuddy --model deepseek-v4.1-flash`），非 SSH 转发 |
| 源码 | `C:\ia`（**origin/main 打包传输，非 git 工作副本**；本机**无 `.git`**）。派工书指认基线 `b7d99b0a`——本机**无法就地核对 commit**（无 `.git`），如实登记为不可验证项 |
| 工具链 | host Python **3.13.7**（`C:\Program Files\Python313`）/ uv **0.13.1** / node **v22.23.3** / npm **10.9.9**（`C:\node-v22.23.3-win-x64`） |
| 车道定义 | `.github/workflows/mm08-d2d3-windows-verify.yml`（`workflow_dispatch` + 特性分支 `push`；本票为**等价手工执行**） |
| **Windows run ID** | **`10_1_0_15-20261016T181448Z-5632`** |
| 读数时间窗 | 2026-10-17 **02:14:48 → 02:21**（本机时区 UTC+8；对应 UTC 起跑时间戳 `20261016T181448Z`） |

**run ID 构造（派工书证据要求 2）**：本机**不是 GitHub Actions runner，平台没有 run ID**，故按派工书
约定由**三个真实运行值**拼成：`$env:COMPUTERNAME`（`10_1_0_15`）+ 起跑时刻 UTC 时间戳
（`20261016T181448Z`）+ 起跑进程 PID（`5632`，cli job 第一条命令所在 PowerShell 进程的 `$PID`）。
三项都取自本次真实执行，**未编造**。全部读数命令均在同一台机、同一份 `C:\ia` 树上、同一时间窗内产生。

**运行头一条命令（cli job 日志首行，逐字）**：`RUN_ID=10_1_0_15-20261016T181448Z-5632`（`C:\Temp\mm08-cli.log:1`）。

### 0.1 启动检查表（`AGENTS.md` §3）

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | `SPEC_ROOT/00_PROJECT_VISION.md` §2.3 Recoverable（Resume/Replay/Fork）、§2.5 Reuse First、§3 冻结原则（append-only SessionEvent / Artifact 优先于 Context Pollution） | READY |
| 当前任务规格 | `docs/tickets/multimodal-2026-10-07/MM-08-cross-surface-recovery-gate.md`（AC1–AC8 票面）+ `MM-08-verification-evidence.md`；`SPEC_ROOT/03_SESSION_EVENT_MODEL.md` §7 Fork / §8 Compaction；`SPEC_ROOT/06_CONTEXT_ARTIFACT_MEMORY.md` §3 Artifact Store / §5 Compaction / §8 Failure Semantics | READY |
| Reuse 相关判定 | `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md` §3 DeepSeek Harness（Oversized output spill → ArtifactStore/MinIO）、§2 Pi（Session Tree/Fork / JSONL）；本票**不新增实现**，故只做判定核对，无新 Reuse 决策 | READY（无新决策） |
| Phase 依据 | `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md` Phase 5（Artifact + Compaction）、Phase 14（Resume/Replay/Fork 完整化）、§2 执行规则、§3 长任务边界指针 | READY |
| 本任务触发细则 | `.github/workflows/mm08-d2d3-windows-verify.yml`（两条判据与命令源）；`docs/live_gate/w21/{60,61,65}-*`（体例）；`docs/SDD_WORKFLOW_PROTOCOL.md`（**全文 929 行读至 EOF**，V3.1-lite）；`tests/test_cli_image.py`、`tests/attachments/test_local_byte_store.py`、`tests/test_instance_lock.py`、`src/agent_harness/storage/local_artifact.py`、`tui/package.json`、`scripts/verify_830_mm08.py` | READY |

> **触发细则补充判定**：本票**不**做设计/选型（不触发协议 §1.3「方案依据」块）、**不**写产品代码（不触发 `docs/agents/implementation-discipline.md`）、**不**操作 GitHub Issue（本机无 `gh`，不触发 `docs/agents/issue-tracker.md`）、**不**做架构决策（不触发 `GLOSSARY.md` / `docs/adr/`）——故上述指针不在本票必读面内。

---

## 1 job `cli`：D2（Windows 字节发布不再是文本模式）

### 1.1 命令（逐字，PowerShell；与 workflow job `cli` 同形）

```powershell
$env:Path = 'C:\Program Files\Python313\Scripts;C:\Program Files\Python313;' + $env:Path
Set-Location C:\ia
& 'C:\Program Files\Python313\Scripts\uv.exe' sync --locked --python 'C:\Program Files\Python313\python.exe'
& 'C:\Program Files\Python313\Scripts\uv.exe' run pytest tests/test_cli_image.py tests/attachments/test_local_byte_store.py tests/test_instance_lock.py -q -p no:cacheprovider 2>&1 | Tee-Object -FilePath C:\Temp\mm08-cli.log
```

### 1.2 完整输出与退出码

```
RUN_ID=10_1_0_15-20261016T181448Z-5632
uv sync start: 2026-10-17T02:14:48.9338702+08:00
Resolved 140 packages in 102ms
Checked 85 packages in 452ms
uv sync exit code = 0
===== cli job pytest start: 2026-10-17T02:14:56.1788844+08:00 =====
.............s.................s..s.......s.s..s........................ [ 93%]
.....                                                                    [100%]
71 passed, 6 skipped in 31.82s
pytest exit code = 0
===== cli job end: 2026-10-17T02:15:33.7019349+08:00 =====
```

| 命令 | 退出码 | 读数 |
| --- | --- | --- |
| `uv sync --locked --python <3.13.7>` | **0** | `Resolved 140 packages` / `Checked 85 packages` |
| `uv run pytest ... -q -p no:cacheprovider` | **0** | **`71 passed, 6 skipped in 31.82s`**（0 failed / 0 error） |

### 1.3 逐文件分解（同机、同树，另跑的 `-v/-q` 取证）

| 文件 | 读数 | 退出码 | 证据 |
| --- | --- | --- | --- |
| `tests/test_cli_image.py` | **16 passed, 1 skipped**（collect 17） | 0 | `C:\Temp\mm08-cli.log:13-43` |
| `tests/attachments/test_local_byte_store.py` | **31 passed, 5 skipped**（collect 36） | 0 | `C:\Temp\mm08-cli-byte-store.log` |
| `tests/test_instance_lock.py` | **24 passed** | 0 | `C:\Temp\mm08-cli-instance-lock.log` |
| 合计 | **71 passed, 6 skipped** | 0 | 与车道整跑一致（16+31+24=71；1+5+0=6） |

### 1.4 D2 判据逐条核对

| D2 判据（来自 workflow） | 本次真机读数 | 判定 |
| --- | --- | --- |
| `tests/test_cli_image.py` 由 **2 failed** 转 **0 failed** | 该文件 **0 failed**；且**修复前被判红的正是** `test_image_is_stored_and_referenced_in_the_user_message[shot.png]` / `[shot.bin]` 两例（#830 §5 D2 点名），本次两例**均 PASSED** | ✅ |
| 新增回归测试通过 | `tests/attachments/test_local_byte_store.py::test_published_object_bytes_are_not_newline_expanded` **PASSED**（最直接地断言「含 `0x0A` 的对象**落盘字节逐字节等于原文**」） | ✅ |
| （同源纪律）模拟用例按设计在 win32 跳过 | `test_publish_blob_forces_binary_mode_under_emulated_text_mode` **SKIPPED**（`skipif(os.name == "nt")`，理由「真 Windows 上由 `test_published_object_bytes_are_not_newline_expanded` 覆盖」） | ✅（非红） |

**修复在位的直接读数（源码，非推测）**：`src/agent_harness/storage/local_artifact.py:468-472` 的
`_publish_blob` 以
`os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)`
发布对象——`O_BINARY` 这一位就是 D2 的一行修法（POSIX 上 `getattr` 取 0，逐位或等价于无操作）。

> **诚实边界（D2 的「由 2 failed」一句）**：本票**未**在本机复现「修复前 2 failed」这一侧
> （需回退修复，超出本票范围）。该侧依据是 workflow 的注释与 #830 §5 D2 的 GA 证据
> （run [#37941646937](https://github.com/EricKingWhy/intelligence-agent/actions/runs/37941646937) job `cli`
> 步骤 5：`2 failed / 14 passed / 1 skipped`）。本机实测侧是「**0 failed**」——即 D2 判据的**目标态**。

---

## 2 job `tui`：D3（tui 全量在 Windows 上可移植）

### 2.1 命令（逐字，PowerShell；与 workflow job `tui` 同形）

```powershell
$env:Path = 'C:\node-v22.23.3-win-x64;' + $env:Path
Set-Location C:\ia\tui
npm ci --no-audit --no-fund
npm run check 2>&1 | Tee-Object -FilePath C:\Temp\mm08-tsc.log
npm test 2>&1 | Tee-Object -FilePath C:\Temp\mm08-tui.log
```

### 2.2 完整输出与退出码

| 命令 | 退出码 | 读数 |
| --- | --- | --- |
| `npm ci --no-audit --no-fund` | **0** | `added 7 packages in 25s` |
| `npm run check`（`tsc --noEmit`） | **0** | 无输出（类型面干净） |
| `npm test` | **0** | 见下（spec reporter 摘要） |

`npm test` 的 spec reporter 摘要行（逐字，`C:\Temp\mm08-tui.log` 末段）：

```
ℹ tests 248
ℹ suites 0
ℹ pass 246
ℹ fail 0
ℹ cancelled 0
ℹ skipped 2
ℹ todo 0
ℹ duration_ms 43500.3379
npm test exit code = 0
```

### 2.3 D3 判据核对

| D3 判据（派工书/ workflow） | 本次真机读数 | 判定 |
| --- | --- | --- |
| 摘要 **`ℹ tests 201`** | `ℹ tests 248`（≥ 201；workflow 注释的 201 系旧数，主线已增长到 248） | ✅ |
| 摘要 **`ℹ fail 0`** | `ℹ fail 0`（`pass 246`） | ✅ |
| `npm test` 退出码 | **0** | ✅ |
| `npm run check` 退出码 | **0** | ✅ |
| `npm ci` 退出码 | **0** | ✅ |

**2 例 skip 的性质（非红）**：`﹣ readImageFile：非常规文件按 missing 明确拒绝…（POSIX FIFO 用例）`
与 `﹣ readImageFile：字符设备（/dev/zero）… # SKIP`——两条都是 win32 上按平台跳过的 POSIX 用例
（`tui/test/image-paste.test.ts`），与 workflow 注释「另有 1 例 win32-skip」同类；**`fail` 才是判据**。

### 2.4 日志编码（如实登记）

首次整跑用控制台默认编码（本机 GBK/936）经 `Tee-Object` 落盘，导致日志里非 ASCII 字形乱码
（**数字与退出码不受影响**：`tests 248` / `fail 0` / `exit code = 0` 均完好）。随后在
`[Console]::OutputEncoding = UTF8` 下**同命令重捕**一份干净 UTF-8 日志覆盖 `mm08-tui.log`
（重捕读数一致：`248 / 246 / 0 / 2`，`duration_ms 43500.3379`，exit 0）；原始乱码版保留为
`C:\Temp\mm08-tui.raw-console.log` 作旁证。

---

## 3 AC1–AC7 读数对照（证据要求 3，对照 #830 的 AC 定义）

### 3.1 为什么 AC1–AC7 读数取自 #830 证据而非本 Windows 车道

MM-08 的 AC1–AC5、AC7（跨端一致 / kill-resume / fork / compaction / 无 base64 / 越权 404）需要
**真服务 + 三端驱动**——即 `scripts/verify_830_mm08.py`。该驱动**在 Windows 上不可跑**：

- `scripts/verify_830_mm08.py:329`（`Server.kill`）用 `os.killpg(os.getpgid(...), signal.SIGKILL)`；
- 本机实测（`uv run python -c` 直读）：
  `platform = Windows-2022Server-10.0.20348-SP0`、`hasattr(os, "killpg") = False`、`hasattr(os, "getpgid") = False`。

即 `os.killpg` / `os.getpgid` 是 **POSIX-only**，Windows 上不存在 ⇒ 该驱动的 AC2（SIGKILL 重启）
构建即不可用，整套驱动不是 Windows 可跑面。**这与派工书给定的「执行命令」一致**：本票的车道
（`mm08-d2d3-windows-verify.yml`）只跑 `pytest`（D2）+ `npm test`（D3），**不含**该驱动。
故 AC1–AC7 读数**如实转录 #830 的权威机器读数**（不冒充为本 Windows 车道新测）。

**来源文件**：`docs/evidence/830-mm08-cross-client-local.json`（本机跑的全量 `evidence.json`；
`counts={'PASS':8,'FAIL':1,'NOT_RUN':0}`、`elapsed_s=61.33`）与同轮
`docs/evidence/830-mm08-cross-client-local-verify.txt`。**这是 #830 执行时的读数（Linux 本机），
不是本票重测**；本票**不**改变、也不重新解读它们。

### 3.2 逐条读数与「本 Windows 车道是否复跑」

| AC | 票面定义（`MM-08-cross-surface-recovery-gate.md`） | #830 读数（来源见 3.1） | 本 Windows 车道是否复跑该 AC |
| --- | --- | --- | --- |
| **AC1** | A 端附图 → B 端（换客户端）看到同一张图与同一事件历史 | **PASS** | **否**（需真服务三端驱动；驱动 POSIX-only） |
| **AC2** | kill / resume 后附件字节与事件引用都不丢 | **PASS** | **否**（同因） |
| **AC3** | fork 会话能看到 fork 边界之前的图 | **FAIL（真缺陷 D1）** | **否**（同因）。**旁证**：本 Windows 车道内两条同源单测 `test_same_bytes_across_sessions_share_one_object` / `test_cross_session_bytes_readable_but_not_owned` **均 PASSED**——但这是**单测面**，**不等同** AC3 的路由级复验（AC3 走 `POST /api/sessions/{id}/forks` + child 读回 + 装配面），如实区分 |
| **AC4** | compaction 后摘要保留 artifact refs、旧图可找回、事实不被删 | **PASS** | **否**（同因） |
| **AC5** | 事件流可定位附件引用且**无 base64** | **PASS** | **否**（同因） |
| **AC6-TUI** | 真机走查 · TUI | **PASS** | **部分**：`tui` 全量在本机 **248/246/0/2**（绿）已被本车道复跑；但那是 tui 单测面，**不是** AC6-TUI 的端到端走查 |
| **AC6-CLI** | 真机走查 · CLI 一次 `--image` | **PASS** | **是（最贴近的一支）**：AC6-CLI 的仓内用例族 `tests/test_cli_image.py` 在本机 **0 failed**（即 D2 判据的正物体；`[shot.png]`/`[shot.bin]` 两例 PASSED） |
| **AC6-VISION** | 真机走查 · 真实视觉模型读图 | **PASS** | **否**（需真视觉模型凭证；本票不注入任何 key） |
| **AC7** | 跨 session 附件 id 读取 404 的授权断言在真实服务复验 | **PASS** | **否**（需真服务驱动） |

> **读法**：AC1–AC7 的读数**全部**是 #830 的既有权威读数；本 Windows 车道**只复跑** D2（对应
> AC6-CLI 的仓内用例族）与 D3（tui 全量套件）。AC3 的 `FAIL` 是 #830 登记的真缺陷 D1（#830 只登记不修），
> 本票**不**改变该结论。

---

## 4 D2 / D3 判据的通过/失败判定（证据要求 4）

| 判据 | 判定 | 依据 |
| --- | --- | --- |
| **D2**（产品，P1）：`tests/test_cli_image.py` 由 2 failed 转 **0 failed**；新增回归通过 | **PASS** | §1：文件 0 failed（16 passed / 1 skipped）、两例 `[shot.png]/[shot.bin]` PASSED、`test_published_object_bytes_are_not_newline_expanded` PASSED、`local_artifact.py:470` 修法在位 |
| **D3**（测试可移植性，P2）：`npm test` 摘要 `tests ≥ 201` 且 `fail 0` | **PASS** | §2：`ℹ tests 248` / `ℹ fail 0` / exit 0（另 `npm ci`、`npm run check` 均 exit 0） |

**两条判据均达成。** —— 但**本文件不宣布任何 Gate 通过**；是否宣布由用户决定。

---

## 5 诚实缺口（仍未执行 / 本机不可执行）

1. **run ID 是构造的**：本机非 GitHub Actions runner，平台无 run ID，故由 COMPUTERNAME + UTC 时间戳 + PID
   构造（§0）。它是**本次真实执行的标识**，但**不是**平台签发的 run ID。
2. **源码 commit 无法核对**：`C:\ia` 无 `.git`，派工书指认的 `b7d99b0a` **本机不可验证**（§0）。
3. **AC1–AC2、AC4–AC5、AC6-VISION、AC7 未在本 Windows 车道复跑**：理由是 `verify_830_mm08.py`
   用了 POSIX-only 的 `os.killpg/os.getpgid`（§3.1，含运行时直读证据），本票车道亦不含该驱动。
   其读数转录自 #830 证据，**标明来源、不冒充新测**。
4. **MM-08 驱动自身的 Windows 不可移植性**：本票**只登记不修**（`verify_830_mm08.py:329`）。
   这是本票实地发现的**新事实**（#830 §10 的驱动读数来自 Linux 本机 / Ubuntu 沙箱）。
5. **「修复前 2 failed」未在本机复现**（D2）：需回退产品修复，超本票范围；该侧依据是 workflow 注释 + #830 GA 证据（§1.4）。
6. **tui 首次日志乱码**：控制台默认 GBK 编码所致；已用 UTF-8 重捕并保留原样（§2.4）。**不影响任何计数或退出码**。
7. **未做**（同 #1225/#1229 口径，非本票范围）：桌面 GUI 走查、真实模型任务、安装/更新/卸载链路。

以上缺口**不由本文件补齐**；如实登记，未用 fake / 简化冒充。

---

## 6 证据路径

- **本文件**（权威读数，入库）：`docs/live_gate/w21/66-issue-1230-mm08-windows-run.md`
- 逐命令完整输出（本机 `C:\Temp`，**未入库**，作旁证）：
  - `C:\Temp\mm08-cli.log`（uv sync + cli job 整跑 + `test_cli_image.py -v`）
  - `C:\Temp\mm08-cli-byte-store.log`（`test_local_byte_store.py -v -rs`，含 D2 回归用例）
  - `C:\Temp\mm08-cli-instance-lock.log`（`test_instance_lock.py -q`）
  - `C:\Temp\mm08-tsc.log`（`npm run check`）
  - `C:\Temp\mm08-tui.log`（`npm test`，UTF-8 干净版）
  - `C:\Temp\mm08-tui.raw-console.log`（同命令首次捕获，控制台 GBK 版，保留原样）
- AC1–AC7 读数来源（入库）：`docs/evidence/830-mm08-cross-client-local.json` +
  `docs/evidence/830-mm08-cross-client-local-verify.txt`

---

## 7 逐节 V3.1-lite 合规声明

- **§1.1 接票**：任务来自 #1230 派工书；已读 `AGENTS.md`、`docs/SDD_WORKFLOW_PROTOCOL.md`（**全文读至 EOF**，V3.1-lite）、
  `SPEC_ROOT/{00_PROJECT_VISION,03_SESSION_EVENT_MODEL,06_CONTEXT_ARTIFACT_MEMORY,13_OPEN_SOURCE_REUSE_MATRIX,14_IMPLEMENTATION_ROADMAP}.md`、
  `docs/live_gate/w21/60-issue-1225-windows-lane-rerun.md`、`61-issue-1226-*`、`65-issue-1229-*`（体例参照）、
  `.github/workflows/mm08-d2d3-windows-verify.yml`、
  `docs/tickets/multimodal-2026-10-07/MM-08-cross-surface-recovery-gate.md` 与 `MM-08-verification-evidence.md`、
  `tests/test_cli_image.py`、`tests/attachments/test_local_byte_store.py`、`tests/test_instance_lock.py`、
  `src/agent_harness/storage/local_artifact.py`、`tui/package.json`、`scripts/verify_830_mm08.py`；启动检查表见 §0.1。
  **如实缺口**：本机**无 `gh`** ⇒ `gh issue view 830/1230` **未执行**；#830 的 AC 与读数以仓内文档 / JSON 为准（派工书已指认）。
  **读取顺序说明（如实）**：§0.1 的必读面是在证据采集**之后**为补齐合规而实读的——车道命令本身只依赖
  `mm08-d2d3-windows-verify.yml` + 三个测试文件 + `local_artifact.py`（这些在施工前已读），尚缺的合规阅读当次已补齐；
  未按「先读后施工」顺序执行的部分如实登记，不遮掩。
- **§1.2 / §9（TDD/Tests）**：本票**不产出产品代码**（纯证据采集），无红→绿环；「验证」= 真机上的车道整跑读数。
- **§2（Code Review）**：无代码提交 ⇒ 无需 review；不触发台账行。
- **§3 / §7 / §8.1（冻结树·全量门禁）**：本票不改 `src/**`/`tests/**`/`tui/**`/`scripts/**`；仅新增
  `docs/**` 一件证据与 `C:\Temp`（机外暂存）日志。不产生新冻结树，不触发全量门禁与 `check_review_coverage.py`。
- **§4.4（施工授权）**：按派工书授权执行**只读+车道命令**；**未修改任何产品源码、测试、配置或锁文件**
  （`uv sync --locked` 只按锁文件校验/装依赖；`npm ci` 只按 package-lock 重装 `node_modules`；未改 `package.json`/`package-lock.json`）。
- **§7 第 6 条（`uv run` / `uv.lock`）**：本票按派工书用了 `uv run`。`uv sync --locked` 的语义是「锁与
  `pyproject.toml` 不同步即失败」——本次 **exit 0** ⇒ 锁本就同步、`uv.lock` 未被改写。**注**：本机非 git
  工作副本，无法用 `git status` 复核锁文件；该结论以 `--locked` 的语义为准，如实登记为「不可用 Git 复核」项。
- **§5 / §16.1（落点）**：证据落在 `docs/live_gate/w21/`，编号 **66**（延续 w21 既有 `<NN>-issue-<num>-*` 体例；
  派工书提到的 62 号在库中**不存在**，实际邻近为 60/61/65）；只留**一处**写全读数。**未改** Tracker / `PHASE_STATUS.md`。
- **§8.9（写盘纪律）**：本中文长文用 `Write` 工具直接落盘，未用 `python -c`/`bash` 内联承载正文。
- **§13 / §14（Git）**：本机**非 git 工作副本**，无任何 Git 写操作。
- **凭据纪律**：全程**未打印**任何 key / token；命令、日志、本文件中都不含凭据明文（AC6-VISION 亦未注入 key）。
- **不宣布任何 Gate 通过 / 不 merge / 不关单** —— 遵守。

---

*生成于 2026-10-17（本机时间）；Windows run ID：`10_1_0_15-20261016T181448Z-5632`；
执行者：CodeBuddy CLI / deepseek-v4.1-flash；授权范围：仅车道命令 + 本证据文档。*
