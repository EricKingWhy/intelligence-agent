# Day04 - Module 4：Tool Runtime 完整闭环

> 状态：Task 1–5 全部 `DONE ✅`——Module 4（Tool Runtime 完整闭环）**全部 Gate 通过**。  
> 节奏决策：合并旧 Day 4+5 的自然工程闭环，但不压缩 Tool Runtime 的核心认知步骤。  
> 执行边界：Claude Code 按本蓝图一次推进一个 Task；Codex 只编排计划，不实现 Day 4 代码。

## Current Module

- **Module：** Module 4 — Tool Runtime 完整闭环
- **重要度 / 主模式：** S / `CORE_LEARNING`
- **当前起点：** Day 3 Agent Loop 已完成；Tool 仍是 `dict[str, Callable]`，由 `AgentRuntime` 自己查找、执行和宽捕获异常。
- **预计推进：** 从 0% 推进到完整闭环；建议时间约 3.5～4.5 小时。
- **今天结束后是否预计完成 Module：** 是；若任一核心 Gate 未通过，下一学习日继续 Module 4，不进入 Docker Sandbox。

## 今日工程目标

把 Day 3 的临时 Tool 执行升级成统一 Tool Runtime，使模型侧 Schema 与运行时 Tool 来自同一份 Contract，并让 Agent Loop 最终只通过 `ToolExecutor.execute_batch()` 获得结构化、可重试、可调度、严格配对的执行结果。

今天完成后，后续 Local Tool、Knowledge Tool 与 MCP Adapter 都能复用同一条执行主链，而不需要在 Agent Loop 中增加具体 Tool 的 `if/elif`。

## 今天必须亲手完成

1. 补写一个 `AddArgs` / `AddTool` Contract，修改参数与 Tool 描述，并检查最终导出的模型 Tool Schema。
2. 参与把 `AgentRuntime` 的临时 `tools[name](**args)` 接线替换为统一 `ToolExecutor`。
3. 制造一次 `INVALID_ARGUMENT`，确认 Tool 没有执行、Executor 没有重试、错误被回填给模型纠正。
4. 制造一次 Timeout 或 Transient Error，观察总 attempt 数和唯一 Retry Layer。
5. 验证全 READ_ONLY 批次并发；混入 MUTATING 后整批按模型原顺序串行。
6. 从 JSONL 中找到一次 `tool_call_id / tool_name / attempt / error_code / duration_ms`，并据此解释为什么重试或不重试。
7. 最后脱离代码口述整条 Tool Runtime 主链及 Registry、Executor、Agent Loop 的责任边界。

## 今日主调用链

```text
LLM tool_call{id, name, args}
  → ToolRegistry.get(name)
  → tool.args_schema.model_validate(args)
  → ToolExecutor timeout boundary
  → tool.execute(validated_args)
  → classify result/error
  → retry only when retryable
  → ToolExecution{tool_call_id, ToolResult}
  → ToolMessage(content=ToolResult JSON, tool_call_id=原 id)
  → Agent Loop 下一轮 LLM
```

批次调度只采用一条可解释规则：**全部 READ_ONLY 才并发；只要有一个 MUTATING，整批按原顺序串行。**

## 今日不做什么

- 不进入 Module 5：Docker、Sandbox、`read/write/edit/bash` 真实 Coding Tools。
- 不实现 Session、Checkpoint、Operation Ledger、MCP、RAG 或 Model Fallback。
- 不做复杂 Retry Budget、Circuit Breaker、Tool 依赖 DAG 或智能冲突分析。
- 不让 Registry 执行 Tool，也不让 Executor 调 LLM、决定 Agent 停止或维护 Session。
- 不深入 Pydantic Core、`asyncio.timeout` 内部调度、SDK transport 或 Backoff 算法理论。
- 不重构 Day 1/2 教学样本；只修改 Module 4 必须触及的 Agent Runtime、调试入口与测试。

## 当前项目起点

- `src/agent_harness/agent/runtime.py` 当前直接持有 `tools: dict[str, Callable]`，并通过 `_exec_tool()` 展开模型 args。
- Runtime 当前用宽 `except Exception` 把任意失败转成普通字符串；Module 4 要把 Lookup、Validation、Timeout、分类与 Retry 下沉到 ToolExecutor。
- `debug_loop.py`、`debug_real_loop.py` 和 9 个 Agent Loop 测试仍按旧 `tools=` 构造 Runtime；正式接入时需要最小迁移并保持原协议行为。
- 结构化日志已有 `tool_operation` 入口，可继续使用，不另建 EventBus。
- 当前基线：`23 passed`，Ruff 全绿；新行为测试继续使用 Fake Tool / ScriptedModel，不以真实 Provider 代替确定性验收。
- 工作区已有用户对 AGENTS、CLAUDE、Workflow 与 Source Plan 的未提交调整；这些不是 Codex 或 Day 4 实现需要清理的内容。

## Task Map

| Task | S/A/B | Learning Mode | 工程成果 | Hands-on | 状态 |
|---|---|---|---|---|---|
| Task 1：建立 Tool Contract、ToolResult 与 Registry | S | `CORE_LEARNING` | 形成 Tool 单一事实源、结构化结果、重复名保护及模型 Schema 导出 | 用户补写一个 Args Schema/描述，检查导出结果，并触发重复注册失败 | `DONE ✅` |
| Task 2：实现 Validation-first 单次执行链 | S | `CORE_LEARNING` | Executor 完成 lookup → validate → execute → error mapping；参数错误不执行、不重试 | 用户制造非法参数，断言 execute 次数为 0，并检查 INVALID_ARGUMENT 回填 | `DONE ✅` |
| Task 3：加入 Timeout、Error Classification 与唯一 Retry Layer | S | `CORE_LEARNING` | 暂时性错误按策略重试，确定性错误只执行一次，日志记录 attempt/duration/error | 用户触发 timeout/transient 与 permission 两类失败，从日志比较 attempt | `DONE ✅` |
| Task 4：实现批次调度与严格 ID 配对 | S | `CORE_LEARNING` | 全 READ_ONLY 并发；含 MUTATING 整批串行；结果按原 call 顺序和 id 返回 | 用户用 Fake Tool 测耗时/顺序，并故意打乱完成顺序检查配对 | `DONE ✅` |
| Task 5：Agent Loop 正式接入与 Module Gate | S | `CORE_LEARNING` | Runtime 只消费 ToolExecutor 结果并回填 ToolResult JSON；旧行为回归、JSONL Debug、模块 Full Review | 用户亲手完成核心接线，跑一次失败自纠错并从 JSONL 还原完整链路 | `DONE ✅` |

> 解锁规则：当前 Task 的测试、Review 和 Checkpoint 通过后，Claude 才将下一 Task 改为 `ACTIVE`；完成后立即 STOP，等待用户继续。

## Current Task

### Task 1 Brief：建立 Tool Contract、ToolResult 与 Registry

**工程目标**

- 在 `src/agent_harness/tooling/` 建立最小 Tool 数据模型层，不超过 Contract、Result/Error、Registry 与必要 Schema 转换职责。
- Tool Contract 至少统一：`name`、`description`、`args_schema`、`timeout_seconds`、`side_effect`、异步 `execute()`。
- 冻结 `ToolSideEffect = READ_ONLY / MUTATING`，以及统一 `ToolResult`：`ok / message / data / error_code / retryable / metadata / artifact_ref`。
- Registry 只提供 `register / get / list / export model tool definitions`，重复 Tool Name 在注册阶段明确失败。
- 用一个最小 `AddTool` 同时证明：模型看到的 name/description/parameters 与 Runtime 注册的 Tool 来自同一份 Contract。

**为什么现在做**

Day 3 的 `dict[str, Callable]` 只有“名字 → 函数”，没有 Schema、Side Effect、Timeout 或统一结果语义。若直接实现 Retry/并发，Executor 只能靠函数名和异常字符串猜行为。先冻结 Contract 和职责边界，后续执行策略才有可靠输入。

**可观察结果**

- 注册 `AddTool` 后，能按 name 获取同一个 Runtime Tool，并列出已注册工具。
- 导出的模型 Tool Definition 明确包含 `name / description / parameters`，参数字段与 `AddArgs` 一致。
- 注册第二个同名 Tool 立即抛出清晰的配置错误，不带冲突继续运行。
- 成功与失败 `ToolResult` 都能稳定 `model_dump_json()`，不依赖错误字符串猜 `error_code/retryable`。
- 当前 AgentRuntime 尚未迁移，Retry/Timeout/并发也尚未出现。

**核心数据流与责任边界**

```text
AddArgs(Pydantic)
  → AddTool Contract
      ├─ Registry：注册 / 查询 / 列表
      ├─ model schema：给 LLM 的 name / description / parameters
      └─ execute()：未来由 Executor 调用

ToolResult
  → 统一表达成功/失败语义
  → 后续同时服务 ToolMessage、日志、测试与恢复
```

| 组件 | 本 Task 的唯一职责 | 明确不负责 |
|---|---|---|
| Tool Contract | Tool 的身份、Schema、执行入口和策略元数据 | 注册、重试、调度 |
| ToolRegistry | 注册、查询、列出、导出模型定义 | Validation、Timeout、执行 |
| ToolResult | 结构化表达执行结果与错误语义 | 决定是否再次调用模型 |
| AgentRuntime | 本 Task 保持不变 | 暂不接入新 Tool 层 |

**必要 Why Card**

- **Contract 为什么是单一事实源：** 同一份 name/description/args_schema 同时生成模型菜单并约束 Runtime Tool，可避免“模型按 A 参数调用、执行端却期待 B 参数”的漂移。以后先查 Contract 与导出 Schema。
- **Description 为什么属于行为控制：** 它会影响模型何时选 Tool、怎样填参数；今天学会写清作用、适用时机和限制即可，不学习 Prompt 理论全集。
- **Registry 为什么不执行：** 它是路由目录；把 Timeout/Retry 塞进去会让注册配置与运行策略耦合，后续无法独立测试和替换 Executor。
- **ToolResult 为什么不是任意字符串：** 模型可以读 message，但 Runtime、日志和测试需要稳定的 `ok/error_code/retryable` 字段做确定性判断。

**用户 Hands-on（约 10～15 分钟）**

1. 在 Claude 给出的最小骨架中，亲手补全 `AddArgs` 两个字段和 `AddTool.description`，让描述包含“做什么、什么时候用、参数含义”。
2. 打印或断言 Registry 导出的 Tool Schema，逐项核对 Tool name、description 和 parameters 是否来自当前 Contract。
3. 临时注册第二个同名 `add`，先预测结果，再运行并确认它在注册阶段失败；随后恢复正确配置。
4. 给 Registry/Schema 测试补一个关键断言，证明改坏参数名或 Tool 名时测试会变红。

**AI 负责的部分**

- Claude 可主导 `ErrorCode`、`ToolSideEffect`、Pydantic DTO、抽象基类与 pytest fixture 等样板。
- 用户重点看 10～20%：Contract 字段如何成为 Schema 单一来源、Registry 的重复名分支、ToolResult 的稳定语义。
- Claude 完成样板后只做 Key Diff Walkthrough，不逐行讲 Enum、Pydantic 普通字段和 fixture。

**Scope Lock**

- 允许新建最小 `tooling/` 包及其直接测试；文件可按 Claude 主 Spec 调整，但不得继续拆出 Adapter/Plugin/Middleware 框架。
- 不改 `AgentRuntime`、`debug_loop.py`、`debug_real_loop.py` 或既有 Agent Loop 测试。
- 不实现 ToolExecutor、参数 Validation、Exception Mapping、Timeout、Retry、Backoff 或 Batch Scheduling。
- 不创建真实文件、Shell、Knowledge、MCP Tool；Task 1 只使用 Add/Fake Tool 证明 Contract。

**当前 Skill / 施工许可**

- 这是新 Module：Claude 维护唯一主 Spec Kit 产物，按需执行 `/speckit.specify → /speckit.plan`；必要时才生成 `/speckit.tasks`。
- 只有用户手动执行 `/speckit.implement` 后，才允许实现当前 Task 1；不授权 Task 2～5。
- Module 完成前不机械运行整套 `/review`、`/qa`；未知根因 Bug 才使用 `/investigate`。

**验收与 S 级 Checkpoint**

- Registry register/get/list、duplicate name、ToolResult JSON 序列化、Schema name/description/parameters 的最小测试通过。
- 原有 23 个测试无回归，Ruff 通过；测试不访问真实 API。
- Claude 做 ≤400 token Full Review：说明单一事实源、三组件职责、最值得看的代码、修改入口与配置 Debug 入口。
- 用户不看代码回答 3 题：
  1. 为什么模型 Tool Schema 与 Runtime Tool 必须来自同一份 Contract？分开维护最容易出现什么故障？
  2. Registry 和未来 Executor 的责任边界分别是什么？为什么 Retry 不能放进 Registry？
  3. ToolResult 已有可读 `message`，为什么仍需要 `error_code` 与 `retryable`？
- 结果标记 `PASS / PARTIAL / NOT YET`；只有 `PASS` 才解锁 Task 2。

## Module 4 Definition of Done

- Agent Loop 已从临时 `_exec_tool()` 切换为统一 `ToolExecutor.execute_batch()`。
- Contract 同时生成模型 Tool Definition 与 Runtime Tool；Registry/Executor 职责清晰。
- Validation 一定发生在执行前；INVALID_ARGUMENT 的 execute 次数为 0、Executor attempt 为 0/不进入重试循环，并回模型自纠错。
- ToolExecutor 是 Tool 执行域唯一 Retry Layer；Timeout/Transient 总尝试次数明确，确定性错误只执行一次。
- READ_ONLY 并发与含 MUTATING 整批串行均有行为证据，结果始终按原 call 顺序和 `tool_call_id` 配对。
- ToolResult 始终可 JSON 序列化并作为 ToolMessage content 回填，Agent Loop 原有成功、失败与 max_steps 行为无回归。
- JSONL 足以定位一次 Tool 的 name/id/attempt/duration/error/retryable，用户完成至少一次真实 Failure Debug。
- 用户通过 Module 4 Full Review，能口述完整 `Call → Registry → Validation → Executor → Retry/Scheduling → Result → Message → LLM` 主链。
- 全部 Task 与 Git 收尾完成后，Claude 在本文件追加简短 `Day 4 Final Summary`，说明用户 Hands-on、AI Coding、真实 Debug、Module 是否完成及下一入口。

## If Time Allows

- 仅在 Module 4 全部 Gate 通过后，运行一次真实模型 `add` smoke test：模型 Schema 来自 Registry，执行经过 ToolExecutor，最终回答正常。
- 可观察一次真实 Provider/SDK 自带 Retry 配置，记录潜在 Retry Amplification；不在本日扩展全局 Retry Budget。
- 不自动进入 Module 5，是否继续由用户决定。

## Backlog / Out-of-Scope

- Module 5：Docker Sandbox 与真实 `read/write/edit/bash` Coding Tools。
- INVALID_ARGUMENT 连续多轮修复次数上限、Repeated Tool Guard：留到后续 Reliability/Run State，不塞进 ToolExecutor 的同调用 Retry。
- Session/Checkpoint/Operation Ledger：后续 Recovery Module；今天没有 `operation_id`。
- MCP Tool Adapter、Knowledge Tool、权限策略系统：以后接入统一 Contract/Executor，不在今天预建框架。
- 复杂依赖分析、细粒度读写冲突、全局 Retry Budget、Circuit Breaker：V1 不做。
- Day 2 的故意教学错误和旧 TODO 保持原样，生产化清理由独立 Scope 决定。

## Day 4 Final Summary（2026-08-19，Module 4 全部 Gate 通过）

**Module 4（Tool Runtime 完整闭环）完成。** Task 1–5 全部 PASS，54 tests passed，ruff 全绿，Git 已推送。

**用户 Hands-on 完成的实作**：
- Task 1：补 AddArgs/描述 + 触发重复注册失败 + 补关键断言
- Task 2：填 ValidationError→INVALID_ARGUMENT 映射 + call_count==0 断言
- Task 3：填 TimeoutError→TIMEOUT 映射 + should_retry 三条件 + 慢工具 call_count==3
- Task 4：填 side_effect==MUTATING→serial；亲手验证 113ms 并发 / 318ms 串行 / 乱序保序
- Task 5：填 outcome=result.ok 映射；迁移 9 个 Agent Loop 测试夹具与两个失败断言

**AI Coding 主导**：Executor 三阶段骨架、Timeout/Retry/execute_batch 样板、测试工具（Counting/Slow/Flaky/Forbidden/Timed*）、AgentRuntime 迁移、debug 脚本 Contract 化。

**真实 Debug（Day 必做第 6 条）**：debug_real_loop 用真实模型跑通——happy path（add 一次成功）与失败自纠错（risky_add 超上限→TOOL_EXECUTION_ERROR→模型读 JSON 错误码建议改用 add）；从 JSONL 按 tool_call_id 还原了 Executor attempt 事件与 Runtime 回填事件的完整链路。

**Module 是否完成**：是。DoD 全部达成；Full Review 口述主链 PASS（请求→路由→把关→限时→止损→排队→回执，前 5 环执行域 / 第 6 环批次层 / 第 7 环 Runtime 接线）。

**下一入口**：Module 5（Docker Sandbox + 真实 read/write/edit/bash Coding Tools）——按 §8 等用户决定是否继续，不自行跨 Module。
