# Day 08 Source Plan — Context Governance + Artifact + Compaction

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day08-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 9 — Context
- **建议时间：** 约 3.5～4 小时
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 今天三个真正的核心

今天只要求把三件事情真正建立起来：

```text
① Persistent History ≠ Runtime Context

② Raw Tool Output
   → Artifact 完整保存
   → Model 只拿 Summary + Ref

③ Context 快满
   → Compaction
   → 原历史不删除
```

外围 TokenCounter / CLI / metadata 可以大胆 AI Coding。

# 2. 今天必须亲手完成

1. 亲手画 History 与 Context 的差别。
2. 制造一个巨大 Fake Bash Output。
3. 确认原始输出完整写入 Artifact。
4. 确认 ToolMessage/Runtime Context 只拿 compact result + artifact_ref。
5. 执行一次 compaction，比较 before/after context。
6. 用 `inspect_artifact` 按关键词重新找回原始细节。

# 3. CORE_LEARNING：History ≠ Context

Persistent History：

> 完整事实记录，不因模型窗口不足而删除。

Runtime Context：

> 某一次模型调用真正发送给模型的信息集合。

错误：

```python
messages = messages[-20:]
```

可能丢：

- 用户约束；
- 旧 Tool Result；
- 决策；
- 恢复信息。

正确：

```text
Persistent Store:
完整历史

Model Context:
System
+ structured summary
+ 最近完整 turns
+ relevant refs
+ active skills
```

# 4. Artifact

大输出不能简单：

```text
truncate
→ 永久丢失
```

也不能：

```text
300k stdout
→ 全塞模型
```

目标：

```text
Tool Raw Output
→ ArtifactStore.save()
→ Output Summary
→ compact ToolResult + artifact_ref
→ LLM
```

用户必须懂：

> **完整保存 ≠ 完整注入。**

# 5. inspect_artifact

作为 READ_ONLY Runtime Tool：

```text
artifact_ref
start/end lines
keyword
```

模型先看到 Summary，需要细节再按需检索。

这是 Context Engineering，不是简单“日志文件浏览器”。

# 6. Compaction

主链：

```text
早期完整 Turns
→ 保留完整 Tool Interaction 边界
→ 结构化 Summary
→ Runtime Context 用 Summary 替换
→ Persistent History 不变
```

必须理解：

- 不能把 AI tool_call 与对应 ToolMessage 拆开；
- Summary 需要保留 facts / decisions / constraints / failed attempts / refs 等稳定字段；
- compact 失败时 hard guard 不能继续无脑撑爆窗口。

# 7. AI Coding 主导

这些可由 Claude 主导实现：

- TokenCounter；
- 70% auto compact / 85% hard guard；
- `/budget`；
- `/compact`；
- Artifact metadata table；
- path/hash/size CRUD；
- Summary DTO；
- fallback summarizer；
- CLI presentation。

用户只需要看关键 Diff 和修改入口。

# 8. 用户 Micro Change

可安排：

- 改 auto compact threshold；
- 修改 Summary Schema 一个字段；
- 给 inspect_artifact 增加一个简单参数；
- 观察 context usage 变化。

# 9. Failure / Debug

### A. 巨型 Tool Output
确认 raw 完整，context 不爆。

### B. Summary LLM 失败
必须有 deterministic fallback，不丢 artifact。

### C. Compaction 拆断 Tool Pair
测试必须拒绝这种结果。

### D. Hard Guard
如果 compaction 失败，阻止继续超窗口调用。

# 10. Scope Lock

不做：

- 长期记忆；
- Vector Memory；
- Agent Skill 自动推荐；
- Context optimizer 复杂算法；
- 精确到 1 token 的 provider-agnostic tokenizer；
- LangGraph state compaction。

# 11. 完成 Gate

- [ ] History / Context 能清楚解释；
- [ ] 原始 Tool Output 永久存在；
- [ ] LLM 默认只拿 Summary + Ref；
- [ ] inspect_artifact 能恢复细节；
- [ ] compaction 不删除持久历史；
- [ ] 用户知道 TokenCounter/阈值是外围实现，不会在此过度下钻。
