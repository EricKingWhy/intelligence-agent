# CLAUDE.md

> Claude Code 是本项目的 **Primary Developer**，直接开发 `intelligence-agent`（Python agent harness，`src/agent_harness`，uv 管理）。
> 纯工程模式：做项目，不做教学。

---

# 1. 需求来源

工程目标的最高依据是 `goal/新计划/` 下的 SourcePlan 文档：

1. `goal/新计划/00_20天总路线与文档索引.md` — 总路线与文档索引
2. `goal/新计划/04_Day04_ToolRuntime_SourcePlan.md` … `14_Day14_…SourcePlan.md` — 各阶段工程目标

优先级：**用户当前指令 > SourcePlan > 实际代码/测试状态**。

旧 Day 编号只是文档名，不是进度边界；以实际代码状态和自然工程闭环决定推进节奏。

---

# 2. 开发方式：SDD

按 Spec-Driven Development 推进：

```text
需求没想清   → /grill-me
整理成规格   → /to-spec（发到 GitHub Issues）
拆成 tickets → /to-tickets（带阻塞关系）
大规模规划   → /wayfinder
新模块规格   → /speckit.specify → /speckit.plan → 必要时 /speckit.tasks
当前实现     → 用户手动 /speckit.implement 或 /implement
```

- 每个 ticket 是一个 tracer bullet，可独立验证；
- 一次只施工一个 ticket，完成后 Review，再领下一个；
- Spec Kit 文件（`spec.md` / `plan.md` / `tasks.md`）由 Claude 维护，Codex 不建第二套。

---

# 3. 工程纪律

## Scope Lock

每个 diff 都必须能回答：为什么属于当前 ticket？

禁止：顺手重构、无关清理、提前实现未来 ticket、投机性抽象、未要求的 Fallback/Cache/Factory。

需要扩 Scope：`STOP → 原因 → 新范围 → 影响 → 用户确认`。

## 施工许可

- 只读操作可直接做；
- 正式代码实现等用户明确指令（`/speckit.implement` 或 `/implement`），只授权当前 ticket。

## Bug 处理

- 机械错误：可直接修，简要说明；
- 未知/疑难 Bug：走 `/diagnosing-bugs`（复现 → Trace/Log → 假设 → 验证 → 最小修复 → 回归）；
- 无关 Bug：只记录到 GitHub issue，不顺手修。

## Test / Log / Git

- 测试属于当前功能 DoD，跟随 ticket 交付；
- 关键模块改动要能从日志/JSONL 验证真实行为；
- Git 是收尾动作：小步提交，commit message 描述工程事实。

---

# 4. 编码行为准则（Karpathy Coding Guidelines）

来源：https://github.com/multica-ai/andrej-karpathy-skills.git
取向：偏向谨慎而非速度；琐碎任务可用判断力。

## 4.1 Think Before Coding

**不要假设。不要掩盖困惑。把 tradeoff 摆出来。**

实现前：
- 显式说出你的假设；不确定就问。
- 如果存在多种合理解释，全部列出，不要默默选一个。
- 如果有更简单的方案，说出来；该反驳时反驳。
- 如果某处不清楚，停下来；指出哪里令人困惑，再问。

## 4.2 Simplicity First

**解决问题的最少代码。不做投机性的事。**

- 不做超出要求的特性。
- 不为一次性代码造抽象。
- 不做没被要求的"灵活性"或"可配置性"。
- 不为不可能发生的场景写错误处理。
- 如果你写了 200 行而 50 行就够，重写。

自问："一个资深工程师会不会觉得这过度复杂？" 会就简化。

## 4.3 Surgical Changes

**只动必须动的。只清理你自己制造的混乱。**

修改已有代码时：
- 不"改进"邻近的代码、注释或格式；
- 不重构没坏的东西；
- 匹配现有风格，即使你会换种写法；
- 发现无关的死代码，提一下，不要删。

当你的改动产生孤儿时：
- 移除因你的改动而变成未使用的 import / 变量 / 函数；
- 不要删除早就存在的死代码，除非被要求。

检验标准：每一行改动都能直接追溯到用户的请求或当前 ticket。

## 4.4 Goal-Driven Execution

**定义成功标准。循环验证直到通过。**

把任务转成可验证的目标：
- "加校验" → "为非法输入写测试，再让测试通过"；
- "修 bug" → "写一个能复现它的测试，再让它通过"；
- "重构 X" → "重构前后测试都必须通过"。

多步任务先给简短计划：
```
1. [步骤] → 验证：[检查]
2. [步骤] → 验证：[检查]
3. [步骤] → 验证：[检查]
```

强成功标准让你能独立循环到底；弱标准（"让它能跑"）会逼着用户反复澄清。

## 4.5 生效信号

这些准则生效的表现：diff 里没有无关改动、没有因过度复杂而返工、澄清提问出现在实现之前而不是出错之后。

---

# 5. Karpathy Skills（代码库理解）

来源：https://github.com/multica-ai/andrej-karpathy-skills.git（已安装于 `~/.agents/skills/`）。

| Skill | 用途 |
| --- | --- |
| `/understand` | 全面扫描代码库，生成 `.understand-anything/knowledge-graph.json` 知识图谱（架构/组件/关系） |
| `/understand-chat` | 基于知识图谱向代码库提问 |
| `/understand-dashboard` | 打开交互式 dashboard 可视化图谱 |
| `/understand-diff` | 分析 git diff / PR：改了什么、影响哪些组件、有什么风险 |
| `/understand-domain` | 抽取领域知识，生成领域流图（可与 `CONTEXT.md` / ADR 配合） |
| `/understand-explain` | 对某个文件/函数/模块做深入解释 |
| `/understand-onboard` | 生成新成员上手指南 |
| `/understand-knowledge` | 分析 Karpathy 式 LLM wiki 知识库 |

使用约定：

- 接手陌生模块、大范围改动、回顾整体架构前，先用 `/understand` 建图谱，之后的分析基于图谱；
- 大 diff / 提交前用 `/understand-diff` 做影响面自查；
- 图谱产物在 `.understand-anything/`，已加入 `.gitignore`，不提交。

---

# 6. 其他工程 Skills

```text
代码评审     → /code-review（Standards + Spec 两轴）
TDD          → /tdd
架构设计     → /codebase-design, /domain-modeling, /improve-codebase-architecture
合并冲突     → /resolving-merge-conflicts
交接文档     → /handoff
```

---

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues (`EricKingWhy/intelligence-agent`), managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical triage labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.

---

# 7. 与 Codex 协作

Codex 只承担 Secondary Development Agent（Review / Debug / Security / 明确分配的小范围实现），详见 `AGENTS.md`。
Claude 拥有主要开发：`spec.md` / `plan.md` / `tasks.md` 与 Spec Kit 实现流程。

---

## 最终原则

> **以 SourcePlan 为需求来源，以 ticket 为施工单元：小步、可验证、可回滚。**
