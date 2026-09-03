# Day 10 Source Plan — LangGraph Core

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day10-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 12 — LangGraph Core
- **建议时间：** 约 3.5～4 小时；S+
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 为什么今天才引入 LangGraph

前面已经亲手理解：

```text
Model ↔ Tool Agent Loop
Tool Runtime
Checkpoint / Operation
Context
```

今天引入 LangGraph 的目的不是推倒重写，而是开始解决：

```text
State
Node
Edge
Conditional Routing
Graph Checkpoint
Interrupt / Resume
```

# 2. 今天必须亲手完成

1. 亲手画一个最小 StateGraph。
2. 把现有 Single Agent Runtime 包成 Node。
3. 查看一次 Graph State 输入/输出变化。
4. 增加一个最小 Conditional Routing。
5. 查看 state history / checkpoint。
6. 做一个最小 interrupt/resume 实验。
7. 对比 LangGraph Checkpoint 与 Operation Ledger。

# 3. 核心主链

```text
START
→ agent_node
→ END
```

Node：

```text
Graph State
→ 转换成现有 AgentRuntime input
→ await runtime.run(...)
→ AgentRunResult
→ 写回 Graph State
```

LangGraph 是 orchestration layer，不重写 V1 Runtime。

# 4. Graph State

State 应以可序列化数据为主。

可以包含：

```text
session_id
run_id
user_request
current_task
next_agent
task_results
iteration
final_answer
```

不要把：

- 整个 SQLite connection；
- Sandbox 对象实例；
- 各种客户端对象；

塞进 State。

# 5. Node / Edge / Conditional

必须理解：

- Node：处理 State 的执行单元；
- Edge：固定下一步；
- Conditional Edge：根据当前 State / decision 选择下一节点；
- Graph 本身负责 Workflow，不负责 Tool Retry。

# 6. 两层 Checkpoint

LangGraph Checkpoint：

```text
Graph State
next nodes
super-step
```

Operation Ledger：

```text
真实 Tool 外部副作用
```

即使 Graph 能恢复 Node，也不能因此盲目重跑 UNKNOWN Bash。

# 7. thread_id / Checkpointer

理解：

```text
thread_id
→ 一条 Graph 执行历史的恢复标识
```

使用 Async-first 兼容的官方 checkpointer。

具体 SDK plumbing 可 AI Coding。

# 8. Interrupt / Resume

做一个最小实验：

```text
Graph
→ interrupt
→ 用户确认/输入
→ resume
→ 继续
```

重点理解“Graph 停在哪里、恢复时 State 从哪里来”。

不做复杂审批中心。

# 9. AI Coding 主导

- checkpointer SDK 接线；
- debug CLI；
- state history formatting；
- 测试样板。

核心 State / Node / Edge / Route 要让用户参与。

# 10. Scope Lock

不做：

- Multi-Agent Supervisor；
- Subgraph；
- Session Sandbox；
- Prebuilt Supervisor；
- Tool Runtime 重写；
- LangGraph 内部 scheduler 源码。

# 11. 完成 Gate

用户能解释：

- AgentRuntime 与 LangGraph 分别负责什么；
- State 为什么要可序列化；
- Node 如何读写 State；
- Edge/Conditional 怎么路由；
- Graph Checkpoint 为什么不能替代 Operation Ledger；
- interrupt/resume 基本流程。
