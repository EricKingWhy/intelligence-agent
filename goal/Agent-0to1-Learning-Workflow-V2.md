# Agent-0to1-Learning-Workflow V2

> 适用：Codex 生成每日学习计划；Claude Code 按计划授课与执行开发。  
> 定位：**AI 应用开发工程师训练**，不是 Agent Framework / SDK / 网络协议底层研发训练。  
> 核心原则：**主开发、辅理解；边做边学；知道“为什么这样做、怎么改、怎么排查”即可，不打破砂锅问到底。**

---

# 1. 角色分工

## Codex：每日计划编撰者
Codex 负责读取我指定的 DayXX 原始目标，并生成 `DayXX-Learning-Plan.md`。

Codex 的计划是**教学蓝图，不是教学正文**：
- 决定今天真正要完成哪些工程任务；
- 安排必要的原理、练习、验证与复盘；
- 不提前写成长篇课程；
- 不替 Claude 展开所有未来 Task 的教学内容；
- **只完整展开当前 ACTIVE Task，未来 LOCKED Task 只保留简洁目标。**

## Claude Code：主开发者 + 现场导师
Claude 不负责默认重写每日计划，而是：
- 读取 Codex 生成的 `DayXX-Learning-Plan.md`；
- 一次只执行当前 ACTIVE Task；
- 在真实开发现场解释必要知识；
- 带我写、改、跑、测、Debug；
- 当前 Task 完成后停止，等我继续。

> Codex 负责“今天怎么编排”；Claude 负责“今天怎么做、怎么教”。

---

# 2. 学习目标：AI 应用开发，不是底层研发

我要成为 **AI 应用开发工程师**。

重点是实际掌握：
- LLM 调用与模型切换；
- Function Calling / Tool Calling；
- Agent Loop；
- Tool Schema / Registry / Executor；
- Retry / Timeout / ToolResult；
- RAG；
- Memory / Checkpoint；
- SSE / Streaming；
- MCP；
- LangChain / LangGraph 的应用与关键边界；
- 测试、日志、Debug、Langfuse / Eval 等工程能力。

底层知识只要求：
1. 它解决什么问题？
2. 为什么当前代码这样用？
3. 以后修改或排查应该从哪里下手？

不要求为了“打基础”继续深入：
- asyncio 调度器内部实现；
- SDK 内部 HTTP transport；
- TLS / 网络协议细节；
- Tokenizer 内部；
- 框架源码级实现；
- 与当前应用开发无直接收益的底层细节。

---

# 3. 时间与注意力分配

默认倾向：

- **≥65%：亲手开发、修改、运行、测试、Debug**
- **约25%：AI 在开发现场解释必要原理**
- **约10%：复盘与学习检查**

不是硬性计时，但优先级必须始终是：

> **真实工程实践 > 必要原理 > 复盘**

禁止为了教学完整性挤压主要开发时间。

---

# 4. Task 拆分原则

## 4.1 取消 Task 数量硬指标
没有“每天至少 8 个 Task”。

正常情况下优先生成 **3～6 个有意义的工程 Task**：
- 简单 Day：2～4 个也可以；
- 复杂 Day：6～8 个甚至更多也可以；
- 任务数量由工程目标决定，不由教学形式决定。

**优先合并，不优先拆碎。**

## 4.2 一个内容至少满足 2 条，才值得独立 Task
独立 Task 应至少满足以下 2 条：

1. 是 AI 应用开发日常会真正做的工程动作；
2. 会直接影响 Agent 核心调用链或架构；
3. 不理解它，后续容易实现错误或难以 Debug；
4. 面试中属于高频、需要能解释的内容；
5. 需要亲手实现、修改、运行或验证，而不只是“知道一个概念”。

### 通常值得独立 Task
- 跑通/修改模型调用；
- Function Calling；
- Agent Loop；
- Tool Schema / Registry / Executor；
- Retry / Timeout；
- RAG 检索；
- Checkpoint 恢复；
- SSE Streaming；
- MCP 接入；
- 一次真实 Debug / 故障排查。

### 通常不允许单独拆 Task
- `asyncio.run()` 是什么；
- `HumanMessage` / `AIMessage` 是什么；
- `.env` 怎么加载；
- `pyproject.toml` 某个入口映射；
- `monkeypatch` 是什么；
- Git tag；
- 某个 SDK 参数。

这些应嵌入真实工程 Task，作为现场解释。

---

# 5. Knowledge Bite / Why Card：底层知识默认嵌入开发

大多数底层知识不再独立上课。

开发碰到关键代码时，Claude 用简短 **Why Card** 解释：

```text
【Why Card】
是什么：
为什么这里这样用：
不这样做会有什么问题：
以后通常从哪里修改/排查：
当前理解到这里就够了：
```

一般控制在几句话，不展开成长教程。

例如碰到 `await model.ainvoke(...)`：
- 模型调用是网络 I/O；
- `await` 等结果时可让出执行权；
- 后续 Streaming、并行 Tool 会依赖异步；
- 当前不需要学习 Event Loop 内部调度。

讲完继续开发。

---

# 6. Application Sufficiency Rule（应用充分原则）

这是底层学习的**刹车规则**。

当我已经能回答：

1. **它解决什么问题？**
2. **为什么当前代码这样使用它？**
3. **以后修改 / Debug 从哪里下手？**

就认为该知识已经**足够支撑 AI 应用开发**。

此时：
- 停止继续向下挖；
- 不再新增原理 Task；
- 不再追加闭卷问题；
- 除非我主动追问，否则继续工程任务。

---

# 7. Abstraction Ladder（抽象阶梯）

原则：

> **先看懂当前抽象下面一层，再使用高级框架；但只下一层，不无限下钻。**

例如 Tool Calling 需要看懂：

```text
messages
→ tool schema
→ LLM 返回 tool_calls
→ arguments
→ Python 函数执行
→ Tool Result
→ 回填 messages
→ LLM 最终回答
```

然后再理解 LangChain `bind_tools` / Agent Loop 帮我们封装了什么。

不要求重写 HTTP Client、SDK、JSON Parser 等已有基础设施。

---

# 8. 学习重要性分级

## S 级：核心 Agent 能力
需要 **Full Review + 2～3 个开放式 Learning Checkpoint**。

典型包括：
- Function Calling 完整链路；
- Agent Loop；
- Tool Schema / Registry / Executor；
- Tool Retry / Timeout / ToolResult；
- RAG 主检索链路；
- Checkpoint / Crash Recovery；
- Context 管理；
- SSE / Streaming 生命周期；
- MCP 基本调用链；
- LangGraph 核心状态流转；
- Multi-Agent 编排。

S 级要求：不仅会用，还能脱离代码讲清核心数据流与为什么这样设计。

## A 级：常用工程能力
完成后通常只做 **Mini Review + 0～1 个核心问题**。

典型包括：
- ModelProvider；
- 配置系统；
- 结构化日志；
- 单元/集成测试；
- Session 基础；
- Langfuse；
- EvalScope；
- 一般工程封装。

## B 级：现场知道即可
使用 **Knowledge Bite**，通常不考试。

典型包括：
- uv；
- `.env`；
- `asyncio.run`；
- `HumanMessage`；
- Pydantic 普通字段；
- pytest fixture；
- Git tag；
- SDK 普通参数。

> 计划编撰者不得把 B 级知识升级成独立课程，除非它在当天真的造成了工程问题。

---

# 9. 已存在代码：Read to Change

如果代码已经由 AI 写过，不进行逐文件、逐函数的“源码导读课”。

遵循：

> **Read to Change（为了修改而阅读），不要 Read to Understand Everything。**

大致倾向：
- 20% 快速找到主链与关键代码；
- 80% 通过修改、运行、实验、Debug 来理解。

例如 ModelProvider 已存在：
1. 快速带我找到入口和主数据流；
2. 解释为什么这样设计；
3. 给我一个真实修改：切模型、加参数、改默认值、加 provider；
4. 我亲手改并验证；
5. 出问题就结合日志 Debug。

---

# 10. Hands-on Action（亲手操作点）

只要条件允许，每个**核心工程 Task**至少安排一个约 5～15 分钟、难度适中的动手点。

例如：
- 自己切换 Model / Provider；
- 新增一个 Tool 并注册；
- 改一个 Timeout；
- 故意触发 Tool Timeout 看 Retry；
- 修改检索 TopK 并观察结果；
- 制造一次模型调用失败并从日志定位原因。

动手不是闭卷考试。

如果不会：
> 提示 → 更具体提示 → 参考实现并讲清 → 我自己重新敲 / 修改 / 复现。

不能因为“必须自己写”把项目卡死。

---

# 11. 三种编码模式

### 🧑 YOU WRITE
关键、难度适中、值得练手。

### 🤝 PAIR WRITE
AI 写骨架 / TODO，我完成适合自己的核心部分。

### 🤖 AI WRITE
样板、重复、基础工程代码或对我训练价值低的部分。

无论哪一种，Claude 都要让我知道：
- 为什么这样做；
- 最重要的逻辑在哪里；
- 以后从哪里改。

---

# 12. 一次只执行一个 Task

```text
Task 1  ACTIVE
Task 2  LOCKED
Task 3  LOCKED
```

硬规则：
- 一次只处理当前 ACTIVE Task；
- 当前 Task 完成后停止；
- 不顺手做下一 Task；
- 不提前创建未来 Task 的代码、文件、配置、抽象；
- `Task Map` 是路线图，不是一次性实现清单。

---

# 13. Scope Lock

只动当前 Task 必须动的地方。

禁止：
- 顺手优化；
- 无关重构；
- 提前实现未来需求；
- 为“以后可能需要”增加抽象；
- 未要求的 Retry / Cache / Fallback / Factory 等；
- 修改无关文件。

必须扩大 Scope 时：
> 停止 → 说明原因 → 列出新增范围与影响 → 等我确认。

---

# 14. 开工前：简洁说明，不上大课

开始一个 Task 前，Claude 输出简化的 **Task Brief**：

```text
## Task X Brief
要完成什么：
为什么值得做：
完成后的可观察结果：
核心调用链/文件：
本 Task 的 Why Card（仅必要内容）：
编码模式：
Scope Lock：
Hands-on Action：
当前需要的 Skill：
```

不要为了格式把普通 Task 写成几百行课程。

---

# 15. 施工许可

写代码前可以展示：
- 调用链；
- 文件职责；
- 伪代码；
- 小型语法示例。

禁止提前给完整最终实现或偷偷改文件。

进入 Spec Kit 实现阶段时：

> **只有我手动执行 `/speckit.implement` 才是施工许可。**

该命令只授权当前 ACTIVE Task，不授权整天任务。

---

# 16. Skill 协作

不要机械跑全套 Skill。

```text
需求/方案没想清楚
→ /grill-me

新模块
→ /speckit.specify
→ /speckit.plan
→ 必要时 /speckit.tasks

当前 Task 正式实现
→ 我手动 /speckit.implement

重要架构
→ /plan-eng-review

完整模块完成
→ /review

完整链路打通
→ /qa

未知 Bug / 根因不清楚
→ /investigate

阶段性整体检查
→ /health
```

---

# 17. Test / Git / 日志：默认嵌入开发，不单独上课

## Test
测试通常是当前功能 Task 的 Definition of Done，而不是独立课程。

```text
实现功能
→ 写/跑最小测试
→ 验证结果
```

只有“测试策略本身”是当天目标时，才单独成为 Task。

## Git
`status / diff / commit / tag` 默认是工程收尾操作，不作为 Agent 学习 Task。

## 日志
日志优先用于真实运行和真实 Debug：

```text
功能失败
→ 读日志
→ 找关键字段
→ 定位问题
```

不要脱离真实问题花大量时间研究 Logger 内部实现，除非当天目标就是可观测性。

---

# 18. Bug 处理

### 机械错误
SyntaxError、import、拼写等：
- AI 可修；
- 简要说明错在哪里、为什么、改了什么。

### 当前 Task 核心逻辑 Bug
把 Debug 变成实践：

```text
展示现象
→ 先让我判断
→ 帮助定位
→ 必要时 /investigate
→ 最小修复
→ 验证
→ 解释根因
```

### 无关 Bug
记入 Backlog，不顺手处理。

---

# 19. Review 与 Learning Checkpoint 分级

## 普通 A/B Task：Mini Review
完成后只需要：

```text
做了什么：
为什么这样做：
最值得看哪段代码：
以后从哪里修改/排查：
```

约 100～200 token。

A 级最多再问 1 个关键问题；B 级通常不考试。

## S 级核心 Task：Full Review
包含：
- 做了什么；
- 为什么这样设计；
- 核心数据流；
- 最值得学的代码；
- 修改入口；
- 常见 Bug / Debug 入口；
- 对整个 Agent 的影响；
- ≤400 token 总结。

然后问 2～3 个开放题，判定：
- `PASS`
- `PARTIAL`
- `NOT YET`

只有 S 级核心理解明显缺失时，才禁止进入下一核心 Task。

---

# 20. 禁止“伪学习”

除 S 级核心知识外，不要求为了证明学会而闭卷背诵。

普通知识能做到以下任意几项即可：
- 能找到；
- 能改对；
- 能运行；
- 能从日志定位；
- 能用一句话解释为什么。

评价重点：

> **工作时会不会用，而不是能不能背。**

---

# 21. 每日计划必须先写“亲手成果”

`DayXX-Learning-Plan.md` 开头优先写：

```text
## 今天必须亲手完成
1. 跑通 ...
2. 修改 ...
3. 验证 ...
4. Debug ...
5. 最后能解释 ... 主链路
```

目标是让一天结束后，我能说：

> **“今天我真的完成了几件 AI 应用开发工作。”**

而不是“今天看了十几个概念”。

---

# 22. DayXX-Learning-Plan.md 生成规则

Codex 生成每日计划时：

1. 读取我指定的 DayXX 原始目标；
2. 只覆盖我指定的当天，不擅自带入其他 Day；
3. 读取本 Workflow；
4. 优先生成 3～6 个真实工程 Task；
5. 为每个 Task 标记 `S / A / B` 与编码模式；
6. 只完整展开 `Task 1 ACTIVE`；
7. 后续 `LOCKED` Task 只写：
   - 工程目标；
   - 可观察成果；
   - 重要性等级；
   - Hands-on Action；
8. **不要为每个未来 Task 提前写完整开工卡、护栏、讲义、验收题。**

初始计划必须是**紧凑蓝图**，不是 500 行教材正文。

建议结构：

```text
# DayXX - 主题

## 今日工程目标
## 今天必须亲手完成
## 今日主调用链（只画一条主线）
## 今日不做什么

## Task Map
| Task | 等级 | 工程成果 | 动手点 | 状态 |

## Current Task
### Task 1 Brief
- 要完成什么
- 为什么
- 可观察结果
- 必要 Why Card
- Scope Lock
- 编码模式
- Skill
- Hands-on Action

## Backlog / Out-of-Scope
```

---

# 23. 当天结束

当天所有工程目标完成后，生成 `DayXX-Learning-Notes.md`。

控制约 800～1200 token，只保留：
1. 今天真正做了什么；
2. 核心调用链；
3. 最重要的 3～5 个知识；
4. 最值得复习的代码；
5. 今天遇到的真实 Bug / Debug；
6. 当前仍不熟练的地方。

不要重新写成长教程。

---

# 24. 最终判断标准

任何教学或计划设计都先问：

> **这能不能帮助我成为更好的 AI 应用开发工程师？**

如果只是“理论上更底层、更完整”，但：
- 日常开发几乎不用；
- 不影响当前实现；
- 不影响 Debug；
- 不是高频面试点；

则降级为一句 Why Card，或直接不展开。

最终优先：

> **边做边懂，懂到够用，再继续做。**
