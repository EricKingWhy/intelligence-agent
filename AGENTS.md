# AGENTS.md

本文件定义所有 Coding Agent 在本项目的默认行为。纯工程模式：做项目，不做教学。
Primary 由当前主开发角色决定，不绑定工具名；并行的非主开发 Agent 做独立审查、Debug、Security、验证或明确分配的任务。

**完整读取当前根规则后，先选择入口，不能混用顺序：**

- **正常 Task（含只读事实提取）**：确认任务/仓库 → 实读 Vision 相关原则、模块规格、Reuse 相关部分、Roadmap 的 Phase 依据、触发细则（路径/判据见 §2–§3）→ 如实列五项启动检查表 → 检查实现与 diff → 范围内施工 → 验证 → 按 §11 交付。
- **恢复（压缩 / 摘要 / 新窗口 / 状态不确定）**：下一项必须先按 §16 判据读完整 `docs/SDD_WORKFLOW_PROTOCOL.md`，再读 Tracker/Git/历史、核对 review 覆盖并确认 Task；禁止先状态后协议。纯恢复只走 §16；确定 Task 后、依赖其结论或施工前再走 §3。
标为“必须读取”的细则是执行前置条件：按触发条件读取指定文件/章节，核对完成条件后再执行相关动作。
**必读阻塞的结论**：缺文件或读取失败时，回复首行明确“任务阻塞（BLOCKED_REQUIRED_READ），未完成【依赖动作】”，并列出路径/错误。停止相关审查、方案或施工，不得以根文件、替代文档、模型记忆或所谓最低要求另定完成判据；不得给出依赖该细则的“已完成 / 通过 / 合规”结论。可继续的独立只读分析另行标注，不能将其包装成依赖任务已完成。任务工具（如 TodoWrite）、工作记录与最终回答必须一致：缺必读正文时，依赖项保持未完成；宿主支持 blocked 就用 blocked，否则用其实际支持的 pending / 未完成状态并注明阻塞原因，不臆造状态值。实际完成的独立阅读或事实分析可单列完成，不能把它当成正式审查或整个依赖任务完成。例如，已读模块规格但缺审查 playbook，只能完成模块阅读，审查与必读检查仍未完成。补齐原必读正文并实际满足完成判据后，才解除阻塞并将依赖项置为完成。
项目规则以当前 workspace 根文件为入口；不要依赖工具自动加载子目录 AGENTS、CLAUDE 或 import。

# 1. 最高需求来源

项目正式 Engineering Specification 位于**当前仓库内**：

```text
SPEC_ROOT = goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/
```

下文所有 `SPEC_ROOT/xxx.md` 均指该目录。三个仓库各有一份，绝对路径各自不同，
一律以**当前仓库根**为基准，不要抄另一个仓库的绝对路径。

**路径陷阱**：根 `docs/spec/` 是流式 UI/Workspace/Web UI 规格族，不是 Engineering Specification。
`SPEC_ROOT/...` 必须解析到上方路径，不简写为 `docs/spec/...`。

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

`PHASE_STATUS.md` 只留当前态和索引；批次/集成/审查明细进 `docs/phase_status/<年-月>.md`。
查历史先按索引或关键词定位，再用 Read offset/limit 或明确 UTF-8 的分段读取，只读相关段。
单条 bullet 上限 2000 字符，超出则索引留一行、正文进归档。不要整读历史大文件。
中文读取用 Read/Grep 或明确 `-Encoding UTF8` 的 PowerShell；避免 Git Bash 的 tail/sed/cat/head 造成 GBK 乱码。末尾按行数和 offset 定位。

# 3. 每个 Task 的阅读协议

后续不需要每次重读全部文档。

开始一个 Task 前：

1. 读取 `SPEC_ROOT/00_PROJECT_VISION.md` 中相关原则；
2. 读取当前任务对应模块规格；
3. 读取 `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md` 中相关部分；
4. 确认当前属于 `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md` 哪个 Phase；
5. 检查当前代码与测试；
6. 再开始 Review / Debug / Implementation。

**本 Task 的阅读完成判定**：用实际读取/检索核对以上相关原则、模块规格、Reuse 部分与 Phase 依据；首次已完成 §2 只免重复整读五文件，不能据“之前读过/根文件有摘要”跳过本 Task 的相关条款。尚缺必读依据时先补读，再给依赖它的审查、方案或施工结论。

**启动检查表（进入具体 Task 后必须执行）**：先实际读取，再在回复或已有工作记录中列以下五项；不新增专用文件，不把未执行的计划写成已读。尚未确认具体 Task 的纯恢复阶段只执行 §16，不把本表五项填成 N/A 或 READY；确认 Task 后，不能借“只读 / 非施工”免去前四项。

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | `SPEC_ROOT/00_PROJECT_VISION.md` 的相关章节 | READY / BLOCKED |
| 当前任务规格 | 本节映射的模块规格；前端同时按 §16.2 读 PRODUCT | READY / BLOCKED |
| Reuse 相关判定 | `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md` 的相关章节 | READY / BLOCKED |
| Phase 依据 | `SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md` 的相关章节 | READY / BLOCKED |
| 本任务触发细则 | 按任务触发的审查、Debug、调研、SDD、Git、验证等正文及其完成判据 | READY / BLOCKED |

第五项还须按以下时机检查关联指针：

- 操作 GitHub Issue 前必须读 `docs/agents/issue-tracker.md`；选用或变更标签前必须读 `docs/agents/triage-labels.md`。
- 依赖领域概念或架构决策进行方案、实现或审查前，必须读 `GLOSSARY.md` 与相关 `docs/adr/`，不要求整读无关 ADR。
- 开始代码实现或缺陷修复前，必须读 `docs/agents/implementation-discipline.md`，核对 §9.5–§9.6 的执行细则与不可简化红线。

将表中示例替换为本次实际读过的文件/章节；没有实际读取、返回错误/截断未补完或未达到该细则读取判据，均不得标 READY；未读项写 `BLOCKED（尚未读取）`，已发现必读文件缺失则同时明确依赖动作阻塞。文件“存在”、本次未依赖、旧摘要提及，都不是实读证据；禁止 `READY（弱）` 或用 N/A 代替具体 Task 的前四项。已读项列实际路径/章节，不编造工具调用或行数。触发细则只读本任务必须读的范围；检查表不扩大完整阅读要求，也不能豁免上述相关阅读。若本任务不触发额外细则，在第五项写明依据，不能据此免读前四项。

入口摘要不能替代必读正文；具体 Task 的依赖结论或施工前必须完成本节。纯恢复先执行文件头的恢复入口，不为填表而改变协议先行顺序。必读阻塞按文件头的结论模板处理。

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

# 4. 审查 / Debug / Security 职责

全体 Agent 均受 §4.3 的 Runtime 安全检查和 §4.4 的施工授权约束；§4.1–§4.2 按任务触发。

## 4.1 Independent Review

先实际读取 `docs/agents/review-debug-playbook.md` 的 Independent Review 分支。读取成功且 §3 前置完成后，才按正文检查项同时审查代码正确性与规格一致性，并依其完成判据报告。正文缺失或读取失败时，按文件头交付“审查阻塞、未完成”的报告；独立只读事实分析不能称作审查完成，本节摘要不能替代正文。

## 4.2 Difficult Bug Investigation

按 `复现 → Trace / JSONL / SessionEvent → 假设 → 验证 → Root Cause → 最小修复 → 回归` 闭环。
调查疑难 Bug 前必须读取上述 playbook 的 Difficult Bug Investigation 分支；涉及 Crash / Tool 副作用时检查完整恢复链。

## 4.3 Security Check

涉及权限、命令、路径、Sandbox、MCP、Artifact 或 SubAgent 的改动，至少检查：

- Prompt 不能替代 Runtime 权限；
- 命令执行、Path Traversal、Host / Sandbox 边界；
- Tool Permission / Approval、MCP remote side effect；
- 不安全默认值、大文件/Artifact 访问控制、动态 SubAgent 权限扩大。

## 4.4 施工授权

只有用户、当前主开发或 ticket 明确分配的 Task 才写代码；只读讨论不构成代码施工授权。
用户明确要求只读分析/不修改文件时，只做无副作用的读取与分析；不得为探权限、记录状态或验证环境而创建、修改、删除任何文件，包括临时文件和探针。需要写入才能继续时，报告缺口并停止依赖动作，不能自行把“测试用”当作写权限。
默认不承担整项目重新规划。

# 5. 工程规划边界

当前主开发（谁在干活谁就是，见文件头）维护 Matt SDD 主工程规划：

- 需求澄清 → Engineering Specification → GitHub Issue / Ticket 拆分 → 实施；当前施工与 review 节奏见 `docs/SDD_WORKFLOW_PROTOCOL.md`（V3.1-lite）
- GitHub Issue、Ticket 依赖与验收标准
- 主 Ticket 拆分与集成

Ticket 是 tracer bullet；一次只施工一个 Ticket，验证完成后再领下一张。批次组织按当前 SDD 协议，逐票验收不减少。
所有 Agent 不重新创建第二套 Engineering Specification。
GitHub Issues 使用 `EricKingWhy/intelligence-agent`；操作约定读 `docs/agents/issue-tracker.md`，标签读 `docs/agents/triage-labels.md`。
领域知识读 `GLOSSARY.md` 与相关 `docs/adr/`；重要架构决定变化 SHOULD 写 ADR，不只留在对话里。

非主开发的 Agent：

- 不创建第二套完整主 SDD 规格；
- 不重新解释整个产品方向；
- 不生成平行 Roadmap；
- 不因为自己偏好的框架修改项目宪法；
- 可以指出主 Spec 与 Engineering Specification 的冲突；
- 可以提出最小修订建议，但未经确认不得自行扩大范围。

---

# 6. Reuse First

**成熟产品调研与 Reuse First 是施工前置要求，不是可选建议。** 实现前必须检查 `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`，明确选择 `REUSE / ADAPT / PORT DESIGN / BUILD / DEFER`。
Agent Harness / Loop / 工具 / 会话优先查 Pi、DeepSeek Harness；其他领域按 `docs/agents/reference-sources.md` 找成熟产品。来源与核实要求按 §6.1，不能只凭模型记忆选型。
先找仓库已有实现、标准库、平台原生能力和已装依赖，再写最小新代码；完整阶梯见 §9.5。
复用遵守 §7 Core 边界；实质复制或 Port 上游代码时检查 License 并保留必要来源。

## 6.1 方案先行调研

设计/选型（新方案、新集成、架构改动）在写实现计划前必须：

1. 读 `docs/agents/reference-sources.md` 对应领域；
2. 按 `docs/SDD_WORKFLOW_PROTOCOL.md` §1.3 核实至少 2 个独立来源并记录机制、契合点和复用判定；
3. 代码来源用本地浅克隆的 `file:line` + commit，第三方笔记先核实；
4. 在方案/Ticket 落“方案依据”块，字段和豁免按协议 §1.3。

以上调研和“方案依据”块完成后，才可进入设计/选型类任务的实现计划与施工；纯 Bug/纯文档豁免按协议 §1.3 判断，不能把新方案伪装成 Bug 修复。
调研必读正文缺失/读取失败、独立来源或方案依据尚未完成时，按 §3 明确报阻塞；不得输出施工步骤或实施计划，包括冠以“草案”的计划。仍可独立只读核实已有代码/规格事实并报告缺口，不能借此绕过调研前置。
不懂先查。无依据的逐方案试错属于 §4.2 Debug，不属于设计阶段。

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

## 7.1 执行细则

所有 Tool（Coding / Knowledge / Web / MCP / Memory / SubAgent / future）统一走 `Contract → Registry → Validation → Permission → Scheduler → ToolExecutor → Ledger → ToolResult → SessionEvent`，不另开隐藏路径。
外部副作用靠 Operation Ledger + Reconcile；UNKNOWN 高风险 Tool 走 `NEED_RECONCILE → 用户处理`。Milvus / MinIO / Langfuse / MCP / Memory Provider 经 Provider / Adapter，故障不得影响基础 Agent。
修改 Tool Runtime / Scheduler 时必须逐项核实：并行须同时满足无 depends_on、无数据依赖、无资源冲突、Permission 与 Tool Contract 允许；依赖来自 depends_on / resource_keys / metadata 等显式信息，V1 不用 LLM 自由文本猜 DAG。
测试随功能交付，按模块选 Unit / Integration / Failure / Recovery / E2E；Recovery 必须有 Kill / Crash Test，Provider 替换不得改 Core，Tool 改动必须验证 Validation / Permission / Retry / Result pairing。§9.4 的闭环摘要不替代这些检查。

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

**不维护静态工具清单**：可调用的 Skill / 命令以当前 Agent 环境**实际枚举**为准；协议要求按路径读取的仓库 Skill 正文按 §16.2 执行，不受工具注册与否影响。

使用 Matt skills（含 setup、ask-matt、implement-spec、pr、retro、handoff）前，必须读取 `docs/SDD_WORKFLOW_PROTOCOL.md` §9 的兼容约束；领域词汇表正文只维护在 `GLOSSARY.md`，旧 `CONTEXT.md` 是必读跳转入口。

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

交付报告以实际工具记录为准：请求被拒绝也要说明尝试及错误，不得因文件没变就称“全程只读/没有尝试写入”。
宣称 Ticket 完成前核实：票面与模块 AC 完成；相关测试/Failure Case 通过；Recovery 有真实 Kill/Resume；JSONL/SessionEvent 可观察实际运行；无 Scope 外改动；diff review 和实际 diff 可解释。门禁按 §14.10；仅创建文件或能运行不足以完成。

---

# 12. 最终原则

> **Engineering Specification 决定“要做什么与不能做什么”；当前 ticket 决定“当前怎么施工”；主开发负责实现与集成，独立审查者的价值是找根因、守住边界、防止自证清白。**

---

# 13. 仓库模型与并行开发

## 13.1 三个独立 clone

| 路径 | 角色 | 稳态分支 |
| --- | --- | --- |
| `D:\intelligence-agent` | 最终集成 | main |
| `D:\intelligence-agent-backend` | 后端施工 | main |
| `D:\intelligence-agent-frontend` | 前端施工 | main |

三者独立 `.git`，对象/分支不共享；跨仓库用 `git -C <repo>`，缺对象先 fetch。
各 clone 内部 linked worktree 用 `worktree list --porcelain` 核实；已有 worktree 默认只读，写入需用户授权。
仓库角色不是目录所有权；同文件只由一条线修改，并行会话使用不同 checkout。
公共文件通过 Git 同步，本地 env/venv/cache/logs/IDE/runtime 临时内容不要求同步；同 commit 比 Git 对象，不比工作树行尾。

## 13.2 施工分支

允许短分支施工，也允许单线直接在施工 clone 的 main 提交；同 clone 第二个写会话或命名分支 review 时必须开短分支。
短分支从 main 出发，集成前先回合最新 main；直接在施工 main 提交无需合回自身。工作分支集成后结束使命。

## 13.3 本地提交

检查 status/diff 后可 add/commit 当前任务的明确文件；小步提交，commit message 描述工程事实，Git 收尾不代替测试。冲突文件先按 §14.7 获批。交付按 §11。

## 13.4 集成入口

同步、merge、push、PR merge 前必须读 `docs/agents/git-workflow.md` §1–§3；授权按 §14.4。
本地 main 验证 → 经批准推分支/开 PR → CI gate0 绿 → 单独获批 PR merge → 回补通知。完整步骤读手册。

## 13.5 Diff

相对共同基线核对 diff；跨 clone 与已验证树对账按 Git 手册 §2.3/§3.2。

# 14. Git Workflow / Merge Safety

## 14.1 集成角色

main 保持稳定；当前主开发负责集成，同一时刻只集成一条线。

## 14.2 操作前检查

每次 Git 写操作前确认仓库、branch、HEAD、status；merge/rebase/reset 前工作树 clean。查看分支用只读命令，不随意 checkout/switch。
同步、合并、发布、关单前必须读 Git 手册对应步骤；读取失败停止依赖动作。

## 14.3 无需批准的操作

只读分析、Test/Lint/Type Check/Diff Review 和 git status/branch/worktree list/log/show/diff/merge-base/rev-list 可直接执行。
fetch origin --prune、跨 clone fetch 到明确 remote-tracking/audit ref 可直接执行；fetch 会改对象/refs，不把任意本地分支目标称作低风险。

## 14.4 Git 授权表（常驻权威）

| 分类 | 动作/条件 |
| --- | --- |
| 可自行执行 | 当前任务 add/commit；冲突文件另走 §14.7 |
| 常设授权 | main 合回自己的短分支；验证过的短分支合进本地 main。前提：§14.10 全绿、一次一条线、集成后通知回补 |
| 每次单独批准 | feature/集成分支 push；GitHub PR merge。两个动作分别批准，合并前 CI gate0 绿 |
| 每次单独批准 | cherry-pick、revert、Branch 删除、Worktree 删除、冲突文件修改与解决后的 add |
| 默认禁止，具体操作获批才执行 | reset --hard、rebase、push --force、push --force-with-lease、branch -D |

发布按受保护 main 处理：推分支 → PR → gate0 绿 → PR merge；旧直推 main 授权不适用。
不以 --admin 绕过门禁；修改/关闭保护先说明理由并取得具体授权。保护状态现场核实，不自动扩大授权。

## 14.5 同步

fetch 后显式分析再 merge，不默认 git pull。最新基准按 Git 手册 §2 获取。

## 14.6 方向

短分支“先回后正”：最新 main → 短分支处理冲突/验证/审查 → 本地 main → PR；直接施工 main 的路径读手册 §3。

## 14.7 Conflict Policy

冲突后立即停止自动解决；获批前不修改冲突文件、不 add。先逐文件报告双方修改及目的、冲突原因、能否共存、推荐语义、Contract/Runtime/Test 影响、风险等级。
批准后做最小修复、验证和暂存；不机械 ours/theirs，不为消冲突删一侧逻辑。九项报告步骤读手册 §4。

## 14.8 Main Safety

main 上未预见的复杂冲突优先 merge --abort，回施工线按 §14.7 处理。

## 14.9 集成顺序与回补

A 集成后重新获取基准并分析 B；旧判断可能过期。集成者通知另一条线/用户；另一条线开工前取得最新集成 ref，再检查它是否为 HEAD 祖先。
未更新的本地 main 自检不算同步证据；落后先同步，冲突按 §14.7。命令读手册 §2。

## 14.10 Validation Gate

进入 main 前：工作树 clean；冲突已处理；diff 可解释；无误删、覆盖其他 Agent 或 Scope 外改动。
必须读取 `docs/agents/verification.md` §1–§2 与 SDD 协议 §7/§8.1，完成全量测试、lint、type check 和必要运行验证；git diff --check 与 Python 审查覆盖闸门 exit 0。
Gate-0 读数引用 docs/gate/<sha>.json；重车道保留可复跑命令和 SHA/tree。对账已验证树/集成树；树不同重跑完整门禁，树相同按协议核实证据传递。
代码提交必须真实审查；coverage 的 .py 是可运行权威，.sh 仅冻结参考。Gate-0 不替代完整门禁/review/真实入口验证；PR merge 还须 CI gate0 绿。本地绿不代替 CI 绿。

## 14.11 批准流程

需批准的动作：Analyze → Report → Ask → Execute → Validate → Report；后续需批准动作再 Ask。前一阶段批准不自动扩展，常设授权按 §14.4。

## 14.12 关单

完成 Ticket 必须立即关闭 issue，先核实实际代码/AC/测试；不凭记忆关单。未集成但完整交付可关单，comment 写分支、commit、集成负责人；部分交付不关单，写已完成/剩余。执行步骤读手册 §5。

## 14.13 跨线检查

修改前检查对侧未提交、暂存和相对最新集成基线的提交；main..HEAD 空不能证明没有在途工作。同文件冲突先协调，跨 clone 比 Git 对象。命令读手册 §2。

# 15. 前端 CSS 主题变量维护纪律（Ticket #35）

修改或新增主题 token 时，必须同步检查暗色 `:root` 与亮色
`:root[data-theme='light']` 两个定义块。完整实现规则与视觉权威见 `web/PRODUCT.md`；
`web/` 是三个 clone 共享的 tracked 文件树，因此无论在哪个 clone 修改都适用。


---

# 16. SDD 长任务工作流协议（入口）

**V3.1-lite 是必须遵循的开发流程；夜间全自动、连续数小时或多 Ticket 任务同样适用，自动运行不扩大授权、不降低验证与审查要求。**
顺序处理多 Ticket、执行 SDD、疑难 Bug 或跨 context 延续任务时，开始依赖流程的恢复分析或施工前，必须完整读取当前 `docs/SDD_WORKFLOW_PROTOCOL.md`；读取 `docs/SDD_TICKET_TRACKER.md` 的当前态与相关 Ticket。
**协议读取完成判定**：实际读取须连续覆盖首行至文件末尾。使用 Read offset/limit 分段时，接着上一段读到 EOF；返回截断就继续补读。只读前 200 行、搜索命中段、旧摘要或本节检查点都不算读完。未满足判定时继续读取，不得给出“流程已恢复/可以按协议继续”的结论；独立只读分析须注明仍缺哪些流程正文。
协议是唯一流程权威；Tracker 记录事实。压缩、摘要或状态不确定时，重读二者并核对 Git，再继续。
Tracker 先定位当前态/相关 Ticket；历史按需读。版本以协议头为准，V1/V2 批次/fixed point/旧 Skill 只作历史。

## 16.1 进度落点

- 在途 Ticket/门禁/review/残余：`docs/SDD_TICKET_TRACKER.md`；
- Phase 当前态/索引：`docs/PHASE_STATUS.md`；明细：`docs/phase_status/<年-月>.md`；
- 机读审查范围：`docs/review_ledger.tsv` + `docs/review_ledger.d/*.tsv`（双读）；
- 一次性交接/集成：`docs/archive/handoffs/`、`docs/archive/integration-prompts/`。

同一事实只写全一次，其余留指针；机制正文写 ADR/设计稿，注释写代码看不出的操作约束，进度写操作事实。维护记录前读协议 §5/§8.5。规格冻结，进度不回写规格。

## 16.2 执行入口

门禁按 §14.10；Git 授权按 §14.4；协作按 §11/§14.13；关单按 §14.12。工具链/review 时点按当前协议。
**Matt / pstack 阶段入口**：走到 V3.1-lite §9 列出的阶段时，必须按该表实际读取并执行对应 Skill 正文；pstack 文件从仓库 `docs/agents/skills/` 读取，无需安装或注册。取用前必须读 `docs/agents/skills/PROVENANCE.md` §4.2 的宿主差异，按协议处理。Matt 主导 Grill / Spec / Tickets / Implement / TDD / Review，pstack 补辅助；七阶段与失败回退只以协议 §8.8 为准，完整 Skill 路由只维护在 §9。
写中文/长文本前必须读协议 §8.9，执行脚本落盘、原子替换、字节核对；前端改动前必须读 `web/PRODUCT.md`。

## 16.3 连续运行的执行检查点

以下是协议入口检查点，执行细则与判据仍以完整协议为准：

1. **接票**：按协议 §1.1/§6 核实当前票面、AC、相关规格、Reuse Matrix、代码与 Git；设计/选型先完成 §6.1 调研。可验证目标和 Scope 确认后才施工。
2. **逐票施工与验证**：按协议 §1.2/§9 做最小垂直切片；修 Bug 或新增行为时先建立真实症状测试；每票做相关 focused tests/lint/type check/必要集成验证；通过后本地提交并按 §5 更新证据，再领下一票。逐票验证不因批次或夜间运行而省略。
3. **审查**：按协议 §2/§8.2–§8.3 安排风险审查，记录审查实际读过的 base..tip；发现问题按证据修复，修复提交也必须覆盖。代码不能用 docs-only 例外放行。审查不可用/超时按协议既定分支处理，不能自行当作通过。
4. **自然收口与集成**：按协议 §3/§7/§8.1 在冻结树跑完整门禁，按其判据传递机器证据并跑 coverage；运行相关真实入口验证。Gate-0 不替代完整门禁。Git 动作逐项核对 §14.4，未获批准的 push/PR merge 停在待批准状态。
5. **异常**：必读文件缺失/读取失败时停止依赖动作并报告路径/错误，不能用入口摘要替代；不依赖它的只读分析可继续。票面/规格实质冲突或新证据推翻计划时，停止相关施工，报告证据/影响/最小替代方案并按 §9.1.1 请求决定；不擅改票面、AC 或制造完成记录。
6. **压缩/恢复/交接**：一旦上下文压缩、摘要、新窗口或不确定状态，第一动作先按本节的协议读取完成判定读完整协议，再按协议自愈条款与 §4 读取 Tracker、核对仓库/branch/HEAD/status 和 review 覆盖；确认上述前置完成后，才按记录与已有授权继续。结束按 §11 交付，标明剩余事项和待批准动作；不能仅凭摘要或旧对话认定已验证/已审查。
