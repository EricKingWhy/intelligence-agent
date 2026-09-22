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
**不是** Engineering Specification。那里的 `README.md` 只是本陷阱的说明文件，
`00_PROJECT_VISION.md` / `01_SYSTEM_ARCHITECTURE.md` / `13_` / `14_` 一个都不存在。
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

> **该文件的结构（2026-09-17 起）**：`PHASE_STATUS.md` 只留**当前态**（Phase 进度表 + 当前焦点）与**索引**；
> 逐条批次 / 集成 / 审查记录在 `docs/phase_status/<年-月>.md`（当月归档）。查历史时按索引里的
> 「按日定位」用 `Read` + `offset/limit` **只读那一段**，或先 `grep -n "关键词" docs/phase_status/*.md` 定位。
> **不要整文件读**（拆分前它是 565 KB，一次吃掉整个上下文）；**单条 bullet 上限 2000 字符**，超出就只留索引一行 + 正文进归档。
>
> ⚠ **读这些中文大文件的工具纪律**：用 `Read`（含 `offset`/`limit`）或 `Grep`；**不要用 `tail` / `sed` / `cat` / `head`**
> 读——Windows Git Bash 的 GBK 控制台会把 UTF-8 显示成乱码（实测踩过两次，其中一次差点拿乱码当编辑锚点）。
> 需要看大文件末尾时用 `Read` 的 `offset`，或用 `grep -c ""` 先取行数。

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

Review 必须同时检查**代码正确性 + 当前规格一致性**，不能只确认“能跑”。以独立审查者身份
工作时，完整检查项与完成判据见 `docs/agents/review-debug-playbook.md` 的 Independent Review 分支。

## 4.2 Difficult Bug Investigation

按 `复现 → Trace / JSONL / SessionEvent → 假设 → 验证 → Root Cause → 最小修复 → 回归`
闭环，优先使用项目自己的可观察链路。涉及 Crash / Tool 副作用时，必须检查完整恢复链；
检查对象与完成判据见 `docs/agents/review-debug-playbook.md` 的 Difficult Bug Investigation 分支。

## 4.3 Security Check

**第 0 条（用户 2026-09-16 定下的红线，本节最高优先）——凭证零泄漏**：
`.env` 的值绝不打印、不提交、不复制进任何文档或命令输出；可以列 key **名**，不可列 key **值**。
（原先只写在 `CLAUDE.md` §3，2026-09-17 搬到此处：本文件是所有 Agent 的默认行为来源，只读
`CLAUDE.md` 的 Claude 之外的 Agent 拿不到它；`CLAUDE.md` 相应改为指针，避免两处漂移。）

至少关注：

- Secret 泄露（含**测试/探针**：实测教训——用例拿真 `SystemCredentialStore` + 带 key 的 `create`
  会往系统凭据管理器写进一条假 key，且 monkeypatch 让删除必失败时**删不掉**，残留只能手工清；
  探针供应商写进全局 `model-providers.json` 会让 3 条内置目录断言变红）；
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

- 需求澄清 → Engineering Specification → GitHub Issue / Ticket 拆分 → 实施；当前施工与 review 节奏见 `docs/SDD_WORKFLOW_PROTOCOL.md`（V3）
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

### 9.1.1 新证据导致的票面变更控制

施工过程中如果出现新线索，足以推翻原有假设、证明票面不可实现、持续暴露未解决 Bug，
或证明当前证据与既定计划冲突，Agent 不得为了“守住旧票面”继续硬顶，也不得伪造完成。
必须先停在分析/报告阶段，明确列出：

- 新证据及其可复现方式；
- 被推翻的假设或无法满足的验收条件；
- 对架构、范围、测试、依赖与风险的影响；
- 可行的最小替代方案及其取舍。

这类情况需要向用户请求决策。只有用户明确批准后，才可以修改 ticket / issue 票面、
重制定实施计划、调整验收标准或扩大/缩小范围，并在 tracker 与相关文档中记录该决策。
用户的批准只覆盖明确批准的变更，不自动授权后续 merge、push、关单或其他高风险动作；
变更后的票面必须重新走对应的实现、测试、审查与 coverage 闸门。

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
阶梯缩短的是解法，不是理解与验证；展开说明见 `docs/agents/implementation-discipline.md`。

## 9.6 工程八荣八耻

查询接口、澄清业务、复用现有、主动验证、遵循规格、诚实说明未知、谨慎修改。
完整口诀及其与 §6 / §7 / §9.4 的对应关系见 `docs/agents/implementation-discipline.md`。

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
| 流程 / 架构疑问 | 先读当前 Specification / ADR；存在实质决策时询问用户 |
| 代码库理解 | `understand` / `understand-chat` / `understand-diff` / `understand-domain` / `understand-explain` |
| 实现 / TDD | `tdd`（在适用时）；其他实现按当前环境可用能力执行 |

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
  tracked 文件树**——**同一个 commit 上，三份逐字节相同**；不存在"三份各自维护的副本"。
  （注意：不同 commit 上内容当然不同，落后未 fetch 的仓库看到的就是旧内容——那正是 §14.9
  「集成后回补」要解决的问题，不是"副本分叉"。）
- 真正的 linked worktree 只存在于**单个仓库内部**（`git worktree list --porcelain` 可查）；
  本仓库内可能另有 worktree，对它们只做只读检查，写操作需用户授权。

> **测量陷阱**：三个 clone 的行尾配置不同；跨 clone 比较必须比 Git 对象，不能据工作树字节
> 判断漂移。命令与原因见 §14.13(b)。

### 开发规则

1. 三个仓库都可以施工；用哪一个是**分工选择**，不是硬边界（用户可以授权任一条线做另一端的活）。
2. 同一时刻，同一个文件只由一条线修改；并行会话不要共用同一个仓库目录。
3. 跨仓库操作用 `git -C <repo-path> <command>`，不用 `cd` 切换。
4. 公共项目资产通过 Git commit 进入版本控制，其他仓库 fetch/merge 后即可见：
   `docs`、`goal`、Spec、`AGENTS.md`、`CLAUDE.md`、`CONTEXT.md`、已确认源码、`tests`、正式配置。
5. 以下本地内容**不要求**跨仓库同步：`.env`、`.venv`、cache、`logs`、IDE 临时文件、
   runtime 临时文件、secrets。

## 13.2 核心模型：main 是稳态，干活开短分支（或直接在施工 clone 的 main 上提交）

```text
平时：三个 clone 都停在 main —— "三方一致"是默认状态，一条命令可验
干活（两种都被授权，按并行度选）：
 (a) 短分支：施工 clone 开短分支 → 施工 → 门禁 → 合回 main → 集成 → push
 (b) 直接在**施工 clone** 的 main 上提交（单线作业时的常态）
```

不变式（仅在走 (a) 时成立，可一行验证）：

```bash
git merge-base --is-ancestor main <feature-branch>   # main 永远是 feature 分支的祖先
```

**两种走法的实际差别（2026-09-17 实测）**：走 (b) 时集成是 `git merge --ff-only`、不产生
merge commit，§13.4 与 §14.6 的「先回后正」那一步自动消失；走 (a) 时它才真正发生。
**什么时候必须走 (a)**：同一个 clone 上有第二个会话 / agent 在写（并行分歧会污染"三方一致"），
或者需要在合入前让别人能指名称地 review 一条分支。
⚠ 走 (b) 时**上面那条不变式无从验证**（没有 feature branch）——此时"三方一致"退化为
「三个 clone 的 `main` 指向同一个 commit」+ §14.9 的集成后回补自检。

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

哪些动作需要用户批准、哪些是常设授权，一律按 §14.4 的分类执行。
完成后的交付报告按 §11，不在本节重复授权表与报告清单。

## 13.4 最终合并规则

最终集成统一在 `D:\intelligence-agent` 的 `main` 进行：

```text
origin/main
→ 先把 main 合回你的短分支（§14.6「先回后正」）：冲突与测试都在短分支上解决，
  不把过期分支直接合进 main
（若走 §13.2(b) 即直接在施工 clone 的 main 上提交，则没有这一步，直接对账）
→ diff 检查 + 门禁全绿（§14.10）
→ **审查覆盖闸门**：`scripts/check_review_coverage.py`（本机默认环境即可跑）
   （`scripts/check_review_coverage.sh` 是**同口径的语义参考实现**、保持冻结——它依赖 coreutils，
    本机默认 PATH 下跑不动，故本机默认用 `.py`；两者的对照结论见 `docs/agents/SDD_ACCELERATION_AUDIT.md` §9.4。
    范围 `<最早台账 base>..HEAD` 的每条 commit 必须有台账归属：审查行、docs-only 白名单，
    或"恰好只改台账文件"的记账提交——**代码提交只有"补一次审查"一条路**；
    台账 `docs/review_ledger.tsv`，机制与**信任边界**见 `docs/SDD_WORKFLOW_PROTOCOL.md` §7 第 8 条）
→ merge 到本地 main（快进优先）
→ **先比 `HEAD^{tree}`，不等才跑全量门禁**：`git -C <集成 clone> rev-parse main^{tree}`
   与施工 clone 的 `HEAD^{tree}` 比——**相等即证明"我跑过门禁的那棵树"就是"被集成的这棵树"**，
   不必再跑一遍（2026-09-17 实测：两 clone tree 同为 `e63c202…`，省掉一次 ~20 分钟的前后端全量）。
   不等（例如集成 clone 的检出行尾/CRLF 造成差异、或合入时产生新内容）⇒ 在集成 clone 里跑全量门禁
→ 确认前后端集成正常
→ git push origin main（当前主开发执行，常设授权见 §14.4）
→ 通知另一条线把 main 合回来（§14.9）
```

**先合并到本地 `main` 并验证，再 push GitHub。** GitHub 不是仓库之间交换代码的必经步骤。
除非用户明确要求，否则不要默认「先 push feature 分支再通过 GitHub PR merge」。

## 13.5 `git diff` 的用途

合并前可以检查实际改动：

```bash
git diff main...<你当前的短分支>
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
feature branch   ← 在这里解决 Conflict、Test、Review（push 不在这里，见下）
    ↓
main             ← feature branch 稳定后再合入 main
    ↓
push origin main ← 集成动作，由当前主开发执行（§14.4 常设授权）
```

**Push 不在 feature 分支上做**：feature 分支上的 `git push` 属"向外发布未集成的工作"，
需要用户单独批准（§14.4）。集成线只在合入 `main` 之后推。

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

然后回**该分支所属的仓库**解决。不在 `main` 上临时拼接复杂业务逻辑。

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
- **审查覆盖闸门通过**：`scripts/check_review_coverage.py` 退出 0（§13.4 那一步；漏了它
  = 允许未审查的 commit 进 main，2026-09-17 立的机械门）。
  `.sh` 是同口径的**语义参考实现**、保持冻结（依赖 coreutils，本机默认 PATH 下跑不动）；
  在能跑 `.sh` 的环境里，两者判据必须一致（对照结论见 `docs/agents/SDD_ACCELERATION_AUDIT.md` §9.4）；
- **门禁读数来自机器落盘**：`docs/gate/<sha>.json`（`scripts/gate0.py` 每次**裸全量**运行写出：
  `sha` + `^{tree}` + 每车道结论 + 墙钟 + 工具版本；`--replay <file>` 可独立复核判定）。
  集成前的读数一律**引用该文件**，不手抄终端数字 —— **"人抄读数"这条路径已删除**
  （#213 的成因就是手抄 fixed point 错一格而静默豁免一票，没有任何东西会报错）。
  **落盘前工作树必须对 `HEAD` 干净**：脏树 ⇒ 拒绝落盘并 FAIL（车道跑在工作树上、读数只能记
  `HEAD` 的树，两者不一致就是"指到一棵没被测过的树"）。机制与边界见协议 §7 第 8 条；
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

修改或新增主题 token 时，必须同步检查暗色 `:root` 与亮色
`:root[data-theme='light']` 两个定义块。完整实现规则与视觉权威见 `web/PRODUCT.md`；
`web/` 是三个 clone 共享的 tracked 文件树，因此无论在哪个 clone 修改都适用。


---

# 16. SDD 长任务工作流协议（入口）

> **触发条件**：用户要求按顺序处理多个 Ticket、执行 SDD、追查疑难 Bug，或跨 context 延续工程任务。

触发后读取：

1. `docs/SDD_WORKFLOW_PROTOCOL.md` —— 当前唯一流程权威（**V3.1-lite：按风险安排 review；提速增补见协议 §8「冻结树单次全量 / 三路并行 / 审查预算 / 批次合并 / 记账压缩 / 既有红与 flake」，**质量门一条未减**）；
2. `docs/SDD_TICKET_TRACKER.md` —— 当前 Ticket、验证、review 覆盖与残余问题（记录事实，不定义流程）。

本节只作入口。流程冲突以 `docs/SDD_WORKFLOW_PROTOCOL.md` 为准，进度事实以 Tracker 为准。Tracker 中 V1/V2 的批次、fixed point 与旧 Skill 指令是历史记录，不是当前要求。集成与 push 由当前主开发按 §14 执行。

**自愈条款**：上下文被压缩、摘要或不确定当前状态时，先重读上述两份文件并核对 Git 状态，再继续施工。

## 16.1 进度落点分工

| 内容 | 落点 |
| --- | --- |
| 在途 ticket、门禁证据、review 覆盖状态与残余问题 | `docs/SDD_TICKET_TRACKER.md` |
| Phase 状态、当前焦点、历史索引 | `docs/PHASE_STATUS.md`（**只放索引一行 + 行号指针**） |
| 批次 / 集成 / 审查的**逐条明细** | `docs/phase_status/<年-月>.md`（当月归档，按需读） |
| 机读的审查范围台账（覆盖闸门的输入） | `docs/review_ledger.tsv` |
| 一次性集成执行资料 | `docs/archive/integration-prompts/`、`docs/archive/handoffs/`；旧路径映射见各目录 README |

**同一事实只在一处写全，其余处只留指针**（2026-09-17 立的规矩，起因：一条机制描述同时住在
ADR、用例头注释、设计稿、tracker 四处，其中一处被后来的实测**推翻**，改一处要改四处才自洽）。
落点约定：**机制 / 决议的完整叙述 → ADR**（或设计稿）；**代码注释只写"这段代码自己看不出来的
操作约束 + 指向 ADR 的一句指针"**；tracker / PHASE_STATUS 只写操作性事实（批次、commit、
门禁数字、结论一行）+ 指针。跨文件重复叙述属于要被清理的债务，不是"写详细一点"。

V1/V2 的批次 / fixed point 留作历史事实；V3 不要求固定批次。规格文件（`SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md` 等）保持冻结，进度变更不回写规格。

## 16.2 不随协议版本变化的红线

门禁与 review coverage 按当前协议及 §14.10，Git 授权按 §14.4，协作避让按 §11，关单按
§14.12；本入口不复制这些规则。前后端的工具链、review 时点与在途进度落点统一以当前
`docs/SDD_WORKFLOW_PROTOCOL.md` V3.1-lite 为准。

---
