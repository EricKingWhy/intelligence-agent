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
