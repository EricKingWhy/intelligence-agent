# CLAUDE.md

> **本项目的全局规则在 `AGENTS.md` —— 进入本仓库前先完整读它。**
> 本文件只保留两部分：(1) Claude 使用上的差异；(2) 不知道就会出事的红线。
> 重复内容一律不复制：两份文件历史上已经在 skill 清单等处给出过互相矛盾的答案，
> 而重复是漂移的唯一来源。
>
> **角色说明**：Primary **不绑定工具名**——谁当前在干活谁就是主开发（见 `AGENTS.md` 文件头）。
> 本文件不宣称 Claude 是主开发。

---

# 1. 规格在哪

```text
SPEC_ROOT = goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/
```

**路径陷阱**：仓库根下另有一个 `docs/spec/`，那是流式 UI 规格族，**不是** Engineering
Specification。那里的 `README.md` 只是本陷阱的说明文件——`00_PROJECT_VISION.md`
`01_SYSTEM_ARCHITECTURE.md` / `13_` / `14_` 一个都不在它里面。
详见 `AGENTS.md` §1 与 `docs/spec/README.md`。

首次进入的阅读协议、每个 task 的阅读协议、模块 → 规格映射表、需求冲突优先级：
见 `AGENTS.md` §1.1 / §2 / §3。

---

# 2. 开发方式：Engineering Spec + Matt SDD

Engineering Specification 负责：

`项目长期产品需求 / 架构边界 / MUST / MUST NOT / Acceptance Criteria`

Matt SDD 负责：

`当前模块如何澄清、规格化、拆分和施工`

推荐流程：

```text
需求仍有重大歧义或需要补足领域术语
→ /grill-with-docs

需要把当前对话整理为功能规格
→ /to-spec

需要拆分为可独立交付、带依赖关系的 Ticket
→ /to-tickets

需要规划跨多个会话的大型工作
→ /wayfinder

用户授权当前 Ticket
→ /implement（在预先约定的 seam 使用 /tdd）

完成
→ Test
→ /code-review（批大小与 fixed point 见 docs/SDD_WORKFLOW_PROTOCOL.md）
→ Acceptance Criteria
→ Git（授权分类见 AGENTS.md §14.4）
```

原则：

- 一个 ticket 是一个 tracer bullet；
- 一次只施工一个 ticket；
- 一个 Ticket 完成后先验证，再领下一个；
- 不重新创建第二套 Engineering Specification；
- Matt SDD 产物不得覆盖 `SPEC_ROOT/` 中冻结的架构原则。

---

# 3. 红线（必须常驻，不靠"用到再读"）

> 这几条要么不可逆，要么会污染整条事实链。**完整的 22 条架构不变量在 `AGENTS.md` §7——
> 改动 Core / Runtime / Session / Tool / Recovery 相关代码前必须去读。**

1. **凭证零泄漏**：`.env` 的值绝不打印、不提交、不复制进任何文档或命令输出。
   可以列 key 名，不可列 key 值。
2. **Tool 只有一条统一执行路径**：任何 Tool（Coding / Knowledge / Web / MCP / Memory /
   SubAgent / future）都走
   `Contract → Registry → Validation → Permission → Scheduler → ToolExecutor → Ledger → ToolResult → SessionEvent`。
   禁止第二套隐藏执行路径；Tool Retry 只有 ToolExecutor 一个责任域。
   **Prompt 不能替代 Runtime 权限**，Sandbox / Permission 是 Runtime 边界。
3. **Event ≠ Diagnostic Log**：Session 是 append-only typed `SessionEvent`；
   `Persistent History ≠ Runtime Context`，`完整保存 ≠ 完整注入`。
   UI 与 Context 都从同一条事实链投影——**Web UI 不维护第二套不可对账的 Session 真相**。
4. **Checkpoint ≠ 副作用恢复**：外部副作用只能靠 `Operation Ledger + Reconcile`；
   `UNKNOWN` 的高风险 Tool **不盲重跑**，走 `NEED_RECONCILE → 用户处理`。
5. **Optional 能力故障不得拖垮 Core**：Milvus / MinIO / Langfuse / MCP / Memory Provider
   一律经过 Provider / Adapter；任何一项挂掉都不能影响基础 Agent 运行。
6. **并发按依赖与资源冲突判定**，不用 READ_ONLY / MUTATING 二分：并行需**同时**满足
   无显式 `depends_on`、无数据依赖、无资源冲突、Permission 允许、Tool Contract 允许。
   依赖来源优先 `depends_on` / `resource_keys` / Tool metadata / 同文件冲突。
   V1 不使用 LLM 自由文本猜 DAG。
7. **危险 Git 默认禁止**：`reset --hard` / `rebase` / `push --force` / `branch -D`。
   `merge` / `push` 的授权分类见 `AGENTS.md` §14.4（集成与 `push origin main` 是常设授权，
   feature 分支上的 push 仍需单独批准）。

---

# 4. 工程纪律

**Scope Lock**：每个 diff 必须能回答「为什么属于当前 Ticket？」。禁止顺手重构、无关清理、
提前实现未来 Phase、投机性抽象、未要求的 Redis/Kafka/K8s 基础设施。
需要实质扩 Scope 时：`STOP → 原因 → 新范围 → 架构影响 → 用户确认`。详见 `AGENTS.md` §8。

**施工许可**：只读分析可直接做；正式实现以当前用户授权 / ticket 为边界；
高风险、不可逆、缺外部账号或 API Key 时再请求用户。

**疑难 Bug**：`复现 → SessionEvent / Trace / JSONL → 假设 → 验证 → Root Cause → 最小修复 → 回归`。
Crash / Recovery 类问题必须检查 Operation Ledger，而不是只看异常栈。

**Tests**：测试跟随功能一起交付，按模块选择 Unit / Integration / Failure / Recovery / E2E。

- 涉及 Recovery 必须有 Kill / Crash Test；
- 涉及 Provider abstraction 必须验证替换 Provider 不改 Core；
- 涉及 Tool 必须验证 Validation / Permission / Retry / Result pairing。

**Git**：小步提交；不覆盖其他 Agent 未提交改动；commit message 描述工程事实；
Git 是收尾动作，不代替测试。

**Reuse First**（`REUSE / ADAPT / PORT DESIGN / BUILD / DEFER`）见 `AGENTS.md` §6；
**编码行为准则**（含懒惰阶梯、工程八荣八耻）见 `AGENTS.md` §9；**Skill 清单**见 `AGENTS.md` §10。

---

# 5. Issue / Domain Docs

GitHub Issues：`EricKingWhy/intelligence-agent`

- Issue tracker 约定：`docs/agents/issue-tracker.md`
- Triage labels：`docs/agents/triage-labels.md`
- Domain docs：`CONTEXT.md`、`docs/adr/`
- 重要架构决定发生变化时 SHOULD 写 ADR，而不是只留在对话里。

---

# 6. 当前 Ticket 完成条件

不能因为"文件创建了 / 能跑了"就宣称完成。必须：

1. 当前 GitHub Issue、Matt 规格产物或 Ticket 要求完成；
2. 当前模块 Acceptance Criteria 相关项通过；
3. 测试通过（门禁命令见 `docs/SDD_WORKFLOW_PROTOCOL.md` §5）；
4. Failure Case 已覆盖；
5. 如果涉及 Recovery，真实 Kill/Resume 已验证；
6. JSONL / SessionEvent 能观察真实行为；
7. 无 Scope 外改动；
8. diff review 无重大遗漏；
9. Git diff 可解释。

---

# 7. 协作与 Git

- 多 Agent 协作：`AGENTS.md` §11；
- 仓库模型（三个独立 clone、main 是稳态、短分支）：`AGENTS.md` §13；
- Git 授权分类：`AGENTS.md` §14.4（常设授权 / 需批准 / 默认禁止）；
  只读且无需批准的 Git 命令清单在 §14.3；
- 跨仓库两条硬规则（文件级避让、跨 clone 比较用 git 对象）：`AGENTS.md` §14.13；
- SDD 长任务流程：`docs/SDD_WORKFLOW_PROTOCOL.md`（**唯一权威**，`AGENTS.md` §16 只是入口）。

---

# 8. 最终原则

> **Engineering Specification 决定长期目标和架构边界；当前 Ticket 决定当前施工范围；代码与测试证明实现是否真实完成。**

> **优先复用成熟设计，保持 Core 小而可控；任何框架、Provider、插件都不能反向拥有 Agent Runtime。**
