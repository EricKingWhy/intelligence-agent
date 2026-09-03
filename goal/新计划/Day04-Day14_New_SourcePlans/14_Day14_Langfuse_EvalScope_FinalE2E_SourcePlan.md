# Day 14 Source Plan — Langfuse + EvalScope + Final Full E2E

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day14-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 16 — Observability / Evaluation / Graduation
- **建议时间：** 约 4～5 小时；必要时分两次完成
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 今天不是学 SDK，而是学“如何证明 Agent 可观察、可评测、可回归”

最终要形成：

```text
真实 Agent
→ JSONL
→ Langfuse Trace
→ Golden Cases
→ Eval Runner
→ EvalScope
→ Regression Result
```

并做**唯一一次 Final Full E2E**。

# 2. 今天必须亲手完成

1. 打开一条真实 Langfuse Trace。
2. 用 Trace 回答：
   - 时间花在哪里；
   - 哪个模型调用最多/最慢；
   - Tool 重试几次；
   - 为什么触发 Web；
   - Main 委派几次；
   - 哪次 Coding/Test 失败。
3. 亲手写/改至少 2～3 条 Golden Cases。
4. 跑一次 Eval。
5. 看 deterministic metrics。
6. 最后完成 Full E2E，包括一次 Kill / Resume。

# 3. Langfuse 定位

JSONL 不删除。

```text
JSONL
= 最底层、可 grep/tail 的结构化 Debug Log

Langfuse
= Trace / Span / UI Observability
```

两者互补。

# 4. Trace 层级

建议：

```text
Agent Run
├─ supervisor
├─ research subagent
│  ├─ model
│  ├─ retrieve_knowledge
│  └─ web_search
├─ coding subagent
│  ├─ model
│  ├─ read/grep/edit/apply_patch
│  └─ bash
└─ final synthesis
```

关键 metadata 统一复用：

```text
session_id
run_id
agent_id
step_id
tool_call_id
operation_id
```

不要发明 5 套平行 ID。

# 5. AI Coding 主导 Langfuse 接入

Claude 可完成：

- 官方当前 SDK 集成；
- callback / tracing glue；
- custom spans；
- graceful no-op；
- metadata mapping。

用户重点看：

- Trace 结构；
- span 边界；
- 怎么从 Trace 定位问题。

Observability 挂了不能让核心 Agent 直接不可用。

# 6. Golden Dataset

项目自己拥有：

```text
evaluation/
├─ cases.jsonl
├─ runner.py
├─ assertions.py
└─ reports/
```

Case 可覆盖：

```text
Tool Selection
Tool Protocol
RAG / Citation
Recovery
Multi-Agent Routing
Task Success
```

# 7. Deterministic First

能代码判断的优先代码判断：

```text
tool_selected_accuracy
citation_valid_rate
task_success_rate
recovery_success_rate
dangling_tool_call_count
unnecessary_tool_rate
```

LLM Judge 用于：

- 答案完整性；
- evidence support；
- review quality。

不要所有指标揉成一个虚假的总分。

# 8. EvalScope

定位：

```text
project-owned eval runner
→ real Agent Runtime
→ RunTrace / Result
→ thin EvalScope adapter
→ report
```

不要为了 EvalScope 重写 Agent Runtime。

接入 plumbing 主要 AI Coding。

# 9. Regression

每次 Eval 记录：

```text
app_version
prompt_version
model_provider
model_name
knowledge_version
eval_dataset_version
git_commit
timestamp
```

否则两次分数不可解释。

第一版 Gate 可以是：

```text
P0 deterministic cases = 100%
dangling tool call = 0
core recovery = 100%
citation validity = 100%
task success 达到校准阈值
```

具体阈值可根据真实数据调整。

# 10. Final Full E2E

最终任务必须同时覆盖：

```text
历史 Session
→ Main
→ Research
→ retrieve_knowledge
→ insufficient
→ rewrite
→ web_search
→ citation
→ Main
→ Coding
→ grep/read/edit/apply_patch
→ bash pytest fail
→ Agent Debug
→ 再修改
→ Tool 执行中 kill Python
→ restart/resume
→ Graph checkpoint
→ Session Sandbox
→ Operation reconcile
→ Message consistency
→ pytest pass
→ Research/Review
→ Main Final
→ Langfuse Trace
→ Eval report
```

这是整个课程的最终验收，不再额外再做第二套“毕业考试”。

# 11. Final Checklist

## Agent Core
- Agent Loop；
- max_steps；
- Tool Calling 连续循环；
- READ_ONLY 并发；
- MUTATING 串行。

## Tool Runtime
- Validation；
- ToolResult；
- 单一 Tool Retry；
- 参数修正回模型；
- tool_call_id 配对。

## Recovery
- Session / Run / Checkpoint；
- Operation Ledger；
- Reconcile；
- Recovery ToolMessage。

## Context
- History / Context 分离；
- Artifact；
- Compaction；
- inspect_artifact。

## RAG
- Chunk；
- Embedding / Milvus；
- Agentic Retrieval；
- Citation；
- insufficient；
- Incremental Index。

## Streaming / Integration
- AgentEvent；
- CLI Streaming；
- FastAPI SSE；
- MCP；
- Skill。

## Multi-Agent
- LangGraph State；
- Supervisor；
- SubAgent；
- Subgraph；
- Graph Checkpoint；
- Session Sandbox。

## Production-ish
- Web Search；
- 简化 CRAG；
- Repeated Tool Guard；
- Model Fallback；
- JSONL；
- Langfuse；
- EvalScope。

# 12. 最终必须会讲的主题

不要求背代码，但应该能用工程语言说明：

1. Function Calling 到 Agent Loop 的完整协议。
2. ToolRegistry / ToolExecutor 为什么分层。
3. Retry 为什么只能有清晰责任域。
4. Crash 时为什么 Checkpoint 不等于副作用恢复。
5. History / Context / Artifact 的区别。
6. Agentic RAG 为什么不是固定 Pipeline。
7. Streaming 为什么仍需要完整 AIMessage。
8. MCP Tool 为什么仍要进统一 Runtime。
9. LangGraph 与自己 AgentRuntime 的职责边界。
10. Multi-Agent 的价值为什么不是“Agent 越多越好”。
11. Langfuse 怎么帮 Debug。
12. Eval 为什么 deterministic checks 优先。

# 13. AI_CODING_PRACTICE

Langfuse SDK、EvalScope adapter、report plumbing 都允许 AI 主导。

今天用户重点是：

> **会用 AI Coding 把平台接进去，再亲手使用这些平台定位问题和判断质量。**

# 14. 完成 Gate

只有当以下成立，课程才真正结束：

- [ ] Final E2E 成功；
- [ ] Kill / Resume 成功；
- [ ] Trace 可读；
- [ ] Eval 可运行；
- [ ] Citation 可验证；
- [ ] 用户能解释关键架构边界；
- [ ] 用户知道哪些代码是 AI Coding 完成、哪些核心机制自己真正掌握；
- [ ] 项目没有为了“炫技”新增超出 Roadmap 的复杂基础设施。
