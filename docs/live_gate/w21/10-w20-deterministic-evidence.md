# W-20 判定器确定性证据（阶段二-1）

- 冻结信息：代码树 `6728cf7e`（origin/main）；判定器 `tools/challenge-fixture/judge.py`（PR #797 已合并）；样例 `batch-import-demo`（`tools/challenge-fixture/templates/`，`generate.py` 两次生成初态一致，7 条 seed hash 见下）
- 复跑命令：`python3 ~/workspace/w21-work/drive.py <fixture_dir> <port> <out.json>`
  - 基线：`python3 ~/workspace/w21-work/drive.py ~/workspace/w21-work/demo-baseline 8931 ~/workspace/w21-work/baseline-result.json`
  - 修复版：`python3 ~/workspace/w21-work/drive.py ~/workspace/w21-work/demo-fixed 8932 ~/workspace/w21-work/fixed-result.json`

## Run 1：基线（植入 bug 未修复）—— 2026-10-07

预期行为：判定器必须 **fail**，且精确命中 B1/B2/B3 对应的三项（证明判定器能抓到坏修复，不是摆设）。

实际 `judge.py` JSON（全文见 `~/workspace/w21-work/baseline-result.json`）：

| check | ok | detail |
|---|---|---|
| old_records_intact | ✅ true | 7/7 hash 匹配 |
| r042_zero_writes | ❌ false | status=completed written=39 db_count=39 error=有（B2：应 failed 且 0 写入） |
| r043_exactly_once | ✅ true | status=completed count=40 unique=40 |
| retry_same_returns_existing | ✅ true | import_audit 中 R-043 行数=1 |
| retry_conflict | ❌ false | POST R-043 不同内容 → HTTP 202（期望 409）（B3） |
| ui_truthful | ❌ false | R-042 页面状态=completed（期望 failed）（B2 状态误报） |

`overall: fail`，`missing: [r042_zero_writes, retry_conflict, ui_truthful]` —— 与预期逐项一致。

流程时间线（`baseline-result.json` → `steps`）：
- R-042 首次提交 → 202 → 终态 `completed`，written=39，error=`row 17: empty id`
- R-042 同内容重试 → 202 `requeued` → 终态 `completed`，written=39（B1：重复入队；DB 去重靠 `INSERT OR IGNORE` 未翻倍，但语义错误）
- R-043 修正 CSV → 终态 `completed`，40 条各一次

## 真实浏览器验证（基线）—— 2026-10-07

- 工具：Playwright 自带 Chromium（`~/.cache/ms-playwright/chromium-1243`；`/opt/meta-chromium/chrome` 会拦本地访问，见 ~/AGENTS.md 教训）
- 脚本：`~/workspace/w21-work/browser-check.js`；命令：`W21_BASE=http://127.0.0.1:8941 node ~/workspace/w21-work/browser-check.js`
- 操作：真实打开状态页 → 输入框填 `R-042` → 点击"查询" → 等待渲染 → 读 `data-status`/`data-written` → 截图
- 结果：页面显示 `request_id: R-042 / 状态: completed / 总行数: 40 已写入: 39 / 错误: row 17: empty id`，`data-status=completed`，`data-written=39`
- 截图：`~/workspace/w21-work/browser-r042.png`（页面如实展示了 B2 的"失败标 completed" bug；判定器 `ui_truthful` 判 fail 与之一致）
- 结论：浏览器链路真实可用；页面状态与 DB 一致（都是错的 completed），判定器正确判 fail。

## kill/reconcile 语义演示（基线 fixture，手动）—— 2026-10-07

- 命令序列见本 doc 对应的 shell 记录；环境变量 `FAULT_KILL_AFTER_COMMIT=1`，端口 8951，fixture `~/workspace/w21-work/demo-kill`
- POST R-043（40 行合法 CSV）→ 202；随后进程 `os._exit(1)`（poll=1，符合故障注入设计）
- kill 后 DB 事实：`import_audit` 行 `status=processing, written_rows=0`（结果未持久化）；`records` 表 `imp_R-043_*` 恰好 **40 条已提交**
- 重启（无 fault）：worker 只取 `status='pending'`，`processing` 行不被重跑；重启后审计仍 `processing`、记录仍 40 条 —— **无盲重跑、无重复写**
- 结论：reconcile 语义成立（以 DB 事实为准裁决，不以"事件链补齐"代替）；但这是 fixture 层的 harness 语义演示，**不是** #365 要求的 TUI 真机 kill/reconcile 流程，不可折抵（见 20-blocked-items.md B-3）。

## Run 2：真实模型修复版 —— 2026-10-07 ✅ pass

- 修复者：CodeBuddy `deepseek-v4.1-flash`（真实模型，非 fake），通过 `~/workspace/system/codebuddy` 驱动；只改 `/home/hatch/workspace/w21-work/demo-fixed/app.py`
- 修复 diff（vs 仓库模板）：`Worker._process_one` 校验失败 `break` + `conn.rollback()` + `written=0` + `final_status="failed" if first_error`；`Handler.do_POST` 同 ID 时比较 `content_hash`：不同 → 409，相同 → 200 返回既有审计行。diff 全文见 `~/workspace/w21-work/`（`app-fixed-by-codebuddy.py` 备份）。
- 修复者自验证：15/15 断言 PASS（R-042 failed+0写、重试返回既有、R-043 40条恰一次、重试不翻倍、异内容409、seed完好；`FAULT_KILL_AFTER_COMMIT=1` 行为保留），日志 `~/workspace/w21-work/codebuddy-repair.log`
- **独立重跑**（`drive.py` + `judge.py`，全新 DB，端口 8932）：

| check | ok | detail |
|---|---|---|
| old_records_intact | ✅ true | 7/7 hash 匹配 |
| r042_zero_writes | ✅ true | status=failed written=0 db_count=0 error=有 |
| r043_exactly_once | ✅ true | status=completed count=40 unique=40 |
| retry_same_returns_existing | ✅ true | import_audit 中 R-043 行数=1 |
| retry_conflict | ✅ true | POST R-043 不同内容 → HTTP 409（期望 409） |
| ui_truthful | ✅ true | R-042 页面状态=failed（期望 failed） |

`overall: pass`，`missing: []` —— 判定器 pass 路径真实跑通（JSON 全文：`~/workspace/w21-work/fixed-result.json`）。

**结论**：W-20 判定器的 fail 路径（Run 1 精确命中 3 个植入 bug）与 pass 路径（Run 2 真实模型修复后 6/6）均已端到端验证。这证明的是**确定性层**（夹具+判定器），按挑战文档"通过判据"第 5 条，它不能代替 W-21 要求的两次真实模型完整通过（Windows 桌面/TUI 长任务仍阻塞，见 20-blocked-items.md）。
- 状态：✅ 完成
