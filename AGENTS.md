# AGENTS.md

> 本文件定义所有 Coding Agent（ZCode / Codex / WorkBuddy / Claude Code …）在 `intelligence-agent` 项目中的默认行为。
> **Primary 不绑定工具名**：谁当前在干活，谁就是当下的 Primary Developer，负责主 Spec Kit 流程、
> 主要实现与集成。用户随时可以换人，规则不因此失效。
> 同一时刻有多个 Agent 并行时，非主开发的 Agent 承担 **独立审查 / Debug / Security / 验证**；
> 该职责由角色（谁在审查）成立，不由工具名成立。
> 纯工程模式：做项目，不做教学。

---

# 1. 最高需求来源

项目正式 Engineering Specification 位于**当前仓库内**：

```text
SPEC_ROOT = goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/
```

下文所有 `SPEC_ROOT/xxx.md` 均指该目录。三个仓库各有一份，绝对路径各自不同，
一律以**当前仓库根**为基准，不要抄另一个仓库的绝对路径。

**路径陷阱（勿踩）**：仓库根下另有一个 `docs/spec/`，内容是流式 UI PRD、
`Observable_Agent_Workspace_SDD/`、`web-ui-redesign-implementation-spec.md`——
**不是** Engineering Specification，其中不存在 `00_PROJECT_VISION.md` / `README.md`。
凡本文件写作 `SPEC_ROOT/...` 的，一律不要简写为 `docs/spec/...`。

旧的 Day / SourcePlan / Learning Plan 已失效，不再作为当前工程依据。

## 1.1 优先级

发生冲突时按以下顺序处理：

1. **用户当前明确指令**
2. `SPEC_ROOT/00_PROJECT_VISION.md`
3. 当前模块对应 Engineering Specification
4. `SPEC_ROOT/01_SYSTEM_ARCHITECTURE.md`
5. `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`
6. `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md`
7. 当前已批准的 GitHub Issue、Matt `to-spec` 产物与 Ticket 拆分
8. 实际代码与测试状态
9. 历史文档

实际代码与测试用于判断“当前实现到了哪里”，不能反向覆盖已经冻结的产品需求；若代码与规格冲突，应报告 Gap，而不是擅自把规格改成现状。

---

# 2. 首次进入项目的阅读协议

首次接手本仓库时，先完整读取：

1. `SPEC_ROOT/README.md`
2. `SPEC_ROOT/00_PROJECT_VISION.md`
3. `SPEC_ROOT/01_SYSTEM_ARCHITECTURE.md`
4. `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`
5. `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md`

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

1. 读取 `SPEC_ROOT/00_PROJECT_VISION.md` 中相关原则；
2. 读取当前任务对应模块规格；
3. 读取 `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md` 中相关部分；
4. 确认当前属于 `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md` 哪个 Phase；
5. 检查当前代码与测试；
6. 再开始 Review / Debug / Implementation。

下表规格文件均在 `SPEC_ROOT/` 下。

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
| MCP / Skills / Knowledge / Web | `09_MCP_SKILLS_KNOWLEDGE_WEB.md` |
| Multi-Agent / Dynamic SubAgent | `10_MULTI_AGENT_DELEGATION.md` |
| CLI / SSE / Web UI | `11_STREAMING_API_WEB_UI.md` |
| JSONL / Langfuse / Eval | `12_OBSERVABILITY_EVALUATION.md` |

---

# 4. 独立审查 / Debug / Security 职责

> 本节描述**角色**，不描述工具。任何 Agent 以「独立审查者」身份进场时（即不是当前主开发），
> 都按本节执行。

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

## 4.4 施工授权

只有用户、当前主开发或 ticket 明确分配的 Task 才写代码。

默认不承担整项目重新规划。

---

# 5. 工程规划边界

当前主开发（谁在干活谁就是，见文件头）维护 Matt SDD 主工程规划：

- `/grill-with-docs → /to-spec → /to-tickets → /implement` workflow
- GitHub Issue、Ticket 依赖与验收标准
- 主 Ticket 拆分与集成

非主开发的 Agent：

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

`SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`

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
- Lightweight 指 Core 小、边界清晰，不是删除 Recovery / Observability 等核心能力；
- 自检标准：资深工程师会觉得这段过于复杂吗？会则重写。

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
- 加 Context 能力 → 必须验证 token / artifact 边界；
- 多步任务先声明简要计划：每步对应一个验证检查（“循环直到达成具体目标”）。

## 9.5 懒惰阶梯（Reuse First 的执行细则，来源：ponytail skills）

动手前从上往下过一遍，停在第一级成立的，到第 7 级才写代码：

1. 这东西根本需要存在吗？不需要就一行说明然后跳过（YAGNI）；
2. 代码库里已有？→ 复用（重写隔壁文件已有的实现是最常见的浪费）；
3. 标准库有？→ 用标准库；
4. 平台原生特性覆盖？→ 原生；
5. 已装依赖能解决？→ 用它，不为几行代码加新依赖；
6. 能一行写完？→ 一行；
7. 到此才写：最小可用代码。

**不许偷懒的红线**：信任边界的输入校验、防数据丢失的错误处理、安全措施、
用户明确要求的一切——永不简化掉（与 §9.2 Lightweight 红线同源）。
阶梯缩短的是解法，不是阅读：先完整理解问题再爬梯，没读全代码就动手写出的
“最小修改”是第二个 bug。

## 9.6 工程八荣八耻

以瞎猜接口为耻，以认真查询为荣；
以模糊执行为耻，以寻求确认为荣；
以臆想业务为耻，以人类确认为荣；
以创造接口为耻，以复用现有为荣；
以跳过验证为耻，以主动测试为荣；
以破坏架构为耻，以遵循规范为荣；
以假装理解为耻，以诚实无知为荣；
以盲目修改为耻，以谨慎重构为荣。

> 保留价值：与 §6 Reuse First / §7 不变量 / §9.4 一一对应，且“诚实无知”
> 显式授权 AI 承认不知道（不装懂）——这是瞎猜接口的根治条目。每句都能落到
> 已有条款，不是新增约束，是已有约束的口诀化。

---

# 10. Skill 使用

**不维护静态清单**：可用 Skill 以当前 Agent 环境**实际枚举**为准。
（本文件旧版列举的 `/review`、`/investigate`、`/cso`、`/qa` 在当前环境中并不存在——
这既让 Agent 找不到命令，也违反了本节自己「不存在的命令不要伪造」的规矩。）

通用意图 → skill 对照（名称以实际枚举为准）：

| 意图 | skill |
| --- | --- |
| 代码审查 | `code-review` |
| 疑难 bug 根因定位 | `diagnosing-bugs` |
| 流程 / 架构疑问求助 | `ask-matt` |
| 代码库理解 | `understand` / `understand-chat` / `understand-diff` / `understand-domain` / `understand-explain` |
| 实现 / TDD | `implement` / `tdd` |

约定：

- 不存在的命令不要伪造，也不要把 skill 名当 shell 命令直接调用；
- 代码库图谱产物放 `.understand-anything/`，不提交；
- 已有图谱产物时优先复用，不重复全仓扫描。

---

# 11. 多 Agent 协作

- 主开发由「谁当前在干活」决定（见文件头），不绑定工具名；
- 非主开发的 Agent 默认做独立 Review、Debug、Security 或用户明确分配的 Task；
- 不重复生成同一模块；
- 修改前先检查 Git diff，避免覆盖其他 Agent 未提交工作；
- 遇到冲突先报告具体文件/范围；
- 交付时明确：
  - 改了什么；
  - 为什么符合 Spec；
  - 测了什么；
  - 还剩什么；
  - 是否存在风险/未决项。

如果用户明确将某一完整模块交给某个 Agent 主导，则该 Agent 可以负责该模块，但仍必须遵守同一 Engineering Specification，且不得创建与项目宪法冲突的平行架构。

---

# 12. 最终原则

> **Engineering Specification 决定“要做什么与不能做什么”；当前 ticket 决定“当前怎么施工”；主开发负责实现与集成，独立审查者的价值是找根因、守住边界、防止自证清白。**

---

# 13. 仓库模型与并行开发

## 13.1 三个独立仓库（不是 worktree）

| 目录 | 角色 | 常态位置 |
| --- | --- | --- |
| `D:\intelligence-agent` | 集成区 | `main` |
| `D:\intelligence-agent-backend` | 后端施工区 | `main`（干活时开短分支） |
| `D:\intelligence-agent-frontend` | 前端施工区 | `main`（干活时开短分支） |

### 拓扑事实（实测，勿凭目录名推断）

三者是**三个独立 Git 仓库（clone）**，**不是**同一仓库的 linked worktree：
各自有独立的 `.git`，对象库不共享，本地分支 ref 互不可见。

后果（必须遵守）：

- 跨仓库取对象必须先 `git fetch <path 或 url>`，不得假设对方的分支 ref 在本仓库存在；
- 三个仓库根各有一份 `AGENTS.md` / `CLAUDE.md` / `docs/**` 的 checkout，但它们**是同一个
  tracked 文件树**——内容由 git 保证一致，**不是**三份各自维护的副本，不会自动分叉；
- 真正的 linked worktree 只存在于**单个仓库内部**（`git worktree list --porcelain` 可查）；
  本仓库内可能另有 worktree，对它们只做只读检查，写操作需用户授权。

> **测量陷阱（踩过一次，代价是两份错误审计结论）**：main / backend 的 `core.autocrlf=true`
> （检出 CRLF），frontend 是 `input`（检出 LF）。**跨 clone 比较必须比 git 对象**
> （`git rev-parse <rev>:<path>`、`git diff --stat <sha>..<sha>`），
> **不要比工作树字节**（`diff`、`sha256sum`、直接拷文件）——否则每个文件都显示为全文件改写，
> 会得出"三个仓库已经漂移""`web/` 有两份不同拷贝"之类的错误结论。

### 开发规则

1. 三个仓库都可以施工；用哪一个是**分工选择**，不是硬边界（用户可以授权任一条线做另一端的活）。
2. 同一时刻，同一个文件只由一条线修改；并行会话不要共用同一个仓库目录。
3. 跨仓库操作用 `git -C <repo-path> <command>`，不用 `cd` 切换。
4. 公共项目资产通过 Git commit 进入版本控制，其他仓库 fetch/merge 后即可见：
   `docs`、`goal`、Spec、`AGENTS.md`、`CLAUDE.md`、`CONTEXT.md`、已确认源码、`tests`、正式配置。
5. 以下本地内容**不要求**跨仓库同步：`.env`、`.venv`、cache、`logs`、IDE 临时文件、
   runtime 临时文件、secrets。

## 13.2 核心模型：main 是稳态，干活开短分支

```text
平时：三个 clone 都停在 main —— "三方一致"是默认状态，一条命令可验
干活：在任意一个 clone 开短分支 → 施工 → 门禁 → 合回 main → push
```

不变式（可一行验证）：

```bash
git merge-base --is-ancestor main <feature-branch>   # main 永远是 feature 分支的祖先
```

**不要**把某个仓库长期挂在一条 feature 分支上：那样"三方一致"只能靠人记得维持，
每次集成之后另一条线会静默落后（历史上就是这么欠账的）。

## 13.3 短分支完成后的默认行为

在一条短分支上完成工作后，可以自行：

```bash
git status
git diff
git add <本次任务相关文件>
git commit -m "..."
```

哪些动作需要用户批准、哪些是常设授权，一律按 §14.4 的分类执行
（`merge`、`push`、删分支等仍是受控动作）。

完成后向用户报告：

- 完成了什么
- 改了哪些文件
- 测试结果
- commit 信息
- 是否建议合并

## 13.4 最终合并规则

最终集成统一在 `D:\intelligence-agent` 的 `main` 进行：

```text
feature branch
→ diff 检查 + 门禁全绿（§14.10）
→ merge 到本地 main
→ 在 D:\intelligence-agent 启动完整项目 / 跑全量门禁
→ 确认前后端集成正常
→ git push origin main（当前主开发执行，常设授权见 §14.4）
→ 通知另一条线把 main 合回来（§14.9）
```

**先合并到本地 `main` 并验证，再 push GitHub。** GitHub 不是仓库之间交换代码的必经步骤。
除非用户明确要求，否则不要默认「先 push feature 分支再通过 GitHub PR merge」。

## 13.5 `git diff` 的用途

合并前可以检查实际改动：

```bash
git diff main...feat/backend
git diff main...feat/frontend
```

`git diff` 只是检查差异；真正进入最终版本需要 `merge` 到 `main`。

---

# 14. Git Workflow / Merge Safety

本节定义跨分支集成、合并冲突、危险 Git 操作与集成 Approval 的长期规则，适用于所有 Agent 与所有仓库。它与 §13 配套：§13 定义「代码在哪个仓库、哪条分支上长」，本节定义「如何安全地把成果合进 `main`」。

## 14.1 Branch Roles

- `main` 是 **Integration Branch（集成主分支）**，必须始终可运行、可验证、稳定，
  也是三个仓库的**共同稳态**（§13.2）。
- 所有工作分支都是**短分支**：从 `main` 出发，合回 `main` 即结束使命。
- 集成由当前**主开发**执行（谁在干活谁就是，见文件头）。用户可以把它交给另一个 Agent，
  但同一时刻只能有一处执行，不允许并发集成。

## 14.2 Git 写操作前置检查

任何 Git 写操作前，必须先确认当前：

- repository（是三个仓库中的哪一个）
- branch
- working tree status

禁止：

- 凭目录名称猜 branch、或凭目录名猜仓库角色；
- 为了查看其他分支随意 `checkout` / `switch`；
- 在 dirty working tree 上执行 merge / rebase / reset。

真实映射来源：

```bash
git worktree list --porcelain    # 只反映**单个仓库内部**的 worktree
git branch --show-current
```

**注意**：main / backend / frontend **之间**不适用 worktree 概念（它们是独立 clone，见 §13.1）。
上面的 `git worktree list` 只能看到当前仓库自己的 worktree，看不到另外两个仓库。

跨仓库操作统一用：

```bash
git -C <repo-path> <command>
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
```

以及：只读源码分析、Test、Lint、Type Check、Diff Review。

**低风险同步操作（无需批准，但不是严格只读）**：

```text
git fetch origin --prune
git fetch <repo-path> <branch>:<ref>
```

它会写入对象库、更新 `FETCH_HEAD` 与 remote-tracking refs，并删除远端已不存在的
remote-tracking ref；不触碰工作树与本地分支。允许无批准执行，但跨仓库审计时要注意
它会改变「本地可见的 ref」。

## 14.4 Git 授权分类

### 常设授权（不必每次重新批准）

用户 2026-09-16 明确：**集成完成后由当前主开发执行 `push origin main`**，不必每次单独确认。

```text
把 main 合回自己的短分支        # 同步动作，§14.9 要求
集成：把验证过的短分支合进 main
集成验证通过后：git push origin main
```

前置条件（缺一不可）：

- 门禁全绿（§14.10）；
- 已按 §14.9 完成「一次只集成一条线」的顺序要求；
- 集成完成后通知另一条线把 main 合回来（§14.9）。

### 需用户明确批准（每次单独确认）

```text
在 feature / 短分支上 git push      # 向外发布未集成的工作，不在常设授权内
GitHub PR Merge
git cherry-pick
git revert
冲突解决后的 git add               # 先按 §14.7 做逐文件语义分析并报告
Branch 删除
Worktree 删除
```

### 默认禁止（除非用户针对具体操作明确批准）

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

## 14.9 一次只集成一条线 + 集成后回补

**禁止同时集成两条线。** 顺序固定：

```text
短分支 A → main（完成并验证）
→ 用新的 main 重新分析短分支 B
→ 短分支 B → main
```

A 合入 `main` 后，之前针对 B 做的 Conflict 判断**全部视为可能过期**，必须重新
`fetch` / `diff` / `merge-base` / conflict analysis。

### 集成后回补（"三方一致"靠这条维持）

`main` 每前进一次，**没被合入的那条线当场落后**。这不是比喻，是当场发生的事实：
集成那一刻只有来源分支与 `main` 对齐。

所以每次集成后必须：

1. 集成者**通知**另一条线（或用户）；
2. 另一条线在**下一次开工前**先自检：`git merge-base --is-ancestor main HEAD`——
   返回非 0 就说明落后，**先把 `main` 合回来再动手**；
3. 合并 `main` 属常设授权（§14.4）；若产生 Conflict，按 §14.7 停下做语义分析。

自检是**开工前的强制第一步**，不靠人记得。

## 14.10 Validation Gate

任何分支准备进入 `main` 前，至少检查：

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

## 14.12 Ticket 关单纪律

**完成的 ticket 必须立即关闭 GitHub issue，不留 OPEN。**（用户 2026-09-09 明确要求：「以后做完的都要关掉」。）

```bash
gh issue close <n> --comment "<验证证据：commit / 测试结果 / 关键文件>"
```

- 已合入 `main` 且门禁通过 → 直接关单。
- 代码完成但尚未合入 `main` → 关单 comment 必须写明分支与 commit，并注明集成由谁执行。
- 只完成部分交付（例如跨端 ticket 只做完一端）→ **不关单**，用 comment 记录已完成部分与剩余项。
- 关单前先核实实际状态（代码/测试），不要凭进度文档或记忆关单。

## 14.13 跨仓库协作的两条硬规则

### （a）文件级避让：同一个文件不要同时在两条线上改

用户允许任意一条线做另一端的活（backend 线可以做前端，frontend 线可以做后端），
所以**不做目录级所有权**——按目录说"这是前端的活"没有意义。真正会出事的是
**同一个文件被两条线同时改**。

开工前（尤其要改 `web/**`、`src/**`、`docs/**` 这类共享路径时）先看一眼对侧：

```bash
git -C <另一个仓库路径> fetch
git -C <另一个仓库路径> log --oneline main..HEAD -- <你打算改的路径>
```

看到对侧有未合并的同文件改动 → 先协调（等它合入，或改由那一条线做），不要并行写同一个文件。

### （b）跨 clone 比较用 git 对象，不要用工作树字节

三个仓库的行尾配置不同（main / backend 是 `core.autocrlf=true`，frontend 是 `input`），
同一份内容在两边的**工作树字节**不同。任何跨 clone 的 `diff` / `sha256sum` / 直接拷文件
都会显示成"全文件改写"，据此会得出"仓库已漂移"的错误结论（§13.1 的测量陷阱）。

正确做法：

```bash
git -C <repo> rev-parse <rev>:<path>       # 比对象
git -C <repo> diff --stat <sha>..<sha>     # 比提交
diff --strip-trailing-cr <a> <b>           # 万不得已比工作树时必须剥掉 CR
```

# 15. 前端 CSS 主题变量维护纪律（Ticket #35）

> 本节是这条规则的**唯一权威**。`web/PRODUCT.md` 引用的是本节。
> `web/` 在三个仓库里都存在（同一个 tracked 目录，§13.1）；无论你在哪一个仓库改
> `web/**`，本节都适用。

`web/src/index.css` 使用 `[data-theme]` 属性切换暗/亮主题。暗色 token 在 `:root` 中定义，亮色 token 在 `:root[data-theme='light']` 中覆盖。

**维护规则：**

1. 修改 `:root` 中的某个 token 时，必须检查 `:root[data-theme='light']` 是否也需要同步覆盖。
2. 新增 token 时在两个块都加定义，或确认亮色可安全继承暗色值。
3. 遗漏亮色覆盖 → 该 token 在亮色模式下仍用暗色值（对比度/可见性问题）。

CSS 原生没有变量组复用机制，手工双份同步是当前最小风险方案。


---

# 16. SDD 长任务工作流协议（入口）

> **触发条件**：用户明确要求「按顺序做剩余 tickets」「使用 SDD 方式」「每完成一个 ticket 必须 code-review」「出现 bug 用 diagnose-bug」「全部完成后用 improve-codebase-architecture」「不知道怎么做用 ask-matt」「完成后写提示词给集成 AI」。

**本节不复制流程细节。触发后第一个动作是读权威文件：**

1. `docs/SDD_WORKFLOW_PROTOCOL.md` —— 当前生效流程（**v2：批量审查循环**；v1 的
   「每票一次 `/code-review`、修复后循环到零 finding」**已作废**）；
2. `docs/SDD_TICKET_TRACKER.md` —— 在途 ticket、批次、fixed point、审查结论。

两者若与本文件不一致，**以它们为准**（本节只是入口）。

**自愈条款**：上下文被压缩 / 不记得批次边界 / 不确定当前在循环哪一步
→ 重读上面两份文件，**禁止凭记忆继续施工**。

## 16.1 进度落点分工

| 内容 | 落点 |
| --- | --- |
| 在途 ticket、批次、fixed point、审查结论 | `docs/SDD_TICKET_TRACKER.md` |
| Phase 状态、关键 commit、Gate 证据、集成记录 | `docs/PHASE_STATUS.md` |
| 一次性集成执行资料 | `docs/integration/`、`docs/INTEGRATION_PROMPT_*.md` |

规格文件（`SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md` 等）保持冻结，进度变更不回写规格。

## 16.2 不随协议版本变化的红线

- 每个 ticket 完成后：**门禁全绿才允许 commit**（命令见 `docs/SDD_WORKFLOW_PROTOCOL.md` §5）；
- 实现线默认只做**本地 commit**；集成与 `push origin main` 由当前主开发执行（§14.4）；
- 不覆盖其他 Agent 未提交的工作；
- 关单判定按 §14.12；跨端 ticket 只完成一端时**不关单**；
- 前端 / 后端的门禁工具链、在途进度落点，一律以当前仓库的
  `docs/SDD_WORKFLOW_PROTOCOL.md` 为准，本文件不再复制。

---
