# Run B：TUI 冷启动 → 库提交后/ToolResult 前 kill Host → 桌面重开 → 人工 reconcile → 续跑（#365）

- 安装件 sha256 `ce5fd54acf925bb6e17731e35990f18c52a0a90da57f834ad39960d0c6ad9880`
- 模型 `mimo-v2.6-flash`；样例 `D:\w21-work\run-b-sample-20261008T045103`；会话 `d4d78a49-c5a4-429f-b1a3-db551447842d`
- 上下文窗口：第 1 段（TUI 冷启动派生服务）用产品默认；第 2 段（续跑）操作者设为 `MAX_CONTEXT_TOKENS=600000`（见「分段与 W-04 暂停/恢复」）
- 操作者证据目录 `D:\w21-work\evidence\run-b\`（`run-b-manifest.json` 收录 91 个文件哈希）

## 结论（先说结果）

Run B 的**核心腿全部实测通过**：在业务库提交之后、ToolResult 之前杀掉 Host（kill 延迟 0.235 s，Ledger 中该调用 `RUNNING`/`finished_at IS NULL`）；
启动扫描把该 Operation 标为 `UNKNOWN` 并**拒绝自动恢复**（不变量 #14），把会话交人工；操作者按库/账本事实裁决 `CONFIRM_SUCCESS`，
服务把人工结果写回事件流并恢复会话；续跑由操作者的下一条指令完成，真实模型交付最终报告，操作者 judge 6/6 `pass`。

**但 Run B 不是完整通过**：票面里 TUI 相关的腿被安装件自身缺陷阻断——

- **TUI 重附着被 D12 阻断**（#842）：`--session <有用户消息的会话>` 启动即崩（`Invalid color value: rgb(38, 34, 38)`），
  因此「桌面重开后由 TUI 继续」这条路径在真机上不可用；续跑指令改为投递**与 TUI 相同的 HTTP 请求**（同端点、同 body），在 `resume-delivery.json` 里如实标注；
- **共存/退出腿改用新开会话的 TUI**（`0db00217-45ff-4b12-b902-ab31979678ad`，Tag=tui3）实测，并注明它附着的不是被恢复的那个会话；
- D13（#843）记录了被 kill 服务留下的 TUI 永久重连失败（端点不重解析）现象，定性交维护者。

本文件**不构成 #365 的通过结论**。

## 票面要求逐条

| 票面要求 | 读数 |
| --- | --- |
| 从安装后的 TUI 冷启动同一 Python 服务协议 | `ia-tui.cmd --check` → `service: started http://127.0.0.1:51742 (pid 9296, protocol 1, credential present)`，`GET /api/sessions` 200/401（`tui-check.txt`、`tui-cold.json`）；随后同一 TUI 创建会话 `d4d78a49…`（`tui-open-workspace.json`） |
| 真实模型完成同样完整任务 | 同一会话跨 kill 两段完成：run `a2a61307`（20:51:40 起，32 次工具调用、18 次审批）被 kill；reconcile 后 run `8f204212`（21:03:22 起，29 次工具调用、13 次审批）跑完并 `run/completed`（21:21:22，最终报告 2582 字符）；操作者 judge 6/6 |
| 数据库提交后、ToolResult 前 kill Host | `kill-window.json`：业务库 `imp_` 行 0→40 与 Ledger 在飞调用**同时**成立时才触发；被杀的 `call_4403e930cf3c41aba43c4f41`（bash）`state=RUNNING`、`started_at 20:58:43.438Z`、`finished_at NULL`；kill 延迟 0.235 s；杀后服务端口拒连；事件流 1698 条止于 `run/interrupted`，**没有任何该 tool_call_id 的 `tool/result`** |
| 从桌面打开 → 查 DB/Ledger → reconcile → 人工确认 → 手动续跑 | 桌面重开派生新服务（pid 5740/port 59380，新 service_uuid）；`GET /api/recovery/interrupted` → `needs_manual_reconcile`；操作者裸 POST `/recover` → 409 `pending_decisions`；按事实裁决 `CONFIRM_SUCCESS` 后 POST → 200，账本 `SUCCEEDED` + `reconcile_meta`；续跑由操作者下一条指令触发（**投递方式见「续跑交付」**） |
| 最终浏览器验证与 diff 审阅 | 操作者用真实 Chrome（Playwright channel，headless）独立读页面：`failed / 20 / 0 / 'row 17: empty id'`（`browser-reading.txt`、`browser-r042.png`）；模型在 TASK2 第 4 步把页面读数与库逐字段比对并保留原始读数；模型自审 diff 写 `review/final-fix.diff`（4846 B，86 行操作者 diff 对照） |
| 桌面/TUI 同时在场、单独退出规则各实测一次 | 同时在场：tui3 附着到桌面托管的同一服务（端点文件仍是 pid 22292/57986，未派生新服务），屏幕 `○ idle`/「还没有会话内容。」；TUI 单独退出：node 26200 消失，服务与 4 个桌面进程存活、HTTP 200；桌面单独退出：4 个桌面进程与服务同时消失（`service_after: null`） |

## 关键事件链（seq）

```
1693 tool/call            call_4403e930…  bash  ← 被杀在这一点之后
1694 tool/approval-requested
1695 permission/resolved  reason="Run B operator approval: isolated W-20 sample directory"
1696 permission/approval-granted
1697 run/interrupted       {"interrupted_seq": 1693, "reason": "process_restart"}
1698 operation/reconcile-required  state=NEED_RECONCILE
1699 tool/result           {"ok":true,"message":"操作 'bash' 崩溃时结果未知，已由用户确认成功（人工 reconcile）。"}
1700 operation/reconciled  {"verdict":"CONFIRM_SUCCESS","state":"SUCCEEDED","reconcile_required_event_id":"0dd5a7f9…"}
1701 session/resumed
1917 run/paused            budget_exhausted / max_context_tokens / version 1 / consumed.total_tokens=233142
1922 run/resumed           （操作者按 next_safe_action 提高 ceiling 后）
4933 run/completed         run 8f204212 结束
```

合计 4934 条事件、2 个 run；`tool/call` 61 = `tool/result` 61；`tool/approval-requested` 31 = `permission/resolved` 31；
`run/interrupted` 1、`operation/reconcile-required` 1、`operation/reconciled` 1、`run/paused` 2、`run/resumed` 2、`run/completed` 1。

## reconcile 契约读数（本票实测）

- **裸 POST** `POST /api/sessions/{id}/recover {}` → **409**，机器可读清单：
  `pending_decisions=[{"tool_call_id":"call_4403e930…","tool_name":"bash","state":"RUNNING","default_action":"DEFER","risk_level":"high","probe":{"verifiable":false,"suggested_action":null}}]`
  （`recover-null.json`）。
- **形状错误** `{"decisions":{…}}` → **422** `{"type":"list_type","loc":["body","decisions"],"msg":"Input should be a valid list"}`。
  即 `decisions` 必须是**裸列表**；这一条是操作者第一次 POST 踩到的真实契约细节（`run-b-recover.txt` 第一段）。
- **带裁决** `[{tool_call_id, verdict:"CONFIRM_SUCCESS", source:"…"}]` → **200** 并回传完整事件列表；账本随之为
  `state=SUCCEEDED`、`finished_at=21:02:37.649Z`、`reconcile_meta.verdict=CONFIRM_SUCCESS`、`reconcile_meta.source` 收录操作者给出的库/账本事实（`recover-result.json`、`decisions-note.md`）。
- 裁决依据（操作者撰写，落 `decisions-note.md`）：R-042 `failed 20/0 error='row 17: empty id'`、R-043 `completed 40/40` 且 `imp_R-043_*` 恰好 40 条、
  7 条 seed 仍在、无重复无半截；示例应用是**独立进程**（pid 26320）不在 kill 范围内，故副作用完成而 Host 未收到结果——因此是 `CONFIRM_SUCCESS` 而非盲重发（不变量 #14）。

## 启动扫描为什么把活儿交给人工

重开后的服务在启动扫描里写下 `GET /api/recovery/interrupted` 的 `detail` 原文：
「存在需要人工裁决的 UNKNOWN Operation（bash(tool_call_id=call_4403e930…)）：未提供 ReconcileCallback，拒绝恢复——避免伪造结果或盲目重跑高风险副作用（不变量 #14）」，
并给出 `interrupted_runs[0] = {run_id: a2a61307…, interrupted_seq: 1693, step_id: 23}`、`resume_available: true`（`recovery-interrupted.json`）。
即：**没有伪造结果、没有盲重跑**，这正是 #365 要看的「人工确认需要项」。

## 分段与 W-04 暂停/恢复

- 第 2 段跑起来后（21:03:22 起）在 seq 1917 触发 `run/paused`：`reason=budget_exhausted`、`trigger_dimension=max_context_tokens`、`budget_version=1`、`consumed.total_tokens=233142`。
  `GET /budget` 的 `next_safe_action` 明说要「提高绝对 ceiling（max_context_tokens）后以同一 run_id 恢复」，且该分支的 `blockers` 文本里 `consumed=未知, ceiling=未知`（两个占位符，实际数值本可给出）。
- 第一次 `POST /resume {budget_increase, expected_version:1}`（未提高 ceiling）→ **200 但立刻 re-pause**，`budget_version` 升到 2；
- 操作者按 W-04 换窗口：`taskkill /F /T /IM "Intelligence Agent.exe"` 后用 `MAX_CONTEXT_TOKENS=600000` 重启桌面（`run-b-relaunch.txt`、`relaunch-endpoint.json`：pid 22292/port 57986/新 uuid），
  再次 `POST /resume {budget_increase, expected_version:2}` → 同一 run 继续，直到 `run/completed`（seq 4933）。
- `POST /resume` 的响应是**该 run 的事件流**（本次 910 s 后才收完，`resume-budget-launch.log`、`run-b-resume-budget.txt`），操作者客户端需按 SSE 处理；
  这一条与 Run A 的记录一致。
- 结束时 `GET /context-usage`：`window_tokens=600000`、`used_tokens=152165`、`state=ok`、缓存命中率 0.61（39 次请求）。

## 续跑交付（为什么不是 TUI 手输）

- 交付请求：`POST /api/sessions/{id}/messages` body `{"content": "<ASCII 指针>", "mode": "queue"}` —— 与 TUI `api.js sendMessage()` **同端点、同 body**（`resume-delivery.json`）。
- 原因：安装件 TUI 附着该会话即崩（D12/#842，见 `46-run-b-defects-tui.md`），而先前那个 TUI 停在已死端口上永久重连失败（D13/#843）。
  本轮没有为了「让证据好看」去改产品二进制或伪造 TUI 环节。
- 指令正文：`TASK2.md`（工作区内）要求以磁盘为准复述中断前状态、查中断痕迹、逐条复测 5 个验收口径、复核托管方浏览器读数、更新 `features.json`/`progress.md`、给出最终 diff 审阅。

## 模型交付物（工作区实读）

- `features.json`：8 项全部 `passes: true`，其中 `kill-reconcile`（kill 后 reconcile 不盲重 POST）本轮**确有真实证据**才置真；条目未删改。
- `progress.md` 8186 B；harness 托管的 `agent-progress/<sid>/progress.md`（5409 B）+ `progress.prev.md` + `progress.meta.json`（轮转在案）。
- `review/final-fix.diff` 4846 B；`review/browser-reading.txt` 保留原始读数并追加比对结论（页面与库逐字段一致、404 归因为 favicon）。
- `verify_log.txt` 8912 B（kill 时是 0 字节，续跑后重跑验证写满），`verify.py` 自报 45/45 通过。
- 操作者独立判据：`judge.py` 6/6 `ok: true`、`overall: pass`（`run-b-judge.json`）；操作者 diff 86 行（`run-b-fix.diff`）。

## 账本终态

会话 `d4d78a49…` 的 `operations`：`SUCCEEDED 59` + `FAILED 2` = 61（与 61 次工具调用一致），**无 PENDING/RUNNING/UNKNOWN/NEED_RECONCILE 残留**；
被杀调用 `call_4403e930…` 的 `reconcile_meta` 记录了裁决人与依据。

## 操作者工具链观察（不是产品结论）

1. 审批回环（`approve-b.py`）把 `run/paused` 当终止事件，第一次暂停时自行退出，需要按当时的终止事件计数重设 baseline 再启动；期间模型停在一个待批工具调用上约 6 分钟（事件连续、无丢失）。
2. `run-b.py` 的 `follow()` 在 resume 之后计数为 0（而事件实际在增长）——操作者脚本的 wart，本轮以直接读事件与 `final` 汇总为准。
3. 两次审批事件带的是旧回环写下的 `reason="Run A operator approval…"` 字样（同一语义，只是标签复制自 Run A 脚本）；修正后的标签在后续审批里生效。

## 未覆盖 / 偏差（如实）

- TUI 重附着与「桌面+TUI 同时在场于**被恢复会话**」两条腿因 D12 无法实测（#842）；共存/退出腿用新开会话的 TUI（Tag=tui3）替代，并已在此标注。
- 续跑段的操作者窗口 600000 大于产品默认（第 1 段用的是默认）；这是 W-04 暂停后按产品 `next_safe_action` 提高 ceiling 的动作，已按段记录。
- 操作者工具链留下 3 个孤儿会话（`896132bb…` 卡死的驱动、`07e85c11…` 第 1 次尝试、`0db00217…` 共存腿 tui3），清理与归档另行处理。
- 本文件只覆盖 Run B；两次独立完整通过、W-16 其余腿与双轴独立审查未在此范围。
