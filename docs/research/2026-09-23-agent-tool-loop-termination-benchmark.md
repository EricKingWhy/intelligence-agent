# Agent / Tool Loop 终止与防死循环机制调研

> 日期：2026-09-23  
> 范围：替代“固定 10 次工具调用后失败”的成熟机制；仅研究，不修改规格、代码、Ticket 或 Issue。  
> 证据标准：优先官方文档、官方仓库源码和官方产品文章。文中 `[事实]` 表示来源直接支持；`[推断]` 表示基于这些事实对本项目的设计归纳，不声称上游产品按该归纳实现。

## 1. 结论先行

`max_steps=10` 不应继续承担“正常任务容量”和“死循环保护”两种职责。成熟实现普遍保留一个很高或可配置的硬兜底，同时用更具体的条件提前终止：自然完成、按工具/错误模式的重复检测、token/cost/time 预算、人工审批或中断、持久化后的暂停/恢复。

最关键的竞品证据是：

1. **OpenAI Agents SDK 的确默认 `max_turns=10`，但它计算的是 LLM agent-loop turns，不是 10 个工具调用，并允许 `None` 禁用；达到上限默认抛异常，也可通过错误处理器返回受控结果。**同一个模型 turn 可包含并行工具调用，因此“turn”和“tool call”不能混为一谈。[官方 Runner 文档](https://openai.github.io/openai-agents-python/running_agents/)；[官方 `RunState` 源码文档](https://openai.github.io/openai-agents-python/ref/run_state/)
2. **OpenAI Codex 产品不是按 10 次工具调用封顶。**官方文章明确说一次 conversation turn 里可能有“hundreds of tool calls”，并在 token 超阈值后自动 compaction 后继续。这证明“Agents SDK 默认 10”不是 Codex 长任务产品的设计上限。[OpenAI《Unrolling the Codex agent loop》](https://openai.com/index/unrolling-the-codex-agent-loop/)
3. **Anthropic Claude Agent SDK 的 `max_turns` 默认是 `None`，另有独立的 `max_budget_usd`；Claude Code 的非交互模式才提供显式 `--max-turns`。**此外，Stop Hook 可以在 Agent 自称完成时做确定性验证并要求继续，但 Claude Code 对连续 8 次阻止停止有内建熔断，避免“完成验证器”自己制造无限循环。[Agent SDK Python reference](https://code.claude.com/docs/en/agent-sdk/python)；[Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)；[Hooks reference](https://code.claude.com/docs/en/hooks)
4. **LangGraph 把 recursion limit 定义为安全网，而不是业务完成条件。**官方建议图本身拥有显式终止边，并可通过 `RemainingSteps` 在硬上限前转入 graceful fallback；Python 1.0.6 起默认 recursion limit 是 1000 super-steps。[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
5. **AutoGen 将业务停止条件做成可组合、可持久化的状态对象。**`MaxMessageTermination`、`TokenUsageTermination`、`TimeoutTermination`、文本/移交/外部终止等可以 `AND` / `OR` 组合；一次 run 停止后，团队状态仍可继续或保存/加载。[AutoGen Termination](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/tutorial/termination.html)；[Managing State](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/tutorial/state.html)
6. **成熟 coding-agent 会检测具体的“卡住模式”。**OpenHands 官方 SDK 默认每 run 允许 500 iterations，却同时默认启用 stuck detection；其检测相同 action+observation、相同 action+error、agent monologue 和交替循环，默认阈值分别为 4、3、3、6，并在 action-error 首次达到阈值时先 nudge、再继续重复才置为 STUCK。[StuckDetector 源码](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py)；[阈值源码](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/types.py)；[LocalConversation 源码](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py)

因此，有依据的总体方向不是“把 10 改成 20/50”，而是：

```text
正常终止：模型给出 final 且无 tool calls / 验收条件已满足
软保护：重复调用、重复错误、无状态变化 → nudge / replan / 降级 / 请求人工
资源预算：model calls、tool calls、token、cost、wall-clock、per-tool timeout 分开计数
硬兜底：很高但有限的 run-level max_steps（最后一道保险丝）
可恢复暂停：预算或人工边界触发时保存 continuation，允许提高预算或用户确认后续跑
用户控制：cancel / interrupt / steer 始终可用
```

这是本调研的 `[推断]`，不是宣称某一个竞品完整采用了上述全部层级；它是多个成熟实现的交集。

## 2. 先校准计数单位

当前问题表述是“调用 10 次工具就停”。但上游框架至少存在四种不同计数单位：

| 单位 | 代表实现 | 语义与陷阱 |
| --- | --- | --- |
| LLM turn / model call | OpenAI Agents SDK `max_turns`、Google ADK `max_llm_calls` | 一次模型响应可能发出多个并行工具调用；不能等同于 tool count |
| tool call | LangChain `ToolCallLimitMiddleware` | 可全局或按特定工具限流；适合昂贵/高风险工具，不适合一刀切所有工具 |
| graph super-step / iteration | LangGraph `recursion_limit`、OpenHands `max_iteration_per_run` | 一步可能运行多个 node/tool；主要是 runtime safety net |
| message / team turn | AutoGen `MaxMessageTermination`、team `max_turns` | 面向多 Agent 交替，不直接等同于单 Agent 的工具次数 |

`[事实]` 本仓当前 `AgentRuntime.max_steps` 统计的是**模型决策轮次**：每轮聚合模型响应，若有 tool calls 则执行 batch 后进入下一轮；Web 与 Session 创建入口默认传 `10`，API 校验范围为 `1..200`，而 `AgentRuntime` 构造默认是 `20`。相关位置：`src/agent_harness/agent/runtime.py`、`src/agent_harness/web/app.py`、`src/agent_harness/session/service.py`、`src/agent_harness/cli.py`。

`[推断]` 产品文案和配置名若继续叫“10 次工具调用”，会掩盖 batch 内多个工具调用的事实，也会造成指标、计费和用户预期错误。PRD 必须先统一 `step/model_call/tool_call` 三者定义。

## 3. 产品与框架逐项证据

### 3.1 OpenAI Codex 与 OpenAI Agents SDK

#### 可验证事实

- Agents SDK Runner 的循环是：模型调用 → final/handoff/tool calls → 再次模型调用；超过 `max_turns` 抛 `MaxTurnsExceeded`，`max_turns=None` 可禁用。当前 Python `RunState` 默认值为 10。[Runner 文档](https://openai.github.io/openai-agents-python/running_agents/)；[`RunState` reference](https://openai.github.io/openai-agents-python/ref/run_state/)
- `max_turns` 是 agent-loop turns / LLM calls，不是单个 tool 的调用次数；同一模型响应可以产生多个工具调用，SDK 另有工具并发上限。[Runner 文档](https://openai.github.io/openai-agents-python/running_agents/)
- 达到 `max_turns` 时可注册 `error_handlers["max_turns"]` 生成受控 final output，而非只能异常退出；该 handler 不重放 tool side effects。[Runner “Errors and recovery”](https://openai.github.io/openai-agents-python/running_agents/)
- Runner 支持将暂停或 `cancel(mode="after_turn")` 后的 `RunState` 重新传入以恢复；工具审批也使用可恢复 RunState。[Runner 文档](https://openai.github.io/openai-agents-python/running_agents/)
- SDK 自动聚合每次 run 的 requests、input/output/total tokens，文档明确说可据此 enforce limits，但 SDK 页面没有宣称内建通用 token/cost 熔断器。[Usage 文档](https://openai.github.io/openai-agents-python/usage/)
- Codex 官方文章明确描述一次用户 turn 内可能有数百次工具调用；当 token 数超过 `auto_compact_limit` 时自动压缩 conversation 后继续。[OpenAI Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/)

#### 推断与边界

- `[推断]` SDK 默认 10 适合作为通用库的保守默认，不足以证明 Codex 产品应该把复杂 coding task 限制在 10；Codex 自身的一手资料反而证明其长任务容量远高于此。
- `[推断]` 本项目若保留硬步数，应像 SDK 那样允许 run/request 层覆盖并返回结构化停止原因；若需要真正长任务，则还必须具备 Codex 式 context compaction，否则只是把撞墙位置后移。
- 未找到 Codex CLI 官方公开的固定“每 turn 最大工具调用次数”默认值；不能编造一个数字。

### 3.2 Anthropic Claude Code / Claude Agent SDK / Managed Agents

#### 可验证事实

- Python Agent SDK 的 `ClaudeAgentOptions.max_turns` 默认 `None`，定义为“Maximum agentic turns (tool-use round trips)”；`max_budget_usd` 也默认 `None`，按本次调用的客户端成本估算停止，resume 前的历史成本不计入本次预算。[Agent SDK Python reference](https://code.claude.com/docs/en/agent-sdk/python)
- SDK 用 `terminal_reason` 区分 `completed`、`max_turns`、`api_error`、`aborted_streaming`、`aborted_tools`；达到用户设置的 turn/cost limit 返回相应 `error_*` 结果。[同一 reference](https://code.claude.com/docs/en/agent-sdk/python)
- Claude Code CLI 的 `--max-turns` 仅用于 non-interactive mode；可通过 `--resume` / `--continue` 继续已有会话。[CLI reference](https://code.claude.com/docs/en/cli-reference)
- Stop Hook 在 Claude 准备结束时触发，hook 可返回 `decision: "block"` 或 `additionalContext` 要求继续；输入带 `stop_hook_active`，且连续阻止 8 次后 Claude Code 会覆盖 hook 并结束 turn，以防验证 hook 自己无限循环。[Hooks reference](https://code.claude.com/docs/en/hooks)
- Hooks 配置有 machine user、project、project-local、managed policy、plugin、skill、subagent 等作用域，多个层级合并而非简单覆盖。这证明成熟产品会区分组织策略、项目默认和局部行为。[Hooks “Hook locations”](https://code.claude.com/docs/en/hooks)
- Managed Agents 的 session budget 是 hard ceiling：达到 `max_list_cost` 后，在下一次模型请求前暂停为 `budget_reached`，而不是销毁 session；提高或移除预算后会自动恢复。session idle 时保留 history，并 checkpoint sandbox，可由新 `user.message` 续跑。[Start a session](https://platform.claude.com/docs/en/managed-agents/sessions)；[Events and streaming](https://platform.claude.com/docs/en/managed-agents/events-and-streaming)；[Session operations](https://platform.claude.com/docs/en/managed-agents/session-operations)
- Managed Agents 支持 `user.interrupt` 中断正在运行的模型响应并随后 steer；工具正在运行时中断可能稍后才生效。[Events and streaming](https://platform.claude.com/docs/en/managed-agents/events-and-streaming)

#### 推断与边界

- `[推断]` Claude 体系最值得复用的是“默认允许复杂任务继续 + 用户显式给自动化任务上限 + 完成验证器也有自己的熔断 + 预算耗尽可恢复暂停”，不是照抄某个 turn 数字。
- `[推断]` 对本项目而言，预算达到时直接 `run/failed` 会丢失“可继续”的产品语义；更接近 Managed Agents 的做法是结构化 `PAUSED_BUDGET` / `NEEDS_INPUT`，保留 continuation。
- `max_budget_usd` 是客户端估算，有超额一个调用的可能；不能把它描述成严格账单上限。Anthropic 官方 cookbook 也明确提醒 run 可在检查时已略过 cap。[Scheduled repository reviewer](https://platform.claude.com/cookbook/claude-agent-sdk-scheduled-repository-reviewer-scheduled-repository-reviewer)

### 3.3 LangGraph / LangChain

#### 可验证事实

- LangGraph `recursion_limit` 限制一次执行的 maximum super-steps；达到时抛 `GraphRecursionError`。Python 1.0.6 起默认 1000，可在每次 invoke/stream 的 config 覆盖。[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- 当前 step 可从 metadata 读取；`RemainingSteps` 让 node 在即将耗尽前切到 fallback/总结。官方示例明确建议“graph 有显式 termination conditions，recursion error 仅作 safety net”。[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- LangGraph checkpoint 支持 interrupt 后恢复；HITL 可以 approve、edit、reject tool call。[Human-in-the-loop guide](https://langchain-ai.github.io/langgraph/how-tos/human_in_the_loop/review-tool-calls/)
- LangChain 的 `ModelCallLimitMiddleware` 分 `thread_limit` 与 `run_limit`，到限可 graceful `end` 或 `error`；`ToolCallLimitMiddleware` 也分 thread/run，且可按 tool name 单独限制，超限可 `continue`（只拦该调用并回错误给模型）、`error` 或单工具场景 `end`。[Built-in middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)
- LangChain 当前官方 built-in middleware 有 model/tool call limit，但没有已发布的通用 token-budget middleware；官方仓库中的 TokenBudgetMiddleware 仍是 feature request，不能当作已存在功能。[官方 issue #38842](https://github.com/langchain-ai/langchain/issues/38842)

#### 推断与边界

- `[推断]` 本项目的硬 `max_steps` 对应 LangGraph recursion safety net，而 repeated-tool guard/完成条件应对应显式边或 middleware，不应依赖同一个计数器。
- `[推断]` 按昂贵工具、网络工具或高风险工具设置独立 quota，比“所有工具共享 10 次”更接近 LangChain 成熟做法；`read`、`grep` 与 `bash`/network/mutating tool 不应共享同一稀缺度假设。

### 3.4 Microsoft AutoGen

#### 可验证事实

- AutoGen AgentChat 内建 `MaxMessageTermination`、`TextMentionTermination`、`TokenUsageTermination`、`TimeoutTermination`、`HandoffTermination`、`ExternalTermination` 等；termination condition 是 stateful callable，每次接收自上次调用后的 delta events/messages。[Termination tutorial](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/tutorial/termination.html)
- 条件可使用 `|` / `&` 组合，例如“critic APPROVE 或达到 10 messages”；框架在 `run()` / `run_stream()` 结束后自动 reset 条件。[同一 tutorial](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/tutorial/termination.html)
- group chat 的 `max_turns` 可与 termination conditions 同时使用，任一命中即停；下次 run turn count 从 0 开始，但团队内部状态和 conversation history 保留。[Human-in-the-loop tutorial](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/human-in-the-loop.html)
- agent/team/termination condition state 均可 `save_state()` / `load_state()`，适合无状态 Web 请求间恢复。[Managing State](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/tutorial/state.html)
- AutoGen core tool contract接收 `CancellationToken`，支持协作取消。[Tool reference](https://microsoft.github.io/autogen/dev/reference/python/autogen_core.tools.html)

#### 推断与边界

- `[推断]` AutoGen 最有价值的是“组合条件 + 每 run 重置局部预算 + session/team 状态继续”，可直接反驳“要么无限跑、要么整个任务只准 10 步”的二选一。
- `[推断]` “模型输出某个 TERMINATE 字符串”可以作为软完成信号，但不能作为唯一安全边界；AutoGen 同样建议与 max condition 组合。

### 3.5 Google ADK（补充）

#### 可验证事实

- Google ADK `RunConfig.max_llm_calls` 是**每个 run 的 LLM call 总数**，当前官方源码默认 500，可由 `ADK_MAX_LLM_CALLS` 覆盖；值 `<=0` 代表不限制，并会记录可能导致永不结束的 warning。[`run_config.py`](https://github.com/google/adk-python/blob/main/src/google/adk/agents/run_config.py)
- `LoopAgent.max_iterations` 可设循环次数；未设置则持续到子 Agent escalate。当前源码同时标注旧 LoopAgent 将由 Workflow 取代，不能把它当长期稳定 API。[`loop_agent.py`](https://github.com/google/adk-python/blob/main/src/google/adk/agents/loop_agent.py)
- ADK Runtime 使用 event loop，一次 invocation 可包含多 Agent run、LLM call、tool execution，统一由 `invocation_id` 关联；`temp:` state 仅在 invocation 内存在。[Runtime Event Loop](https://google.github.io/adk-docs/runtime/event-loop/)
- Workflow/HITL 可通过 `RequestInput` 暂停并用 event history replay 或 resumable checkpoint 恢复；文档建议大型图与多步中断使用 resumable mode。[官方 HITL reference](https://github.com/google/adk-python/blob/main/.agents/skills/adk-agent-builder/references/human-in-the-loop.md)

#### 推断与边界

- `[推断]` ADK 的 500 更像防灾上限，不是推荐业务预算。官方 issue 已出现同一错误变换参数不断重试、只能等 500 次 LLM call 兜底的缺陷，这恰好说明“把上限调高”不能代替错误族/无进展检测。[官方 issue #5684](https://github.com/google/adk-python/issues/5684)

### 3.6 OpenHands SDK（重复/无进展检测的强一手样本）

#### 可验证事实

- `LocalConversation` 当前默认 `max_iteration_per_run=500`、`stuck_detection=True`，并另有可选 `max_budget_per_run`；预算检查覆盖 run 内所有 LLM 的累计成本，与 iteration cap 分离。[LocalConversation](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py)
- `StuckDetector` 只扫描最近最多 20 个 events，并从最后一条 user message 之后开始，避免大历史开销和跨用户回合误判。[StuckDetector](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py)
- 默认检测阈值：相同 action-observation 4 次；相同 action-error 3 次；连续 agent monologue 3 次；交替 pattern 6 次。[StuckDetectionThresholds](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/types.py)
- action-error 到阈值时先生成一次环境 nudge；若继续超过阈值才将 execution status 设为 `STUCK`。event equality 忽略 tool_call_id 等每次变化的标识，比较 action/observation/error 的实质内容。[StuckDetector](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py)
- Conversation state/events 可持久化恢复；每次 run 的 iteration cap 不等于 conversation 生命周期总 cap。[ConversationState](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/state.py)

#### 推断与边界

- `[推断]` 本项目规格中的 `same tool + same critical args + no relevant state/resource change` 与 OpenHands action-observation fingerprint 高度同构，有成熟开源依据；但本项目还应利用已有 resource version / changed files / test result，而不是只做事件文本 equality。
- `[推断]` 推荐采用“第一次命中先 nudge/replan，继续重复才 stop”的两阶段策略，依据正是 OpenHands 的现行实现；这比一次重复就熔断更能容忍合法的重读和自修正。

## 4. 两个 WorkBuddy 页面核查

两个分享链接可访问。外层页面通过 `window.__PUBLISH_BOOTSTRAP__` 指向静态 HTML artifact：

- [Agent 长任务：怎么停得住又不忘事 · 图解](https://workbuddy.link/p/eW7sW6kDpzc24aadJ4rD0P?ext2=copy_link) → [静态 artifact](https://workbuddy-space-static.codebuddy.work/page/eW7sW6kDpzc24aadJ4rD0P/0/eli5-agent-longtask.html)
- [Agent 跑几小时为什么不失忆 · 五层上下文压缩图解](https://workbuddy.link/p/F11pWMerWim48Z9WHy22ja?ext2=copy_link) → [静态 artifact](https://workbuddy-space-static.codebuddy.work/page/F11pWMerWim48Z9WHy22ja/0/eli5-context-compaction.html)

它们是用户提供的参考材料，不是厂商一手资料。逐项核查如下：

| WorkBuddy 主张 | 核查结果 |
| --- | --- |
| 长任务不是一个无限循环，而是有界局部循环 + 总预算 + 可恢复续跑 | **方向成立，但属于综合设计归纳。** OpenAI/Anthropic/AutoGen/LangGraph 的一手资料分别支持局部 hard cap、cost/time/token condition、checkpoint/resume；没有单一厂商把它命名为该完整“六层”模型 |
| Codex 一个 turn 可有数百 tool calls，并自动 compaction | **有 OpenAI 官方文章直接支持** |
| Stop/完成验证 hook 本身必须有防无限循环保护 | **有 Claude Code 官方 Hooks 文档直接支持；当前公开值为连续 8 次 block 后覆盖 hook** |
| 连续 3~5 轮无进展即处理 | **数字不是通用行业标准。** OpenHands 一手源码给出具体默认：action-error 3、action-observation 4、monologue 3、alternating 6；应把 3~5 视为设计区间而非通用事实 |
| “单工具 ≤2min、局部循环 20~50、总任务 6~8h”等具体数字 | **未找到这些数字的统一官方依据，不应写成成熟产品共识。** 可作为待压测参数候选，不能作为 PRD 冻结值的证据 |
| Claude Code 的“五层压缩”是官方产品架构 | **页面自己也正确标注为社区归纳，不是官方术语。** 官方可验证的是 compaction 与 tool result/context 管理，不能把五层名称写成 Anthropic 官方机制 |
| Context 不是完整历史；历史/状态/工件应持久化，按需构建 context | **与本项目冻结规格一致，也被 Codex compaction、AutoGen save/load、ADK event/session、Claude session checkpoint 支持** |

## 5. 对本项目 PRD/Ticket 设计的证据化建议

以下均为 `[推断]`，供主 PRD 选择；不是本调研直接修改规格。

### 5.1 推荐方案：高硬顶 + 多维预算 + 语义 guard + 可恢复暂停

这是 OpenHands（500 + stuck detector + cost）、LangGraph（1000 + explicit termination + RemainingSteps）、Claude（turn/cost 分离 + resume）、AutoGen（组合条件 + state resume）的共同方向。

建议的概念模型：

```text
RunBudget
  max_model_calls?       # 模型轮次；高位灾难兜底
  max_tool_calls_total?  # 可选；不要代替 tool-specific quota
  max_tokens?            # 聚合 usage 后在下一次 model call 前检查
  max_cost?              # provider-neutral estimated cost；标注非账单精确值
  deadline_at?           # wall-clock deadline，不用 elapsed tick 重建

ToolBudgetPolicy
  per_tool_call_limit?   # 例如 web/network/昂贵 API
  per_tool_timeout       # 已在 Tool Contract；与 run deadline 取更早者
  retry_budget           # 只属于 ToolExecutor，不能算成 Agent 的自修正 step

ProgressGuard
  exact_repeat           # tool + normalized critical args + resource/version snapshot
  repeated_error_family  # 即使参数轻微变化，也可识别同一 deterministic failure
  no_progress_window     # completed goals / changed resources / test signature 均无变化
  response: NUDGE -> REPLAN_REQUIRED -> NEEDS_INPUT/STOPPED_STUCK

Continuation
  checkpoint + budgets consumed + last progress signature + stop reason
  resume_with(additional_budget | user_input | changed_policy)
```

### 5.2 不推荐的三个方案

1. **只把默认 10 改成 50/200。** Google ADK 官方 issue 和 OpenHands 的双层设计都表明，高 hard cap 只能减少误杀，不能阻止 50/200 次无效循环。
2. **完全删除所有 hard cap。** OpenAI Agents SDK、LangGraph、AutoGen、Google ADK、OpenHands 都保留了某种硬上限或允许调用方设置；完全依赖模型判断没有成熟依据。
3. **只做“相同 tool + 完全相同 args”检测。** Google ADK #5684 的实际 bug 正是模型每次换一个错误路径，exact-match 永远不触发；需再加 error family 与“相关状态是否变化”。

### 5.3 配置层级建议

有依据的层级不是“一个全局 max_steps”：

```text
组织/部署硬策略（不可被请求放宽）
  ↓
AgentProfile 默认预算（不同 main/coding/research 可不同）
  ↓
Session / Run 请求覆盖（只能在硬策略范围内）
  ↓
Tool Contract 的 timeout / quota / retry policy
```

依据：Claude Hooks 有 managed/user/project/local/skill/subagent scope；LangChain 区分 thread/run/tool-specific；OpenAI Runner 是 per-run 参数；Google ADK 是 `RunConfig` + environment default；本项目已有 AgentProfile 与 request `max_steps` 两层。具体 override 合并规则仍需产品裁决。

### 5.4 必须明确的停止状态，而不是都叫 failed

建议至少区分：

- `COMPLETED`：自然完成或验收通过；
- `PAUSED_BUDGET`：资源预算到限，可追加预算恢复；
- `PAUSED_APPROVAL` / `NEEDS_INPUT`：等待人；
- `STOPPED_STUCK`：进展 guard 确认卡住；
- `CANCELLED_USER` / `CANCELLED_DEADLINE`：取消来源不同；
- `FAILED_RUNTIME`：真正异常；
- `NEED_RECONCILE`：副作用状态未知，延续现有冻结架构。

Claude Managed Agents 的 `budget_reached` pause、Agent SDK 的 `terminal_reason`、OpenHands 的 `STUCK` status、AutoGen 的 `StopMessage` 都证明成熟实现不会把所有停止原因压成同一种失败。

## 6. 仍需用户裁决的问题

以下不能靠竞品资料替用户决定：

1. **默认产品承诺是“尽量自动完成”还是“预算可预测优先”？** 前者更接近 Claude Agent SDK 默认不设 turns + 独立成本预算；后者更接近 OpenAI Agents SDK 默认 10 turns。两者都有成熟依据，但面向不同产品场景。
2. **到预算时自动追加一个“收尾模型轮次”，还是立即暂停？** LangGraph `RemainingSteps` 支持预留 graceful fallback；Claude Managed Agents 则在下一次模型请求前暂停。前者用户体验好但会多花预算，后者边界更严格。
3. **哪些 tool 需要独立 quota？** Web/search、远程 MCP、subagent、昂贵 provider、mutating tool 的成本与风险不同；这必须按本产品 capability catalog 决定，不能照抄 LangChain 示例数字。
4. **无进展命中后的自动阶梯允许几次？** OpenHands 有 nudge 后再 stop 的依据，但是否增加 replan、fallback model 或请求人工，是产品行为，需明确。
5. **预算是一次 Run、一次 Session，还是项目/租户周期累计？** LangChain 同时支持 run/thread；Claude Managed Agents 是 session cost；Agent SDK 的 `max_budget_usd` 只算本次调用。需要产品计费与 UX 决策。

## 7. 无法验证或不应写入 PRD 的主张

- 未找到统一行业标准规定 `max_steps` 应为 20、50、200 或其他固定值。
- 未找到 Codex CLI 官方公开的固定工具次数上限；只有“可能数百次工具调用”和自动 compaction 的官方陈述。
- 未找到成熟产品统一采用“连续 3~5 轮无进展”这一数字；只能引用 OpenHands 的具体分类阈值。
- WorkBuddy 页中的“单工具 2 分钟、局部 20~50 步、总任务 6~8 小时”没有逐项一手出处，不能作为冻结默认值。
- “五层 Claude Code compaction”是社区模型，不是 Anthropic 官方命名。
- 成本预算普遍是已完成调用后的聚合/估算，可能略过 cap；若产品要严格不超账单，必须额外设计 provider reservation/预估机制，现有来源不能保证。

## 8. 一句话设计判断

**保留 `max_steps`，但把它降级为高位灾难保险丝；真正替代固定 10 的，是按 model/tool/token/cost/time 分账、基于状态变化的 stuck guard、结构化暂停原因，以及能追加预算/人工输入后恢复的 continuation。**
