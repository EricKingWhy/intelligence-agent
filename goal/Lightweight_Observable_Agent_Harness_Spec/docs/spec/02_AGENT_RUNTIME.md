# 02 — Agent Runtime

## 1. 目标

实现一个可读、可测试、Async-first 的最小 Agent Loop。它只协调模型调用、Context、Tool Calling、事件与持久化，不包含具体业务能力。

## 2. 主循环

```text
accept user input
→ append user SessionEvent
→ build runtime context
→ call ModelProvider
→ stream ModelDelta events
→ aggregate complete AIMessage
→ if no tool calls: finalize
→ if tool calls:
     ToolExecutor.execute_batch(...)
     append tool results
     next step
→ until completed / paused / cancelled / failed / guard
```

Agent Loop MUST NOT 知道具体 Tool 怎么执行。

## 3. ModelProvider

抽象至少支持：

```text
invoke / astream
model identity
tool definitions
structured output
usage metadata
error classification
```

默认实现：
- OpenAI-compatible Adapter
- Qwen Adapter
- DeepSeek Adapter

Provider-specific HTTP/SSE 细节 SHOULD 复用 LangChain 或成熟官方 SDK，不自研协议层。

## 4. Streaming

Streaming MUST：

- 对外产生 `ModelDelta`；
- 同时聚合成完整 AIMessage；
- 完整 AIMessage 继续承担 `tool_calls`、SessionEvent、Checkpoint、下一轮输入。

不能因为有 Streaming 就只保存 token/chunk 而失去最终结构化消息。

## 5. Run / Step

建议语义：

- `Session`：跨请求的长期任务/对话容器；
- `Run`：一次用户请求驱动的一轮 Agent 执行；
- `Step`：一次模型决策及其后续 Tool batch。

终止不再由低位 `max_steps` 决定。四类出口各有契约：**完成**（§5.4）、**暂停**（§5.2）、
**取消**（立即语义，不被改写成暂停）、**失败**。`max_steps` 只在迁移期作为
`budget.local.max_agent_turns` 的 deprecated alias 存在（§5.1）。

### 5.1 预算层级与 counter（冻结契约）

三种控制在语义上**互不替代**，实现 MUST NOT 把它们合并成一个计数器：

| 控制 | 作用域 | 语义 | 默认 |
| --- | --- | --- | --- |
| **Local AgentRuntime fuse** | 单个 `AgentRuntime` 实例 | 高位保险丝，按**被接受**的模型决策计数；不跨兄弟池化 | `max_agent_turns = 500` |
| **RunBudget** | 一个逻辑 `run_id`（含它创建的子孙） | 累积账本：turns / model requests / tool calls / tokens / cost / deadline / 每工具配额 | 除显式配置外无 ceiling |
| **SessionBudget** | 会话及其跨 run 的委派后代 | 累积账本；同 run 恢复保留，fork 新建 | 仅 `max_delegations = 8` |

配置优先级固定为 `Deployment > AgentProfile > Session/Run request override > Tool policy`。
**下层只能收窄**：试图越过生效上层 ceiling 的配置 MUST 被**拒绝**（不静默截断）——判定发生在
**任何 model / tool / child 工作开始之前**（状态码口径见 `11 §6.1`）。

七个 counter 的定义与**唯一计数点**：

| counter | 定义（计数点） |
| --- | --- |
| `agent_turns` | 一个 AgentRuntime 内**被接受**的模型决策数。决策在 Provider 响应被规范化并**接纳进 loop** 后才计；被拒绝或传输失败的请求不增此数 |
| `model_requests` | 实际发出的**每一次** Provider 请求。primary / fallback / closeout / 子 Agent 请求各自独立计数 |
| `tool_calls` | 被接纳进 `ToolExecutor` 的**规范化逻辑工具调用**数（唯一接纳点）。一次 retry 仍是一个逻辑调用 |
| `tool_attempts` | `ToolExecutor` 的**每一次实际尝试**（含 retry）。可观察，但 MUST NOT 被当成 `tool_calls` 的别名 |
| `total_tokens` | 配置作用域内 Provider 自报的 input + output token 之和 |
| `cost_usd` | Provider 归属的 USD 成本之和；**不可得时记 unavailable，MUST NOT 编价、MUST NOT 记 0** |
| `delegations` | 委派树内被接纳的 `delegate` 调用数 |

**兼容与迁移**：公开字段是 `budget.local.max_agent_turns`。只发 `max_steps` 的旧客户端 ⇒ 解释为
根 AgentRuntime 的 local fuse；两者同时出现且**相等** ⇒ 接受；**不等** ⇒ 422（开工前）。`max_steps`
的**删除**是 contract 阶段的独立票，且必须有真实 Live Gate 通过作前置（§12 §9.1）。
公开 `budget` 对象的字段形状、校验规则与投影字段见 `11 §6.1`。

### 5.2 暂停与恢复（durable 边界，不是终态）

- 命中预算、deadline 或 stuck 时，Runtime MUST 落**一个** `run/paused`（持久化、**非终态**），
  MUST NOT 落 `run/completed` / `run/failed` 来顶替它。暂停停止活动执行，但**不**关闭逻辑 `run_id`。
- **closeout 预留**：暂停成立前，在适用预算内**预留**容量，给模型**一次有界**机会产出 continuation
  （已完成 / 剩余 / 阻塞 / 下一步安全动作）。预留容量**不是**额外不记账的工作。
- 模型 closeout 不可用或产出无效 ⇒ 落**确定性** continuation：只用已持久化事实组装，
  MUST NOT 伪造进展、成功或工具结果。
- **恢复沿用同一 `run_id`**，接受**绝对** ceiling 与 `expected_version`，MUST NOT 重置任何 counter
  或 stuck 指纹；版本过期 / 把 ceiling 降到已消耗之下 / 缺少必要变更依据 / 存在未 reconcile 副作用
  ⇒ 拒绝且不启动任何工作。并发恢复同一版本 ⇒ 至多一个生效。
- 事件字段、投影字段与状态枚举的契约见 `03 §3.4`、`11 §6.1`。

### 5.3 Stuck 检测（五模式，扩展既有 guard 责任域）

默认阈值：

| 模式 | 阈值 |
| --- | ---: |
| 同动作 + 同错误/结果失败 | 3 |
| 同动作 + 同观察 | 4 |
| 无进展独白 / 模型决策 | 3 |
| 两模式交替循环 | 6 个决策 |
| 项目级无进展窗口 | 4 个决策 |

规则：

- 责任域**唯一**：扩展 §6 的既有护栏（ADR-0014），MUST NOT 新增第二个 loop guard。
- 阈值**首达** ⇒ 发一条结构化 guard 事件并允许**恰好一次**纠正性 replan；
  replan 后同一模式仍持续且**无进展证据** ⇒ 落 `run/paused(reason=stuck)`。
- 指纹 MUST **规范化**（call id、无关格式、等价参数抖动、修饰性文字变化都 MUST NOT 构成进展），
  MUST **脱敏**（凭证/密钥值 MUST NOT 进指纹或持久化），并跨进程重启与同 run 恢复**保持**。
- 只有出现相关进展时才复位**受影响的那一个**模式。
- 恢复前置：**相关** steer 事件、**比暂停快照更新**的工作区/环境 revision、或**比暂停快照更新**的
  policy/profile 版本，三者其一；缺依据的 plain continue 与无关变更 ⇒ 拒绝（409）。

### 5.4 完成判定：Quiescence 强制 + CompletionPolicy 可插拔

Core 提供**一个** `CompletionPolicy` seam，位置在既有的最高完成边界上。调用它**之前**，
Runtime Quiescence MUST 先证明六条（缺一不可）：

1. 没有已接纳但缺结果或恢复分类的工具调用；
2. 没有未决的 `ApprovalRequest`；
3. 没有活动或待恢复的子 Agent；
4. 没有 pending / unknown 且未定 reconcile 状态的 `Operation Ledger` 记录；
5. 没有 pending 的 reconcile 操作；
6. 最新被接纳的模型决策不再请求新的工具调用。

默认通用策略 = 静止后接受最终模型响应。域策略可以**增加**完成证据，但 MUST NOT 绕过 quiescence、
权限或账本检查；MUST NOT 把缺失结果或未知副作用当成完成。不静止时 MUST NOT 落 `run/completed`——
应保持未解 owner 活动，或进入准确的暂停 / 恢复 / reconcile 状态；未解工作结清后**重入同一完成闸门**，
且终态事件仍然**恰好一个**。

## 6. Repeated Tool Guard

除了 `max_steps`，实现更具体的重复调用保护：

```text
same tool
+ same critical args
+ no relevant state/resource change
+ repeated N times
→ REPEATED_TOOL_CALL
```

MUST NOT 粗暴禁止所有重复 read。若发生 edit 后再次 read，状态已变化，允许重复。

本节同时也是**多模式 stuck 检测（§5.3）的唯一责任域**：扩展它时 MUST NOT 再引入第二个 loop guard。

## 7. Model Fallback

Model Fallback 只在 Provider 调用域触发。

允许：
- timeout
- 429
- provider unavailable
- 明确 transient provider error

禁止因为：
- Tool 参数错误
- 普通业务失败
- “答案看起来不好”
- Permission denied

就切换模型。

Fallback 每次发生 MUST 记录：
- primary provider/model
- fallback provider/model
- reason
- attempt
- run_id / step_id

## 8. Error Semantics

Runtime 应明确区分：
- model deterministic/config error；
- model transient error；
- tool deterministic error；
- tool transient error；
- cancellation；
- guard stop；
- recovery required。

不要用自由文本字符串推断异常类型。

## 9. MUST NOT

- 不得在 Loop 中硬编码 RAG/Finance/Coding。
- 不得在 Loop 中直接 `print()` 业务执行状态；必须 emit event。
- 不得把 LangGraph 作为 Loop 本身。
- 不得由 ModelProvider 重试 Tool。
- 不得无限自循环。
- 不得把具体工具（Git / pytest / Todo / 某个 Coding Tool）的完成规则硬编码进 Core——完成判定只能经 §5.4 的 Quiescence + `CompletionPolicy` seam。
- 不得让 `CompletionPolicy` 绕过 quiescence、权限或 Operation Ledger 检查。
- 不得用 Prompt 文本表达预算或权限边界（边界是 Runtime 事实，不是提示词）。
- 不得新增第二个 loop guard，也不得新增第二条 Tool 执行路径。

## 10. Acceptance Criteria

- 模型无 Tool 时正常结束；
- 单 Tool、多 Tool 均可循环；
- streaming 后 tool_calls 不丢；
- `max_steps` 可终止（迁移期作为 alias，见 §5.1）；
- repeated-tool guard 在无状态变化的重复调用时触发；
- Model transient failure 可 fallback；
- auth/config 错误不会无限 fallback；
- 每个 Step 都能在 SessionEvent/Diagnostic Log 中定位；
- 无显式多维预算时 local fuse 解析为 `max_agent_turns=500`，且可被 Deployment/AgentProfile 收窄；
- 超过生效上层 ceiling 的配置在**任何工作开始前**被拒（不是静默截断）；
- `agent_turns` / `model_requests` / `tool_calls` / `tool_attempts` 四者计数互不混同（含 fallback、retry、子 Agent 与 closeout 请求）；
- 命中预算 / deadline / stuck 时只落 `run/paused`，该逻辑 run 不出现 `run/completed` / `run/failed`；
- 同 `run_id` 恢复不重置任何 counter 与 stuck 指纹；版本过期或并发恢复只允许一个生效，其余被拒且不开工；
- §5.3 五个模式各自可测：首达恰好一次 replan，持续则 `PAUSED_STUCK`，无依据 continue 被拒；
- §5.4 的六条 quiescence 各自独立阻断 `run/completed`，全部静止后才允许唯一终态事件；
- 委派树共享 RunBudget/SessionBudget 且 `max_delegations=8` 生效（子 Agent 创建/恢复不重置、不放大）；
- replay 消耗零预算且不调 Provider/Tool；fork 得到新 SessionBudget 身份 + 父快照/lineage。
