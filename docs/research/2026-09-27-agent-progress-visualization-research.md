# Agent 任务进度可视化：一手资料调研

- 调研日期：2026-09-27
- 调研问题：成熟 AI 编程产品如何实现「复杂任务拆成子任务、逐个执行、完成即划掉，让用户看到当前进度/已完成/剩余」
- 引用纪律：每条结论标注 产品名 + URL + 关键句；二手资料标「二手」；查不到的标「未找到公开证据」
- 重要事实澄清：公开产品名为 **ZCode**（zcode.z.ai）的是**智谱（Z.ai / GLM 系列）**的产品，不是腾讯产品；腾讯对应产品是 **CodeBuddy**。本报告「zcode」一节按智谱 ZCode 调研并如实标注归属。用户提供的 UI 行为观察（进程 14/16、已完成 13 项、→ 前缀、绿勾+划线）为实测截图证据，本报告将其与公开资料对照。

---

## 执行摘要

对 8 个对象（zcode、Claude Code、OpenAI Codex、Cline、Roo Code、Aider、Gemini CLI、Anthropic/OpenAI 长任务博客）调研后，「任务进度可视化」已收敛出一套高度一致的业界做法：

1. **数据模型收敛**：清单项几乎都有 `描述 + 状态`两个核心字段；状态机收敛为 `pending → in_progress → completed` 三态，差异项是 `cancelled/blocked/deleted`（Gemini CLI 有 cancelled+blocked；Claude Code Task 系有 deleted；Codex 只有三态）。
2. **「同时最多一个 in_progress」是最普遍的不变量**：Codex（工具描述明文）、Gemini CLI（文档明文「Only one task can be marked in_progress at any time」）、Claude Code（系统提示词明文「exactly ONE task」）均强制。
3. **两种工具契约范式**：
   - **整表覆盖式**：每次调用传完整清单（Claude Code 旧 TodoWrite、Codex update_plan、Gemini CLI write_todos、Cline task_progress）。
   - **增量补丁式**：按 taskId 单项增删改，支持依赖边（Claude Code 新 TaskCreate/TaskUpdate/TaskGet/TaskList，含 blocks/blockedBy/owner，是唯一带依赖图的方案）。
4. **渲染形态收敛**：计数 `N/M` + 当前项高亮（activeForm/→ 前缀）+ 完成项划线/绿勾 + 可折叠列表。终端产品（Codex/Gemini CLI）把当前 in_progress 项钉在输入框上方；IDE/桌面产品（Claude Code SDK 宿主、Cline、ZCode）做成进度面板。
5. **持久化分两派**：**内存/会话事件派**（Claude Code、Codex、Gemini CLI——会话作用域，Codex 靠 rollout 事件重放恢复）vs **文件派**（Cline Focus Chain 存 `focus_chain_<taskid>.md` 并用文件监听同步用户编辑；Anthropic 长任务实验存 `feature_list.json` + `claude-progress.txt` + git；OpenAI 长任务博客存 4 个 markdown 文件；AiderDesk 存 `todos.json`）。
6. **与上下文管理的关系**：文件派明确把清单当「跨上下文边界的记忆锚点」（Anthropic：「finding a way for agents to quickly understand the state of work when starting with a fresh context window」）；Codex 的 compaction prompt 会引用 plan 摘要（二手证据）；Cline 把清单反复重注入 prompt（官方博客「the plan itself becomes part of the prompt」）。
7. **对「服务端单一事实 + 多端薄渲染」**：Cline 的「文件 + 文件监听 + UI 同步」与 Codex 的「core 事件流 → app-server protocol → TUI/多端重建」两条已验证路径都支持该架构可行，详见第 11 节。

---

## 1. zcode（ZCode, zcode.z.ai —— 实为智谱产品，非腾讯）

> 归属说明：检索「腾讯 zcode」返回的全部是腾讯 CodeBuddy 资料（cloud.tencent.com/document/product/1749）；名为 ZCode 的公开产品域名为 zcode.z.ai，官方文档自述「ZCode 是一个把 GLM-5.3 带入真实编程工作流的 Agentic Development Environment」。未找到腾讯名下名为「zcode」的公开产品证据。以下按智谱 ZCode 记录。

| 维度 | 结论 | 证据 |
|---|---|---|
| 数据模型 | **未找到公开实现级证据**。二手文章称有 Goal 概念：「一个 Goal 代表一个完整的开发目标，可以包含多个子任务和检查点」「每个 Goal 都有明确的状态跟踪：规划中、执行中、已完成、已失败」（二手，CSDN）。 | 二手：https://codearts.csdn.net/6a90e661ec9fcc4cd3d067f4.html |
| 工具契约 | **未找到公开证据**（Agent 用什么工具/API 更新清单，官方文档未披露）。 | zcode.z.ai/cn/docs 无相关页面 |
| 渲染形态 | 官方文档（一手）确认存在右侧 Goal 面板与多端进度查看：「通过桌面端工作区、Remote 和 Bot Channel，也可以在长任务执行中持续查看进度并补充指令」「桌面端、手机端 Remote 与飞书/微信 Bot 可以共同推进同一个工作区任务」。二手描述：「ZCode 的 Goal 面板会把目标、完成项、分支、Git 变化和工作时长放在旁边」。用户实测截图：「进程 14/16」计数、「已完成 13 项」折叠列表、进行中项 → 前缀、未完成空心圆、完成绿勾+划线——与 Claude Code/Cline 的渲染约定一致。 | 一手：https://zcode.z.ai/cn/docs ；二手：https://finance.sina.com.cn/wm/2026-07-02/doc-inifnaik5776341.shtml ；实测截图（用户提供） |
| 持久化与恢复 | 官方文档（一手）说明任务列表侧栏支持分组/归档：「已完成、无未读、未置顶且超过保留期的任务会进入自动归档候选（保留期可设 3/7/14/30 天）」——说明任务级状态有服务端/本地持久化，但**清单项级（子任务进度）存哪未披露**。 | 一手：https://zcode.z.ai/cn/docs/task-management |
| 与上下文管理关系 | **未找到公开证据**。官方仅称「结合 GLM-5.3 的长上下文能力，持续读取文件、终端、浏览器、执行模式和 Git 状态」。 | 一手：https://zcode.z.ai/cn/docs |

**结论**：zcode 进度可视化的**渲染形态与多端同步存在**（一手官方文档 + 用户实测），但**数据模型、工具契约、压缩关系均无公开实现证据**。

## 2. Claude Code（docs.anthropic.com / code.claude.com）

| 维度 | 结论 | 证据 |
|---|---|---|
| 数据模型 | 两代并存。**旧 TodoWrite**：每项 `{content, status, activeForm}`，status ∈ `pending/in_progress/completed`；content 用祈使句、activeForm 用现在进行时（渲染 spinner 用）。**新 Task 系**（v2.1.268+ 默认）：`{id, subject, description, status, activeForm?, metadata?, owner?, blocks[], blockedBy[]}`，status 增加 `deleted`；是唯一带**依赖边**（blocks/blockedBy）的方案。生命周期（官方原文）：「Created: pending → Activated: in_progress → Completed: completed → Removed: 用 TaskUpdate 设 `status: "deleted"`」。 | 一手：https://code.claude.com/docs/en/agent-sdk/todo-tracking |
| 工具契约 | 旧：`TodoWrite(todos[])` 整表覆盖。新：`TaskCreate{subject, description, activeForm?, metadata?}` / `TaskUpdate{taskId, status?, ..., addBlocks?, addBlockedBy?, owner?}` / `TaskGet` / `TaskList`。不变量（系统提示词，经反汇编二手确认）：「Exactly ONE task must have status in_progress at any time」「Mark tasks as completed IMMEDIATELY after finishing，不要批量」「in_progress 必须在开始工作前标记」。触发条件：「≥3 步的复杂任务/用户提供列表/显式请求」。注意陷阱：taskId 不在 TaskCreate 输入里，要从配对 tool_result 的 `tool_use_result.task.id` 读；模型可能发出 `id`/`task_id`/`active_form` 等非规范键名，宿主需防御性读取（官方原文「Read TaskUpdate input fields defensively」）。 | 一手：https://code.claude.com/docs/en/agent-sdk/todo-tracking ；二手（提示词反汇编）：https://deepwiki.com/marckrenn/cc-mvp-prompts/4.1-todowrite-system |
| 渲染形态 | 官方 SDK 文档给出两种推荐形态：**日志式**（每个 create/update 打一行）与**实时进度面板**（维护 Map<taskId, task>，渲染 `Progress: {completed}/{total} completed` + `Currently working on: {n} task(s)` + 逐项图标 ✅/🔧 + 进行中项显示 activeForm）。即官方背书了「N/M 计数 + 当前项用 activeForm 文案」的渲染范式。 | 一手：https://code.claude.com/docs/en/agent-sdk/todo-tracking |
| 持久化与恢复 | **官方文档未明确**存储位置与 resume 语义。可确认的是：所有变更以结构化 `tool_use` 块进入消息流（「You see each change in the message stream as a structured tool call」），宿主应用自行维护状态 Map。v2.1.142 起 Task 工具替代 TodoWrite 的迁移由 `CLAUDE_CODE_ENABLE_TASKS` 环境变量控制。 | 一手：同上；存储细节未找到公开证据 |
| 与上下文管理关系 | **未找到公开证据**（todo-tracking 页只字未提 compaction）。官方仅称「Newer models track multi-step work without a written todo list」——新模型在内部跟踪多步工作，不再默认下发书面 todo 工具。 | 一手：同上 |

## 3. OpenAI Codex（github.com/openai/codex + developers.openai.com）

| 维度 | 结论 | 证据 |
|---|---|---|
| 数据模型 | plan 项 = `{step: string, status: "pending"\|"in_progress"\|"completed"}`，顶层 `{plan: [...], explanation?: string}`。无 id/优先级/依赖字段——顺序即优先级。**源码一手**：`plan_spec.rs` 中 `JsonSchema::string_enum(vec![json!("pending"), json!("in_progress"), json!("completed")])`。 | 一手（源码）：https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/plan_spec.rs |
| 工具契约 | 工具名 `update_plan`，整表覆盖式。工具描述原文（源码）：「Updates the task plan. Provide an optional explanation and a list of plan items, each with a step and status. **At most one step can be in_progress at a time.**」——不变量写在工具描述里（提示层约束，handler 只做 JSON 解析，不校验）。handler 源码：`parse_update_plan_arguments` 仅 serde 反序列化，然后 `session.send_event(turn.as_ref(), EventMsg::PlanUpdate(args))`；Plan mode 下禁用该工具（「update_plan is a TODO/checklist tool and is not allowed in Plan mode」）。官方 prompting guide 称其为「our default TODO tool」。v0.152.0 起 update_plan 从默认开改为 opt-in（`[tools.update_plan] enabled = true`）。 | 一手（源码）：plan.rs / plan_spec.rs（URL 同上）；一手（官方 guide）：https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide ；二手（opt-in 变更）：https://codex.danielvaughan.com/2026/09/03/codex-cli-v0152-vim-search-mcp-per-tool-token-limits-planning-tool-opt-in |
| 渲染形态 | `EventMsg::PlanUpdate` 事件 → TUI plan view 渲染结构化清单（一手源码注释：「renders that structure in the TUI's plan view」为二手转述，源码本身只到事件层）。Plan mode 另有独立渲染：`TurnItem::Plan` + `PlanDelta` 流式，TUI 有专用 proposed-plan 历史单元格（特殊背景+padding），有计划项时才显示「Implement this plan?」提示（一手 PR #9786）。 | 一手（PR）：https://github.com/openai/codex/pull/9786 ；二手：https://codex.danielvaughan.com/2026/08/31/codex-cli-update-plan-tool-opt-in-external-planning-competing-surfaces |
| 持久化与恢复 | **事件重放恢复**（一手 PR #9786）：「Persist ItemCompleted events only for plan items for rollout replay」「Rebuild plan items from persisted ItemCompleted events on resume」——plan 项通过 rollout（会话事件日志）持久化，resume 时重建；并经 app-server protocol v2（`ThreadItem::Plan` / `PlanDeltaNotification`，标 EXPERIMENTAL）分发给多客户端。 | 一手（PR）：https://github.com/openai/codex/pull/9786 |
| 与上下文管理关系 | **二手证据**：「Subsequent compaction prompts, goal-continuation prompts, and collaboration-mode instructions all reference the plan, giving the model a compact, structured digest of where it is in a long task without replaying the entire conversation history」——plan 被注入 compaction 摘要 prompt，作为压缩锚点。「otherwise compaction or session resumption erases its working state and it starts repeating completed steps」明确说明动机。 | 二手：https://codex.danielvaughan.com/2026/08/31/codex-cli-update-plan-tool-opt-in-external-planning-competing-surfaces |

## 4. Cline / Roo Code（github.com/cline/cline 等）

| 维度 | 结论 | 证据 |
|---|---|---|
| 数据模型 | Cline 的机制叫 **Focus Chain**：markdown 清单（`- [ ]` / `- [x]` checkbox），无独立状态枚举——勾选状态即数据。文件注释含元信息。任务标题显示计数「3/8」。 | 一手（官方文档）：https://docs.cline.net.cn/features/focus-chain ；一手（DeepWiki 源码解读）：https://deepwiki.com/a24z-ai/cline/5.5-focus-chain-and-task-progress |
| 工具契约 | 无独立工具——**任何工具调用都可附带 `task_progress` 参数**（值为完整 markdown 清单，覆盖式）。配套**提醒不变量**：`apiRequestsSinceLastTodoUpdate` 计数器，默认每 6 条消息（可配 1-100）提醒模型更新；Plan→Act 切换时强制要求建清单（「TODO LIST CREATION REQUIRED - ACT MODE ACTIVATED」）；按完成百分比注入差异化提醒文案（0%：「No items are marked complete yet. Remember to mark items as complete when finished.」）。 | 一手（DeepWiki 源码解读）：https://deepwiki.com/a24z-ai/cline/5.5-focus-chain-and-task-progress |
| 渲染形态 | 官方文档（一手）：「任务标题显示清晰的进度指示器；步骤计数器显示当前进度（例如 3/8）；已完成项目以复选标记清晰标记；当前工作以指示器突出显示；可展开视图查看完整的待办事项列表」。示例渲染：`✓ 已完成项` / `○ 未完成项 ← Currently working`。 | 一手：https://docs.cline.net.cn/features/focus-chain |
| 持久化与恢复 | **文件持久化 + 文件监听**（一手源码解读）：每任务一个 `focus_chain_<taskId>.md`，存于任务目录，「persists between sessions」；用 Chokidar 监听该文件（300ms 稳定阈值 + 300ms 应用层防抖），**用户在任意 markdown 编辑器里改清单会被自动同步回任务状态和 UI**——这是「单一事实源 + 薄渲染」的开源先例。 | 一手（DeepWiki 源码解读）：https://deepwiki.com/a24z-ai/cline/5.5-focus-chain-and-task-progress |
| 与上下文管理关系 | 官方博客（一手）明确定位为 context 管理工具而非 UI 装饰：「This is not just a to-do list or a UI nicety – it's a context-forward approach」「the plan itself becomes part of the prompt, reminding the model of past actions and upcoming actions」「the evolving to-do list travels with it through the context」。无 task_progress 更新时会重读文件并通过 `say("task_progress")` 重新注入。Roo Code 另有 **Checkpoints**（git 双分支快照，支持恢复工作区/对话，注意：**不支持 Windows**）——属于状态快照而非清单机制。 | 一手（官方博客）：http://cline.ghost.io/focus-attention-isnt-enough/ ；一手（DeepWiki）：https://deepwiki.com/aidrivencoder/Roo-Cline/5.4-checkpoints |

## 5. Aider

| 维度 | 结论 | 证据 |
|---|---|---|
| 全部 | **Aider 本体（Aider-AI/aider）未找到内置 todo/plan 机制的公开证据**。其设计哲学是 pair programming 而非 agent 长任务（二手：创始人 Paul Gauthier 坚持「Aider is an AI pair programming tool」，一次响应一条指令）。第三方衍生 **AiderDesk** 有 todo 系统（二手）：工具 `set_items`/`get_items`/`update_item_completion`/`clear_items`，清单存每任务目录的 `todos.json`（「persistent across task sessions」），GUI 浮动 todo 窗口支持手动勾选/增删改。 | 二手：https://aiderdesk.hotovo.com/docs/agent-mode/task-management ；二手：https://zenn.dev/takets/articles/how-to-use-aider-en |

## 6. Gemini CLI（google-gemini/gemini-cli）

| 维度 | 结论 | 证据 |
|---|---|---|
| 数据模型 | 每项 `{description: string, status}`，status ∈ `pending / in_progress / completed / cancelled / blocked`——**五态，是调研对象中状态最多**的（比 Claude Code 多 blocked，比 Codex 多 cancelled+blocked）。 | 一手（官方文档）：http://geminicli.com/docs/tools/todos |
| 工具契约 | 工具名 `write_todos(todos[])`，**整表覆盖**（「This replaces the existing list」）。不变量：「Only one task can be marked in_progress at any time」（文档明文；二手称源码有强制校验）。动态调整：计划可演化，「new tasks being added or unnecessary ones being cancelled」。默认启用，`settings.json` 设 `"useWriteTodos": false` 可关。 | 一手：http://geminicli.com/docs/tools/todos ；一手（仓库文档镜像）：https://github.com/st-le/gemini-cli/blob/main/docs/tools/todos.md ；二手（源码校验细节）：https://blog.gitcode.com/ff6a187455fb1c7db872ae023a6a385d.html |
| 渲染形态 | 终端渲染：「Updates the progress indicator above the CLI input prompt」——当前 in_progress 项钉在输入框上方；`Ctrl+T` 切换完整清单视图。教程示例：`[IN_PROGRESS] Create tsconfig.json` 高亮当前焦点。 | 一手：http://geminicli.com/docs/tools/todos ；https://geminicli.com/docs/cli/tutorials/task-planning |
| 持久化与恢复 | 「Persistence: Todo state is scoped to the current session」——**会话作用域，不跨会话持久化**。 | 一手：http://geminicli.com/docs/tools/todos |
| 与上下文管理关系 | 官方教程定位为对抗上下文遗忘：「Standard LLMs have a limited context window and can 'forget' the original goal after 10 turns of code generation. Task planning provides: Visibility / Focus / Resilience」。压缩时是否作为锚点保留：**未找到公开证据**。 | 一手：https://geminicli.com/docs/cli/tutorials/task-planning |

## 7. Anthropic 工程博客：长任务 harness 实验

URL：https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents （全部一手）

| 维度 | 结论 |
|---|---|
| 数据模型 | `feature_list.json`，每项 4 字段：`{category, description, steps[], passes: bool}`。steps 是**端到端验证步骤**（以人类用户视角写，如「Click the 'New Chat' button」）；passes 初始全 false。规模示例：claude.ai 克隆生成了 200+ 条。刻意选 JSON 而非 Markdown：「the model is less likely to inappropriately change or overwrite JSON files compared to Markdown files」。 |
| 工具契约 | **无专用工具**——就是普通文件读写 + 强硬 prompt 约束：「We prompt coding agents to edit this file only by changing the status of a passes field」「It is unacceptable to remove or edit tests because this could lead to missing or buggy functionality」。关键操作纪律：**一次只做一个 feature**（「This incremental approach turned out to be critical to addressing the agent's tendency to do too much at once」）；**只有端到端测试通过才能改 passes**（要用浏览器自动化像人一样验证，不能只用单测/curl）。 |
| 渲染形态 | 无 UI 渲染（实验 harness，非产品）。进度可见性 = 文件 + git log。 |
| 持久化与恢复 | 三件套：`feature_list.json`（需求清单）+ `claude-progress.txt`（进度日志，每个 session 开头读、结尾写）+ git（描述性 commit，可回滚坏改动）。核心洞察原文：「The key insight here was finding a way for agents to quickly understand the state of work when starting with a fresh context window, which is accomplished with the claude-progress.txt file alongside the git history」。 |
| 与上下文管理关系 | 整个机制就是**为跨上下文边界设计的**：initializer agent（第一个 session）生成全部清单 → 后续每个 coding session 以固定启动序列恢复状态（`pwd` → 读 progress 文件 → 读 feature_list → `git log --oneline -20` → 跑 init.sh 起服务验证）→ 选最高优先级未完成项 → 做完 → 改 passes → commit + 写进度。 |

## 8. OpenAI 长任务博客：Codex 跑 25 小时

URL：https://developers.openai.com/blog/run-long-horizon-tasks-with-codex/ （全部一手）

| 维度 | 结论 |
|---|---|
| 数据模型 | 不用 JSON，用 **4 个 markdown 文件**做「durable project memory」：`Prompt.md`（Goals/non-goals/硬约束/Deliverables/"Done when"）、`Plan.md`（里程碑，每个小到能在一个 loop 内完成 + 每个里程碑的验收标准与验证命令 + stop-and-fix 规则 + 决策记录防反复）、`Implement.md`（runbook：跟着 plan 走、diff 不扩scope、每里程碑跑验证、持续更新文档）、`Documentation.md`（**Current milestone status: what's done, what's next** + 决策与原因 + 运行方法 + known issues）。 |
| 工具契约 | 无专用工具。agent loop 固定第 6 步「Update docs/status」（Plan → Edit → Run → Observe → Repair → **Update docs/status** → Repeat）。状态机无枚举，靠「what's done / what's next」两段式 + stop-and-fix（验证不过不许推进）。 |
| 渲染形态 | documentation.md 即进度面板：「This is the shared memory and audit log. It's how I can step away for hours and still understand what happened.」另让 Codex 生成了 session summary dashboard 页。 |
| 持久化与恢复 | 全部外化到仓库文件。「Externalized state (repo, files, docs, worktrees, outputs)」是 agent loop 三大能力之一。 |
| 与上下文管理关系 | 全文未提 compaction 技术，但机制等价：「The most important technique was durable project memory. I wrote the spec, plan, constraints, and status in markdown files that Codex could revisit repeatedly. That prevented drift and kept a stable definition of 'done.'」另注：Codex 已有原生 plan mode（`/plan` 斜杠命令，app/CLI/IDE 都有），「break a larger task into a clear, reviewable sequence of steps before making changes」。 |

---

## 9. 横向对比表

| 产品 | 清单项字段 | 状态机 | 工具契约 | 单 in_progress 不变量 | 渲染 | 持久化 | 压缩锚点 |
|---|---|---|---|---|---|---|---|
| Claude Code (Task 系) | id/subject/description/activeForm/metadata/owner/**blocks/blockedBy** | pending→in_progress→completed + deleted | TaskCreate/TaskUpdate/TaskGet/TaskList（**增量补丁**） | 是（提示词「exactly ONE」） | N/M 计数+activeForm（SDK 官方范式） | 会话消息流；存储未公开 | 未找到公开证据 |
| Codex | step/status | 三态 | update_plan（**整表覆盖**） | 是（工具描述明文） | TUI plan view；Plan mode 独立 cell | **rollout 事件重放恢复** | 是，compaction prompt 引用 plan（二手） |
| Gemini CLI | description/status | **五态**（+cancelled/blocked） | write_todos（整表覆盖） | 是（文档明文） | 输入框上方当前项 + Ctrl+T 全表 | 会话作用域 | 未找到公开证据 |
| Cline (Focus Chain) | markdown checkbox | 勾选/未勾（文件态） | 任意工具附带 task_progress 参数 | 无（靠提醒纪律） | 标题 3/8 计数+✓/○+当前项指示 | **markdown 文件 + Chokidar 监听用户编辑** | 是，清单反复重注入 prompt（官方博客） |
| zcode (智谱) | 未公开 | 二手：规划中/执行中/已完成/已失败（Goal 级） | 未公开 | 未公开 | 进程 14/16 + 已完成 13 折叠 + →/○/绿勾划线（实测） | 任务级有归档持久化；清单级未公开 | 未公开 |
| Anthropic 长任务实验 | category/description/steps[]/passes | passes: bool | 无工具，文件+prompt 强约束 | 一次做一个 feature | 无 UI | **JSON 文件+progress.txt+git** | 是，整个机制为跨上下文设计 |
| OpenAI 长任务博客 | 4 个 md 文件的 section 结构 | what's done/what's next 两段式 | 无工具，agent loop 固定更新步 | stop-and-fix | md 文件即面板 | **仓库文件外化** | 是（durable project memory） |
| Aider | — | — | 本体无此机制（AiderDesk 衍生有 todos.json） | — | — | — | — |

### 收敛点

1. **三态核心 + 最多一个 in_progress**：所有有专用工具的产品一致。
2. **「描述 + 状态」最小字段集**：id/依赖/优先级是奢侈品，只有 Claude Code Task 系有依赖边。
3. **渲染四件套**：N/M 计数、当前项高亮/进行时文案、完成项划线或绿勾、可折叠全表——zcode 实测 UI 与 Cline/Claude Code 完全同构。
4. **触发阈值**：≥3 步才建清单（Claude Code 明文；其余产品行为一致）。
5. **长任务必配外化状态**：两个长任务博客都不约而同用「文件 + git」做跨上下文记忆，清单是唯一允许改状态位、禁止改内容的神圣文件。

### 分歧点

1. **整表覆盖 vs 增量补丁**：覆盖式简单防漂移（Codex/Gemini/旧 TodoWrite/Cline）；补丁式省 token 且支持依赖图（Claude Code Task 系）。
2. **内存事件 vs 文件**：会话产品偏内存/事件（恢复靠重放）；长任务/可编辑场景偏文件（用户可直接改，文件监听回同步）。
3. **状态粒度**：bool（passes）→ 三态 → 五态（Gemini 的 blocked/cancelled）→ 依赖图（Claude Code）。越面向弱模型/长任务，状态越简单（Anthropic 实验只用 bool）。
4. **不变量的强制层级**：工具描述明文（Codex）vs 文档明文+源码校验（Gemini）vs 系统提示词（Claude Code）vs 纯 prompt 纪律（Anthropic 实验）。**没有任何一家在 handler 里硬校验单 in_progress**（Codex handler 源码只反序列化）——不变量靠 prompt 约束，渲染端需容忍违规。

---

## 10. 对「服务端单一事实 + 多端薄渲染」可行性的证据评估

**结论：可行，且有三条已验证的先例路径。**

1. **Codex 路径（事件流 + 协议分发）**：core 发 `EventMsg::PlanUpdate` → app-server protocol v2 定义 `ThreadItem::Plan` / `PlanDeltaNotification` → TUI 等多客户端各自渲染；resume 时从持久化的 `ItemCompleted` 事件**重建** plan 项（PR #9786 一手）。这证明「清单状态放服务端、客户端只做事件订阅 + 薄渲染、断线后重放恢复」在工程上已被 OpenAI 采用。注意其 delta 被标注 EXPERIMENTAL 且「deltas may not match the final plan item」——**增量流要允许最终以完整快照为准**。
2. **Cline 路径（文件单一事实 + 监听同步）**：`focus_chain_<taskId>.md` 是唯一事实源，AI 写（task_progress）、用户改（任意编辑器）、文件监听（Chokidar + 双重防抖）把变更同步回 UI——证明「单一事实源被多方修改、渲染端被动同步」可行，且防抖参数（300ms 稳定阈值 + 300ms 应用层）可直接参考。
3. **zcode 路径（多端产品化）**：官方文档一手确认「桌面端、手机端 Remote 与飞书/微信 Bot 可以共同推进同一个工作区任务」——证明多终端共享同一任务状态在产品层面已落地（实现细节未公开）。

**给低成本模型执行者的落地建议（由证据直接推出）：**

- 状态机用三态 + deleted/cancelled 软删除即可，**不要引入 blocked**（只有 Gemini 有，且会迫使弱模型做依赖判断）；需要依赖再学 Claude Code 加 blockedBy 数组。
- 不变量「最多一个 in_progress」写进工具描述 + 系统提示词，但**渲染端必须容忍违规**（ evidence：Codex handler 不校验）。
- 工具契约：弱模型优先**整表覆盖式**（Gemini/Codex 同款，无 id 关联难题）；若选增量补丁式，必须处理 Claude Code 文档警告的键名漂移（id/task_id/taskId）与 create→result 的 id 关联问题。
- 恢复：采用「事件持久化 + resume 重放重建」（Codex 模式）或「清单落盘文件 + 重读注入」（Cline/Anthropic 模式），二选一；后者对弱模型更友好（每次压缩后重新读文件即可，Anthropic 实验已验证）。
- 压缩锚点：压缩摘要 prompt 中强制包含当前清单快照（Codex 二手证据 + Cline 官方博客「the plan itself becomes part of the prompt」+ Anthropic 启动序列三件套）。

---

## 11. 未决问题清单

1. **zcode 实现细节全无公开证据**：工具名、字段、状态机、清单级持久化位置、压缩关系均未知。若 zcode 是内部/合作产品，需内部渠道确认；若能拿到其桌面端，可通过抓包/本地数据目录逆向（需授权）。
2. **Claude Code todo 的持久化与压缩语义**：官方文档未写清单存哪、resume/compact 后是否保留。需读 Claude Code 本地 session 文件格式或 changelog 进一步确认。
3. **Gemini CLI 的 blocked 状态语义**：谁负责标记 blocked（模型自主还是检测到错误后）？blocked 如何解除？文档未展开。
4. **Codex compaction 引用 plan 的确切机制**：仅二手（codex.danielvaughan.com）称 compaction prompt 含 plan 摘要，未在源码中定位到对应模板，需在 codex-rs 仓库搜 compaction 相关 prompt 模板验证。
5. **「最多一个 in_progress」违规率**：没有任何公开数据说明弱模型违反该不变量的频率；对低成本模型执行者，建议自测后再决定是否需要在宿主侧加纠正逻辑。
6. **多窗口/多 Agent 并发改同一清单的冲突处理**：Cline 文件监听解决「人+AI」两方，但「多 AI 实例 + 多端」并发写的合并策略（CRDT？最后写入胜出？）所有产品均无公开方案。Claude Code 的 blocks/blockedBy 只解决依赖表达，不解决并发写。
7. **进度百分比的分母稳定性**：清单会动态增删（Gemini 文档明确允许），N/M 计数在 M 变化时如何避免用户困惑（如 14/16 变 14/20），无产品公开 UX 处理细节。
