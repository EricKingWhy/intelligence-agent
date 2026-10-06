# W-12（#356）第一阶段调研报告

- **分支**：`codebuddy/356-w12-client-presence`（基线 `cf6e5470`，干净检出）
- **日期**：2026-10-06
- **范围声明**：本阶段只做成熟产品调研与票面成立性判断（SDD §6.1 / §1.3），不写产品代码、不做设计、不施工。本阶段唯一允许落盘的文件是本报告；未触碰 `src/`、`tests/`、`web/`。
- **驱动**：CodeBuddy CLI 无头模式（包装脚本默认 `deepseek-v4.1-flash`）做成熟产品调研；两处关键引用由我独立复核（见 §6）。

## 1. 启动检查表复核（五项，任一项不成立即停——全部成立）

| 前置项 | 实际读取依据 | 状态 |
|---|---|---|
| Vision 相关原则 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/02_AGENT_RUNTIME.md §5.2.1`、`11_STREAMING_API_WEB_UI.md §6.2`、`docs/adr/0046-client-presence-safe-pause.md`（accepted） | READY |
| 当前任务规格 | `gh issue view 356` 完整票面（含 2026-10-03 审计节、五场景状态表、工作指令、验收、成熟参考） | READY |
| Reuse 相关判定 | 票面参考（asyncio shield 标准库 REUSE、DSH quit-confirmation.ts MIT ADAPT）；本报告按 SDD §1.3 落方案依据块 | READY |
| Phase 依据 | `gh issue view` 逐个核实 #366（W-22）、#355（W-11）、#341、#342、#354（W-10）均为 CLOSED；`src/agent_harness/agent/client_presence.py`（W-22 `ClientPresenceGate` 已合入）与 `src/agent_harness/workspace/lease.py` 的 `TaskPresenceReader` 只读 seam（W-10 预埋）在位 | READY |
| 本任务触发细则 | `AGENTS.md` 全文（543 行）、`docs/SDD_WORKFLOW_PROTOCOL.md` 全文（895 行，首行至 EOF 分段读完）、`docs/agents/reference-sources.md` 对应领域清单、PROVENANCE.md §4.2（只取方法论，跳过 `.cursor/`、`agent-transcripts/`、`scripts/log.sh` 等悬空引用） | READY |

## 2. 方案依据（SDD §1.3：来源 / 机制摘要 / 契合点 / 判定 / License）

### 2.1 Python asyncio 取消与 shield 语义

- **来源**：官方文档 <https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation>（读取 2026-10-06）；中文镜像 <https://docs.python.org/zh-tw/3.7/library/asyncio-task.html>。
- **机制摘要**：`asyncio.shield(aw)` 保护内部 awaitable 不被**外层**取消：外层被取消时内部 Task 不取消（站在内部视角取消"未发生"），但**调用者仍会收到 `CancelledError`**，且 shield 不替你等待内部 Task——必须自行持有**强引用**（事件循环只持弱引用）并显式等待收尾。
- **契合点**：它回答的是"取消收尾"而非"客户端在场"，恰好划出边界：presence 是独立问题，shield 只解决"外层被取消时别把在途 Tool 弄丢、只产生一次终态"这一半。与 Async-first、不变量 #13（Ledger 支持 reconcile）天然契合；与 W-22 `ClientPresenceGate`（缺席置位不取消 task）方向一致（不强杀）。无冲突。
- **判定**：**REUSE**（语言级原语，无代码移植）。票面审计节已标 REUSE（标准库；不替代 kill/reconcile）。
- **License**：PSF。

### 2.2 Pi（earendil-works/pi，原 badlogic/pi-mono）

- **来源**：官方 <https://earendil.com/posts/pi-durable/>（约 2026-10-02）；仓库 `packages/durable` <https://github.com/earendil-works/pi/tree/main/packages/durable>；官方 SDK 文档 <https://pi.dev/docs/latest/sdk>；百科 <https://aiwiki.ai/wiki/pi_agent>（更名时间线与 MIT 承诺 RFC 0015）；issue <https://github.com/earendil-works/pi/issues/9386>。
- **机制摘要**：Pi 是**本地单进程 TUI**，架构上**无 client/server 在场概念**——刻意缺省后台进程管理器（用户被指向 tmux）。① 会话 = 本地 append-only JSONL 树（"认父不认子"，回退/分支只移 `leafId` 不删节点），恢复靠重开会话文件；② 中止纪律："The active run owns idle-timeout and abort cleanup. Abort produces terminal behavior once; do not emit or settle a run twice."（run 自己负责 idle 超时与 abort 收尾，**只产生一次终态**）；③ Pi Durable："every step of a run is a task that stores a checkpoint before it moves on. If the process dies, a new process continues from the last checkpoint."（进程死→新进程从 checkpoint 续跑，Durable 甚至自动续跑）；④ #9386 指出 `abort()` 启动后不暴露完成/失败——正是"收口必须可观测/可 reconcile"要避免的坑。
- **契合点**：强契合——"run owns abort cleanup / terminal-once" 与本仓不变量 #3（append-only typed SessionEvent）、#13（Ledger 支持 reconcile）同源，可作外部佐证。**强冲突**：Pi 对"客户端离开"的答案是**继续跑 + 从 checkpoint 续跑**，与票面"暂停 + 重连不自动续跑"**方向相反**。**无 presence 可复用**。
- **判定**：abort 纪律 / terminal-once / append-only record log → **ADAPT**（语义参照）；durable checkpoint 续跑 → **ADAPT 作为更简单选项的证据**；按 Task 在场登记 → **DEFER/BUILD**（无先例）。
- **License**：MIT（RFC 0015：Pi 保持 MIT 不变）。

### 2.3 Claude Code

- **来源**：官方 <https://code.claude.com/docs/en/sessions>（会话=绑定项目目录的已保存对话，本地存储，可恢复/分支）；后台任务文档（`/bg`、`claude --bg`、Ctrl+B 后台跑 Bash、`TaskOutput` 查看）；会话存储分析 <https://github.com/CHANGJianshuo/claude-code-analysis/blob/main/analysis/04i-session-storage-resume.md>（每 session 一 `.jsonl`，append-only）。
- **机制摘要**：① Session = 绑定项目目录的本地 append-only transcript，断后靠 `--continue`/`--resume`/`/resume` 恢复、rewind/fork 回退分支；② **无服务端按 Task 在场**，客户端即本地 CLI，会话持久化在磁盘；③ 长任务 = **后台化而非暂停**：`/bg` 把整 session detach 后**继续运行**；④ 中断（Esc/Ctrl+C）是用户主动输入，非"探测到离开"。
- **契合点**：append-only transcript + resume/rewind/branch 与本仓 append-only SessionEvent 同源。**冲突**：客户端离开≈detach/关闭，Claude Code 让工作**继续（后台）**，不是"最后离开→暂停"。**无 presence 可复用**。
- **判定**：append-only transcript + resume/rewind → **ADAPT**；"离开→后台继续 + 事后显式 resume" → **ADAPT 作为更简单选项的证据**；按 Task 在场+宽限+暂停 → **DEFER/BUILD**。
- **License**：Anthropic 专有（仅行为参照，不可复制）。

### 2.4 DeepSeek Harness（apps/desktop）

- **来源**：官方 README <https://github.com/deepseek-ai/deepseek-harness/blob/HEAD/apps/desktop/README.md>（"Closing the window and quitting" 节，读取 2026-10-06）；官方设计说明 `.agents/notes/implemented/architecture/2026-09-23-desktop-close-to-background-and-quit-confirmation.md`；提交 <https://github.com/deepseek-ai/deepseek-harness/commit/474593468347d06f4d614b6e0b7d270827dd82c3>（feat：关窗隐藏 + 可中断退出的确认）；加固提交 <https://github.com/deepseek-ai/deepseek-harness/commit/e6f135f15cf11ee1a8f3b3f9d0b258504426ae30>；源码 `apps/desktop/src/quit-confirmation.ts`、`apps/desktop-host/src/quit-inspection.ts`（正文未逐行 raw fetch，行为以官方 README/设计说明为准）。
- **机制摘要**：**窗口/进程级**的"软离开 vs 硬退出"，**非按 Task 在场**。① **关窗=隐藏**：主窗只 `hide`，Host 继续、任务继续；设计说明记录旧 bug（关窗销毁窗口导致 macOS Dock 重建新页，丢失会话/草稿/滚动），改为隐藏+保留 Host 会话；首次隐藏弹窗提示"运行中任务不会中断"，写 `background-close-confirmed`；② **退出=硬退出，先问 Host"会打断什么"**：所有普通退出入口经**私有 IPC**（`quit-inspection`）查询运行中任务（运行中 agent 含子代理、等待审批回合、排队消息、后台任务）+ 已加载会话的定时提醒；两者皆无→静默退出，否则弹无父窗原生框（确认后不再复查）；③ **不确定性 fallback**：Host 未 ready/失败→直接退出；**检查失败或 Host 超 2 秒未应答→按"有任务在跑"处理**（偏保守 busy）。
- **契合点**：意图最接近票面"区分明确退出与意外断线"的一半——确实区分**软离开（隐藏）与硬退出（quit）**，且硬退出前做"会打断什么"的**能力查询**（而非在场登记）。**关键分歧**：DSH 对软离开的处置是"**继续运行**"；无 per-Task presence 表、无宽限、无 `client_absent`、无"最后离开→暂停"。可借鉴的不变量：**不确定性偏向"有任务在跑"** + **退出前向执行侧做能力询问**。
- **判定**：软/硬退出区分 → **ADAPT**；退出前能力询问（quit-inspection 设计）→ **ADAPT**；不确定性偏 busy → **ADAPT**（注意与票面"grace 不发新模型"的方向差异，见 §4）；按 Task 在场+宽限+暂停 → **DEFER/BUILD**（无此物且方向相反）。
- **License**：**MIT**（`quit-confirmation.ts` 同仓；票面审计节已标 ADAPT）。

### 2.5 Cline 与 OpenAI Codex

- **来源**：
  - Cline：官方 <https://docs.cline.bot/core-workflows/task-management>；DeepWiki <https://deepwiki.org/cline/cline/3.1-task-lifecycle>；PR <https://github.com/cline/cline/pull/3307/files>。
  - Codex：<https://github.com/openai/codex/issues/50118>（turn completed 后仍 markedStreaming=true，独立复核，读取 2026-10-06）；<https://github.com/openai/codex/issues/43182>（Desktop resume 选旧 turn 为 latest，JSONL 里后续历史仍在，独立复核，读取 2026-10-06）。
- **机制摘要**：
  - **Cline**：`abortTask(isAbandoned)` 置 abort、取消在途 API、清消息队列、杀终端/关浏览器、发 `TaskAborted`；abort 原因分类 `user_cancelled` / `streaming_failed` / `task_delegated`（**区分"用户取消"与"流式失败"**）；恢复靠 `resumePausedTask()` / `resumeTaskFromHistory` 从落盘历史重建，**用户显式触发**；无心跳在场/宽限/服务端暂停。
  - **Codex**：客户端断线→**连接层有界重连 + 流空闲超时 + 请求重试**（连接层处理断续，而非按 Task 在场表）；服务端持 session/turn 状态可重连继续。**痛点（高价值）**：#50118（后台 turn 被报 completed 而线程仍 markedStreaming）、#43182（三处视图对"最新 turn 是哪个"结论不一致）——**即"run 到底 paused/orphaned/仍在跑"的歧义，成熟产品上真实存在且未干净解决，正是 NEED_RECONCILE 要解决的问题**。
- **契合点**：Cline 的 abort 原因分类支持票面"区分明确退出与意外断线"的方向；Codex 的歧义 bug 反向证明"**run 终态必须由持久化状态机决定，而非连接层**"（02 §5.2.1 的 NEED_RECONCILE 优先正对这一坑）。冲突：两家都没有 per-Task 在场表；Codex 对断线是连接级重试+继续，不是暂停。
- **判定**：abort 原因分类 + 持久化显式 resume → **ADAPT**；连接层有界重试+idle 超时 → **ADAPT**；按 Task 在场登记 → **DEFER/BUILD**。
- **License**：Cline / Roo-Code **Apache-2.0**；Codex 多为 **Apache-2.0**（以仓库为准）。

## 3. 覆盖矩阵

| 能力 | DSH | Pi（含 Durable） | Claude Code | Cline | Codex |
|---|---|---|---|---|---|
| 服务端按 Task 登记在场 | ✗ | ✗ | ✗ | ✗ | ✗ |
| 区分明确退出 vs 意外断线 | △ 隐藏 vs quit | ✗ | ✗ | △ user_cancelled/streaming_failed | △ 连接层重试耗尽 vs 完成 |
| 重连宽限 | ✗ | ✗ | ✗ | ✗ | △ 有界重连+idle 超时（连接级） |
| 最后离开→**安全暂停** | ✗（继续跑） | ✗（继续/自动续跑） | ✗（后台继续） | △（用户 abort 或显式 resume） | ✗（继续/重连） |
| 重连**不**自动续跑 | ✗ | ✗（Durable 自动续跑） | ✗（显式 resume） | ✓（显式 resume） | ✗ |
| Ledger/日志收口在途 Tool | △（有 reconcile 语义） | ✓（abort terminal-once） | △（transcript append-only） | △（abort 清理） | △（turn 状态机，有歧义 bug） |

**读法**：没有任何一列同时满足"按 Task 在场 + 断线/退出区分 + 宽限 + 最后离开暂停 + 不自动续跑"。各家的解都在两条线上：① **继续跑 / 持久化续跑**（Pi、Claude Code、DSH、Codex）；② **软/硬退出区分 + 退出前确认/能力询问**（DSH）。

## 4. 核心裁决：票面 framing 是否成立？

### 4.1 一句话结论

**票面 framing 作为"按 Task 登记在场 + 区分明确退出/意外断线 + 30 秒重连宽限 + 最后客户端离开→安全暂停 + 重连不自动续跑"的整套机制不成立**（无成熟产品先例，且成熟产品方向相反）；但其中"**区分断线与失败、不走 `run/failed(reason=orphaned)`、在途 Tool 按 Operation Ledger 收口、NEED_RECONCILE 优先**"的子命题成立，应保留为真正的核心。

### 4.2 逐项裁决

- **成立、保留（本票真正的价值）**：
  1. "区分断线与失败"：被 Cline 的 abort 原因分类（`user_cancelled` vs `streaming_failed`）、DSH 的隐藏/退出区分部分佐证；
  2. "不走 `run/failed(reason=orphaned)`，走 `run/paused(reason=client_absent)` 或 `needs_reconcile`"：被 Codex #50118/#43182 **直接佐证**——成熟产品上"run 到底什么状态"的歧义真实存在且未干净解决，reconcile 方向正确；W-22 已合入的 `ClientPresenceGate`（缺席置位只走准入点暂停，不取消 task、不强杀）与之一致；
  3. "在途 Tool 按 Operation Ledger 收口而非强杀"：被 Pi 的 terminal-once、asyncio shield 语义共同佐证；
  4. "暂停前更新 W-05 进度；UNKNOWN/NEED_RECONCILE 优先于可安全续跑的 paused"：02 §5.2.1 已批准的语义，无冲突证据。
- **不成立、建议 DEFER/收窄**：
  1. **按 Task 的四字段在场登记表**（`client_id + task_session_id + presence_kind + last_seen`）：没有任何成熟产品做服务端按 Task 在场登记；各家要么根本没有这个概念（Pi、Claude Code），要么只在**连接层**做 liveness（Codex 的重连+idle 超时）；
  2. **30 秒重连宽限 + "宽限内不发新模型 step"**：宽限本身在连接层有先例（Codex），但"服务端按 Task 记宽限、宽限内冻结新模型步骤"这一整套无先例；
  3. **"最后客户端离开→安全暂停" + "重连不自动续跑"**：成熟产品方向**相反**——客户端离开→继续跑（DSH 关窗隐藏继续、Pi Durable 自动续跑、Claude Code 后台继续），暂停/续跑由**用户显式动作**触发（Cline resume、Claude Code --resume）。

### 4.3 明显更简单的做法（均有成熟先例，符合"简单实用优先"原则）

1. **服务端不感知 + 持久化 reconcile/续跑（最省，推荐默认）**：Pi Durable（每步 checkpoint，进程死→新进程续）、Claude Code（append-only transcript + resume/rewind）、DSH（关窗隐藏、任务继续）。客户端离开**不触发 run 状态变更**；`SessionEvent` 照写；Ledger 在**下次接触/重启时** reconcile；`failed(orphaned)` 仅在**确证进程死亡或 reconcile 失败**时用。覆盖票面五场景 90% 诉求，且**不引入 presence 状态机**。
2. **软/硬信号拆分（DSH 式）**：明确退出=客户端**主动发**"我要走了"的信号；意外断线=没有该信号。软信号→继续跑；硬信号→先问执行侧"会打断什么"（quit-inspection 式能力询问）。**不引入服务端在场表**。
3. **连接级 lease（若产品确需"客户端不在就该停"）**：用 SSE/流上的 **idle 超时**做 liveness（先例：Codex 的 `stream_idle_timeout_ms`、有界重试）。这是**连接级**租约，不是**按 Task 的 presence registry**；租约到期最多标 `run/paused(reason=client_absent)` 并 reconcile 在途 Tool，且**run 终态必须由持久化状态机决定，不能由连接层决定**（Codex #50118/#43182 即连接层决定终态的反例）。
4. **不确定性 fallback 方向**：DSH 的做法是"判不准→按有任务在跑"（偏 busy）；票面当前是"grace 不发新模型"（偏暂停）。**两个方向只能选一个**，且一旦选"偏暂停"就必须能**可靠区分明确退出与意外断线**——实践上很难（DSH 用 UI 绕开、Claude Code/Codex 干脆不区分）。建议：要区分时**不确定必须偏向"继续跑"**。

### 4.4 推荐选项（等用户拍板，不做设计、不施工）

- **选项 A（推荐）**：保留成立的子命题（§4.2 上半：断线≠失败、NEED_RECONCILE、Ledger 收口、W-22 闸门），**DEFER 整套 presence 登记表+宽限+最后离开暂停**；客户端离开默认继续跑，靠持久化+重连 replay + 显式 resume 自愈。若后续真实用户反馈"客户端不在还在烧 token"是痛点，再上选项 C。
- **选项 B（折中）**：保留成立子命题 + 采用 **DSH 式软/硬信号拆分**：明确退出信号→按 02 §5.2.1 走 `run/paused(client_absent)` 收口；无信号的断线→连接级重试+继续跑。不建服务端在场表。
- **选项 C（收窄版原票面）**：若产品坚持"客户端不在就该停"，只做**连接级 lease/idle 超时**（Codex 式），到期标 `run/paused(client_absent)`；**不做**按 Task 四字段在场表。
- **反面清单（无论哪个选项都不做）**：不要补写"合成收尾事件"却不推进序号（DSH 曾有 seq 对齐 bug 的教训）；不要让连接层决定 run 终态（Codex 歧义 bug）；不要为未登记 run 改变旧语义（`11 §6.2`，`ClientPresenceGate` 的防御性 no-op 已守住）。

## 5. 审计红线（调研结论确认：以下施工硬约束成立，无一被成熟实践推翻）

1. 与 W-10 队首在场条件用显式接口依赖：复用 `TaskPresenceReader` 只读 seam，**不反向调用 lease**（避免 presence↔lease 循环）。——Pi 的单向依赖纪律与 DSH 的 quit-inspection（单向查询）同向支持。
2. 定义"无需服务 Task"是否含 durably paused（票面审计节要求；设计阶段必须回答）。
3. 退出后**等待 Ledger 收口而非强杀**；30 秒 grace（如保留）不发新模型；重连不自动续跑；不重写 #305 的通用 pause 状态机。
4. 票面"不做"：预算、stuck、CAS、relay cleanup 的二次实现。——各家 abort/暂停语义均不碰预算与 CAS，支持该边界。
5. W-22 闸门的三态语义（未登记→恒不缺席、缺席单向、恢复是新执行段）与 `11 §6.2`"协议只接管明确登记的 Task"一致，调研未发现冲突。

## 6. 证据局限与待补核实

1. CodeBuddy 沙箱拒绝了 `Write`/`Bash`/`WebFetch`（权限拒绝，非临时故障）：调研基于权威页面的搜索索引摘要 + 官方设计说明/提交记录，`quit-confirmation.ts` 等源码**未逐行 raw fetch**，`file:line` 级引用缺失——本报告已在 §2.4 如实标注。第二阶段若需逐行核对，用可联网沙箱补做。
2. 我独立复核了两处关键引用：DSH quit-confirmation + quit-inspection IPC 存在（官方 README 原文与两提交）；Codex #50118/#43182 存在且状态与引用一致。Python asyncio shield 为官方文档一手常识，未逐字复核。
3. Pi 的 `packages/agent/AGENTS.md` 不变量引文来自第三方镜像转录，判定时按"语义参照"处理，未作为硬证据。
4. 本报告未引入任何新依赖、无实质代码复制；`quit-confirmation.ts`（MIT）的 ADAPT 判定若在第二阶段实质借鉴，需保留来源并遵守 MIT 声明要求。

## 7. 待用户拍板

1. §4.4 的选项 A/B/C 三选一（推荐 A）。
2. 若选 B 或 C："不确定性 fallback 偏 busy 还是偏暂停"（§4.3 第 4 条）需要明确。
3. §5 第 2 条："无需服务 Task"是否含 durably paused（设计阶段必须回答）。
4. 第二阶段（TDD 实现）是否开工、按哪个选项开工——由用户决定，本阶段到此为止。
