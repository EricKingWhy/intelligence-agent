# Day 12 Source Plan — Subgraph + Shared State + Session Sandbox + Multi-Agent Recovery

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day12-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 14
- **建议时间：** 约 4 小时；S
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 今天让 Multi-Agent 进入“可恢复的真实工作区”

目标：

```text
Main Graph
├─ supervisor
├─ coding subgraph
└─ research subgraph

Session
→ Sandbox mapping
→ same workspace

Crash
→ Graph checkpoint
→ Sandbox restore
→ Operation reconcile
→ Graph resume
```

# 2. 今天必须亲手完成

1. 把至少一个 SubAgent 组织成 Subgraph。
2. 检查父图 Shared State 只保留必要字段。
3. 让 `agent_id` 出现在 Event/Log/Operation 中。
4. 创建 Session-scoped Sandbox。
5. 验证同一 Session 的 Coding/Research 看到同一个 workspace。
6. 做一次 Multi-Agent 运行中 Kill。
7. 按正确顺序 Resume。
8. 验证已经完成的外部操作不会被错误重复。

# 3. Subgraph 什么时候值得用

当 SubAgent 内部：

- 有多步状态；
- 需要独立测试；
- 需要独立观察；
- 需要恢复；

才有必要做 Subgraph。

不要为了“架构高级”所有节点都强行 subgraph。

# 4. Persistence

当前推荐：

```text
SubAgent per-invocation
```

即：

> 每次 Main 委派一个 task，SubAgent 完成后返回，不默认长期累积自己的完整对话。

长期事实仍放：

- Main Session；
- Artifact；
- Knowledge；
- Shared task results。

# 5. Shared State

只共享必要字段：

```text
current_task
task_results
iteration
final_answer
```

不要让多个 Agent 随意覆盖一个巨大 `messages` 列表。

避免：

- 状态冲突；
- Context 爆炸；
- 不知道是谁写入。

# 6. agent_id

正式使用：

```text
main
coding
research_review
```

贯穿：

```text
Event
Log
Checkpoint
Operation
Trace（后续）
```

便于 Debug “到底哪个 Agent 做的”。

# 7. Session Sandbox

从：

```text
Project → Container
```

升级：

```text
Session
→ SandboxSession
→ container_id + volume/workspace
```

生命周期：

```text
/new
→ create session + sandbox

/exit
→ 可 stop，但保留 workspace

/resume
→ 恢复原 sandbox

delete
→ explicit cleanup
```

# 8. 多 Agent 是否共用 Sandbox

当前项目：

```text
同一个 Session
→ Main/Coding/Research 共用一个 workspace
```

但 Tool 权限不同。

这样 Coding 修改后，Research Review 能看到真实修改。

以后真正并行 Agent Team / worktree 不属于当前范围。

# 9. MUTATING Lock

同一 Session 不允许多个写 Agent 同时修改。

V1 可用：

```text
session-level asyncio.Lock / lease
```

实现细节 AI Coding。

# 10. Resume 顺序

必须牢记：

```text
load Graph checkpoint
→ load Session-Sandbox mapping
→ ensure sandbox started
→ reconcile Tool Operations
→ restore message consistency
→ Graph resume
```

不能 Graph 先跑起来，才发现 workspace 不见了。

# 11. AI Coding 主导

- sandbox mapping table；
- container/volume plumbing；
- lock 样板；
- graph history CLI；
- lifecycle CRUD。

用户重点看恢复顺序和职责边界。

# 12. Failure Experiment

运行：

```text
Main
→ Coding
→ MUTATING Tool 执行中
→ kill Python
```

重启检查：

```text
Graph checkpoint
+
Session Sandbox
+
Operation Ledger
```

三者如何协同。

# 13. Scope Lock

不做：

- 多 Coding Agent 并行；
- Worktree；
- 分布式锁；
- Agent Team Scheduler；
- CRAG；
- Langfuse。

# 14. 完成 Gate

能解释：

- Graph 恢复什么；
- Sandbox 恢复什么；
- Operation Ledger 恢复什么；
- 为什么三者不能互相替代；
- Shared State 为什么必须收窄；
- 为什么同 Session 共用 workspace 但权限不同。
