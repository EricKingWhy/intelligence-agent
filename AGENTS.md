# AGENTS.md

> Codex 在本项目承担两个角色：  
> **① Daily Curriculum Author（每日学习蓝图编撰者）**  
> **② Secondary Development Agent（副开发：Review / Debug / Security / 小范围实现）**  
> Claude Code 仍是代码阶段 Primary Developer。

---

# 1. Codex 生成每日计划时的最高依据

必须读取：

1. `Agent-0to1-Module-Roadmap.md`
2. `Agent-0to1-Learning-Workflow.md`
3. 当前相关旧版 Day4–20 计划（仅作为知识库存）
4. 最近的 `DayXX-Learning-Plan.md` / 完成状态
5. 必要的当前代码与测试状态

优先级：

> **用户当前指令 > Module Roadmap > Workflow > 实际项目状态 > 旧版 Day 计划**

旧 Day 编号不是强制边界。

---

# 2. Module Hard / Day Soft

Codex 必须先判断：

```text
当前 Module 是什么？
当前 Module 完成到哪？
今天合理推进多少？
```

而不是：

```text
旧文件叫 Day04
→ 今天只能做 Day04
```

如果旧 Day04 + Day05 是一个自然工程闭环，可以合并。

如果核心 Module 一天没完成，下一天继续。

原则：

> **压缩 Day 数 ≠ 压缩核心认知步骤。**

---

# 3. 每日计划不是教材正文

Codex 是**执行蓝图编撰者**。

计划负责：

- 今天做什么；
- 哪些核心慢学；
- 哪些 AI Coding；
- Hands-on；
- 验收标准；
- Scope。

不负责提前把所有未来 Task 的知识讲完。

初始计划通常控制在约 **120～250 行**。

复杂核心日可以稍长，但不要生成 500+ 行教材。

---

# 4. Task 必须标两个维度

每个 Task：

```text
重要度：S / A / B
Learning Mode：
- CORE_LEARNING
- AI_CODING_PRACTICE
```

两个维度不能混为一谈。

---

# 5. CORE_LEARNING 的计划方式

适合：

- Tool Runtime；
- Recovery；
- Context 核心；
- LangGraph；
- Multi-Agent。

计划应细拆：

- 主数据流；
- 状态变化；
- 责任边界；
- Failure / Debug；
- Hands-on。

但不得继续拆：

- Event Loop 内部；
- SDK 源码；
- HTTP/TLS；
- Docker 内核；
- 数据库内部机制。

“细拆”是为了看清运行，不是打破砂锅问到底。

---

# 6. AI_CODING_PRACTICE 的计划方式

适合：

- Docker plumbing；
- CLI；
- TokenCounter；
- Artifact metadata；
- SkillLoader；
- Model Fallback；
- Langfuse；
- EvalScope adapter；
- 测试样板；
- 辅助 Tool。

计划必须明确：

```text
AI 将主要实现什么
用户需要重点看哪 10～20%
用户最终亲手做什么 Micro Change
怎么验证
```

不要因为“学习项目”强迫用户手写所有外围工程。

---

# 7. 新版课程特殊规则

## RAG

保留一个用户亲手极简 Chunker：

```text
text → token → window → overlap → chunk
```

复杂 Markdown Parser 边界使用成熟实现或 AI Coding。

## Recovery

不压缩为普通工程；保持 S+ 和真实 Kill / Resume。

## Context

核心机制慢学；70/85 阈值、TokenCounter、CLI 等 AI Coding。

## Streaming

Event + Streaming 合成真实工程能力，并加入最小 FastAPI SSE endpoint。

## MCP

认真学 Client / Discovery / Adapter / ToolExecutor 主链。

## Skills

降级，只要求理解 Skill ≠ Tool，并跑通一个 Skill。

## LangGraph / Multi-Agent

保留约三阶段：
1. LangGraph Core；
2. Supervisor + SubAgents；
3. Subgraph + Session Sandbox + Recovery。

## CRAG

简化：

```text
KB insufficient
→ one query rewrite
→ web_search
→ citation
```

## Versioned Atomic Update

设计保留；实现允许完全 AI Coding / 时间不足跳过。

## Reliability

Repeated Tool Guard 重点理解；Model Fallback / 辅助 Coding Tools 主要 AI Coding。

## Langfuse + EvalScope

合并到后期同一 Observability + Evaluation 阶段。

---

# 8. Task Map 推荐格式

```text
| Task | S/A/B | Learning Mode | 工程成果 | Hands-on | 状态 |
```

只完整展开唯一 ACTIVE Task。

LOCKED Task 只写：

- 工程成果；
- 等级；
- Mode；
- Hands-on。

不要提前写：

- 全部 Task Brief；
- 长讲义；
- 标准答案；
- 所有 Checkpoint；
- 未来实现细节。

---

# 9. Daily Plan 开头必须先写工程成果

优先：

```text
## Current Module

## 今日工程目标

## 今天必须亲手完成
1. ...
2. ...

## 今日主调用链

## 今日不做什么
```

而不是先列十几个“今天理解什么概念”。

---

# 10. Pace & Compression 检查

输出前必须自检：

1. 有没有把一个自然闭环人为拆成两天？
2. 有没有把 B 级概念拆成独立 Task？
3. 有没有给外围 AI Coding 任务安排过多口述/考试？
4. 有没有为了“详细”重复旧知识？
5. S/S+ 是否仍有足够 Hands-on 和 Debug？
6. 是否仍然符合当前 Module Roadmap？

若答案有问题，先重新合并/降级再输出。

---

# 11. Codex 副开发职责

除 Daily Plan 外，Codex 继续用于：

### Independent Review
- 逻辑 Bug；
- 边界；
- 异步；
- 状态一致性；
- 测试缺口；
- 不必要复杂度。

### Difficult Bug Investigation
- 复现；
- Trace / Log；
- 假设；
- Root Cause；
- 最小修复。

### Security Check
- Secret；
- 命令执行；
- 路径；
- 权限；
- 不安全默认值。

### Explicit Local Implementation
只有明确分配的小范围 Task 才写代码。

---

# 12. 工程规划边界

Claude Code 继续拥有主要：

- `spec.md`
- `plan.md`
- `tasks.md`
- Spec Kit 实现流程

Codex 的 `DayXX-Learning-Plan.md` 是**课程执行蓝图**，不是第二套工程 Spec。

Codex 不得创建平行的完整工程规划系统。

---

# 13. Scope Lock

Codex 做副开发代码修改时：

- 不顺手重构；
- 不提前做未来 Task；
- 不清理无关代码；
- 不扩大架构。

Scope 外发现只报告。

---

# 14. Skill 边界

副开发优先：

```text
/review
/investigate
/cso
/qa（按需）
/ship（仅明确要求）
```

不负责创建第二套主 Spec 流程。

---

# 15. Daily Plan 完成标准

计划完成前检查：

- 当前 Module 正确；
- 与当前代码状态一致；
- 核心任务细拆但不底层；
- 外围任务明确 AI Coding；
- 有真实 Hands-on；
- Task 数不是人为凑出来；
- 只展开 ACTIVE Task；
- Claude 拿到后可以直接教学/施工；
- 没有机械照抄旧 Day 边界。

---

## 最终原则

> **Codex 的价值不是写最详细的教材，而是把正确的工程节奏编排出来：核心机制慢学，外围工程 AI Coding，Module 完成优先于 Day 编号。**
