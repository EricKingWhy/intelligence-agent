# #352 [W-08] 验收项与真实证据的服务端投影 —— 成熟产品调研报告

> 阶段：研究（只调研、不施工）。工作区 `~/workspace/intelligence-agent-wt-352`，
> 分支 `codebuddy/352-w08-evidence-projection`（基线 `cf6e5470`，干净）。
> 本报告是 SDD §1.3 / AGENTS.md §6.1 要求的「方案依据」块 + 票面成立性结论。

## 0. 启动检查表（AGENTS.md §3，如实）

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md` §2.2 Observable / §2.5 Reuse First / §3 冻结原则（1/3/5/6/9/17） | READY |
| 当前任务规格 | `goal/.../06_CONTEXT_ARTIFACT_MEMORY.md` §3 Artifact Store / §8 Failure Semantics；`docs/PRD_RECOVERABLE_REVIEWABLE_AGENT_WORKBENCH.md` §4.2 证据与审阅；`docs/tickets/workbench-2026-09-27/W-08-evidence-projection.md` 全文；`gh issue view 352` 全文（含 2026-10-03 审计节） | READY |
| Reuse 相关判定 | `goal/.../13_OPEN_SOURCE_REUSE_MATRIX.md` §2 Pi / §3 DeepSeek Harness / §5 BUILD 条件 / §7 上游核查链接 | READY |
| Phase 依据 | issue #352（父 #344，P0，in-progress；blocked-by #350/#351 均已关）+ W-08 票（`docs/tickets/workbench-2026-09-27/`）。注：W-* 工作台票系不在 `14_IMPLEMENTATION_ROADMAP.md` 内（该 Roadmap 无 W-* 章节），Phase 依据以 workbench 票系 + 父 issue 为准，如实记录 | READY |
| 本任务触发细则 | `docs/SDD_WORKFLOW_PROTOCOL.md` §1.3 / §8.8.1 / §9 全文；`AGENTS.md` §3/§6/§7/§9.1.1/§16.2 全文；`docs/agents/skills/PROVENANCE.md` §4.2 | READY |

---

## 1. 票面摘要（复核用，以票面为准）

- **目标**：验收项 ↔ 真实证据（Tool Result、Artifact、浏览器结果、Git diff、reviewer 结论）的服务端关联投影。
- **范围**：Event / API / Artifact 引用与工作区快照投影。候选 `src/agent_harness/session/service.py`、`src/agent_harness/web/artifacts.py`、现有 workspace/diff 工具。
- **每条证据最小字段**（14 个）：`evidence_id, task_session_id, run_id, criterion_id, kind(test|ui|diff|external), source_event_seq/tool_call_id, captured_at, result(pass|fail|blocked), command_or_action, exit_code_or_observation, artifact_ref, base_head, workspace_manifest_hash`。
- **依赖**：W-07（#351，已关）、W-06（#350，已关）——均已满足。
- **不做**：重跑 #319 五个 Runtime Gate、新审查台账；不碰 `src/agent_harness/context/`（#651/#647/#648 在途）、`session/fork.py`（#527 在施工）、approve_policy。

## 2. 2026-10-03 审计红线（施工必须遵守，设计已按此收敛）

1. 全 workspace hash 含 `progress.md` 会让证据自我过期 → 证据必须标明覆盖的**精确文件 manifest**、**独立元数据 hash**、**明确过期原因**。
2. `artifact_ref` 必须**可回读 + 权限检查**。
3. manifest 不是新 workspace 恢复系统 → **绝不引入 #527 的快照基础设施**。
4. 复用 V3.1-lite 真实入口验证与 review ledger → **不建第二套工程 Gate**。

---

## 3. 方案依据（SDD §1.3 五字段）

### 3.1 来源（3 个独立来源，均一手核实）

| # | 来源 | 读取方式与日期 | 形态 |
| --- | --- | --- | --- |
| 1 | Anthropic 工程博客《Effective harnesses for long-running agents》<https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents> | 2026-10-06 一手整页读取（`browser.open`，217 行） | 公开网页文章，非代码 |
| 2a | OpenAI dev blog《Run long-horizon tasks with Codex》<https://developers.openai.com/blog/run-long-horizon-tasks-with-codex> | 2026-10-06 一手整页读取（`browser.open`，260 行） | 公开网页文章，非代码 |
| 2b | Codex app 介绍页 <https://openai.com/index/introducing-the-codex-app/> | 2026-10-06 一手整页读取（`browser.open`，140 行）；另有仓库内 2026-10-04 一手记录（`docs/tickets/workbench-2026-09-27/W-07-task-delivery-state.md:23`、`docs/agents/reference-sources.md:88`） | 公开网页，非代码 |
| 3a | Pi（badlogic/pi-mono）会话/压缩机制 | 2026-10-06 本地浅克隆 `/tmp/pi-mono` @ `9ad0831`（`feat(ai): let apps name themselves in OpenAI logins (#10433)`），一手 grep + 读码 | 代码（MIT，见 §3.5） |
| 3b | DeepSeek Harness `docs/subsystems/session.md` | 2026-10-06 本地浅克隆 `/tmp/deepseek-harness` @ `5badb15`（`Merge pull request #5648 … release-dsh-0.2.1-alpha.1`），一手读 1294 行文档 | 文档（MIT，见 §3.5） |

> 诚实记录：CodeBuddy 无头 `-p` 会话内 `WebFetch`/`Bash` 被权限拒绝，来源 1/2 的 CodeBuddy 子报告依赖仓库既有记录 + 转述并如实标注为「暂定」；本报告作者随后用 `browser.open` 对三个页面做了**一手整页读取**（2026-10-06），子报告的机制结论与一手读取一致，「暂定」标记就此解除。来源 3 的 CodeBuddy 子报告同样因 Bash 被拒只能读仓库旧语料，本报告作者用本地浅克隆做了**一手复核**（并纠正了子报告两处事实错误：Pi License 是 MIT 不是 Apache-2.0；DSH `docs/subsystems/session.md` 真实存在且已一手读取）。

### 3.2 机制摘要（每个来源怎么解这个问题）

#### 来源 1 · Anthropic：用「外置清单 + 真实端到端验证」对抗过早宣布完成

- **M1 双 agent 分工**：initializer agent 负责搭环境（`init.sh`、`claude-progress.txt` 进度文件、初始 git commit），coding agent 每 session 只做增量进展、离场时留结构化更新。核心洞察是让新 context window 的 agent 快速理解工作状态，靠「进度文件 + git 历史」实现交接。
- **M2 需求清单初始全失败**：initializer 把用户 prompt 展开成结构化 JSON 需求清单（示例 200+ 条端到端 feature），每条 `passes` 初始为 `false`。coding agent 只允许改 `passes` 字段；强措辞禁令 *"It is unacceptable to remove or edit tests because this could lead to missing or buggy functionality."*；用 JSON 而不用 Markdown（模型更不容易乱改 JSON）。
- **M3 真实端到端验证才许标通过**：*"Only mark features as 'passing' after careful testing."* 明确点名的失败模式是 agent 改完代码、跑了单元测试/`curl` 就宣布完成，却没做端到端验证。对策是显式 prompt 要求用浏览器自动化（Puppeteer MCP）像真实用户一样操作、截图验证。
- **M4 干净离场**：每 session 结束提交描述性 git commit + 更新进度文件；"clean state" 定义为可合入 main 的状态；git 既做进展证据也支持回滚坏改动。
- **M5 交接仪式**：新窗口依次读进度文件 → git log → feature list → 选最高优先级未完成项 → 先跑冒烟 E2E（发现坏状态立刻修，不先开新功能）。

#### 来源 2 · OpenAI Codex：证据次序 = 循环 + 外置记忆 + "done when" 纪律

- **N1 Agent 循环**：Plan → Edit code → Run tools (tests/build/lint) → Observe results → Repair failures → Update docs/status → Repeat。循环的价值是给 agent 真实反馈（errors、diffs、logs）与外置状态（repo、files、docs、worktrees、outputs）。
- **N2 持久项目记忆（durable project memory）**：spec/plan/约束/状态写进 markdown 文件，agent 反复重读，防止漂移、稳定 "done" 的定义。文件栈：`Prompt.md`（目标 + "Done when" 检查）、`Plan.md`（里程碑 + 每里程碑的 **acceptance criteria + validation commands** + stop-and-fix 规则：验证失败先修再往前）、`Implement.md`（runbook）、`Documentation.md`（状态 + 决策审计日志）。
- **N3 每里程碑验证**：每个里程碑完成后跑验证命令（tests、lint、typecheck），修好失败再继续。完成标准不是 "it compiles" 而是 "does it follow the instructions, and does it actually work?"。
- **N4 执行与采纳分离（Codex app）**：agent 在隔离副本（git worktree）上工作；线程内可 review diff、评论、可在编辑器打开；Automations 的结果 *"land in a review queue so you can jump back in and continue working"* —— 完成不由执行者单方宣告，采纳动作落在人审侧。
- **N5 技能化 QA**：agent 可承担 QA tester 角色 *"to validate its work by actually playing the game"*；每 feature 后 *"thoroughly test it … and confirm it works"*。

#### 来源 3a · Pi：append-only 会话 + 跨压缩文件账目（无验收项实体）

- **P1 会话模型**：`packages/coding-agent/src/core/session-manager.ts` 为 append-only 消息数组（带 `parentId` 的树在文件内）；JSONL 全历史永不删除。
- **P2 跨压缩文件账目**：`compaction.ts` 的 `extractFileOperations` 合并上次 `details` 与本轮工具调用文件清单，`formatFileOperations` 输出 `<read-files>`/`<modified-files>`（`core/compaction/utils.ts`），调用处传 `prevCompactionIndex` 实现**跨压缩累积**。这是**文件路径清单，不是逐文件 hash，也不是逐验收项证据**。
- **P3 合法切点**：`toolResult` 永不切（`compaction.ts` 白名单），保证工具调用/结果配对不断裂。
- **P4 关键否定结论**（一手 grep `acceptance|criterion|evidence` 全仓）：Pi 的会话模型里**不存在** acceptance-criteria / verification / evidence 实体；唯一 "evidence" 命中是 `config.ts:117` 注释里的 npm 用词，无关。

#### 来源 3b · DeepSeek Harness：event-sourced Session + 只派生不另存（无验收项实体）

- **D1 Session = append-only typed SessionEvent 日志**，是 agent 全部交互历史的唯一真源；LLM 消息历史**从日志派生、永不另存**；replay = 从同一事件重新派生（`docs/subsystems/session.md` 首段，一手）。
- **D2 事件词汇可合并扩展**：插件可声明新事件类型（如 compaction  seam 的 `compaction/start|summary|end`）；**不**是 SurfaceEventType 的不进 surface。
- **D3 surface vs 日志**：模型读 surface 投影，人读原始日志（transcript/replay）；`surfaceOp` 的 `replace` 只改投影不改日志。
- **D4 关键否定结论**（一手 grep 全文 1294 行）：**未见** acceptance-criteria / verification / evidence 实体；最接近任务态的是 `todo/write`、`goal/change`，但它们不是"逐验收项 + 可回读证据"的关联结构。

### 3.3 契合点（与 AGENTS.md §7 不变量逐条点名）

**契合（可借鉴）：**

- **#2 Core Runtime 自己掌控**：三个来源的机制都是 harness 流程约定，不绑外部 framework；与本仓自研 Runtime 同向。
- **#3 append-only typed SessionEvent**：DSH 是本仓 SessionEvent 的直接设计来源（spec 03 已 PORT DESIGN），强契合；Anthropic 的真相是可变工作区文件（`passes` 反复改写）——**冲突**，本仓若照搬会破坏 append-only（见下）。
- **#4 Event ≠ Diagnostic Log**：DSH 明确区分瞬态 Cordis 事件与持久 SessionEvent，强契合；Anthropic 把运行日志和证据真相合成 `claude-progress.txt` 一个文件——**冲突**，正是本仓要分层的两件事。
- **#5/#6 Persistent History ≠ Runtime Context / 完整保存≠完整注入**：Pi 的 `convertToLlm` 边界翻译、DSH 的 surface 投影，与本仓 derive_messages/ContextBuilder 同构，契合。
- **#7 Tool 只有一条统一执行路径 / #11 Sandbox·Permission 是 Runtime 边界**：Anthropic 的反伪造/受限写入**全部靠 prompt 措辞**（"strongly-worded instructions"）——**直接违反**本仓红线；本仓的证据写入校验必须走 Runtime 形状校验，不靠 prompt。
- **#13 Operation Ledger 支持 reconcile**：三个来源均未涉及 Ledger；本票证据投影**不得**绕过 Ledger、不得建第二套 Gate（票面已定）。
- **#15 Artifact 大内容优先 Local/MinIO，模型只拿 summary + ref**：Pi 用 `[Showing lines x-y of z. Full output: /tmp/…]` 做 ref 化截断，方向契合；但三来源**均无** artifact 权限/可回读保证。
- **#17 Artifact 优先于 Context Pollution**：Anthropic 把状态外置到文件而非塞满上下文，方向一致。
- **#22 Web UI 不维护第二套不可对账 Session 真相**：Codex "review queue + 线程内 diff" 契合本仓 W-07/W-09 的服务端投影驱动审阅页方向；证据投影必须落可回读 SessionEvent/Artifact 引用，**不得**新增可变证据表（=第二真相）。

**不变量未被触及**（#1 Python/Async、#8 Tool Retry、#9 Fallback/Retry 分离、#10 并发依赖、#12 Checkpoint≠副作用恢复、#14、#16/#17 Memory、#18 Capability、#19 SubAgent、#20 LangGraph、#21 Optional 故障隔离）：三来源无对应机制，本票不应据此改动它们。

### 3.4 判定（REUSE / ADAPT / PORT DESIGN / BUILD / DEFER + 理由）

| 子机制 | 判定 | 理由 |
| --- | --- | --- |
| Anthropic 需求清单「初始全失败」规则 | **ADAPT** | 值得吸收「验收项先置未通过、只许改状态位」的防漂移规则；但必须 (a) 由 Runtime/Permission 强制而非 prompt（§7 #11）；(b) 状态从 bool 升级为票面 `pass\|fail\|blocked` 三态；（W-07 六态 `not_started/in_progress/passed/failed/blocked/incomplete` 已覆盖） |
| Anthropic 进度文件交接形态 | **PORT DESIGN（truth 归属反转）+ DEFER 交接部分** | 「外置交接文件便新窗口快速理解状态」的思想可借，但本仓真相是 SessionEvent，进度/证据只能是**派生投影**（W-05/#349 已把 `progress.md` 做成 SessionEvent 的确定性投影）；跨窗口接力属 W-06/#350 范围，不属 #352（Scope Lock） |
| Anthropic 真实端到端验证 | **REUSE**（本仓既有链路） | 本仓 V3.1-lite 已有真实入口验证；票面已写「无 Chrome/MCP 时浏览器项是未完成/缺工具，不是 pass」「用户手工验收与机器验证分列」——与 M3 同构，无需搬实现 |
| Codex 证据次序（plan→test/observe→repair 循环 + 每里程碑验证） | **PORT DESIGN** | 只借次序/循环与 "done when" 纪律；本仓落点 = 证据 `kind` 的采集顺序与 `command_or_action`/`exit_code_or_observation` 字段语义 |
| Codex app 执行→人审 diff→采纳分离 | **PORT DESIGN** | 「执行者不自宣完成 + 人审采纳」已在 W-07 落为三轴事实（执行/验证/接受分离）；review queue 产品形态不借 |
| Pi 跨压缩累积的文件账目 | **PORT DESIGN**（最小设计思想） | 只借「证据性账目随压缩累积、原文永存」原则；Pi 无 hash/失效原因，本票必须补齐；Pi runtime 整体维持复用矩阵 `DEFER`（不嵌入 TS runtime） |
| DSH event-sourced + 只派生不另存 | **PORT DESIGN**（已实现，作纪律背书） | 本票的「写侧新事件 + 读侧纯 derive、绝不建平行可变表」正是 DSH 纪律的直接应用；DSH 无 criterion↔evidence 功能 |
| **#352 核心：14 字段证据实体 + 精确 manifest + 独立元数据 hash + 明确过期原因 + base_head + artifact 权限回读** | **BUILD（最小）** | 三来源**全部没有对应物**（见 §3.6）；必须自建，但复用本仓 ArtifactStore / SessionEvent / V3.1-lite 真实入口 / review ledger，不建第二套 Gate |
| 上游 runtime 整体（Pi TS runtime / DSH） | **DEFER** | 复用矩阵既定；本票只取设计思想 |

### 3.5 License 结论

- 来源 1/2/2b：公开网页文章/博客，**零代码复制、零实质 Port**，只做思想级借鉴 → **无 License 传导义务**。
- 来源 3a Pi：`/tmp/pi-mono/LICENSE` 一手核实为 **MIT**（Copyright (c) 2025 Mario Zechner）；本次只读调研、无复制 → 无传导义务；将来若实质复制须保留 MIT 版权声明与来源。
- 来源 3b DSH：`/tmp/deepseek-harness/LICENSE` 一手核实为 **MIT**（Copyright (c) 2026 DeepSeek）；同上。
- 若 #352 施工阶段实质复制任一上游代码片段，须按复用矩阵 §1 第 3–6 条重新核查 License 并保留来源。

### 3.6 票面 14 字段在成熟产品中的对应物核查（关键证据）

| #352 字段 | Anthropic | OpenAI Codex | Pi | DSH | 结论 |
| --- | --- | --- | --- | --- | --- |
| `evidence_id` | 无（无 evidence 实体） | 无 | 无 | 无 | **BUILD** |
| `task_session_id` / `run_id` | 无（无 session/run 模型） | thread/run 概念但无此二字段映射 | 有 session 概念，无此字段 | 有 session/turn/step，无 task 概念 | **BUILD** |
| `criterion_id` | 弱对应 = feature 条目身份（category/description/steps 下标），无 ID 契约 | Plan.md 每里程碑 acceptance criteria，无 ID 契约 | 无 | 无 | **BUILD**（本仓 `task.py:78` item_id 已定义对齐键，REUSE） |
| `kind(test\|ui\|diff\|external)` | 无分类 | 无分类 | 无 | 无 | **BUILD** |
| `source_event_seq` / `tool_call_id` | 无（最接近的是 git commit SHA） | 无 | 无 | DSH 有 `seq` 信封（`sourceEventSeqs`）但无此用途 | **BUILD**（本仓 SessionEvent seq 机制 REUSE） |
| `captured_at` | 无 | 无 | 无 | 有 `time` 信封字段（思想可借） | **BUILD** |
| `result(pass\|fail\|blocked)` | 弱对应 = `passes` 布尔（无 blocked） | 无三态 | 无 | 无 | **ADAPT**（bool→三态；W-07 六态已覆盖） |
| `command_or_action` / `exit_code_or_observation` | 半对应（curl/浏览器观察是口头描述，无结构化字段） | 测试命令是口头描述，无 schema | 无 | 无 | **BUILD** |
| `artifact_ref`（可回读 + 权限检查） | 无（截图不持久化、无权限层） | 无 | 无 | 无 | **BUILD**（本仓 ArtifactStore REUSE，见 §4） |
| `base_head` | 半对应（有 git commit，无显式字段） | worktree 隔离但无 base 字段 | 无 | 无 | **BUILD** |
| `workspace_manifest_hash` | 无（刻意不 auto-commit、无 manifest、无失效原因） | 无 | 只有路径清单（无 hash） | 无 | **BUILD** |

**一句话**：成熟产品的「证据」最多是「清单里的一个布尔 + 一个 git commit + 一句口头观察」；#352 要求的 evidence 实体、事件引用、artifact 权限/回读、精确 manifest、base_head、明确过期原因**在四个来源里全部没有对应物**。这是本仓自有设计，不是从别处抄来的。

---

## 4. 本仓现有代码可复用核实（AGENTS.md §9.5 懒惰阶梯）

### 4.1 可复用清单（文件:行 + 复用什么）

**验收/验证轴（W-07 #351 已交付）**
- `src/agent_harness/session/task.py:76-99` `TaskCriterion`（`item_id` 是"验证轴与接受轴的对齐键"）、`VerificationEntry(value, evidence: str|None)`；`:110-161` `TaskState`；`:126-140` `product_state` 纯投影；`derive_task_state`（纯函数、幂等、可重放）。→ **REUSE**：证据投影挂同一条 derive 轴，不另起状态机。
- `src/agent_harness/session/event.py` 事件常量集 + 词汇表/前端类型生成链（`scripts/gen_event_vocabulary.py`、`scripts/gen_event_types.py` + 守卫测试）。→ **REUSE**：新证据事件的登记点。
- `src/agent_harness/session/service.py:3946-4018` `task_state`（只读投影）/`task_definition`/`task_acceptance_revision`/`task_verification`/`task_acceptance`/`task_acceptance_release`；`:3934-3944` `_apply_task_and_refresh`（写后刷新钩子）。→ **REUSE**：证据读/写链路的接入形状。
- `src/agent_harness/web/task_delivery.py:98-168` `GET /api/sessions/{id}/task`、`POST /task/verification`、`VerificationRequest`、422/409 映射。→ **REUSE**：传输面与错误码口径。

**Artifact 引用与可回读**
- `src/agent_harness/storage/artifact.py:81-120` `ArtifactStore.save/load/inspect`、`compute_artifact_id`（`sha256(content)[:16]`）；`:46` `SESSION_KEY_PATTERN`（key = `{session_id}/{artifact_id}`）。→ **REUSE**：`artifact_ref` 形态、按 session 命名空间隔离、内容自证。
- `src/agent_harness/storage/local_artifact.py:128-176` `load` 的 content-hash 自证（hash 不符即 KeyError）+ 旁挂元数据 `source_tool/tool_call_id/created_at`。→ **REUSE**：可回读性 + 完整性。
- `src/agent_harness/web/app.py:2525-2620` `GET /api/sessions/{id}/artifacts/{artifact_id}`（422/404/503）。→ **REUSE**：读回路径。
- `src/agent_harness/web/artifacts.py` `build_read_artifact_store`（读写选择器统一，`select_artifact_store` 单一选择器）。→ **REUSE**：读写一致性纪律。

**工作区文件 / diff / Git**
- `src/agent_harness/web/workspace_files.py:469-592` `GET .../workspace/files`、`.../workspace/file`、`.../workspace/git/status`、`.../workspace/git/diff`；`:21-33` 三道访问闸（来源闸/路径边界/会话归属）。→ **REUSE**：文件枚举、git diff、边界校验。
- `src/agent_harness/tools/git.py:78-120` `git_status_command`/`git_diff_command`（scope 路径围栏 + pathspec 白名单）、`run_git_command`；`GitStatusTool`/`GitDiffTool`（READ_ONLY）。→ **REUSE**：git 命令构造纪律；新增 HEAD 读取须走同一围栏。
- `src/agent_harness/tools/_diff_data.py:19-30` `diff_data(before, after)`。→ **REUSE**：diff 类证据的原件来源。

**独立元数据 hash / 陈旧判定先例（#352 最直接的仓内范式）**
- `src/agent_harness/session/progress.py:552-561` `progress_content_digest`（归一化后 sha256 公共契约）；`:836-851` `_classify_disk_body`（current/server_stale/external 三分类）；`:925-1066` `verify_progress_file`（`artifact_exists` 可读回校验、`source_event_seq` + `content_sha256` + `previous` 元数据）。→ **REUSE 范式**：证据的"独立元数据 hash + 明确过期原因 + 可回读校验"**镜像此模式**，不是新恢复系统。
- `src/agent_harness/session/store.py:555` `line_sha256`（行级完整性）、`read_events`/`read_events_report`。→ **REUSE**：只读 event replay。
- `src/agent_harness/agent/completion_evidence.py:80-104` `_successful_tool_calls`（按 `tool_call_id` 配对 `tool/call`↔`tool/result` 且 `ok=true`）。→ **REUSE**：`kind=test/external` 的工具成功事实配对逻辑。**注意：#524 是 Runtime 完成门，不是本票服务端投影，二者不得混同**（票面依赖只列 W-07/W-06）。

### 4.2 缺口清单（必须新建，最小范围）

1. **证据实体本身**：14 字段当前**零实现**（`VerificationEntry.evidence` 只是 `str|None` 自由文本）。
2. **工作区 manifest（path→SHA-256）**：无现成能力（`workspace/` 内 `manifest|sha256|hashlib` 零命中；Sandbox 只有 `list_files`/`read_text`）。新建**最小** manifest：显式文件清单 + 逐文件 sha256（复用 `hashlib` + `progress_content_digest` 归一化纪律）。**审计红线**：`progress.md` 不得纳入被覆盖文件集（否则更新进度即自我过期）；其 hash 独立单列。
3. **base_head**：`tools/git.py` 只有 status/diff，无 `rev-parse HEAD`/`HEAD^{tree}`。新增只读 HEAD 读取（走同一围栏/白名单纪律）。
4. **证据的 durable 载体**：需**新的 append-only typed SessionEvent**（见 §4.3）；现有事件不足以重放出 manifest/base_head/captured_at/artifact_ref 关联。
5. **陈旧求值**：读取时比较 `evidence.workspace_manifest_hash` vs 当前 manifest、`evidence.base_head` vs 当前 HEAD，产出"哪些文件变了"的明确原因；绝不沿用旧"通过"。形态镜像 `progress.py` 的 `_classify_disk_body`/`verify_progress_file`。
6. **artifact_ref 显式归属校验**：读回路径存在，但 artifact 路由**未挂 `require_trusted_origin`**（对比 `workspace_files.py` 三道闸），且 `artifact_id` 是内容哈希不携带归属（隔离仅靠 URL session_id 构造前缀）。证据"只引用权限受控的原件"需要显式的归属 + 来源校验步骤。
7. **API 投影面**：`GET /task` 只回 `TaskState.to_payload()`，无证据列表/陈旧标识；扩展此 GET 或新增 `GET /evidence`（只回服务端投影，客户端不推断）。
8. **`ui` 类证据后端暂无来源**（Chrome MCP 属 W-18 未接入）：设计必须能表达"缺工具 = blocked/未完成"，绝不落 pass（票面工作指令 2 已定）。

**明确不做**：第二套工程 Gate、重跑 #319、#527 workspace 快照/恢复基础设施、自动 TTL 清理。

### 4.3 `VerificationEntry.evidence` 的层级建议（不变量论证）

**结论：不升级 `VerificationEntry.evidence` 字段，也不只做纯 derive；走"新增 append-only typed 事件（写）+ 纯 derive 投影（读）"两条腿，`evidence` 字符串保留为人类摘要/指针。**

- **#3（必须新事件）**：manifest/base_head/captured_at/artifact_ref/command/exit_code 都是**采集时点的真实事实**，现有事件流里没有、也无法在有界重放下"算"出来。只做 derive = 让投影发明未持久化的事实，破坏重放确定性。
- **#4**：证据是任务事实，进 SessionEvent；诊断保持另一层。
- **#22（必须 derive）**：读侧是服务端纯投影（幂等、可 replay），Web/TUI 消费同一份。
- **为何不动 `VerificationEntry.evidence`**：W-07 验证轴是**逐项 last-wins 单值观察**，而证据天然是 **1 criterion ↔ N 证据**（"保留原始失败与重跑记录、不覆盖旧证据"，票面工作指令 1）——与 last-wins 单值直接冲突。改它 = 破坏 W-07 已有契约（票面明令不改 #305/#351 语义）+ 基数模型错误。`evidence: str|None` 保留作人类可读摘要 / 指向 `evidence_id` 的指针；结构化证据走**独立证据轴**，以 `evidence_id` 为主键、`criterion_id` 外键 join 验收清单（`item_id` 即对齐键，`task.py:78`）。
- **反模式明示**：不得把证据只存在 `progress.md`（投影文件，自身 hash 引发自我过期）；不得落 SessionEvent 之外的可变证据表（=第二真相，违反 #22）。

---

## 5. 票面成立性结论

### 结论：① 票面成立

成熟产品调研**没有**推翻票面的问题 framing，也**没有**给出明显更好的做法：

1. 四个独立来源（Anthropic / OpenAI Codex / Pi / DSH）**全都没有**「验收项 ↔ 结构化证据关联 + 过期语义」的机制（§3.6 逐字段核查为证）。票面的 14 字段设计不是从别处抄来的，是本仓自有、且已被 PRD §4.2 冻结字段集。
2. 票面范围（Event/API/Artifact 引用 + 工作区快照投影）与审计红线自洽：不建第二套 Gate、不引 #527 快照基础设施、manifest 精确文件集 + 独立元数据 hash + 明确过期原因。
3. 依赖（W-07/W-06）真实存在且已交付：`task.py` 三轴投影、`task_verification(item_id, value, evidence)` 链路、`progress.py` 的陈旧分类范式都是现成复用点（§4.1），不存在"票面假设的代码不存在"的情况。
4. 票面没有与 AGENTS.md §7 不变量冲突之处；§3.3 的冲突项（Anthropic 的 prompt 级反伪造、可变文件当真相）恰好是**票面要求用 Runtime 校验 + append-only 事件来纠正**的方向，调研反而加固了票面。

### 简单实用的设计方向（不超票面范围，供施工阶段细化）

1. **写侧**：新增 append-only typed 事件（如 `evidence/recorded`），payload = 14 字段 DTO；形状校验放 session 层 handler（坏形状→拒绝、零事件，沿用 `plan.py`/`task.py` 判据）；重跑追加新证据、不覆盖旧记录；登记 `event.py` 常量集并重生成词汇表/前端类型。
2. **读侧**：新增 `derive_evidence_state(events)` 纯投影，按 `criterion_id` 聚合当前有效证据集；幂等、可 replay；与 `derive_task_state` 同域（#22），不新增状态机。
3. **陈旧层**：读取时求值——比较 `evidence.workspace_manifest_hash` vs 当前被覆盖文件 manifest、`evidence.base_head` vs 当前 HEAD，产出明确过期原因（列出变动文件）；绝不沿用旧"通过"。形态镜像 `progress.py:_classify_disk_body`/`verify_progress_file`，独立于 progress 机制。
4. **manifest**：显式文件清单 + 逐文件 sha256；`progress.md` 不纳入覆盖集、其 hash 独立单列（防自我过期，审计红线 1）。
5. **artifact**：复用 ArtifactStore 可回读 + content-hash 自证；补显式归属/来源校验（缺口 §4.2-6）。
6. **传输**：扩展 `GET /api/sessions/{id}/task` 或新增 `GET /evidence`，只回服务端投影；错误码沿用 422/409/404 口径。
7. **分层纪律**：`VerificationEntry.evidence` 维持 `str|None`（人类摘要/指针）；`#524 completion_evidence`（Runtime 完成门）与本票（服务端投影）严格区分，不混同。

### 待用户决策事项（研究阶段不决断、不施工）

1. `ui` 类证据：W-18（Chrome MCP）未到之前，是否先以 `blocked`/`incomplete` 表达缺工具（票面工作指令 2 已倾向此答案，施工前请确认）。
2. 新事件命名与 14 字段 payload 的精确形状（施工阶段按 §9.1.1 走 ticket 细化）。
3. `GET /task` 扩展 vs 新 `GET /evidence` 端点（施工阶段定）。
4. artifact 路由补 `require_trusted_origin` 与显式归属校验：属本票范围还是另立票（建议本票内最小补齐，因票面要求"只引用权限受控的原件"）。

---

## 6. 证据指针（关键出处速查）

- 票面：`gh issue view 352`；本仓票 `docs/tickets/workbench-2026-09-27/W-08-evidence-projection.md`；审计节 `<!-- issue-audit-2026-10-03 -->`。
- 规格：`docs/PRD_RECOVERABLE_REVIEWABLE_AGENT_WORKBENCH.md` §4.2（第 53–57 行 diff 证据字段与过期语义）；`goal/.../06_CONTEXT_ARTIFACT_MEMORY.md` §3/§8；`goal/.../13_OPEN_SOURCE_REUSE_MATRIX.md` §2/§3。
- W-07 方案依据范例：`docs/tickets/workbench-2026-09-27/W-07-task-delivery-state.md` §方案依据（§6.1 五字段模板）。
- 现有实现：`src/agent_harness/session/task.py`（三轴投影）、`src/agent_harness/session/progress.py:552-561,836-851,925-1066`（hash/陈旧范式）、`src/agent_harness/storage/artifact.py:81-120`（ArtifactStore）、`src/agent_harness/web/artifacts.py`（读写选择器统一）、`src/agent_harness/web/task_delivery.py:98-168`（传输面）、`src/agent_harness/web/workspace_files.py:469-592`（git 端点）、`src/agent_harness/tools/git.py:78-120`（git 围栏）。
- 一手来源：Anthropic 博客 / OpenAI long-horizon 博客 / Codex app 页（2026-10-06 `browser.open` 整页读取）；`/tmp/pi-mono` @ `9ad0831`（MIT）；`/tmp/deepseek-harness` @ `5badb15`（MIT，`docs/subsystems/session.md` 1294 行一手）。

---

*本报告只含调研结论与设计方向，无实现代码。施工需另行立项并按 §9.1.1 走票面细化与用户批准。*
