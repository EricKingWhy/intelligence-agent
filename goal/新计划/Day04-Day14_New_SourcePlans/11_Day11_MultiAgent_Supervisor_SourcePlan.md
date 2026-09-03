# Day 11 Source Plan — Multi-Agent Supervisor + SubAgents

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day11-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 13 — Multi-Agent
- **建议时间：** 约 3.5～4 小时；S+
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 今天真正进入 Multi-Agent

目标架构：

```text
                 Main / Supervisor
                  /             \
        Coding SubAgent     Research/Review SubAgent
                  \             /
                   Main synthesize
```

不是为了“Agent 越多越高级”，而是学习：

> **角色分工、权限收窄、Context 边界、结构化 Delegation 与收敛。**

# 2. 今天必须亲手完成

1. 参与定义 Main / Coding / Research 的职责边界。
2. 参与定义各 Agent ToolRegistry 权限。
3. 参与 `DelegationDecision` 结构。
4. 验证：
   - research task → Research；
   - coding task → Coding；
   - mixed task → Main 协调。
5. 故意让 Supervisor 连续委派同一 Agent，验证 `max_delegations` 终止。
6. 看一次 SubAgentResult 如何回父图，而不是倒全量 messages。

# 3. Main / Supervisor

负责：

```text
理解目标
任务拆解
选择 Agent
判断结果是否足够
控制迭代
最终综合
```

Main 不应该亲自承担所有 Coding/Research Tool。

# 4. Coding SubAgent

典型 Tool：

```text
read
write
edit
bash
后续 grep/glob/apply_patch
```

默认不开放 Web Research。

# 5. Research / Review SubAgent

典型能力：

```text
retrieve_knowledge
MCP research/read-only tools
read（按需）
未来 web_search
```

默认不开放 write/bash 高风险能力。

# 6. 为什么不同 Agent 不共用全部 Tool

Multi-Agent 的一个真实价值是：

> **Context 与 Tool Permission 按角色收窄。**

好处：

- 模型选择空间更小；
- Tool 误用概率更低；
- Context 更干净；
- 风险能力更容易治理。

# 7. SubAgent 复用现有 Runtime

不要给每个 SubAgent 重造 Agent Loop。

```text
coding_runtime
= existing AgentRuntime + coding prompt + coding registry

research_runtime
= existing AgentRuntime + research prompt + research registry
```

# 8. Supervisor Structured Decision

不要依赖自由文本：

```text
“我觉得下一步给 coding”
```

使用结构化输出：

```text
next_agent = coding / research_review / finish
task
reason
```

`reason` 只需简短可审计理由，不要求完整思维链。

# 9. SubAgentResult

返回“任务结果”，例如：

```text
agent
status
summary
artifacts
citations
changed_files
tests
unresolved
```

原则：

> SubAgent 交付结果，不把自己的完整内部 Context 倾倒给 Main。

# 10. Context Engineering

Main → Coding：

```text
明确 coding task
必要文件 / artifact refs
必要 constraints
```

Research → Main：

```text
summary
evidence / citations
recommendation
```

Main 再决定给 Coding 哪些必要信息。

# 11. max_delegations

这是 Multi-Agent 层硬兜底：

```text
Agent Loop max_steps
≠
Supervisor max_delegations
```

两者作用域不同。

# 12. AI Coding 可主导

- Agent factory/config 样板；
- Pydantic result DTO；
- routing test fixture；
- role prompt 基础文本。

但角色边界、权限、Delegation、Context contract 不能黑盒。

# 13. Scope Lock

不做：

- Subgraph；
- Session-scoped Sandbox；
- Web Search；
- CRAG；
- Prebuilt Supervisor；
- 无限动态 Agent 创建。

# 14. 完成 Gate

用户必须能说明：

- 为什么要 Multi-Agent；
- Main 与 SubAgent 分别负责什么；
- 为什么不共用所有 Tool；
- 为什么 SubAgent 不返回完整历史；
- Structured Delegation 的价值；
- max_steps 与 max_delegations 的区别。
