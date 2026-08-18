# Agent Harness 0→1 新版 Module Roadmap

> 状态：Day 1–3 已完成。  
> 目标岗位：**AI 应用开发工程师**。  
> 课程目标：既快速完成一条完整 AI Agent 应用开发主线，又对真正影响实现、Debug、架构与面试的核心机制做细拆理解。  
> 课程节奏：**Module 是硬边界，Day 是软时间盒。** 不再机械服从旧 Day4–20 的日期切分。

---

# 0. 总体策略

## 0.1 学什么

后续课程围绕真实 Agent 工程能力展开：

```text
Day1–3 已完成
模型调用
→ Function Calling
→ Agent Loop

后续
→ Tool Runtime
→ Sandbox + Coding Tools
→ RAG Foundation
→ Agentic RAG
→ Session / Checkpoint / Crash Recovery
→ Context / Artifact / Compaction
→ Event / Streaming / FastAPI SSE
→ MCP / Skills
→ LangGraph
→ Multi-Agent
→ Web Research / 简化 CRAG / Reliability
→ Langfuse / EvalScope / Final E2E
```

## 0.2 怎么学

核心机制采用：

> **CORE_LEARNING：细拆运行链路，慢一点真正理解，但不向无关底层无限下钻。**

外围工程采用：

> **AI_CODING_PRACTICE：AI 主导实现，用户看方案、关键 Diff、验证结果，并亲手做一个小修改。**

## 0.3 周期

建议剩余约 **10～11 个学习日**，整个项目约 **13～14 个学习日**完成。

这只是期望节奏，不是硬 Deadline：

- 核心模块没真正理解，可以自然延长；
- 两个轻量模块当天都完成，可以自然合并；
- 不为了“凑 Day”拖慢，也不为了“压天数”牺牲 S 级核心理解。

---

# Module 4 — Tool Runtime 完整闭环

**重要度：S**  
**主要模式：CORE_LEARNING**  
**预计：1～1.5 Day**

## 工程目标

把 Day3 的临时 `dict[str, Callable]` Tool 执行升级成统一 Runtime：

```text
LLM Tool Call
→ Tool Contract
→ ToolRegistry
→ Validation
→ ToolExecutor
→ Timeout / Retry / Scheduling
→ ToolResult
→ ToolMessage
→ Agent Loop
```

## 必须细拆理解

1. Tool Contract 为什么是模型 Schema 与 Runtime Tool 的共同来源。
2. ToolRegistry 为什么只做注册/查询，不负责执行。
3. ToolResult 为什么必须结构化，而不是直接返回字符串。
4. Pydantic Validation 为什么发生在执行前。
5. `INVALID_ARGUMENT` 为什么不能在 ToolExecutor 内重试，而应该回给模型自纠错。
6. 为什么 ToolExecutor 是唯一 Tool Retry Layer。
7. READ_ONLY 批次为什么可以并发，出现 MUTATING 为什么整批串行。
8. `tool_call_id` 为什么始终保持严格配对。

## Hands-on

用户至少亲手完成/修改：

- 一个 Tool Schema；
- Agent Loop 接入 ToolExecutor 的核心位置；
- 一个 Validation / Retry / 并发行为实验；
- 从日志判断一次 Tool 为什么被重试或没有被重试。

## AI Coding 可主导

- ErrorCode 枚举；
- Pydantic DTO 样板；
- Exception Mapping 辅助代码；
- 大部分 pytest fixture / Fake Tool。

## 明确不做

- Docker；
- Session / Checkpoint；
- MCP Adapter；
- 复杂 Retry Budget；
- Tool 依赖 DAG。

## 完成标志

能脱离代码说清：

> `Tool Call → Registry → Validation → Executor → Retry/Scheduling → ToolResult → ToolMessage → 下一轮 LLM`

---

# Module 5 — Docker Sandbox + Coding Tools

**重要度：A**  
**主要模式：AI_CODING_PRACTICE + 核心边界理解**  
**预计：0.5～1 Day**

## 工程目标

形成：

```text
Agent
→ ToolExecutor
→ read / write / edit / bash
→ DockerSandbox
→ /workspace
```

## 必须理解

1. Sandbox 为什么是 Runtime 安全边界，不是 Prompt 约束。
2. Host / Container / Workspace 三者边界。
3. Tool 为什么不直接操作宿主机路径。
4. Sandbox 与 Tool 为什么分层。
5. `bash exit_code != 0` 为什么不等于 Runtime Exception。
6. read 是 READ_ONLY，write/edit/bash 为什么默认 MUTATING。

## AI Coding 主导

- `docker exec / cp / inspect` plumbing；
- `ensure_started()`；
- Docker 生命周期和异常映射；
- Docker 集成测试的大部分代码。

## Hands-on

用户必须亲手跑一个 Toy Project：

```text
read
→ edit
→ bash pytest
→ 根据失败结果继续修改
→ tests pass
```

至少从 JSONL / ToolResult 定位一次真实命令失败。

## 明确不深挖

- Docker CLI 每个参数；
- Docker daemon 内部；
- namespace / cgroup 底层；
- 容器网络细节。

---

# Module 6 — RAG Foundation：极简 Chunker + Embedding + Milvus

**重要度：A/S 混合**  
**主要模式：CORE_LEARNING（Chunk 主链）+ AI_CODING_PRACTICE（工程接入）**  
**预计：0.75～1 Day**

## 工程目标

跑通：

```text
Markdown
→ parse
→ chunk
→ embedding
→ Milvus
→ persistent search
```

## 用户必须亲手理解的极简 Chunker

只亲手实现最小窗口：

```text
text
→ tokenize
→ token ids
→ window
→ overlap
→ chunk
```

要求真正理解：

- `chunk_size`；
- `overlap`；
- `step = chunk_size - overlap`；
- 为什么 overlap 有价值；
- `overlap >= chunk_size` 为什么会出问题。

## 不再把 Markdown Parser 当底层课程

Heading-aware 思路保留：

```text
heading_path + chunk content
→ embedding_text
```

但复杂 Parser 边界优先：

- 使用成熟方案；或
- Claude 生成一个简化实现。

用户只需要理解 Heading Metadata 为什么影响检索语义。

## AI Coding 主导

- EmbeddingProvider；
- batch embedding；
- Milvus Store；
- Collection / metadata schema；
- ingestion CLI；
- 大部分持久化与失败状态代码。

## 用户必须会

1. Ingestion Pipeline 为什么和 Agent Runtime 解耦。
2. Chunk / Embedding / Vector Store 数据怎么流。
3. Metadata 为什么不能后补。
4. 为什么进程重启后知识仍应存在。
5. Milvus 只掌握 `insert/search/filter/delete/persistence`，不学集群运维。

---

# Module 7 — Agentic RAG + Incremental Index + Citation

**重要度：S/A**  
**主要模式：CORE_LEARNING**  
**预计：0.75～1 Day**

## 工程目标

把知识库变成 Runtime 中正式的 READ_ONLY Tool：

```text
Agent Loop
→ model decides
→ retrieve_knowledge
→ EmbeddingProvider
→ Milvus
→ retrieval results + citation
→ ToolResult
→ model
```

## 核心必须理解

1. Agentic RAG ≠ 固定“先检索再生成”。
2. 不允许关键词 Router 硬编码触发 Knowledge。
3. Knowledge Tool Description 本身会影响模型行为。
4. `sufficient=false` 的意义。
5. Citation 为什么必须从检索结果开始全链路保留 metadata。
6. 增量索引为什么 unchanged 文件不应该重新 embedding。
7. `doc_id` 为什么是稳定文档身份。

## AI Coding 可主导

- mtime + hash 的工程细节；
- metadata 状态更新；
- incremental index CRUD；
- 大部分测试样板。

## Hands-on

至少测试三种行为：

```text
该搜知识库
不该搜知识库
知识库没有证据
```

并检查最终 Citation。

---

# Module 8 — Session / Checkpoint / Operation Ledger / Crash Recovery

**重要度：S+**  
**主要模式：CORE_LEARNING**  
**预计：1 Day，必要时允许延长**

这是后续课程**不允许为了赶进度压缩**的模块。

## 核心目标

真正区分：

```text
Session
Run
Checkpoint
Tool Operation
```

理解：

> **状态恢复 ≠ 外部副作用恢复。**

## 必须细拆

1. `session_id / run_id / checkpoint_id / operation_id / tool_call_id` 各自解决什么。
2. Checkpoint 为什么必须代表“已经持久化的可恢复事实”。
3. Message + Checkpoint 为什么需要事务边界。
4. Tool Operation：
   `PENDING → RUNNING → SUCCEEDED/FAILED/...`
5. 为什么 crash 后 `RUNNING` 只能说明“开始过”，不能说明“成功/失败”。
6. 为什么恢复前要 reconcile。
7. READ_ONLY Tool 为什么容易恢复。
8. write/edit/bash 为什么必须更谨慎。
9. Tool 已成功但 ToolMessage 未写时怎么恢复原消息链。

## Hands-on

必须做真实 Kill / Resume 实验。

至少观察一次：

```text
Tool 开始
→ 外部动作发生
→ Python 进程被杀
→ 重启
→ Checkpoint + Operation Ledger
→ reconcile
→ 不盲目重复副作用
```

## AI Coding 可辅助

SQLite CRUD、DAO、表定义样板可以由 AI 完成，但用户必须看懂状态转移和恢复入口。

---

# Module 9 — Context Governance + Artifact + Compaction

**重要度：S/A**  
**主要模式：混合：核心 CORE_LEARNING，外围 AI_CODING_PRACTICE**  
**预计：0.75～1 Day**

## 三个必须真正理解的核心

### ① Persistent History ≠ Runtime Context

```text
完整历史
→ 持久化

每次发给模型的内容
→ ContextManager 重新构建
```

### ② Raw Tool Output ≠ 全部注入模型

```text
Raw Tool Output
→ Artifact 完整保存

Model Context
→ Summary + artifact_ref
```

### ③ Context 快满 → Compaction

```text
早期完整 Turns
→ structured summary
→ Runtime Context 替换
→ 原始 History 不删除
```

## 用户必须理解

- 为什么不能简单 `messages[-20:]`；
- 为什么 Tool Call / ToolMessage 不能被 compaction 拆断；
- Artifact 为什么解决“原始信息不丢”和“上下文不爆”的矛盾；
- `inspect_artifact` 为什么是按需取细节。

## AI Coding 主导

以下只需快速浏览设计和关键 Diff：

- 70% / 85% 阈值；
- TokenCounter；
- `/budget`；
- `/compact`；
- Artifact metadata CRUD；
- Summary Schema 样板；
- deterministic fallback summary。

## Hands-on

制造一个巨大 Fake Bash Output：

```text
Artifact = 完整
ToolMessage = summary + ref
Context 没爆
```

---

# Module 10 — Event-driven Streaming + FastAPI SSE

**重要度：S/A**  
**主要模式：CORE_LEARNING 主链 + AI_CODING_PRACTICE 外围**  
**预计：0.5～0.75 Day**

## 工程目标

从“Runtime 直接输出”升级为：

```text
AgentRuntime
→ AgentEvent
→ CLI Renderer
→ FastAPI SSE
```

模型侧：

```text
astream
→ ModelDelta
→ 实时 Event
→ 最终仍组装完整 AIMessage
→ Tool Calling / Checkpoint 继续
```

## 必须理解

1. Event ≠ Log。
2. Runtime 为什么不能直接 `print()`。
3. Streaming 为什么不能破坏 Agent Loop。
4. 为什么流结束后仍然需要完整 AIMessage。
5. FastAPI SSE 如何消费同一条 Event Stream。

## AI Coding 主导

- 大部分 Event DTO；
- CLI Renderer；
- SSE response formatting；
- event serialization；
- 普通顺序测试。

## Hands-on

亲手：

- 调一次最小 `/chat/stream` 或等价 SSE endpoint；
- 看模型 token/delta；
- 看 ToolStarted / ToolCompleted；
- 断开一次客户端并观察任务/生成器行为。

不扩展成完整 Web 产品。

---

# Module 11 — MCP + Skills + V1 Smoke E2E

**重要度：MCP=A/S；Skills=B/A**  
**主要模式：MCP 核心理解 + AI_CODING_PRACTICE**  
**预计：0.5～0.75 Day**

## MCP 必须理解

```text
Remote MCP Server
→ MCP Client
→ Tool Discovery
→ metadata/schema
→ MCPToolAdapter
→ ToolRegistry
→ ToolExecutor
→ remote call
→ ToolResult
```

重点：

- Agent 是 MCP Client；
- 不自己实现 MCP 协议；
- MCP Tool 不能绕过统一 ToolExecutor；
- Side Effect 不能靠 Tool 名字猜；
- MCP SDK retry 与 ToolExecutor retry 不能叠加放大。

## Skills 只学到够用

理解：

```text
Tool = 可执行能力
Skill = 按需加载的指导 / 知识 / 工作方法
```

SkillLoader / Registry 主要交 AI Coding，跑通一个 `SKILL.md` 即可。

## V1 Smoke E2E

只做轻量中期验收，不做“第二次毕业考试”：

```text
Knowledge
→ read/edit/bash
→ MCP（按需）
→ Final
```

确认：

- Session 可恢复；
- Checkpoint / Operation 可查；
- Artifact 存在；
- Streaming/SSE 可消费；
- Citation 存在。

控制在 30～60 分钟。

---

# Module 12 — LangGraph Core

**重要度：S+**  
**主要模式：CORE_LEARNING**  
**预计：1 Day**

## 核心目标

真正掌握：

```text
State
Node
Edge
Conditional Routing
Checkpointer
Interrupt
Resume
```

## 工程路线

先把已有 Single Agent Runtime 包入最小 Graph：

```text
START
→ agent_node
→ END
```

再增加一个最小 Conditional Routing / Interrupt 实验。

## 必须理解

1. LangGraph 是外层 orchestration，不替代 ToolExecutor。
2. Graph State 为什么应尽量可序列化。
3. Node 输入/输出怎么改变 State。
4. Conditional Edge 怎么决定下一节点。
5. `thread_id` / checkpointer / state history。
6. Graph Checkpoint 和 Operation Ledger 的职责区别。
7. Interrupt / Resume 的基本机制。

## Hands-on

用户亲手画和修改一条 StateGraph，并查看 state history。

---

# Module 13 — Multi-Agent Supervisor + SubAgent

**重要度：S+**  
**主要模式：CORE_LEARNING**  
**预计：1 Day**

## 工程目标

实现：

```text
Main / Supervisor
├─ Coding SubAgent
└─ Research / Review SubAgent
```

## 必须理解

1. Main 负责目标理解、delegation、收敛和最终综合。
2. SubAgent 复用已有 AgentRuntime，而不是每个重造 Agent Loop。
3. 不同 Agent 为什么拥有不同 ToolRegistry / 权限。
4. Supervisor 为什么应该 Structured Output。
5. `SubAgentResult` 为什么返回“任务结果”，而不是倾倒完整 Message History。
6. Main → SubAgent 的 Context 为什么必须收窄。
7. `max_delegations` 为什么是 Multi-Agent 的硬兜底。

## Hands-on

至少验证：

```text
research task → Research
coding task → Coding
mixed task → Main 协调
Supervisor 死循环 → max_delegations 终止
```

---

# Module 14 — Subgraph + Shared State + Session Sandbox + Multi-Agent Recovery

**重要度：S**  
**主要模式：CORE_LEARNING + AI_CODING_PRACTICE**  
**预计：1 Day**

## 核心理解

1. 什么时候 SubAgent 值得成为 Subgraph。
2. 父图只共享必要 State。
3. per-invocation SubAgent persistence 的意义。
4. `agent_id` 如何贯穿 Event / Log / Operation。
5. Session ↔ Sandbox 生命周期。
6. 同一 Session 多 Agent 为什么共享 workspace、但 Tool 权限不同。
7. MUTATING 为什么需要 Session 级 lock。
8. 恢复顺序：

```text
Graph checkpoint
→ Session Sandbox mapping
→ ensure sandbox
→ Operation reconcile
→ Graph resume
```

## AI Coding 主导

- sandbox mapping CRUD；
- volume/container lifecycle plumbing；
- lock 样板；
- graph history CLI。

## Hands-on

必须做 Multi-Agent Kill / Resume E2E。

---

# Module 15 — Web Research + 简化 CRAG + Reliability

**重要度：A/S**  
**主要模式：核心行为理解 + AI_CODING_PRACTICE**  
**预计：0.75～1 Day**

## 简化 CRAG

保留：

```text
retrieve_knowledge
→ sufficient=false
→ 生成一个更适合联网搜索的 query
→ web_search
→ Citation
→ synthesis
```

不要求复杂 Multi-query。

LLM `EvidenceGrade` 不作为必做核心；可保留简单 `sufficient` / evidence 判断。

## 必须理解

1. `web_search` 必须是独立 Tool，不能偷偷塞进 `retrieve_knowledge`。
2. Knowledge Source / Web Source 必须可追溯。
3. Agent 为什么决定“什么时候联网”。

## V2 原子知识更新

保留设计：

```text
new version
→ write/verify
→ active_version switch
→ delete old
```

实现完全允许 AI Coding；时间不足可只保留设计，不作为 Hands-on / Checkpoint。

## Reliability

### 用户重点理解
- Repeated Tool Guard；
- `max_steps` / `max_delegations` / repeated-tool guard 的作用域区别。

### AI Coding 主导
- Model Fallback；
- transient vs deterministic provider error mapping；
- glob / grep / apply_patch / git_status / git_diff；
- Fallback Trace 字段。

不做复杂 Circuit Breaker。

---

# Module 16 — Langfuse + EvalScope + Final Full E2E

**重要度：A/S**  
**主要模式：AI_CODING_PRACTICE + 最终工程验收**  
**预计：1～1.25 Day**

## Observability

Langfuse 接入主要交 AI Coding。

用户重点不是学习 SDK，而是学会看：

```text
Agent Run
├─ supervisor
├─ research
│  ├─ model
│  ├─ retrieve_knowledge
│  └─ web_search
├─ coding
│  ├─ model
│  ├─ read/edit/bash
└─ final
```

必须能用 Trace 回答：

- 时间花在哪里；
- 哪次模型调用慢；
- Tool 重试几次；
- 为什么触发 Web；
- Coding 哪次测试失败；
- Main 委派几次。

JSONL 继续保留。

## Evaluation

EvalScope 接入也主要交 AI Coding。

用户必须理解：

1. Golden Cases 为什么属于项目自己。
2. deterministic metric 优先于 LLM Judge。
3. Tool Selection / Protocol / Citation / Recovery / Routing / Task Success 怎么测。
4. 为什么不应该只看一个“总平均分”。

## 最终 Full E2E

只在这里做真正毕业验收：

```text
历史 Session
→ Main
→ Research
→ Knowledge insufficient
→ Web
→ Citation
→ Coding
→ 修改
→ pytest fail
→ Debug / 再修改
→ Tool 执行中 Kill
→ Resume
→ Graph checkpoint
→ Sandbox restore
→ Operation reconcile
→ pytest pass
→ Research Review
→ Main Final
→ Langfuse Trace
→ Eval report
```

这是整个课程最终闭环。

---

# 17. 新版路线的优先级

## 绝对不能为了赶时间压缩

- Agent Loop（已完成）
- Tool Runtime 核心
- Crash Recovery / Operation Ledger
- LangGraph 核心
- Multi-Agent Supervisor / Context 边界

## 可以“核心理解 + AI Coding”

- Sandbox plumbing
- RAG Ingestion 工程代码
- Context 外围能力
- Event DTO / SSE formatting
- MCP Adapter
- Session Sandbox plumbing
- Model Fallback
- Langfuse
- EvalScope

## 只需知道怎么回事

- Docker CLI 细节
- Markdown Parser 复杂边界
- TokenCounter 实现细节
- `/budget` / `/compact` CLI 样板
- SkillLoader 大部分代码
- 原子知识版本切换实现细节
- 辅助 Coding Tools 样板

---

# 18. 执行原则

以后不要问：

> “旧计划写 Day 7，所以今天是不是只能做 Day 7？”

而要问：

> “当前 Module 的工程能力是否完成？核心机制是否达到应用开发所需理解？”

**Module 完成才进入下一个 Module。**

如果一个 Module 只花半天：

- 可以继续下一个 Module；
- 但仍然一次只允许一个 ACTIVE Task。

如果一个核心 Module 一天没学透：

- 第二天继续；
- 不因日期切换强行跳模块。

最终标准：

> **核心机制慢一点真正掌握；普通工程大胆用 AI Coding；整条 AI 应用开发主线尽快跑完整。**
