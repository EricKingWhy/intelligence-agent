# Day 01–02：模型协议与 Function Calling 基础

## 两天结束时你必须得到什么

不要急着写 Agent Loop。

Day 1–2 只解决三个问题：

1. Runtime 怎么以统一方式创建 Qwen / DeepSeek；
2. LangChain Message 到底长什么样；
3. 模型提出 Tool Call 时，原始数据是什么样。

两天结束时，你应该能在终端看到：

```text
HumanMessage
  ↓
ChatModel.bind_tools(...)
  ↓
AIMessage
  ├─ content
  └─ tool_calls
       ├─ id
       ├─ name
       └─ args
```

并且能口述：

> Function Calling 并不是模型自己执行函数。模型只生成结构化“调用请求”，真正执行 Tool 的是 Agent Runtime。

---

# Day 01：项目骨架、Async-first、配置、日志、ModelProvider

## Day 01 · 3 小时时间盒

- 0:00–0:35 原理/目录：Async、Provider 边界、配置。
- 0:35–1:35 Claude Code 分模块实现 Config/ModelProvider/Logging。
- 1:35–2:15 Unit + 一次真实 Model Integration Test。
- 2:15–2:40 故障实验：错误 API Key，自行读 JSONL。
- 2:40–3:00 Codex Review、口述、commit/tag。


## 1. 今天为什么先不写 Agent Loop

如果第一天直接写：

```python
while True:
    ...
```

你会同时处理：

- 模型配置
- Provider
- 消息结构
- Tool Calling
- Loop
- 错误

学习目标会混在一起。

Day 1 只搭最薄的底座。

---
## 2. 建议目录

```text
agent-harness/
├─ pyproject.toml
├─ .env.example
├─ src/
│  └─ agent_harness/
│     ├─ __init__.py
│     ├─ config.py
│     ├─ logging.py
│     ├─ model/
│     │  ├─ __init__.py
│     │  ├─ config.py
│     │  └─ provider.py
│     └─ cli.py
└─ tests/
   └─ test_model_provider.py
```

现在不要创建：

```text
services/
repositories/
controllers/
middleware/
```

---

## 3. 初始化

建议：

```bash
uv init
uv add langchain-core langchain-openai pydantic pydantic-settings python-dotenv
uv add --dev pytest pytest-asyncio ruff
```

如果当前 LangChain Provider 包发生变化，以当前官方文档为准；原则不变：

> Provider Adapter 用成熟库，Agent Runtime 自己写。

---

## 4. Config 只解决“运行需要什么”

建议字段：

```python
class Settings(BaseSettings):
    model_provider: str = "deepseek"
    model_name: str
    model_api_key: str
    model_base_url: str
    temperature: float = 0.2

    log_level: str = "INFO"
    workspace_dir: str = ".agent/workspace"
```

不要把 50 个未来配置提前塞进来。

`.env.example`：

```env
MODEL_PROVIDER=deepseek
MODEL_NAME=...
MODEL_API_KEY=...
MODEL_BASE_URL=...
```

---

## 5. ModelProvider 应该薄到什么程度

目标：

```python
class ModelProvider:
    def create(self, config: ModelConfig):
        ...
```

或者更简单：

```python
def create_chat_model(config: ModelConfig):
    ...
```

只要能做到：

```text
Agent Runtime
    ↓
create_chat_model(config)
    ↓
ChatModel
```

不要自己写 HTTP Client。

Qwen、DeepSeek 都可以通过各自的 OpenAI-compatible Endpoint 接入统一 ChatModel；你的抽象目标不是“统一所有模型能力”，只是把 Provider 初始化从 Agent Core 拆出去。

---

## 6. 今天必须加入结构化日志

建议至少两个 Handler：

```text
Console
  → 人看

.agent/logs/agent.jsonl
  → 程序查
```

JSONL 最小字段：

```json
{
  "timestamp": "...",
  "level": "INFO",
  "event_type": "model_created",
  "provider": "deepseek",
  "model": "..."
}
```

现在还没有 run_id/session_id，不要硬造。

---

## 7. Claude Code Prompt：Day 01 / 模块 1

```text
实现 Day01 的项目底座。

只做：
1. Python 3.11 + uv 项目结构。
2. Pydantic Settings 配置。
3. 一个极薄的 ModelProvider/create_chat_model。
4. 支持 deepseek/qwen/openai-compatible 配置切换。
5. Console + JSONL 结构化日志。
6. 一个最小 CLI 命令：启动后向模型发送纯文本并打印回复。
7. pytest 测试 ModelProvider 的配置选择逻辑，测试中不要真实请求模型。

强约束：
- 不创建 Agent。
- 不创建 Tool。
- 不使用 create_agent。
- 不提前实现 Session、RAG、Checkpoint。
- 代码保持直接。
```

---

## 8. Day 01 Unit Test

至少验证：

```text
provider=deepseek
→ 正确使用对应 base_url/config

provider=qwen
→ 正确构建

未知 provider
→ 明确抛 ConfigError
```

不要为了测试真实 API 而烧 Token。

---

## 9. Day 01 Integration Test

只跑一次：

```text
User: reply exactly "pong"
Model: pong
```

验证：

- `.env` 可用；
- Provider 初始化可用；
- Async 调用可用；
- 日志能记录模型调用成功/失败。

---

## 10. Day 01 Failure Experiment

故意：

```text
MODEL_API_KEY=wrong
```

要求你自己从日志中找到：

- Provider
- Model
- 异常类型
- 错误信息

不要第一反应直接把错误复制给 AI。

---

## 11. Day 01 口述验收

不看代码回答：

1. 为什么 ModelProvider 要薄？
2. 为什么不自己写 DeepSeek/Qwen HTTP Client？
3. Async-first 对后面的并行 Tool、Streaming、MCP 有什么价值？
4. 日志为什么从 Day 1 就加入？

答不清楚，不进入 Day 2。

---

## 12. Git

```bash
git add .
git commit -m "day01: bootstrap async runtime and model provider"
git tag checkpoint-day-01
```

---

# Day 02：Message、Function Calling、FakeModel

## Day 02 · 3 小时时间盒

- 0:00–0:40 手工观察 Human/AI/Tool Message 与 tool_call_id。
- 0:40–1:30 Claude Code 实现 Function Calling demo + FakeModel。
- 1:30–2:10 Unit Test：Request Snapshot/消息配对。
- 2:10–2:35 故障实验：错误 tool_call_id。
- 2:35–3:00 Review、口述、checkpoint。


## 1. 今天最重要的知识

必须理解四个对象：

```text
HumanMessage
AIMessage
ToolMessage
tool_call_id
```

最关键的一句话：

> `AIMessage.tool_calls` 是模型提出的“请求”；Tool 执行结果必须通过相同 `tool_call_id` 的 `ToolMessage` 回到模型，模型才能知道哪个结果对应哪个调用。

---

## 2. 先做一个“假的 Tool Schema”

今天不执行 Tool。

只定义一个 Calculator Schema：

```python
class AddArgs(BaseModel):
    a: float
    b: float
```

生成供模型绑定的 JSON Schema。

描述必须明确：

```text
name: add
description:
  对两个数字做加法。
  仅在用户明确需要数值加法时使用。
  不用于字符串拼接。
```

---

## 3. 手工观察 Tool Call

流程：

```python
messages = [
    HumanMessage(content="计算 123 + 456")
]

model_with_tools = model.bind_tools([...])

ai_message = await model_with_tools.ainvoke(messages)

print(ai_message.content)
print(ai_message.tool_calls)
```

你要亲眼看到类似：

```python
[
    {
        "name": "add",
        "args": {"a": 123, "b": 456},
        "id": "call_xxx",
        ...
    }
]
```

不要只看最终答案。

---

## 4. 再手工模拟 Tool Result

今天可以手工：

```python
result = 579

messages.append(ai_message)
messages.append(
    ToolMessage(
        content='{"ok":true,"data":{"value":579}}',
        tool_call_id=ai_message.tool_calls[0]["id"],
    )
)

final = await model.ainvoke(messages)
```

这一步就是未来 Agent Loop 的最小原型。

---

## 5. FakeModel 为什么非常重要

真实模型输出不确定，无法可靠验证 Agent Loop。

所以定义：

```text
FakeModel / ScriptedModel

第 1 次调用
→ 固定返回 Tool Call

第 2 次调用
→ 固定返回 Final Answer
```

例如：

```python
script = [
    AIMessage(
        content="",
        tool_calls=[{
            "id": "call_1",
            "name": "add",
            "args": {"a": 1, "b": 2},
        }],
    ),
    AIMessage(content="结果是 3"),
]
```

后面 Day 3 的 Agent Loop Unit Test 全靠它。

---

## 6. FakeModel 要记录 Request Snapshot

每次模型被调用时保存：

```text
第几次调用
收到哪些 messages
绑定了哪些 tools
```

这样测试可以断言：

```text
第二次调用时
messages 中必须存在：
HumanMessage
AIMessage(tool_call)
ToolMessage(tool_call_id=call_1)
```

这比只断言 `"结果是 3"` 有价值。

---

## 7. Claude Code Prompt：Day 02

```text
实现 Day02 的 Message / Function Calling 学习模块。

要求：
1. 增加一个最小 Tool Schema 示例，但今天不要做 ToolExecutor。
2. 通过 bind_tools 让真实模型产生一次 tool_calls，并提供一个独立 demo 命令打印：
   - AIMessage.content
   - tool_calls
   - tool_call id
   - args
3. 手工构造 ToolMessage，把结果回填给模型，再请求最终回答。
4. 新增 FakeModel/ScriptedModel：
   - 支持按顺序返回预设 AIMessage；
   - 每次调用记录收到的 messages 快照；
   - 适合后续 Agent Loop 单元测试。
5. 不写 Agent Loop。
6. 对 FakeModel 写 pytest。
```

---

## 8. Unit Test

必须验证：

### Test 1

```text
FakeModel 两个预设 response
→ 连续调用两次
→ 顺序正确
```

### Test 2

```text
Request Snapshot
→ 能看到调用时真实 messages
```

### Test 3

```text
ToolMessage.tool_call_id
→ 和 AIMessage.tool_calls[0].id 相同
```

---

## 9. Failure Experiment：故意错配 tool_call_id

人为：

```python
ToolMessage(
    content="3",
    tool_call_id="wrong-id"
)
```

用真实 Provider 看会发生什么。

目的不是背错误字符串，而是理解：

> Tool Result 不是普通聊天消息，它属于 Function Calling 协议的一部分。

---

## 10. Day 02 口述验收

必须能说：

> 模型不会真的调用 Tool。它只返回带 `name / args / id` 的 Tool Call。Runtime 根据 name 找到工具并执行，然后构建携带相同 `tool_call_id` 的 ToolMessage 回填。模型下一轮才能继续推理。

再回答：

1. FakeModel 为什么比 Mock 一个字符串强？
2. `bind_tools()` 做了什么，没做什么？
3. Tool Schema 写得差为什么会造成误调用？
4. 为什么 Day 2 还不应该写 ToolRegistry？

---

## 11. Git

```bash
git commit -am "day02: expose function calling protocol and scripted model"
git tag checkpoint-day-02
```

---

# 两天结束后的架构

```text
CLI
 ↓
ModelProvider
 ↓
ChatModel
 ↓
Messages
 ↓
Function Calling Protocol

Tests
 ↓
FakeModel
```

还没有：

```text
Agent Loop
Tool Runtime
RAG
Session
```

这是刻意的。
