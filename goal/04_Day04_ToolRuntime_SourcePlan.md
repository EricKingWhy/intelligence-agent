# Day 04 Source Plan — Tool Runtime 完整闭环

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day04-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 4 — Tool Runtime
- **建议时间：** 约 3.5～4.5 小时；必要时延续
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 今天真正要获得的工程能力

把 Day3 的最小 Agent Loop 从“临时函数/字典执行 Tool”升级成统一、可扩展、可测试的 Tool Runtime：

```text
LLM Tool Call
→ Tool Contract
→ ToolRegistry
→ Pydantic Validation
→ ToolExecutor
→ Timeout / Error Classification / Retry
→ Batch Scheduling
→ ToolResult
→ ToolMessage
→ Agent Loop 下一轮
```

今天结束后，Agent Loop 不再知道某个具体 Tool 怎么执行，只知道：

```python
executions = await tool_executor.execute_batch(ai_message.tool_calls)
```

这是后续 Local Tool、Knowledge Tool、MCP Tool 共用的执行底座。

# 2. 为什么把旧 Day04 + Day05 合并

旧计划把：

```text
Tool Contract / Registry / ToolResult
```

和：

```text
ToolExecutor / Validation / Retry / Concurrency
```

拆成两天。

新版认为它们是同一条自然工程闭环，因此合并到同一个 Module / 学习日。

**注意：合并 Day 数，不等于压缩核心认知步骤。**

Tool Runtime 属于 S 级，仍然需要细拆主链、Failure、Debug 和核心设计边界。

# 3. 今天必须亲手完成

至少包含以下动手点：

1. 亲手补/改一个 Tool Args Schema，并观察最终模型侧 Tool Schema。
2. 亲手参与 Agent Loop 从临时 Tool 执行切换到 `ToolExecutor` 的接线。
3. 故意制造一次 `INVALID_ARGUMENT`，确认 Executor 不做无意义 Retry，而是把错误作为 ToolResult 回给模型。
4. 故意制造一次 timeout/transient error，观察 Retry attempt。
5. 验证一批 READ_ONLY Tool 并发；混入 MUTATING 后按原顺序串行。
6. 从 JSONL 中找到一次 `tool_call_id / tool_name / attempt / error_code / duration`。

# 4. CORE_LEARNING：必须真正看懂

## 4.1 Tool Contract

核心字段应覆盖：

```text
name
description
args_schema
timeout_seconds
side_effect
execute()
```

真正要懂：

- Contract 为什么同时服务“模型 Tool Definition”和“Runtime Tool 对象”。
- 为什么 Schema 不只是类型，description 也会直接影响 Agent 行为。
- 为什么模型侧 Schema 与 Runtime Tool 必须来自同一份 Contract，避免名称/参数漂移。

不要深入：
- Pydantic Core 内部实现；
- JSON Schema 标准全部细节。

## 4.2 ToolRegistry

职责只应包含：

```text
register
get
list
export model tool definitions
```

必须理解：

> Registry 是“工具目录/路由表”，Executor 才负责真正运行。

Registry 不负责：

- Retry
- Timeout
- Session
- Permission policy 的完整执行流程
- LLM 决策

重复 Tool Name 应尽早失败，因为 Tool Name 是模型返回 Tool Call 的路由 Key。

## 4.3 ToolResult

建议保持统一结构：

```text
ok
message
data
error_code
retryable
metadata
artifact_ref
```

用户必须理解：

- 为什么不能所有 Tool 都返回随意字符串；
- 为什么结构化结果更适合模型自纠错、日志、恢复、测试；
- `error_code` 与 `retryable` 是 Runtime 语义，不应靠字符串猜。

ErrorCode 至少覆盖：

```text
INVALID_ARGUMENT
TIMEOUT
TRANSIENT_ERROR
PERMISSION_DENIED
NOT_FOUND
EXECUTION_ERROR
CANCELLED
UNKNOWN
```

具体枚举样板可 AI Coding。

## 4.4 Validation

主链：

```text
LLM Tool Call args
→ tool.args_schema.model_validate(...)
→ valid → execute
→ invalid → INVALID_ARGUMENT ToolResult
→ ToolMessage
→ LLM 下一轮自己修参数
```

必须理解：

> Executor 不应该偷偷替模型改参数。

参数错误是确定性输入错误，重复执行相同参数没有价值。

## 4.5 Retry

只对明确暂时性错误做 Tool Retry：

```text
TIMEOUT
TRANSIENT_ERROR
临时服务不可用
```

默认不重试：

```text
INVALID_ARGUMENT
PERMISSION_DENIED
NOT_FOUND（V1 默认）
明确配置错误
```

必须理解 **Single Retry Layer**：

```text
Agent Retry
× ToolExecutor Retry
× SDK Retry
```

会造成 Retry Amplification。

本项目 ToolExecutor 是 Tool 执行域的统一 Retry 决策层。

Backoff 可以使用轻量实现，例如：

```text
0.5s
1.0s
```

不要把 Backoff 算法本身拆成课程。

## 4.6 Timeout

理解：

```text
Executor
→ 为具体 Tool 加 timeout boundary
→ TimeoutError
→ 映射 ToolResult
→ 根据 Retry Policy 决定下一步
```

不要研究 asyncio timeout 内部调度机制。

## 4.7 Batch Scheduling

规则冻结：

```text
同一批全部 READ_ONLY
→ asyncio.gather 并发

只要包含一个 MUTATING
→ 整批按模型 tool_calls 原顺序串行
```

V1 不做复杂依赖分析。

必须理解为什么这是安全、简单、可解释的工程折中。

## 4.8 Tool Result 配对

即使并发完成顺序不同：

```text
call_2 完成
call_1 完成
call_3 完成
```

回填模型时仍必须依据原始：

```text
tool_call_id
```

严格配对，建议按原 Tool Call 顺序组织。

# 5. AI_CODING_PRACTICE：可以让 Claude 主导

可交 AI Coding：

- ToolSideEffect / ErrorCode 枚举样板；
- Pydantic DTO；
- Exception Mapping helper；
- Fake Tool；
- 大部分 pytest fixture；
- backoff 辅助函数；
- 日志字段样板。

Codex 在最终 Day Plan 中不要让这些内容占据独立 CORE Task。

# 6. 推荐工程 Task 粒度

Codex 可根据代码现状压成约 4～5 个真实 Task，例如：

```text
Task A — Tool Contract + Registry + Schema
Task B — ToolResult + Validation + Error Mapping
Task C — ToolExecutor Timeout / Retry
Task D — Batch Scheduling + tool_call_id pairing
Task E — Agent Loop 接入 + Failure / Log 验证
```

这是参考，不要求机械固定 5 个。

# 7. 必做 Failure / Debug

至少选择三个：

### Failure A：重复注册 Tool Name
应在注册/启动阶段明确失败。

### Failure B：参数错误
确认：
- Tool 实际 execute 次数为 0；
- Executor 不重试；
- 错误回给模型。

### Failure C：Timeout
Fake Tool sleep 超时，观察 attempt 和最终 ToolResult。

### Failure D：Permission / deterministic error
确认只执行一次。

### Failure E：Concurrency
三个 READ_ONLY Fake Tool 各 sleep 一段时间，验证并发明显快于串行。

### Failure F：混入 MUTATING
记录执行顺序，必须按原 Tool Call 顺序执行。

# 8. 最小测试集合

测试重点在行为，不追求测试数量：

- Registry register/get/list；
- duplicate name；
- ToolResult JSON serializable；
- Schema name/description/parameters；
- Validation before execute；
- transient retry；
- deterministic no retry；
- timeout；
- READ_ONLY concurrency；
- MUTATING serialization；
- result/tool_call_id pairing；
- Agent Loop 使用正式 ToolExecutor。

# 9. Scope Lock

今天不要进入：

- Docker Sandbox；
- read/write/edit/bash 真实 Tool；
- Session / Checkpoint；
- Operation Ledger；
- MCP；
- RAG；
- Model Fallback；
- 复杂 Retry Budget；
- Dependency Graph。

# 10. 完成 Gate

Day4 / Module4 只有满足以下条件才算完成：

- [ ] Agent Loop 已改为统一 ToolExecutor；
- [ ] Registry / Executor 职责说得清；
- [ ] 参数错误回模型而不是 Executor 瞎重试；
- [ ] Tool Retry Layer 唯一；
- [ ] READ_ONLY 并发 / MUTATING 串行已验证；
- [ ] `tool_call_id` 配对正确；
- [ ] 至少做过一次 Failure Debug；
- [ ] 用户能口述整条 Tool Runtime 主链。

若未完成，不要因为 Day5 到了就跳 Sandbox。
