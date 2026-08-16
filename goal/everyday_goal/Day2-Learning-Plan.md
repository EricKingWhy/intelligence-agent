# Day02 - Function Calling 协议与可测试模型替身

> 状态：Day 2 开始，只有 Task 1 为 `ACTIVE`。  
> 定位：今天学习模型如何提出 Tool Call，以及 Runtime 如何把 Tool Result 回填；不实现 Agent Loop。  
> 执行者：Claude Code 按本计划逐 Task 带用户实现；Codex 仅负责编排本蓝图。

## 今日工程目标

在 Day 1 的 `ModelProvider → ChatModel` 底座上，亲手完成 Function Calling 的最小协议闭环，并建立可供 Day 3 Agent Loop 测试使用的确定性模型替身。

今天结束时，程序应能：

- 用明确的 `add` schema 让真实模型返回结构化 Tool Call；
- 观察并使用 Tool Call 的 `name / args / id`；
- 手工执行一次加法，把结果通过相同 `tool_call_id` 的 `ToolMessage` 回填；
- 获得模型基于 Tool Result 生成的最终回答；
- 用 ScriptedModel 固定返回序列并保存 Request Snapshot；
- 通过错误 ID 实验说明 Tool Result 是协议消息，不是普通聊天文本。

## 今天必须亲手完成

1. 写出/补全 `add` 的参数 schema 和描述，并让真实模型产生一次 Tool Call。
2. 打印并检查 `AIMessage.content`、`tool_calls`、`id`、`name`、`args`，不只看最终答案。
3. 手工执行 `add`，构造匹配 ID 的 `ToolMessage`，完成第二次模型调用。
4. 完成 ScriptedModel 的预设响应与 Request Snapshot 核心逻辑，并用测试验证消息顺序和 ID 配对。
5. 故意错配一次 `tool_call_id`，记录真实现象并从消息链定位原因。
6. 最后能脱离代码说明：模型只提出调用请求，Runtime 才负责执行与回填。

## 今日主调用链

```text
HumanMessage
  → ChatModel.bind_tools([add schema])
  → AIMessage.tool_calls[{id, name, args}]
  → Runtime 按 name/args 手工执行 add
  → ToolMessage(content=result, tool_call_id=同一个 id)
  → messages 追加 AIMessage + ToolMessage
  → ChatModel.ainvoke(messages)
  → AIMessage.content（最终回答）
```

关键边界：`bind_tools()` 让模型知道可用工具的协议描述，但不会替 Runtime 执行 Python 函数。

## 今日不做什么

- 不写 `while` Agent Loop。
- 不创建 ToolRegistry、ToolExecutor 或通用分发器。
- 不实现 Retry、Timeout、并行 Tool 或错误恢复。
- 不使用 `create_agent` / LangGraph 隐藏本次协议链。
- 不把 Calculator 扩展成通用工具系统。
- 不研究 LangChain、OpenAI SDK 或 JSON Schema 的内部源码。
- 不重构 Day 1 的 CLI、日志和 ModelProvider。

## 当前项目起点

- Day 1 已有 `Settings → ModelConfig → create_chat_model → ainvoke` 单次文本调用。
- 当前尚无 Tool Schema、`bind_tools` demo、`ToolMessage` 回填或 ScriptedModel。
- `tests/test_structured_logging.py` 中的局部 `FakeModel` 只返回普通文本，不是 Day 2 所需的顺序脚本模型。
- 日志测试中的 `tool_call_id` 只是结构化字段示例，不代表 Function Calling Runtime 已存在。
- 本地 API 已核对：当前 `ChatOpenAI.bind_tools()` 接受 dict、Pydantic 类型、Callable 或 BaseTool，并返回产生 `AIMessage` 的 Runnable。

## Task Map

| Task | 等级 | 工程成果 | Hands-on Action | 编码模式 | 状态 |
|---|---|---|---|---|---|
| Task 1：让真实模型产生并暴露 Tool Call | S | 独立 demo 绑定 `add` schema，终端可见 `content / id / name / args` | 用户补全 schema、绑定逻辑和原始字段输出，运行一次真实请求 | 🤝 PAIR WRITE | ✅ [x] COMPLETE |
| Task 2：手工完成 Tool Result 回填闭环 | S | 手工执行 `add`，用相同 ID 构造 `ToolMessage`，获得最终模型回答 | 用户亲手追加 `AIMessage + ToolMessage` 并发起第二次调用 | 🧑 YOU WRITE | ✅ [x] COMPLETE |
| Task 3：建立 ScriptedModel 与 Request Snapshot 测试 | A | 固定两次 AIMessage 响应，记录每次 messages/tools 快照，测试顺序与 ID 配对 | 用户完成快照或核心断言，运行不访问真实 API 的 pytest | 🤝 PAIR WRITE | ✅ [x] COMPLETE |
| Task 4：错误 ID Debug 与协议验收 | S | 复现错误 `tool_call_id`，记录真实 Provider 行为，完成全链测试与 Function Calling 复盘 | 用户制造错配、定位消息链、恢复正确 ID 并重新验证 | 🧑 YOU WRITE | ✅ [x] COMPLETE |

## Definition of Done

Day 2 只有同时满足以下条件才完成：

- 真实模型至少产生过一次包含 `id / name / args` 的 Tool Call；
- 正确 ID 的手工回填得到最终回答；
- ScriptedModel 顺序响应、Request Snapshot、消息配对测试通过；
- 错误 ID 实验完成，实际现象和根因有记录；
- S 级 Full Review 与 2～3 个开放式 Learning Checkpoint 通过；
- 用户能说明 `bind_tools()` 做了什么、没做什么，以及真正执行 Tool 的责任属于谁；
- 仍未出现 Agent Loop、ToolRegistry 或 ToolExecutor。

## Current Task
## Current Task

### Task 4 Brief：错误 ID Debug 与协议验收

**Task 3 完成记录（2026-08-16）**

- 新增 `tests/scripted_model.py`（ScriptedModel + RequestSnapshot）与 `tests/test_tool_call_demo.py`（3 测试），全程零 API、零 Token。
- `tool_call_demo.run(message, model=None)` 加入注入点：None 走真实 Provider，测试注入替身。
- `pyproject.toml` 新增 `[tool.pytest.ini_options] pythonpath = ["."]`，解决 tests 跨模块导入（用户已同意该最小配置变更）。
- 用户完成：ScriptedModel.ainvoke 的三步逻辑（耗尽报错 / list(messages) 拷贝快照 / 吐剧本进游标）、测试的顺序断言与 ID 双向配对断言。
- 关键工程动作——反向验证测试：临时把 demo 的 tool_call_id 改成 WRONG_ID，测试立刻红，证明断言非恒真；还原后恢复绿。这是比"跑绿"更可靠的测试可信度判据。
- 现状：14 passed（原 11 + 新 3），ruff All checks passed。用户选择保留 TODO 注释用于复习。

**Task 4 完成记录（2026-08-16）**

- 受控实验已执行（真实 Provider：tencent / GLM）：错误 `tool_call_id`（实验 A）未报错且回答正确；正确 id（实验 B 对照）同样正确。
- 关键认知（用户自行推导得出）：`tool_call_id` 的本职是**多调用路由凭证**，单调用下无歧义故 Provider 不强制校验；多工具并行时错配会**静默串结果**（不报错但结果对调），比报错更危险。
- 可移植性结论：GLM 不校验 id 配对是其实现选择而非协议保证；OpenAI 官方端点更严。工程准则——永远按协议正确配对，不依赖当前 Provider 的宽容。
- 用户预测 (a) 报错 vs 实测不报错，预测落空处即最大学习点：理论严格性 ≠ 某 Provider 的实际校验。
- 实验脚本 `tasks_experiment_wrong_id.py` 为一次性产物，已删除（不入主链、不入测试）。

**编码模式**：`🧑 YOU WRITE`（复现实验 + 根因记录 + 复盘）

## Backlog / Out-of-Scope

- `ToolRegistry / ToolExecutor / Agent Loop`：留到后续 Agent Runtime 课程。
- 多工具选择、并行 Tool Call、Retry、Timeout：不属于 Day 2。
- 当前日志体系已有 `tool_operation` 字段，但 Day 2 不据此扩建可观测性架构。
- 错误 `tool_call_id` 的具体报错可能因 Provider 不同而不同；Task 4 记录真实现象，不背固定错误字符串。
- Git commit/tag 是完成 Day 2 后的 B 级收尾动作，嵌入最终工程收尾，不单独设 Task。
- 多工具并行场景下 `tool_call_id` 错配的"静默串结果"现象：Day 2 范围外，留作 Day 3+ 自验证（双 add 并行剧本即可复现）。
