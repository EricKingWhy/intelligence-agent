# ADR-0051 — Protected fact 冲突澄清：持久用户输入与同 run 续跑

- **Status**: Accepted（用户于 2026-10-05 选择“暂停并续跑当前 run”）
- **Date**: 2026-10-05
- **Deciders**: 用户（交互范围与恢复方式）+ 本 Agent（按现有 SessionEvent / ToolExecutor 机制落地）
- **Related**:
  - GitHub Issue [#663](https://github.com/EricKingWhy/intelligence-agent/issues/663)
  - ADR-0044（既有长任务暂停/恢复账本；本 ADR 为 #663 增加受限输入等待原因）
  - `03_SESSION_EVENT_MODEL.md`、`04_TOOL_RUNTIME.md`、`06_CONTEXT_ARTIFACT_MEMORY.md`、`07_STORAGE_PERSISTENCE_RECOVERY.md`、`11_STREAMING_API_WEB_UI.md`
  - 产品机制：[Claude Code AskUserQuestion](https://github.com/anthropics/claude-code/blob/main/plugins/plugin-dev/skills/command-development/references/interactive-commands.md)、[OpenAI Agents API user question flow](https://developers.openai.com/api/docs/guides/agents-api/tools/computer-use)

## 1. Context

#663 中 protected facts 的新增仍由主模型自主决定。普通消息、寒暄和非长期约束不触发询问。主模型识别到用户明确更正某条 active protected constraint 时，必须发起一次有界澄清，让用户选择永久替换、仅当前任务例外、保留旧约束或自定义回复。对没有明确更正、但可能与 active constraint 冲突的新要求，只有主模型无法可靠判断其持久范围时才询问。普通非冲突补充不触发询问。

Claude Code 的官方 AskUserQuestion 指南支持少量明确选项、自动提供 Other 自定义输入，并建议已知/可脚本化流程避免多余提问。OpenAI Agents API 将问题交给宿主 UI 展示，再把答案回传同一会话继续处理。这里借鉴交互形状和同会话续跑机制，不移植产品运行时。

## 2. Decisions

### D1 — 明确更正进入澄清，其他冲突按持久范围判断

主模型日常自主判断是否调用 `register_constraint`。用户明确更正某条 active protected constraint 时，主模型调用专用 `request_constraint_resolution`，由用户选择更正的持久范围。对没有明确更正、但可能与 active protected constraint 冲突的新要求，仅在主模型不能可靠判断持久范围时调用该工具。普通消息、寒暄和不冲突的新增约束不询问。该工具不是后台语义冲突检测器，也不要求用户为每条消息确认。

请求卡展示旧约束与新约束，并提供四个动作：永久替换旧约束、只在当前任务采用新约束、保留旧约束并忽略本次要求、自定义回复。用户未回答前不新增、替代或撤销任何 protected fact。

### D2 — 澄清走 Runtime / ToolExecutor，并独占当前工具批次

请求工具只接受一个 active constraint fact id 和来自当前直接用户消息的候选原文。Runtime 在主模型范围内开放；委派 child 不获得该工具。它通过现有 ToolExecutor 产生 `user/input-requested` durable SessionEvent。含该请求的工具批次不得执行其他工具，也不得在之后发起新的模型或工具工作。

这是一种用户输入等待，不是 Tool Permission / Approval 决策；不新增权限枚举，也不复用审批队列。

### D3 — 等待状态使用既有 Run 生命周期与同一 Run 恢复

请求事件持久化后，Runtime 以 `run/paused(reason=user_input, input_request_id=...)` 收束当前执行，不发 closeout 模型请求。该状态不终结逻辑 run。

回答通过既有 resume 入口提交，必须匹配当前 paused run、CAS version 和 pending request id；`resume_basis=user_input`。答案作为一条普通 `user/message` 保存，携带 `input_request_id`，随后以同一 `run_id` 记录 `run/resumed` 并继续。既有 consumed counters、limits 和 budget version 规则保持有效；未对账副作用、预算拒绝、过期版本和错误 request id 继续 fail-closed。

### D4 — 只让“永久替换”写入新约束关系

- 永久替换：答案中包含用户选定的新约束，并经现有 `protected_facts` + `supersedes_fact_id` 来源绑定注解，形成追加式新事实。
- 当前任务例外、保留旧约束、自定义回复：只保存为普通直接用户输入，不自动修改 protected facts。
- 每个 request id 最多接纳一条答案；重复或不匹配的回答不得产生第二条用户消息或第二次 run 恢复。

### D5 — UI 从事件重建，不另造内存等待队列

Web modal 的 pending 状态从 `user/input-requested`、匹配的回答消息和 run 生命周期事件派生。刷新、SSE 重连或服务重启后仍可显示同一张 pending 卡；提交成功后由 durable events 收起。不得把现有 permission approval queue 改名成语义更正流程。

### D6 — Deadline 禁止等待，预算先检查再复查

run 或 session 只要配置了绝对 `deadline_at`，`request_constraint_resolution` 都不会创建 `user/input-requested`。工具到达执行层时返回正常 `rejected`；如果 run deadline 在 tool admission 前已到，则由 ToolExecutor 的既有 deadline 闸门先拒绝，仍不创建问题卡。未来 deadline 也适用。创建请求前，Runtime 使用既有 run/session 恢复余量判据，计入本轮模型决策和本次澄清工具调用；任一作用域无法证明回答后还能继续，就不创建问题卡。这里检查当前余量，不向等待中的 run 持久预留 session 预算；并发会话可能在等待期间消耗共享余量。用户回答时再次检查 session 预算，余量已不足则以 409 拒绝，且不保存答案或 `run/resumed`。这与 OpenAI Agents SDK 在没有剩余 model turns 时拒绝 `RunState.add_input()` 的准入思路一致；其文档没有规定持久预留。

启动扫描对所有 session 的澄清工具预算增量做幂等重放；若重建失败，该 session 在当前进程中拒绝启动新 run，避免使用低估的 session 预算。

## 3. Consequences and boundaries

本决定只扩展 #663 明确批准的冲突交互：一个专用工具、一个 durable event、一个 `user_input` pause/resume basis、一个 Web modal 和对应 API 字段。不新增通用 AskUserQuestion 框架、第二套 Session 真相、长期 Memory Provider 或后台判断模型。预算使用既有 run/session ceiling 和恢复判据，不新增持久预留账本；恢复扫描失败时仅隔离受影响 session，不阻断其他会话。

ADR-0044 和正式 Engineering Specifications 保持冻结；#663 与本 ADR 记录这次明确批准的增量契约。若现有预算/恢复实现不能满足上述 AC，按项目规则停在证据与最小替代方案，不静默改变冻结合同。

## 4. References

- Claude Code AskUserQuestion：选项数量保持少量、提供自定义输入、避免可预先确定的多余询问。
- OpenAI Agents API：由宿主展示问题、把回答送回同一 session 并继续消费事件。
- [OpenAI Agents SDK RunState](https://openai.github.io/openai-agents-python/ref/run_state/)：没有剩余 model turns 时拒绝添加输入。
- #663 更新后的 AC18–AC22：独占批次、同 run answer/resume、刷新与重启恢复、deadline 与预算准入。
