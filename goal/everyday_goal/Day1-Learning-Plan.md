# Day01 - Async Model Runtime 与 ModelProvider 底座

> 状态：**✅ Day 1 已完成（由用户确认）**  
> 用途：这是 Day 1 的工程学习记录与复盘蓝图，不再包含待执行任务。  
> 范围：只覆盖原始目标中的 Day 1；Function Calling、Tool Message 和 FakeModel 属于 Day 2。

## 今日工程目标

搭建并验证一个可以通过配置切换 OpenAI-compatible 模型的最小异步 Runtime：

- 配置负责提供运行参数；
- `ModelConfig` 负责解析厂商预设与显式覆盖；
- 薄 `ModelProvider` 负责创建统一 `ChatModel`；
- CLI 完成一次真实异步模型调用；
- Console + JSONL 记录成功与失败；
- 单元测试、真实调用和故障实验共同验证底座可用。

Day 1 的重点不是研究 SDK 底层，而是让后续 Agent Core 能稳定地获得一个可调用、可切换、可排查的模型对象。

## 今天必须亲手完成

- [x] 配置并运行一个真实 Provider，通过 CLI 获得模型回复。
- [x] 沿主链找到配置、模型创建和 `ainvoke` 的修改入口。
- [x] 运行不访问真实 API 的单元测试，并完成一次真实 Integration Test。
- [x] 制造一次模型调用失败，从 JSONL 中定位 Provider、Model、异常类型和错误信息。
- [x] 说明薄 ModelProvider、Async-first 和 Day 1 加日志的工程价值。

## 今日主调用链

```text
CLI message
  → Settings
  → ModelConfig.from_settings()
  → create_chat_model()
  → ChatOpenAI（指向所选 Provider 的 compatible endpoint）
  → await model.ainvoke([HumanMessage(...)])
  → AIMessage.content
  → Console 输出 + JSONL 运行记录
```

这是一条“单次模型调用链”，还不是 Agent Loop。

## 今日不做什么

- 不写 Agent Loop。
- 不创建或执行 Tool。
- 不学习 `bind_tools()`、`tool_calls`、`ToolMessage`、`tool_call_id`。
- 不创建 Session、Checkpoint、RAG、MCP。
- 不自行实现 HTTP Client、TLS、Tokenizer 或 SDK 内部能力。
- 不为了理解日志而深入 trace/span 的底层设计。

## 完成证据

### 学习状态

- Day 1 全部工程 Task：**由用户明确确认完成**。
- 本计划按用户要求直接记录为完成态，不重新打开教学流程。

### 当前技术状态（2026-08-15 复核）

- `uv run pytest -q`：**11 passed**。
- `uv run ruff check .`：**All checks passed**。
- 项目中存在真实成功与失败的 JSONL 调用记录。
- Provider 代码支持 `deepseek / qwen / tencent` 预设及显式配置覆盖。
- 本次复核没有读取 `.env`，也没有再次消耗 Token 调用真实模型。

### 状态说明

- 当前 Git 中未检测到 `checkpoint-day-01` tag。Git tag 属于 B 级工程收尾，不影响用户指定的 Day 1 学习完成状态；如需要仓库 checkpoint，可后续单独补做。

## Task Map

| Task | 等级 | 工程成果 | Hands-on Action | 状态 |
|---|---|---|---|---|
| Task 1：跑通配置驱动的 Model Runtime | A | 完成 `Settings → ModelConfig → create_chat_model`，可通过配置选择兼容 Provider | 定位配置入口、切换/确认模型配置并创建可用 ChatModel | ✅ [x] COMPLETE |
| Task 2：完成真实异步 CLI 与结构化日志闭环 | A | CLI 能发送文本、等待模型回复，并将关键事件写到 Console + JSONL | 亲手运行一次真实请求，观察终端回复与对应日志 | ✅ [x] COMPLETE |
| Task 3：用测试与故障实验验证底座 | A | 单元测试隔离真实 API；Integration Test 验证真实链路；失败日志可用于定位 | 运行测试，制造一次错误配置/凭证故障并提取日志四要素 | ✅ [x] COMPLETE |
| Task 4：完成工程收尾与 Day 1 Mini Review | A | 能说清主调用链、设计边界、修改入口与后续能力缺口 | 根据下方 Summary 完成一次 5～10 分钟复盘 | ✅ [x] COMPLETE |

## Task 完成记录

### Task 1：跑通配置驱动的 Model Runtime — ✅ COMPLETE

- 工程成果：项目具备集中配置、Provider 预设、显式参数覆盖和薄模型工厂。
- 最值得看的代码：
  - `src/agent_harness/config.py`
  - `src/agent_harness/model/config.py`
  - `src/agent_harness/model/provider.py`
- Hands-on 结果：已完成模型配置与真实 Provider 接入。
- Mini Review：以后切换模型或端点，先查配置和 `ModelConfig`；改变模型对象创建方式，再查 `create_chat_model()`。

【Why Card：薄 ModelProvider】

- 解决什么：把厂商初始化细节与 Agent Core 分离。
- 为什么这样用：Agent Core 只依赖统一 ChatModel，不需要知道每家 endpoint 的差异。
- 不继续下钻：HTTP 连接、鉴权传输和序列化交给成熟 SDK。

### Task 2：完成真实异步 CLI 与结构化日志闭环 — ✅ COMPLETE

- 工程成果：终端输入可以穿过配置和 Provider，到达真实模型并打印回复；同一次运行留下结构化日志。
- 最值得看的代码：
  - `src/agent_harness/cli.py` 的 `run()`、`main()` 和 `await model.ainvoke(...)`
  - `src/agent_harness/logging.py` 的 `setup_logging()` 与 `log_event()`
- Hands-on 结果：真实模型调用已跑通，日志中存在成功调用记录。
- Mini Review：调用行为从 `cli.run()` 排查；模型初始化从 Provider 排查；运行失败先看最新 `llm_call` / `error` 日志。

【Why Card：Async-first】

- 模型调用是网络 I/O，`await` 等待结果时允许异步 Runtime 保持可扩展性。
- 后续 Streaming、并行 Tool 和 MCP 都会复用异步调用方式。
- 当前只需会使用和定位 `asyncio.run → async run → await ainvoke`，不研究事件循环内部实现。

【Why Card：结构化日志】

- Console 方便人在现场观察，JSONL 方便按字段检索和还原失败。
- 日志应帮助定位 Provider、Model、结果和异常，而不是为了“高级”堆字段。

### Task 3：用测试与故障实验验证底座 — ✅ COMPLETE

- 工程成果：单元测试验证配置选择与对象构造，真实 Integration Test 验证外部链路，故障实验验证日志排查能力。
- 最值得看的代码：
  - `tests/test_model_provider.py`
  - `tests/test_structured_logging.py`
- Hands-on 结果：当前 11 个测试通过；真实成功/失败记录均存在。
- Mini Review：对象构造测试通过不代表真实 API 一定可用；真实调用用于验证密钥、端点和网络，失败日志用于定位原因。

【Why Card：测试边界】

- 单元测试使用隔离配置/测试替身，不应烧 Token。
- Integration Test 只做必要次数，用来验证 `.env + Provider + 网络 + 模型` 的完整组合。

### Task 4：工程收尾与 Mini Review — ✅ COMPLETE

- 工程成果：Day 1 范围收束为一条可运行、可测试、可观测的模型调用底座。
- Hands-on 结果：用户已确认 Day 1 全部任务完成。
- 修改入口已经明确：
  - 配置字段：`src/agent_harness/config.py`
  - Provider 预设/校验：`src/agent_harness/model/config.py`
  - ChatModel 创建：`src/agent_harness/model/provider.py`
  - 请求与返回主链：`src/agent_harness/cli.py`
  - 日志与故障观察：`src/agent_harness/logging.py`、`.agent/logs/agent.jsonl`

## Current Task

**无。Day 1 已完成，当前没有待执行任务。**

Claude Code 不应重新执行或拆解上述 Task。用户若要求复盘，只使用下方总结进行短时回顾；用户若要求开始 Day 2，应先使用 Day 2 原始目标生成独立计划。

## Day 1 Final Summary（复盘用）

### 今天真正完成了什么

你完成了一个最小但完整的 AI 模型运行底座：配置从环境进入 `Settings`，经过 `ModelConfig` 得到完整模型参数，再由薄 `create_chat_model()` 创建统一的 `ChatOpenAI` 对象。CLI 使用异步 `ainvoke` 发起真实请求，得到 `AIMessage.content` 并输出；与此同时，Console 和 JSONL 记录运行成功或失败。单元测试验证配置和构造逻辑，Integration Test 验证真实 Provider 链路，故障实验验证了日志的排查价值。

### 最重要的 5 个工程认识

1. **ModelProvider 要薄**：它只把配置变成模型对象，不承担 Agent 推理、Tool 执行、重试或业务逻辑。
2. **OpenAI-compatible 是接入协议，不是厂商身份**：Qwen、DeepSeek、Tencent 可以复用同一 Adapter，但 model、base URL 和 key 不同。
3. **Async-first 是为后续能力留正确运行方式**：当前只是一次请求，未来 Streaming、并行 Tool、MCP 会直接受益。
4. **日志必须服务于 Debug**：先用 Console 看现象，再从 JSONL 找 Provider、Model、异常类型和错误信息。
5. **测试要分层**：单元测试稳定且不烧 Token；真实测试只验证外部组合是否真的可用。

### 一句话主链

> 用户输入经过配置和薄 Provider 创建 ChatModel，由异步 CLI 调用模型，返回文本，同时把成功或失败写入结构化日志。

### 最值得复习的代码

1. `src/agent_harness/model/config.py`：配置选择和早失败。
2. `src/agent_harness/model/provider.py`：最薄的模型创建边界。
3. `src/agent_harness/cli.py`：从输入到模型回复的完整主链。
4. `tests/test_model_provider.py`：如何不访问真实 API 验证 Provider。

### 5～10 分钟复盘提示

1. 为什么 ModelProvider 应该薄，而不是把 Retry、日志和业务判断都放进去？
2. 为什么 Qwen / DeepSeek 能使用同一种 ChatModel Adapter，但仍然需要不同配置？
3. 单元测试通过以后，为什么还需要一次真实 Integration Test？
4. 如果下一次模型请求失败，你会按什么顺序从配置、Provider、CLI 和 JSONL 排查？

### Day 1 到 Day 2 的接口

Day 1 已经解决“如何稳定获得并调用 ChatModel”。Day 2 才在这个模型对象上学习：

```text
bind_tools
  → AIMessage.tool_calls
  → name / args / id
  → ToolMessage 回填
```

现在仍然没有 Agent Loop，也没有真正的 Tool Runtime。这正是 Day 2 要继续补上的协议层。

## Backlog / Out-of-Scope

- 当前 `logging.py` / `cli.py` 的可观测性实现高于 Day 1 最小需求；本计划不安排源码级深挖或重构。
- JSONL 可能包含模型输入输出；正式产品阶段需要考虑日志脱敏与保留策略。
- 当前未检测到 `checkpoint-day-01` tag；如需要 Git checkpoint，可作为独立工程收尾补做。
- Function Calling、FakeModel/ScriptedModel、消息配对全部留到 Day 2。
