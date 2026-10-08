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
