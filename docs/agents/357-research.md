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
