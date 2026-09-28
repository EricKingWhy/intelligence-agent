# 长任务 Agent 上下文管理调研报告

> 调研日期：2026-09-27
> 方法：WebSearch + WebFetch 抓取一手文档/源码 README/官方博客；二手来源逐条标注。
> 读者假设：执行者是能力较弱的低成本模型，因此本报告优先记录「确定性、可配置、有确切阈值/字段名」的机制，而非依赖模型自觉的机制。

---

## 0. 执行摘要

**核心结论：成熟产品防漂移的答案不是「更大的窗口」或「更好的摘要」，而是把状态外置成确定性制品，让上下文随时可重建。**

1. **压缩是最后手段，裁剪是前置手段**。几乎所有产品都在压缩之前先做确定性裁剪：
   - DeepSeek Harness 先做无模型调用的 tool result 裁剪（超过 8192 字符裁成「头 4096 + 标记 + 尾 1024」），裁完若已低于阈值则**跳过摘要**（`compaction-tool-result-pruner` README）。
   - Cline 先删除同一文件的过期旧版本读取，只留最新版，且「非常保守地」才进一步截断消息。
   - Anthropic API 层的 `clear_tool_uses_20250919` 策略在 100K token 触发时把最旧 tool result 换成占位文本，`keep` 最近 3 对，`exclude_tools` 可豁免关键工具。
   - Claude Code 压缩后最多重读 5 个最近修改的文件；Codex 压缩后仍不够时退化为 head trimming（最旧消息先砍）。

2. **触发阈值全部是「窗口 − 预留」公式，且默认值惊人一致地落在 ~70–90%**：
   - Codex：90% context window（源码 `(context_window * 9) / 10`，用户配置只能调低）。
   - DeepSeek Harness：`floor(min(W × 0.8, W − O − 65536))`，保留最近 16% 逐字。
   - zcode：`window − min(max output, 21000) − ~13000` → 128K 窗口约 94K 触发（73%），1M 约 966K。
   - Claude Code：默认到模型上限；1M 原生窗口模型约 967K；可用 `/autocompact 500k` 等四种途径调。
   - Pi：`contextTokens > contextWindow − reserveTokens(16384)`，保留最近 20000 token。
   - Cline：`maxAllowedSize = contextWindow − 40K(Claude) / 27K(DeepSeek) / 30K(其他)`。
   - Aider：聊天历史独立于窗口，超过 `min(max_input/16, 8192)` token 即后台异步摘要。

3. **摘要内容契约高度收敛**。各产品的压缩摘要几乎都包含同一组字段：原始目标/用户意图、约束与偏好、关键决策及理由、已完成/进行中的工作、错误及修复、下一步、精确标识（文件路径/命令/错误串）。DeepSeek Harness 是唯一把 8 节 Markdown 结构写成硬契约并要求 "Preserve exact file paths, commands, error strings, identifiers, numeric values" 的；Pi 的摘要模板含 `## Critical Context` 和 `<read-files>/<modified-files>` 清单且跨压缩累积。

4. **目标锚定的主流做法是「压缩后从磁盘确定性重注入」，而非指望摘要保真**：
   - Claude Code：`/compact` 后项目根 CLAUDE.md 自动从磁盘重读注入；还可用 `SessionStart(compact)` hook 在每次压缩后把任意内容（如 `git log --oneline -5`、当前 sprint 目标）注入上下文。
   - Cline：Focus Chain 的 todo 列表**穿透压缩保留**，且每 6 条消息重新注入一次（默认节奏，可调）。
   - zcode：`/goal` 命令显式维护「当前会话目标」。
   - Codex：`/goal` 设持久目标；官方长任务博客的核心是四份 Markdown（Prompt/Plan/Implement/Documentation）作为 durable project memory。
   - Anthropic 工程博客：initializer agent 生成 200+ 条 feature 的 JSON 清单，coding agent 只允许改 `passes` 布尔值；选 JSON 的原因是 "the model is less likely to inappropriately change or overwrite JSON files compared to Markdown files"——**用格式约束替代模型自觉**，这对弱模型执行者尤其重要。

5. **「受保护事实」显式数据结构确实存在**：
   - Letta/MemGPT 的 core memory blocks（label/value/limit，agent 用 `core_memory_append/replace` 自编辑，pinned 在 system prompt，驱逐摘要永远动不到它）。
   - Cline Focus Chain 的 todo list（压缩时单独保留）。
   - Anthropic API 的 memory tool（`memory_20250818`）：接近清除阈值时 Claude 收到自动 warning，先把重要信息写进 memory files 再被清。
   - Claude Code 的 auto memory（MEMORY.md 前 200 行/25KB 每次对话开始注入）与「压缩后重注入」清单（CLAUDE.md、plan、skill 正文、5 个最近文件）。

6. **子代理做脏活是标配**：Claude Code 子代理独立上下文窗口、只回摘要（内置 Explore/Plan 还跳过 CLAUDE.md 和 git 快照以保持廉价）；Claude Code 文档明确建议 "send research to a subagent so the file contents stay in its context window, not yours"。Codex 的 ReviewTask 同理（子线程评审，不污染主线）。

7. **对弱模型执行者的直接启示**（详见第 4 节）：优先复制「确定性裁剪 + 受限写入的结构化清单 + 磁盘重注入 + 固定字段摘要模板」这一组合；避免依赖模型自觉维护记忆（Letta 式自编辑记忆对弱模型风险最高）。

**一个重要纠偏**：任务书中「腾讯 zcode」经查证为**智谱（Z.ai）ZCode**（github.com/zai-org/ZCode，2026-09-21 开源）；未找到任何「腾讯 zcode」产品的公开证据。本报告按智谱 ZCode 记录，并在其产品节标注此差异。

---

## 1. 分产品机制清单

### 1.1 Claude Code（含 Anthropic API 层 context editing 与官方工程博客）

#### 机制清单表

| 维度 | 机制 | 关键参数/阈值 | 持久化载体 | 来源级别 |
|---|---|---|---|---|
| 自动压缩触发 | auto-compact window | 默认=模型上下文上限；1M 原生窗口模型约 967K；200K 窗口在 200K 边界 | — | 一手 |
| 压缩配置 | `/autocompact 500k` / settings 键 `autoCompactWindow` / flag `--autocompact` / 环境变量 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`（优先级最高，仅接受纯数字） | 100K–1M | user settings | 一手 |
| 禁用压缩 | `DISABLE_COMPACT` 环境变量 | 禁用后超限直接报 context-limit 错误 | — | 一手 |
| 压缩算法 | LLM 摘要：同一 system prompt+tools+history，末尾追加 summarization instruction；摘要**替换**整个历史 | — | 会话内替换 | 一手 |
| 摘要内容契约 | requests and intent / key technical concepts / files examined or modified with code snippets / errors and fixes / pending tasks / current work；丢弃 full tool outputs 与 intermediate reasoning | — | 摘要消息 | 一手 |
| 自定义压缩指令 | `/compact <instructions>`；或 CLAUDE.md 内 `# Compact instructions` 节 | — | CLAUDE.md | 一手 |
| 压缩后确定性重注入 | 项目根 CLAUDE.md、auto memory、git status、plan mode 计划、最多 5 个最近修改文件（>5000 token 只回路径引用 `Referenced file`）、skill 正文（每 skill 5000 / 总 25000 token 上限，最旧先丢） | 5 个文件；skill 5K/25K | 磁盘文件 | 一手 |
| 压缩后 hook 注入 | `SessionStart` hook + `matcher: "compact"`：压缩后执行并把 stdout 注入上下文 | — | `.claude/settings.json` | 一手 |
| 指令文件层级 | Managed policy → User(`~/.claude/CLAUDE.md`) → Project(`./CLAUDE.md`) → Local(`./CLAUDE.local.md`)；向上拼接不覆盖，向下子目录按需加载；`@path` 导入（递归 4 层） | 单文件上限 4 MiB | 磁盘 | 一手 |
| Auto memory | `~/.claude/projects/<project>/memory/MEMORY.md` 索引 + topic 文件；4 种 type（user/feedback/project/reference） | 前 200 行或 25KB 每次对话开始注入 | 磁盘 | 一手 |
| Todo/任务 | `TaskCreate/TaskUpdate/TaskGet/TaskList`（字段 `subject`/`activeForm`/`status`：pending→in_progress→completed/deleted）；多步任务自动创建 | — | 会话内 | 一手 |
| 目标完成度校验 | `Stop` hook（type: prompt/agent）：任务未完成则以 reason 反馈继续；连续 8 次无进展阻断后强制放行 | 8 次上限，`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` 可调 | settings | 一手 |
| 压缩钩子 | `PreCompact` / `PostCompact` hook（matcher 区分 `manual`/`auto`） | — | settings | 一手 |
| 子代理隔离 | 每个子代理独立上下文窗口，只回摘要；内置 Explore/Plan 只读、跳过 CLAUDE.md 与 git 快照、一次性不可恢复 | 所有子代理 description 合计 >15K token 启动告警 | `.claude/agents/*.md` | 一手 |
| 回退/恢复 | `/rewind`（截断回已缓存前缀，含 "Summarize from here"）；resume 大会话可从摘要恢复；`/fork` | — | 会话存储 | 一手 |
| API 层确定性裁剪 | context editing `clear_tool_uses_20250919` | trigger 默认 100K input tokens；keep 默认 3 对；`clear_at_least`；`exclude_tools`；`clear_tool_inputs` 默认 false | 服务端编辑，客户端保留完整历史 | 一手 |
| API 层保护事实 | memory tool（`memory_20250818`）+ 接近阈值时自动 warning 提示先落盘 | — | memory files | 一手 |
| API 层服务端压缩 | `compact_20260112`（服务端 compaction，官方称优先于已弃用的 SDK `compaction_control`） | beta header `context-management-2025-06-27` | 服务端 | 一手 |

#### 一手引用

- 阈值默认值（code.claude.com/docs/en/model-config）："Models running with a native 1M window, such as Sonnet 5, the Fable models, and Opus 4.7 and later on the Anthropic API, compact before the window fills, at about 967K tokens by default."
- 配置优先级（同上）："set `CLAUDE_CODE_AUTO_COMPACT_WINDOW`. While it's set, it takes precedence over the command, the flag, and the setting"。
- 禁用（同上）："the variable takes effect only when you also set `DISABLE_COMPACT`, which disables all compaction." 禁用后 "sessions stop at the 200K boundary with the context-limit error instead of compacting."
- 压缩机制（code.claude.com/docs/en/prompt-caching）："Compaction replaces your message history with a summary."；"Claude Code sends a separate request with the same system prompt, tools, and history as your conversation, plus a summarization instruction appended as a final user message."
- 摘要契约（code.claude.com/docs/en/context-window）："The summary keeps: your requests and intent, key technical concepts, files examined or modified with important code snippets, errors and how they were fixed, pending tasks, and current work. It replaces the verbatim conversation: full tool outputs and intermediate reasoning are gone."
- 压缩后重读文件（同上）："Right after compaction, Claude Code re-reads up to five of the files Claude has read or edited in the session, choosing the ones modified most recently. A file over 5,000 tokens comes back as a path reference without its content."
- CLAUDE.md 穿透压缩（code.claude.com/docs/en/memory）："Project-root CLAUDE.md survives compaction: after `/compact`, Claude re-reads it from disk and re-injects it into the session."
- 压缩后 hook 锚定（code.claude.com/docs/en/hooks-guide）："When Claude's context window fills up, compaction summarizes the conversation to free space. This can lose important details. Use a `SessionStart` hook with a `compact` matcher to re-inject critical context after every compaction."
- 子代理（code.claude.com/docs/en/sub-agents）："Each subagent runs in its own context window..."；"the subagent does that work in its own context and returns only the summary."
- context editing（platform.claude.com/docs/en/docs/build-with-claude/context-editing）：trigger "Defines when the context editing strategy activates. Once the prompt exceeds this threshold, clearing begins."（默认 100,000 input tokens）；"The API replaces each cleared result with placeholder text indicating to Claude that it was removed."；"Your client application maintains the full, unmodified conversation history."
- memory tool 联动（同上）："When your conversation context approaches the configured clearing threshold, Claude receives an automatic warning to preserve important information. This enables Claude to save tool results or context to its memory files before they're cleared from the conversation history."

#### Anthropic 工程博客：Effective harnesses for long-running agents（anthropic.com/engineering/effective-harnesses-for-long-running-agents）

- 分体架构："an initializer agent that sets up the environment on the first run, and a coding agent that is tasked with making incremental progress in every session, while leaving clear artifacts for the next session." 两者仅初始 user prompt 不同："The system prompt, set of tools, and overall agent harness was otherwise identical."
- 制品三件套：`init.sh`（起 dev server）+ `claude-progress.txt`（进度日志）+ feature list JSON（200+ 功能，初始全部 `"passes": false`）。
- 受限写入："we prompt coding agents to edit this file only by changing the status of a passes field"；"It is unacceptable to remove or edit tests because this could lead to missing or buggy functionality."
- 选 JSON 的理由："the model is less likely to inappropriately change or overwrite JSON files compared to Markdown files."
- 每 session 启动三步：`pwd` → "Read the git logs and progress files to get up to speed" → "Read the features list file and choose the highest-priority feature that's not yet done."
- 先冒烟后开发：每 session 先用 init.sh 起服务器 + Puppeteer MCP 做基础端到端验证，"This ensured that Claude could quickly identify if the app had been left in a broken state."
- 对 compaction 的官方评价："It has context management capabilities such as compaction... However, compaction isn't sufficient."；"compaction... doesn't always pass perfectly clear instructions to the next agent."（注：该文未披露任何压缩阈值/算法。）
- 参考实现：github.com/anthropics/claude-quickstarts/tree/main/autonomous-coding。

---

### 1.2 OpenAI Codex

#### 机制清单表

| 维度 | 机制 | 关键参数/阈值 | 持久化载体 | 来源级别 |
|---|---|---|---|---|
| 自动压缩触发 | auto-compact | `auto_compact_token_limit = context_window × 90%`；`effective_context_window_percent` 默认 95% | — | 一手（GitHub issue 引源码） |
| 用户配置 | `model_auto_compact_token_limit`（config.toml） | 只能调低，被 90% 上限 clamp | config.toml | 一手（issue 引源码注释） |
| 压缩路径 | inline（本地 LLM 摘要）vs remote（POST `/v1/compact`，Responses API 摘要）；v2 支持流式与错误恢复 | 由 `supports_remote_compaction` 与中断/工具调用状态决定 | rollout | 二手（deepwiki 引源码） |
| 两个触发点 | pre-turn（新用户回合前，`DoNotInject`）与 mid-turn（采样后仍需继续工具调用时，`InitialContextInjection::BeforeLastUserMessage`，保留最后一条用户消息） | — | — | 二手（gist 引 codex.rs 行号） |
| 压缩 prompt 模板 | `core/templates/compact/prompt.md`：CONTEXT CHECKPOINT COMPACTION，要求含 current progress and key decisions / constraints / user preferences / what remains / critical data | — | 仓库模板 | 二手（gist 全文转录模板） |
| 摘要前缀 | `summary_prefix.md`：告知"另一个模型已做过摘要，在其基础上继续" | 仅本地路径使用 | 仓库模板 | 二手（gist） |
| 兜底 | 压缩后仍超限 → `TruncationPolicy` head trimming（最旧消息/工具输出先砍）；Ghost snapshots 把压缩前状态记入 rollout 供调试 | — | rollout | 二手（deepwiki/gist） |
| 手动压缩 | `/compact`："Compact the current chat's context." | — | — | 一手（learn.chatgpt.com） |
| 目标管理 | `/goal`：persistent objective，可 pause/resume/edit/clear；建议先 `/plan` 再 `/goal` | — | 会话 | 一手 |
| 指令文件 | `/init` 生成 AGENTS.md 脚手架 | — | AGENTS.md | 一手 |
| 会话恢复 | `codex resume [--last\|--all]`、`codex fork`、`codex exec resume`、`codex archive/unarchive` | resume 按 cwd 过滤会话 | 会话存储 | 一手 |
| 长任务方法（官方博客） | durable project memory：Prompt.md（spec + "Done when"）/ Plan.md（里程碑+验收）/ Implement.md（runbook）/ Documentation.md（status+决策+known issues 持续更新） | 案例：~25 小时、~13M tokens 单 run | 仓库 Markdown | 一手 |
| 子代理隔离 | ReviewTask 子线程评审，不污染主线历史 | — | — | 二手（deepwiki） |

#### 一手引用

- 阈值（github.com/openai/codex/issues/31860，引 `codex-rs/protocol/src/openai_models.rs`）：`let context_limit = self.resolved_context_window().map(|context_window| (context_window * 9) / 10);`；源码注释："Token threshold for automatic compaction. When omitted, core derives it from `context_window` (90%). When provided, core clamps it to 90% of the context window when available."
- `/goal`（learn.chatgpt.com/docs/reference/slash-commands）："A goal is a persistent objective that ChatGPT works toward until it finishes the task, pauses, or needs more input."
- `/init`（同上）："Generate an `AGENTS.md` scaffold for the current project."
- 长任务博客（developers.openai.com/blog/run-long-horizon-tasks-with-codex/）："The most important technique was durable project memory. I wrote the spec, plan, constraints, and status in markdown files that Codex could revisit repeatedly. That prevented drift and kept a stable definition of 'done.'"；成功组合："A clear target and constraints (spec file) / Checkpointed milestones with acceptance criteria / A runbook for how the agent should operate / Continuous verification / A live status/audit log so the run stayed inspectable."
- 注意：该博客**完全未提 compression/compaction**，其思路是外置记忆替代上下文保留。

#### 二手引用（标注）

- gist.github.com/sam-saffron-jarvis（二手，含源码行号）：压缩 prompt 模板全文 "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task. Include: Current progress and key decisions made / Important context, constraints, or user preferences / What remains to be done (clear next steps) / Any critical data, examples, or references needed to continue"；"If neither `context_window` nor `model_auto_compact_token_limit` is set... compaction never fires automatically"（fallback `i64::MAX`）。
- deepwiki（二手）：inline vs remote 分派逻辑；`compact_conversation_history` POST `/v1/compact`；Ghost snapshots "stores the pre-compaction state for potential debugging or reconstruction"。
- wujiaming88.github.io 深度解读（二手）："用户消息逐字保留、20K 上限、从尾部选取"的压缩细节——**仅此一家来源，未在官方文档/源码直接核实，列为待证**。

---

### 1.3 zcode（Zhipu ZCode — 用户任务书称「腾讯 zcode」，实为智谱产品）

> **归属纠偏**：公开一手资料（zcode.z.ai 官方文档、github.com/zai-org/ZCode 开源仓库、官方 FAQ 的 GLM-5.2 绑定表述）均指向智谱 AI。未找到「腾讯 zcode」的公开证据。

#### 机制清单表

| 维度 | 机制 | 关键参数/阈值 | 持久化载体 | 来源级别 |
|---|---|---|---|---|
| 自动压缩触发 | auto-compaction | `触发点 = context window − output reserve（取 min(Max output tokens, 21000)）− safety buffer ≈ 13000`；合计固定约 34K 扣除 | — | 一手（官方 FAQ） |
| 触发点实例 | 128K 窗口约 94K（~73%）；200K 约 166K；1M 约 966K | 触发计数含本轮 tool results 实时估计，故"看起来提前触发" | — | 一手（官方 FAQ） |
| 用户可调性 | "There is currently no user-facing switch or threshold for automatic compaction." | 无开关、无阈值配置 | — | 一手（官方 FAQ） |
| 手动压缩 | `/compact`："压缩当前对话上下文，保留关键信息" | — | — | 一手（commands 文档） |
| 目标锚定 | `/goal`："查看、设置、替换、暂停、恢复或清除当前会话目标，适合持续执行的长任务" | — | 会话 | 一手（commands 文档） |
| 指令文件 | `~/.zcode/AGENTS.md`（用户级）+ workspace `AGENTS.md`（工作区级）；用户级在前、工作区级在后；CLAUDE.md 仅 onboarding 一次性迁移，运行时**不读** | — | 磁盘 | 一手（agents 文档） |
| 窗口识别 | 模型 ID 以 `[1m]` 结尾自动按 1,000,000 计；无元数据默认 200,000；自定义模型可手填窗口值 | — | settings | 一手（官方 FAQ） |
| 长任务模式 | Goal Mode："Organizes sustained work around an objective, completion checks, and state recovery"（产品页描述，无实现级公开细节） | — | — | 一手（产品页，营销层） |
| 开源实现 | github.com/zai-org/ZCode（2026-09-21 开源，含 apps/zcode-cli 的 Agent Runtime） | — | — | 一手（仓库存在性） |

#### 一手引用

- 触发公式（zcode.z.ai/en/docs/qa）："Trigger point = context window − output reserve (capped at 21,000) − a safety buffer of about 13,000"；"a 128K window compacts around 94K tokens (~73%), 200K around 166K, 1M around 966K."
- 提前触发表象（同上）："The trigger count includes a live estimate of this turn's tool results, so it runs slightly ahead of the meter above the input box."
- 无开关（同上）："There is currently no user-facing switch or threshold for automatic compaction."
- `/goal` 与 `/compact`（zcode.z.ai/cn/newdocs/commands）："/goal 查看、设置、替换、暂停、恢复或清除当前会话目标，适合持续执行的长任务"；"/compact 压缩当前对话上下文，保留关键信息"。
- AGENTS.md（zcode-ai.com/en/newdocs/agents）："ZCode currently reads two sources... When both sources exist, ZCode appends the user global instructions first, then the workspace instructions."；"CLAUDE.md is not continuously read by ZCode Agent at runtime... only uses it during onboarding as a one-time migration source."

#### 证据边界

- **压缩摘要的内容契约、裁剪策略、压缩失败行为、压缩后重注入机制：均无公开实现证据**。官方文档只到「触发公式 + 两个命令 + AGENTS.md」层级。开源仓库已存在（zai-org/ZCode），本次未深入其源码核实压缩实现——列为待办（见第 5 节）。

---

### 1.4 Aider

#### 机制清单表

| 维度 | 机制 | 关键参数/阈值 | 持久化载体 | 来源级别 |
|---|---|---|---|---|
| 代码上下文 | repo map：tree-sitter 提取关键符号签名；依赖图 graph ranking 选最重要部分塞进预算 | `--map-tokens` 默认 1024；无文件加入时会显著扩张 | 每次请求重算 | 一手 |
| 聊天历史压缩 | ChatSummary 递归二分摘要：超阈值则 head（旧）摘要不递归、tail（新）原样；摘要失败换模型（weak→main） | `--max-chat-history-tokens` 默认 `min(max_input/16, 8192)`，下限 1024 | 内存 + 历史文件 | 二手（deepwiki/工程手册引 aider/history.py） |
| 摘要异步性 | 后台线程摘要，不阻塞主循环；结果在下一次组装上下文时生效 | — | — | 二手 |
| 摘要 prompt | 简单模板（"I spoke to you previously about a number of things."），**无固定结构契约** | — | aider/prompts.py | 二手 |
| 双缓冲 | `done_messages`（已完成轮次，可被摘要）与 `cur_messages`（当前轮次，永远原样） | — | 内存 | 二手 |
| 历史持久化 | `.aider.chat.history.md`（markdown，用户消息 `####` 前缀）；`--restore-chat-history` 跨会话恢复 | — | 磁盘 | 一手（commands 提及）+ 二手（文件格式） |
| 手动管理 | `/tokens`（分类用量）、`/drop`（释放上下文）、`/clear`、`/reset`、`/read-only`、`/map-refresh` | — | — | 一手 |
| 超限恢复 | 发送前 `check_tokens()` 告警建议 /drop 或 /clear；LiteLLM 抛 `ContextWindowExceededError` 后展示 exhausted 明细 | — | — | 二手 |
| 目标锚定 | **无** todo/goal/检查点类机制（防漂移完全靠用户手动管理 + repo map + git auto-commits） | — | — | 一手（文档无此功能） |

#### 一手引用

- repo map（aider.chat/docs/repomap.html）："The repo map contains a list of the files in the repo, along with the key symbols which are defined in each file."；"Aider solves this problem by sending just the most relevant portions of the repo map... using a graph ranking algorithm, computed on a graph where each source file is a node and edges connect files which have dependencies."；"The token budget is influenced by the `--map-tokens` switch, which defaults to 1k tokens."
- 手动命令（aider.chat/docs/usage/commands.html）："/drop Remove files from the chat session to free up context space"；"/tokens Report on the number of tokens used by the current chat context"。

#### 二手引用（标注）

- deepwiki.com/dwash96/aider-ce（二手，引 aider/history.py 行号）：递归算法 "Base Case: If messages fit within max_tokens... Split Point: Starting from the end, accumulate tail_tokens until reaching max_tokens/2, ensuring the split occurs after an assistant message... maximum depth of 4"；`--max-chat-history-tokens` 默认 8000。
- soviar-systems.github.io/ai_engineering_handbook/aider（二手，引源码）："`max_chat_history_tokens = min(max_input_tokens / 16, 8192), floor 1024`"；后台线程 "summarize_start() / summarize_worker() / summarize_end()... the main conversation loop is never blocked by summarization."

---

### 1.5 Cline

#### 机制清单表

| 维度 | 机制 | 关键参数/阈值 | 持久化载体 | 来源级别 |
|---|---|---|---|---|
| 规则式截断（兜底） | `maxAllowedSize = contextWindow − buffer`；始终保留首条任务消息；按对删除消息维持 user/assistant 流；切换到小窗口模型时 keep 从 half 变 quarter | buffer：Claude 40K / DeepSeek 27K / 标准 30K | — | 一手（官方博客） |
| 确定性去重（前置） | 删除同一文件的过期旧版本读取，只留最新版；"very conservative"，优先保 prompt cache | — | — | 一手（官方博客） |
| Auto Compact | LLM 摘要（同模型 + prompt caching）→ 摘要替换历史 → continuation prompt 续作；不支持的模型自动回退规则式截断 | 触发阈值在 `context-window-utils.ts`（文档未给数值；中文镜像文档称约 80%） | 会话 | 一手（docs.cline.net.cn 镜像） |
| 压缩摘要保真 | "All technical decisions and code patterns are preserved / File changes and project context stay intact"；摘要 tool call 显示成本 | — | — | 一手（镜像文档） |
| Focus Chain（目标锚定） | 任务开始生成 todo list，每 6 条消息（可调）重注入上下文；**todo list 穿透压缩保留** | 默认 reminder interval = 6 messages | markdown todo | 一手（官方博客） |
| Checkpoints | shadow git：每次工具使用（文件编辑/命令）后快照工作区；三种恢复：Restore Files / Restore Task Only / Restore Files & Task | 默认开启 | shadow git 仓库 | 一手 |
| 手动压缩/交接 | `/smol`（=`/compact` 原地压缩）；`/newtask`（把 plan/已完成工作/相关文件/next steps 打包进全新任务窗口）；`/deep-planning`（调研后写 `implementation_plan.md` 并开新任务） | — | task 存储 | 一手 |
| Memory Bank（跨会话） | 6 个 markdown：projectbrief/productContext/activeContext/systemPatterns/techContext/progress；自定义指令强制 "I MUST read ALL memory bank files at the start of EVERY task"；"update memory bank" 触发全量复审 | — | 仓库 memory-bank/ | 一手 |
| 压缩前回滚 | 编辑摘要前的消息可类似 checkpoint 回滚到该点；checkpoints 可恢复压缩前状态 | — | — | 一手（镜像文档） |

#### 一手引用

- 截断阈值与保留策略（cline.bot/blog/understanding-the-new-context-window-progress-bar-in-cline）："maxAllowedSize = contextWindow - 40_000 // 160k usable tokens"（Claude）；`const truncatedMessages = [messages[0]]` "Always keeps the initial task message"；`messagesToRemove = Math.floor((messages.length - startOfRest) / 4) * 2` "By removing messages in pairs, we maintain the natural flow of user-assistant conversation patterns."
- 文件去重（cline.bot/blog/inside-clines-framework-for-optimizing-context...）："we've begun removing these older outdated file reads, leaving only the latest version of the file in context, while retaining the overall narrative integrity of the conversation. We've started with a very conservative approach... Our current approach prioritizes removing file redundancy first, maximizing cache hits."
- Auto Compact（docs.cline.net.cn/features/auto-compact，官方中文镜像）："创建对已发生事件的全面摘要 / 保留所有技术细节、代码更改和决策 / 用摘要替换对话历史记录 / 从他离开的地方继续工作"；"当使用其他模型时，即使在设置中启用了自动压缩，Cline 也会自动回退到标准的基于规则的上下文截断方法"；"您可以在 context-window-utils.ts 中查看如何确定阈值"。
- Focus Chain（cline.bot/blog/how-to-think-about-context-engineering-in-cline）："Cline generates a todo list at task start and reinjects it into context on a cadence so the thread does not drift. You can set the reminder interval in settings; the default is every 6 messages."；"With Focus Chain on, the todo list persists through summarizations so progress stays anchored."
- Checkpoints（docs.cline.bot/core-workflows/checkpoints）："Cline maintains a shadow Git repository separate from your project's actual Git history. After each tool use (file edits, commands, etc.), Cline commits the current state of your files to this shadow repo."
- `/newtask`（docs.cline.bot/core-workflows/using-commands）："It packages what matters (overall plan, work accomplished, relevant files, next steps) into a fresh task with a clean context window, leaving behind the noise of tool calls and implementation details."
- Memory Bank（docs.cline.bot/best-practices/memory-bank）：自定义指令原文 "I MUST read ALL memory bank files at the start of EVERY task - this is not optional."；更新时机："1. Discovering new project patterns 2. After implementing significant changes 3. When user requests with **update memory bank** (MUST review ALL files) 4. When context needs clarification."

---

### 1.6 Roo Code

> Cline 的 fork，上下文机制同源但有独立实现与配置。Memory Bank 在 Roo 生态是**社区模式**（非官方内置）。

#### 机制清单表

| 维度 | 机制 | 关键参数/阈值 | 持久化载体 | 来源级别 |
|---|---|---|---|---|
| Intelligent Context Condensing | LLM 摘要早期对话；默认开启；可手动 Condense Context 按钮 | 阈值滑杆默认 100%（如设 80% 则到容量 80% 触发） | 会话 | 一手 |
| 自定义压缩 prompt | Custom Context Condensing Prompt：用户写明必须保留什么（示例：保留完整错误栈、变量名及最后已知值、已尝试方案及结果） | — | settings | 一手 |
| 压缩审计 | 压缩后显示前后 token 数、本次压缩 AI 调用成本、可展开的摘要明细 | — | ContextCondenseRow | 一手 |
| 压缩穿透 | 首条消息中的 slash commands 跨压缩保留；Checkpoints 可回滚到压缩前原始消息 | — | checkpoints | 一手 |
| 按 profile 阈值 | 每个 API 配置 profile 可设不同 condensing 阈值（v3.21.3） | — | settings | 一手（release notes） |
| 超限自动恢复 | API 报上下文超限错误时自动截减 25% 并在重试限制内自动重试 | 25% | — | 二手（引官方文档的博客） |
| 窗口分配 | 会话历史 ~70% / 输出预留 ~20% / 安全 buffer ~10% | — | — | 二手（deepwiki 引文档） |
| Memory Bank | 社区仓库 roo-code-memory-bank：memory-bank/ 下 activeContext/decisionLog/productContext/progress（+可选 projectBrief/systemPatterns）；各 mode 实时更新；手动 "UMB"/"update memory bank" 兜底 | — | 仓库 markdown | 二手（社区仓库，非官方） |

#### 一手引用

- 阈值（docs.roocode.com/features/intelligent-context-condensing）："Threshold to trigger intelligent context condensing: A percentage slider (default 100%) that determines when condensing activates based on context window usage"；"Roo Code will attempt to condense the context automatically when the conversation reaches this level of capacity."
- 自定义 prompt（同上）："Modify the prompt used for condensing to better suit your workflow or emphasize what should be preserved." 示例："Always preserve error messages and stack traces in full / Maintain all variable names and their last known values / Keep track of all attempted solutions and their outcomes."
- 穿透与回滚（同上）："Slash commands included in the first message are preserved across condensations."；"original messages are preserved if you use Checkpoints to rewind."
- 审计（同上）："The context token counts before and after context condensing. The cost associated with the context condensing AI call. An expandable summary detailing what was condensed."
- 按 profile（docs.roocode.com/update-notes/v3.21.3）："You can now configure different intelligent context condensing thresholds for each of your API configuration profiles."

---

### 1.7 Letta / MemGPT

#### 机制清单表

| 维度 | 机制 | 关键参数/阈值 | 持久化载体 | 来源级别 |
|---|---|---|---|---|
| 分层记忆 | core memory（in-context blocks，pinned system prompt）/ archival memory（向量库）/ recall memory（全量消息库） | — | 数据库 | 一手（概念） |
| Core block 结构 | label / description / value / limit（字符上限）；默认 human + persona 两块；可 read-only；可多 agent 共享（shared blocks） | 默认每块约 2000 字符（二手）；建议总量 < 窗口 80%（二手） | 数据库 | 一手（结构）+ 二手（默认值） |
| 自我编辑工具 | `core_memory_append` / `core_memory_replace` / `memory_replace` / `memory_insert` / `memory_rethink` / `memory_apply_patch`（精确编辑：append、精确串替换、按行插入、patch） | 精确替换须唯一匹配 | 数据库 | 二手（源码引用） |
| 外置检索 | `archival_memory_insert` / `archival_memory_search`（语义检索）；`conversation_search` / `conversation_search_date`（按内容/时间查历史消息） | — | 向量库/消息库 | 一手（存在性）+ 二手（行为细节） |
| 驱逐即摘要 | 旧消息被 evict 出窗口时，与既有 summary 合并做**递归摘要**，压缩版留在上下文 | — | 数据库 | 二手 |
| 全量持久化 | "all state, includes memories, user messages, reasoning, tool calls, are all persisted in a database, so they are never lost, even once evicted from the context window" | — | 数据库 | 一手 |
| Sleep-time compute | 独立 sleep-time agent 在空闲时异步整理主 agent 的 core memory（抽象模式、消解矛盾） | — | 数据库 | 二手 |

#### 一手引用

- docs.letta.com/v1-sdk/concepts/stateful-agents："In Letta, all state, includes memories, user messages, reasoning, tool calls, are all persisted in a database, so they are never lost, even once evicted from the context window. Important 'core' memories are injected into the context window of the LLM, and the agent can modify its own memories through tools."
- 同上："Memory blocks can be attached and detached from agents - memory blocks that are attached to an agent are in-context (pinned to the system prompt). Memory blocks can be attached to multiple agents at once ('shared blocks')."
- 同上："The Letta API stores all messages, so even after a compaction / eviction, an agent's old messages are still retrievable via the API (for developers) and retrieval tools (for agents)."

#### 二手引用（标注）

- aiwiki.ai/wiki/letta（二手）："Core memory... has a size limit (the default per-block character limit is 2,000 characters)"；驱逐摘要："As messages are evicted they are passed through a recursive summarization step: the system generates a summary of the evicted messages together with any existing summary."
- neoneye.github.io/agent-memory-atlas/systems/letta（二手，引 Letta 源码路径）：`core_memory_append() / core_memory_replace() / memory_apply_patch()` 实现于 `letta/services/tool_executor/core_tool_executor.py`；guardrails："Read-only block check... Exact replace must match once. Patch hunk must match unique context. Prompt is rebuilt after memory changes."
- lobehub Letta skill（二手）："Keep total core memory under 80% of context window"；"archival memory... NOT automatically populated from context overflow"（ archival 不会自动承接溢出，须 agent 显式写入）。
- **风险注记**（二手 agent-memory-atlas）："Agent-controlled memory can silently encode wrong beliefs. No first-class verified/candidate/rejected trust layer."——自编辑记忆对弱模型执行者风险最高。

---

### 1.8 Pi

#### 机制清单表

| 维度 | 机制 | 关键参数/阈值 | 持久化载体 | 来源级别 |
|---|---|---|---|---|
| 自动压缩触发 | `contextTokens > contextWindow − reserveTokens` | `reserveTokens` 默认 16384；`keepRecentTokens` 默认 20000（保留最近 20K 原文） | `~/.pi/agent/settings.json` | 一手 |
| 检查时机 | 三处：工具批次结束后/新用户 prompt 前/run 结束后溢出恢复 | 溢出恢复仅一次 compact-and-retry | — | 一手 |
| 压缩算法 | LLM 结构化摘要五步：找切点（倒序累计到 keepRecentTokens）→ 收集消息 → 生成摘要（带前次摘要迭代）→ 追加 CompactionEntry → 重建上下文 | 切点规则：never cut at tool results | session 树 | 一手 |
| 摘要结构 | `## Goal / ## Constraints & Preferences / ## Progress (Done/In Progress/Blocked) / ## Key Decisions / ## Next Steps / ## Critical Context` + `<read-files>/<modified-files>` | file lists 跨压缩累积 | CompactionEntry.details | 一手 |
| 滚动迭代 | 重复压缩从上次的 `firstKeptEntryId` 起算，上次幸存的原文也参与下一轮摘要 | — | — | 一手 |
| 摘要前确定性处理 | 送摘要模型时 tool result 截断到 2000 字符（标记截断数）；`serializeConversation()` 转纯文本防"继续对话"混淆 | 2000 chars | 仅摘要请求 | 一手 |
| 持久化与回读 | append-only session 树；压缩只是追加 CompactionEntry，原始 entry 全部保留："Raw transcript history, exports, billing totals, and history-search extensions can still inspect the omitted attempt" | — | session 存储 | 一手 |
| 扩展点 | `session_before_compact` 事件可取消或提供自定义摘要（reason: manual/threshold/overflow）；`session_compact_failed` 遥测 | — | extensions | 一手 |
| 按模型覆盖 | `compaction.modelOverrides`（键 `provider/modelId`）；`reserveTokens` 同时影响摘要输出上限 | — | settings | 一手 |

#### 一手引用

- 触发（pi.dev/docs/latest/compaction）："Auto-compaction triggers when: `contextTokens > contextWindow - reserveTokens`"。
- 五步流程（同上）："Generate summary: Call LLM to summarize with structured format, passing the previous summary as iterative context when present."
- 切点（同上）："Valid cut points are: User messages, Assistant messages, BashExecution messages, Custom messages... Never cut at tool results (they must stay with their tool call)."
- 迭代（同上）："On repeated compactions, the summarized span starts at the previous compaction's kept boundary (`firstKeptEntryId`)... This preserves messages that survived the earlier compaction by including them in the next summarization pass as well."
- 截断（同上）："Tool results are truncated to 2000 characters during serialization... since tool results (especially from `read` and `bash`) are typically the largest contributors to context size."
- 回读（同上）："Omitted raw entries remain stored but do not affect cut selection, summaries, checkpoints, or token estimates."
- 失败（同上）："If recovery compaction fails or is cancelled, Pi keeps the omission edits, appends no compaction, and schedules no internal retry."

---

### 1.9 DeepSeek Harness（dsh）

> 开源 agent harness（github.com/deepseek-ai/deepseek-harness），everything-is-a-plugin。compaction 能力族拆成 5 个包，是本调研中**机制披露最完整、确定性最强**的实现。

#### 机制清单表

| 包 | 职责 | 关键参数/阈值 | 来源级别 |
|---|---|---|---|
| `compaction/` | 共享压缩契约（操作与摘要格式），`ctx.compaction` | — | 一手 |
| `compaction-basic/` | 自动压缩：压力阈值 `floor(min(W × 0.8, W − O − 65536))`；保留最近 16%（`W − O`）逐字；溢出恢复 | `thresholdRatio=0.8`、`headroomTokens=65536`、`retainRatio=0.16`、`maxTokens=65536`、`compactionRetries=1`、`maxOverflowRetries=1`、`modelPolicies[]`、`auto=true` | 一手 |
| `compaction-tool-result-pruner/` | 压缩前无模型调用的 tool result 裁剪 | `thresholdChars=8192`、`headChars=4096`、`tailChars=1024` | 一手 |
| `compaction-image-offload/` | 图像超预算时换占位符（监听 `agent/request-error`） | — | 一手 |
| `command-compact/` | `/compact` 手动压缩（低于阈值也可立即执行） | — | 一手 |
| `token-meter/`（llm 族） | 独立 token 计量服务，决定压力是否解除 | — | 一手 |

#### 关键机制细节（均为一手 README）

**触发与保留**（compaction-basic README）：
- "With context window `W`, effective request output cap `O`, and headroom `B`, the default trigger is `floor(min(W × 0.8, W − O − B))`, where `B = 65,536` tokens. Retention keeps the newest 16% of `W − O` verbatim."
- 先裁后摘："Trimming makes no model call and can remove the need to summarize at all: when the trimmed conversation fits within the threshold, condensation skips the summary. Trimming only runs after a condensation trigger qualifies — a below-pressure conversation is never touched."
- system prompt 豁免："a system prompt at surface node 0 is never shadowed."
- 溢出兜底："The `agent/request-error` listener reacts to a provider-confirmed `CONTEXT_WINDOW_EXCEEDED`: it bypasses the normal threshold and retention policy, attempts one maximal balanced head reduction, and authorizes a retry only after the surface replacement generation advances."

**摘要契约（8 节固定结构，顺序不可变）**（compaction-basic README）：
1. `## Primary Request and Intent`（用户原始与演化目标，关键措辞逐字引用）
2. `## Key Technical Concepts`
3. `## Files and Code`（精确路径、关键改动）
4. `## Errors and Fixes`（错误及解决、用户反馈）
5. `## Pending Jobs`（明确请求但未完成）
6. `## Current Work`
7. `## Next Step`（唯一下一步，无则写 "(none)"）
8. `## Critical Context`（决策及理由、约束、用户偏好、待解问题）

摘要指令原文："Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write '(none)' for an empty section — never drop a section."；"Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments."
滚动合并规则："If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure."
缓存友好："Replaying the system prompt... and the shadowed-region messages byte-for-byte makes the auxiliary call a genuine prefix of the conversation, so only the trailing instruction and the summary output are uncached."

**失败行为**（compaction-basic README）：
- 配置错误加载期 fail fast（未知字段、重复 per-model override、retain ≥ threshold 直接拒载）。
- "Summarization failure preserves the latest durable surface — before any replacement, the auto path logs a warning and proceeds with full over-budget history."
- "rejects a summary that does not shrink its source"（摘要没变小则拒绝）。
- "If nothing can be condensed safely — for example the whole conversation is one indivisible unit — nothing changes and nothing is written to the session log."

**Tool result pruner**（compaction-tool-result-pruner README）：
- "the defaults trim any result with more than 8,192 text characters to its first 4,096 plus its last 1,024, joined by the marker"（marker：`\n\n[... tool result middle pruned ...]\n\n`）。
- 确定性："Slicing is by Unicode code point with fixed budgets, so every emitted result has exactly the configured head, marker, and tail... and is strictly smaller than the triggering input."
- 原文不丢："The complete original result remains in the session log for exact replay and inspection."；替换通过 `sourceEventSeqs` 引用原事件。
- 保留结构："The replacement keeps the tool call, step, errors, and metadata — only the text content changes."
- 已知局限（README 自陈）："Pruning is syntactic — it retains the beginning and end without interpreting which middle lines are semantically important."；"Character budgets are not token budgets."

---

## 2. 四层分类法横向对比

> 用户四层分类：① 必须保留 / ② 可直接清理 / ③ 压缩为语义骨架（精确信息结构化另存）/ ④ 外置存储+引用。

### 2.1 ① 必须保留（当前目标/用户约束/已确认状态/未完成项/关键决策）

| 产品 | 保护机制 | 数据结构 | 保护方式 |
|---|---|---|---|
| Claude Code | CLAUDE.md 压缩后磁盘重注入；SessionStart(compact) hook 任意重注入；摘要契约含 intent/pending/current work | CLAUDE.md、MEMORY.md、settings hooks | 磁盘重读 + hook 注入 + 摘要字段 |
| Codex | 压缩模板含 constraints/user preferences/what remains；`/goal` 持久目标；博客四件套 Markdown | goal 状态、Prompt/Plan/Implement/Documentation.md | 模板字段 + 磁盘文件 |
| zcode | `/goal` 显式目标对象 | 会话 goal | 命令维护（细节未公开） |
| Cline | Focus Chain todo 穿透压缩 + 每 6 条消息重注入；首条任务消息永不截断 | todo markdown、messages[0] | 重注入 + 截断豁免 |
| Roo Code | 首条消息 slash commands 穿透；自定义 condense prompt 指定保留项 | 首条消息、settings prompt | 穿透 + 用户指定 |
| Letta | core memory blocks pinned 在 system prompt，驱逐/摘要永远动不到 | Block(label/value/limit) | 架构性豁免 |
| Pi | 摘要模板 `## Goal / ## Constraints & Preferences / ## Key Decisions / ## Progress / ## Next Steps / ## Critical Context` | CompactionEntry.summary | 模板字段 |
| DeepSeek Harness | 8 节契约中 Primary Request and Intent / Pending Jobs / Critical Context 强制保留，"(none)" 也不许删节 | `<compacted-summary>` 消息 | 硬模板契约 |
| Aider | 无专门机制 | — | — |

### 2.2 ② 可直接清理（重复检索结果/失效过程/无价值日志/重复 Tool Result）

| 产品 | 清理机制 | 确定性？ |
|---|---|---|
| DeepSeek Harness | tool-result-pruner：>8192 字符裁成 4096 头+1024 尾，原文留 session log | 完全确定性（Unicode code point 切片，无模型调用） |
| Cline | 同文件旧版本读取去重，只留最新版；截断时优先删"早期冗余对话/已完成工具输出/中间调试步骤/冗长解释" | 确定性 |
| Anthropic API | `clear_tool_uses_20250919`：100K 触发，最旧 tool result 换占位文本，keep 3 对，exclude_tools 豁免 | 确定性（服务端策略） |
| Claude Code | hook 输出 >10000 字符自动落盘，Claude 只见预览+路径 | 确定性 |
| Pi | 送摘要时 tool result 截断 2000 字符（只影响摘要输入，不动原始存储） | 确定性 |
| Codex | 压缩后仍超限 → TruncationPolicy 砍最旧消息/工具输出 | 确定性（兜底） |
| Roo Code | 超限错误时自动截减 25% 重试 | 确定性（二手） |
| Aider | 无自动裁剪；用户 /drop /clear 手动 | 手动 |
| zcode | 未找到公开证据 | — |

### 2.3 ③ 压缩为语义骨架（精确信息单独结构化保存）

| 产品 | 骨架（摘要） | 精确信息的结构化另存 |
|---|---|---|
| DeepSeek Harness | 8 节 Markdown 摘要 | 要求摘要内逐字保留 "exact file paths, commands, error strings, identifiers, numeric values"；pruner 原文留 log |
| Pi | 6 节摘要 | `<read-files>/<modified-files>` 清单独立存于 CompactionEntry.details 且跨压缩累积 |
| Claude Code | 摘要（intent/concepts/files+snippets/errors/pending/current） | CLAUDE.md/MEMORY.md 磁盘；压缩后重读 5 个最近文件 |
| Codex | handoff 摘要 | 模板要求 "critical data, examples, or references"；用户消息逐字保留（二手、待证） |
| Cline | 全面摘要 | Focus Chain todo 独立保留；checkpoints shadow git |
| Roo Code | 摘要（可自定义保留项） | checkpoints；自定义 prompt 指定精确信息 |
| Letta | 递归滚动摘要 | core blocks（精确事实）、archival（语义检索）、recall（全文可查） |
| Aider | 弱模型单段摘要（无结构契约） | repo map 每轮重算（精确符号签名）；git auto-commits |
| zcode | 未找到公开证据 | — |

### 2.4 ④ 外置存储 + 引用回读

| 产品 | 外置载体 | 回读机制 |
|---|---|---|
| Claude Code | hook 大输出落盘（>10000 字符）；CLAUDE.md `@path` 导入；子代理独立窗口 | 路径引用 `Referenced file`；按需读取；子代理只回摘要 |
| Pi | append-only session 树（全部原始 entry） | exports/history-search 扩展可回读被省略内容 |
| DeepSeek Harness | session log（append-only，`sourceEventSeqs` 引用） | "exact replay and inspection" |
| Letta | archival（向量库）+ recall（消息库），全量状态落库 | `archival_memory_search` / `conversation_search` 工具主动检索 |
| Anthropic API | memory files（memory_20250818） | memory tool 按需查找已清除内容 |
| Cline | shadow git checkpoints；Memory Bank markdown；任务历史 | Restore 三选项；新任务读 memory bank |
| Codex | rollout（含 Ghost snapshots）；博客四件套 Markdown | resume/fork；agent 重读文件 |
| Aider | `.aider.chat.history.md` | `--restore-chat-history` |
| zcode | 未找到公开证据（开源仓库可查，未核） | — |

---

## 3. 最终 Compaction 摘要保存什么字段（各产品对比）

| 产品 | 摘要字段/结构 | 结构强度 |
|---|---|---|
| DeepSeek Harness | 固定 8 节：Primary Request and Intent / Key Technical Concepts / Files and Code / Errors and Fixes / Pending Jobs / Current Work / Next Step / Critical Context；空节写 "(none)" 不许删；逐字保留精确标识 | ★★★ 硬契约（prompt 级强制） |
| Pi | 固定 6 节：Goal / Constraints & Preferences / Progress(Done/In Progress/Blocked) / Key Decisions / Next Steps / Critical Context + read-files/modified-files 清单 | ★★★ 硬契约 |
| Claude Code | 官方文档列举：requests and intent、key technical concepts、files examined/modified with code snippets、errors and fixes、pending tasks、current work；可用 `/compact <指令>` 或 CLAUDE.md 定制 | ★★ 文档级契约（prompt 未公开） |
| Codex | 模板要求：current progress and key decisions、context/constraints/user preferences、what remains (clear next steps)、critical data/examples/references | ★★ 模板级（二手转录） |
| Roo Code | 默认摘要 + 用户自定义保留指令（示例：错误栈/变量名/已试方案） | ★★ 用户可编程 |
| Cline | "全面摘要"保留技术决策/代码更改/项目状态；todo list 独立穿透 | ★★（todo 另算 ★★★） |
| Aider | 无结构契约：单段自由文本摘要（"I spoke to you previously..."） | ★ |
| Letta | 递归滚动摘要（无公开固定结构）；精确事实靠 core blocks 而非摘要 | ★（摘要）/ ★★★（blocks） |
| zcode | 未找到公开证据 | ? |

**结论**：「都做完仍超限时」的最终摘要，行业最佳实践 = **固定字段骨架 + 精确标识逐字保留 + 空字段显式占位 + 与上一轮摘要滚动合并（保真去陈）**，四点在 DeepSeek Harness 与 Pi 上均有完整一手实现证据，可直接照抄。

---

## 4. 可复用 vs 需自建清单

> 面向「执行者是低成本弱模型」的 PRD/ticket 输入。原则：弱模型不可靠的能力（写高质量摘要、自觉维护记忆、自觉不漂移）一律用确定性机制兜底。

### 4.1 可直接复用（有完整一手实现可照抄）

| # | 机制 | 出处 | 复用要点 |
|---|---|---|---|
| R1 | Tool result 确定性裁剪（头+标记+尾） | dsh `compaction-tool-result-pruner`（thresholdChars 8192 / head 4096 / tail 1024） | 无模型调用；原文进 append-only log；先裁后摘，裁完达标就跳过摘要 |
| R2 | 压缩触发公式 `min(W×0.8, W−O−B)` | dsh `compaction-basic` | 按模型 policy 覆盖；输出预留 + 安全余量分开算 |
| R3 | 8 节硬契约摘要模板 + 滚动合并规则 | dsh `compaction-basic` | 直接抄 prompt；含"不许删节、空节写 (none)、逐字保留精确标识、旧摘要保真去陈"四条规则 |
| R4 | 摘要请求字节级重放前缀（命中 KV cache） | dsh | 摘要调用成本最小化 |
| R5 | 压缩后从磁盘确定性重注入 | Claude Code（CLAUDE.md/plan/skills/5 个最近文件） | 弱模型不该靠摘要记住规则——规则文件压缩后重读注入 |
| R6 | SessionStart(compact) 式 hook 重注入 | Claude Code hooks | 每次压缩后注入 git log 摘要 + 当前 sprint 目标；确定性脚本产出，不依赖模型 |
| R7 | 受限写入的 JSON 任务清单（只许改 `passes` 布尔） | Anthropic 工程博客 feature_list.json | 用格式（JSON）+ 强措辞约束替代模型自觉；配 git 可审计 |
| R8 | todo list 穿透压缩 + 定节奏重注入（每 6 条消息） | Cline Focus Chain | 目标锚定成本最低实现 |
| R9 | 子代理独立窗口做脏活，只回摘要 | Claude Code sub-agents | 探索/搜索/日志分析全丢给子代理；主上下文只进摘要 |
| R10 | 压缩失败保留完整历史仅告警；摘要未变小则拒绝 | dsh | 失败哲学：宁可超预算也不丢数据 |
| R11 | 切点规则：永不在 tool result 处切（tool call/result 原子） | Pi | 保证剩余上下文自洽 |
| R12 | 溢出错误兜底：绕过阈值做一次最大幅度压缩 + 重试一次 | dsh（maxOverflowRetries=1）、Pi（一次 compact-and-retry）、Roo（截 25% 重试） | 双防线：主动阈值 + 被动恢复 |
| R13 | 会话制品三件套（progress 日志 + feature 清单 + init 脚本）+ 每 session 三步启动 | Anthropic 工程博客 | 跨会话/跨压缩重建状态的标准流程 |

### 4.2 需自建（无现成实现 / 需按本仓情况设计）

| # | 机制 | 原因 |
|---|---|---|
| B1 | 「保护事实」显式注册表（用户约束/精确标识/未完成项的结构化清单，压缩时强制注入摘要或独立保留） | 各产品只有近似物（Letta blocks 需模型自编辑、Claude hook 需用户写脚本、Roo 自定义 prompt 需用户写）；没有一个产品把它做成 harness 自动维护的一等公民 |
| B2 | 弱模型适用的摘要降级策略（弱模型摘要质量差：考虑模板化填空式摘要——harness 从对话中程序化提取文件列表/命令/错误串填入固定模板，模型只补「决策与下一步」） | 现有产品的摘要全靠模型生成全文；无产品做「程序化预填 + 模型补空」混合模式 |
| B3 | 压缩摘要的质量校验（如：摘要必须含 feature_list 中所有未完成项 ID，缺失则重生成或降级为纯裁剪） | 无产品有摘要完整性校验的公开证据 |
| B4 | 跨会话恢复协议（重启后读哪些文件、按什么顺序、如何验证工作区未损坏） | Anthropic 博客给的是 prompt 级约定，不是 harness 强制流程；需落成 initializer/迭代 agent 的确定性启动脚本 |
| B5 | Tool result 去重（同一文件多次读取只留最新版） | Cline 有但无公开算法细节；需自写（按 file path 建索引，旧读取替换为占位） |
| B6 | 用户消息逐字保留策略（压缩不吞用户原话） | Codex 有此设计但只有二手来源；阈值（20K）与回退需自定 |

### 4.3 不建议复制（对弱模型执行者）

| 机制 | 原因 |
|---|---|
| Letta 式 agent 自编辑 core memory | 弱模型会"silently encode wrong beliefs"（二手源码分析明示风险），且无信任分级 |
| Aider 式无结构自由摘要 | 无契约 = 漂移温床 |
| 依赖大窗口硬扛（1M window） | Claude Code/zcode 文档都显示 1M 窗口也只是把压缩推迟到 ~966K，机制一个不能少 |

---

## 5. 未决问题 / 未找到证据清单

| # | 问题 | 状态 |
|---|---|---|
| Q1 | Claude Code 压缩摘要的完整 prompt 与字段结构 | 只有文档级字段列举（context-window 页），prompt 全文未公开 |
| Q2 | Claude Code 是否有压缩前确定性 tool result 裁剪（microcompact） | 未找到公开证据；仅有 hook 大输出落盘（>10000 字符）与 API 层 clear_tool_uses |
| Q3 | Cline auto-compact 精确触发阈值 | 文档指向源码 `context-window-utils.ts`（未读源码核实）；中文镜像文档称约 80% |
| Q4 | Codex「用户消息逐字保留、20K 上限、尾部选取」 | 仅二手（wujiaming88 解读 + gist），未在官方文档核实 |
| Q5 | zcode 压缩摘要结构、裁剪、失败行为 | 无公开实现证据；开源仓库 zai-org/ZCode 可查但本次未深入源码 |
| Q6 | 「腾讯 zcode」产品 | 不存在公开证据；实际为智谱 ZCode，已在 1.3 节纠偏 |
| Q7 | Letta 递归摘要的结构契约与触发阈值 | 官方概念页只描述分层与持久化；细节仅有二手（aiwiki/源码分析）；官方 memory 指南页多次 404 |
| Q8 | Cline 文件读取去重的精确算法 | 官方博客只有行为描述（"leaving only the latest version"），无算法细节 |
| Q9 | Cursor / Gemini CLI / Amp / Devin | 可选低优先级，本次未覆盖 |
| Q10 | Roo Code「超限自动截减 25% 重试」与 70/20/10 窗口分配 | 仅二手（博客/deepwiki 转述官方文档），未直接读到官方原文 |
| Q11 | Pi 摘要模型的选择（是否可用更便宜模型做摘要） | 文档提到 modelOverrides 影响阈值，摘要模型本身是否可配未明示 |
| Q12 | DeepSeek Harness 摘要是否支持换更便宜模型 | README 显示 `summarizationProvider/summarizationModel` 可配（已确认支持），但换模型会失去前缀缓存复用 |

---

## 附：本次调研使用过的一手来源清单

- code.claude.com/docs/en/{memory, sub-agents, hooks-guide, costs, model-config, context-window, prompt-caching, agent-sdk/todo-tracking}
- anthropic.com/engineering/effective-harnesses-for-long-running-agents
- platform.claude.com/docs/en/docs/build-with-claude/context-editing
- developers.openai.com/blog/run-long-horizon-tasks-with-codex/
- learn.chatgpt.com/docs/{developer-commands, reference/slash-commands}
- github.com/openai/codex/issues/31860（引 codex-rs 源码）
- zcode.z.ai/en/docs/qa；zcode.z.ai/cn/newdocs/commands；zcode-ai.com/en/newdocs/agents；github.com/zai-org/ZCode
- aider.chat/docs/{repomap, usage/commands}.html
- cline.bot/blog/{understanding-the-new-context-window-progress-bar-in-cline, inside-clines-framework-for-optimizing-context-maintaining-narrative-integrity-and-enabling-smarter-ai, how-to-think-about-context-engineering-in-cline}
- docs.cline.bot/{core-workflows/checkpoints, core-workflows/using-commands, best-practices/memory-bank}；docs.cline.net.cn/features/auto-compact（官方中文镜像）
- docs.roocode.com/features/intelligent-context-condensing；docs.roocode.com/update-notes/v3.21.3
- docs.letta.com/v1-sdk/concepts/stateful-agents
- pi.dev/docs/latest/compaction
- github.com/deepseek-ai/deepseek-harness：README、packages/compaction/{README, compaction-basic/README, compaction-tool-result-pruner/README}
