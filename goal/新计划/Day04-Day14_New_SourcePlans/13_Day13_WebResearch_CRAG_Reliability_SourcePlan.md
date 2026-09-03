# Day 13 Source Plan — Web Research + 简化 CRAG + Reliability

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day13-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 15
- **建议时间：** 约 3.5～4.5 小时
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 今天目标

让 Research 能力从：

```text
Knowledge sufficient=false
→ 只能说证据不足
```

升级到：

```text
Knowledge
→ insufficient
→ rewrite one query
→ web_search
→ Citation
→ synthesis
```

同时补两个真正值得的 Reliability 能力：

```text
Repeated Tool Guard
+
Model Fallback（实现主要 AI Coding）
```

# 2. 今天必须亲手完成

1. 注册一个独立 `web_search` Tool。
2. 做一次 KB 足够 → 不联网。
3. 做一次 KB 不足 → rewrite → web_search。
4. 最终答案能区分 Knowledge Citation 与 Web Citation。
5. FakeModel 连续产生重复 Tool Call，触发 Repeated Tool Guard。
6. 模拟 transient provider failure，观察 Model Fallback。
7. 查看日志中的 fallback reason / repeated-tool guard。

# 3. 简化 CRAG

今天不要做复杂学术式 CRAG Pipeline。

只保留：

```text
retrieve_knowledge
→ sufficient=false
→ Main/Research 生成一个更适合搜索的 query
→ web_search
→ synthesis
```

不做：
- Multi-query；
- 复杂 query planning；
- 多阶段 Evidence Grader。

# 4. Web Search 必须独立 Tool

禁止：

```python
retrieve_knowledge():
    if no_result:
        secretly_search_web()
```

原因：

- Trace 看不出为什么联网；
- Tool 权限混乱；
- 无法单独 Eval；
- 无法清晰控制 Citation。

正确：

```text
retrieve_knowledge
web_search
```

由 Agent 决定下一步。

# 5. WebSearchProvider

抽象可以薄：

```text
search(query, top_k)
→ WebSearchResult[]
```

结果至少：

```text
title
url
snippet
source
published_at?
score?
```

Provider adapter 代码主要 AI Coding。

# 6. Citation

必须区分：

```text
Knowledge Source
Web Source
```

Citation 只能来自真实 Tool Result。

不能把模型记忆伪装成 Web evidence。

# 7. Versioned Atomic Knowledge Update

只保留设计：

```text
old active=N
→ write N+1
→ embed/insert
→ verify
→ switch active=N+1
→ delete N
```

实现：
- 允许完全 AI Coding；
- 时间不足可以不实现；
- 不作为 Hands-on / Checkpoint。

用户只需要知道 V1 `delete old → insert new` 的窗口风险，以及生产上如何避免。

# 8. Repeated Tool Guard

核心要理解。

已有兜底：

```text
max_steps
max_delegations
```

再增加更具体的：

```text
same tool
+ same critical args
+ no relevant state change
+ repeated N times
→ REPEATED_TOOL_CALL
→ 提示模型换策略
```

不能粗暴禁止所有重复 read，因为 edit 后重新 read 可能合理。

# 9. Model Fallback

实现主要 AI Coding。

必须懂触发边界：

可以：

```text
timeout
429
provider unavailable
明确 transient provider error
```

不应该因为：

```text
答案“不够好”
Tool 参数错误
普通业务失败
```

就切模型。

Tool Retry 与 Model Fallback 属于不同调用域。

# 10. 辅助 Coding Tools

如果项目需要，可 AI Coding 增加：

```text
glob
grep
apply_patch
git_status
git_diff
```

仍走统一 Tool Contract / Executor。

不要花时间逐个手写学习。

# 11. Failure / Debug

### A. KB sufficient
不得联网。

### B. Web provider failure
明确失败，不伪造结果。

### C. Repeat loop
同状态下连续相同 read，Guard 触发。

### D. Provider transient
Primary fail → fallback；日志有原因。

### E. Authentication error
理解它是 deterministic/config 类错误，不应无限 retry。

# 12. Scope Lock

不做：

- 复杂 Circuit Breaker；
- Redis global health；
- Multi-query CRAG；
- 自动 Git commit/push；
- 多 Web Provider 自动竞速；
- Agent 自主安装依赖无限制。

# 13. 完成 Gate

- [ ] KB 足够不联网；
- [ ] KB 不足能联网；
- [ ] Web 是独立 Tool；
- [ ] Citation 可追溯；
- [ ] Repeated Tool Guard 理解并验证；
- [ ] Fallback 只针对合适模型错误；
- [ ] 原子知识更新理解设计即可。
