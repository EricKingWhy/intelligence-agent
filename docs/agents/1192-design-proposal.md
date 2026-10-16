# #1192 [Phase 3] Subagent 约束继承 —— 设计/方案依据

> 归属 #1176 Phase 3，依赖 #1191（三层作用域规则层）已合并（`origin/main@211bfe48`）。
> 本文只写**机制**与**方案依据**；进度/门禁读数落 Tracker / `docs/gate/`。

## 1. 目标（票面 §三 逐字）

子 Session 构建时默认注入：项目级 active 约束全集 + 全局级 active 约束全集 + 当前有效
authorization（含父会话 session 级 active 授权与项目/全局级 active 授权）。本会话专属事实
不自动继承。拒绝随继承传播（D4=A）。提供 `omit_constraints` 干净启动开关。

## 2. 方案依据（SDD §1.3；`AGENTS.md` §6.1）

来源（≥2 独立一手来源；代码来源给 file:line，文章给 URL + 读取日期）：

| # | 来源 | 读了什么 | 机制摘要 |
|---|------|----------|----------|
| S1 | Claude Code 官方文档 `https://code.claude.com/docs/en/sub-agents`（2026-10-16 读取） | "What loads at startup" / `omitClaudeMd` / permission 继承 | 非 fork subagent **默认继承 CLAUDE.md 全层级**（用户/项目/本地/managed 的持续约束）；`omitClaudeMd: true` 可关（managed 仍加载）；**不继承**对话历史、auto memory、output style、上下文窗口大小；*"Each inherits the parent conversation's permission rules"*，`permissionMode` 未设时**继承主会话的 permission mode**。 |
| S2 | Cursor 官方文档 `https://cursor.com/docs/subagents`（2026-10-16 读取） | Context isolation / `readonly` | subagent **干净上下文**、无对话历史，父在 prompt 里传必要信息；权限由 `readonly` 收窄。规则/记忆是否继承**文档未提**（未找到）。 |
| S3 | 本仓既有调研 `~/workspace/research/codebuddy-1176-scope-research.md`（2026-10-15，5 家；Claude Code / Cursor / ChatGPT / Windsurf / OpenHands） | 问题 3 结论 | 唯一给出明确子 agent 继承设计的是 Claude Code（继承持续约束、不继承会话态、有 omit 逃生门）；据此 PRD §7 已定 "Phase 3 照抄"。 |

**契合点（对本仓不变量）**：
- 复用 #1191 已落地的注入缝（`protected_facts_for_context(events, extra_facts=...)` +
  `serialize_protected_facts` 只投影 active），**不新造第二套序列化**（不变量 #7 / §9.5 懒惰阶梯第 2 级）。
- 授权是 **Runtime 边界不是 Prompt**（不变量 #11）：注入的 authorization 只是上下文；
  真正的权限闸由**子与父共享的 executor**（`assembly.py:859` `_child_executor_factory` 捕获父 `policy`
  / `approval_callback` / `permission_rules`，"决策 11 权限传递"）承担 ⇒ "父被允许、子被拦"的结构性错位
  **不存在**（子执行器与父同一审批面）。
- 子会话独立 JSONL（`multiagent/provider.py`），**不**改写子事件流伪造来源（票面 §四.3）：
  继承集以**新增独立快照事件**落子会话事件流（票面 §五.5 明确允许），非 fact 来源事件。

**判定**：`ADAPT`（复用 #1191 注入缝与授权面，照抄 Claude Code 的"持续约束随行 + 会话态不随行 + omit 逃生门"模型）。
**License**：不复制上游源码（S1/S2 为闭源产品文档，仅作交互/语义参考，照抄的是**设计**）。

## 3. 精确语义落点

| 票面语义 | 落点 |
|---|---|
| 继承项目 + 全局 active 约束 | 复用 `scoped_constraints.scoped_context_facts(store, parent_facts, project_key=父项目键)` |
| 继承父 session 级 active 授权 | `[f for f in parent_facts if f.type=="authorization" and f.status=="active"]`（会话事件流投影） |
| 不继承会话专属事实 | parent session 级**非 authorization** active facts → 只进快照的 `not_inherited`（展示用），不注入 |
| `omit_constraints` 干净启动 | `delegate` 工具参数 → provider.run → 继承计算短路（inherited=[]，denied=[]） |
| 拒绝随继承传播（D4=A） | 不继承任何 approval 授权（子会话无 grant）；父的 denied 操作身份记入快照 `denied_operations`；子遇到 Y 走**子父共享的**审批/拒绝路径，不静默放行 |
| 快照持久化 + 展示 | 新事件 `agent/constraint-inheritance`（含 injected fact 列表 + `omit_constraints` 取值）；展示层按 `subagent_id` 取数 |
| 注入点 | 子 agent ContextBuilder 组装处（`AgentRuntime` 默认 builder 的 `scoped_fact_provider`） |

## 4. 不做（票面 §七）

- 不改变 fork 语义（fork 本就继承全部 protected facts + 授权重映射 `session/fork.py:317`）。
- 不给子 agent 自动继承会话专属事实。
- 不设条数上限（上下文压力另开票）。
