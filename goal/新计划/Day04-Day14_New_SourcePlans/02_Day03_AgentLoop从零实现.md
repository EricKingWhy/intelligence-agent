# Day 03：从零实现 Agent Loop

## Day 03 · 3 小时时间盒

- 0:00–0:45 手画 Message Trace，先写最小 Loop 伪代码。
- 0:45–1:40 Claude Code 只实现透明 Agent Loop。
- 1:40–2:20 4 组 FakeModel 行为测试。
- 2:20–2:40 Failure：不存在工具/max_steps/content 为空。
- 2:40–3:00 Codex Review + 口述 + tag。


> 这是 V1 最重要的一天之一。今天不要追求功能数量，只把 Agent Loop 做透。

---

# 1. 今天的唯一核心问题

如何把 Day 2 的手工流程自动化：

```text
User
 ↓
LLM
 ↓
Tool Call?
 ├─ No → Final Answer
 └─ Yes
      ↓
    Execute
      ↓
    ToolMessage
      ↓
    LLM
      ↓
    ...
```

Agent 的“自主性”很大一部分就体现在：

> 模型根据当前 Messages 决定下一步是回答，还是继续调用 Tool。

---

# 2. V1 Agent Loop 明确不使用什么

禁止：

```python
create_agent(...)
AgentExecutor(...)
```

禁止 LangGraph。

今天的代码必须能在一个文件里顺着读完。

---

# 3. 建议目录变化

```text
src/agent_harness/
├─ agent/
│  ├─ __init__.py
│  ├─ runtime.py
│  └─ types.py
└─ ...
tests/
└─ agent/
   └─ test_agent_loop.py
```

现在 `runtime.py` 不要超过必要复杂度。

---

# 4. AgentRuntime 最小输入

```python
class AgentRuntime:
    def __init__(
        self,
        model,
        tools,
        max_steps: int = 20,
    ):
        ...
```

今天 `tools` 可以先用一个简单 dict：

```python
{
    "add": add_function
}
```

真正 ToolRegistry Day 4 再做。

---

# 5. Loop 的正确顺序

推荐逻辑：

```python
async def run(self, user_input: str) -> AgentRunResult:
    messages.append(HumanMessage(content=user_input))

    for step in range(self.max_steps):
        ai_message = await self.model.ainvoke(messages)
        messages.append(ai_message)

        if not ai_message.tool_calls:
            return AgentRunResult(
                status="completed",
                final_text=...,
                steps=step + 1,
            )

        for tool_call in ai_message.tool_calls:
            result = await execute_minimal_tool(tool_call)

            messages.append(
                ToolMessage(
                    content=result,
                    tool_call_id=tool_call["id"],
                )
            )

    return AgentRunResult(
        status="max_steps_exceeded",
        ...
    )
```

今天重点是“顺序”，不是 Tool 抽象。

---

# 6. 为什么 AIMessage 必须先 append

错误写法：

```text
模型返回 Tool Call
→ 直接执行 Tool
→ 只 append ToolMessage
```

这会导致 Message History 缺少：

```text
assistant 请求调用哪个工具
```

正确：

```text
Human
AI(tool_call)
Tool(result)
AI(...)
```

每轮都必须形成完整协议。

---

# 7. 为什么 max_steps 是硬兜底而不是“正常停止逻辑”

正常停止条件：

```text
模型不再返回 tool_calls
```

`max_steps=20` 只处理：

- 模型陷入循环；
- 工具结果让模型反复尝试；
- Prompt/Tool 描述出现问题；
- Provider 行为异常。

不要写：

```text
执行到第 5 步就强行结束
```

Agent 应自主决定停止，`max_steps` 是保险丝。

---

# 8. 今天要先只支持串行 Tool

虽然最终需求是 READ_ONLY 并发，但 Day 3 故意串行。

因为你今天要把：

```text
tool_call
→ execute
→ ToolMessage
→ next model call
```

看明白。

并发留到 Day 5。

---

# 9. AgentRunResult

不要只 return 字符串。

最小：

```python
class AgentRunResult(BaseModel):
    status: Literal[
        "completed",
        "max_steps_exceeded",
        "failed",
    ]
    final_text: str | None = None
    steps: int
```

详细 Run/Session Day 10 再扩展。

---

# 10. Day 03 最关键 Unit Test

## 场景 A：无工具

FakeModel：

```text
第 1 次 → "hello"
```

断言：

```text
只调用模型一次
status=completed
```

---

## 场景 B：一次 Tool 往返

FakeModel：

```text
Call 1:
add(a=1,b=2)

Call 2:
"3"
```

断言第二次 Model Request 的 Messages：

```text
Human
AI(add tool call)
Tool(tool_call_id=call_1)
```

这是今天最重要的测试。

---

## 场景 C：连续两次工具

FakeModel：

```text
Call 1 → add
Call 2 → add
Call 3 → final
```

验证 Agent 不是“只支持一次 Tool”。

---

## 场景 D：max_steps

FakeModel 永远返回：

```text
add(1,2)
```

设置：

```text
max_steps=3
```

断言：

```text
模型调用 3 轮后终止
status=max_steps_exceeded
```

---

# 11. 为什么不要在 max_steps 后伪造 Final Answer

错误做法：

```text
max_steps 到了
→ 调一次模型说“请总结”
```

这可能继续触发 Tool。

V1 明确返回：

```text
MAX_STEPS_EXCEEDED
```

让上层知道：

> 任务没有正常收敛。

以后 V2 再做 Graceful Degradation。

---

# 12. Claude Code Prompt：Agent Loop

```text
实现 Day03 最小 Agent Loop。

严格要求：
1. 不允许 create_agent / AgentExecutor / LangGraph。
2. Agent Loop 必须在 runtime.py 中直接可读。
3. 使用 Async。
4. 输入 user text 后追加 HumanMessage。
5. 每轮调用模型。
6. AIMessage 必须先写入 messages。
7. 如果没有 tool_calls，结束。
8. 如果有 tool_calls，暂时按顺序串行执行。
9. ToolMessage 必须复用原始 tool_call_id。
10. 支持连续多轮 Tool Calling。
11. max_steps 默认 20，超过后返回明确状态，不伪造成功。
12. 今天不要提前做 ToolRegistry/Retry/Session/Checkpoint/Event。
13. 使用 Day02 FakeModel 写完整行为测试。

代码越直接越好。
```

---

# 13. Claude Code 讲解 Prompt

代码完成后单独问：

```text
不要改代码。只根据当前 Agent Loop 给我做一次“执行流讲解”。

使用这个案例：
用户：“读取 a.txt，再读取 b.txt，然后告诉我差异。”

从第一条 HumanMessage 开始，逐步说明：
1. messages 每一步长什么样；
2. 模型第几次被调用；
3. tool_calls 什么时候出现；
4. tool_call_id 在哪生成、在哪复用；
5. ToolMessage 在哪加入；
6. 为什么模型下一轮知道前一个 Tool 的结果；
7. 最终什么条件让 while/for loop 结束。

最后再指出当前 Day03 Agent Loop 和生产级 Agent Runtime 相比缺少什么。
```

---

# 14. Failure Experiment 1：Tool 名不存在

FakeModel 返回：

```text
tool name = "not_exists"
```

今天最小执行器应把它作为 Tool Result 错误回给模型，而不是 Python 直接炸掉进程。

可以临时返回：

```json
{
  "ok": false,
  "error": "tool not found"
}
```

正式 ToolResult Day 4 再做。

---

# 15. Failure Experiment 2：模型返回空 content + Tool Call

确保 Runtime 不写：

```python
if not ai_message.content:
    return
```

因为 Tool Calling 场景下 AIMessage.content 很可能为空。

停止条件必须看：

```text
tool_calls
```

而不是只看 content。

---

# 16. 日志

今天至少记录：

```text
agent_run_started
agent_step_started
model_completed
tool_call_requested
tool_result_returned
agent_run_completed
max_steps_exceeded
```

但不要为了日志引入复杂 EventBus；Day 12 再正式统一 AgentEvent。

---

# 17. Codex Review Prompt：Day 03

```text
只 Review Day03 Agent Loop。

重点检查：
- 是否存在漏 append AIMessage 的情况；
- ToolMessage.tool_call_id 是否始终正确；
- 连续多轮工具是否真的可运行；
- max_steps 是不是 off-by-one；
- 模型没有 tool_calls 时能否可靠结束；
- content 为空但存在 tool_calls 时会不会误结束；
- Tool 执行异常会不会让整个 Runtime 非预期崩溃；
- 是否提前引入了 Day04 之后才需要的复杂抽象。

给出最小修改建议，不要重构成框架。
```

---

# 18. 今天必须自己画一次 Message Trace

手写：

```text
M0 Human("...")
M1 AI(tool_call id=1)
M2 Tool(id=1, result=...)
M3 AI(tool_call id=2)
M4 Tool(id=2, result=...)
M5 AI(final)
```

如果你不能解释每条 Message 谁产生的，不算掌握 Agent Loop。

---

# 19. 口述验收

不看代码回答：

1. Agent Loop 的真正停止条件是什么？
2. 为什么 ToolMessage 必须带 tool_call_id？
3. 为什么模型不是 Tool Executor？
4. `max_steps` 解决什么问题？
5. 为什么 AIMessage 即使 content 为空也可能非常重要？
6. 连续两次工具调用时，Messages 怎么变化？
7. 如果第二个 Tool 失败，错误为什么也应该回填给模型？

---

# 20. Git

建议几个小 commit：

```bash
git commit -m "day03: add minimal agent run result"
git commit -m "day03: implement transparent agent loop"
git commit -m "day03: test multi-turn tool calling"
git tag checkpoint-day-03
```

---

# Day 03 结束后的架构

```text
User
 ↓
AgentRuntime
 ↓
Model
 ↑ ↓
Tool
 ↑ ↓
Messages
```

现在它已经是一个真正的 Agent 雏形。

Day 4 才开始把临时 Tool 执行逻辑升级成正式 Tool Runtime。
