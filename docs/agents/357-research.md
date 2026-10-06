# W-13（#357）成熟产品调研报告

- **分支**：`codebuddy/357-w13-recovery-reconcile`（基线 `e165d5ec`，干净检出）
- **日期**：2026-10-06
- **范围声明**：本阶段只做成熟产品调研 + 票面成立性判断（SDD §6.1 / §1.3），不写产品代码、不做设计、不施工。本阶段落盘文件：本报告 + `docs/agents/356-design.md`/`356-research.md` 的复核引用（只读）。未触碰 `src/`、`tests/`、`web/`。
- **驱动**：CodeBuddy CLI 无头模式（包装脚本默认 `deepseek-v4.1-flash`）做竞品调研；本仓现状核实由我独立完成（见 §6）。

## 1. 启动检查表复核（五项，任一项不成立即停——全部成立）

| 前置项 | 实际读取依据 | 状态 |
|---|---|---|
| Vision 相关原则 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md` §2.3 Recoverable（恢复≠重跑、UNKNOWN→人工决策） | READY |
| 当前任务规格 | `goal/.../docs/spec/07_STORAGE_PERSISTENCE_RECOVERY.md` §9（Resume 8 步顺序）§10（真实 Kill 测试五种形状）；`02_AGENT_RUNTIME.md` §5.2.1（2026-10-06 按 #356 选项 B 修订：明确退出→paused(client_absent)，无信号断线→继续跑）；`gh issue view 357` 完整票面（含 2026-10-03 审计节） | READY |
| Reuse 相关判定 | `goal/.../docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md` 相关章节；本报告按 SDD §1.3 落方案依据块 | READY |
| Phase 依据 | `docs/PHASE_STATUS.md` W 系列批次；`gh issue view` 核实 W-06 #350、W-12 #356、#337、#342 均为 CLOSED；`docs/agents/356-design.md`（454 行）+ `docs/agents/356-research.md`（129 行）已读 | READY |
| 本任务触发细则 | `AGENTS.md` 全文（543 行）、`docs/SDD_WORKFLOW_PROTOCOL.md` 全文（895 行，首行至 EOF 分段读完）、`docs/agents/reference-sources.md` 对应领域清单、`docs/agents/skills/PROVENANCE.md` §4.2（只取方法论，跳过 `.cursor/`、`agent-transcripts/`、`scripts/log.sh` 等悬空引用） | READY |

## 2. 方案依据（SDD §1.3：来源 / 机制摘要 / 契合点 / 判定 / License）

### 2.1 RabbitMQ 确认机制（必查，PORT DESIGN，不引入 RabbitMQ）

- **来源**：官方文档 <https://www.rabbitmq.com/docs/confirms>（读取 2026-10-06）+ 独立技术博客「RabbitMQ 可靠投递」<https://zhiwenliang.github.io/learning/rabbitmq/02-reliability.html>（二手，个人解读）。
- **机制摘要**：发布确认（broker→publisher）与消费确认（consumer→broker）**方向相反、完全正交**（官方原文 *"entirely orthogonal and unaware of each other"*）。发布确认只确认**保管**（quorum 队列 = 多数副本接受并持久化），完全不涉及消费者是否处理；消费确认确认的是「已处理、可删除」（手动 ack 把删除推迟到业务处理完成）；连接/进程失败→未 ack 消息自动 requeue，**消费者必须幂等**。二手博客补一刀：classic 队列 confirm 前不保证 fsync，confirm 是对账工具、不是持久化级别本身。
- **契合点**：兼容并强化不变量 #14（UNKNOWN 不盲重跑）。RabbitMQ 用「两段正交、互不替代」表达「确认 ≠ 副作用完成证明」：确认是某一跳的收据，副作用证据是另一端的独立事实。本仓映射：用户裁决 `CONFIRM_SUCCESS` = 当事方断言（类 consumer ack）；Ledger 终态 + Tool-specific probe = 外部证据（类 persisted/processed）。二者都必需、互不替代。无冲突：不引入 RabbitMQ（守 #7 单一 Tool 执行路径）。
- **判定**：**PORT DESIGN**（只 Port 概念词汇与边界，不引组件、不抄代码）。
- **License**：文档，未借代码。

### 2.2 Pi Durable（earendil-works/pi，`packages/durable`）

- **来源**：官方 SDK 文档 <https://pi.dev/docs/latest/sdk>、Sessions 文档 <https://pi.dev/docs/latest/sessions>、官方博客 <https://earendil.com/posts/pi-durable/>、仓库 `packages/durable`（`package.json` Release **v1.0.3**，2026-10-04/05 文件；commit SHA 未取，若 Port 需按引用纪律补 `rev-parse HEAD`）+ 独立分析 Tony Bai《Pi 1.0…杀不死的 Harness》<https://tonybai.com/2026/10/03/pi-1-0-durable-agent-harness-release>（二手，转述官方 + 独立点评）。
- **机制摘要**：每步一个 checkpoint（官博逐字：*"every step of a run is a task that stores a checkpoint before it moves on. If the process dies, a new process opens the same storage, finds the unfinished tasks, and continues each one from its last checkpoint."*）；中断的工具调用按「重放安全声明」分流——`replay: "safe"`（只读）才重跑，无声明（如 `deploy`）**绝不自动重跑**，只告知模型「被打断」由模型决定；`requestId` 恰好一次提交；approval hook 答案存进 **memo**（*"a small value stored with the task, where the first write wins"*，重启后不再重复问）。
- **契合点**：兼容不变量 #14 / Vision §2.3（`replay: "safe"` ≈ 只读可安全重放判据；无声明即不重跑 ≈ UNKNOWN 高风险不盲重跑）。**关键分歧**：Pi 把不可重放的中断调用决策权交给**模型**，本仓把 UNKNOWN 高风险交给**人工裁决**（Vision §2.3、07 §7 `NEED_RECONCILE → 用户处理`）——照搬 Pi 的「让模型决定」会违反 #14 精神。memo「first write wins」兼容 `RecoveryAdjudicationToken` 的 CAS / 先写入者胜，可印证「裁决需耐久留痕、重启不重复问」。无「先查外部事实再让人裁决」的一等原语（Pi 对不可重放调用是交给模型决定，不是交给用户）。
- **判定**：**PORT DESIGN**（Port「重放安全声明 + memo 先写者胜 + 恰好一次提交键」三个机制）；**人工裁决主体与 Tool-specific probe = BUILD**（Pi 没有，须自建）。
- **License**：MIT（raw `LICENSE` 逐字核实，Copyright 2025 Mario Zechner）。

### 2.3 Claude Code（恢复 UX）

- **来源**：官方 <https://code.claude.com/docs/en/sessions>（引用了 v2.1.281 / v2.1.285 版本分界）、官方 docs 索引 `llms.txt`、官方 `agents.md` + 独立来源 DeepWiki（对逆向工程仓库的 AI 摘要，二手，未读其上游 `sessionStorage.ts` 原文）、中文学习站（个人学习站，二手）。**未读到**：`CHANGJianshuo/claude-code-analysis` → 404；`/bg` 独立文档页在官方索引中不存在（`agents.md` 全文不含 `/bg`，只有 `claude agents` 与 `/tasks`）。
- **机制摘要**：transcript 以 JSONL 存 `~/.claude/projects/<project>/<session-id>.jsonl`，持续写入（100ms 批量 flush）；**恢复 UX「先列后续」**：`claude --continue`（最近一个）、`claude --resume`（**交互式 picker**，每行显示名称/AI 标题/摘要/首 prompt/距今时间/git 分支/文件大小）、`/resume`——**这是 W-13(a) 的直接先例**。中断工具调用（官文逐字、最关键）：*"A tool that was still running when the previous process ended... doesn't finish or run again when you resume. Claude sees the call marked as cut off before its result was recorded and is told to **check whether it took effect before running it again**."* 后台遗留工作只以 note 形式出现，不自动起 turn。`claude agents`/`/tasks` 提供状态一览 + attach 形态。
- **契合点**：W-13(a) 强先例（resume picker「列出上次状态再选」≈ W-13「先列 Task/Run/cwd/进度文件版本再出现继续」）。W-13(b) 部分先例（「先查是否 took effect 再重跑」≈「先查外部事实」），**分歧**：Claude 让**模型**去查并决定，W-13 要「Tool-specific probe 查事实 → 仍未知才**用户裁决**」——只能取其模式，主体与 probe 归本仓。**冲突（必须显式拒绝）**：Claude JSONL 非类型化、跨版本变动、metadata 与消息混写——违 #3/#4（typed append-only SessionEvent、Event ≠ 诊断日志），绝不能照搬存储格式，只能借 UX。
- **判定**：**ADAPT**（借「先列后继续」picker 形态 + 「先查是否 took effect 再重跑」模式；拒绝其存储格式）。
- **License**：Anthropic 专有（仅行为参照，不可复制）。

### 2.4 Cline（任务恢复）

- **来源**：官方 <https://docs.cline.bot/core-workflows/task-management>、官方 <https://docs.cline.bot/core-workflows/checkpoints> + 独立来源：GitHub issue #4359（任务历史丢失，二手）、第三方 fork `cny123222/cline` 的 `ui.proto`（`ClineApiReqCancelReason` 枚举，**未与上游核实，不保证与上游一致**）、上游 LICENSE（Apache-2.0，一手核实）。**未读到**：上游 `abortTask`/`resumeTaskFromHistory` TypeScript 源码。
- **机制摘要**：Task 自包含（唯一 id + 独立存储目录），History 视图列出 initial prompt/timestamp/token usage；shadow Git 仓库做文件 checkpoint（每次 tool use 后提交），恢复三选项 Restore Files / Task Only / Both——**注意语义**：checkpoint 只是文件快照，与对话分离，**不涉外部副作用**；恢复流程：打开历史任务 → 载入对话 → 文件状态对 checkpoint → 用户补上下文。中断原因类型化（fork proto：`STREAMING_FAILED` / `USER_CANCELLED` / `RETRIES_EXHAUSTED`）。issue #4359 的教训：JSON 分片 + 裸 `JSON.parse()` = resume 脆弱（已修 taskHistory 重建 + 原子写）。
- **契合点**：兼容不变量 #12（Checkpoint ≠ 副作用恢复）——Cline 的 shadow-git 恰恰是「文件快照 ≠ 副作用证据」的活例，可作**反面对照**。Cline 无 Operation Ledger、无 reconcile，证明本仓 Ledger+reconcile 是**超集能力**，属 BUILD。#4359 教训支持本仓设计：恢复 UX 必须优雅降级、不删数据。
- **判定**：**ADAPT**（借 History 列表 resume + 中断原因分类的呈现思路）；**DEFER** shadow-git 文件 checkpoint；**拒绝**其 JSON 分片持久化。
- **License**：Apache-2.0（Copyright 2026 Cline Bot Inc.）。

## 3. 核心裁决：票面 framing 是否成立

| 子命题 | 裁决 | 依据与分歧 |
|---|---|---|
| (a) 恢复列表 UX：先列上次运行的 Task/Run/工作目录/进度文件版本，再出现「继续」 | **成立（强）** | 三个独立产品收敛：Claude `--resume` picker（名称/摘要/时间/分支/大小）、Pi session picker（搜索/改名/删除）、Cline History（prompt/时间/用量）。先列后继续是成熟产品共识。 |
| (b) UNKNOWN 先查外部事实，仍未知才让用户裁决 | **部分成立（模式成立，主体分歧）** | 「先查是否 took effect 再重跑」有 Claude 一手明文；Pi 用 `replay: safe` 声明分级；RabbitMQ 用「确认≠端到端」提供词汇。但 Claude/Pi 的裁决主体是**模型**，且都没有 Tool-specific 外部 probe 一等原语。W-13 的「仍未知才交用户」是本仓 #14/Vision §2.3 的加强，不是竞品已有行为——成立但须标注为**扩展**而非复制。 |
| (c) 用户判「已成功」留裁决 + 来源，不伪造自动验证 | **部分成立（有先例，无完整形态）** | Pi 的 memo「first write wins」直接支持「留痕」；RabbitMQ 两段正交支持「断言与外部证据必须分开」。但没有任何竞品提供「用户判已成功 + 来源」作为一等 reconcile 产物——与 #14 一致，属本仓**自建**。 |

**统一裁决**：成熟产品证据**支持 W-13 恢复页的 framing**（未发现任何反证）。但支撑强度不均：(a) 可直接对标（ADAPT）；(b)「先查外部事实」有先例，但「未知后交人还是交模型」是分水岭——本仓选择交人，比竞品更严，方向与 #14 一致；(c) 只有「耐久留痕」先例，「裁决 + 来源」需自建，不得声称竞品已验证。

**反向风险（照搬竞品会违反的不变量）**：① 照搬 Claude 非类型化 JSONL → 违 #3/#4；② 照搬 Pi「让模型决定高危不可重放调用」→ 违 #14 精神；③ 照搬 Cline 文件 checkpoint 当副作用恢复 → 违 #12；④ 恢复列表在客户端维护 pending_decisions 缓存 → 违 #22（UI 不得维护第二套不可对账真相）——恢复页必须由后端 `/api/sessions` + `recover` 的 `pending_decisions` 驱动。

## 4. 判定汇总

| 来源 | 借用内容 | 判定 |
|---|---|---|
| RabbitMQ | 确认边界词汇（confirm=托管≠端到端；两段正交） | PORT DESIGN |
| Pi Durable | 重放安全声明、memo 先写者胜、requestId 恰好一次 | PORT DESIGN；人工裁决主体与 probe = BUILD |
| Claude Code | resume picker「先列后继续」+「先查是否 took effect 再重跑」 | ADAPT（拒绝其存储格式） |
| Cline | History 列表 resume + 中断原因分类 | ADAPT；DEFER 文件 checkpoint；拒绝 JSON 分片持久化 |

**跨来源汇总**：没有任何竞品具备「Operation Ledger + 外部副作用 reconcile + 人工裁决」的完整形态——核心 reconcile 逻辑属 BUILD，竞品只提供 UX 形态（ADAPT）与机制词汇（PORT DESIGN）。

## 5. 本仓现状核实（独立核实，非竞品调研）

本节是我对当前基线（`e165d5ec`）的独立核实结论，直接决定 §7 的选项：

1. **#547 的后端裁决合同已存在且已合入**（2026-10-03，PR #585，merge `6c9ca07e`，是我分支 HEAD 的祖先）：
   - `POST /api/sessions/{session_id}/recover`（`src/agent_harness/web/app.py:2678`）接受 `RecoverRequest.decisions=[{tool_call_id, verdict}]`，verdict 复用 `ReconcileVerdict` 四值；
   - `SessionService.recover(session_id, decisions=...)`（`src/agent_harness/session/service.py:3392`）：开工前预检（非法值/重复提交/目标非待裁决 → 422；待裁决未覆盖全 → 409 附 `pending_decisions` 清单；被拒请求零写入）→ `DecisionsReconcileCallback` → 协调器既有 token-CAS 提交链（`reconcile_meta` 记裁决原文 + `operation/reconciled` 审计事件）；
   - RETRY = 原调用 CANCELLED 终止 + retryable 结果由模型重发起，服务端永不盲跑（不变量 #14）。
2. **前端现状**：`web/src/lib/api.ts::recoverSession` 不发送 decisions；409 时解析 `pendingDecisions` 但 `useSession.ts:432` 明文「本期只展示原因，不做裁决」——App 显示「需人工裁决」但**无裁决提交 UI**。
3. **启动扫描结果未暴露给前端**：`scan_interrupted()` 在 web lifespan 跑，结果只进日志；无「列出上次运行 Task/Run/工作目录/进度文件版本」的端点或页面。
4. `SessionService.recover` docstring 明文：「外部事实核查由 UI 引导（#357 W-13 恢复页复用同一合同）」——#547 关单时用户已拍板「页面 UX 归 #357 W-13」。

## 6. 引用核实状态（如实标注）

**一手/逐字读到**：R1-a（RabbitMQ confirms 官文）、R2-a/b/c/d（Pi 官文/官博/仓库 README+package.json v1.0.3+MIT LICENSE）、R3-a/b/c（Claude 官文/索引/agents.md）、R4-a/b/e（Cline 官文 + Apache-2.0 LICENSE）。
**二手/摘要**：R1-b（个人博客）、R2-e（Tony Bai 转述）、R3-d（DeepWiki AI 摘要，未读上游 `sessionStorage.ts` 原文）、R3-e（个人学习站）、R4-c（issue 讨论）、R4-d（**第三方 fork proto，未与上游核实**）。
**未读到**：`CHANGJianshuo/claude-code-analysis`（404）；Claude `/bg` 独立文档（官方索引无）；Cline 上游 `abortTask`/`resumeTaskFromHistory` 源码；Pi Durable 的 commit SHA（未取）。
**未实质复制/Port 任何第三方代码**（本阶段零代码改动）。

## 7. 待用户拍板（§7 的选项见子代理最终汇报）

## 8. UNKNOWN 深挖：成熟产品到底怎么处理"不确定副作用"（第二轮，2026-10-06）

**范围声明**：本节只做调研，不写产品代码、不做设计、不施工、不关单。直接回答用户 2026-10-06 的质疑："要是交人去处理，如果用户是个小白，那么咋处理？优先去参考成熟产品的做法和设计。"——"交人裁决"隐含假设这个人答得上来，但"这个工具调用到底生没生效"这种题小白根本答不上来。

**方法**：对每家成熟产品回答四个必答问题（① UNKNOWN 从哪来 ② 用什么机制让 UNKNOWN 少发生 ③ 真发生时默认动作是什么 ④ 摆到用户面前的问题原话是什么、小白答得上来吗、还是根本不问人）；答不上来写"未找到"，不编造。驱动：CodeBuddy 无头 `-p`（deepseek-v4.1-flash 优先，失败自动降级）抓取一手资料；关键断言我用本地浅克隆（`/tmp/pi-src`、`/tmp/cline-src`、`/tmp/dsh-src`，commit 见各节）与独立搜索二次复核。SDD §1.3 要求"版本与 License 必须实际核实"——每家方案依据块均含逐字核实结果；文档页无版本号的产品如实标注替代证据。

**口径**：本节 UNKNOWN = "副作用可能已经发生，但'已完成'这一事实尚未持久化"的窗口。

### 8.0 必查名单覆盖情况

| 产品 | 覆盖 | 证据等级 |
|---|---|---|
| Temporal.io | ✅ | 官方文档逐字引用 + LICENSE 逐仓核实 + 独立第二来源 |
| Pi Durable | ✅（第一轮已查，本轮下到源码级） | 仓库源码 file:line + commit SHA + 测试断言 + LICENSE 逐字 |
| Claude Code | ✅ | 官方文档十余页逐字核查 + 原话引用 |
| Cline | ✅ | 仓库源码 file:line + commit SHA（纠正了文档与实现不符处）+ LICENSE 逐字 |
| Codex #50118 / #43182 | ✅ | `gh issue view --comments` 全文 + 事件流 + LICENSE/NOTICE |
| DeepSeek Harness | ✅ | 仓库源码 file:line + commit SHA + locale 原文 + LICENSE |
| Inngest / Trigger.dev / LangGraph | ✅（视情况加） | 官方文档逐字引用 + LICENSE 文件核实 |

### 8.1 Temporal.io（durable execution 标杆）

**四问**：
1. **UNKNOWN 从哪来**：Activity worker 在函数被调用后崩溃、任务投递途中丢失、Heartbeat 超时、Start-To-Close 超时（*"The main use case for the Start-To-Close timeout is to detect when a Worker crashes after it has started executing an Activity Task."*）；最典型的是"函数成功了、但 worker 在上报 Server 前崩溃"（*"the Event History won't reflect the successful completion of the Task, so the Activity will be retried."*）。关键洞察：Server **不直接检测**任务丢失——*"Temporal doesn't detect task loss directly. It relies on Start-To-Close timeout."*（UNKNOWN 不是被"发现"的，是被超时**兜底判定**的。）
2. **减少机制**：默认 at-least-once（指数退避无限重试）；观测 exactly-once、执行可能多次（*"the Activity may be executed multiple times and may even partially complete more than once"*）；Activity ID 唯一性约束（但文档明说这**不等于**幂等：*"This is a different problem from idempotency, and you need both."*）；heartbeat details 供下次 attempt 续跑（*"so your code can continue processing where it left off"*，注意重试本身是从头跑）；**幂等是应用的责任**（*"You should always make your business logic Activities idempotent in Temporal."*），平台只提供幂等键素材（Workflow Run ID + Activity ID 组合）。
3. **默认动作**：按 Retry Policy **自动重试**（默认 Initial Interval 1s、backoff 2.0、Maximum Attempts = ∞），*"require no action on your part"*；**不阻塞、不问人**；只有重试耗尽/不可重试/被取消，才把 ActivityFailure 抛回 Workflow 代码。**没有任何"阻塞等人工"的默认路径**（未找到）。
4. **摆给用户什么**：Workflow **开发者**看到 ActivityFailure/TimeoutFailure 异常（含 cause + 最后一次 heartbeat details）与 Event History（`ActivityTaskTimedOut` / `ActivityTaskFailed`）。**终端用户：未找到**——文档无任何面向终端用户的 UI/通知约定。**它不问人**，全靠"重试 + 幂等"；唯一的"等人"是开发者**自建**的异步完成/信号模式，非平台默认行为。

**方案依据块（SDD §1.3）**：
- **来源**：`docs.temporal.io` 官方文档（activities / activity-execution / activity-definition / encyclopedia/detecting-activity-failures / retry-policies / references/failures+events，一手，2026-10-06 抓取，关键原话经独立搜索二次印证）+ 独立第二来源（`jwcarman/nessy` 的 inbox-outbox 设计笔记：*"Temporal does not solve it either — it declares it the activity's problem"*）。
- **机制摘要**：超时兜底判定（Start-To-Close/Heartbeat）→ 默认自动重试（at-least-once）→ heartbeat details 续跑 → 幂等责任明确推给应用 → 重试耗尽才抛异常给 Workflow 代码。
- **契合点**：兼容并细化不变量 #14。Temporal 的"默认重试"建立在"应用已保证幂等"这一**前置契约**上；本仓的高风险工具恰是"未声明幂等/重放安全"的，故**不能照搬默认重试**，只能 Port 其"幂等声明前置 + observed exactly-once vs executed multiple times"的责任划分与词汇。Ledger 终态语义可借后者表达。
- **判定**：**PORT DESIGN**（Port 责任划分与概念词汇；默认无限重试不适用本仓高风险工具）。
- **License**：**MIT**（Server 与 sdk-python/go/typescript 逐仓读 LICENSE 原文核实；Server 版权 `Copyright (c) 2025 Temporal Technologies Inc.` + `Copyright (c) 2020 Uber Technologies, Inc.`）。
- **版本**：文档页**无版本号、无 last updated**（逐页核实页脚，仅 "Feedback"）；替代证据：文档源 `temporalio/documentation` 对应 `.mdx` 最近提交 2026-09-04～2026-10-05。

### 8.2 Pi Durable（effect sandwich + replay safe；第一轮已查，本轮下到源码）

**四问**：
1. **UNKNOWN 从哪来**：两类，终态不同。**A. 用户/宿主主动 abort**：提交 `abortRequested` → join 运行中 invocation → 启动全新 abort invocation → abort handler **必须**提交一个终态（`types.ts:244-245` *"must commit a terminal outcome"*；没提交就被判 faulted）。**B. 进程被杀/close（崩溃）**：`close()` 不写终态，任务停在**意图阶段**（工具 intent 已提交、结果未提交）——这正是"上一步到底写没写库"的窗口。博客原话：*"If the process dies, a new process opens the same storage, finds the unfinished tasks, and continues each one from its last checkpoint."*
2. **减少机制**：① **effect sandwich** 三阶段 checkpoint（`spec.md:1849-1860`）：提交意图 → 执行外部副作用 → 提交结果；工具调用在执行副作用的边界上打点（`tool.ts:35-38`，`{ phase: "execute", arguments, replay }`）。② **`replay: "safe"` 默认 `unsafe`**（`harness/types.ts:214`），恢复时**存储的策略与当前注册的策略必须双 safe 才重跑**（`tool.ts:93-105`，防注册表改动放宽策略）；内置 read/write/edit/bash 全未声明 → 全默认 unsafe。③ **requestId 恰好一次提交**（`submissions.ts:155-162`，命中则返回已有 submission id *"without writing"*）。④ **approval memo first-write-wins**（`types.ts:203-206`、`scheduler.ts:1106-1108`，重启不重复问）。
3. **默认动作**：恢复分支二选一（`tool.ts:94-111`）——双 safe 则**重跑**；否则**不重放、不跳过**，直接判中断，写给模型：`` `Tool ${call.name} was interrupted and may have partially run` ``（`tool.ts:107`，已用本地克隆逐字复核）。框架**不做外部事实核查**；规范把三种标准做法交给工具作者：*"The phase handler retries safely, polls an external handle, or records interruption."*（`spec.md:1857-1858`）。模型请求被切断则直接重发，残留部分回答标 `stopReason: "aborted"` 留在 transcript。
4. **摆给用户什么**：**基本不问用户**。博客原话：*"After a crash, a tool reruns only if it says that is safe. **Otherwise the model is told the call was interrupted, with the output stored so far, and decides what to do.**"*——决策主体是 **model**，不是终端用户。中断/恢复路径没有可点选项，也没有面向小白的开放技术问题；用户唯一需要做的是显式 abort 这个动作本身（TUI：`── ⠧ Working... (esc to abort) ──`）。

**方案依据块（SDD §1.3）**：
- **来源**：`earendil-works/pi` 仓库源码（浅克隆 `/tmp/pi-src`，commit `428a12bc775145afa342530a9eaa652efb3e4422`，2026-10-06；`packages/durable/src/harness/{tool,generation,scheduler,submissions,types}.ts`、`docs/spec.md` 4692 行、`test/harness-tools-recovery.test.ts`，file:line 见 §8.2 四问）+ 官方博客 `earendil.com/posts/pi-durable/`（2026-10-01）。
- **机制摘要**：意图/结果两阶段 checkpoint（effect sandwich）+ replay safe 双 safe 才重跑 + requestId 恰好一次 + memo first-write-wins + abort terminal-once（abort handler 只跑一次且必须提交终态）。
- **契合点**：`replay: "safe"` ≈ 本仓"只读可安全重放"判据的形式化前身，可直接抄其"声明式重放安全 + 双 safe 才重跑"的形状；memo first-write-wins 印证 `RecoveryAdjudicationToken` 的 CAS/先写者胜（第一轮已 Port）。**分歧（重申）**：Pi 把不可重放中断的决策权交给**模型**，本仓按 #14/Vision §2.3 交给**人工**——"may have partially run 如实告知决策者"可抄，决策主体不抄。
- **判定**：**PORT DESIGN**（第一轮结论维持；本轮新增 abort terminal-once 语义与"如实告知"文案可抄）。
- **License**：**MIT**（根 LICENSE 逐字：`MIT License` / `Copyright (c) 2025 Mario Zechner`；`packages/durable/package.json` 声明 `"license": "MIT"`）。
- **版本**：`packages/durable` **v1.0.4**（`package.json:3`；monorepo 各包同版本；commit SHA 见来源行）。

### 8.3 Claude Code（cut-off 标记 + 交模型 + resume picker）

**四问**：
1. **UNKNOWN 从哪来**：官方文档**没有任何一处用字面量 `UNKNOWN` 命名中断的工具调用状态**（已核查 sessions/permissions/settings/interactive-mode 等十余页）→ 其等价概念是 *"the call marked as cut off before its result was recorded"*。触发入口：Esc（*"Stop the current response or tool call mid-turn… Claude keeps the work done so far."*）、Ctrl+C、进程退出/崩溃（*"A tool that was still running when the previous process ended, for example in a crash, doesn't finish or run again when you resume."*）、后台任务（恢复后只以 note 形式出现，*"Claude Code doesn't start a turn from those notes"*）。
2. **减少机制**：**持续追加写 JSONL transcript**（*"Sessions are saved continuously to local transcript files as you work"*，`~/.claude/projects/<project>/<session-id>.jsonl`）；具体落盘频率/fsync 语义**未找到**。v2.1.281 起把 cut-off 调用**显式标记**交给模型处置（此前是丢弃或误标为"被你中断"）。
3. **默认动作**：**跳过，不重放**——*"doesn't finish or run again when you resume"*。*"Claude sees the call marked as cut off before its result was recorded and is told to **check whether it took effect before running it again**"*。这个 check **由模型做，不是机器规则**；文档层面**未找到**任何独立于模型的机器外部事实核查。覆盖开关：`CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`（SDK 模式自动续跑）、VS Code 面板"中断 <1 小时且未在他处打开则自动继续该 step"。
4. **摆给用户什么**：**从不向用户提问"那次调用是否已生效"**——那是发给模型的。用户看到的是一个**可执行的选择器（resume picker）**，不是技术问答：每行显示 *"the session name… AI-generated session title, conversation summary, or first prompt, along with **time since last activity, git branch, and file size**"*；`↑/↓` 选、`Enter` 恢复、`Space` 预览。**小白能答**——按时间/分支/标题选即可，无需懂工具调用语义。另有 permission 之外的**硬安全默认**（`permission-modes` 页）：关键路径 `rm`/`rmdir` 断路器（*"Claude Code **never lets** a `permissions.allow` rule… approve… **even in modes that skip other prompts**. This circuit breaker guards against model error."*）、deny 规则全模式生效、沙箱默认无网络、`--dangerously-skip-permissions` 拒绝 root。

**方案依据块（SDD §1.3）**：
- **来源**：`code.claude.com/docs` 官方文档英文版（sessions / interactive-mode / how-claude-code-works / permission-modes / env-vars / vs-code / agent-view 等，一手，2026-10-06 抓取；关键原话逐字引用）。
- **机制摘要**：cut-off 显式标记 + 默认跳过不重放 + "told to check whether it took effect" 交模型 + resume picker 可点选择 + permission 外硬断路器。
- **契合点**："标记 cut-off 而非丢弃/误标"可抄（对应本仓 Ledger 终态必须诚实）；resume picker"先列后继续"是 W-13(a) 的直接形态先例（第一轮已 ADAPT）。**分歧**：Claude 把"是否 took effect"的核查与决策都交给**模型**，且机器无外部核查——本仓按 #14 要"Tool-specific probe 查外部事实 + 人工裁决"，只能取其 UX 形态，不能取其决策主体。
- **判定**：**ADAPT**（借 picker 形态与"诚实标记"语义；拒绝其存储格式与"交模型决策"）。
- **License**：**Anthropic 专有**（仅行为参照，不可复制）。
- **版本**：文档页无独立版本号；页面内联版本标记最高 **v2.1.285 / v2.1.288**，核心行为边界 **v2.1.281**；`llms.txt` Weekly Digest 最新 Week 37（Sep 7–11, 2026）。

### 8.4 Cline（四态 + 默认亮 Resume + Reset Code/Chat）

**四问**：
1. **UNKNOWN 从哪来**：字面量 `UNKNOWN` 作为中断状态**未找到**（全仓唯一 `UNKNOWN` 是会话来源枚举，与中断无关）。中断在数据层是四值：`completed | failed | cancelled | interrupted`（`sdk/packages/core/src/types/session.ts:81`）；`cancelled` 聚合 `aborted|max_iterations|mistake_limit|无 finishReason`（`local-runtime-host.ts:1815-1832`）；`interrupted` **专指 Hub/进程重启打断**（`agenda-task-manager.ts:1199-1209`，`error:"Hub restarted while this task was running."`）。UI 层 `ClineApiReqCancelReason` 有三值（`ExtensionMessage.ts:404`），但本 commit **只有 `user_cancelled` 有生产代码**（`sdk-message-coordinator.ts:127`），`streaming_failed` / `retries_exhausted` 仅有类型声明、**语义未找到**——第一轮引用的"三类 abort 原因"需据此修正。
2. **减少机制**：① **按用户轮粒度的 checkpoint**（**不是**每次工具调用——文档称 "After each tool use" 但代码是每新用户轮一次，`checkpoint-hooks.ts:658-708`）；② 存储**不是独立 shadow git 库**（文档语），实为用户本仓内的 stash 提交 + 私有 ref（`refs/cline/checkpoints/{sessionId}/{runCount}`，`checkpoint-hooks.ts:349-648`），不改用户 HEAD/索引；③ 崩溃运行显式标 `interrupted`；④ **状态不可读时默认亮 Resume**（`sdk-task-control-coordinator.ts:281-285`）：*"When the status is unknown (e.g. a transient read failure), default to the Resume affordance: resuming a completed task is harmless, while hiding Resume on an interrupted one is the data-loss illusion this exists to prevent."*（本地克隆逐字复核）——这是直接消灭"静态未知"的关键设计。
3. **默认动作**：**标记 + 截断，不重放**。落盘前把最后一条无 cost 的 `api_req_started` 标 `user_cancelled`（`sdk-message-coordinator.ts:113-138`）；历史中间的中断轮**故意不重标**（*"persisted transcripts carry no per-turn outcome… retagging it would present an interrupted response as a deliberate turn end"*，`message-translator.ts:2414-2418`）；最终以 **Resume 按钮把"继续/放弃"交还用户**。无自动重发失败请求的逻辑。
4. **摆给用户什么**：文档的三选项菜单（*Restore Files* / *Restore Task Only* / *Restore Files & Task*）在**当前代码中已不存在**（字符串无命中）；当前 UI 是：用户消息进编辑态后两个按钮 **`Reset Code`**（tooltip *"Rewind conversation, reset code edits"*）与 **`Reset Chat`**（tooltip *"Rewind conversation, keep current code edits"*）（`UserMessage.tsx:152,233,225,34`），+ footer **`Resume Task`** / **`Start New Task`**（`buttonConfig.ts:135,143`）。History 行显示：初始 prompt、时间、费用（`$` + 4 位小数）、可展开 tokens/model/size/export（`HistoryViewItem.tsx`）。**小白可点**（都是按钮），但"恢复代码改动"需先点进消息编辑态、文案不如文档直白。

**方案依据块（SDD §1.3）**：
- **来源**：`cline/cline` 仓库源码（浅克隆 `/tmp/cline-src`，commit `cd80a20e96481f5f5d413789f6847accf846487b`，2026-10-06；路径 file:line 见四问）+ `docs.cline.bot`（task-management / checkpoints）。
- **机制摘要**：completed/failed/cancelled/interrupted 四态 + 用户轮粒度 checkpoint（本仓 stash + 私有 ref）+ 状态不可读默认亮 Resume + 中断标记截断不重放 + Resume 交还决定权。
- **契合点**："状态不可读默认给安全侧 affordance"（默认亮 Resume 而非隐藏）是 W-13 恢复页可直接抄的 fail-safe 形态；`interrupted` 专态支持本仓"崩溃 vs 用户取消必须区分"（对应 #356 选项 B 的 DSH 式信号拆分思想）。**拒绝**：JSON 分片持久化（第一轮 #4359 教训维持）；文档"shadow git/每次工具调用/三选项菜单"三处与实现不符，不可引文档语。
- **判定**：**ADAPT**（借四态与"默认亮 Resume"兜底；DEFER 文件 checkpoint）。
- **License**：**Apache-2.0**（LICENSE 逐字：`Copyright 2026 Cline Bot Inc.`）。
- **版本**：扩展 `claude-dev` **4.1.22** / `@cline/core` **0.0.90**（`package.json` 实读）。

### 8.5 Codex（#50118 / #43182：歧义状态最后谁拍板）

**四问**：
1. **UNKNOWN 从哪来**：两个 issue 都是"**正常完成后残留未决状态**"，非模型中断/崩溃直接造成，而是**状态对账/校验契约缺陷**。#50118：线程 `latestTurnStatus=completed` 却 `markedStreaming=true`，发送队列队首 `submission.status = outcome-unknown` + `pausedReason = submission-outcome-unknown`；根因是 JSON 往返丢可选字段 + 整对象相等守卫 + `JSON.stringify(undefined)` 传输出错。#43182：投影游标 `next_rollout_ordinal` 落后期望序号 1（写侧序号复用 + 读侧严格物化器 `expected ordinal N, got N−1` 中止），同一 turn 在三种视图下分别是 `inProgress` / `interrupted` / 磁盘正常。
2. **减少机制**：持久化检查点（`next_rollout_byte_offset` + `next_rollout_ordinal` 单事务 upsert）+ **容忍性物化**（`ordinal < next_ordinal` → 跳过并记 `DuplicateOrRegressedOrdinal` anomaly，`#42369`/`#42378`）+ `client_id` 与已接受历史对账（判断"是否已被接受"而不重发）。
3. **默认动作**：#50118：**扣住不发、不回滚重发**——*"Reconciliation removes it without resending"*；**无自动重试**，用户只能手动重试/重载/降级。#43182（0.153.4）：严格中止、投影停在旧边界；修复后（0.154.0+）：跳过问题记录、推进检查点、记 anomaly、继续投影。**共同默认：不确定时不重发/不重做**。
4. **谁拍的板**：#50118 **至今无人拍板**（OPEN，25 条评论零维护者参与；末条是报告者自己的降级绕过）。#43182 由**人类报告者本人**依经验性恢复验证关闭（*"Closing this report as resolved for my case."*）；机器（社区贡献者）给出代码级判定（哪个 tag 含哪两个 commit），人类给关闭拍板，官方维护者全程缺席。**是不是小白能答的题？不是**——裁决需要读 Rust 源码、SQLite 只读取证、万行 JSONL 定位序号、GitHub commit 祖先核实；小白现实中只能"重启/降级/重试"。

**方案依据块（SDD §1.3）**：
- **来源**：`openai/codex` 的 `gh issue view #50118/#43182 --comments` 全文 + `/events` + LICENSE/NOTICE（一手，2026-10-06）。
- **机制摘要**：outcome-unknown 一等状态 + 对账删除不重发 + 容忍性跳过记 anomaly + client_id 去重。
- **契合点**：#50118 是"UNKNOWN 不是一等状态就没有自动收敛"的活教材——本仓 `NEED_RECONCILE` 必须是一等、可查询、可审计的状态，不能是隐式标记；"不确定时扣住不重发"与 #14（不盲重跑）同向，可作默认动作的佐证。
- **判定**：**PORT DESIGN**（"扣住不重发 + anomaly 记账"的收敛形状）。
- **License**：**Apache-2.0**（LICENSE 逐字：`Copyright 2025 OpenAI`；NOTICE 含 Ratatui MIT 衍生声明）。
- **版本**：#50118（OPEN，无修复版本）/ #43182（0.153.4 受影响，0.154.0-alpha.3+ 含修复 `095ac4f131` + `69cebb5d15`）。

### 8.6 DeepSeek Harness（'unknown' → 保守询问 + Quit/Cancel）

**四问**：
1. **UNKNOWN 从哪来**：小写 `'unknown'`（代码中不是大写标记；大写 `'UNKNOWN'` 是无关错误码）= **"Host 答不出这次退出检查"**，只由三失败合流：Host 不可用 / 检查抛错 / **超过 2 秒未答**（`quit-confirmation.ts:11-16,68-74`，`QUIT_INSPECTION_DEADLINE_MS = 2_000`）。用户退出只是触发检查；更新/安装器/OS 关机路径直接跳过检查，**不产生 UNKNOWN**。
2. **减少机制**：启动即注册的轻量检查器（退出只读 `activeTasks`/`scheduledTasks` 两个布尔值）；2 秒硬超时；与更新重启检查**口径复用**同一函数（`hasDesktopActiveTasks`）；Host 内部出错时**主动回 `activeTasks: true`**（`desktop-host/src/index.ts:70-73`）——把"未知"降级为"保守询问"而非静默退出；Host 未 ready 则直接放行不弹窗。
3. **默认动作**：**一键动作，不是逐条确认**。弹一个原生消息框，两个按钮 `[Quit, Cancel]`，`defaultId: 0`（**默认 = Quit，接受中断**）；UNKNOWN 按"有运行任务"显示，Quit 仍是默认。点 Quit 后不重新检查。
4. **摆给用户什么**：原话（`locale.ts:35,42,44-47`）：标题 *"Quit DeepSeek Harness?"*；*"Running tasks will be interrupted."* / *"Scheduled tasks will not run while the app is closed."*；按钮 **Quit / Cancel**。**小白能答**——它不是让用户读技术清单，而是把"Host 答不上来"**翻译成一句通俗后果 + Yes/No 确认框**。这正是本轮要抄的"翻译方式"。

**方案依据块（SDD §1.3）**：
- **来源**：`deepseek-ai/deepseek-harness` 仓库源码（浅克隆 `/tmp/dsh-src`，commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc`，`release-dsh-0.2.1-alpha.1`，2026-10-03；`apps/desktop-host/src/quit-inspection.ts`、`apps/desktop/src/{quit-confirmation,locale,host-process,main}.ts`、`apps/desktop/README.md:39`）。
- **机制摘要**：退出前检查点（2s 超时）+ Host 答不出→保守按"有任务"处理 + 一键 Quit/Cancel 确认（默认 Quit）。
- **契合点**："未知→保守侧默认值→一句通俗后果+二选一"是小白裁决 UI 的**直接模板**；2s 超时 + 检查器只读布尔事实的轻量形状可作 probe 超时设计的参考。注意其默认是"接受中断"（退出场景），本仓 reconcile 场景的默认必须是 #14 的安全侧（不盲重跑），**默认方向不抄、只抄翻译形态**。
- **判定**：**ADAPT**（借"未知转通俗确认"的翻译形态；默认动作方向按 #14 重定）。
- **License**：**MIT**（LICENSE 逐字：`Copyright (c) 2026 DeepSeek`；`package.json: "license": "MIT"`）。
- **版本**：`0.2.1-alpha.1`（根与 `apps/desktop` 的 `package.json` 实读 + release commit 信息）。

### 8.7 Inngest / Trigger.dev / LangGraph（横向：durable execution 的三种默认姿态）

| 维度 | Inngest | Trigger.dev | LangGraph |
|---|---|---|---|
| UNKNOWN 主因 | step 副作用成功、但结果未记账 → 重试重放（*"Make `refundOrder` idempotent because a refund can succeed before Inngest saves the step result"*） | 执行期崩溃**无 checkpoint** → run 进终态 `Crashed` **不重试**；重试从头跑（*"every API call, database write, or email inside it happens again unless that operation is itself idempotent"*） | 节点失败/崩溃；*"A task that started but did not finish may run again on that resume, so design side effects to be idempotent."* |
| 减少机制 | step checkpoint + memoization（*"Doesn't run again once it succeeds."*）；事件/函数 dedup（24h）；**外部幂等键**（*"Event and function deduplication do not remove that risk."*） | CRIU Checkpoint-Resume（**仅等待/子任务时**）；idempotency key 只去重 trigger（*"it does not make the code inside a task's `run()` function idempotent"*）；结果缓存 | checkpointer（super-step checkpoint + per-task pending writes）；durability modes（`exit`/`async`/`sync`）；RetryPolicy（JS 为 opt-in）；graceful shutdown（drain 只在 super-step 边界） |
| 默认动作 | 失败 step 自动重试（默认 4 次）→ 耗尽抛 `StepError` / `onFailure`；**不是跳过、不是默认问人** | 按 retry 设置重试；`Crashed` **不重试、直接暴露**；`Timed out` 记 failed 且不调生命周期钩子 | **默认不自动重试**（JS opt-in；Python 需配 RetryPolicy）；耗尽后 error_handler 或冒泡；恢复是手动同 `thread_id` 重 invoke |
| 幂等责任 | 应用/外部服务（文档明说） | 应用/provider（文档明说 key 只去重 trigger） | 应用（文档明说） |
| 摆给用户 | 开发者：run trace（steps/waits/retries/errors）+ `onFailure`；终端用户：**未找到**；默认问人：**未找到** | 开发者：dashboard 状态机（含 Crashed）/attempt/trace/replay；终端用户：**未找到**；`wait.forToken` 是显式 HITL 原语，非 UNKNOWN 默认路径 | 开发者：`NodeError`、attempt、checkpoint；终端用户：**未找到**；`interrupt()` 是显式 HITL，*"bypassing both retry policies and error handlers"* |
| License（一手） | **SSPL v1.0 + Apache-2.0 Future License**（`LICENSE.md` 逐字：*"Server Side Public License, Version 1.0… Apache 2.0 Future License"*，Copyright © 2022 Inngest, Inc.）⚠ 非标准宽松许可，实质复制/Port 代码需法务口径，**只借概念** | **Apache-2.0**（根 LICENSE） | **MIT**（根 LICENSE，`Copyright (c) 2024 LangChain, Inc.`） |
| 版本 | 文档无更新日期；TypeScript SDK v4 为准，`StepError` 标注 v3.12.0+ | 文档提及 SDK 4.3.1+/4.5.x（v3/v4.x 现状） | `langgraph>=1.2` / `@langchain/langgraph>=1.4.0`（per-node timeout/error handler 门槛） |

**方案依据块（三家合一，SDD §1.3）**：
- **来源**：三家官方文档（一手，2026-10-06 抓取，URL 见上表行）+ 三家仓库 LICENSE 文件。
- **机制摘要**：step/super-step checkpoint + 幂等责任归应用 + 三种默认姿态（Inngest 自动重试 / Trigger.dev Crashed 不重试直接暴露 / LangGraph 默认不重试）。
- **契合点**：三家一致把"恰好一次副作用"的责任推给应用层幂等——印证本仓"重放安全声明前置"（Pi replay safe / #14）的方向；Trigger.dev 的"Crashed 不重试、直接把 UNKNOWN 摆出来"是"诚实暴露 > 猜测收敛"的先例，支持本仓 NEED_RECONCILE 一等状态。
- **判定**：**PORT DESIGN**（概念层；Inngest 只借概念不碰代码——SSPL）。
- **License**：见上表（Inngest SSPL 特别注意）。

### 8.8 横向结论：七条跨产品收敛（本轮核心交付）

1. **没有一家把"这次调用到底生没生效"做成开放式技术问答摆给小白用户**。Temporal/Inngest/LangGraph/Trigger.dev/Pi/Claude Code/Cline 的默认路径**全都不问人**；唯一问人的 DSH，问的是 *"Running tasks will be interrupted."* + **Quit/Cancel**——一句通俗后果加二选一。第一轮的"仍未知才交人"若落地成开放式问答，**无任何成熟产品先例**，是反模式。
2. **默认动作恒为"安全侧"，且"不猜"**：Temporal=自动重试（但前提是应用已声明幂等）；Pi/Claude=不重放、如实告知（*"may have partially run"* / *"marked as cut off"*）；Cline=标记截断+Resume；Codex=扣住不重发（*"Reconciliation removes it without resending"*）；Trigger.dev Crashed=不重试直接暴露。**没有任何一家在 UNKNOWN 上默认"盲重跑高风险操作"**——这正是 #14 的跨产品印证。
3. **"先查外部事实"的机器核查，只有文件/索引级，没有通用副作用 probe**：Cline 查文件对 checkpoint（≠副作用）；Claude 的 *"check whether it took effect"* 是**模型**去查；Pi 是工具作者自己写重建逻辑（`scanConversations` + `requestId`）。**没有任何竞品提供 Tool-specific 外部副作用 probe 的一等原语**——本轮确认第一轮 §3(b) 的判断：probe 属本仓 BUILD，且是超集能力。
4. **"让 UNKNOWN 少发生"的标准答案是幂等/重放安全声明前置**：Temporal（应用责任+幂等键素材）、Pi（`replay:"safe"` 默认 unsafe、双 safe 才重跑）、Inngest/LangGraph/Trigger.dev（文档明说应用负责）。**声明在工具作者，检查在平台**——本仓 Tool Contract 加 `replay_safe` 声明位有五家以上的先例支撑。
5. **UNKNOWN 必须是一等状态，否则没有自动收敛**：Codex #50118 的 `outcome-unknown` 无收敛逻辑 → 卡死至今 OPEN；#43182 的严格物化器 → 必须靠发版修复。映射到本仓：`NEED_RECONCILE` 必须是一等、可查询、可审计、可超时的状态，不能是隐式标记或日志里的一行字。
6. **小白能点的 UI 永远是选择题/确认框**：DSH（后果+Quit/Cancel）、Claude resume picker（时间/分支/标题四要素选会话）、Cline（Resume Task / Reset Code / Reset Chat 按钮）。**翻译公式 = 技术状态 → 一句通俗后果 → 2~3 个可点动作 → 默认选中最安全项**。
7. **审计留痕是共识**：Pi memo first-write-wins、Codex anomaly 记账（`DuplicateOrRegressedOrdinal`）、Temporal Event History（`ActivityTaskTimedOut` 带 timeout 类型）、Cline 故意不重标历史轮（防伪造）。对应本仓：裁决原文 + 来源必须进 `reconcile_meta`（#547 合同已有），**不伪造自动验证**（第一轮 §3(c) 维持）。

## 9. 修订版推荐（替代第一轮 A/B/C）

**说明**：第一轮的 A/B/C 选项只存在于子代理最终回报中，**未落文档**；本节为 supersede 修订版，直接回答"小白用户场景下裁决 UI 应该长什么样"。

### 9.1 修订 A（推荐）：默认安全动作 + 可行动选择题 + 审计留痕

"交人"保留，但交的**不是开放式技术问答**，而是一张"后果语言 + 三选一按钮"的卡片：
- **默认动作先行**：高风险（未声明 `replay_safe`）UNKNOWN 的默认 = **跳过/稍后**（#14；Codex"扣住不重发"、Trigger.dev"Crashed 不重试"为先例）；低风险（已声明 safe）默认 = 安全重试（Pi 双 safe 形状）。
- **问人的形态**：见 §9.3 的 UI 设计——可行动的选择题，默认选中最安全项；用户改选即一次显式裁决，原文 + 来源写入 `reconcile_meta`（#547 合同已支持）。
- **证据依据**：§8.8 第 1、2、6 条（七家产品无一家用开放式问答；默认恒为安全侧；小白 UI 恒为选择题）。

### 9.2 修订 B（备选，需用户明确降级）：交模型决定

照抄 Pi/Claude 做法：如实告知模型 *"was interrupted and may have partially run"*，由模型查外部事实并决定。与不变量 **#14**（UNKNOWN 高风险→人工决策）及 Vision §2.3 冲突；仅在用户明确接受"模型代答"时可用，且必须保留审计（模型给出的"已生效/未生效"结论同样记来源）。**默认不选**。

### 9.3 修订 C（逃生舱，不作默认）：纯人工开放式裁决

#547 的 `decisions` 合同保留；UI 上收进"高级/详情"而不作为默认路径。用于审计、客服、事后复核场景。

### 9.4 小白用户场景下，裁决 UI 应该长什么样

按 §8.8 第 6 条的翻译公式（技术状态 → 一句通俗后果 → 2~3 个可点动作 → 默认选中最安全项），抄 DSH 的形态、按 #14 重定默认方向：

1. **标题用后果语言，不用技术黑话**。抄 DSH 的 *"Running tasks will be interrupted."* → 我们的："有 N 个操作的结果不确定——不知道它们到底生没生效。"**绝不出现**"这个工具调用到底生没生效？"这种开放式技术问答（§8.5：Codex issue 证明小白答不上来）。
2. **每个 UNKNOWN op 一张卡片**：工具名（人类可读）、参数摘要、最后已知状态、probe 查到的外部事实清单（"已查到 / 未查到"，**不伪造**——第一轮 §3(c) 维持）。
3. **每张卡三个单选按钮，默认选中最安全项**：
   - 「当作已生效，继续」（= `CONFIRM_SUCCESS`；须留来源——来源给可选项如"我刚才亲眼看到结果了/我查了外部系统" + 自定义输入，不强迫写技术细节）；
   - 「当作没生效，安全重做」（= `RETRY`；走服务端原调用 CANCELLED + 模型重发起，**永不盲跑**）；
   - 「先跳过，稍后再说」（= `DEFER`；Ledger 记 pending，可审计、可稍后处理——Temporal/Codex 式"标记待查稍后"）。
4. **批量操作**：一键"全部按默认安全动作处理"（抄 DSH 的一键动作思想；弹窗期间不重新检查，决策只做一次）。
5. **#22 合规**：卡片全部由后端 `/api/sessions` + `recover` 的 `pending_decisions` 驱动；UI 不维护第二套真相（第一轮 §3 反向风险④维持）。
6. **诚实边界**：probe 查不到 = 卡片上写"未查到"，不写"可能已成功"之类的猜测语；Cline"故意不重标历史轮"的教训——**不回填、不伪造**。

### 9.5 对第一轮 §3 裁决的修正

| 第一轮命题 | 本轮修正 |
|---|---|
| (b) "UNKNOWN 先查外部事实，仍未知才交人"（部分成立） | **成立，但"交人"必须修正为"交可行动的选择题"**。开放式技术问答无任何成熟产品先例，是反模式（§8.8-1）。 |
| (c) "用户判已成功留裁决+来源，不伪造自动验证"（部分成立） | **维持**；补充：来源采集用"可选项+自定义"降低小白门槛（§9.4-3）；审计留痕有四家以上先例（§8.8-7）。 |
| — | **新增**：UNKNOWN 必须是一等状态（Codex #50118 教训，§8.8-5）；默认安全动作必须先于问人（§8.8-2）；Tool Contract 加 `replay_safe` 声明位有五家先例（§8.8-4）。 |
| 判定汇总 | Temporal/DSH/Inngest/Trigger.dev/LangGraph 新增 **PORT DESIGN**（概念层）；Pi/Claude/Cline/Codex 维持第一轮判定，本轮只加深证据。注意 Inngest 为 **SSPL**，只借概念不碰代码。 |

**引用核实状态（本轮新增）**：一手源码 `file:line` + commit（Pi `428a12bc` / Cline `cd80a20e` / DSH `5badb150`）；LICENSE 逐字核实（Temporal MIT / Pi MIT / Cline Apache-2.0 / Codex Apache-2.0 / DSH MIT / Trigger.dev Apache-2.0 / LangGraph MIT / Inngest SSPL+Apache-2.0-Future / Claude Code Anthropic 专有）；文档无版本号处如实标注替代证据。**未实质复制/Port 任何第三方代码**（本阶段零代码改动，只读）。
