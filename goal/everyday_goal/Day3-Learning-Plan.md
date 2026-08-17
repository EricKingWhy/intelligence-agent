# Day03 - 从手工协议到透明 Agent Loop

> 状态：Task 1、2、3 全部完成（均 PASS）；Day 3 工程目标达成，待 Git 收尾 + Final Summary。
> 定位：把 Day 2 手工完成的 Function Calling 两轮协议，收进一个直接可读、可测试、能安全终止的异步循环。  
> 执行者：Claude Code 按本蓝图逐 Task 带用户开发；Codex 不实现 Day 3 代码。

## 今日工程目标

在不使用 `create_agent`、`AgentExecutor` 或 LangGraph 的前提下，从零实现最小 `AgentRuntime`。今天结束时，它应能根据每一轮 `AIMessage.tool_calls` 自动决定“继续执行工具”还是“返回最终回答”，支持连续多轮工具调用，并对未知工具、空 content、工具异常和不收敛循环给出可观察结果。

## 今天必须亲手完成

1. 手画一次 `Human → AI(tool_call) → Tool → AI(...) → Final` Message Trace，标清每条消息的产生者。
2. 写出 Agent Loop 最关键的顺序：调用模型、先追加 `AIMessage`、判断 `tool_calls`、串行执行、追加匹配 ID 的 `ToolMessage`、进入下一轮。
3. 用现有 `ScriptedModel + RequestSnapshot` 验证无工具、一次工具、连续两轮工具和 `max_steps` 四种行为。
4. 故意触发未知工具、空 content 但有 tool call、工具异常，依据消息链与日志判断 Runtime 是否正确处理。
5. 最后脱离代码讲清：正常停止条件、`max_steps` 保险丝、`tool_call_id` 配对，以及模型与 Tool Executor 的职责边界。

## 今日主调用链

```text
HumanMessage(user_input)
  → model.ainvoke(messages)
  → messages.append(AIMessage)
  → AIMessage.tool_calls 为空？
      ├─ 是：AgentRunResult(completed, final_text, steps)
      └─ 否：按顺序查找并执行 tools[name](**args)
              → ToolMessage(result/error, 原 tool_call_id)
              → messages.append(ToolMessage)
              → 下一轮 model.ainvoke(messages)
  → 达到 max_steps：AgentRunResult(max_steps_exceeded)
```

`steps` 统一表示“模型被调用的轮数”，不是 Tool 数量；正常完成由“模型不再返回 `tool_calls`”决定，`max_steps` 只是不收敛时的硬兜底。

## 今日不做什么

- 不使用高级 Agent 框架隐藏循环。
- 不实现 Day 4 的 ToolRegistry、正式 ToolExecutor、统一 ToolResult 或 Schema 注册系统。
- 不实现 Retry、Timeout、并行 Tool、Session、Checkpoint、Streaming 或 EventBus。
- Tool 暂时使用 `dict[str, Callable]` 并串行执行，不为未来需求提前抽象。
- 不逐行导读或重构 Day 2 的 `tool_call_demo.py`；其中刻意保留的错误/TODO 继续作为教学样本。
- 不深入 asyncio 调度器、LangChain 内部消息实现或 Provider 网络协议。

## 当前项目起点

- Day 2 已跑通手工两轮协议，现有主链位于 `src/agent_harness/tool_call_demo.py`。
- `tests/scripted_model.py` 已能按剧本返回 `AIMessage`，并快照每次模型请求的 messages；Day 3 直接复用，不另造 FakeModel。
- 当前基线为 `14 passed`，Ruff 全绿；Day 3 新测试不得依赖真实 API 或消耗 Token。
- Runtime 接收“已经具备 tool-call 输出能力的 model”；今天的 `tools` dict 只负责按 `name` 找到并执行 Python 函数。
- 当前工作区已有用户对原始目标文件的移动/替换，不属于 Day 3 代码 Scope，Claude 不应把它混入实现改动。

## Task Map

| Task | 等级 | 工程成果 | Hands-on Action | 编码模式 | 状态 |
|---|---|---|---|---|---|
| Task 1：实现透明最小 Agent Loop | S | 新增最小 `AgentRunResult` 与 `AgentRuntime.run()`；先跑通”无工具直接完成”和”一次 Tool 往返” | 用户亲手完成循环核心分支和消息 append 顺序，再用快照核对第二轮消息链 | 🤝 PAIR WRITE | ✅ DONE（PASS） |
| Task 2：证明连续多轮与步数语义 | S | 用四组确定性剧本锁定无工具、一次 Tool、连续两轮 Tool、`max_steps`；验证循环不是只能跑一次 | 用户补连续两轮剧本、关键快照断言，并做一次反向验证让测试先红后绿 | 🧑 YOU WRITE | ✅ DONE（PASS） |
| Task 3：失败边界、最小日志与 Agent Loop 验收 | S | 未知工具/工具异常被回填为错误 ToolMessage；空 content 不误停；日志可定位 step/model/tool/终止原因；完成 Full Review | 用户制造一个失败，从日志和 Message Trace 定位分支，修复后口述完整主链 | 🤝 PAIR WRITE | ✅ DONE（PASS） |

> 锁定规则：Task 1 完成并通过 S 级 Checkpoint 后，Claude 才把 Task 2 改为 `ACTIVE`；每次只推进一个 Task。

## Current Task

### Task 1 Brief：实现透明最小 Agent Loop

**要完成什么**

- 新建 `src/agent_harness/agent/`，用 `types.py` 保存最小 `AgentRunResult`，用 `runtime.py` 保存直接可读的 `AgentRuntime`。
- `AgentRuntime` 最小输入为 `model`、`tools: dict[str, Callable]`、`max_steps=20`；一次 `run()` 使用一条本地 messages 链。
- 本 Task 先打通两条成功路径：
  1. 模型首轮没有 Tool Call，返回 `completed`；
  2. 模型首轮提出 `add`，Runtime 串行执行并回填，模型第二轮给最终回答。
- 同步写最小行为测试 A/B；测试通过 `ScriptedModel.snapshots` 观察 Runtime 实际发给模型的消息，而不是只断言最终字符串。

**为什么值得做**

Day 2 已经证明模型只会提出 Tool Call，执行与回填属于 Runtime。Agent Loop 的价值就是把这段人工编排变成可重复的控制流；只有亲手写过这层，后续使用 LangGraph 或其他 Agent 框架时才知道框架替你维护了什么、出错应从哪里查。

**完成后的可观察结果**

- 无工具剧本：模型只调用 1 次，结果为 `status="completed"`，`steps=1`。
- 一次工具剧本：模型调用 2 次；第二次快照严格为 `HumanMessage → AIMessage(tool_call) → ToolMessage(同 id)`。
- `AIMessage` 在 Tool 执行前已进入 messages；最终回答来自“没有 tool_calls 的那一轮”，不是 Runtime 自己拼接。
- 原有 14 个测试继续通过，新增测试不访问 Provider。

**核心文件 / Read to Change 路线**

1. 只快速查看 `tool_call_demo.py` 中“模型提议 → 执行 → ToolMessage 回填”的顺序，不修旧代码。
2. 复用 `tests/scripted_model.py` 的顺序响应和 Request Snapshot。
3. 主要改动限定在新建的 `agent/types.py`、`agent/runtime.py`、`agent/__init__.py` 与 `tests/agent/test_agent_loop.py`。

**必要 Why Card**

- **为什么先 append AIMessage：** ToolMessage 必须回答历史中真实存在的 assistant tool request；漏掉它，协议链就变成“只有结果、没有请求”。Debug 先看 messages 类型顺序。
- **为什么按 `tool_calls` 停止：** Tool Calling 时 content 允许为空；`tool_calls` 是否为空才代表模型这轮选择“执行工具”还是“给最终答复”。
- **为什么保留 `max_steps`：** 模型可能反复请求工具而不收敛；它是保险丝，不是正常业务停止条件。今天只理解到“限制模型轮数并返回明确状态”即可。
- **为什么仍用 Async：** 模型调用是 I/O，后续 Streaming 和并行 Tool 会复用异步边界；当前不学习 Event Loop 内部实现。

**实现护栏**

- 每轮固定顺序：`ainvoke → append AIMessage → 判断 tool_calls → 执行/回填`，不能调换。
- 一个 `AIMessage` 若含多个 Tool Call，今天按返回顺序逐个执行并逐个追加 ToolMessage；不并发。
- ToolMessage 必须复用当前 Tool Call 的原始 `id`；不能新生成，也不能按列表下标猜配对。
- `steps` 每成功发起一轮模型调用加 1；Tool 数量不影响它，避免 `max_steps` off-by-one。
- Runtime 代码保持一眼能顺着读完；不引入 Registry、Protocol 层、Hook、Middleware 或通用事件抽象。

**Scope Lock**

- 本 Task 只实现成功路径 A/B 与所需最小类型/测试。
- 未知工具、工具异常、空 content 实验、完整日志在 Task 3，当前不提前施工。
- 连续两轮 Tool 和 `max_steps` 的完整行为矩阵在 Task 2；当前只保留循环与计数所需的最小结构，不提前写其测试。
- 不改 `tool_call_demo.py`、ModelProvider、CLI、配置系统、日志底座或 Day 1/2 计划。

**编码模式**

`🤝 PAIR WRITE`：Claude 给出最小文件骨架与测试外壳；用户亲手完成 `run()` 中的核心循环分支、AIMessage append、ToolMessage 构造和 A/B 关键断言。

**当前 Skill / 施工许可**

- 这是新 Agent 模块：Claude 维护主 Spec Kit 产物，按需执行 `/speckit.specify → /speckit.plan`；不再创建第二套规划文件。
- 只有用户手动执行 `/speckit.implement` 后，才施工当前 Task 1；该命令不授权 Task 2/3。
- 当前 Task 未完成前不运行整日 `/review` 或 `/qa`。

**Hands-on Action（约 10～15 分钟）**

1. 先在纸上写 `M0 Human → M1 AI(call_1) → M2 Tool(call_1) → M3 AI(final)`，标出谁产生每条消息。
2. 在 Claude 提供的骨架中亲手补全四个关键动作：追加 AIMessage、判断 `tool_calls`、按 name 调用函数、用原 id 构造 ToolMessage。
3. 运行 A/B 测试，打印或检查第二次 Snapshot；随后临时交换一次 append 顺序，观察断言变红，再还原并确认变绿。

不会时按“方向提示 → 具体提示 → 参考片段 → 用户重新补写并运行”推进，不因闭卷手写卡住工程。

**Task 1 验收与 S 级 Checkpoint**

- 相关测试通过，原有测试无回归，Ruff 通过。
- Claude 做一次 ≤400 token 的 Full Review：说明实现、核心数据流、修改入口、常见消息顺序 Bug 及其对 Agent 的影响。
- 用户不看代码回答 3 题：
  1. 为什么 `AIMessage(tool_call)` 必须在对应 `ToolMessage` 之前进入 messages？
  2. 为什么 content 为空不能作为停止条件，真正的正常停止条件是什么？
  3. `steps` 应该数模型轮数还是工具个数？这个选择怎样避免 `max_steps` 的 off-by-one？
- 结果标记 `PASS / PARTIAL / NOT YET`；只有 `PASS` 才解锁 Task 2。完成后 Claude 更新本文件状态并停止，等待用户继续。

## Day 3 Definition of Done

- 最小 Agent Loop 直接可读，不依赖高级 Agent 框架。
- 四组 ScriptedModel 行为测试全部通过，且验证消息顺序、ID 配对、调用次数和终止状态。
- 支持连续多轮 Tool Call；一个响应中的多个 Tool Call 暂时串行执行。
- 未知工具与 Tool 异常不会让 Runtime 无说明崩溃，而会形成可识别的错误回填；空 content + tool_calls 不误停。
- `max_steps` 恰好限制模型轮数，达到上限返回 `max_steps_exceeded`，不伪造最终回答。
- 最小结构化日志能还原 run/step/model/tool/完成或超限主线，但没有新建 EventBus。
- 用户完成 Agent Loop Full Review 与开放式 Checkpoint，能够独立口述完整 Message Trace。
- 所有 Task 完成并做 Git 收尾后，Claude 按 `CLAUDE.md` 第 16 节把 `Day 3 Final Summary（复盘用）` 追加到本文件；未完成前不预写总结答案。

## Backlog / Out-of-Scope

- Day 4：把临时 `tools` dict 升级为 Tool Schema / Registry / Executor / 正式 ToolResult。
- Day 5 及以后：并行 READ_ONLY Tool、Retry、Timeout 与更完整错误策略。
- Day 10 及以后：Run/Session、Checkpoint、Recovery。
- Day 12：统一 AgentEvent / EventBus；Day 3 只复用现有 `log_event`，用稳定事件类型与字段表达 run、step、model、tool、complete、max-steps 里程碑。
- 真实 Provider 的端到端运行可作为时间允许时的 smoke test，不作为确定性验收替代品。
- Day 2 的故意错误、陈旧 TODO 与错误 ID 教学实验保持原样；若未来要生产化清理，单独建 Scope。

---

## Day 3 Final Summary（复盘用）

### 今天真正完成了什么

你在 Day 2 的手工两轮协议之上，亲手把那段人工编排收进了一个**透明、可读、可测试、能安全终止**的异步循环——`AgentRuntime`。没有用 `create_agent`、`AgentExecutor`、LangGraph 任何一个高级抽象，每一行控制流都是你写的：

```text
HumanMessage ──> while True:
  ai = await model.ainvoke(messages)     # 第 N 轮模型调用
  messages.append(AIMessage)              # ★ 先落地"模型这轮说了什么"
  steps += 1                              # 数模型轮数，不是工具个数
  if steps >= max_steps: return max_steps_exceeded   # 不收敛兜底
  if ai.tool_calls 为空: return completed(final=ai.content)   # 正常停止
  for tc in tool_calls:
      try: result = exec_tool(tc)
      except Exception: result = 错误信息      # ★ 失败回填，不崩
      messages.append(ToolMessage(result, 原 tool_call_id))   # ★ 原 id 配对
```

并用**四象限剧本测试**（无工具 / 一次工具 / 连续两轮工具 / max_steps 兜底）+ **三个失败边界测试**（未知工具 / 工具异常 / 空 content 不误停）把这层循环的逻辑钉死（9 passed，零回归，Ruff 全绿）。最后用**真实 GLM-5.2** 接上这个 Loop，亲眼看到它驱动真实 LLM 完成工具往返，并在工具故意炸掉时通过失败回填让模型自我纠错。

### 最重要的 6 个工程认识

1. **`AIMessage(tool_call)` 必须在对应 `ToolMessage` 之前进链**：ToolMessage 是"对某个请求的回执"，协议上必须指认历史里真实存在的 assistant 请求。先 append AI 请求再回填结果，顺序反了就是"结果排在它自己的请求前面"——严格 Provider 直接 400 拒绝整个请求。机制：消息顺序是 Provider 校验的硬约束；收益：Debug 消息链问题时，第一步永远是打印 `snapshots[i].messages` 的类型顺序。

2. **停止信号是 `tool_calls` 是否为空，不是 `content` 是否为空**：Tool Calling 时 content **允许为空**（模型把"话"全放进 tool_calls）。机制：`content=""` + `tool_calls=[...]` 是合法且常见的模型输出；收益：用这个判断停止，Agent 不会在模型明明要调工具时误判为"没话说"而提前终止。今天用专门的测试把这个不变量锁死了。

3. **`steps` 数模型轮数，不数工具个数**：避免 max_steps 的 off-by-N。机制：一轮提议 3 个工具若数工具个数则 +3，阈值语义（"允许模型思考几轮"）和计数单位（工具个数）不一致，保险丝会变成"依赖工具个数的随机跳闸"；收益：`max_steps=3` 精确意味着"允许调用模型 3 次"，可预测、可测试。今天用反向验证（改 4 则 steps 变 4）实证了这一点。

4. **失败回填 ≠ 报告失败，而是给模型的行动指引**：`except Exception` 捕获后，错误消息 content 要写清【哪个工具】【什么异常】【怎么纠错】。机制：错误 ToolMessage 是写给 LLM 的 prompt，不是写给人的 log——真实 GLM-5.2 读懂了"请改用其他方式完成任务"，主动诊断 1300>1000 的根因并提议换 `add` 工具；收益：Agent 不会因一个工具偶发失败就崩掉，而是把错误反馈给 LLM 让它自我纠错。这是 Agent 从"逻辑正确"走向"现实健壮"的关键一跃。

5. **日志零污染设计：Runtime 只表达，不处置**：`AgentRuntime` 只调 `log_event`，不调 `setup_logging`；`_log` 用 `hasHandlers()` 短路无 handler 场景。机制：测试环境无 handler → 日志是廉价 no-op，不产生文件、不污染断言、一行测试不用改；调用方一旦 `setup_logging()` → 同一份代码立刻写 `agent.jsonl`；收益：同一份 Runtime 代码在"零成本沉默"和"全量记录"之间无改切换。这个"核心层只表达、处置权交基础设施"的哲学，和失败回填（处置权交模型）是同构的。

6. **`except Exception` 在工具边界是正确的（`# noqa: BLE001`）**：工具是开放世界（Day 4+ 用户可注册任意工具），Runtime 无法预知会抛什么。机制：宽捕获 + 把错误回填给模型是 Agent executor 的标准做法，和 Ruff 的通用建议不矛盾——这是边界语义的合理例外；收益：Bug 型异常（如签名写错）也会经 ToolMessage 暴露给模型和日志，不会被静默掩盖。

### 两处超出计划的工程认识（值得记住）

- **快照是"请求日志"，永远落后响应一条**：`snapshots[i].messages` 是 Runtime 在第 i 次 `ainvoke` 时喂给模型的消息链，而 final AIMessage 是那次调用的响应——它返回即 return，永远不会有第 i+1 次 ainvoke 把它拍进快照。所以验证最终回答要从 `result.final_text` 取，验证历史链从 `snapshots` 取，**输入和输出分开取证**。这是写测试时最容易踩的坑（一开始想断言 6 条消息，实际只有 5 条输入）。
- **剧本测逻辑，真实演示验假设**：真实运行日志里出现了 `Retrying request ... in 0.46s`（SDK 对网络抖动的自动重试），9 个剧本测试物理上不可能触发。剧本证明"Loop 逻辑正确"，真实演示暴露"Loop 所依赖的世界"（网络、限流、SDK 行为、模型不听话）。只用剧本会得出"Agent 永远两轮收敛、工具从不超时"的错误结论——**两条腿走路，缺一即是自欺**。

### 一句话主链

> 每轮先把模型的 `AIMessage` 落地、再按 `tool_calls` 是否为空决定"给最终回答"还是"执行工具回填进下一轮"，失败也用原 id 回填成错误消息让模型自我纠错——没有高级框架，这段循环就是 Agent 的心脏。

### 最值得复习的代码

1. `src/agent_harness/agent/runtime.py:63-158` —— `run()` 的完整循环：六步固定顺序 + 失败回填 + max_steps 兜底。整段一眼读完，是理解所有 Agent 框架（LangGraph 的 StateGraph、各家 executor）的基线。
2. `src/agent_harness/agent/runtime.py:160-172` —— `_log` 的 no-op 短路：看它如何用 `hasHandlers()` 让日志"可选而不缺失"。
3. `src/agent_harness/agent/types.py` —— `AgentRunResult` 的最小字段（status/final_text/steps）和字符串常量设计（为什么不 Enum）。
4. `tests/agent/test_agent_loop.py:182-217` —— 连续两轮工具的 5 条消息链断言 + 两组 id 配对：看测试如何锁住"循环性"和"id 不串台"这两个剧本才能精确复现的不变量。
5. `debug_real_loop.py` —— 真实模型接入：看 `bind_tools` 为什么放调用方而不是 Runtime（职责边界），以及如何用一个会炸的 `risky_add` 触发失败-恢复闭环。

### 6 道闭卷自测题（复习时自测，不给答案）

1. 为什么 `AIMessage(tool_call)` 必须在对应 `ToolMessage` 之前进入 messages？顺序反了，严格 Provider 会怎样？
2. 模型某一轮返回 `content=""` 且 `tool_calls=[add(...)]`，你的 Loop 应该停还是继续？为什么 content 不能当停止信号？
3. `steps` 数模型轮数还是工具个数？一轮提议 3 个 tool_call 算几步？这个选择如何避免 max_steps 的 off-by-N？
4. `max_steps=3` 时，为什么剧本放 3 条就够、不需要第 4 条？如果把兜底判断移到 `steps += 1` 之前，会发生什么？
5. 模型调了一个 `tools` 里不存在的工具名，你的 Runtime 现在会怎么处理？错误 ToolMessage 为什么必须用原 `tool_call_id` 回填？用随机新 id 会怎样？
6. `AgentRuntime` 自己不调 `setup_logging`，只调 `log_event`——这个设计如何让测试零回归、又让真实调试有日志？它和"失败回填"共享什么工程哲学？

### Day 3 到 Day 4 的接口

你已经有了：
- 一个**透明、可读、能驱动真实 LLM** 的 Agent Loop（`AgentRuntime`），支持连续多轮、串行工具、失败回填、max_steps 兜底；
- 一套**四象限 + 失败边界**的确定性测试底座（9 passed，剧本复现协议不变量）；
- 一个**零污染的结构化日志**接入点（`_log` + `log_event`，可还原 run/step/llm/tool/决策主线）；
- 两个**真实模型调试脚本**（`debug_loop.py` 看剧本消息链、`debug_real_loop.py` 接真实 LLM 含失败演示）。

明天 Day 4 在此基础上：
- 把临时的 `tools: dict[str, Callable]` 升级为**正式 Tool Schema / Registry / Executor / ToolResult**——今天的 `tools[name]` 查找和宽捕获，会变成带 schema 校验、统一结果类型的工具系统；
- 参数 schema 校验（今天故意没碰，留给 Day 4）会从 Day 2 的 demo 版搬进正式 ToolExecutor；
- 这层失败处理和日志会**原样保留**——它们是 Loop 的心脏能力，不随工具系统升级而重写。

一个留着的问题（真实运行观察到、今天不解决）：真实 GLM-5.2 在工具失败后选择"问你要不要重试"而非"直接换工具重试"——**Agent 的主动性来自哪里**（工具描述 vs system prompt）？Day 4+ 做更复杂 Agent 时会回到这个观察。

