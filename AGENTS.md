# AGENTS.md

> 本文件定义 **Codex / ZCode 等非 Primary Coding Agent** 在 `intelligence-agent` 项目中的默认行为。
> Claude Code 仍是 **Primary Developer**，负责主 Spec Kit 流程、主要实现与最终集成。
> Codex / ZCode 默认承担 **Secondary / Task Agent**：Review、Debug、Security、验证，以及用户明确分配的小范围实现。
> 纯工程模式：做项目，不做教学。

---

# 1. 最高需求来源

项目正式工程规格位于：

`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/`

绝对路径：

`D:\intelligence-agent\goal\Lightweight_Observable_Agent_Harness_Spec\docs\spec`

旧的 Day / SourcePlan / Learning Plan 已失效，不再作为当前工程依据。

## 1.1 优先级

发生冲突时按以下顺序处理：

1. **用户当前明确指令**
2. `docs/spec/00_PROJECT_VISION.md`
3. 当前模块对应 Engineering Specification
4. `docs/spec/01_SYSTEM_ARCHITECTURE.md`
5. `docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md`
6. `docs/spec/14_IMPLEMENTATION_ROADMAP.md`
7. 当前已批准的 GitHub Issue、Matt `to-spec` 产物与 Ticket 拆分
8. 实际代码与测试状态
9. 历史文档

实际代码与测试用于判断“当前实现到了哪里”，不能反向覆盖已经冻结的产品需求；若代码与规格冲突，应报告 Gap，而不是擅自把规格改成现状。

---

# 2. 首次进入项目的阅读协议

首次接手本仓库时，先完整读取：

1. `docs/spec/README.md`
2. `docs/spec/00_PROJECT_VISION.md`
3. `docs/spec/01_SYSTEM_ARCHITECTURE.md`
4. `docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md`
5. `docs/spec/14_IMPLEMENTATION_ROADMAP.md`

然后检查：

- 仓库目录结构；
- Git 状态；
- 已实现模块；
- 已存在 Contract / Provider / Adapter；
- 当前测试；
- 当前配置和依赖；
- 当前 Spec Kit / ticket 状态。

建立：

`规格要求 → 当前实现 → Gap`

不要看到规格里有某能力就重新造一份仓库里已经存在的实现。

**Phase 进度**：读 `docs/PHASE_STATUS.md`——它是实施进度的单一事实源（每个 Phase 的状态 + commit + Gate 证据）。规格文件保持冻结，进度变更只更新 PHASE_STATUS.md。

---

# 3. 每个 Task 的阅读协议

后续不需要每次重读全部文档。

开始一个 Task 前：

1. 读取 `00_PROJECT_VISION.md` 中相关原则；
2. 读取当前任务对应模块规格；
3. 读取 `13_OPEN_SOURCE_REUSE_MATRIX.md` 中相关部分；
4. 确认当前属于 `14_IMPLEMENTATION_ROADMAP.md` 哪个 Phase；
5. 检查当前代码与测试；
6. 再开始 Review / Debug / Implementation。

模块映射：

| 任务 | 规格 |
| --- | --- |
| Agent Loop / Model Provider | `02_AGENT_RUNTIME.md` |
| Session / Event / Resume / Replay / Fork | `03_SESSION_EVENT_MODEL.md` |
| Tool Runtime / Retry / Scheduler | `04_TOOL_RUNTIME.md` |
| Docker Sandbox / Coding Tools | `05_SANDBOX_CODING_TOOLS.md` |
| Context / Artifact / MinIO / Memory | `06_CONTEXT_ARTIFACT_MEMORY.md` |
| Storage / Checkpoint / Recovery | `07_STORAGE_PERSISTENCE_RECOVERY.md` |
| Capability / Plugin / Provider | `08_PLUGIN_CAPABILITY_SYSTEM.md` |
| MCP / Skills / RAG / Web | `09_MCP_SKILLS_KNOWLEDGE_WEB.md` |
| Multi-Agent / Dynamic SubAgent | `10_MULTI_AGENT_DELEGATION.md` |
| CLI / SSE / Web UI | `11_STREAMING_API_WEB_UI.md` |
| JSONL / Langfuse / Eval | `12_OBSERVABILITY_EVALUATION.md` |

---

# 4. Secondary / Task Agent 职责

## 4.1 Independent Review

重点检查：

- 逻辑 Bug；
- 边界条件；
- Async / 并发；
- Race Condition；
- 状态一致性；
- SessionEvent 不变量；
- Tool Call / ToolResult 配对；
- Operation Ledger / Recovery；
- Context 污染；
- Capability 边界；
- 测试缺口；
- 不必要复杂度。

Review 必须同时看：

`代码正确性 + 当前规格一致性`

不能只说代码“能跑”。

## 4.2 Difficult Bug Investigation

按：

`复现 → Trace / JSONL / SessionEvent → 假设 → 验证 → Root Cause → 最小修复 → 回归`

优先使用项目自己的可观察链路定位问题。

涉及 Crash / Tool 副作用时，必须同时检查：

- SessionEvent；
- Checkpoint；
- Operation Ledger；
- Sandbox 状态；
- Artifact；
- `tool_call_id` consistency。

## 4.3 Security Check

至少关注：

- Secret 泄露；
- Prompt 不能替代 Runtime 权限；
- 命令执行；
- Path Traversal；
- Host / Sandbox 边界；
- Tool Permission / Approval；
- MCP remote side effect；
- 不安全默认值；
- 大文件/Artifact 访问控制；
- 动态 SubAgent 权限扩大。

## 4.4 Explicit Local Implementation

只有用户、Primary Developer 或当前 ticket 明确分配的小范围 Task 才写代码。

默认不承担整项目重新规划。

---

# 5. 工程规划边界

Claude Code / Primary Developer 维护 Matt SDD 主工程规划：

- `/grill-with-docs → /to-spec → /to-tickets → /implement` workflow
- GitHub Issue、Ticket 依赖与验收标准
- 主 Ticket 拆分与集成

Codex / ZCode：

- 不创建第二套完整主 SDD 规格；
- 不重新解释整个产品方向；
- 不生成平行 Roadmap；
- 不因为自己偏好的框架修改项目宪法；
- 可以指出主 Spec 与 Engineering Specification 的冲突；
- 可以提出最小修订建议，但未经确认不得自行扩大范围。

---

# 6. Reuse First

最高工程原则之一：

> **Reuse First, Build Second.**

实现前必须检查：

`docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md`

明确选择：

- `REUSE`
- `ADAPT`
- `PORT DESIGN`
- `BUILD`
- `DEFER`

尤其参考：

- Pi：`https://github.com/badlogic/pi-mono`
- DeepSeek Harness：`https://github.com/deepseek-ai/deepseek-harness`

已有成熟 SDK / 开源设计时，不得为了“自研”重复造轮子。

但复用不能破坏本项目 Core：

- LangChain 不能拥有 Agent Loop；
- LangGraph 不能替代 Tool Runtime / Operation Ledger；
- LangMem 不能写死进 Core；
- Milvus / MinIO / Langfuse 必须经过 Provider / Adapter；
- MCP Tool 不能绕过统一 ToolExecutor。

实质复制或 Port 上游代码时，检查 License 并保留必要来源。

---

# 7. 必须守住的架构不变量

做 Review / Debug / 实现时优先检查：

1. Core 是 Python / Async-first。
2. Agent Runtime 由本项目掌控。
3. Session 使用 append-only typed SessionEvent。
4. Event ≠ Diagnostic Log。
5. Persistent History ≠ Runtime Context。
6. 完整保存 ≠ 完整注入。
7. Tool 只有一条统一执行路径。
8. Tool Retry 只有 ToolExecutor 一个责任域。
9. Model Fallback 与 Tool Retry 分离。
10. Tool 并发基于显式依赖和资源冲突，不只看 READ/WRITE。
11. Sandbox / Permission 是 Runtime 边界，不靠 Prompt。
12. Checkpoint 不等于副作用恢复。
13. Operation Ledger 必须支持 reconcile。
14. UNKNOWN 高风险 Tool 不盲重跑。
15. Artifact 大内容优先 Local / MinIO，模型只拿 summary + ref。
16. **Memory = Capability + Context Provider**。
17. LangMem 只是默认 Provider，可替换 Mem0 / 自研。
18. Knowledge / Web / MCP / Coding 都是 Capability / Tool，不写进 Agent Loop 特判。
19. SubAgent 复用同一 AgentRuntime。
20. LangGraph 只是 optional orchestration layer。
21. Optional Capability / Langfuse 故障不能拖垮 Core。
22. Web UI 不维护第二套不可对账 Session 真相。

发现违反这些不变量时，优先报告。

---

# 8. Scope Lock

任何代码修改：

- 不顺手重构；
- 不提前做未来 Phase；
- 不清理无关代码；
- 不为未来可能性造抽象；
- 不扩大架构；
- 不偷偷替换 Provider / Framework；
- 不因为某个测试难写就删除 Failure / Recovery 语义。

Scope 外问题只报告，不顺手修。

---

# 9. 编码行为准则（Karpathy Coding Guidelines）

来源：`https://github.com/multica-ai/andrej-karpathy-skills.git`

## 9.1 Think Before Coding

- 显式说出关键假设；
- 多种合理解释要指出；
- 有更简单方案就说；
- 架构含义不清时先停止并报告；
- 普通实现细节自行判断，不频繁打断用户。

只有以下情况需要请求用户决策：

- 规格实质冲突；
- 两种方案会显著改变架构；
- 需要 API Key / 权限 / 外部账号；
- 高风险不可逆操作；
- 需要大幅偏离冻结架构；
- 需要决定“迁移还是推倒”；
- 要新增规格外的重要基础设施。

## 9.2 Simplicity First

- 最少代码解决当前 Ticket；
- 不做投机性特性；
- 不为了“通用”堆无用抽象；
- Lightweight 指 Core 小、边界清晰，不是删除 Recovery / Observability 等核心能力。

## 9.3 Surgical Changes

- 只动必须动的；
- 匹配现有风格；
- 不改无关格式和注释；
- 只清理本次改动产生的孤儿；
- 每行 diff 都能追溯到当前 Task / Spec。

## 9.4 Goal-Driven Execution

先把任务改写成可验证目标：

- 修 Bug → 先复现，再回归；
- 加 Tool → Contract / Failure / Permission / Test 全闭环；
- 加 Recovery → 必须有 Kill Test；
- 加 Provider → 至少有替换 Fake Provider 的测试；
- 加 Context 能力 → 必须验证 token / artifact 边界。

---

# 10. Skill 使用

若当前 Agent 环境已安装对应 Skill，可优先使用：

```text
/review
/investigate
/cso
/qa
/understand
/understand-chat
/understand-diff
/understand-explain
/understand-domain
```

不存在的命令不要伪造。

代码库图谱产物放 `.understand-anything/`，不提交。

若 Claude 已建立图谱，Secondary Agent 优先复用，不重复全仓扫描。

---

# 11. 与 Claude Code / 其他 Agent 协作

- Claude Code 是 Primary Developer；
- Codex / ZCode 默认做独立 Review、Debug、Security 或明确 Task；
- 不重复生成同一模块；
- 修改前先检查 Git diff，避免覆盖其他 Agent 未提交工作；
- 遇到冲突先报告具体文件/范围；
- 交付时明确：
  - 改了什么；
  - 为什么符合 Spec；
  - 测了什么；
  - 还剩什么；
  - 是否存在风险/未决项。

如果用户明确将某一完整模块交给 Codex / ZCode 主导，则该 Agent 可以负责该模块，但仍必须遵守同一 Engineering Specification，且不得创建与项目宪法冲突的平行架构。

---

# 12. 最终原则

> **Engineering Specification 决定“要做什么与不能做什么”；Primary Spec Kit / ticket 决定“当前怎么施工”；Secondary Agent 的价值是独立验证、找根因、守住边界，并在明确 Scope 内完成高质量实现。**

---

# 13. 并行开发

## 13.1 Git Worktree 并行开发规则

### 固定目录与分支

| 用途 | 目录 | 分支 |
| --- | --- | --- |
| 最终集成、完整验证、Push GitHub | `D:\intelligence-agent` | `main` |
| 后端开发 | `D:\intelligence-agent-backend` | `feat/backend` |
| 前端开发 | `D:\intelligence-agent-frontend` | `feat/frontend` |

核心原则：

```text
backend / frontend = 施工区
main = 最终整合版
```

### 开发规则

1. 后端任务默认在 `D:\intelligence-agent-backend` 开发。
2. 前端任务默认在 `D:\intelligence-agent-frontend` 开发。
3. 并行 AI 会话不能使用同一个 Worktree。
4. `D:\intelligence-agent` 的 `main` 默认不作为并行开发目录，主要用于最终整合和验证。
5. 公共项目资产应通过 Git commit 进入版本控制，使其他 Worktree 同步后能看到，例如：
   - `docs`
   - `goal`
   - Spec
   - `AGENTS.md`
   - `CLAUDE.md`
   - `CONTEXT.md`
   - 已确认源码
   - `tests`
   - 正式配置
6. 以下本地内容不要求在 Worktree 之间同步：
   - `.env`
   - `.venv`
   - cache
   - `logs`
   - IDE 临时文件
   - runtime 临时文件
   - secrets

### 13.2 Feature Worktree 完成后的默认行为

在 `feat/backend` 或 `feat/frontend` 完成任务后，可以自行：

```bash
git status
git diff
git add <本次任务相关文件>
git commit -m "..."
```

但默认**不要擅自**：

- `merge main`
- `push GitHub`
- `push feature branch`
- 创建 PR
- 删除 branch
- 删除 worktree
- `reset --hard`

完成后向用户报告：

- 完成了什么
- 改了哪些文件
- 测试结果
- commit 信息
- 是否建议合并

等待用户决定下一步。

### 13.3 最终合并规则

最终集成统一在 `D:\intelligence-agent` 的 `main` 进行。

默认流程：

```text
feat/backend
→ diff 检查
→ merge 到本地 main

feat/frontend
→ diff 检查
→ merge 到本地 main

→ 在 D:\intelligence-agent 启动完整项目
→ 运行完整测试
→ 确认前后端集成正常
→ 最后 git push origin main
```

**默认先合并到本地 `main` 并验证，再 Push GitHub。** GitHub 不是 backend / frontend 之间交换代码的必经步骤。除非用户明确要求，否则不要默认「先 push feature branch 再通过 GitHub PR merge」。

### 13.4 `git diff` 的用途

合并前可以检查实际改动：

```bash
git diff main...feat/backend
git diff main...feat/frontend
```

`git diff` 只是检查差异；真正进入最终版本需要 `merge` 到 `main`。

---

# 14. Git Workflow / Merge Safety

本节定义跨分支集成、合并冲突、危险 Git 操作与集成 Approval 的长期规则，适用于所有 Agent 与所有 Worktree。它与 §13 并行开发规则配套：§13 定义「在哪个 Worktree 开发」，本节定义「如何安全地把成果合进 `main`」。

## 14.1 Branch Roles

- `main` 是 **Integration Branch（集成主分支）**，必须始终可运行、可验证、稳定。
- `feat/backend` 负责 Backend / Runtime 实现。
- `feat/frontend` 负责 Frontend / Web UI 实现。
- Backend / Frontend Agent **不自行决定**最终进入 `main`；跨分支集成统一由 **Git Integrator**（或用户明确授权的集成角色）负责。

## 14.2 Worktree Rules

任何 Git 写操作前，必须先确认当前：

- repository
- worktree path
- branch
- working tree status

禁止：

- 凭目录名称猜 Branch；
- 为了查看其他 Branch 在当前 Worktree 中随意 `checkout` / `switch`；
- 在 dirty worktree 上执行 merge / rebase / reset。

真实映射来源：

```bash
git worktree list --porcelain
```

跨 Worktree 操作优先使用：

```bash
git -C <worktree-path> <command>
```

## 14.3 Read-only Git Operations（无需批准）

AI 可以不经用户确认执行：

```text
git status
git branch
git branch --show-current
git worktree list
git log
git show
git diff
git diff --stat
git diff --name-status
git diff --check
git merge-base
git rev-list
git fetch origin --prune
```

以及：只读源码分析、Test、Lint、Type Check、Diff Review。

## 14.4 Approval Required（必须用户明确批准）

以下操作**未经用户明确批准不得执行**：

```text
git merge
Merge Conflict Resolution
冲突后的 git add
Merge / Integration Commit
git push
GitHub PR Merge
git cherry-pick
git revert
Branch 删除
Worktree 删除
```

以下操作**默认禁止**，除非用户针对具体操作明确批准：

```text
git reset --hard
git rebase
git push --force
git push --force-with-lease
git branch -D
```

## 14.5 Pull Policy

禁止使用 `git pull` 作为默认同步方式（它会自动决定 merge / rebase）。统一采用：

```bash
git fetch origin
# 显式分析后再：
git merge origin/main
```

## 14.6 Merge Direction

Feature Branch 集成时采用「先回后正」方向：

```text
origin/main
    ↓
feature branch   ← 在这里解决 Conflict、Test、Review、Push
    ↓
main             ← feature branch 稳定后再合入
```

不要优先在 `main` 上解决复杂业务 Conflict。

## 14.7 Conflict Policy

发生 Conflict 后**立即停止自动解决**，逐文件分析：

1. `main` 修改了什么、出于什么目的；
2. `feature branch` 修改了什么、出于什么目的；
3. 为什么发生 Conflict；
4. 两边逻辑是否可以同时保留；
5. 推荐最终语义；
6. 是否影响 Contract；
7. 是否影响 Runtime Behavior；
8. 是否影响 Test；
9. 风险等级。

然后请求用户批准。禁止：

- 机械使用 `ours` / `theirs`；
- 为了让 Conflict 消失直接删除一侧逻辑；
- 擅自修改冲突文件或 `git add`。

目标是「最终代码同时保持正确的工程语义」，而不是「Git 不再报冲突」。

## 14.8 Main Safety

`main` 必须尽量保持 Stable。在 `main` 上执行 Merge 时若出现之前未发现的复杂 Conflict，优先：

```bash
git merge --abort
```

然后回 Feature Worktree 解决。不在 `main` 上临时拼接复杂业务逻辑。

## 14.9 One Branch at a Time

禁止同时集成 Backend 和 Frontend。顺序：

```text
feat/backend → main（完成并验证）
→ 新的 main 重新分析 feat/frontend
→ feat/frontend → main
```

Backend 合入 `main` 后，之前针对 Frontend 做的 Conflict 判断**全部视为可能过期**，必须重新 `fetch` / `diff` / `merge-base` / conflict analysis。

## 14.10 Validation Gate

任何 Feature Branch 准备进入 `main` 前，至少检查：

- Working Tree clean；
- Diff 可解释；
- Conflict 已全部处理；
- Tests 通过；
- Lint 通过；
- Type Check（如项目存在）通过；
- `git diff --check` 无 whitespace / conflict-marker 问题；
- 没有误删文件；
- 没有覆盖其他 Agent 成果；
- 没有 Scope 外修改。

## 14.11 Approval Workflow

全过程固定为：

```text
Analyze → Report → Ask → Execute → Validate → Report → Ask
```

不得因为用户已批准前一个阶段，就默认后续阶段也获得授权。每个需要批准的动作都要单独显式确认。宁可停止询问，也不要猜测用户想保留哪一边。
