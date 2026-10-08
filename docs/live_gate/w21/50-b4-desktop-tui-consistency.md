# B-4 桌面 / TUI 同一 Task 状态与 Event seq 一致（实测）

- 票面原文（#365 交接注释 B-4）：「桌面/TUI 同时在场和单独退出规则各实测一次」「桌面/TUI 同一 Task
  状态与 Event seq 一致」。
- 环境：安装件 `Intelligence-Agent-Setup-0.1.0.exe`（sha256 `01477e59…`，HEAD `28a382cc`）；
  **一个服务** `pid=8472 port=57257`（由桌面派生，TUI 附着到同一个它）；一个数据根
  `%APPDATA%\intelligence-agent\workspace`。
- 取数方式：桌面用 `--remote-debugging-port=9222` 起，CDP 读**渲染层**（会话栏 title 属性 +
  `document.body.innerText` + 截图），脚本 `D:\w21-work\b4-dom2.mjs`；TUI 用真实控制台驱动
  `tui-console.ps1`（内核级按键注入 + `ReadConsoleOutputCharacterW` 整屏读取），新控制台由
  `D:\w21-work\b4-tui-launch2.py` 起；服务端权威值直接读 `GET /api/sessions`、`/events`、`/stream`。
- 工作树证据：`D:\w21-work\evidence\b4\`（清单见文末）。

## 一致的部分（读数为证）

### 长会话 `0c3b6366-1ac8-457d-ac21-4677a3a3a9fa`（701 事件）

| 读数 | 桌面（CDP 渲染层） | TUI（整屏） | 服务端权威 |
| --- | --- | --- | --- |
| 事件数 | 会话栏 `title` = `0c3b6366-1ac8-457d-ac21-4677a3a3a9fa · 701 事件 · 未分组` | `/sessions` 选择器首行 `701 events · 2026-10-08T02:57:27.773+00:00` | `GET /api/sessions` → `event_count = 701` |
| 投影游标（seq） | 会话内容与该 seq 同源（末条 `run/completed` 的内容逐字一致） | `/progress` → `expected_source_event_seq = 700`（= 末 seq；701 条 = seq 0..700） | `GET /events` 末条 `seq = 700` |
| 同一内容 | 最终报告：`R-042 failed/0`、`R-043 40/40`、`HTTP 409`、`旧 7 条 seed … payload_hash 未改写`、`还剩什么 / 无剩余项`、`约束遵守情况 … CONFIRM_SUCCESS，未重跑` | 同一份最终报告（同一段文字，逐字同源） | seq 698 `run/completed` |
| 计划（Task 状态） | `进程 7 项 · 已完成 7 项` | **无计划投影面**（TUI 只在 `update_plan` 工具卡里体现） | 末条 `task/plan_updated` 7 项 completed |

### 短会话 `896132bb-49ad-4ace-a54a-8512adbfada6`（附着重建后）

| 读数 | 桌面（CDP 渲染层） | TUI（新控制台附着，`b4short3`） | 服务端权威 |
| --- | --- | --- | --- |
| Task 状态 | `已完成` | `─ completed · tokens in 4.8k, out 3` | 末条 `run/completed`（`final_text=OK`） |
| token | `· 4,812 tok` | `tokens in 4.8k, out 3` | `usage_total {prompt 4809, completion 3, total 4812}` |
| 同一内容 | 第 1 轮 `Reply with exactly: OK. Do not call any tools.` → `glm-5.3-flash` → `OK` | 同一条用户消息 + `OK` | seq 2 `user/message`、seq 7 `model/completed` |

结论：**同一服务、同一会话、同一事件流，两个客户端在「事件数 / 投影游标 / 同一 Task 内容」上可以逐项对齐**
（0c3b6366 的 701 与 896132bb 的 `已完成`/`─ completed` 两组读数）。

## 不一致的部分（本段实测出的两条新缺陷，B-4 因此未通过）

### N-1（P1，已登记 #853）TUI 对**空闲**会话发消息必然报假失败，并丢弃这次 run 的 SSE 流

- 复现（2026-10-08 11:50，会话 `896132bb`，此前 1 条事件、idle）：
  在 TUI 里键入 `Reply with exactly: OK. Do not call any tools.` 回车。
- TUI 整屏读数（`b4short2-run2.txt` / `b4short2-now.txt`）：
  `○ idle` + `还没有会话内容。` + 备注行
  `send failed: SyntaxError: Unexpected token 'd', "data: {"ty"... is not valid JSON`。
- 同一时刻的服务端事实：`POST /messages` **200 / `content-type: text/event-stream`**，
  首帧即 `data: {"type": "user/message", … "seq": 2}`；随后 seq 3 `run/started` … seq 8 `run/completed`
  （`final_text=OK`，`usage_total.total_tokens=4812`）。**消息已被接受并跑完**。
- 桌面读数（同一会话、同一时刻，`b4short-desktop-conversation.txt`）：`896132bb` + `已完成` +
  `· 4,812 tok` + 第 1 轮 `… OK. Do not call any tools.` → `OK`。
- 根因（代码级，`file:line` 可复核）：
  1. 服务端：`src/agent_harness/web/app.py:3358-3363` —— `mode=queue` 且会话 idle 时走
     `result.status == "launched"` 分支，返回 **SSE**（`_launched_response`）；
     只有在途 run 才返回 JSON 确认（`:3364-3368`）。
  2. TUI：`tui/src/api.ts:78-83` 恒发 `{content, mode:"queue"}`；`tui/src/api.ts:52`
     `return (text ? JSON.parse(text) : null)` **无条件把响应体当 JSON 解析** ⇒ 对 SSE 体抛
     `SyntaxError`；`tui/src/app.ts:209-213` 捕获后落 `send failed:` 备注。
  3. 次生影响：`response.text()` 会**等整条 SSE 流结束**（即等这次 run 收尾）才返回，
     所以「发送」在 UI 上是「挂住整轮 run，然后报失败」；用户既看不到自己的消息回显，也看不到失败原因。
- 该缺陷**在票面跑次里就存在**：Run B 第 1 次的 `evidence/run-b-pass1/tui2-screen-before-exit.txt`
  含同一行 `send failed: SyntaxError: Unexpected token 'd', "data: {"ty"... is not valid JSON`
  （当时未记录；因 run 由服务端驱动并完成，未影响该次判定器读数）。

### N-2（P1，已登记 #854）空闲时附着的 TUI 收不到后续 run 的直播帧，状态栏一直停在 `idle`

- 复现（同一控制台 `b4short2`，附着时会话 idle）：
  1. 11:50 由 TUI 自己发起的 run（seq 1..8）跑完后，11:50:43 / 11:50:55 / 11:51:07 / 11:52 四次整屏
     都是 `○ idle` + `还没有会话内容。`；
  2. 11:55 由操作者经 `POST /messages` 起第 2 个 run（seq 9..16，末条 `run/completed` `OK3`），
     35 s 后整屏仍是 `○ idle` + `还没有会话内容。`（`b4short2-live.txt`）；
  3. 同一会话上新起一个控制台附着（`b4short3`）**立刻**渲染出该 run：
     `─ completed · tokens in 4.8k, out 3` + 用户消息 + `OK`。
- 桌面在同一时刻读到两轮（`b4short-desktop2-conversation.txt`）：`已完成`、`· 4,987 tok`、
  第 1 轮 `OK`、**第 2 轮** `OK3`。
- 即：**同一会话、同一时刻，桌面显示 2 轮已完成；TUI 显示「没有会话内容 + idle」。**
- 根因未完全定位（诚实标注），两个可复核的嫌疑点：
  1. `src/agent_harness/web/app.py:2480-2498`（`GET /stream` 的 `event_generator`）：
     `if subscriber is None: return` —— 会话 idle 时没有在途 run 的 subscriber，流按设计「重放到 latest
     后收尾」；此后新起的 run 的帧不会推到这条已收尾的连接上，客户端必须**自己重连**才能拿到。
  2. `tui/src/app.ts:163-197`（`subscribeLoop`）+ `tui/src/api.ts:39-52`：TUI 只在 `openStream`
     收束后才按 1 s 重连；而它自己那次 run 的 SSE 流（`POST /messages` 的响应，`app.py:3358-3363`）
     被 `response.text()` 读空后丢弃（见 N-1），等于把唯一带着本次 run 的流扔掉。
  3. 观测到的旁证：`b4-tui-conversation.txt`（Run B 第 2 次 attempt 2 遗留控制台）里是
     `stream reconnect: TypeError: fetch failed` 循环 —— 即 TUI 流通道在别的时刻以另一种方式断掉，
     与已登记的 #843 同族。
- 严重度：B-4 的票面要求正是「桌面/TUI 同一 Task 状态与 Event seq 一致」；此形态下**同时在场时两者不一致**，
  且 TUI 侧的状态栏是错的（`idle`）。不涉及数据正确性（服务端事件流与账本不受影响），故按 P1/P2 之间报。

### N-3（观察项，未单独立票）会话终态投影在「restart 回填 `run/interrupted`」后与最新 run 不一致

同一长会话 `0c3b6366…` 的两端终态读数（桌面 CDP `b4-desktop-conversation.txt`）：

| 面 | 读数 |
| --- | --- |
| 桌面状态徽标 | **`已中断`** + `上次运行在第 34 步中断（原因：process_restart）` |
| 桌面计划 | `进程 7 项 · 已完成 7 项` |
| TUI `/progress` | `status: "stale"`、`expected_source_event_seq: 700` vs `file_source_event_seq: 698`、`verifiable: false`、`reason: "文件 source_event_seq=698 落后于当前投影 700"` |
| 服务端事件流 | 最新 run 于 **seq 698 `run/completed`** 收口；**seq 699 `run/interrupted`** 是启动扫描为更早的孤儿 run `de1700fa-e8be-490d-bb42-7adce098eab8`（`interrupted_seq 534`）回填的 |

即：**最新 run 已完成，但两端的终态投影都指向「中断」**——桌面按「最后一个 `run/interrupted`」判定，
启动扫描又把孤儿 run 的中断事件补在完成事件**之后**，投影层无法区分这两者。
`web/src/lib/projection.ts::projectRunStarted` 的注释意图是「**最近一个** run 以中断收口」，
本读数与该意图相悖；TUI 侧同源表现为 progress 永久 `stale`。
`GET /api/recovery/interrupted` 同刻报 `recovery: "recovered"`、`progress.source_event_seq: 698`，
却仍把 `de1700fa` 列为 `resume_available: true`。

本项与 #854（陈旧终态展示）同族，**未单独立票**，登记在此供裁决时一并处置（#365 不在本票修产品代码）。

## 诚实注记

1. **桌面会话栏的 `N 事件` 是加载时快照**：`896132bb` 已有 17 条事件时，会话栏仍写
   `1 事件 · 7 小时前`（`b4short-desktop2-session-list.txt`）；点选该会话后会话区立刻是当前事实
   （`已完成` / 2 轮）。所以「事件数一致」只在两端都是**新鲜读数**时成立（0c3b6366 那组即是）。
   这条不计入缺陷，只说明取数口径。
2. **TUI 状态栏在长会话里看不到**：`tui/src/app.ts:115` 把 `statusText` 作为第一个子组件，
   而 `TuiMainScreen` 渲染进主屏并**把视口钉在底部**（
   `node_modules/@earendil-works/pi-tui/dist/tui-main-screen.js` 的
   `previousViewportTop = max(0, bufferLength - height)`），所以会话长于一屏时状态行被裁到屏外
   （本机控制台缓冲区只有 `120x40`，无回滚可查）。短会话（`b4short2`/`b4short3`）能读到该行，
   长会话（`b4tui`，701 事件）读不到。这是本段「TUI 侧 Task 状态」取证不全的原因，也是产品侧一个
   可议的可用性问题（未单独立票，供裁决）。
3. **注入器只能注入 ASCII**：`tui-console.ps1` 的 `WriteConsoleInputW` 结构体里 `char` 字段未标
   `CharSet.Unicode`，注入非 ASCII（中文）时抛
   `传递给系统调用的数据区域太小 (0x8007007A)`。这是**我这边证据工具的限制**，不是产品缺陷
   （TUI 渲染中文正常）。B-4 的短任务因此改用 ASCII 文本。
4. **本文件不宣布 B-4 通过**：一致的部分已逐项读数；不一致的部分（N-1/N-2）使「桌面/TUI 同一 Task 状态
   一致」在**同时在场**这一条上不成立。两条缺陷按 #365「不在本票修产品代码」只登记、不修。

## 工作树证据清单（`D:\w21-work\evidence\b4\`）

- 桌面侧：`b4-desktop-facts.json`、`b4-desktop-session-list.txt/.png`、`b4-desktop-conversation.txt/.png`、
  `b4-desktop-dom.json`（长会话 0c3b6366）；`b4short-desktop-conversation.txt`、
  `b4short-desktop2-conversation.txt`、`b4short-desktop2-session-list.txt`、
  `b4short-desktop2-conversation.png`、`b4short-desktop2-session-list.png`、`b4short-desktop-dom.json`、
  `b4short-desktop2-dom.json`（短会话 896132bb）。
- TUI 侧：`b4-tui2-conversation.txt`、`b4-tui2-picker3.txt`（`/sessions` 选择器，含 `701 events` 行）、
  `b4-tui2-progress.txt`（`expected_source_event_seq: 700`）、`b4-tui2-help.txt`、
  `b4tui-buffer.txt`、`b4tui-console.txt`、`b4tui-inbox.txt`（长会话控制台）；
  `b4short-screen.txt`、`b4short-buffer.txt`（8 事件会话）；`b4short2-screen.txt`、`b4short2-run0/1/2.txt`、
  `b4short2-now.txt`、`b4short2-progress.txt`、`b4short2-live.txt`、`b4short2-buffer.txt`、
  `b4short2-console.txt`（N-1/N-2 的控制台与驱动日志，含 0x8007007A 那条）；`b4short3-screen.txt`、
  `b4short3-buffer.txt`（附着重建后 `─ completed`）。
- 脚本：`D:\w21-work\b4-tui-launch2.py`（新控制台）、`D:\w21-work\b4-dom2.mjs`（CDP 读渲染层）。
