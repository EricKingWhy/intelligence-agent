# intelligence-agent

一个 **Python / Async-first、轻量、透明、可恢复、插件化** 的通用 Agent Harness。
Coding / Knowledge / Research 等能力通过 Capability / Provider / Tool 可插拔接入，不是 Core 的特判分支。
本文件是项目的领域词汇表，只定义概念，不写实现细节。

## Language

**Sandbox**:
模型发起的命令与文件操作实际运行的隔离执行环境，是 Runtime 的安全边界而非 Prompt 约束。
_Avoid_: container, executor, environment

**Workspace**:
Sandbox 内部允许 Coding Tool 读写的唯一目录；越界访问会被 Sandbox 拒绝。
_Avoid_: working dir, project folder, bind mount, volume

**Coding Tool**:
在 Sandbox 内执行、按 `Tool` 契约暴露给模型的工具（read / write / edit / bash）。
_Avoid_: code tool, file tool, action

**Host**:
用户真实机器，Sandbox 的隔离边界之外的环境。
_Avoid_: 本机, server, node

**Container**:
由 Docker 提供的隔离执行环境，是 Sandbox 的一种后端实现。
_Avoid_: VM, jail, pod

**ExecResult**:
Sandbox 执行一条 shell 命令后返回的原生结果（exit_code / stdout / stderr / duration），由 bash 工具映射成 `ToolResult`。
_Avoid_: CommandResult, ShellResult, RunOutput

**命令业务失败**:
shell 命令返回非零 exit_code 但 Sandbox 调用本身成功（如 pytest 测试不通过）。这不是 Tool Runtime 异常，Agent 应读 stdout/stderr 决定下一步。
_Avoid_: tool failure, execution error, transient error

## Session / Event 层

**Session**:
一次会话的聚合根，持有 `session_id` 与已加载的事件列表；对外提供 `start` / `resume` / `append` / `derive_messages` / `begin_run` / `end_run` 等业务方法，是 Runtime 与外部世界（CLI / Web UI）交互的单一入口。
_Avoid_: conversation, dialogue, chat, context window

**SessionEvent**:
持久化、append-only、类型化的会话事实事件（`session/started`、`run/completed`、`tool/call` 等）。是 Resume / Replay / Fork / derive_messages 的唯一事实源；不可原地修改，修订用新事件表达。
_Avoid_: log entry, message, record, history item

**SessionStore**:
SessionEvent 的薄 IO 层，只负责"读 JSONL / append JSONL"两件事，不持有业务状态。V1 只实现 `JsonlSessionStore`，未来 SQLite / PostgreSQL 作为可替换后端接入。
_Avoid_: database, repository, event bus, message queue

**derive_messages**:
从 SessionEvent 序列投影出模型可见 messages 列表的纯函数。负责 tool_call / tool_result 配对与 dangling 检测；不修改事件、不产生副作用。遵循 `events → derive_messages() → message history → ContextBuilder` 流向。
_Avoid_: serialize messages, flatten history, get messages

**dangling tool_call**:
事件序列中存在 `tool/call` 但无匹配 `tool/result` 的孤立状态（通常因进程崩溃）。处理方式：检测后注入合成 `ToolMessage`（content 写明"执行被中断，结果未知"），让消息链自洽，模型能看到失败并自主决定下一步。
_Avoid_: orphan call, broken chain, missing result

**Run**:
一次 `AgentRuntime.run()` 调用的生命周期单元，绑定 `run_id`。同一 Session 可有多次 Run；Run 边界由 `run/started` 与 `run/completed` / `run/failed` 事件标记，是 Phase 14 Fork 的切分依据。
_Avoid_: turn, iteration, loop, attempt

**Resume**:
从已持久化 SessionEvent 加载 Session 并继续对话的能力。流程：`load events → validate seq → restore state → reconcile → continue`。Resume MUST NOT 默认重放已完成 Tool。
_Avoid_: restart, reload, reconnect, replay（Replay 是独立概念，见 Phase 14）

**Diagnostic Log**:
用于 debug / 性能追踪 / 全链路观察的结构化日志（span / trace / agent_decision / retry 等），写入 `logs/agent.jsonl`。与 SessionEvent 分层（不变量 #5：Event ≠ Log），不是业务事实源，不可用于恢复。
_Avoid_: event log, session log, audit trail

## Storage / Recovery 层

**Operation**:
一次 Tool 调用在持久化层中的身份单元，绑定 `operation_id`（与 `tool_call_id` 1:1）。记录调用从 PENDING → RUNNING → SUCCEEDED / FAILED / CANCELLED 的状态流转；崩溃时 RUNNING 转 UNKNOWN / NEED_RECONCILE。是 reconcile 的最小粒度。
_Avoid_: tool call, invocation, action, transaction

**Operation Ledger**:
所有 Operation 的持久化状态账本。不是 SessionEvent 的副本——SessionEvent 记录"对话历史发生了什么"，Ledger 记录"每次 Tool 调用现在处于什么状态"。Reconcile 时读它而非重放 events，因为外部世界可能在崩溃期间已变（迁移已跑、付款已扣）。
_Avoid_: event log, operation history, execution log

**Checkpoint**:
Session 在某个稳定边界（`USER_ACCEPTED` / `MODEL_COMPLETED` / `TOOL_BATCH_COMPLETED` / `FINAL_COMPLETED`）上的"可恢复事实"快照。不是"代码执行到某一行"，也不是副作用恢复机制（不变量 #12）。Checkpoint + 对应的 SessionEvent SHOULD 尽可能事务化提交。
_Avoid_: save point, snapshot, game save

**Reconcile**:
崩溃恢复时，对照 Operation Ledger 判断每个未终止 Operation 的真实结局并补齐 ToolResult 的过程。按 Operation 终态分四种语义：SUCCEEDED + 缺结果 → 从 Ledger 合成恢复 ToolResult；FAILED → 合成失败结果；CANCELLED → 合成显式取消；PENDING → 可重执行；UNKNOWN → 进入 Tool-specific reconcile 或交用户处理（不变量 #14）。
_Avoid_: rollback, undo, replay, redo

**NEED_RECONCILE**:
UNKNOWN Operation 进入的待裁决状态——Ledger 无法自动判定副作用是否已发生。默认交用户处理，不允许盲重跑（不变量 #14）。
_Avoid_: error, blocked, paused, pending review

**Recovery ToolResult**:
Reconcile 阶段从 Ledger 合成的 ToolResult，复用原始 `tool_call_id`，让 derive_messages 能配对 dangling tool_call。与崩溃时注入的合成 ToolMessage 同目的，但数据来自 Ledger 而非"结果未知"占位。
_Avoid_: fake result, placeholder, dummy result

**ReconcileCallback**:
Resume 时遇到 UNKNOWN Operation 调用的用户裁决回调。与 `ApprovalCallback`（Phase 3，事前授权）平行——ReconcileCallback 是事后裁决（"这个 Operation 的副作用到底发生了吗"）。用户拿到 Operation 全上下文返回裁决（确认成功 / 确认失败 / 手动重跑 / 放弃）。
_Avoid_: approval callback, permission callback, review gate

**CheckpointPolicy**:
注入 AgentRuntime 的薄 seam，决定何时写 Checkpoint。AgentRuntime 在每个稳定边界（`USER_ACCEPTED` / `MODEL_COMPLETED` / `TOOL_BATCH_COMPLETED` / `FINAL_COMPLETED`）调 `policy.maybe_save(session, boundary_type)`。默认实现 `OnStableBoundary`；测试可用 `NoCheckpoint` / `EveryStep`。
_Avoid_: checkpoint manager, save strategy, persistence policy

**ReconcileHint**:
Tool 对"我怎么验证自己是否成功执行"的封装，供 ReconcileCallback 参考。含可验证性标记（verifiable / unverifiable）+ 建议验证动作。Tool ABC 默认返回 `ReconcileHint(verifiable=False)`——安全默认即 NEED_RECONCILE；可验证的工具（read/write/edit/glob/grep/git_status/git_diff）覆写。
_Avoid_: recovery hint, verification plan, side effect description

**RecoveryCoordinator**:
Resume 的编排器，注入 SessionStore + WorkspaceRegistry + OperationLedger + ReconcileCallback，按 07 §9 冻结顺序编排 8 步恢复，暴露 `recover(session_id) -> Session`。Session 只管 events，Runtime 只调 coordinator 一行——恢复是独立关注点。
_Avoid_: session manager, recovery manager, resume controller

**PendingPolicy**:
Reconcile 时对 PENDING Operation（Tool 未启动）的处理策略。默认 skip（合成 skipped ToolResult，最安全）；可注入 retry 策略。Ledger-first 顺序下 PENDING 极罕见。
_Avoid_: retry policy, execution policy, pending handler

## Streaming / Web UI 层

**AgentEvent**:
Runtime 向外发出的业务事件流（`agent/started`、`model/delta`、`tool/started`、`tool/completed`、`run/completed` 等），供 CLI / SSE / Web UI / Test / Trace 多面消费。与 Diagnostic Log 分层（不变量 #4 重申）：AgentEvent 是业务事实可重放，Diagnostic Log 是运维调试不可恢复。`print()` 不能当事件通道。
_Avoid_: log entry, debug message, notification

**ModelDelta**:
模型逐 token 流式产出的事件，对应 `model.astream()` 的单个 chunk。持久化可配置（spec 03 §3：live 必有，落盘可选）；完整 `AIMessage` 始终由 `model/completed` 持久化，delta 不替代完整消息。是「流式输出效果」的数据来源。
_Avoid_: token stream, partial message, streaming chunk

**AgentRuntime.run_stream**:
新增的流式驱动方法，签名为 `async def run_stream(session, user_input) -> AsyncIterator[AgentEvent]`。内部用 `model.astream()` 逐 chunk 产 `model/delta`，保留现有 `tool/call`/`tool/result`/`run/*`/`model/completed` 事件语义。旧 `run()` 保留签名不变（现有 252 测试和 demo 不破），重构为 `run_stream` 的消费端薄封装。
_Avoid_: streaming run, async run, chunked run

**SSE Surface**:
FastAPI 提供的 Server-Sent Events 下行通道（`GET /sessions/{id}/stream`），把 `run_stream` 的 AgentEvent 逐条推给前端。SSE 只是一层传输 surface，**不持有 Runtime 状态**（spec 11 §4）。客户端断连时要清理 generator/queue，不泄漏 producer task，不破坏 Session 一致性。
_Avoid_: push connection, event endpoint, streaming pipe

**Event Projection**:
前端的纯函数 reducer，把 raw SessionEvent / AgentEvent 流投影成渲染模型（对话流 / 工具列表 / step detail）。镜像 Python 侧 `derive_messages` 的逻辑，**不存独立业务真相**（不变量 #22）。保证刷新后从 `GET /sessions/{id}/events` 读历史 + 接 live SSE 完整重建视图。
_Avoid_: view model, state store, client-side truth

**Turn**:
Chat 视图里一个用户输入到下一次用户输入之间的完整循环，含模型若干轮 + 中间的所有工具调用。DSH 式「Thought for a while」折叠默认把过程收起，展开看完整推理 + 工具卡片。
_Avoid_: step, loop, iteration, response

**Tool Card**:
对话流里工具调用的可视化单元，按工具类型分专属卡片：bash → 终端黑卡（stdout/stderr/exit_code）；edit/apply_patch/write → diff 双栏（绿增红删，数据来自工具返回的 before/after）；其余 7 个 → 统一折叠卡片（参数 + 结果 JSON）。每张卡片显式生命周期态（running / success / failed / interrupted）。
_Avoid_: tool widget, call bubble, action block

**Approval Card**:
对话流内联的审批 UI 单元。agent 卡在需审批的 tool_call 时原地出现（工具名 + 参数 + 风险说明 + 同意/拒绝按钮），用户决策走 `POST /sessions/{id}/approve` 回传，批准后工具继续执行、结果回填同一卡片。不用 modal（割裂上下文）。
_Avoid_: permission dialog, confirm popup, auth modal

**Inspector 三栏**:
Web UI 的冻结布局（spec 11 §5）：左栏 Sessions/Runs/Fork Tree；中栏 Conversation + Agent activity + Tool calls；右栏 Step Detail（model 元数据 / tool args/result / retry / artifact / context / checkpoint / recovery）。Phase 4-5 字段在 initial 版本留空槽 + graceful empty state，后续 Phase 填。
_Avoid_: dashboard, console, panel layout

## Artifact / Context 层

**Artifact**:
完整保存但默认不直接注入模型的大对象/大输出。由 ToolResult 溢出自动产生（非 Tool 显式声明），通过 content-hash 寻址（`artifact_id` = 内容哈希）。存于 Runtime 域存储（七牛云 Kodo S3 兼容），不经过 Sandbox。模型只拿到 summary + `artifact_ref`，需要细节时用 `inspect_artifact` 按行局部读取。守不变量 #15「Artifact 大内容优先 Local / MinIO，模型只拿 summary + ref」——此处 MinIO 泛化为对象存储。
_Avoid_: cache file, blob, attachment, large output

**ArtifactStore**:
Artifact 的持久化边界（ABC）。`save()` 存内容返回 `Artifact` 元数据；`load()` 全量读回；`inspect()` 按行范围/关键词局部读。默认实现 `S3ArtifactStore`（七牛云 Kodo S3 兼容端点，用 `aioboto3`），测试用 `FakeArtifactStore`（内存）。接口保持 S3 抽象，未来可换 AWS S3 / R2 / COS。
_Avoid_: file system, blob store, object storage wrapper

**Artifact Overflow**:
ToolResult 后处理的自动溢出检测。当 ToolResult 的主输出字段超过阈值（字符数），Executor 的 `OverflowHandler` 自动调 `ArtifactStore.save()` 存原始内容，然后把 ToolResult 替换成截断摘要 + `artifact_ref`。摘要零 LLM——纯截断（前 N 行 + 后 N 行 + 元数据），在 Ledger 写入之前完成，保证 Ledger 记录的 `result_json` 与 `artifact_ref` 一致。
_Avoid_: truncation, output filter, result compression

**inspect_artifact**:
Phase 5 新增的第 10 个 Coding Tool，READ_ONLY 但操作 Runtime 存储而非 Sandbox。构造时注入 `ArtifactStore`（不是 `Sandbox`）。模型通过 `artifact_ref`（从 ToolResult 获得）按行局部读取 Artifact 细节：`start_line` / `end_line` / `keyword` / `max_lines`。大 Artifact 永远不完整灌回 Context。
_Avoid_: view artifact, artifact reader, file viewer

**ContextBuilder**:
Runtime loop 第 1 步的替换层（`build(session) -> list[AnyMessage]`），内部复用 `derive_messages` 投影 + 做后处理：替换 artifact overflow 后的 ToolMessage、检测 token 占用、按需触发 Compaction。单一入口，Runtime 不再直接调 `derive_messages`。预留 `context_providers: list[ContextProvider]` 扩展点（Phase 6 填 MemoryContextProvider，Phase 5 空列表）。
_Avoid_: context manager, message builder, prompt assembler

**estimate_tokens**:
Token 估算函数（`estimate_tokens(text) -> int`），用 `tiktoken` cl100k_base 精确计数。对所有 provider 一致（对非 OpenAI 模型是 ~10% 近似）。Compaction 的阈值（auto 0.70 / hard 0.85）相对于 `max_context_tokens`（默认 200000），基于这个估算。未来换 Claude 原生 tokenizer 是一行改动。
_Avoid_: token counter, length calculator, context meter

**Compaction**:
当 Runtime Context 的 token 估算超过 `auto_compact_threshold`（默认 0.70 × max_context_tokens）时，ContextBuilder 将早期完整的 AIMessage+ToolMessage 块（以 AIMessage 为原子边界，不可拆断 tool_call/ToolResult 配对）压缩成结构化 summary，注入 messages 头部。持久化 SessionEvent 不变（不变量：完整保存 ≠ 完整注入）；压缩产生 `context/compacted` 事件记录投影变更。
_Avoid_: context truncation, history pruning, window sliding

**Compaction 三层降级**:
摘要生成的降级链：(1) LLM 结构化摘要（用同一个 ModelProvider，保留 facts/decisions/constraints/failed_attempts/unresolved/artifact_refs/citations/tool outcomes）→ (2) LLM 失败时走 deterministic 机械提取（保留 HumanMessage 原文截断 + AIMessage 只留 tool_calls + ToolMessage 只留 tool_call_id + 截断 content）→ (3) 机械提取后仍超 `hard_guard_threshold`（0.85）则抛 `ContextWindowExceededError` 阻止 loop（spec §8：必须停止或要求用户处理）。
_Avoid_: fallback summary, emergency compression, context eviction

**ContextProvider**:
Phase 5 只定义 Protocol（`select(session, token_budget) -> list[AnyMessage]`），不实现。是 ContextBuilder 的扩展点——Phase 6 的 MemoryContextProvider 通过它往 Runtime Context 注入 memory entries。Core 不直接依赖任何具体 Provider。
_Avoid_: context plugin, injection hook, context source

**artifact/created**:
新增 typed SessionEvent，在 Artifact 溢出自动产生时 append。data 带 `{artifact_id, session_id, source_tool, tool_call_id, size, mime_type}`。是业务事实（Tool 副作用产生了外部存储对象），replay 和 fork 都需要。
_Avoid_: artifact log, storage record

**context/compacted**:
新增 typed SessionEvent，在 Compaction 完成后 append。data 带 `{compacted_turn_count, summary_message_count, token_estimate, fallback_used}`。记录 Runtime Context 投影的语义变更（从这个点开始早期 turns 被压缩），replay 时重建 Context 投影需要。
_Avoid_: compaction log, context snapshot

---

## Memory 层（Phase 6）

**IdentityContext**:
请求级的身份上下文（`tenant_id` + `user_id` + `scopes`），由 HTTP 中间件从 JWT 解析后设入 Python `contextvar`。所有 Memory 读写操作从 `contextvar` 读取，不接受外部参数传入。关键安全属性：中间件设置后模型层无法修改——模型不能伪造 `user_id` 查别人的数据。CLI / 测试设默认值 `(tenant_id="local", user_id="local")`。不进 SessionEvent（身份是请求级的，事件是持久的）。
_Avoid_: auth context, user session, identity token

**MemoryCapability**:
Memory 能力的读写原语层（`store` / `recall` / `search`），是 Protocol 不是具体实现。管「能不能存取记忆」。Core 永远只依赖这个 Protocol，不感知 LangMem 或任何具体 Provider。默认实现 `LangMemMemoryCapability`（通过 BaseStore 适配我们的存储），测试实现 `FakeMemoryCapability`（内存 dict）。换 Mem0 只换这个实现，上层不动。
_Avoid_: memory provider (provider 是具体实现，capability 是接口), memory manager

**MemoryContextProvider**:
Memory 能力的上下文注入层，实现 Phase 5 已定义的 `ContextProvider` Protocol（`select(session, token_budget) -> list[AnyMessage]`）。管「按 budget 选哪些记忆注入 Context」。内部调 `MemoryCapability.search` → 按 relevance / recency / importance 修剪到 budget → 拼成单条 SystemMessage 注入 Context（插在 system prompt 之后、对话历史之前）。是 `ContextBuilder.context_providers` 列表的填充者（Phase 5 空列表，Phase 6 填入）。
_Avoid_: memory injector, context memory hook

**MemoryScope**:
记忆的归属层级，对外是 5 值枚举（`GLOBAL` / `TENANT` / `USER` / `SESSION` / `AGENT`），对内映射成 namespace tuple（对齐 LangMem namespace + Milvus partition key）。V1 只实现 `USER`（跨 session 记住用户偏好）+ `SESSION`（session 内临时记忆），其余留枚举不实现。检索默认只查当前用户的数据（由 IdentityContext 约束）。
_Avoid_: memory level, memory tier, memory namespace（namespace 是内部编码，不是用户面词汇）

**MemoryEntry**:
一条记忆的结构化表示（`id` / `content` / `metadata` / `score` / `created_at`）。由 MemoryExtractor 从 Session 事件流提取，存进 SQLite（权威记录）+ Milvus（向量索引），检索后拼进 SystemMessage 注入 Context。
_Avoid_: memory record（record 是存储层词汇，entry 是领域词汇）, memory item

**MemoryStore**:
Memory 的持久化边界（对外单接口，内部组合 `MemoryRecordStore` + `VectorIndexStore` 两个 Protocol）。权威记录存 SQLite（事实源），向量索引存 Milvus（partition key 按 tenant_id 隔离）。双写通过 outbox pattern 保证最终一致：SQLite 事务同时写记忆行 + outbox 行，进程内 asyncio relay 读 outbox 推到 Milvus。Milvus 写失败不丢数据（SQLite 里有，标记 `indexed=False`，后台重试）。Milvus 索引可从 SQLite 重建。
_Avoid_: memory database, memory backend

**MemoryExtractor**:
从 Session 事件流提取记忆条目的组件，两层降级：(1) LLM 抽取（用 ModelProvider，prompt 要求输出结构化 JSON 记忆条目，失败判据是超时 / 非 JSON / schema 不匹配）→ (2) 启发式规则（`user/message` 抽偏好关键词、`run/completed` 的 `final_text` 抽关键决策、`tool/result` `ok=False` 抽失败模式，纯规则不需 LLM）→ (3) 返回空列表（不写 Memory）。每次 run 结束后台自动触发，模型不参与写入决策。
_Avoid_: memory scraper, memory harvester

**memory/degraded**:
新增 typed SessionEvent，在 Memory Provider 故障降级时 append。记录「本次 Memory 不可用，未注入历史记忆」事实，前端可据此显示降级提示。不阻塞 Runtime loop。
_Avoid_: memory error, memory failed

**LangMem**:
默认 Memory Capability 实现（通过 `[memory]` optional extra 安装）。负责 Memory Formation（从对话提取结构化记忆）+ Consolidation（相似记忆合并去重）+ search（embedding + 相似度检索）。不拥有存储——通过 LangGraph 的 `BaseStore` Protocol 操作我们的 SQLite + Milvus。Core 禁止直接 import LangMem concrete class（不变量 #17）。
_Avoid_: memory engine, memory service

**Outbox（Memory）**:
Memory 双写一致性机制（transactional outbox pattern）。SQLite 单事务同时写记忆行 + outbox 行（要么都成功要么都回滚），进程内 asyncio 后台 relay 定期 poll outbox 表把未同步的行推到 Milvus 向量索引，成功后标记。relay 崩溃重启自动恢复（outbox 行持久化在 SQLite 里）。幂等性由 consumer 保证（按 memory_id 去重）。
_Avoid_: memory sync queue, vector indexer
