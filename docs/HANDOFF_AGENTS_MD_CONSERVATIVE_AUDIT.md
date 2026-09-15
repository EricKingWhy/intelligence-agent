# 交接手册：`AGENTS.md` / `CLAUDE.md` 保守型精简

| 项 | 值 |
| --- | --- |
| 交接方 | ZCode（Secondary Agent，**只读审计**，未改任何文件） |
| 接手方 | 任意 Coding Agent（Claude Code / Codex / ZCode 均可） |
| 日期 | 2026-09-15 |
| 工作目录 | `D:\intelligence-agent-backend`，分支 `feat/backend` |
| 审计对象 | 本仓库根 `AGENTS.md`（797 行）、`CLAUDE.md`（637 行） |
| 当前状态 | 工作树干净，HEAD `0e170ed` |

---

## 0. 一句话任务

把根规则文件里**已经发生的事实漂移和规则冲突**修正掉，让根文件回归「所有 Agent 都必须常驻知道的全局约束」，
**不是**追求行数下降；删规则不是目标，消除自相矛盾才是。

---

## 1. 铁律（覆盖一切默认行为）

1. **只改本仓库**。`D:\intelligence-agent`（main）、`D:\intelligence-agent-frontend`（feat/frontend）
   是**独立仓库**，本任务一律不碰；对它们只允许只读查看。
2. **禁止 `git push`**。本地 commit 可以，push 归集成 AI（`AGENTS.md` §13.2 / §14.4）。
3. **禁止 `merge` / `rebase` / `reset --hard` / force push / 删除分支或 worktree**。
4. **`.env` 零泄漏**：可以列 key 名，**绝不打印、提交、复制 key 值**到任何文档或命令输出。
5. **本仓库内另有 linked worktree** `D:\intelligence-agent-fixbug`（分支 `feat/FIX-test-BUG`）。
   只读，不修改，不在其中执行任何写操作。
6. **不碰 `web/` 的源码**。本任务最多只改根 `AGENTS.md` / `CLAUDE.md`（以及第二轮新增的
   `web/AGENTS.md` 这类**规则文件**，且只在第二轮且用户批准后）。
7. **不做顺手重构**：本次每一行 diff 都必须能映射到本手册 §4 的某一条（R1-x）。
8. **不确定就停下问用户**，不要自行决定架构方向（`AGENTS.md` §9.1）。

---

## 2. 动手前必须重新测量的现场事实

本手册所有数字是 **2026-09-15 实测快照**。**先复测，再施工**；数字对不上时以你的实测为准，
并在报告里写明差异。

```bash
cd /d/intelligence-agent-backend
git status --short && git branch --show-current          # 期望：干净 / feat/backend
wc -l AGENTS.md CLAUDE.md
grep -n '^#' AGENTS.md                                    # 章节行号基线
git worktree list --porcelain                             # 期望：本目录 + fixbug 两个
ls docs/spec/                                             # 期望：不是 Engineering Specification
ls goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/   # 期望：SPEC_ROOT 真身
```

---

## 3. 已核验事实基座

### 3.1 文件规模（2026-09-15 实测）

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `AGENTS.md` | 797 | 本任务主对象 |
| `CLAUDE.md` | 637 | 与 `AGENTS.md` 大面积重复 |
| `docs/SDD_WORKFLOW_PROTOCOL.md` | 165 | 当前生效流程（**v2**） |
| `docs/SDD_TICKET_TRACKER.md` | 1922 | 在途台账（会持续增长） |
| `docs/PHASE_STATUS.md` | 445 | Phase / 集成证据 |

### 3.2 ⚠️ 三条被修正的旧结论（上一轮审计说错了，以此处为准）

| 旧说法 | 实测真相 | 影响 |
| --- | --- | --- |
| 「backend 仓库没有根级 `docs/spec`」 | **错**。根下**确实有** `docs/spec/`，但里面是**另一套规格**：`01_AGENT_RUNTIME_STREAMING_UI_PRD_v2.md`、`02_RUNTIME_STREAMING_PROTOCOL_SPEC.md`、`03_FRONTEND_STREAMING_UI_IMPLEMENTATION_SPEC.md`、`Observable_Agent_Workspace_SDD/`、`web-ui-redesign-implementation-spec.md` | 这比「路径不存在」更危险：Agent 按 §2 读 `docs/spec/README.md` 会**读错文档族**而不是报错 |
| 「前端规则纯属污染 backend 上下文」 | **不准确**。backend 仓库内有 `web/`，**184 个 tracked 文件**，近期仍有提交（`f766848` / `47ea3e4` / `5e0a396`），且 `web/src/index.css` 与前端仓库副本**哈希不同** | §15 不能简单删除；也说明 backend worktree 确实会改前端 |
| 「`SDD_TICKET_TRACKER.md` 约 1787 行」 | 实测 **1922 行**（持续增长） | 不要把它的行数当验收指标 |

**`docs/spec/` 缺失文件已逐条核实**（这些在 `docs/spec/` 下**不存在**）：
`00_PROJECT_VISION.md`、`README.md`、`02_AGENT_RUNTIME.md`。

### 3.3 Git 拓扑（`git worktree list --porcelain` 实测）

| 目录 | 分支 | 真实关系 |
| --- | --- | --- |
| `D:\intelligence-agent` | `main` | **独立仓库**（集成区） |
| `D:\intelligence-agent-backend` | `feat/backend` | **独立仓库**（本目录） |
| `D:\intelligence-agent-frontend` | `feat/frontend` | **独立仓库** |
| `D:\intelligence-agent-fixbug` | `feat/FIX-test-BUG` | **本仓库的 linked worktree**（真 worktree） |

- 三个开发目录之间**不是** worktree 关系，对象库与本地分支 ref 互不可见。
- 本仓库另有遗留本地分支 `feat/FixBUG`（`4492eb6`），不是当前工作分支。
- `feat/backend` **没有配置 upstream**（`git rev-parse @{u}` → `fatal: no upstream configured`）。
- 实测 `origin/feat/backend` = `9727344`，HEAD = `0e170ed`，**领先 149 个 commit**。
  → 即：`feat/backend` 的推送状态与根规则里「worktree 天然共享」的隐含假设无关，
  跨仓库取对象**必须显式 fetch**。

### 3.4 根规则文件是「三仓库各自一份副本」

| 文件 | main 与 backend | frontend |
| --- | --- | --- |
| `AGENTS.md` | **完全相同**（sha256 `bfeb1ece771e4b90`） | **不同**（sha256 `f5859547aadd3776`） |
| `CLAUDE.md` | **完全相同**（sha256 `4bb3534e03049a76`） | **不同**（sha256 `7fbde00d6dc4855c`） |

→ **在 backend 修 `AGENTS.md` 不会传播到 main / frontend**。三者已漂移。
→ 交接必须写明：本轮只修 backend 的副本；main / frontend 的副本由各自会话或集成角色另行处理。

### 3.5 `web/AGENTS.md` 不存在

| 路径 | 状态 |
| --- | --- |
| `D:\intelligence-agent-backend\web\AGENTS.md` | **不存在** |
| `D:\intelligence-agent-frontend\web\AGENTS.md` | **不存在** |

→ 候选方案「把 §15 前端规则迁到 `web/AGENTS.md`」**前置条件尚未满足**，第一轮不能直接删 §15。

### 3.6 SDD 规则冲突（最高价值发现）

`AGENTS.md` §16 与 `docs/SDD_WORKFLOW_PROTOCOL.md` **直接矛盾**：

| `AGENTS.md` §16（旧，行号实测） | `docs/SDD_WORKFLOW_PROTOCOL.md` v2（生效） |
| --- | --- |
| §16.1 步骤 3：「`/code-review` … 循环直到零 finding」（L732-734） | §1.1：「**跳过**该票自带的 `/code-review`，改为批量审」 |
| §16.4：「**不允许跳过** `/code-review`：即使代码看起来没问题，也必须走完整 SDD 循环」（L769） | §1.2：每 2–3 个 ticket 对累计 diff 跑一次 |
| §16.2 步骤 4：「再 `/code-review` … 循环直到零 finding」（L752-753） | §1.2 第 5 条：修复后**不重跑全量 review**，仅架构/契约改动才增量复查 |
| §16.5：每个 ticket 完成后在 `PHASE_STATUS.md` 追加记录（L777） | §1.1：单 ticket 记 `SDD_TICKET_TRACKER.md` |
| §16.6：把前端门禁 / 前端 Tracker 规则塞进 backend 根文件（L785-795） | 前端仓库**已有自己的** `AGENTS.md` + `docs/SDD_WORKFLOW_PROTOCOL.md` |
| §16.2 写 `/diagnose-bug`（L749） | §1.2 写 `/diagnosing-bugs` |

→ 两个文件同时被自动加载时，Agent 无法判断该听哪个。**这是必须修的第一优先级问题。**

### 3.7 Skill 清单已漂移

`AGENTS.md` §10（L356-366）列举：`/review`、`/investigate`、`/cso`、`/qa`、`/understand*`。

实测当前环境**不存在** `/review`、`/investigate`、`/cso`、`/qa`；实际可用的是
`code-review`、`diagnosing-bugs`、`ask-matt`、`understand*`、`implement`、`tdd` 等。
`CLAUDE.md` §10（L494-527）列的又是**另一套**（`/code-review`、`/tdd`、`/codebase-design`…）。
两份文件对同一件事给出不同答案。

### 3.8 `git fetch origin --prune` 被错列为只读

`AGENTS.md` §14.3（标题「Read-only Git Operations（无需批准）」）在 L566 列出了
`git fetch origin --prune`。它会写对象库、更新 `FETCH_HEAD`、更新并**修剪** remote-tracking refs，
不是严格只读。审批豁免可以保留，**标签必须修正**。

### 3.9 前端 CSS 规则的多点重复（支持"暂不删、只加指针"）

`data-theme='light'` 相关规则出现在：
`web/src/index.css`、**`AGENTS.md` §15**、`web/PRODUCT.md`、
`docs/spec/Observable_Agent_Workspace_SDD/FRONTEND_AUDIT.md`、
`docs/design/CONTEXT_CAPACITY_DASHBOARD.md`、`docs/design/WEB_UI_BATCH_REDESIGN.md`、
`docs/adr/0032-custom-model-provider-management.md`（后三者为不同语境）。

→ 规则本身**未丢失风险**，但同一约束散落多处，将来必然再次漂移。

---

## 4. 第一轮：可直接施工的编辑清单

> 施工顺序建议 R1-1 → R1-2 → R1-3 → R1-4 → R1-5 → R1-6。
> 每完成一条，单独 commit（小步提交），commit message 描述工程事实。
> **只改 backend 仓库**。

### R1-1 修正规格路径漂移（最高优先级，零语义变更）

**问题**：§1.1 / §2 / §3 使用裸 `docs/spec/...`，在本仓库会命中**另一套规格目录**。

**改动 1｜`AGENTS.md` L11-19 整段替换**

现状（逐字）：

```text
项目正式工程规格位于：

`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/`

绝对路径：

`D:\intelligence-agent\goal\Lightweight_Observable_Agent_Harness_Spec\docs\spec`

旧的 Day / SourcePlan / Learning Plan 已失效，不再作为当前工程依据。
```

替换为：

```text
项目正式 Engineering Specification 位于**本仓库内**：

```text
SPEC_ROOT = goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/
```

下文所有 `SPEC_ROOT/xxx.md` 均指该目录。

**路径陷阱（勿踩）**：本仓库根下另有一个 `docs/spec/`，内容是流式 UI PRD、
`Observable_Agent_Workspace_SDD/`、`web-ui-redesign-implementation-spec.md`——
**不是** Engineering Specification，其中不存在 `00_PROJECT_VISION.md` / `README.md`。
凡本文件写作 `SPEC_ROOT/...` 的，一律不要简写为 `docs/spec/...`。

集成仓库（`D:\intelligence-agent`，`main`）内同一目录的绝对路径为
`D:\intelligence-agent\goal\Lightweight_Observable_Agent_Harness_Spec\docs\spec`，
仅供跨仓库集成时参考；backend 仓库一律使用上面的相对路径。

旧的 Day / SourcePlan / Learning Plan 已失效，不再作为当前工程依据。
```

**改动 2｜`AGENTS.md` §1.1 的 4 条路径**（L26 / L28 / L29 / L30）
把 `docs/spec/00_PROJECT_VISION.md`、`docs/spec/01_SYSTEM_ARCHITECTURE.md`、
`docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md`、`docs/spec/14_IMPLEMENTATION_ROADMAP.md`
分别改为 `SPEC_ROOT/...`。第 3 条「当前模块对应 Engineering Specification」保持不变。

**改动 3｜`AGENTS.md` §2 的 5 条路径**（L43-47）
`docs/spec/README.md` 等 5 条全部前缀 `SPEC_ROOT/`。

**改动 4｜`AGENTS.md` §3**
- L75 / L77 / L78 的 3 条清单项前缀 `SPEC_ROOT/`；
- 在模块映射表（L84）**上方**插入一行：

  ```text
  下表规格文件均在 `SPEC_ROOT/` 下。
  ```

**改动 5｜`CLAUDE.md` 同步同一修正**
- §1（L11-19）用与改动 1 相同的替换（保留 `CLAUDE.md` 自己的收尾句
  「旧的 Day / SourcePlan / Learning Plan 已被这套模块化规格取代，不再作为当前工程依据。」）；
- §1.1（L26-33）4 条路径；
- §2（L47-51）5 条路径；
- §3 模块映射表上方插入同一行说明，`SOURCE_TRACEABILITY.md` 那行（L101）改为
  `` `SPEC_ROOT/SOURCE_TRACEABILITY.md` ``。

**判据**：`grep -n 'docs/spec/' AGENTS.md CLAUDE.md` 的结果中，**不再出现**指向
`00_PROJECT_VISION.md` / `README.md` / `01_SYSTEM_ARCHITECTURE.md` / `13_...` / `14_...`
的裸 `docs/spec/` 路径。

**风险**：极低。纯路径修正，不改任何工程语义。

---

### R1-2 消除 `§16` 与 SDD v2 的冲突（第一优先级）

**做法**：把 §16（L719-797）**整体替换**为「触发条件 + 权威入口 + 进度落点分工 + 不随版本变化的红线」。
详细流程**不复制**，留在 `docs/SDD_WORKFLOW_PROTOCOL.md`。

替换文本建议：

```markdown
# 16. SDD 长任务工作流协议（入口）

> **触发条件**：用户明确要求「按顺序做剩余 tickets」「使用 SDD 方式」「每完成一个 ticket
> 必须 code-review」「出现 bug 用 diagnose-bug」「全部完成后用 improve-codebase-architecture」
> 「不知道怎么做用 ask-matt」「每完成一个 ticket 不许推送到远程 GitHub——这是集成 AI 做的事」
> 「完成后写提示词给集成 AI」。

**本节不复制流程细节。触发后第一个动作是读取权威文件：**

1. `docs/SDD_WORKFLOW_PROTOCOL.md` —— 当前生效流程（**v2：批量审查循环**；
   v1 的「每票一次 `/code-review`、修复后循环到零 finding」已作废）；
2. `docs/SDD_TICKET_TRACKER.md` —— 在途 ticket、批次、fixed point、审查结论。

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

- 每个 ticket 完成后：`ruff check` + 全量 `pytest` 通过才允许 commit；
- 禁止 `git push`：push 由集成 AI 执行（§13.2 / §14.4）；
- 不覆盖其他 Agent 未提交的工作；
- 关单判定按 §14.12；跨端 ticket 只完成一端时**不关单**。
- 前端 worktree 的门禁工具链与在途进度落点，以其**自己仓库**的
  `AGENTS.md` / `docs/SDD_WORKFLOW_PROTOCOL.md` 为准，本文件不再复制。
```

**同时删除**：旧 §16.1（单 ticket 循环，含「循环直到零 finding」）、§16.2（Bug 协议中的
「再 `/code-review` … 循环直到零 finding」）、§16.3、§16.4（「不允许跳过 `/code-review`」）、
§16.5（旧的每票 PHASE_STATUS 格式）、§16.6（前端 worktree 补充）。

**§16.6 可以直接删的依据（已实测，不是推测）**：
前端仓库**已有自己的一份** `AGENTS.md`（sha 与 backend 不同），且**同样带有 §16.6 内容**
（实测位于 `D:\intelligence-agent-frontend\AGENTS.md` L789-794），
前端仓库还有自己的 `docs/SDD_WORKFLOW_PROTOCOL.md`（165 行同构，第 164 行已写明前端门禁
`tsc + vitest + oxlint + playwright(--workers=2) + build`）。
→ 从 **backend** 副本删除 §16.6，对前端 Agent 零信息损失。

**判据**：
- `grep -n '零 finding\|循环直到' AGENTS.md` 无输出；
- `grep -n 'SDD_WORKFLOW_PROTOCOL' AGENTS.md` 有输出（入口仍在）；
- `grep -n 'workers=2' AGENTS.md` 无输出（前端门禁已不在 backend 根文件）。

**风险**：中。这是**语义**修正，改的是流程规则。理由充分（v2 已是生效协议、根文件是过期副本），
但建议在 commit message 里写明「对齐已生效的 SDD v2，非新增政策」，便于日后回溯。

---

### R1-3 修正 Git 拓扑术语（独立仓库 ≠ worktree）

**改动 1｜`AGENTS.md` §13.1 标题与内容**（L402-443）

- 标题 `## 13.1 Git Worktree 并行开发规则` → `## 13.1 固定工作区与分支映射（三个独立仓库）`
- 在「固定目录与分支」表**之后**、「核心原则」之前，插入实测事实块：

```markdown
### 仓库拓扑（实测，勿凭目录名推断）

| 目录 | 分支 | Git 关系 |
| --- | --- | --- |
| `D:\intelligence-agent` | `main` | **独立仓库**（集成区） |
| `D:\intelligence-agent-backend` | `feat/backend` | **独立仓库**（后端施工区） |
| `D:\intelligence-agent-frontend` | `feat/frontend` | **独立仓库**（前端施工区） |

三者是**三个独立 Git 仓库（clone）**，**不是**同一仓库的 linked worktree：
对象库不共享，本地分支 ref 互不可见。后果（必须遵守）：

- 跨仓库取对象必须先 `git fetch`（本地路径或远端），不得假设对方分支 ref 在本仓库存在；
- 三个仓库根**各有一份** `AGENTS.md` / `CLAUDE.md`，内容会漂移；
  修改本仓库的那一份**不会**传播到另外两个仓库；
- 真正的 linked worktree 只存在于单个仓库内部：本仓库内另有
  `D:\intelligence-agent-fixbug`（`git worktree list --porcelain` 可见）；
- 对非本仓库目录与其他 worktree **只做只读检查**，不执行写操作。
```

- 「开发规则」第 1 条「后端任务默认在 `D:\intelligence-agent-backend` 开发」保留；
  在末尾补一条：「跨仓库操作用 `git -C <repo-path> <command>`，不用 `cd` 切换」。

**改动 2｜§13.2 标题**：`### 13.2 Feature Worktree 完成后的默认行为`
→ `### 13.2 Feature 分支完成后的默认行为`（正文不动）。

**改动 3｜§14.2 Worktree Rules 补一句**：在「真实映射来源」代码块之后插入：

```markdown
注意：main / backend / frontend **之间**不适用 worktree 概念（它们是独立仓库）；
本节的 `git worktree list --porcelain` 映射只对**单个仓库内部**的 worktree 有效。
```

**判据**：`grep -n 'Worktree' AGENTS.md` 的剩余命中，每一处都不是在把三个开发目录称为 worktree。

**风险**：低。纯事实修正，不改变任何审批或流程语义。

---

### R1-4 修正 `git fetch origin --prune` 的分类

**改动**：`AGENTS.md` §14.3，把 L566 从「Read-only Git Operations」代码块中**移出**
（连同其后的「以及：只读源码分析…」行保持原位），在代码块之后插入：

```markdown
**低风险同步操作（无需批准，但不是严格只读）**：

```text
git fetch origin --prune
```

它会写入对象库、更新 `FETCH_HEAD` 与 remote-tracking refs，并删除远端已不存在的
remote-tracking ref；不触碰工作树与本地分支。允许无批准执行，但跨仓库审计时要注意
它会改变「本地可见的 ref」。
```

**判据**：§14.3 代码块内不再含 `git fetch`；新增段落含「不是严格只读」字样。

**风险**：极低。审批豁免不变，只是标签更诚实。

---

### R1-5 把 §10 的静态 Skill 清单改为动态发现

**改动**：`AGENTS.md` §10（L352-372）整节替换：

```markdown
# 10. Skill 使用

**不维护静态清单**：可用 Skill 以当前 Agent 环境**实际枚举**为准。
（本文件旧版列举的 `/review`、`/investigate`、`/cso`、`/qa` 在当前环境中并不存在，已删除。）

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
- 已存在图谱时优先复用，不重复全仓扫描。
```

**`CLAUDE.md` 同步**：§10（L494-527）保留 `understand*` 表，把「其他可用 Skill」列表
改为指向 `AGENTS.md` §10 的一句话（避免两份文件再次漂移）。

**判据**：`grep -n '/review\b\|/cso\|/qa\b' AGENTS.md CLAUDE.md` 无输出。

**风险**：低。删除的是不存在命令的清单。

---

### R1-6 文件头声明「三仓库各自一份副本」

**改动**：`AGENTS.md` 开头（L1-5 引用块内）追加一行：

```markdown
> **本文件是三仓库各自的副本之一**（main / backend / frontend 各有一份，内容可能已漂移）。
> 改动仅对当前仓库生效；其他仓库的副本需在各自仓库另行提交。
```

`CLAUDE.md` 开头同样追加。

**判据**：两份文件开头 5 行内出现该声明。

**风险**：极低。纯声明。

---

## 5. 第二轮：需先建前置产物（前置未满足前**不要**执行）

### R2-1 前端 CSS 规则迁出根文件（候选方案 O）

**前置条件（当前**均未**满足，逐条核实后再动）**：

1. 本仓库 `web/AGENTS.md` 尚不存在 → 需先创建；
2. 需确认 Agent 会自动加载子目录 `AGENTS.md`（**这是关键未知项**，
   未确认前删除根 §15 会造成规则空窗）；
3. §15 的规则在 `web/src/index.css`、`web/PRODUCT.md` 等处已有内容，
   迁移时要做**去重后的单一权威版本**，不是复制第三份。

**最低风险做法**：先**只加指针**，不删内容 ——
在 §15 顶部加一行「本节规则同见 `web/PRODUCT.md`；修改 `web/**` 前先读该文件」，
把删除留到加载机制确认之后。

### R2-2 合并 §13 / §14 的 Git 规则

§13 只保留**仓库所有权与目录映射**；§14 作为 Git 流程唯一权威。
`§13.3 最终合并规则` 与 `§14.6 Merge Direction` 存在重叠，应以 §14.6 的
`origin/main → feature → main`（先回后正）为准，§13.3 改为引用。

**注意**：这是结构性改写，属第三轮更合适；第二轮最多做「§13.3 加一句指向 §14.6」。

---

## 6. 第三轮：需用户拍板（**不要自行执行**）

| 编号 | 事项 | 为什么需要用户 |
| --- | --- | --- |
| R3-1 | 压缩 §9.5 懒惰阶梯 / §9.6 八荣八耻 | 这两节是**用户 2026-09-15 明确要求新增**的（commit `1ad7781`）。压缩等于回退用户刚做的决定 |
| R3-2 | 压缩 §9.2 / §9.4 的 Karpathy 补充条目 | 同上，用户明确要求补齐 |
| R3-3 | 合并 §4 / §5 / §6 / §7 / §11 等章节 | 结构改写，影响所有 Agent 的加载内容 |
| R3-4 | 让 `CLAUDE.md` 只保留 Claude-specific 内容，其余引用 `AGENTS.md` | 改变 Primary Developer 的工作方式，需用户确认 |
| R3-5 | 同步修正 main / frontend 两份副本 | 跨仓库写操作，且用户已明确「不动 main」 |
| R3-6 | 重写 `docs/integration/MERGE_EXECUTION_ORDER.md` | 内含 2026-09-10 旧 SHA / 旧测试数，属历史快照；改写方式需用户定 |
| R3-7 | 修正 `docs/SDD_WORKFLOW_PROTOCOL.md` §4 残留的 FE-T7/T8/T9 清单 | 该阶段已完成，通用协议不应携带阶段性 ticket 清单；但这是流程文件，改动需用户知情 |

---

## 7. 绝对不要动

1. 架构不变量（§7）与 Tool Runtime 统一执行路径；
2. SessionEvent / Persistent History / Runtime Context / Artifact / Operation Ledger / Recovery 边界；
3. Optional Provider / Langfuse 故障不得拖垮 Core；
4. SubAgent 复用 AgentRuntime、权限收窄、LangGraph 可选；
5. `REUSE / ADAPT / PORT DESIGN / BUILD / DEFER` 决策枚举；
6. 模块 → Engineering Spec 映射表（只做 R1-1 的路径前缀，不改映射关系）；
7. §4 Secondary Agent 的 Review / Debug / Security 职责；
8. Crash/Recovery 必须检查 Ledger / Checkpoint / Sandbox / Artifact 与事件配对；
9. §14.4 / §14.7 / §14.8 / §14.11 的全部审批与冲突规则；
10. §14.12 关单纪律；
11. §15 前端 CSS 规则**本体**（R2-1 前置未满足前不删）；
12. §9.5 / §9.6 / §9.2 补充 / §9.4 补充（用户 2026-09-15 新增）；
13. `docs/PHASE_STATUS.md` 既有历史记录、`docs/SDD_TICKET_TRACKER.md` 既有批次记录；
14. `web/` 下任何源码。

**安全红线（原样保留，永不简化）**：

- 凭证零泄漏：`.env` 值绝不打印 / 提交 / 复制进文档；key 名可列，值不可列；
- 用户提供的 API key 只允许存在于 gitignored 的 `.env`；
- never force-push / rebase / reset-hard；
- merge main、push main 归集成 AI；
- 不修改其他 worktree，只做只读检查；
- merge、push、冲突解决后的 `git add`、cherry-pick、revert、删除 branch/worktree
  必须用户明确批准；
- 不在 dirty worktree 上 merge / rebase / reset；
- 禁止机械使用 `ours` / `theirs`；禁止为了让冲突消失删除一侧逻辑。

---

## 8. 门禁与验收

```bash
cd /d/intelligence-agent-backend

# 1) 白空格 / 冲突标记
git diff --check                          # 期望：无输出

# 2) 改动面必须只有规则文件
git status --short                        # 期望：仅 AGENTS.md / CLAUDE.md（+ 本手册）
git diff --stat

# 3) 结构自检
grep -n '^#' AGENTS.md                    # 章节编号必须连续、无重号
wc -l AGENTS.md CLAUDE.md                 # 记录数字，但**不作为验收指标**

# 4) R1 各项判据（见 §4 各条）
grep -n '零 finding\|循环直到' AGENTS.md    # 期望：无输出
grep -n 'docs/spec/' AGENTS.md CLAUDE.md   # 期望：无指向 SPEC_ROOT 文档的裸路径
grep -n 'workers=2' AGENTS.md              # 期望：无输出
grep -n '/cso\|/qa' AGENTS.md CLAUDE.md    # 期望：无输出

# 5) 文档改动不影响代码门禁，但按项目惯例跑一遍确认没意外
ruff check src/ tests/
pytest -q                                  # 记录实际数字（不要照抄任何历史数字）
```

**注意**：本次只改 Markdown。若 `pytest` 出现与本改动无关的失败，
**先确认它是否 flaky / 是否来自并行会话干扰**，不要带病提交，也不要把无关失败算作本次回归。

**验收标准**：

- R1-1 ~ R1-6 全部完成且各条判据通过；
- 无 §7 清单中的任何文件被修改；
- 每一条 diff 都能映射到 R1-x；
- 报告格式见 §9。

---

## 9. 交付报告格式

```markdown
## 交接手册执行报告

**范围**：D:\intelligence-agent-backend（feat/backend），仅规则文件
**基线**：HEAD <sha> → 结束 <sha>

### 已完成
| 编号 | 内容 | 文件 | 判定判据实测结果 |
|---|---|---|---|
| R1-1 | 规格路径 SPEC_ROOT 化 | AGENTS.md / CLAUDE.md | grep 结果：… |
| R1-2 | §16 对齐 SDD v2 | AGENTS.md | … |
| … | … | … | … |

### 未完成 / 跳过
| 编号 | 原因 | 建议 |
|---|---|---|

### 门禁
- `git diff --check`：…
- `ruff check`：…
- `pytest`：N passed / M skipped / 0 failed（如实填写；有 flake 要写清）
- 改动文件清单：…

### 现场事实与手册基座的差异
（§3 的任何数字/结论与实测不符，在此逐条列出）

### 需要用户裁决的开放问题
（引用 §6 / §10）

### 未推送声明
本地 commit 已完成；**未 push、未 merge**（§13.2 / §14.4）。
集成提示词：docs/INTEGRATION_PROMPT_*.md（如有）
```

---

## 10. 开放问题（需用户裁决）

1. **§16 的替换是否直接执行？** 依据是 v2 已是生效协议、根文件是过期副本。
   属语义修正，建议执行；若用户要求"只加指针不动旧文"，则改为在 §16 顶部加
   「本节已过期，以 v2 协议为准」并保留旧文（不推荐，冲突仍在）。
2. **main / frontend 两份副本是否同步？** 当前用户口径是「不动 main」。
   若不同步，三仓库规则会继续分叉（frontend 的 `AGENTS.md` 已经与 backend 不同）。
3. **`CLAUDE.md` 的定位**：继续做 `AGENTS.md` 的 637 行镜像，还是收敛为
   Claude-specific 薄文件 + 引用？后者需要用户确认 Primary Developer 的工作方式不变。
4. **`docs/spec/`（流式 UI 那一套）的归属**：它与 `SPEC_ROOT` 同名不同物，
   是否要改名（例如 `docs/spec-streaming/`）以彻底消除歧义？属改名操作，需用户批准。
5. **backend 仓库内的 `web/`**：184 个 tracked 文件、近期仍在改，且 `index.css`
   与前端仓库副本哈希不同。这是有意的双份，还是历史遗留？需要用户定性，
   否则 §15 规则与「前端在 frontend 仓库」的口径会长期矛盾。

---

## 附录 A：Source of Truth 分层（建议冻结）

| 内容 | 权威落点 |
| --- | --- |
| 长期产品需求、架构边界、MUST / MUST NOT | `SPEC_ROOT/`（`goal/.../docs/spec/`），冻结 |
| 已拍板架构决策 | `docs/adr/` |
| 当前 SDD 执行流程 | `docs/SDD_WORKFLOW_PROTOCOL.md`（v2） |
| 在途 ticket / 批次 / fixed point | `docs/SDD_TICKET_TRACKER.md` |
| Phase 状态 / 集成 commit / Gate 证据 | `docs/PHASE_STATUS.md` |
| 一次性集成执行资料 | `docs/integration/`、`docs/INTEGRATION_PROMPT_*.md` |
| 实现现状与回归证据 | 代码 + 测试 |
| 三仓库 git / 合并规则 | `AGENTS.md` §13（拓扑）+ §14（流程） |

---

## 附录 B：候选建议 A–R 裁决（供执行时判断"哪些不做"）

| 候选 | 裁决 | 要点 |
| --- | --- | --- |
| A | 部分同意 | 删重复绝对路径可以；固定 workspace 映射必须留。规格路径改 repo-relative → 见 R1-1 |
| B | 部分同意 | 首次 bootstrap 与每任务增量阅读职责不同，可合并表达，不可删任一套 |
| C | KEEP / 轻压 | 模块映射表高价值，保留 |
| D | KEEP / COMPRESS | Secondary Agent 审计职责是核心，只压表述 |
| E | MERGE | 身份声明与规划边界合并为 Role & Authority，不删规划边界 |
| F | 部分同意 | 矩阵细节归 `SPEC_ROOT/13_...`；Core 边界留在根文件 |
| G | KEEP | 架构不变量第一轮整体保留 |
| H | MERGE | Scope Lock + Surgical Changes 合并，保留"越界先停"与"每行可追溯" |
| I | KEEP | Simplicity First / Goal-Driven Execution 保留 |
| J | 部分同意 / COMPRESS | 阶梯可压，**但属 R3（用户新增内容）** |
| K | 部分同意 | 八荣八耻可提炼，**但属 R3（用户新增内容）** |
| L | REWRITE | 静态 skill 清单 → 动态发现 → 见 R1-5 |
| M | COMPRESS | 协作与交付字段压成两段，保留信息 |
| N | MERGE | §13 拓扑 / §14 流程分工 → 见 R2-2（结构改写留后） |
| O | MOVE，不可立即 DELETE | 前置：`web/AGENTS.md` + 加载机制确认 → 见 R2-1 |
| P | MOVE / POINTERIZE | §16 只留入口 → 见 R1-2 |
| Q | 按 v2 重写 | 不改成"只修 High"；按 v2 分级 + 增量复查 |
| R | 否决硬目标 | 行数不是指标；本次验收看判据，不看行数 |

---

## 附录 C：本手册自身的状态

- 本手册由只读审计产出，**未修改** `AGENTS.md` / `CLAUDE.md` 或任何其他既有文件；
- 未执行任何 Git 写操作（未 commit / 未 merge / 未 push）；
- §3 全部数字为 2026-09-15 实测；执行前按 §2 复测。

---

# 勘误（2026-09-16 执行前复测发现；原文保留不动）

> 执行方在动工前重新测量了 §3 基座。**绝大多数数字成立，但有两处结论是错的**，
> 而且错法一样——都把「工作树字节差异」当成了「内容漂移」。
> 原文不改（保留当时的记录），但下一个人必须连着本节一起读。

## E-1（推翻 §3.2 第二行 与 §3.4）：三份根规则文件**没有漂移**，只是行尾不同

原文据「哈希不同」判定 main / backend / frontend 的三份 `AGENTS.md`、`CLAUDE.md`「已漂移」。
实测：**三个仓库的 git blob 完全相同**（`AGENTS.md` = `ad3de0f9…`，`CLAUDE.md` = `0eb8bbdd…`），
内容逐字节一致。原文引用的两个 sha256（`bfeb1ece…` / `f5859547…`）之差
**恰好等于每行一个 `\r`**。

根因：main / backend 的 `core.autocrlf=true`（检出 CRLF），frontend 是 `input`（检出 LF）；
`.gitattributes` 只钉了 `*.sh` 与 `*.ps1`，其余走 git 默认。**比工作树字节必然看到"全文件改写"。**

连带作废的推论：

- §3.4 的「在 backend 修 `AGENTS.md` 不会传播到 main / frontend，**三者已漂移**」——
  内容没有漂移；传播是 git 机制问题（各仓库各自 merge），不是内容分叉；
- §10 开放问题 2（main / frontend 副本是否同步、会不会继续分叉）——**前提不成立**：
  它们本来就是同一份内容，除非有人分别去改。

## E-2（修正 §3.2 第三行）：`web/` 不是「两份拷贝」

原文用「`web/src/index.css` 与前端仓库副本哈希不同」支撑「不能简单删 §15」。
实测：**`web/` 是一个 tracked 目录在三份 checkout 里的三个不同 commit**，
不是两份独立维护的拷贝。证据：

- 三个仓库 remote 相同、共享 `origin/main = e4da691c…`；
- `HEAD:web` tree hash：main 与 backend 同为 `0a49d0d2…`；frontend 为 `98b932bd…`，
  且恰好是前者的**超集**（多 4 个文件，全部来自 WS 批次）；
- `index.css` 的 blob 三处同为 `d20da640…`——那 737 行 diff 是 CRLF；
- `feat/backend` **从未提交过任何 `web/` 改动**（其分支独有 commit 只碰 `docs/` 与后端 Python）。

原文的**结论**（不删 §15）依然成立，但理由变了：不是"规则已分叉"，而是
用户 2026-09-16 明确「backend 线也可以做前端的事，frontend 线也可以做后端的事」。

## E-3（修正 §3.3 拓扑表）：frontend 不在 `feat/frontend`，现场还有第五个目录

- `D:\intelligence-agent-frontend` 实测在 `integrate/ws-stream`，不是 `feat/frontend`；
- 现场另有 **`D:\intelligence-agent-frontend-ws6`**（frontend 仓库的 linked worktree，
  分支 `feat/frontend-ws6-ws7`），不在原文的四行拓扑表里。

## E-4：本手册漏掉、执行时另找到的 11 处缺陷

R1-1~R1-6 未覆盖以下各处：

| # | 位置 | 缺陷 | 本轮处置 |
| --- | --- | --- | --- |
| 1 | `CLAUDE.md` §9.6 | 交叉引用「§6 Reuse First / §7 不变量」在本文件指向错误章节（逐字复制内容却没改引用） | 随 §9 薄化移除 |
| 2 | `CLAUDE.md` §15 | 同样把三个目录称作 worktree，且写 frontend 在 `feat/frontend` | 随薄化移除 |
| 3 | `AGENTS.md` §13.3 | 合并序列缺 §14.6 的「先把 main 合回 feature 分支」，照字面执行会合并过期分支 | 已重写 |
| 4 | `AGENTS.md` §13.1 / `CLAUDE.md` §15 | 声称 frontend 在 `feat/frontend`（实测 `integrate/ws-stream`） | 已改 |
| 5 | `AGENTS.md` §16.6 | 把 `docs/SDD_TICKET_TRACKER.md` 标成「frontend worktree 内」，backend 仓库里也有 | §16.6 已删 |
| 6 | `AGENTS.md` | 标题层级错乱（§13.2~13.4 是 `###` 挂在 `## 13.1` 下；§15、§16.6 级别与父级不匹配） | 已修 |
| 7 | `AGENTS.md` §3 表 | `MCP / Skills / RAG / Web` 与 `CLAUDE.md` 的 `Knowledge` 不一致 | 已统一 |
| 8 | `AGENTS.md` §16.2 | 调用 `/diagnose-bug`，实际 skill 名是 `diagnosing-bugs`（同段括号已自认） | §16 已重写 |
| 9 | 两份文件共 20 处裸 `docs/spec/` | 全指向流式 UI 规格族，且所引 5 个文件在该目录都不存在 | R1-1 覆盖 |
| 10 | `AGENTS.md` §10 | `/review` `/investigate` `/cso` `/qa` 不存在，与同节「不存在的命令不要伪造」自我打脸 | R1-5 覆盖 |
| 11 | `AGENTS.md` §14.3 | `git fetch origin --prune` 被列进「Read-only」代码块 | R1-4 覆盖 |

## E-5：R1-6 的措辞必须改

「三仓库各一份副本、内容可能已漂移」按 E-1 是错的。正确表述：三个仓库各有一份 **checkout**，
但它们是**同一个 tracked 文件树**，内容由 git 保证一致。把错误的恐惧写进根文件比不写更糟。

## E-6：另需单独决策的遗留项

- **跨 clone 行尾策略**：`.gitattributes` 只钉 `*.sh` / `*.ps1`，其余走默认，于是同一个 blob
  在 main / backend 检出 CRLF、在 frontend 检出 LF。这不是正确性 bug，但会让**任何跨 clone
  的字节比较产生假差异**（本手册自己就被它骗了两次）。是否统一行尾留待单独决策。
- **`docs/spec` 改名**：实测该字面出现在 **38 个 tracked 文件、107 处**（32 处指向流式 UI 族）。
  改名要动 38 个文件，而 R1-1 已把根文件里的裸路径全部消除、陷阱随之失效——
  故**本轮不改名**，改为在目录内放 `docs/spec/README.md` 说明；改名若仍要做需单独立票。

## E-7：本手册 §1 / §7 里"禁止 push、push 归集成 AI"的条款已被用户授权取代

用户的 2026-09-16 决定改变了本手册 §1 铁律 2 与 §7 安全红线背后隐含的两个默认：

1. 本手册 §1 铁律 2 写「**禁止 `git push`**，push 归集成 AI」——已被**常设授权**取代：
   现在「集成完成后的 `push origin main`」由**当前主开发**执行，不必每次重新批准
   （见 `AGENTS.md` §14.4 常设授权）。feature 分支上的 push、PR merge 等仍需单独批准。
2. §7 安全红线里「merge main、push main 归集成 AI」同理；「集成 AI」这个固定角色已废除，
   由"谁当前在干活谁就是主开发"取代（`AGENTS.md` 文件头）。

照抄本手册执行前，先以 `AGENTS.md` §13 / §14 的现行版本为准。
