# Lightweight Observable Agent Harness — Engineering Specification

本目录是项目的正式工程规格（Engineering Specification），主要受众是 **Codex / Claude Code / ZCode 等 AI Coding Agent**。

这不是教学计划，不按 Day 划分，不包含“今天学什么 / 用户亲手写什么 / 学习时间 / AI Coding 主导”等教学内容。旧 Day04–Day14 中真正有价值的工程需求、Failure Case、Scope Lock、Completion Gate 已被重组为模块级规格。

## 项目一句话定位

一个 **Python / Async-first、轻量、透明、可恢复、插件化** 的通用 Agent Harness。

Core 只拥有 Agent Runtime、Session/Event、Tool Runtime、Context、Recovery、Event/Streaming 与 Capability 基础设施；Coding、RAG、Research、Web、Memory、Finance、Multi-Agent 等均作为可插拔 Capability / Provider 存在。

## 阅读顺序

AI Coding Agent 在开始任何实现前，MUST 先读取：

1. `00_PROJECT_VISION.md`
2. `01_SYSTEM_ARCHITECTURE.md`
3. `13_OPEN_SOURCE_REUSE_MATRIX.md`
4. 当前要实现模块对应的规格
5. `14_IMPLEMENTATION_ROADMAP.md`

## 文件索引

- `00_PROJECT_VISION.md`：项目宪法、最高级工程原则、核心卖点、禁止事项。
- `01_SYSTEM_ARCHITECTURE.md`：总体架构、依赖方向、数据流、建议代码边界。
- `02_AGENT_RUNTIME.md`：最小 Agent Loop、Model Provider、Step/Run、分层预算与 counter、暂停/恢复、stuck 检测、完成判定（Quiescence + CompletionPolicy）、Fallback、Repeated Tool Guard。
- `03_SESSION_EVENT_MODEL.md`：append-only SessionEvent、暂停/恢复生命周期事件、Resume / Replay / Fork、事件事实源。
- `04_TOOL_RUNTIME.md`：Tool Contract、Registry、Executor、Validation、Retry、逻辑调用计数与显式配额、deadline 接纳边界、权限、依赖感知调度。
- `05_SANDBOX_CODING_TOOLS.md`：Docker Sandbox、Session Workspace、Coding Tools、Approval。
- `06_CONTEXT_ARTIFACT_MEMORY.md`：Context Builder、Compaction、Artifact/MinIO、Memory Capability / Context Provider。
- `07_STORAGE_PERSISTENCE_RECOVERY.md`：SQLite/PostgreSQL、Operation Ledger、Checkpoint、Crash Recovery、Reconcile。
- `08_PLUGIN_CAPABILITY_SYSTEM.md`：Capability seam、Provider/Consumer、动态扩展边界。
- `09_MCP_SKILLS_KNOWLEDGE_WEB.md`：MCP、Skills、Knowledge/RAG、Web Search、Citation。
- `10_MULTI_AGENT_DELEGATION.md`：AgentProfile、AgentFactory、Supervisor、动态 SubAgent、委派树共享预算（owner #287）、LangGraph 可选编排层。
- `11_STREAMING_API_WEB_UI.md`：AgentEvent、CLI、FastAPI SSE、预算/暂停的接口契约与投影、轻量 Session Inspector。
- `12_OBSERVABILITY_EVALUATION.md`：JSONL、Diagnostic Log、Langfuse、Golden Cases、强制 Live Gate（五场景 / 同树 3/3）、EvalScope Adapter。
- `13_OPEN_SOURCE_REUSE_MATRIX.md`：Pi / DeepSeek Harness / 官方 SDK 的 REUSE / ADAPT / PORT DESIGN / BUILD 决策。
- `14_IMPLEMENTATION_ROADMAP.md`：按依赖关系排列的实施阶段与 Gate，不按天数。

## 术语

- **Core**：任何业务 Capability 缺失时仍能运行的 Harness 基础。
- **Capability**：可替换、可插拔的能力边界。
- **Provider**：Capability 的一个实现，例如 `LangMemMemoryProvider`。
- **Consumer**：消费 Capability 的 Runtime、Context Provider 或模型可调用 Tool。
- **SessionEvent**：持久化、append-only 的会话事实事件。
- **Diagnostic Log**：用于 Debug/性能的结构化日志，不是业务事实源。
- **Operation Ledger**：真实外部副作用的执行账本。
- **Artifact**：不适合直接进入模型 Context 的完整大对象/大输出。
- **Context Provider**：向一次 Model Call 提供按需上下文的可插拔来源。
- **Local fuse**：单个 `AgentRuntime` 实例的高位保险丝（默认 `max_agent_turns=500`），不跨兄弟池化。
- **RunBudget**：一个逻辑 `run_id`（含它创建的子孙）的累积预算账本。
- **SessionBudget**：会话及其跨 run 委派后代的累积预算账本；同 run 恢复保留，fork 新建。
- **暂停（paused）**：命中预算 / deadline / stuck 后的 durable、**非终态**执行边界；恢复沿用同一 `run_id`。
- **NEED_RECONCILE**：副作用结果不可知时进入的状态，优先于预算恢复；未对账前不得续跑。
- **Runtime Quiescence**：完成前必须证明的静止条件（无悬空工具 / 审批 / 子 Agent / 账本 / reconcile）。
- **CompletionPolicy**：可插拔的完成判定 seam；可增加证据，不得绕过 Quiescence 与权限 / 账本检查。
