# Pi 精读批次 A2：第 6–10 章章节卡片（对照上游 HEAD 核实）

- **上游 commit（实测）**：`1b347794e2a630e4359f2584f4eea388145d0ddf`（D:\reference\pi，HEAD -> main，grafted 浅克隆）
- **笔记基准**：D:\reference\dg-ai-notes\pi-agent\pi_source_dive\typescript\（作者基于上游 v0.80.2；A1 批次已确认 HEAD 为 v0.99.1，存在系统性版本漂移）
- **核实方法**：每条笔记声称先在本章卡片列出，再于 D:\reference\pi 用 grep 定位 + Read(offset/limit) 抽查实现，逐条给 file:line 证据；行号漂移不算偏差，机制/语义变化才标 ⚠️；全仓（`grep -r` over packages/，排除 node_modules）搜不到的标 ❓。
- **总统计**：第6章 ✅5 / ⚠️1 / ❓1；第7章 ✅8 / ⚠️1 / ❓1；第8章 ✅8 / ⚠️1 / ❓0；第9章 ✅9 / ⚠️2 / ❓0；第10章 ✅8 / ⚠️4 / ❓0。合计 ✅38 / ⚠️9 / ❓2。
- **总体结论**：五章的**架构级机制全部在 HEAD 存活**（双层消息+convertToLlm、两管道事件、四层上下文工程、Compaction 算法、append-only Session Tree）。⚠️ 集中在三类：(a) Message 联合类型已加入 SystemMessage（第6章"3 种"口径过时）；(b) agent-core 会话层已重构（SessionStorage→Storage、EntryType 收敛为 4 种，第10章）；(c) 若干实现细节演进（Promise.all 并行→顺序、延迟落盘触发点、assistant 消息参与 model 状态提取、分支摘要 maxTokens 2048→4096）。

---

## 第6章 消息系统 —— Agent 的记忆如何组织与传递

笔记文件：`第6章-消息系统-Agent的记忆如何组织与传递.md`

### 逐条核实

| # | 笔记声称 | 结论 | 上游证据（HEAD 1b34779） |
|---|---------|------|------------------------|
| 1 | LLM 认识的 `Message` 只有 3 种（User/Assistant/ToolResult），定义在 `packages/ai/src/types.ts:322-408` | ⚠️ 笔记偏差 | HEAD `packages/ai/src/types.ts:610`：`export type Message = SystemMessage \| UserMessage \| AssistantMessage \| ToolResultMessage;` —— **HEAD 已是 4 种，新增 SystemMessage**（:522 定义）。system prompt 以 system message 形式进入 transcript（agent/types.ts:382-389 "replayed from the transcript's system messages"）。笔记的"3 种"是 v0.80.x 口径 |
| 2 | `AgentMessage = Message \| CustomAgentMessages[keyof CustomAgentMessages]`（agent/types.ts:314） | ✅ | `packages/agent/src/types.ts:374` 逐字相同；CustomAgentMessages 空接口在 :365（注释含 declaration merging 示例 :351-364）。行号漂移属正常 |
| 3 | coding-agent 定义 4 种自定义消息（BashExecution/Custom/BranchSummary/CompactionSummary）+ `declare module` 声明合并 | ✅ | `packages/coding-agent/src/core/messages.ts:29,46,55,62` 四个 interface；:70-77 声明合并块逐字吻合 |
| 4 | BashExecutionMessage 有 `excludeFromContext` 字段（`!!` 前缀对 LLM 隐身、UI 照常渲染） | ✅ | messages.ts:38-39：`/** If true, this message is excluded from LLM context (!! prefix) */ excludeFromContext?: boolean;` |
| 5 | `convertToLlm` 规则表：bashExecution（exclude 过滤否则转 user）/custom→user/branchSummary、compactionSummary→user 加 `<summary>` XML；标准消息透传 | ✅ | messages.ts:148-196 完整对应：:152-161 bashExecution 过滤+转 user（`Ran \`..\`` 文本格式 :82-98）；:162-169 custom→user；:170-183 branch/compaction summary→user 包 `<summary>`（前缀常量 :11-24，"The conversation history before this point was compacted..."）；:184-188 user/assistant/toolResult/**system** 透传。⚠️ 顺带印证条目1：透传分支里已有 `case "system"` |
| 6 | 两阶段管道：先 transformContext（AgentMessage[]→AgentMessage[]）再 convertToLlm（→Message[]），在 `streamAssistantResponse` 内，笔记索引 agent-loop.ts:275-308 | ✅ | `packages/agent/src/agent-loop.ts:381-397`：`streamAssistantResponse` 内 :388-392 transformContext、:394-395 convertToLlm，注释原话 "This is where AgentMessage[] gets transformed to Message[] for the LLM" |
| 7 | "Web UI 就注册了自己的消息类型（user-with-attachments、artifact）" | ❓ 存疑 | 全仓 grep `user-with-attachments`（packages/ 全部 .ts/.tsx）零命中；`artifact` 仅出现为注释词（agent/types.ts:359 的声明合并示例）。无法在 HEAD 验证该说法，疑为笔记作者当时的某个示例/已删代码 |

补充 ✅：笔记"进阶细节"称 content 块有 `textSignature`/`thinkingSignature` 等 Provider 不透明签名（OpenAI/Google 上下文连续性）——`packages/ai/src/types.ts:398,404-406` 逐条吻合（406 注释：被安全过滤器编辑的内容密文存于 thinkingSignature）。

### 映射到我们的 SPEC

- **03 §2/§3/§4（Event Envelope / Event Vocabulary / Derive Messages）**：Pi 的"AgentMessage（内层丰富）→ convertToLlm（LLM 边界翻译）"与 SPEC 的 "events → derive_messages() → message history → ContextBuilder" 同构：持久化层存全量结构化事实，模型历史是投影。Pi 的自定义消息 role 分派 ≈ 我们 SessionEvent type 分派。
- **03 §4"Compaction Summary 是一种新的 Context 投影，不删除原 Event"**：Pi 的 CompactionSummaryMessage/BranchSummaryMessage 作为自定义消息驻留 messages 而原始 entry 不动，正是同一思想。
- **11（Streaming API / Web UI）**：UI 按 role 分派渲染、`excludeFromContext` 做"UI 可见 / LLM 不可见"，对应我们 Event 流同时供 UI 与 derive_messages 消费、"UI 不维护第二套 Session 真相"。

### 对 Python Core 项目可借鉴的设计点（以 HEAD 为准）

1. **"两个读者，两层格式"**：内部消息/event 用结构化富格式（含 details 字段给 UI），到模型边界一次性有损翻译（Pydantic 模型 + `to_llm_messages()` 单函数边界），永不提前拍扁。
2. **核心包留空扩展插槽 + 应用侧声明合并**：Python 等价物是 `Literal` 类型别名 + 泛型参数或 `TypedDict` 注册表（如 `CustomEventTypes`），core 不 import 应用类型但保持类型安全。
3. **翻译与 Provider 协议分层解耦**：convertToLlm（消息类型翻译）与 Provider 私有格式翻译（pi-ai 层）独立，换 Provider 不动前者——对应我们"模型适配层不碰 Session 语义"。
4. **`excludeFromContext` 布尔可见性开关**：一条数据三种可见级别（全可见/LLM 不可见/仅持久化），实现成本极低。

### 与冻结不变量可能冲突的点

- 无实质冲突；反向印证。唯一要小心：Pi 允许**自定义消息直接躺在 messages 数组里**参与持久化，我们的不变量是"Session 用 append-only typed SessionEvent、Event ≠ Diagnostic Log"——借鉴时应把"UI 专用渲染字段"放 SessionEvent payload/details（derive 时丢弃），而不是新造一种"不进模型但进历史"的第二个事件大类。

---

## 第7章 事件驱动 —— Agent 的神经系统

笔记文件：`第7章-事件驱动-Agent的神经系统.md`

### 逐条核实

| # | 笔记声称 | 结论 | 上游证据（HEAD 1b34779） |
|---|---------|------|------------------------|
| 1 | 内核 10 种 AgentEvent（agent_start/end、turn_start/end、message_start/update/end、tool_execution_start/update/end），4 层嵌套生命周期，定义于 agent/types.ts:422-437 | ✅ | `packages/agent/src/types.ts:514-529`：10 种逐字吻合（含字段），注释同样按 Agent/Turn/Message/Tool execution lifecycle 分组。行号漂移 |
| 2 | 两条管道：`session.subscribe`（listener 返回 void，Agent 同步 `_emit` 不等）vs 扩展 `pi.on`（Agent await 并读返回值）；分叉点在 `_handleAgentEvent`（先 await 扩展，再同步 `_emit`） | ✅ | `packages/coding-agent/src/core/agent-session.ts:1097-1099`：注释 "Emit to extensions first, then notify public listeners" → `await this._emitExtensionEvent(event)` 然后 `this._emit(...)`；`_emit` :998-1003 是同步 for 循环（不 await）；内核侧 `agent.ts:266 subscribe()` / :565 `processEvents` 对内核 listener 是 await 的（注意：内核 subscribe 与 Session 层 subscribe 签名不同，笔记讲的是 Session 层，准确） |
| 3 | 产品级 Session 事件：agent_settled、compaction_start/end、auto_retry_start/end、queue_update、session_info_changed、thinking_level_changed | ✅ | agent-session.ts:197（agent_settled）、:199（queue_update）、:203（compaction_start，reason: "manual"\|"threshold"\|"overflow"）、:205（session_info_changed）、:206（thinking_level_changed）、:215-216（auto_retry_start/end） |
| 4 | 扩展独占决策点：tool_call/tool_result/input/before_agent_start/context + provider 前后事件，触发位置在 agent-session.ts / sdk.ts 各 hook 上 | ✅ | HEAD 行号全部漂移但机制吻合：emitToolCall 调用 agent-session.ts:627、emitToolResult :649、emitInput :1842、emitBeforeAgentStart :1977；context 走 `sdk.ts:412-415`（`transformContext: async (messages) => ... runner.emitContext(messages)`）；before_provider_request/after_provider_response 走 `sdk.ts:361,363-367`（onPayload/onResponse 回调）。HEAD 还新增了笔记没提的独占事件：user_bash（runner.ts:1253）、emitBoundary、emitCacheWarmingDecision、emitMessageEnd、emitResourcesDiscover |
| 5 | `emitToolCall` 是唯一不包 try-catch 的派发方法——扩展抛错即 block，fail-closed | ✅ | `packages/coding-agent/src/core/extensions/runner.ts:1233-1251`：await + 读返回值 + `if (result.block) return result` 短路 + **无 try-catch**；对照 emitUserBash（:1253-1282）与 emit（:1080-1110）都有 try-catch。笔记逐行描述与 HEAD 代码一致 |
| 6 | 通知型 `emit()`：串行 await、try-catch 隔离、忽略返回值（session_before_* 例外读 cancel） | ✅ | runner.ts:1080-1110：try-catch 隔离逐 handler；`if (this.isSessionBeforeEvent(event) && handlerResult) ... if (result.cancel) return` |
| 7 | `pi.on` 实现就是往 `extension.handlers: Map<事件名, handler[]>` push | ✅ | `packages/coding-agent/src/core/extensions/loader.ts:271-284`：`on(event, handler)` 取 list/push/set back，另支持返回注销函数（HEAD 增强）；`handlers: new Map()` :583 |
| 8 | ExtensionContext：ui/mode/cwd/**sessionManager（Readonly，只读）**/modelRegistry/model/compact/getSystemPrompt/abort 等 | ✅ | `packages/coding-agent/src/core/extensions/types.ts`：`sessionManager: ReadonlySessionManager`（:335 区域）、`mode: ExtensionMode = "tui"|"rpc"|"json"|"print"`（:323 区域）、ui/cwd/modelRegistry/model 均在；写入口走 action 方法（setup 回调给全量 SessionManager :411） |
| 9 | "工具进度更新可以不等——先攒着，最后一次性等完"（高频 update 事件批量 drain） | ❓→⚠️ | HEAD 内核每事件全链路 await：agent-loop.ts 所有 `await emit(...)`（:117-287）、agent.ts `processEvents` :565-610 对每个事件逐 listener await、agent-session `_handleAgentEvent` 也是逐事件 await 扩展。**未找到事件级批量 drain**；真正的节流在**工具层**：bash.ts:278-293 OutputAccumulator 的 `updateTimer/lastUpdateAt/updateDirty` 对 partial 结果做时间节流。笔记引用的旧版 executePreparedToolCall:666-707 批量机制在 HEAD 已不可辨，只能按 ❓ 记（搜了 agent-loop emit 位点、agent.ts processEvents、agent-session _handleAgentEvent） |
| 10 | pi.on 有 30 个重载 | ✅（数字微漂） | `extensions/types.ts` 中 `on(event:` 32 处重载（HEAD 多了 user_bash 等新事件） |

### 映射到我们的 SPEC

- **03 §3（核心 Event Vocabulary）+ 11（SSE）**：Pi 的 AgentEvent 4 层嵌套（agent/turn/message/tool）与我们的 SessionEvent 分层同构；"产品级事件在 Session 层追加、内核不感知"对应我们"Event ≠ Diagnostic Log，Diagnostic 在另一层"。
- **04（Tool Runtime）+ 08（Plugin/Capability）**：管道 B 的 tool_call 拦截（fail-closed、block 短路）就是我们的 ToolExecutor 统一路径上的 Permission/Approval 决策点；Pi 用"扩展 handler 被 await"实现 runtime 权限，而不是 prompt——直接印证不变量 11"Sandbox/Permission 是 Runtime 边界"。
- **10（Multi-Agent）可参考**：ExtensionContext 的只读 sessionManager + 显式 action 写入，是"SubAgent/插件复用同一 Runtime 但权限收窄"的现成形态。

### 对 Python Core 项目可借鉴的设计点（以 HEAD 为准）

1. **双管道分野的一句话判据**："Agent 要不要读你的返回值"——要，await + 决策语义（block/transform）；不要，同步广播。Python 里即 `async def emit()`（await handlers，聚合返回）与 `def notify()`（schedule listener，不收集结果）两个方法，比单一 callback 列表干净。
2. **fail-closed 例外要显式**：默认 try-catch 隔离每个 handler（单插件崩不连累），唯独 tool_call 派发不隔离——错误即拒绝。值得写进我们 ToolExecutor 的异常语义。
3. **产品级事件与内核事件分层**（agent_settled vs agent_end：前者含重试/压缩/队列全部收尾，每 prompt 一次）：我们对应 `run/completed` 事件 vs 更细的 turn/message 事件，落库收尾挂可靠的那一个。
4. **扩展上下文给只读会话门面**（ReadonlySessionManager），写操作走显式 action——防止插件绕过 append-only。

### 与冻结不变量可能冲突的点

- Pi 的**内核级 `agent.subscribe` 是 await 的**（processEvents 逐 listener await）——若照搬，监听器慢 I/O 会拖住 Agent Loop。我们的口径：落库/审计走"不阻塞主循环"的通道（fire-and-forget 或队列），§14.10 前提下不让 Observability 故障拖垮 Core（不变量 21）。
- 扩展 `context` hook 能**整表替换发给 LLM 的 messages**（emitContext 链式）——与"Persistent History ≠ Runtime Context、完整保存 ≠ 完整注入"不冲突（它改的是 Runtime Context 投影），但实现时必须保证改写只影响投影、不回写 SessionEvent（Pi 用 structuredClone 隔离，runner.ts:1291）。

---

## 第8章 上下文工程 —— 让有限窗口装下无限对话

笔记文件：`第8章-上下文工程-让有限窗口装下无限对话.md`

### 逐条核实

| # | 笔记声称 | 结论 | 上游证据（HEAD 1b34779） |
|---|---------|------|------------------------|
| 1 | 截断常量：DEFAULT_MAX_LINES=2000、DEFAULT_MAX_BYTES=50*1024、GREP_MAX_LINE_LENGTH=500（truncate.ts:11-13） | ✅ | `packages/coding-agent/src/core/tools/truncate.ts:11-13` 逐字吻合（含 50KB 注释） |
| 2 | truncateHead/truncateTail/truncateLine + TruncationResult 元信息结构（truncated/truncatedBy/totalLines/lastLinePartial...） | ✅ | truncate.ts:15（TruncationResult）、:78（truncateHead）、:168（truncateTail）、:268（truncateLine，默认 GREP_MAX_LINE_LENGTH）；HEAD 新增笔记未提的 truncateMiddle :292 |
| 3 | `replaceUnpairedSurrogates` 仅存在于 agent 包（笔记标注 coding-agent 的 truncate.ts 简化未保留） | ✅ | coding-agent truncate.ts 无此函数；`packages/agent/src/harness/utils/truncate.ts` 有（笔记写 agent 包 truncate.ts:82，文件路径小漂移，"仅在 agent 包"的结论成立） |
| 4 | bash 工具描述 "Output is truncated to last 2000 lines or 50KB (whichever is hit first). If truncated, full output is saved to a temp file."；截断后追加 `[Showing lines x-y of z. Full output: /tmp/...]` 进 LLM 上下文 | ✅ | bash.ts:258（description，模板变量拼 DEFAULT_MAX_LINES/BYTES，措辞一致）；:351-355 三种提示文案（含 lastLinePartial 分支 `[Showing last ... of line ...]`）；流式侧 OutputAccumulator :277 存在（persistIfTruncated :291） |
| 5 | 项目上下文文件：AGENTS.md/CLAUDE.md（大小写都试）从 cwd 向上递归 + agentDir 全局，loadProjectContextFiles 在 resource-loader.ts:85-123 | ✅ | `packages/coding-agent/src/core/resource-loader.ts:185`（candidates 数组）+ :232（loadProjectContextFiles）。⚠️ 小增强：HEAD candidates 为 `["AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"]`——多了 AGENTS.override.md 最高优先 |
| 6 | buildSystemPrompt 用 XML `<project_context>`/`<project_instructions path="...">` 包装 | ✅ | `packages/coding-agent/src/core/system-prompt.ts:76`（`<project_instructions path="${path}">` 渲染）、:164（promptSections.project_context）、:195（buildSystemPrompt）。HEAD 已重构成 buildSystemPromptSections/State 两段式（:121/:186） |
| 7 | Skills 懒加载：formatSkillsForPrompt 只放清单 + "Use the read tool to load a skill's file when the task matches its description." | ✅ | `packages/coding-agent/src/core/skills.ts:355`（formatSkillsForPrompt，HEAD 多了 fileReadTool 参数）、:365（该句逐字在）、:369-380（`<available_skills>` XML） |
| 8 | 分支摘要：collectEntriesForBranchSummary（LCA）+ generateBranchSummary + 5-section prompt（无 Critical Context）+ preamble "The user explored a different conversation branch before returning here." | ✅ | coding-agent `core/compaction/branch-summarization.ts:108`（collectEntriesForBranchSummary）、:258-290（BRANCH_SUMMARY_PROMPT：Goal/Constraints & Preferences/Progress/Key Decisions/Next Steps 五节）、:293（generateBranchSummary）；agent 包 `harness/compaction/branch-summarization.ts:184`（preamble 逐字吻合）。笔记说同名文件两包并存——属实 |
| 9 | 分支摘要 maxTokens 写死 2048（branch-summarization.ts:234） | ⚠️ 部分过时 | agent 包版本仍 `createSummaryRequestOptions({ maxTokens: 2048 }, ...)`（:274）——笔记引用的就是 agent 包，仍准；但 **coding-agent 包 HEAD 改为 `Math.min(4096, model.maxTokens)`**（:345）。两包数值已分叉 |

### 映射到我们的 SPEC

- **06 §2（Context Builder）**：Pi 四层漏斗（工具输出截断→系统提示词组装→Compaction→分支摘要）正是 SPEC 拒绝的 `messages[-20:]` 反例的正向实现；Skills 懒加载对应 SPEC 组合式里的 "selected Skills"，XML 包装对应 "structured session summary" 的边界语义。
- **06 §3（Artifact Store）**：`[Full output: /tmp/...]` 逃生通道 = "大内容落盘、模型拿 summary+ref"不变量的最简工业形态（temp file + 路径 ref + 按需 read 工具），我们对应 ArtifactStore.save() → summary + artifact_ref。
- **03 §7（Fork）+ 06 §5**：分支摘要是"fork 到被放弃分支后信息不丢"的上下文侧补丁，对应我们 Fork 后 ContextBuilder 可注入 parent lineage 摘要。

### 对 Python Core 项目可借鉴的设计点（以 HEAD 为准）

1. **双重限制 + 双向策略截断**（2000 行/50KB 先触者胜；read 保头/bash 保尾/grep 限行）+ `TruncationResult` 元信息随行（truncatedBy 等）+ 末尾逃生行。四个部件都值得原样移植到我们的 ToolExecutor 输出整形。
2. **截断提示对模型诚实**（"不偷偷干"）：损失发生处告诉模型去哪取全量——与 Artifact 不变量天然契合。
3. **Skills 拉模式**：清单进 prompt（name/description/location 三行 XML），全文靠 read 工具按需拉——"工具调用 = 按需上下文加载"范式。
4. **UTF-8 边界安全**（逐字符字节预算 + 代理对整体处理 + 未配对代理替换）与"单行超限取该行末尾 + lastLinePartial 标志"两个边角，实现前就想好。
5. **系统提示词 = 分层规范手册**（全局→祖先→cwd，后覆盖前）+ XML 边界。

### 与冻结不变量可能冲突的点

- 基本无冲突，整体是"Context/大内容走 Artifact（summary+ref）"的正面示范。一点提醒：Pi 的截断默认参数（2000 行/50KB）是**工具描述的一部分**（契约），我们若把截断做成 Runtime 层可配置，须避免 SPEC 06 §5 的 thresholds 被 prompt 层私自覆盖——配置归属要单一。
- 分支摘要、Compaction 都是**派生数据写进会话树**（BranchSummaryEntry/CompactionEntry），符合"Compaction Summary 是投影、不删原 Event"；照搬时保持派生 entry 可从全量 Event 重算即可。

---

## 第9章 上下文压缩 —— 当对话太长怎么办

笔记文件：`第9章-上下文压缩-当对话太长怎么办.md`

### 逐条核实

| # | 笔记声称 | 结论 | 上游证据（HEAD 1b34779） |
|---|---------|------|------------------------|
| 1 | `shouldCompact`: `contextTokens > contextWindow - reserveTokens`（enabled 开关） | ✅ | `packages/coding-agent/src/core/compaction/compaction.ts:267-270` 与笔记伪码逐字相同 |
| 2 | estimateTokens = chars/4 启发式，按 role 累加各字段字符（compaction.ts:256-296） | ✅ | :294-298 注释 "Estimate token count for a message using chars/4 heuristic... conservative (overestimates)"；:298 起按 role switch。⚠️ 小增量：HEAD 计入 system 消息 sections（:302-307）与 image 固定 4800 chars（ESTIMATED_IMAGE_CHARS :276）——中文低估问题笔记的讨论仍适用（估计仍以 chars/4 为基） |
| 3 | findValidCutPoints：user/assistant 合法切点，toolResult 非法；注释 "When we cut at an assistant message with tool calls, its tool results follow it and will be kept." | ✅ | :390-394 函数注释逐字含该句（"Never cut at tool results (they must follow their tool call)"）；:394 findValidCutPoints。HEAD 用 isTurnStartEntry(:381) 判"turn 起点"，语义等价且更泛（custom 起始的 turn 也算） |
| 4 | findCutPoint 从后往前累积 token，≥keepRecentTokens(默认 20000) 停，取之后最近合法切点 | ✅ | :446-501：`keepRecentTokens: 20000` 默认值 :129；:458-479 向后走 + `cutPoints.find((candidate) => candidate >= i)`；:481-489 HEAD 新增"向回吞相邻元数据 entry"的收尾扫描（笔记未提，增强） |
| 5 | split turn：切点非 user 时向前找 turnStart，isSplitTurn = !isUser && turnStartIndex !== -1 | ✅（机制） | :491-499：`const startsTurn = isTurnStartEntry(cutEntry); const turnStartIndex = startsTurn ? -1 : findTurnStartIndex(...); isSplitTurn: !startsTurn && turnStartIndex !== -1`。⚠️ 笔记引用的旧代码 `cutEntry.message.role === "user"` 在 HEAD 已抽象为 isTurnStartEntry——结论不变（user 一定不是 split turn） |
| 6 | turnPrefix 机制：turnPrefixMessages 单独生成前缀摘要，TURN_PREFIX_SUMMARIZATION_PROMPT 3 段格式（Original Request / Early Progress / Context for Suffix） | ✅ | :778（turnPrefixMessages 字段）、:908（isSplitTurn 分支填充）、:942（TURN_PREFIX_SUMMARIZATION_PROMPT，"Later messages are stored separately and do not need to be reconstructed..."）、:1096（prompt 组装） |
| 7 | 主摘要与 turnPrefix 摘要用 `Promise.all` 并行生成（笔记引 L784-813） | ⚠️ 笔记偏差 | HEAD **无 Promise.all**（全文件 grep 零命中）；:998/:1036 主摘要 `await generateSummaryWithUsage(...)`、:1017 `await generateTurnPrefixSummary(...)`——**顺序生成**。HEAD 还引入了"先投影后压缩"的两轮结构（projectedEntries/findProjectedCutPoint :898） |
| 8 | SUMMARIZATION_PROMPT 6 固定 section（Goal/Constraints & Preferences/Progress(Done/In Progress/Blocked)/Key Decisions/Next Steps/Critical Context）+ UPDATE_SUMMARIZATION_PROMPT 增量更新 | ✅ | :507（SUMMARIZATION_PROMPT 六节逐一在，Progress 含 Blocked 子节）、:577（UPDATE_SUMMARIZATION_PROMPT，previous-summary 标签）、:718（`previousSummary ? UPDATE_ : SUMMARIZATION_`） |
| 9 | 文件跟踪：extractFileOperations 合并上次 details 与本次工具调用文件，formatFileOperations 输出 `<read-files>`/`<modified-files>` | ✅ | :60（extractFileOperations）、:917（调用处，传入 prevCompactionIndex 跨压缩累积）；`core/compaction/utils.ts:77-83`（formatFileOperations 两个 XML 节） |
| 10 | CompactionEntry：type:"compaction" + summary + tokensBefore + firstKeptEntryId + details（readFiles/modifiedFiles） | ✅ | `core/session-manager.ts:91-104`：字段全在（HEAD 另有 usage/fromHook/systemMessage 增强字段） |
| 11 | 压缩事件 compaction_start（reason: manual/threshold/overflow）/ compaction_end；自动压缩挂在 agent_end 处理后 | ✅ | agent-session.ts:203（逐字同枚举）、:208/:2793/:2806（compaction_end）；agent_settled 流程内触发（:3034 `_emit({type:"compaction_start", reason})`） |

### 映射到我们的 SPEC

- **06 §5（Compaction）**：逐点对应——"preserve tool interaction boundaries" = findValidCutPoints 排除 toolResult；"不能拆断 AI tool_call 与对应 ToolResult" = 同一约束的另一面；"structured summary（facts/decisions/constraints/failed_attempts/unresolved/artifact_refs...）" = 6-section 模板的超集方向；"Summary 记录 source range" = firstKeptEntryId + tokensBefore；"replace only runtime projection, persistent SessionEvent unchanged" = CompactionEntry 追加 + buildContextEntries 选择性收集。
- **03 §8（Compaction 与 Session）**："Fork 到 compaction 之前的历史节点仍应可解释" = 笔记"回退到 e4 之前，e1-e3 又作为正常消息出现"——压缩非破坏性。
- **06 §8（Failure Semantics）**：笔记未展开摘要 LLM 失败 fallback；SPEC 要求 deterministic fallback——Pi 的 generateSummary 失败路径（agent-session compaction_end 携 error :2800 区域）可作对照。

### 对 Python Core 项目可借鉴的设计点（以 HEAD 为准）

1. **"切点 = 保留区第一条"语义 + 合法切点白名单**（user/assistant 可切、toolResult 永不切）：一条规则同时满足协议配对与 token 精度（允许 assistant 切点换压缩精度，split turn 用第二份轻量前缀摘要补完整性）。
2. **向后累积 keepRecentTokens**（默认 20K）："先决定保护多少近期上下文，剩余全压缩"，比"删最旧 N 条"可验证。
3. **结构化 6-section 模板 + 增量更新 prompt**（previous-summary 标签）+ `<read-files>/<modified-files>` 领域文件账目跨压缩累积——对抗摘要漂移。
4. **chars/4 保守估算 + 预防性/应急双触发**（threshold + overflow recovery，`_overflowRecoveryAttempted` agent-session.ts:1078 区域）：估算不准时兜底而非崩溃。
5. **CompactionEntry 自带 firstKeptEntryId**：重建上下文时的"选择性收集"由 entry 自描述，重建逻辑无状态。

### 与冻结不变量可能冲突的点

- 无冲突，是 SPEC 06 §5 的同构实现。照搬时的两个守住点：(a) Pi 估算用 chars/4 而 SPEC 阈值是 `auto_compact_threshold=0.70`（比例制）——中文场景低估问题在我们这里更致命，Python Core 应优先用 Provider 返回的真实 usage 计数，chars/4 只做无 usage 时的 fallback；(b) `context`/`transformContext` 级联（第7章扩展可改写消息）发生在 Compaction **之后**的投影层，不得把扩展改写结果回写进 CompactionEntry。

---

## 第10章 会话管理 —— 对话的存储、恢复与分叉

笔记文件：`第10章-会话管理-对话的存储恢复与分叉.md`

### 逐条核实

| # | 笔记声称 | 结论 | 上游证据（HEAD 1b34779） |
|---|---------|------|------------------------|
| 1 | coding-agent 选本地 JSONL（每会话一个 .jsonl，按项目目录组织）；JSONL 行级追加契合 append-only | ✅ | `core/session-manager.ts:589-594`（getDefaultSessionDirPath：`<agentDir>/sessions/<编码后的 cwd>`）、:1172-1189 `_persist`（flushed 后 appendFileSync 追加一行）。行级追加属实 |
| 2 | agent-core 提供 `SessionStorage` 接口（harness/types.ts:440）+ JsonlSessionStorage（harness/session/jsonl-storage.ts）+ InMemorySessionStorage（memory-storage.ts），可插拔换数据库 | ⚠️ 笔记偏差（HEAD 已重构） | 三个旧名在 HEAD **全部消失**（全 agent 包 grep `SessionStorage|JsonlSessionStorage|InMemorySessionStorage` 零命中）。现结构：`packages/agent/src/harness/session/`（session.ts 定义 `StorageBackedSession implements Session` + 一组不变量错误类 SessionInvalidBranchError/SessionBranchExistsError/SessionPendingAssistantMessageError；jsonl/storage.ts:40 `class JsonlStorage implements Storage`；memory.ts；另有 fork.ts/fork-policy.ts/commit.ts/mutation-line.ts）。"可插拔存储"的**意图存活但形态全变** |
| 3 | coding-agent 的 SessionManager 是**独立实现**，不实现 agent-core 的 SessionStorage 接口，直接读写自己的 JSONL | ✅（仍成立） | `core/session-manager.ts:987` `export class SessionManager {`（无 implements 子句），自带 byId/leafId/fileEntries/_persist。⚠️ 但对照物已变：HEAD 的 agent-core 接口叫 `Storage`/`Session`（见条目2），"签名不兼容"的论述需按新接口重新成立 |
| 4 | Session Tree append-only：节点带 id/parentId/timestamp；"认父不认子"（父不维护 children，追加不改旧节点）；byId 映射表 + leafId 指针；branch() = 存在性检查 + `leafId = branchFromId` | ✅ | session-manager.ts:59-62（SessionEntryBase：type/id/parentId/timestamp）、:1191-1196（_appendEntry：fileEntries.push + byId.set + leafId=entry.id，三步与笔记一致）、:1579-1581（branch：`if (!this.byId.has(...)) throw` + `this.leafId = branchFromId`，与笔记代码逐字级吻合）。agent-core 侧 EntryBase（harness/session/types.ts:18-24）在 parentId 外**新增 seq 字段**（树形不变，加序号） |
| 5 | 9 种 Entry 类型按"对 LLM 的影响"分三组（进上下文 4 / 改状态 2 / 纯元数据 3）；agent-core 层 11 种 | ⚠️ 笔记偏差 | HEAD coding-agent `SessionEntry` 联合为 **11 种**（session-manager.ts:183-194：message/thinking_level_change/model_change/**usage**/compaction/branch_summary/custom/**custom_message**/**context_edit**/label/session_info——比笔记的 9 种多 usage 与 context_edit；笔记的三组分类法仍适用：usage/context_edit 归"元数据/投影修饰"组）。agent-core 侧从旧 11 种**收敛为 4 种** EntryType（harness/session/types.ts:16：message/compaction/branch_summary/custom），走 seq/lane 新模型 |
| 6 | buildSessionContext：leaf→root 路径遍历 + reverse；按类型分派（message→messages、model_change/thinking→状态覆盖、元数据跳过） | ✅ | :576-583（buildSessionContext → buildSessionProjection）、:548（buildSessionPath leaf→root）、:418-436（getSessionContextSettings 逐 entry 覆盖 thinkingLevel/model）、:439-466（sessionEntryToContextMessages 按类型投影）。HEAD 增加 ContextEditEntry 的投影修饰（projectContextEntry :519-540：对早前 entry 做内容替换/隐藏——append-only 的新形态"投影层编辑"） |
| 7 | CompactionEntry 特殊处理：按 firstKeptEntryId 选择性收集（compaction entry 本身 + firstKept 之后的 + compaction 之后的；被摘要的旧 entry 跳过不删） | ✅ | :468-512 buildContextEntries：doc 注释逐字对应（"the kept entries starting at firstKeptEntryId and all entries after the compaction entry. Older summarized entries are omitted"）；:555-565 buildSessionProjection 中只让最新一个 compaction 出 summary（index>0 的旧 compaction 消息置空）——HEAD 对多 compaction 路径的处理比笔记描述更精细 |
| 8 | model 状态：buildSessionContext 里 model 初始 null，无 model_change 时返回 null 由调用方兜底；**assistant 消息本身不携带"用哪个模型生成"，model 完全由 model_change 节点决定** | ⚠️ 后半被 HEAD 推翻 | :418-436 getSessionContextSettings：初始 `model: null` ✅；但 HEAD 循环里 **assistant 消息也更新 model**：`else if (entry.type === "message" && entry.message.role === "assistant") { model = { provider: entry.message.provider, modelId: entry.message.model }; }`（:431-433）。"assistant 不携带 model"的前提不再成立（AssistantMessage 自带 provider/model 字段）。覆盖式提取 ✅，"最后生效"语义 ✅ |
| 9 | branchWithSummary()：生成被弃分支摘要 + 追加 BranchSummaryEntry（parentId 指向分叉点） | ✅ | :1600-1622：参数校验 → `this.leafId = branchFromId` → 构造 `type:"branch_summary"` entry（parentId=branchFromId, fromId=旧 leaf）→ _appendEntry。BranchSummaryEntry 定义 :106-116 |
| 10 | 延迟写入：首次 assistant 消息到达前不落盘（防"有问无答"半截对话）；首次 flush 用 `openSync("wx")+writeFileSync` 全量重写，之后 appendFileSync；另有两种全量重写场景（分支副本/修复损坏文件） | ⚠️ 机制在、语义已变 | :1172-1189 `_persist`：`if (!this.flushed) { if (!this._hasConversation()) return; openSync(this.sessionFile,"wx") + 逐行 writeFileSync } else appendFileSync(...)`——四态结构与笔记一致。**但 `_hasConversation()`（:1166-1170）= user 或 assistant 消息即触发落盘**，注释明确引 #10000："Starting at the user message (not the first assistant reply) keeps the prompt on disk if the first turn never completes"。即 HEAD 是"setup-only 不建文件 + 首条用户消息即落盘"，笔记的"等首条 assistant 防孤问"动机已被有意推翻 |
| 11 | JSONL 细节：首行 header `type:"session"` 带 cwd/version；session id 用 UUIDv7；entry id 是 8 位短 UUID（randomUUID().slice(0,8)） | ✅ | :42-50（SessionHeader：type "session"/version/id/timestamp/cwd/parentSession，CURRENT_SESSION_VERSION=3 :42）、:265（uuidv7()）、:277-283（generateId：`randomUUID().slice(0, 8)`，冲突重试）；:293-319 含 v1/v2→v3 迁移逻辑 |
| 12 | createBranchSummaryMessage（session-manager.ts:397）/ createCompactionSummaryMessage（:403） | ✅（文件漂移） | 两函数现居 `core/messages.ts:100/:109`（签名一致：summary + fromId/tokensBefore + timestamp） |

### 映射到我们的 SPEC

- **03 §2/§3（Event Envelope / Vocabulary）**：SessionEntry 的 id/parentId/timestamp + type 判别 ≈ 我们的 SessionEvent envelope；"认父不认子"是我们 append-only typed SessionEvent 的树版论证（父维护 children 就要改旧节点）。HEAD 给 EntryBase 加 `seq`，与 SPEC "validate lineage / seq"（§5 Resume）直接同源。
- **03 §5/§7（Resume / Fork）**：buildSessionContext = Resume 的"load events → derive messages"一步；branch()/branchWithSummary() = Fork 的最简形态（move leaf vs new lineage）；SessionHeader.parentSession 支持会话级克隆（cloneSession）。SPEC Fork 要求"child 保存 parent identity + fork point"——parentSession 字段即雏形。
- **07 §1/§3（Storage 不是数据库 / Checkpoint）**：Pi "存在哪里（JSONL）× 长什么样（树）正交" + 接口可换后端 = SPEC §9 "SessionStore 抽象负责 append，JSONL/SQLite/PG Adapter"；Pi 的 `_hasConversation` 落盘门槛是会话级 durability 策略，不等价于我们的 Checkpoint（副作用恢复），不要混用。
- **11（Web UI 不维护第二套真相）**：UI 消费同一 JSONL/entry 流（getTree 返回防御性拷贝 :199-207），树视图 + label 均从 entry 派生。

### 对 Python Core 项目可借鉴的设计点（以 HEAD 为准）

1. **两个正交维度先拆后合**：存储介质（JSONL→可换 SessionStore Adapter）× 逻辑形态（append-only 树）；JSONL 行级追加 + 树 append-only 是同一原则在两个层的投影。
2. **"认父不认子 + byId 全局索引 + leafId 单指针"**：O(1) 追加/回退，回退=改指针，历史零删除；Python 用 `dict[str, SessionEvent]` + `leaf_id` 即可，children 查询走反扫。
3. **节点化状态变量**（model_change/thinking_level_change 存成 entry 而非全局字段）：回退天然正确——路径上没有的变更自动不生效。HEAD 的增强（assistant 消息同样更新 model）说明状态可由多种 entry 派生，但提取函数要单一（getSessionContextSettings 一处）。
4. **Compaction/分支摘要都是普通 entry**：派生数据与原始数据同构存储，重建逻辑（buildContextEntries）无状态可重放——SPEC "derive_messages() 可重算"的现成样板。
5. **HEAD 新增的 ContextEditEntry**（append-only 地修饰早前 entry 的投影：替换内容或整体隐藏）——"不删历史但允许投影层纠错"的正规化机制，对我们处理"敏感信息事后隐去/工具结果修正"很有价值。
6. **落盘门槛策略显式化 + openSync("wx") 原子首写**：首次落盘用独占创建防并发双写；HEAD 用 #10000 这类 issue 号在代码注释里锚定决策——我们应同样在 ADR/issue 锚定。

### 与冻结不变量可能冲突的点

- **Checkpoint ≠ 副作用恢复**：Pi 的"JSONL + leafId 回退"只恢复**对话状态**，不含工具副作用（无 Operation Ledger/reconcile 概念）。照搬 Pi 会话层时不得宣称它提供恢复语义——我们仍需 07 §4-§6 的 Operation Ledger + reconcile 作为独立层。
- **`_persist` 只在 user/assistant 出现后落盘**（HEAD 语义）：若我们照搬，必须先证明 setup/权限类 SessionEvent 丢失不影响恢复——否则与 07 §9 Resume 顺序冲突；稳妥做法是恢复关键事件（approval 授予、checkpoint）绕过该门槛立即落盘。
- **Pi 的 coding-agent SessionManager 与 agent-core 会话层是两套平行实现**（笔记 §七的核心观察，HEAD 更甚：两套 Entry 类型集都不同）。映射到我们："SessionEvent 是唯一真相"要求**一条**append-only 路径；借鉴 Pi 的分层时，Core 的 SessionStore 与 UI 用的投影应共享同一 entry 定义，不要复刻"接口存在但产品层另写一套"的分叉。
- **ContextEditEntry 允许隐藏早前消息**（replacement: null → 从模型上下文剔除）：只影响 Runtime Context 投影、原始 entry 保留——符合"完整保存 ≠ 完整注入"，但若允许它作用于**权限审批/工具调用事实**类事件，就会触碰"不允许删除原 tool interaction 事实"（03 §8）。实现时应对可编辑 content 白名单（Pi 也只允许 user/assistant/toolResult/custom 四种消息的内容被替换，见 :168-172 ContextEditableContent）。

---

## 附：核对中确认的 HEAD 新增机制（笔记未覆盖，供后续批次）

- agent-core 会话层重构：`harness/session/`（StorageBackedSession + Storage/JsonlStorage + seq/lanes + SessionInvariantError 族 + fork-policy/commit/mutation-line），v3 legacy 迁移在 `jsonl/legacy-v3.ts`。
- 扩展事件新增：user_bash、emitBoundary、emitCacheWarmingDecision、emitMessageEnd、emitResourcesDiscover（runner.ts:1020-1511）。
- coding-agent Entry 新增：usage、context_edit（session-manager.ts:80-89, :175-180）。
- 截断新增 truncateMiddle（truncate.ts:292）；项目上下文文件新增 AGENTS.override.md 优先级（resource-loader.ts:185）。
- bash.ts 截断描述改为模板变量拼接（:258），数值与笔记一致。
