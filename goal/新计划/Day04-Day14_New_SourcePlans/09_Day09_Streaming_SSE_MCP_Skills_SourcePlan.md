# Day 09 Source Plan — Event / Streaming / FastAPI SSE + MCP / Skills + V1 Smoke

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day09-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 10 + Module 11
- **建议时间：** 约 4～5 小时；轻量内容可合并
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 今天最终工程目标

今天把 Single Agent V1 从“能运行”升级成：

```text
Runtime 可对外持续发事件
+
CLI 可实时显示
+
FastAPI 可通过 SSE 流式推送
+
Remote MCP Tool 可进入统一 Tool Runtime
+
Skill 可按需注入
+
完成一次中期 Smoke E2E
```

核心部分是 Streaming/Event 与 MCP 主链；Skills 轻量。

# 2. 主架构

```text
AgentRuntime
→ AgentEvent Stream
   ├─ CLI Renderer
   ├─ FastAPI SSE
   ├─ Test Recorder
   └─ 后续 Langfuse
```

模型：

```text
model.astream()
→ ModelDelta
→ yield event
→ 累积完整 AIMessage
→ Tool Calling / persistence 继续
```

# 3. 今天必须亲手完成

1. 跑一次 CLI Streaming。
2. 调一次最小 FastAPI SSE endpoint。
3. 实时看到 ModelDelta。
4. 实时看到 ToolStarted / ToolCompleted。
5. 断开一次 SSE 客户端，观察生成器/任务行为。
6. 接一个最小 Remote MCP Tool。
7. 检查 MCP Tool 最终仍经过项目 ToolExecutor。
8. 加载并跑通一个 `SKILL.md`。
9. 最后跑一次 30～60 分钟以内的 V1 Smoke E2E。

# 4. CORE_LEARNING：Event ≠ Log

Event：

> Runtime 对外发布的业务执行事件，可以被 UI/SSE/Test/Trace 消费。

Log：

> Debug / 运维记录，可以包含更内部、更细的实现信息。

不要把两者完全绑死。

Runtime 不应直接：

```python
print("tool started")
```

而是：

```text
Runtime
→ ToolStarted Event
→ Renderer/SSE 决定怎么展示
```

# 5. AgentEvent

类型可包括：

```text
AgentStarted
ModelStarted
ModelDelta
ModelCompleted
ToolStarted
ToolCompleted
ToolFailed
CheckpointSaved
ContextCompacted
RecoveryRequired
AgentCompleted
AgentFailed
```

DTO 样板可 AI Coding。

用户重点理解：
- Event 在哪里产生；
- 谁消费；
- Event 与状态持久化顺序的关系。

# 6. Streaming 核心

不能只追求“字一个字出来”。

真正核心：

```text
astream
→ chunks/delta
→ 对外 ModelDelta
→ 最终聚合成完整 AIMessage
```

完整 AIMessage 仍然需要用于：

- `tool_calls`；
- message persistence；
- checkpoint；
- 下一轮 Agent Loop。

因此 Streaming 是输出形态变化，不应破坏 Tool Calling 协议。

# 7. FastAPI SSE

只做最小应用层：

```text
POST /chat/stream
或等价 endpoint
→ AgentEvent async stream
→ SSE data frames
```

要看懂：

- async generator；
- event serialization；
- 客户端断开；
- Runtime task / generator 清理基本边界。

不扩展：
- 登录；
- Web UI；
- 完整前端；
- 多实例生产部署。

# 8. AI Coding 主导

- AgentEvent DTO；
- CLI Renderer；
- SSE frame formatting；
- FastAPI route plumbing；
- event order test；
- 客户端断开基础处理。

用户看主链和关键 Diff。

# 9. MCP 核心

必须看懂：

```text
Remote MCP Server
→ MCP Client
→ Tool Discovery
→ metadata/schema
→ MCPToolAdapter
→ ToolRegistry
→ ToolExecutor
→ remote invoke
→ ToolResult
```

核心原则：

- Agent 是 MCP Client；
- 不自己写 MCP wire protocol；
- 不用现成 `create_agent` 隐藏自己的 Runtime；
- MCP Tool 也必须进入统一 Contract/Registry/Executor；
- 远程 Tool side effect 默认不能靠名字猜；
- MCP Client SDK Retry 与 ToolExecutor Retry 不能叠加。

# 10. Skills

只学到应用充分：

```text
Tool
= 可执行能力

Skill
= 按需加载的指导 / 方法 / 知识
```

SkillLoader：

```text
discover
→ metadata
→ load
→ ContextManager 注入
```

实现主要 AI Coding。

只需要跑通一个 `SKILL.md`，不做 Marketplace / 推荐系统。

# 11. V1 Smoke E2E

这是中期 Smoke，不是毕业考试。

建议任务：

```text
根据知识库规则
→ read toy project
→ edit
→ bash pytest
→ 必要时调用一个 MCP Tool
→ Final + Citation
```

确认：

- Session 已持久化；
- Checkpoint/Operation 可查；
- Artifact 存在；
- CLI/SSE 能看到过程；
- Citation 正常。

如果有条件，可做轻量 resume 检查，但不要重复 Day7 那种完整 Recovery 大实验。

# 12. Scope Lock

今天不做：

- LangGraph；
- Multi-Agent；
- Web Search；
- Langfuse；
- EvalScope；
- Skill Marketplace；
- MCP Server 自研；
- 完整 Web App。

# 13. 完成 Gate

- [ ] Streaming 不破坏完整 AIMessage；
- [ ] Event 与 Log 区别清楚；
- [ ] 最小 FastAPI SSE 跑通；
- [ ] 客户端断开至少观察一次；
- [ ] MCP Tool 走统一 ToolExecutor；
- [ ] Skill 与 Tool 区别清楚；
- [ ] V1 Smoke E2E 主链能跑通。
