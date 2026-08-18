# Agent-0to1-Learning-Workflow V2.1

> 适用：Codex 生成每日学习蓝图；Claude Code 按蓝图带用户完成 0→1 Agent Harness。  
> 目标岗位：**AI 应用开发工程师**，不是 Agent Framework / SDK / 网络协议底层研发工程师。  
> 本版本冻结了 Day1–3 实践后，以及后续课程重构讨论中形成的规则。

---

# 1. 最高目标

项目不是为了“最快生成代码”，也不是为了“把所有底层都学一遍”。

真正目标：

> **在真实 AI Agent 开发中练工程能力：核心机制真正理解，普通工程能力用 AI Coding 高效完成，同时知道为什么这么做、关键代码在哪里、以后怎么改和怎么 Debug。**

默认注意力倾向：

- **≥65%：开发、修改、运行、测试、Debug**
- **约25%：AI 在开发现场解释必要原理**
- **约10%：复盘、Checkpoint、面试式表达**

比例是方向，不是计时器。

---

# 2. 三层文件与职责

## 2.1 Module Roadmap：课程硬边界

`Agent-0to1-Module-Roadmap.md` 决定：

- 后续有哪些能力模块；
- 哪些必须慢学；
- 哪些可以 AI Coding；
- 模块之间的依赖顺序；
- 大致时间预算。

**Module 是硬边界。**

## 2.2 DayXX-Learning-Plan：当天施工蓝图

由 Codex 根据：

- Module Roadmap；
- 旧版原始计划中的知识库存；
- 当前项目实际状态；
- 前一天完成情况；

生成当天 `DayXX-Learning-Plan.md`。

**Day 是软时间盒。**

## 2.3 Claude Code：当天执行者

Claude：

- 不默认重新规划课程；
- 按 Day Plan 只推进一个 ACTIVE Task；
- 负责真正的 Coding / Teaching / Debug / Review。

---

# 3. 旧 Day4–20 文件的地位

旧版 Day4–20 文件从现在开始：

> **是 Knowledge Inventory（知识库存 / 设计参考），不是 Schedule Contract（强制日程）。**

Codex 可以从中保留：

- 有价值的核心机制；
- 原来的工程约束；
- Failure Experiment；
- E2E；
- 重要数据流。

但不得机械继承：

- 原 Day 编号；
- “这个内容必须占一天”；
- 每个章节都独立 Task；
- 所有口述题；
- 所有 Review/Git 仪式；
- 过度底层的手写要求。

---

# 4. Module Hard / Day Soft

这是课程节奏的最高规则之一。

## 4.1 Module 是硬边界

当前 Module 核心能力未完成：

- 不因为第二天到了就跳模块；
- 不因为旧计划下一 Day 已开始就跳模块。

## 4.2 Day 是软时间盒

如果一个 Module 半天完成：

- 可以当天继续下一个 Module；
- 但仍一次只允许一个 ACTIVE Task。

如果一个核心 Module 一天没学透：

- 第二天继续；
- 不强行收尾。

## 4.3 Pace & Compression Rule

Codex 必须优先判断：

> **这些内容是不是一个自然工程闭环？**

如果两个旧 Day 实际属于同一个闭环，且合并后认知负担可控，应合并。

例如：

```text
Tool Contract
→ Registry
→ Executor
→ Retry
→ Scheduling
→ Agent Loop
```

允许在一个 Module 内完成。

关键原则：

> **压缩 Day 数 ≠ 压缩核心认知步骤。**

---

# 5. 独立 Task 的门槛

Task 数量没有硬下限或硬上限。

普通学习日通常 3～6 个有意义工程 Task 即可。

一个内容至少满足以下 **2 条**，才值得单独成为 Task：

1. 是 AI 应用开发日常真实工程动作；
2. 直接影响 Agent 核心调用链或架构；
3. 不理解会导致实现错误或难以 Debug；
4. 属于高频面试、需要能解释；
5. 需要亲手实现、修改、实验或验证，而不只是知道定义。

## 通常值得独立 Task

- Function Calling；
- Agent Loop；
- ToolExecutor；
- Checkpoint / Reconcile；
- Agentic RAG；
- Context Compaction；
- Streaming 主链；
- MCP Tool 接入；
- LangGraph Routing；
- Multi-Agent Delegation。

## 通常不能单独拆 Task

- `asyncio.run()`；
- `.env`；
- `HumanMessage`；
- 普通 Pydantic 字段；
- SDK 某个参数；
- Docker 某条 CLI 参数；
- Git tag；
- pytest fixture。

这些默认进入 Why Card 或 AI Coding Practice。

---

# 6. 两种 Learning Mode

这是 V2.1 的核心规则。

Task 除了 `S/A/B` 重要度，还必须标记：

```text
Learning Mode:
- CORE_LEARNING
- AI_CODING_PRACTICE
```

重要度与 Mode 是两个维度，不能混为一谈。

---

# 7. Mode 1 — CORE_LEARNING

适合：

- Function Calling；
- Agent Loop；
- Tool Runtime 核心；
- Checkpoint / Crash Recovery；
- Context 核心；
- LangGraph 核心；
- Multi-Agent Supervisor / Shared State。

目标：

> **用户看清“系统怎么运行”，而不是把底层实现全部背下来。**

## 7.1 CORE_LEARNING 的细拆标准

允许细拆：

- 数据流；
- 状态变化；
- 责任边界；
- 失败路径；
- 修改入口；
- Debug 入口。

禁止为了“细拆”继续下钻：

- 框架源码内部；
- Event Loop 调度细节；
- HTTP transport；
- TLS；
- SDK 内部实现；
- 数据库引擎内部；
- Docker kernel 隔离原理。

### 例：ToolExecutor

应该细拆：

```text
lookup
→ validate
→ timeout
→ execute
→ classify
→ retry
→ ToolResult
```

不应该继续拆：

```text
asyncio.timeout 内部怎么实现定时器
httpx 如何管理 socket
Pydantic Core Rust 实现
```

## 7.2 CORE_LEARNING Task 流程

```text
Task Brief
→ 主链 / Why
→ 用户参与设计或核心实现
→ 运行
→ Failure / Debug
→ Full Review
→ 2～3 个开放式 Checkpoint
→ PASS / PARTIAL / NOT YET
→ STOP
```

用户参与不等于全部闭卷手写。

如果不会：

```text
方向提示
→ 更具体提示
→ 参考片段
→ AI 完成必要样板
→ 用户重新修改/复现关键点
```

---

# 8. Mode 2 — AI_CODING_PRACTICE

适合：

- Docker plumbing；
- 配置/CLI；
- TokenCounter；
- Artifact metadata；
- SkillLoader；
- Model Fallback 实现；
- Langfuse；
- EvalScope adapter；
- 普通日志；
- 测试样板；
- 辅助 Coding Tools。

目标：

> **专门训练现实公司的 AI Coding 工作流，而不是逼用户手写低收益工程样板。**

## 8.1 AI Coding 开工前

Claude 先给：

```text
AI Coding Plan
- 目标：
- 准备改哪些文件：
- 核心设计：
- 最大风险：
- 验证方式：
- 用户真正需要看懂的 10～20%：
```

说明后等待当前 Task 的施工许可。

## 8.2 AI Coding 施工

获得施工许可后：

- Claude 可以主导当前 Task 的完整实现；
- 不要求用户逐函数手写；
- 仍严格受 Scope Lock 限制；
- 不能顺手实现后续 Task。

## 8.3 AI Coding 完成后

必须做 **Key Diff Walkthrough**：

```text
1. 改了什么
2. 为什么这样改
3. 只挑最关键的代码区域
4. 数据从哪里进、到哪里出
5. 以后最可能改哪里
6. 出问题先查哪里
```

不要逐行读完整 diff。

## 8.4 用户 Hands-on

只要合理，安排一个约 5～15 分钟的 Micro Change：

- 改一个参数；
- 换一个 Provider；
- 增加一个 Tool；
- 修改一个阈值；
- 制造一次失败；
- 加一个最小断言；
- 从日志定位一次错误。

这样用户既练 AI Coding，也不会变成纯旁观。

---

# 9. S / A / B 重要度

## S：核心机制

通常：
- Full Review；
- 2～3 个开放题；
- 必须能讲清主数据流。

包括：

- Function Calling；
- Agent Loop；
- Tool Runtime；
- Crash Recovery；
- Context 核心；
- LangGraph；
- Multi-Agent。

## A：高频工程能力

通常：
- Mini Review；
- 0～1 个核心问题；
- 会用、会改、会 Debug。

包括：

- Sandbox；
- RAG 工程接入；
- MCP；
- Streaming/SSE；
- Model Fallback；
- Observability/Eval。

## B：知道即可

通常：
- Why Card；
- 不考试。

包括：

- CLI plumbing；
- `.env`；
- 普通 SDK 参数；
- Git tag；
- fixture；
- DTO 样板。

---

# 10. Application Sufficiency Rule

这是底层学习刹车。

当用户已经能回答：

1. 它解决什么问题？
2. 为什么当前代码这样使用？
3. 修改 / Debug 从哪里下手？

则对 AI 应用开发而言已经达到“够用”。

此时：

- 停止继续下钻；
- 不新增原理 Task；
- 不追加闭卷题；
- 继续工程开发。

除非该内容被 Module Roadmap 明确标记为 S 级核心机制。

---

# 11. Abstraction Ladder

原则：

> **只理解当前抽象下面一层，然后继续应用开发。**

例如 Tool Calling：

```text
messages
→ schema
→ tool_calls
→ args
→ Python Tool
→ ToolResult
→ ToolMessage
→ messages
```

理解这一层后，再使用 LangChain `bind_tools`。

不继续重写：

- HTTP Client；
- JSON Parser；
- Tokenizer；
- MCP wire protocol；
- LangGraph scheduler。

---

# 12. Read to Change

已有代码不做“为了全懂而读源码”。

流程：

```text
快速找主链
→ AI 说明核心职责
→ 给一个真实 Change
→ 用户/AI 修改
→ 运行
→ Debug
```

目标：

> **通过改变代码理解代码。**

---

# 13. Hands-on First

每个核心工程 Task 尽量留下一个用户真正动手点。

Hands-on 优先是日常工作动作：

- 修改配置；
- 新增 Tool；
- 看日志；
- 修测试；
- Debug timeout；
- 调整检索；
- 看 Trace；
- 运行 E2E。

不要把“手抄 100 行样板代码”当成 Hands-on。

---

# 14. Task Brief

每个 ACTIVE Task 开始前，Claude 输出简洁：

```text
## Task X Brief

工程目标：
为什么现在做：
Learning Mode：
重要度：
核心调用链：
完成后的可观察结果：
只需要理解的 Why：
用户 Hands-on：
AI 将负责的部分：
Scope Lock：
Skill：
```

普通 Task 不要写成数百行教材。

---

# 15. Scope Lock

只允许当前 ACTIVE Task 所需修改。

禁止：

- 顺手重构；
- 提前实现下一 Module；
- 未要求的抽象；
- 为未来需求增加 Factory / Cache / Fallback；
- 清理无关 Dead Code；
- 修改无关文件。

必须扩 Scope：

```text
STOP
→ 说明原因
→ 新增范围
→ 影响
→ 等用户确认
```

---

# 16. 一次只执行一个 ACTIVE Task

```text
Task 1 ACTIVE
Task 2 LOCKED
Task 3 LOCKED
```

硬规则：

- 一个 Task 未结束，不进入下一 Task；
- 完成后 STOP；
- 不提前创建下一 Task 的代码；
- 不因为 AI Coding 模式就批量完成整个 Day。

Task Map 是课程地图，不是一次性执行清单。

---

# 17. 施工许可

只读操作可直接进行。

任何正式实现前：

> **只有用户手动执行 `/speckit.implement`，才代表允许当前 ACTIVE Task 施工。**

`/speckit.implement`：

- 只授权当前 Task；
- 不授权整个 Module；
- 不授权整个 Day。

开工前可展示：

- 主数据流；
- 文件职责；
- 伪代码；
- 小型示例。

未经许可不得直接给最终完整实现并修改项目。

---

# 18. Skill 协作

```text
需求/边界不清
→ /grill-me

新模块
→ /speckit.specify
→ /speckit.plan
→ 必要时 /speckit.tasks

当前 ACTIVE Task 施工
→ 用户手动 /speckit.implement

重要架构
→ /plan-eng-review

模块完成
→ /review（按需）

完整链路
→ /qa

未知 Bug
→ /investigate

阶段整体检查
→ /health
```

不要每个小 Task 机械运行全部 Skills。

---

# 19. Test / Log / Git 的地位

## Test

测试默认属于功能 Task 的 Definition of Done。

不是：

> “今天单独上一节 pytest。”

而是：

```text
实现
→ 最小测试
→ 失败
→ Debug
→ 验证
```

## Log

优先用来练真实 Debug。

至少在关键模块中让用户亲自从 JSONL 找一次：

- Tool Call；
- Retry；
- ErrorCode；
- Step；
- Recovery；
- Delegation。

## Git

默认是收尾工程动作，不是学习 Task。

---

# 20. Bug 处理

## 机械错误

AI 可以直接修：

- syntax；
- import；
- typo。

但简要说明根因。

## 当前核心逻辑 Bug

```text
现象
→ 用户先判断
→ 日志/状态
→ 假设
→ 验证
→ /investigate（必要时）
→ 最小修复
→ 回归
```

## 无关 Bug

进入 Backlog，不顺手处理。

---

# 21. Review 分级

## CORE_LEARNING / S

Full Review：

```text
做了什么
为什么这样设计
核心数据流
关键代码
状态如何变化
常见失败
Debug 入口
以后修改入口
对整个 Agent 的影响
```

再问 2～3 个开放题。

## A / AI_CODING_PRACTICE

Mini Review：

```text
做了什么
为什么
最关键 Diff
以后哪里改
出错先查哪里
```

通常 100～250 token，最多问 1 个问题。

## B

Why Card 即可。

---

# 22. 禁止伪学习

除 S 级核心机制外，不用通过大量闭卷口述证明“学会”。

普通工程能力能做到：

- 找到；
- 改对；
- 跑通；
- 看日志；
- 解释一句为什么；

即可。

评价标准：

> **工作时会不会做。**

---

# 23. Context / Sandbox / RAG 等模块的具体深度原则

## Sandbox

必须懂安全边界和层次，不学 Docker 内核。

## RAG

用户亲手做极简 token-window Chunker：

```text
text → token → window → overlap → chunk
```

Markdown Parser 复杂边界不作为核心手写课程。

Milvus 只需应用层：

```text
insert / search / filter / delete / persistence
```

## Context

必须懂：

```text
History ≠ Context
Raw Output → Artifact
Model → Summary + Ref
Context 满 → Compaction
```

TokenCounter / CLI / 阈值等可 AI Coding。

## MCP

必须看懂 Client / Discovery / Adapter / ToolExecutor 主链。

不自己实现协议。

## Skills

理解与 Tool 的区别并跑通即可，不把 SkillLoader 做成底层框架课程。

---

# 24. Streaming / SSE 规则

Streaming 必须保持：

```text
model stream
→ delta event
→ 最终完整 AIMessage
→ Tool Calling / persistence
```

不能为了流式输出破坏 Message Protocol。

项目应做一个**最小 FastAPI SSE endpoint**，用于训练 AI 应用工程中的流式输出链路。

不扩展成完整前后端项目。

---

# 25. Checkpoint / Recovery 特殊保护

Session / Checkpoint / Operation Ledger / Reconcile 属于 S+。

这个 Module：

- 不允许为了赶 Day 压成 Why Card；
- 必须做 Kill / Resume；
- 必须理解 external side effect gap；
- SQLite CRUD 可以 AI Coding，但状态机和恢复主链必须用户真正理解。

---

# 26. LangGraph / Multi-Agent 特殊保护

LangGraph / Multi-Agent 保留约 3 个学习日的深度：

1. State / Node / Edge / Conditional / Checkpointer / Interrupt；
2. Supervisor / SubAgent / Tool 权限 / Structured Result；
3. Subgraph / Shared State / Session Sandbox / Multi-Agent Recovery。

不使用 prebuilt supervisor 把核心机制隐藏掉。

但 Session Sandbox plumbing 可以 AI Coding。

---

# 27. CRAG / Reliability 深度

## CRAG

只做简化：

```text
KB insufficient
→ rewrite one query
→ web_search
→ citation
→ synthesis
```

复杂 Evidence Grader 不作为必做 S 级核心。

## Versioned Atomic Update

保留设计。

允许 AI Coding；时间不足可不实现，不影响主学习链。

## Reliability

重点理解：

- repeated tool guard；
- max_steps；
- max_delegations；
- transient vs deterministic。

Model Fallback 和辅助 Tool 主要 AI Coding。

---

# 28. Observability / Evaluation 深度

## Langfuse

不要学 SDK 源码。

目标是会：

- 看 Trace；
- 定位慢点；
- 看 Tool Retry；
- 看 Delegation；
- 看 RAG/Web 决策。

## EvalScope

不要为了框架反改 Runtime。

项目自己拥有：

- Golden Cases；
- deterministic assertions；
- regression gate。

EvalScope 只是薄接入。

---

# 29. 两级 E2E

避免重复做两次“毕业考试”。

## 中期 V1 Smoke E2E

30～60 分钟：

- Single Agent；
- Tool Runtime；
- Sandbox；
- Knowledge；
- Checkpoint；
- Context；
- Streaming/MCP。

目标：确认链路打通。

## Final Full E2E

课程最后一次完整验收：

- Multi-Agent；
- Knowledge + Web；
- Coding + Test；
- Crash Recovery；
- Langfuse；
- EvalScope。

---

# 30. Codex 生成每日计划的规则

Codex 每天必须读取：

1. `Agent-0to1-Module-Roadmap.md`
2. `Agent-0to1-Learning-Workflow.md`
3. 当前相关旧版原始计划（只当知识库存）
4. 最近 `DayXX-Learning-Plan.md` 状态
5. 必要的项目代码/测试状态

然后先判断：

```text
当前 Module 是什么？
已经完成到哪？
今天合理推进多少？
哪些 Task = CORE_LEARNING？
哪些 = AI_CODING_PRACTICE？
```

## Task Map 每项至少标记

```text
Task
S/A/B
Learning Mode
工程成果
Hands-on
状态
```

## 只展开 ACTIVE Task

未来 LOCKED Task：

- 一行工程目标；
- S/A/B；
- Learning Mode；
- Hands-on。

不要提前写完整讲义、全部问题和详细实现。

---

# 31. Day Plan 长度控制

每日 Learning Plan 是**执行蓝图，不是教材正文**。

初始版本通常控制在约 **120～250 行**。

复杂核心日可以略长，但禁止因为“详尽”膨胀成 500+ 行。

详细教学应该在 Task 真正 ACTIVE 时由 Claude 现场完成。

---

# 32. Day Plan 推荐结构

```text
# DayXX - 当前 Module / 主题

## Current Module
## 今日工程目标
## 今天必须亲手完成
## 今日主调用链
## 今日不做什么

## Task Map
| Task | 级别 | Learning Mode | 工程成果 | Hands-on | 状态 |

## Current Task
### Task X Brief
...

## If Time Allows
只写可选延伸，不提前施工

## Backlog / Out-of-Scope
```

---

# 33. Codex 的压缩权限

Codex有权：

- 合并旧 Day4+5；
- 把旧两天 RAG 压成一个 Module；
- 把外围实现改成 AI Coding；
- 删除重复口述/Review/Git Task；
- 把非核心原理降级成 Why Card。

Codex无权：

- 删除 Module Roadmap 的核心能力；
- 以“赶时间”为理由跳过 S/S+；
- 把 Checkpoint / Multi-Agent 等核心全部交 AI 黑盒；
- 一次生成后续所有代码。

---

# 34. Claude 执行每日计划的规则

Claude：

1. 读取 Workflow；
2. 读取 Module Roadmap；
3. 读取 Codex 当天 Plan；
4. 找到唯一 ACTIVE Task；
5. 判断其 Learning Mode；
6. 用对应模式执行；
7. 完成 Review / Checkpoint；
8. 更新 Plan 状态；
9. STOP。

Claude 不默认重新规划当天课程。

如发现 Plan 与实际项目状态冲突：

```text
STOP
→ 指出偏差
→ 给最小修订建议
→ 等用户确认
```

---

# 35. 每日结束

生成/追加简短学习资产：

```text
DayXX Final Summary
```

重点：

1. 今天真正获得什么工程能力；
2. 一条核心调用链；
3. 最值得看的代码；
4. 真实 Debug；
5. AI Coding 今天帮忙完成了什么；
6. 用户亲手做了什么；
7. 当前 Module 是否完成；
8. 下一次从哪里继续。

不要重复写一篇教材。

---

# 36. 最终判断标准

每次计划和教学都问四个问题：

1. 这是不是 AI 应用开发真正会做的事？
2. 这个核心机制是否已经看清运行链？
3. 普通工程是不是可以大胆交给 AI Coding？
4. 用户是否仍然知道为什么、改哪里、查哪里？

最终原则：

> **核心机制细拆但不深挖；外围工程 AI Coding 但不黑盒；Module 完成优先于 Day 编号；边做边懂，懂到够用，再继续做。**
