# 两次独立完整通过（安装件 `01477e59…`）——分段记录

- 场景：#365 票面要求「用新样本跑两次独立完整通过」才谈 B-5 与关单。本文件按跑次分段记录，
  每段一次写全，不靠后续推断。
- 安装件：`Intelligence-Agent-Setup-0.1.0.exe`，204,379,033 B，
  sha256 `01477e59779693299216f9595966106e71a7f7d92409856aa18729899b8591e6`（HEAD `28a382cc`）。
- 前置（桌面开窗 + TUI 冷启动附着 + #847 真机）见 `48-pre-run-artifact-verification.md`。

---

## Run A · 第 1 次（2026-10-08）

- **样本**：`D:\w21-work\run-a-sample-20261008T071551`（`tools/challenge-fixture/generate.py` 新生成；
  7 条 seed + `app.py` + `demo.db` + `schema.sql` + `features.json` + `progress.md` + `FAKE_ADVICE.md`）。
  样例应用由操作者托管在 `http://127.0.0.1:8911`（`host-fixture.py`，保存 `app.py` 后自动重启）。
- **会话**：`c9d49b73-f97b-4323-9cd1-5e5a8ae04325`（桌面客户端创建，`permission_mode=workspace-write`，
  逐次 bash 审批由操作者 `approve.py --loop` 应答，共 **21 次** `approve_session`）。
- **驱动**：`D:\w21-work\run-a.py`（`RUN_A_EVIDENCE=D:\w21-work\evidence\run-a-pass1`）。

### 分段读数

| 段 | 内容 | 读数 |
| --- | --- | --- |
| phase1 | 真实模型完成完整修复任务（读现状 → 定位 B1/B2/B3 → 最小修复 → 真实 HTTP 复现 5 个验收场景 → 改 `features.json`/`progress.md`） | `run/completed` @ **1068.6 s**，基线后 **1418** 事件，21 次审批，**未触发** pause/硬护栏 |
| turn2 | 复核操作者真实浏览器读数 + 按 diff 审阅自己的改动 | `run/completed` @ **150.8 s**，**588** 事件 |
| turn3 | 按操作者给定文本把计划清单置为「1 completed + 1 in_progress」 | `run/completed` @ **15 s**，**31** 事件 |
| compact-fail | 摘要模型不可达（`?model=glm-5.3-flash`） | `context/compaction_failed` ×2（attempt 1/2），`error_class=transport_error`、`OpenAIAuthenticationError`；`tokens_before=tokens_after=56061`、`compacted_turn_count=0`、`bracket_id=null`（原投影保留、零写入） |
| compact-ok | 真实模型摘要 | `context/compaction_failed` ×2（attempt 1/2），`error_class=plan_section_mismatch`、message=`Plan section is inconsistent with the progress plan; in-progress item(s) missing: r2`；`tokens_before=tokens_after=56061`、`compacted_turn_count=0` |
| phase2 | 压缩后新窗口继续（读 `progress.md`/`features.json`/`review/*` → 复核 seed 禁令 → 浏览器读数与 DB 对照 → diff 审阅 → 更新两份文件） | `run/completed` @ **181.2 s**，**649** 事件 |

### 判定器与终态

- `tools/challenge-fixture/judge.py`（操作者侧，`--db <sample>/demo.db --base-url http://127.0.0.1:8911`）：
  **`overall = pass`，`missing = []`**，逐项 `ok=true`：R-042 失败且写入 0、R-043 恰好一次（40 行/40 唯一）、
  同 ID 同内容重试返回既有结果（审计行不增）、同 ID 不同内容 → HTTP 409、页面状态与 DB 一致。
- `final`：`records` 共 **47** 行 = 7 条 seed + 40 条 `imp_R-043_*`（**`imp_R-042_*` = 0**）；
  事件 **2691** 条、末条 `run/completed`；上下文 **90629 / 200000**（45%，未触发自动压缩阈值 0.7）；
  改动 diff **88 行**（相对原始模板）。
- **真实浏览器读数**（`browser-check.mjs`：Playwright `channel: chrome`，headless，Chrome 154.0.8037.98）：
  `http://127.0.0.1:8911` 输入 `R-042` 点查询 → 页面 `状态: failed / 总行数: 20 已写入: 0 / 错误: row 17: empty id`，
  `data-status=failed`、`data-written=0` —— 与数据库事实一致。控制台 1 条 404（页面引用的非关键资源）。

### 与上一轮（旧安装件）的差异

- 上一轮 phase1 撞上上下文硬护栏并 pause，靠 `relaunch` 改小 `MAX_CONTEXT_TOKENS` + `resume` 续跑；
  本轮窗口保持产品默认 200000，峰值 90629（45%）**没有触发**护栏，故 `relaunch`/`resume` 两段未使用。
- 压缩腿两次都失败，但**失败方向与上一轮不同**：上一轮是 `Plan section must be (none): no plan item is in progress`
  （计划清单里没有 in_progress），本轮是 `in-progress item(s) missing: r2`（有 in_progress 但摘要第 5 节没列出它）。
  同一道闸门在两个方向上都拒真实模型摘要 ⇒ 该状态下压缩不可用（**#844 的新读数**，本票不修产品代码，仅回票）。

### 诚实注记

- 本次「完整通过」的判据是：票面场景全部执行、判定器 `pass`、真实浏览器读数与 DB 一致、终态事件为 `run/completed`。
  压缩腿**失败**是产品缺陷（#844）的复现，不是本段的通过项；它不阻断 phase2（上下文远未吃紧）。
- 证据目录：`D:\w21-work\evidence\run-a-pass1\`（`run-a-*.txt`、`run-a-*.sse`、`run-a-judge.json`、
  `run-a-events*.json`、`run-a-final.txt`、`run-a-fix.diff`、`approve.txt`、`host.log` 等）。

---

## Run B · 第 1 次（2026-10-08）

票面协议：从安装后的 TUI 冷启动同一 Python 服务协议，真实模型完成同样完整任务；在数据库提交后、
ToolResult 前 kill Host，再从桌面打开，先查询 DB/Ledger、reconcile、人工确认需要项、手动续跑；
最终浏览器验证和 diff 审阅仍须完成。桌面/TUI 同时在场和单独退出规则各实测一次。

- **样本**：`D:\w21-work\run-b-sample-20261008T080318`（新生成），样例应用由操作者托管在 `http://127.0.0.1:8912`。
- **会话**：`430e304a-dff0-4dd8-96e2-3ed78e6504ee`（由**真实控制台里的 TUI** 创建）。
- **驱动**：`D:\w21-work\run-b.py`（`RUN_B_EVIDENCE=D:\w21-work\evidence\run-b-pass1`）；
  控制台驱动 `tui-console.ps1`（真实 Windows 控制台 + 内核级按键注入 + 整屏读取）；审批 `approve-b.py --loop`。

### 分段读数

| 段 | 内容 | 读数 |
| --- | --- | --- |
| tui-cold | 无服务时由安装后的 TUI 冷启动服务 | `ia-tui --check` **rc=0 / 8.7 s**（热缓存），服务 `pid=26508 port=64360 uuid=ad345e93…`；带凭据 200、不带 401 |
| tui-open | 真控制台里开一个 TUI 新会话 | 会话 `430e304a…`；TUI node `pid=25544` |
| seed | 把样本灌进 TUI 建的 workspace（含 `TASK.md`、原始 `app.py`） | 11 个条目就位 |
| host | 操作者托管样例应用 | `127.0.0.1:8912` LISTENING |
| kill-watch | 本地 SQLite 触发：业务库已提交 + 账本仍有在飞调用 ⇒ 杀 Host | `import_rows 0 -> 40`，在飞 `call_81ef7c09f3384d918e89e8b0`（`bash: timeout 120 python verify_scenarios.py`，账本 `state=RUNNING`、`started_at=2026-10-08T00:09:37.162Z`）；**kill 延迟 0.281 s**；服务 `pid=26508` 已死 |
| task | 把指针文本键入 TUI | 事件流 656 条止于 **`run/interrupted`**，无该 `tool_call_id` 的 `tool/result` |
| reopen | 从桌面重开（新服务）+ 读中断快照 | 新服务 `pid=27216 port=53526`；`GET /api/recovery/interrupted` → 200，`recovery=needs_manual_reconcile`，文案明写「未提供 ReconcileCallback，拒绝恢复——避免伪造结果或盲目重跑高风险副作用（不变量 #14）」；`interrupted_runs=[{run_id 0cfbbdb8…, interrupted_seq 651, step_id 11}]`、`resume_available=true` |
| tui-attach | 第二个真控制台 TUI 附着到新服务（同时在场） | 附着成功，node `[25544, 2844]` |
| investigate | 操作者查业务库 / 账本 / 悬空调用 | 业务库 `import_rows=40`；审计 R-042 `failed 20/0 'row 17: empty id'`、R-043 `completed 40/40`；账本该会话 17 行；**悬空调用恰为 `call_81ef7c09…`**（seq 651） |
| recover | 人工裁决后 reconcile | 操作者按账本事实（`state=RUNNING`、`finished_at=NULL`、`result_json=NULL`）与业务库读数裁决 **`CONFIRM_SUCCESS`**；`POST /recover`（1 条 decisions）→ **200**；事件 660 条、**悬空 `[]`** |
| browser | 操作者真实 Chrome 读页面 | `http://127.0.0.1:8912` 查 R-042 → 页面 `状态: failed / 总行数: 20 已写入: 0 / 错误: row 17: empty id`，`data-status=failed`、`data-written=0` —— 与 DB 一致 |
| resume | 把续跑指令键入 TUI（`tui2`） | `run/completed` @ **816 s**，新增 **2145** 事件 |
| coexist | 三方同场事实表 | 桌面 4 进程 `[10160,11768,24228,26312]`、TUI node `[25544,2844]`、**一个服务** `pid=27216`；`GET /api/sessions` 200 |
| tui-exit | TUI 单独退出 | node `[25544,2844] -> [25544]`；服务 `27216 -> 27216`（存活）；桌面 pid 不变 ⇒ 通过 |
| desktop-exit | 桌面单独退出 | 桌面 `[...] -> []`；服务 `27216 -> None`（**随桌面消失**） |

### 终态

- `tools/challenge-fixture/judge.py`：**`overall = pass`，`missing = []`**（逐项同 Run A：R-042 失败且 0 写入、
  R-043 恰好 40、重试幂等、不同内容 409、页面与 DB 一致）。
- `final`：事件 **2809** 条 / **2** 个 run；`tool/call` **50** 与 `tool/result` **50** **成对**；
  关键链齐全 —— `run/interrupted` ×1、`operation/reconcile-required` ×1、`operation/reconciled` ×1、`run/completed` ×1；
  上下文 **81661 / 200000**；改动 diff **84 行**；证据目录 67 个文件。
- 业务库终态：seed **7** 条（`payload_hash` 与各自 content 的 sha256 **逐一相符**）、`imp_R-042_*` **0**、
  `imp_R-043_*` **40**（DISTINCT 40）、审计 **2** 行。
- 账本终态：该会话 `SUCCEEDED 48 / FAILED 1 / CANCELLED 1`，**0 非终态**；
  被裁决的 `call_81ef7c09…` 转 `SUCCEEDED`，`finished_at` 已写，`reconcile_meta.verdict = CONFIRM_SUCCESS`。

### 桌面 / TUI 单独退出的两条规则

- 本轮测到的是**桌面拉起服务**那一条：`reopen` 由桌面派生新服务 ⇒ 桌面退出时服务随之消失（`survived=False`）。
- **互补的那一条**（服务由 TUI 冷启动 ⇒ 桌面退出后服务存活）已在 `47-w21-run-b-tui-legs-rerun.md` 实测。
  两条合起来覆盖票面「同时在场和单独退出规则各实测一次」。

### 诚实注记

1. **`POST /recover` 的裸请求（409 + `pending_decisions`）分支本轮未复测**：驱动脚本只在
   `decisions.json` 缺失时才发裸请求，而本轮操作者按协议先查了 DB/Ledger 再落裁决，故直接走了带 decisions 的分支。
   等价的读侧证据是本轮的 `GET /api/recovery/interrupted` = `needs_manual_reconcile` + 不变量 #14 文案；
   上一轮的 409 读数在 `45-run-b-kill-reconcile.md`。**第 2 次会先发裸请求再落裁决**，把这条补上。
2. **`final` 段先补了一次冷启动**：`desktop-exit` 腿刚把服务带走，而 `final` 要读 `/context-usage`，
   故用 `ia-tui.cmd --check` 重新冷启动一个服务（rc=0 / 7.7 s）再收尾。这是操作者侧的取数动作，不改变上面的读数。
3. **`window=0,0,0,0`**：控制台事实表里窗口矩形读成 0（真实控制台窗口的句柄查询在该环境下未取到尺寸），
   但 node 进程与整屏文本读取都正常（`tui2-screen-*.txt`），故不影响「同时在场 / 单独退出」的判定。
4. 本段同样是**一次**独立完整通过；票面要求两次，第 2 次见下。

---

## Run A · 第 2 次（2026-10-08）

**模型替换的由来**：第 1 次用的 `mimo` profile（`mimo-v2.6-flash`）在 gate 进行中账号余额耗尽，
第 2 次起改用操作者侧 `glm` profile（`senseaudio` / `glm-5.3-flash`，`https://api.senseaudio.cn/v1`）。
两次都是**真实模型**；换 profile 只换服务商，不改任何验收口径。下面每次运行都单独记 profile。

### attempt 1（样本 `…T083632`，会话 `3cf0462a-86fd-4259-9cef-2bbaf370145e`）

| 段 | 读数 |
| --- | --- |
| phase1 | `run/completed` @ **357.8 s**，基线后 **518** 事件；**38** 次模型请求、**22** 次审批 |
| judge | **`overall = fail`**：`r042_zero_writes` `status=completed written=19 db_count=19`、`r043_exactly_once` `count=41 unique=41`、`ui_truthful` `页面=completed（期望 failed）`；`old_records_intact` / `retry_same_returns_existing` / `retry_conflict` 通过 |
| 事件 | 519 条；`tool/call` 40 与 `tool/result` 40 成对；多行 bash 静默 no-op **6** 次；`edit` 失败 **1**、`apply_patch` 失败 **1** |

**根因（已开 #849）**：判定器读的是**同一个 `demo.db`**，而模型在探索阶段是用**未修复的旧代码**跑过
R-042 / R-043 的，留下的行一直没清。修复后代码正确，但判定器看到的是被自己污染过的终态。
操作者侧的对照实验（`run-a-judge-order-diagnostic.json`，新样本 `…T090954`）：

- 复位前：R-042 `completed 20/19`、R-043 `completed 41/41`、`records_total=107`；
- 删除 60 条业务行 + 2 条审计行后重新 POST：R-042 → `failed 20/0`、R-043 → `completed 40/40`、`records_total=87`；
- **同一份修复代码**再跑 `judge.py` → **`overall = pass`**。

即：本 attempt 的 fail 是**判定器对探索顺序敏感**（判定器把模型的中间产物当成终态），不是修复本身错。
判定器归操作者所有，本票不改；判定口径的缺口记为 #849。

### attempt 2（样本 `…T092458`，会话 `2ea284fa-1814-4c8f-88d6-3a55723c59c9`）

| 段 | 内容 | 读数 |
| --- | --- | --- |
| phase1 | 完整修复任务 | `run/completed` @ **718.8 s**，基线后 **812** 事件（该段 813 条）；**47** 次审批 |
| ↳ 自愈 | `guard/stuck` @ seq 487 | `level=replan`、`pattern=stuck.alternating_loop`、`count=6`/`threshold=6`、`replan_count=1` —— 守卫先**重规划**而不是暂停；模型随即自述「bash 里 python 写出的文件…在命令结束后都没有持久化——只有 write 工具写的文件留了下来。换路：改用 write 工具整体…」并改用 `write` 工具，随后跑到完成 |
| turn2 | 复核操作者浏览器读数 + diff 审阅 | `run/completed`，**101** 事件 |
| turn3 | 按给定文本置计划清单状态 | `run/completed`，**63** 事件 |
| compact-fail | 指定摘要模型不可达（`?model=qwen3.8-27b`） | `context/compaction_failed` ×2，`error_class=transport_error`、`OpenAIPermissionDeniedError`；`tokens_before=tokens_after=80021`、`compacted_turn_count=0`（零写入） |
| compact-ok | 真实模型摘要（默认模型） | 20.6 s 后 `context/compaction_failed` ×2，`error_class=plan_section_mismatch`、message=`Plan section must be (none): no plan item is in progress`；同样零写入 |
| phase2 | 压缩后新窗口继续 | `run/completed`，**150** 事件 |

**终态**：

- `judge.py`：**`overall = pass`，`missing = []`**，逐项 `ok=true`（R-042 失败且写入 0、R-043 恰好 40、
  重试幂等、不同内容 409、页面与 DB 一致）。
- `final`：`records` **47** 行 = 7 条 seed + 40 条 `imp_R-043_*`（**`imp_R-042_*` = 0**）；
  事件 **1131** 条、末条 `run/completed`；上下文 **101431 / 200000**；改动 diff **84 行**。
- 真实浏览器（`browser-check.mjs`，Chrome 154.0.8037.98 headless）：
  `R-042 → 状态: failed / 总行数: 40 已写入: 0 / 错误: row 17: empty id`，`data-status=failed`、`data-written=0`。
- 本段计数：多行 bash 静默 no-op **26** 次；`edit` 失败 **1**、`apply_patch` 失败 **1**。

**诚实注记（本段）**

1. 压缩腿**两次都失败**，且**两个方向**都出现了：先 `transport_error`（摘要模型无权限），
   再 `plan_section_mismatch: Plan section must be (none)` —— 与第 1 次的 `in-progress item(s) missing: r2`
   合起来说明该状态下压缩在两个方向都被拒（**#844 的新读数**，本票只回票不修产品代码）。
2. 本段 phase1 的 `guard/stuck` 是 `level=replan`（自愈），**不是** pause；Run B 第 2 次 attempt 1 里
   同样的 pattern 升级成了 `level=paused`。同一道守卫在两处的处置不同，取决于循环是否被打破。
3. `总行数` 在两次运行里读到 40（本次）与 20（第 1 次）—— 样本的 R-042 CSV 行数不同，
   任务文本没有钉住这个数字；判定器也只判 `written=0`，不判总行数。见披露项。

---

## Run B · 第 2 次（2026-10-08）

### attempt 1（样本 `…T094214`，会话 `e2aaad3c-be05-4111-9b61-abe5fcb287f1`）：**stuck 暂停，未走到 kill 窗口**

| 段 | 读数 |
| --- | --- |
| tui-cold / tui-open / seed / host | 正常；`kill-watch` 基线 `import_rows=0`，**窗口始终没开** |
| task | `run/paused` @ **362.2 s**（388 事件） |
| 守卫 | `guard/stuck` @ seq 389：`level=paused`、`pattern=stuck.alternating_loop`、`count=12`/`threshold=6`、`replan_count=1`；`run/paused.reason=stuck`、`trigger_dimension=stuck.alternating_loop`；`consumed={agent_turns 47, model_requests 48, total_tokens 1013258, tool_calls 50}`（bash 36 / read 5 / update_plan 6 / edit 1 / apply_patch 1 / glob 1） |
| 循环形态 | 9 行的 `python -c`（本意写 `snippet.txt`）→ `exit_code=0`、`stdout=""`；紧跟 1 行 `python -c "…os.path.exists('baseline.txt')"` → 6 字符输出。两组交替 12 次 |
| 计数 | 多行 bash `exit=0` 且 `stdout` 为空 **16** 次；`edit` 失败 **1**、`apply_patch` 失败 **1**；`update_plan` 状态机被拒 ×2（seq 145/150，`pending→completed` 不允许） |

**根因（已开 #850 / #851）**：`edit` / `apply_patch` 因行尾不匹配失败（#851）⇒ 模型改用多行
`python -c` 补丁 ⇒ CPython 在 Windows 上把 `shell=True` 包成 `cmd.exe /c "<整段>"`，多行命令**静默空转**
（返回 `exit_code=0`、无输出、什么都没执行）（#850）⇒ 交替循环 ⇒ 守卫升级为 stuck 暂停。
本 attempt 因此**无法执行票面的 Run B 协议**（没有 kill 窗口、没有 reconcile）。

**续跑尝试与结论（记录在案）**：`budget_increase` 对 stuck 暂停被拒（409），
`relevant_steer` 需要「暂停之后有新 steer」而**注册入口当前不存在**（残余 6/9），
只剩 `environment_change` / `policy_change` 两条，且都必须有**当场观测到的真实差异**。
暂停快照里除 `permission_mode` 外各维 `policy_inputs` 为空，操作者没有可诚实主张的差异，
故**不伪造证据**，把本 attempt 记为失败，另起 attempt 2。

### attempt 2（样本 `…T102428`，会话 `0c3b6366-1ac8-457d-ac21-4677a3a3a9fa`）：**全腿完成**

| 段 | 内容 | 读数 |
| --- | --- | --- |
| tui-cold | 无服务时由安装后的 TUI 冷启动 | `ia-tui --check` **rc=0 / 13.6 s**；服务 `pid=26460 port=60743 uuid=21227b23…`；带凭据 200、不带 401 |
| tui-open | 真控制台里开新会话 | cmd `pid=25248`、node `[26464]`；会话 `0c3b6366…` |
| seed | 灌样本进 workspace | 10 个条目（含 `TASK.md`、原始 `app.py`、`review/`） |
| host | 操作者托管样例应用 | `127.0.0.1:8912` LISTENING（listener pid 21568） |
| kill-watch | 业务库已提交 + 账本有在飞调用 ⇒ 杀 Host | 窗口 @ **222.9 s** 命中：`import_rows 0 -> 40`，在飞 `chatcmpl-tool-a23f0d10895d5759`（bash `python verify.py`，`state=RUNNING`）；**kill 延迟 0.328 s**；服务已死 |
| ↳ kill 时刻读数 | 业务库 / 审计 | `R-042 failed 20/16 'row 17: empty id'`（**部分写入**）、`R-043 completed 40/40` |
| task | 指针文本经 TUI 键入 | 198.6 s 时事件读取抛 `RuntimeError`（服务已被杀）+ `kill.json` 标记出现 ⇒ 正常中断 |
| reopen | 从桌面重开（新服务）+ 读中断快照 | 新服务 `pid=27772 port=58492`；`GET /api/recovery/interrupted` → 200，`recovery=needs_manual_reconcile`，detail 点名 UNKNOWN 的 bash 调用并明写不变量 #14；`interrupted_runs=[{run 9a4bcc31…, interrupted_seq 106, step_id 13}]`、`resume_available=true` |
| tui-attach | 第二个真控制台 TUI 附着（同时在场） | node `[26464, 25612]` |
| investigate | 操作者查业务库 / 账本 / 悬空调用 | 业务库 `import_rows=40`；账本该会话 16 行；**悬空调用恰为 `chatcmpl-tool-a23f0d…`** |
| recover（裸请求） | 先发**不带 decisions** 的 `POST /recover` | **409**，`pending_decisions=[{tool_call_id chatcmpl-tool-a23f0d…, tool_name bash, state RUNNING, default_action DEFER, risk_level high, probe {verifiable false}}]` —— 把上一轮没复测的分支补上了 |
| recover（带裁决） | 按外部事实裁决 | **`CONFIRM_SUCCESS`** → `POST /recover`（1 条 decisions）→ **200**；事件 660→ 该会话 `operation/reconciled` @ seq 113、悬空 `[]` |
| browser（修复前） | 真实 Chrome 读页面 | `R-042 → failed / 总行数 20 已写入 16 / row 17: empty id`，`data-status=failed data-written=16` —— 与 kill 时刻 DB 一致 |
| resume | 续跑指令键入 `tui2` | `run/completed` @ **1026.4 s**，新增 **580** 事件；模型把 R-042 修到 `written_rows=0` 并写了属实的 `progress.md` |
| ↳ 第二个 UNKNOWN | 修复期间一条命令里的 `find` 落到 **MSYS `find`** 上，开始扫 C: | bash **60 s 超时** ⇒ 账本 `UNKNOWN`，`reconcile_meta={"unproven_side_effect": true, "error_code": "TIMEOUT"}`；**静默完成闸门**随即按住该 run（零写入：无 SessionEvent、无终态、无 `run/paused`） |
| recover-2 | 第二次 reconcile | 裸请求 → **409**（点名 `chatcmpl-tool-a84d2b1198357633`）；带 `CONFIRM_SUCCESS` → **200**（seq 533/534）。**reconcile 本身只产生 `session/resumed`，没有任何新的模型活动** ⇒ 操作者再往 `tui2` 键入一条收尾指令 |
| closeout | 收尾指令 | `run/completed` @ seq **698**（最终报告） |
| coexist | 三方同场事实表 | 桌面 4 进程 `[2632,15796,26092,27716]`、TUI node `[26464,25612]`、**一个服务** `pid=27772`；`GET /api/sessions` 200（16） |
| tui-exit | TUI 单独退出 | node `[26464,25612] -> [26464]`；服务 `27772 -> 27772`（存活）；桌面 pid 不变 ⇒ 通过 |
| desktop-exit | 桌面单独退出 | 桌面 `[...] -> []`；服务 `27772 -> None`（**随桌面消失**，与第 1 次一致） |

**终态**：

- `judge.py`：**`overall = pass`，`missing = []`**（六项逐项 `ok=true`）；操作者 diff（`app.py` 对原始模板）**73 行**。
- `final`：事件 **701** 条 / **3** 个 run；关键链齐全 —— `run/interrupted` ×2、
  `operation/reconcile-required` ×2、`operation/reconciled` ×2、`run/completed` ×1；
  `tool/call` 39 与 `tool/result` 39 **成对**；上下文 **41458 / 200000**（缓存命中率 0.989）；证据目录 **73** 个文件。
- 业务库终态：`imp_R-042_*` **0**、`imp_R-043_*` **40**、seed 7 条哈希不变；审计 2 行。
- 账本终态：两次 UNKNOWN 都被裁决为 `SUCCEEDED` + `CONFIRM_SUCCESS`，**0 非终态**。

**诚实注记（本段）**

1. **环境说明让步**：attempt 2 的操作者任务简报里加了两条**属实的环境事实**（cmd.exe 一条命令只能一行；
   仓库文本是 CRLF，整文件改动用 `write` 最稳）。这是为替代模型做的让步，**明确标注为环境补充、
   与验收口径同级但不改变任何验收条件**；第 1 次（以及 Run A 两次）用的都是未加说明的原始简报。
   两条说明分别对应 #850 与 #851 —— 换句话说，本 attempt 是在**绕过已知缺陷**的条件下跑通的。
2. **MSYS `find` 的 PATH 是 harness 伪影**：操作者的 Git Bash PATH 漏进了产品的 `cmd.exe`，
   使 `find` 解析到 MSYS 版本。这是操作者侧环境问题，不是产品缺陷；但它真实地演示了
   「UNKNOWN 高风险 Tool → 不盲重跑 → 人工 reconcile」这条链路。
3. **静默完成闸门后的终态需要另一条操作者消息**：reconcile 只解开了闸门，不会自动续跑；
   run 要到操作者再发一条消息才写出 `run/completed`。这是设计（ADR-0047 D3）下的正常形态，
   但操作者必须知道这一步，否则会以为卡死。
4. **重启扫描会把静默闸门按住的 run 补记为 interrupted**：收尾后操作者重开桌面，
   启动扫描发现 run `de1700fa…` 没有终态（闸门是零写入的），于是补写
   `run/interrupted{interrupted_seq 534, reason=process_restart}` + `session/resumed`（seq 699/700）。
   随后 `GET /api/recovery/interrupted` 报该会话 `recovery="recovered"`、`progress.source_event_seq=698`，
   同时仍把 `de1700fa…` 列在 `interrupted_runs` 里且 `resume_available=true`。
   这是 `recovery/scan.py::_mark_interrupted` 的既定行为（补记无终态 run），语义自洽；
   唯一毛刺是**已恢复的会话仍带 `resume_available=true`**，记为披露项，不单独开票。
5. **`final` 段前补了一次桌面重启**：`desktop-exit` 腿刚把服务带走，而 `final` 要读 `/context-usage`，
   故按 `reopen` 腿同样的方式（同 exe、同 cwd、同模型环境、默认 `APPDATA`）重新拉起桌面
   （`pid=3552 port=59371`），写进 `final-relaunch-endpoint.json`。这是操作者侧的取数动作，不改变上面的读数。

---

## 两个缺陷跨全部 gate 运行的计数（操作者侧只读统计）

对六次真实运行的会话事件流逐个统计（`D:\w21-work\summarize-run.py`）：

| 运行 | 会话 | 事件 | 多行 bash `exit=0` 且无输出（#850） | `edit` 失败（#851） | `apply_patch` 失败（#851） | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| Run A 第 1 次 | `c9d49b73…` | 2691 | 4 | 0 | 2 | judge pass |
| Run A 第 2 次 attempt 1 | `3cf0462a…` | 519 | 6 | 1 | 1 | judge fail（#849） |
| Run A 第 2 次 attempt 2 | `2ea284fa…` | 1131 | 26 | 1 | 1 | judge pass（靠 `guard/stuck` replan + 换 `write`） |
| Run B 第 1 次 | `430e304a…` | 2809 | 3 | 0 | 0 | judge pass |
| Run B 第 2 次 attempt 1 | `e2aaad3c…` | 393 | **16** | 1 | 1 | **stuck 暂停** |
| Run B 第 2 次 attempt 2 | `0c3b6366…` | 701 | **0** | 2 | 0 | judge pass（**带环境说明**） |

两个缺陷在**六次运行里六次都在场**。它们不一定阻断（Run A 第 1 次只有 4 次空转，靠 `write` 绕过），
但 Run B 第 2 次 attempt 1 证明它们能合成一个**自持的交替循环**把 run 推到 stuck 暂停；
attempt 2 的 0 次空转则是**人为加了环境说明**的结果，不是缺陷消失。
