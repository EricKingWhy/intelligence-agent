# Pi 精读批次 A1 —— 第 1–5 章章节卡片（对照上游源码逐条核实）

- **上游 commit（实测）**：`git -C D:\reference\pi rev-parse HEAD` → `1b347794e2a630e4359f2584f4eea388145d0ddf`（package 版本 **v0.99.1**）
- **核实方法**：只读本地克隆；笔记每条机制声称用 Grep/Read 定位上游实现，给出 file:line 证据；全部行号以 1b34779 为准。
- **关键前提**：笔记基于 **v0.80.2**，上游 HEAD 已到 **v0.99.1**，存在系统性版本漂移。凡机制仍在、行号/结构迁移的，标 ⚠️ 并写明差异；凡 HEAD 行为已改变的，单独标 ⚠️ 重点说明。
- **总体统计**：

| 章节 | ✅ 吻合 | ⚠️ 笔记偏差/版本漂移 | ❓ 存疑 |
| --- | --- | --- | --- |
| 第1章 总览 | 4 | 3 | 0 |
| 第2章 三层架构 | 4 | 4 | 0 |
| 第3章 Agent Loop | 6 | 4 | 0 |
| 第4章 模型调用 | 6 | 2 | 0 |
| 第5章 工具系统 | 9 | 2 | 0 |
| **合计** | **29** | **15** | **0** |

---

## 第 1 章：开篇 —— Pi-Agent 框架总览

### 笔记声称的核心机制与核实

**1.1 四个核心包（pi-ai / pi-agent-core / pi-tui / pi-coding-agent）构成三层堆栈 + 正交 UI 层；pi-tui 运行时只依赖 marked + get-east-asian-width；stream/complete 等全局 API 在 compat 子模块** —— ✅
- `packages/tui/package.json`：dependencies 仅 `get-east-asian-width@1.6.0`、`marked@18.0.11`；chalk/@xterm/headless 在 devDependencies。逐字吻合。
- `packages/ai/src/index.ts:3-7` 头注释："Core only, side-effect free: no generated catalogs, no provider factories, no api-registry, no OAuth implementations, no compat"——与笔记第 2 章引文逐字一致；stream/streamSimple 确在 `packages/ai/src/compat.ts:252-293`。
- `packages/coding-agent/package.json` dependencies 直接含 `pi-ai`、`pi-agent-core`、`pi-tui`（另有 chord/codemode/mcp，见 1.3）。

**1.2 内置工具 4 核心（read/write/edit/bash）+ 3 辅助（grep/find/ls）** —— ✅
- `packages/coding-agent/src/core/system-prompt.ts:58`：默认 selectedTools `[ "read", "bash", "edit", "write" ]`；`core/tools/` 目录含 grep.ts / find.ts / ls.ts。

**1.3 五个包（含实验性 pi-orchestrator）、"Pi 不构建 MCP"** —— ⚠️（HEAD 已变）
- HEAD `packages/` 实有 **13 个包**（agent, ai, chord, client, codemode, coding-agent, durable, evals, mcp, protocol, server, session-backends, telemetry, tui）；**无 orchestrator 包**（`ls packages | grep -i orch` exit 1）。
- **MCP 支持已存在**：`packages/mcp/package.json`："Standalone Model Context Protocol client for pi and other applications"，且 coding-agent 依赖 `@earendil-works/pi-mcp`。另有 durable（"Durable conversation, task, and document runtime"）、codemode（沙箱 JS 执行）、protocol/client（远程会话 CBOR）、session-backends/sqlite-node。笔记的"极简不做清单"是 v0.80.2 时代事实，引用时必须注明版本。

**1.4 models.json 由 ModelRegistry 在启动时自动读取；schema 含 providers/baseUrl/api/apiKey/models/contextWindow/maxTokens + modelOverrides + compat** —— ⚠️（机制吻合，落点文件漂移）
- HEAD 读取点：`packages/coding-agent/src/core/model-runtime.ts:218`：`join(getAgentDir(), "models.json")`；schema 在 `core/model-config.ts:203-243`（ProviderCompatSchema / modelOverrides / providers Record）。笔记指 `model-registry.ts#L367` 与 `:158-218`——`ModelRegistry` 仍在（model-registry.ts:43）但 models.json 的加载/校验已拆到 model-runtime + model-config。

**1.5 SDK 与运行模式：createAgentSession()；交互 / print / RPC / SDK 四模式** —— ✅
- `packages/coding-agent/src/index.ts:257` 导出 `createAgentSession`（定义于 `core/sdk.ts`）；`src/modes/` 目录：interactive / print-mode / rpc / json-event，四模式吻合。

**1.6 树状会话（id/parentId DAG）、SYSTEM.md 可整体替换系统提示词、扩展热重载、默认不经审批直接执行** —— ✅（树/SYSTEM.md/热重载 ✅；"YOLO"为产品称呼，机制证据如下）
- `core/session-manager.ts:182`（"Session entry - has id/parentId for tree structure"）、:286-298（v1→v2 迁移加 parentId）。
- `core/resource-loader.ts:1202-1216`：project `<cwd>/.pi/SYSTEM.md`、global `<agentDir>/SYSTEM.md`、APPEND_SYSTEM.md。
- 热重载：`core/extensions/loader.ts:196`（ctx.reload() 语义）。
- 默认直接执行：`packages/agent/src/agent.ts:241`（`beforeToolCall` 默认 undefined）——AgentLoop 仅在配置了钩子时拦截（agent-loop.ts:727），core 内无 permission/approval/yolo 字符串。

### 映射到我们的 SPEC

| Pi 机制 | SPEC 模块 |
| --- | --- |
| 包分层 / 每层可独立使用 | SPEC_ROOT/01_SYSTEM_ARCHITECTURE.md（整体分层）；Core Python / Async-first |
| models.json（第三方 provider 显式声明 baseUrl/api/model） | 02_AGENT_RUNTIME.md §3 ModelProvider（OpenAI-compatible / Qwen / DeepSeek Adapter） |
| 树状会话 id/parentId | 03_SESSION_EVENT_MODEL.md §7 Fork、§5 Resume |
| 4 运行模式（print/RPC/SDK） | 11_STREAMING_API_WEB_UI.md（SSE/Web UI 之外的多入口形态参考） |
| SYSTEM.md / 扩展 / 技能 | 09_MCP_SKILLS_KNOWLEDGE_WEB.md（Skills/Knowledge 作为可加损能力） |

### 对 Python Core 项目可借鉴的设计点

- models.json 的显式 provider 声明 schema（baseUrl + api 协议名 + contextWindow/maxTokens + compat 兼容字段）是接国产模型的最小充分配置面，可直接映射到我们 ModelProvider 目录的配置层。
- "默认零拦截 + 钩子点预留"（beforeToolCall 默认 undefined）与我们的红线相反（我们要求 runtime 强制权限），但**钩子位置本身**（参数验证后、execute 前）是正确的 runtime 权限拦截点。
- 会话条目 id/parentId 让 Fork 天然成为数据结构操作而非复制粘贴。

### 与冻结不变量可能冲突的点

- "Pi 不做权限/沙箱，安全靠容器化 + 扩展" 与我们 **"Sandbox/Permission 是 runtime 边界，不靠 prompt"** 相反——借鉴 Pi 时必须保留我们 04 §8 的 Permission/Risk 与 Operation Ledger，不能把权限降级为可选扩展。
- Pi HEAD 已自带 mcp/durable/codemode 等外围包——对照我们 "MCP Tool 不能绕过统一 ToolExecutor"：Pi 的 mcp 包是独立 client，工具注册走 ToolDefinition→AgentTool 包装链（见第 5 章），路径统一性靠 wrapToolDefinition 保证，我们复用其思路时必须落在唯一 ToolExecutor 之后。

---

## 第 2 章：三层架构 —— 项目的骨骼

### 笔记声称的核心机制与核实

**2.1 分层规则 = 依赖方向单向向上；pi-ai 不依赖任何上层；agent-core 只依赖 pi-ai；coding-agent 依赖下两层** —— ✅（附一处小偏差）
- `packages/agent/package.json`：依赖 `@earendil-works/pi-ai`（+typebox/telemetry/chord），无 coding-agent/tui。
- ⚠️ 小偏差：笔记说 pi-ai "不依赖任何 pi-xxx 包"——HEAD 的 ai 依赖 `@earendil-works/pi-telemetry`（兄弟 workspace 包，纯遥测类型），不是 Agent/UI 层，方向仍单向。

**2.2 agent-core 的类型从 pi-ai 导入基础原子（types.ts 顶部 import）** —— ✅
- `packages/agent/src/types.ts:1-16`：import Api/AssistantMessage/Context/Message/Model/Tool/ToolResultMessage 等 from "@earendil-works/pi-ai"。

**2.3 类型递进扩展：Tool（名片）→ AgentTool（+execute/executionMode，继承）→ ToolDefinition（+渲染/提示词注入，独立 interface 结构兼容而非 extends）；ToolDefinition.execute 多一个 ctx 参数** —— ✅
- `packages/ai/src/types.ts:715-720`：Tool = name/description/parameters（HEAD 另加可选 constrainedSampling）。
- `packages/agent/src/types.ts:464-497`：AgentTool extends Tool，含 label/prepareArguments/execute/executionMode（HEAD 另加 outputSchema、replay 两个新字段——比笔记多 2 个字段）。
- `packages/coding-agent/src/core/extensions/types.ts:560-636`：ToolDefinition 独立 interface（非 extends），execute 签名第 5 参 `ctx: ExtensionToolContext`，含 promptSnippet/promptGuidelines/renderShell/prepareArguments/executionMode/renderCall/renderResult。与笔记描述完全一致（行号 435→560 漂移）。

**2.4 AgentMessage = Message | CustomAgentMessages（联合类型扩展，不改底层）；Extension 用 Map 存 handlers/tools/commands** —— ✅
- `packages/agent/src/types.ts:365-374`（CustomAgentMessages declaration merging + AgentMessage 联合）。
- `extensions/types.ts:2205-2216`：Extension{path, resolvedPath, sourceInfo, handlers: Map, tools: Map, messageRenderers: Map, commands: Map, flags: Map, shortcuts: Map}——与笔记逐字段吻合。

**2.5 Message 只有三种标准消息（User/Assistant/ToolResult）** —— ⚠️（HEAD 已变）
- `packages/ai/src/types.ts:610`：`Message = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage`——**SystemMessage 已入联合**。这是 v0.99 的 transcript 架构：系统提示词与工具声明由 transcript 的 leading system message 携带（types.ts:726-749 的 Context/TranscriptContext 注释写明 "systemPrompt and tools are shorthand for a leading system message"）。

**2.6 KnownApi 9 种 API 类型** —— ⚠️
- `packages/ai/src/types.ts:17-27`：KnownApi 实为 **10 种**（openai-completions / mistral-conversations / openai-responses / azure-openai-responses / openai-codex-responses / anthropic-messages / bedrock-converse-stream / google-generative-ai / google-vertex / pi-messages）。

**2.7 pi-orchestrator 是第五个包（实验性外围编排层，依赖 coding-agent）** —— ⚠️
- HEAD 无此包（见 1.3）。笔记关于"编排在外围、不进内核"的定位判断本身与我们 LangGraph 观一致，但对象已不存在。

**2.8 ai/index.ts 头注释与 agent/index.ts 导出（agent/agent-loop/harness session/compaction）** —— ✅
- 见 1.1 引文逐字吻合；`packages/agent/src/index.ts` 导出 agent.ts、agent-loop.ts、harness/compaction/compaction.ts（compact/shouldCompact 等）、harness/session/index.ts。

### 映射到我们的 SPEC

| Pi 机制 | SPEC 模块 |
| --- | --- |
| 依赖漏斗（底层不知道上层） | 01_SYSTEM_ARCHITECTURE.md；不变量 #21（可选层故障不拖垮 Core） |
| 类型递进 Tool→AgentTool→ToolDefinition | 04_TOOL_RUNTIME.md §2 Tool Contract（模型侧定义与 Runtime Tool 同源） |
| AgentMessage 联合扩展 | 03_SESSION_EVENT_MODEL.md §3 核心 Event Vocabulary（typed、可扩展、不改基类） |
| compaction 在 agent-core | 03 §8 Compaction 与 Session；06_CONTEXT_ARTIFACT_MEMORY |

### 对 Python Core 项目可借鉴的设计点

- "每层只加自己关心的字段"的三层 Tool 类型：Pydantic 侧可用基类 ToolSpec(name/description/args_schema) → RuntimeTool(+execute/timeout/risk) → ProductToolDefinition(+render/promptSnippet) 复刻，避免一个大而全 Contract。
- 底层类型不可变、上层联合扩展（AgentMessage = Message | Custom）对应我们 typed SessionEvent 的可扩展 vocabulary 思路。
- "去掉上层还能跑"作为分层回归测试（笔记方法 3）值得进我们 CI。

### 与冻结不变量可能冲突的点

- 无直接冲突。注意：Pi 把 compaction 放在 agent-core（harness/compaction），我们对应能力（06 Context/Artifact/Memory）按不变量 #16 必须以 Capability + Context Provider 形态接入，不能焊进 Agent Loop 主体——Pi 的 transformContext 钩子（见第 3 章）恰好是"外挂"形态，反而是正面参考。

---

## 第 3 章：Agent Loop —— 让模型转动起来的引擎

### 笔记声称的核心机制与核实

**3.1 Trace/Turn 模型：一个 Turn = 一次模型调用 + 该次触发的整批工具执行，由 turn_start/turn_end 包裹；agent_start 在入口发，首轮 turn_start 也在入口发** —— ✅
- `packages/agent/src/agent-loop.ts:117-122`：runAgentLoop 入口 emit agent_start → turn_start → 每条 prompt message_start/message_end；`packages/agent/src/types.ts:514-529` AgentEvent 联合（agent_start/agent_end/turn_start/turn_end/message_start/message_update/message_end/tool_execution_start/update/end）。

**3.2 stopReason 五种：toolUse/stop/length 来自模型 API；error/aborted 由流式层 catch 注入** —— ✅
- `packages/ai/src/types.ts:767-783`：done reason = stop|length|toolUse（**HEAD 另有 `deferred`** ⚠️ 小偏差），error reason = aborted|error。
- "永不抛出"契约成文：`packages/agent/src/types.ts:27-32`（StreamFn contract："Failures must be encoded in the returned stream ... stopReason 'error' or 'aborted' and errorMessage"）、`packages/ai/src/types.ts:362-370`。

**3.3 真正驱动循环的不是 stopReason=="toolUse"，而是 `toolCalls.length > 0 && !terminate`；terminate 是 every 不是 some** —— ✅
- `agent-loop.ts:259-278`：filter toolCall → executeToolCalls → `hasMoreToolCalls = !executedToolBatch.terminate`。
- `agent-loop.ts:689-691`：`shouldTerminateToolBatch = finalizedCalls.length > 0 && finalizedCalls.every(r => r.result.terminate === true)`。

**3.4 "即使 stopReason==length，只要 content 里有 toolCall，循环仍会执行工具"** —— ⚠️（HEAD 行为已反转，重要）
- `agent-loop.ts:263-270` + `:478-503`：HEAD 对 length 截断消息**一律不执行**工具——`failToolCallsFromTruncatedMessage` 给每个 toolCall 生成错误结果（"arguments may be truncated. Re-issue the tool call"），流式参数经 JSON salvage 解析后可能"看似合法实则残缺"，故全部判错让模型重发。比笔记版本更保守，且语义更安全。

**3.5 双层循环：内层 `while (hasMoreToolCalls || pendingMessages.length > 0)`，steering 每圈头尾检查（进循环前先查一次）；外层 followUp 在内层全部结束后续命，同一 Trace 内重启内层** —— ✅
- `agent-loop.ts:175-176`（进循环前 steering 首查，注释 "user may have typed while waiting"）、:179（外层 while true）、:183（内层条件逐字吻合）、:295（圈尾再查 steering）、:301-308（followUp → pendingMessages → continue）。
- ⚠️ 小偏差：笔记"firstTurn 标志跳过首轮 turn_start"——HEAD 无此标志，改由 `lastCompletedTurn` 是否已设置决定（:183-208，首个内层圈不发 turn_start），功能等价。

**3.6 error/aborted 硬停止：发 turn_end + agent_end 后直接 return，不检查 followUp** —— ✅
- `agent-loop.ts:245-256`（HEAD 在此之前多调一次 `config.finishTurn`，其余一致）。

**3.7 退出路径之"外部钩子停"由 shouldStopAfterTurn 承担；prepareNextTurn 可换 model/context/thinkingLevel** —— ⚠️（钩子已重构）
- HEAD 无 `shouldStopAfterTurn`：改为 **`finishTurn`**（`packages/agent/src/types.ts:146-158, 256-264`）返回 `{action:"end"}` / `{action:"continue"}`，在 agent-loop.ts:286-298 消费；另有新钩子 **`prepareRequest`**（每次 provider 请求前，types.ts:182-189）。prepareNextTurn 仍在（agent-loop.ts:184-200，可覆盖 context/model/thinkingLevel，HEAD 还可追加 messages）。

**3.8 streamAssistantResponse 四阶段（transformContext → convertToLlm → 构建 Context → 流式）；流式"先 push 空壳、逐事件原地替换最后一条、done 时替换为最终消息"** —— ✅（构建 Context 一项 ⚠️）
- `agent-loop.ts:381-407`：transformContext、convertToLlm、resolveApiKey（getApiKey 动态解析，与笔记一致）、streamFunction 调用。
- `:411-458`：start → push 空壳 + message_start；delta 族 → `context.messages[last] = partialMessage` + message_update；done/error → 最终消息替换 + message_end。逐字吻合。
- ⚠️ 笔记说 llmContext = `{systemPrompt, messages, tools}` 每圈重建：HEAD 是 `normalizeContext({ messages: llmMessages })`（:397）——系统提示词与工具声明已由 transcript system message 携带（types.ts:22-25 注释："the system prompt and tool declarations are carried by the transcript's system messages, never by context.systemPrompt or context.tools"）。笔记的 cache 论证（system/tools 字节稳定 → prefix 命中）结论仍成立，但论据对象变了。
- ⚠️ 默认 convertToLlm：HEAD 保留 system|user|assistant|toolResult 四种（agent.ts:38-46），笔记说只保留三种。

**3.9 Anthropic prompt cache 三处打点：system 末尾 / 最后一个 tool / 最后一条 user message（rolling）** —— ✅（行号漂移）
- `packages/ai/src/api/anthropic-messages.ts:1093-1108`（system 块 cache_control）、:1497（`index === tools.length - 1` 打在最后一个 tool）、:1407-1434（"Add cache_control to the last user or system message to cache conversation history"，rolling；HEAD 的回退角色含 system）。

**3.10 工具批执行：一票否决（任一 sequential 则整批串行）；并行三阶段=顺序准备（验证+beforeHook）→ 并行 execute → 结果按调用顺序回填** —— ✅
- `agent-loop.ts:508-523`（some → sequential 判定）、:586-660（并行：准备循环逐个 await → thunk 进 Promise.all → `orderedFinalizedCalls` 按调用顺序 emitToolResultMessage）；`packages/agent/src/types.ts:39-46` 注释明说 "tool_execution_end in completion order, tool-result message artifacts in assistant source order"。

### 映射到我们的 SPEC

| Pi 机制 | SPEC 模块 |
| --- | --- |
| Loop 主干（调模型→工具→结果回填→终判） | 02_AGENT_RUNTIME.md §2 主循环（几乎同构） |
| ModelDelta 流式 + 聚合完整 AssistantMessage | 02 §4 Streaming（原地替换 ≈ 我们"聚合完整 AIMessage 承担 tool_calls/SessionEvent/Checkpoint"） |
| stopReason 枚举 + error/aborted 编码进流 | 02 §8 Error Semantics（不要自由文本推断异常类型） |
| prepareNextTurn / finishTurn 钩子 | 02 §5.4 CompletionPolicy 可插拔、§7 Model Fallback 边界 |
| steering / followUp 队列 | 03 §2 Event Envelope 之外的运行时注 入机制（对应我们 interrupt/steering 语义设计） |
| Anthropic rolling cache | 12_OBSERVABILITY_EVALUATION.md / 成本优化议题（Spec 内未冻结，属实现优化） |

### 对 Python Core 项目可借鉴的设计点

- "驱动循环的是 content 里有无 toolCall + terminate，而非 stopReason 字符串"——比按 stopReason 分支更健壮，Python Loop 应照抄这个判据（配合 HEAD 的 length-截断全判错防线）。
- 双层循环 + 两个消息队列（steering 插队 / followUp 排队）+ 全部经 `getSteeringMessages()` 函数拉取（Loop 与队列实现解耦），是我们 Session 注入用户打断消息的现成蓝本。
- 流式原地替换（空壳→partial 覆盖 last→final 覆盖）正好实现我们 02 §4 的"流式聚合完整 AIMessage"，Python 侧用"占位 AIMessage + 就地更新"即可，同时保持 append-only 的是**事件流**而非内存列表。
- prompt cache 三打点策略（稳定前缀 + rolling 断点）可移植到 Anthropic Adapter。

### 与冻结不变量可能冲突的点

- **原地替换发生在 `context.messages` 上**：Pi 的运行时 transcript 是可变列表（Agent.processEvents 也直接 push，agent.ts:577）。对照不变量 "Session 使用 append-only typed SessionEvent"——不冲突的前提是像 Pi 一样把"运行时 transcript（可变）"与"持久 Session（append-only entries + parentId 树，session-manager.ts）"分成两层；我们移植时必须保证可变只发生在 Runtime Context 层，SessionEvent 流始终 append-only。
- finishTurn/prepareNextTurn 允许中途换模型：与我们 "Model Fallback 只在 Provider 调用域触发"（02 §7）边界不同——Pi 这是**产品层显式换模型**（非故障 fallback），借鉴时须保证它不与故障 Fallback 共用一条路径，且每次换模型落审计记录。

---

## 第 4 章：模型调用 —— 一行代码驾驭多个模型

### 笔记声称的核心机制与核实

**4.1 三层架构：统一入口（查表派活）→ 事件协议（统一事件流）→ 翻译器（每 API 一个适配器）；registerApiProvider 注册、resolveApiProvider 查表** —— ✅（行号漂移）
- `packages/ai/src/compat.ts:102`（apiProviderRegistry Map）、:128-140（registerApiProvider）、:180-191（BUILTIN_APIS 10 项，与笔记"4 个列出 + 还有 5 个"≈9 略差 1）、:244-250（resolveApiProvider）、:252-267（stream）、:278-293（streamSimple）。

**4.2 事件协议 12 种 AssistantMessageEvent；每事件携带 partial 完整快照（原地替换的来源）** —— ✅
- `packages/ai/src/types.ts:767-783`：start / text_start-delta-end / thinking_start-delta-end / toolcall_start-delta-end / done / error，恰好 12 种，全部带 partial。⚠️ 小注：done 的 reason 枚举 HEAD 多了 `deferred`。

**4.3 StreamFunction 是"宪法"：输入相同（model/context/options）、输出相同（AssistantMessageEventStream）、错误不抛异常而是编码进流** —— ✅
- `packages/ai/src/types.ts:371-375` 签名逐字吻合；⚠️ 小注：context 参数现为 branded `TranscriptContext`（:738-749，只能由 normalizeContext 产生），比笔记的裸 Context 更严格。

**4.4 streamSimple 是便捷层：自动做思考级别翻译（查表 + clamp + 调整 maxTokens）** —— ✅（落点说明）
- HEAD 的 compat.streamSimple 是纯转发；思考级别翻译在各 provider 的 streamSimple 实现内：`api/anthropic-messages.ts:866-900`（mapThinkingLevelToEffort）、`api/openai-completions.ts:731`。笔记的"帮你翻译"结论成立，位置在翻译器内部而非 compat。

**4.5 ThinkingLevel 统一枚举 + Model.thinkingLevelMap 翻译表 + clampThinkingLevel 先向上后向下回退** —— ✅ / ⚠️（级别数漂移）
- clamp：`packages/ai/src/models.ts:1228-1247`——先 `i = requestedIndex → length`（向上），再 `i = requestedIndex-1 → 0`（向下），与笔记"先向上找，找不到再向下"逐句吻合。
- ⚠️ 枚举：`ai/src/types.ts:85-87`：ThinkingLevel = minimal|low|medium|high|xhigh|**max**（6 级），ModelThinkingLevel 加 off 共 7 档；笔记正文"五级"、配图 6 级（off/minimal/low/medium/high/xhigh），均不含 max。

**4.6 缓存控制统一为 CacheRetention = none|short|long；四家打点方式不同（Anthropic 贴 cache_control、Bedrock 插 cachePoint 节点、OpenAI Responses 发 prompt_cache_key=sessionId、OpenAI 兼容抄 Anthropic 协议）** —— ✅（行号漂移）
- `ai/src/types.ts:110`：`CacheRetention = "none" | "short" | "long"` 逐字吻合。
- Anthropic 三打点：见 3.9（anthropic-messages.ts:1093-1108 / 1497 / 1407-1434）。
- Bedrock：`api/bedrock-converse-stream.ts:901`（`cachePoint: {type: DEFAULT, ...(long ? {ttl: ONE_HOUR})}`）、:1111。
- OpenAI Responses：`api/openai-responses.ts:334-335`（`prompt_cache_key` + `prompt_cache_retention`）。
- OpenAI 兼容：`api/openai-completions.ts:821`（prompt_cache_key）、:861/:1081（applyAnthropicCacheControl）、:1634（cacheControlFormat 按品牌/模型自动判定 "anthropic"）。

**4.7 isContextOverflow 三重检测（错误消息模式匹配 / token 数对比 / 输出为零+length 停止）** —— ✅
- `packages/ai/src/utils/overflow.ts:136-170`：Case1 错误模式（OVERFLOW_PATTERNS，另有 NON_OVERFLOW_PATTERNS 与 cerebras 特例）、Case2 静默溢出（input+cacheRead > contextWindow）、Case3 length 且 output==0 且 input ≥ 0.99*contextWindow。与笔记"三重检测"一致（笔记未提后两个特例，属简化）。

**4.8 接入新模型三步：写翻译器 → registerApiProvider → 配置 Model（api 字段路由）** —— ✅
- 注册点 `compat.ts:128-140`；Model.api 路由 `compat.ts:244-250`；`packages/ai/src/types.ts:29` `Api = KnownApi | (string & {})` 允许自定义 api 名。

### 映射到我们的 SPEC

| Pi 机制 | SPEC 模块 |
| --- | --- |
| 三层模型抽象（入口/协议/翻译器） | 02_AGENT_RUNTIME.md §3 ModelProvider（invoke/astream/usage/error classification）+ §4 Streaming |
| 12 种事件协议 + partial 快照 | 02 §4 ModelDelta（对外增量 + 内部聚合完整 AIMessage） |
| 错误编码进流、永不抛出 | 02 §8 Error Semantics（结构化错误分类，不自由文本） |
| CacheRetention 语义接口 | 06_CONTEXT_ARTIFACT_MEMORY.md（上下文成本工程）；Provider Adapter 内部机制 |
| isContextOverflow | 02 §5 预算层级与 counter 的溢出判定输入 |

### 对 Python Core 项目可借鉴的设计点

- "统一枚举 + 各家翻译表"（ThinkingLevel/CacheRetention）是对付 30+ provider 参数方言的最优模式：Python 侧用 `Literal` 枚举 + 每模型一张映射表 + clamp 回退函数，比 BaseProvider 继承树便宜得多。
- StreamFunction 契约三条（同输入/同输出/错误编码进流）应原样写进我们 ModelProvider 的 Protocol 文档与 Fake Provider 测试断言（对应 9.4 "加 Provider 至少有替换 Fake Provider 的测试"）。
- isContextOverflow 的三重检测值得直接移植（尤其"静默截断"与"length+零输出"两类，国内网关常见）。

### 与冻结不变量可能冲突的点

- 无直接冲突。注意边界：Pi 的翻译器各自内嵌 retry/maxTokens 调整逻辑，我们这边 **Retry 只有 ToolExecutor 一个责任域、Model Fallback 与 Tool Retry 分离**——移植 Pi 适配器时，其内部任何重试/退避必须剥离到我们的 Provider 调用域 Fallback（02 §7），不能让翻译器私自重试。

---

## 第 5 章：工具系统 —— Agent 的手脚是怎么被管住的

### 笔记声称的核心机制与核实

**5.1 三层类型 Tool / AgentTool / ToolDefinition；wrapToolDefinition 用闭包注入 ExtensionContext，Agent Loop 不知道 ExtensionContext 存在** —— ✅（行号漂移 + AgentTool 多 2 字段）
- 见 2.3 三处定义。`packages/coding-agent/src/core/tools/tool-definition-wrapper.ts:8-30`：包装器逐字段拷贝 + execute 闭包注入 `ctx ?? ctxFactory(toolCallId, signal)`。⚠️ 微差：HEAD 包装后的 execute 带可选第 5 参允许显式传 ctx（runToolCall 场景），非纯 4 参。

**5.2 五步管道：prepareArguments（兼容垫片）→ validateToolArguments（Schema 验证）→ beforeToolCall（可 block）→ execute（onUpdate 进度）→ afterToolCall（字段级覆盖结果）** —— ✅
- `agent-loop.ts:693-705`（prepareToolCallArguments，无 prepareArguments 则透传）、:726（validateToolArguments，来自 pi-ai）、:727-755（beforeToolCall，`beforeResult?.block` → createErrorToolResult(reason)，block 还可带 terminate）、:829-837（execute + onUpdate）、:853-903（finalizeExecutedToolCall 字段级合并：content/details/usage/terminate/isError 提供才替换——与 types.ts:77-91 的合并语义注释一致）。
- validateToolArguments 定义在 `packages/ai`（agent-loop.ts:16 导入）——"工具永远不会收到类型错误的参数"成立。

**5.3 所有错误统一产物 = isError:true 的 ToolResultMessage；createErrorToolResult 只有三行，error.message 原样搬运** —— ✅
- 六条错误路径：tool 未找到 `agent-loop.ts:715-722`、prepare/validate/before 异常 `:769-775`（catch → createErrorToolResult）、execute 异常 `:841-847`、afterToolCall 异常 `:892-895`；`createErrorToolResult` :905-910（content=[{type:text,text:message}] + details:{}，确实三行）。
- 全管道无一个 throw 逃逸：runToolCall 顶注（:801-809）"Never rejects for tool failures"。

**5.4 acceptingUpdates 标志位：execute settle 后孤儿 onUpdate 一律丢弃；catch 里先 await 全部进度事件再编码错误（防乱序）** —— ✅（逐字吻合）
- `agent-loop.ts:820-851`：acceptingUpdates 开/关、`await Promise.all(updateEvents)` 在成功与 catch 两分支都有、finally 兜底关闭。

**5.5 并行一票否决 + 三阶段（顺序准备/并行执行/结果按调用顺序）；7 个内置工具都未声明 executionMode，默认 parallel；Edit 用 withFileMutationQueue 对同文件编辑串行化（第二道防线）** —— ✅
- 调度：`agent-loop.ts:508-523`、三阶段 `:586-660`（见 3.10）。
- 内置工具零 executionMode 声明：`grep -n executionMode coding-agent/src/core/tools/*.ts` 仅命中 wrapper 转发行，7 个工具实现均未声明 ✅；默认 parallel：`agent/src/types.ts:317`（"Default: parallel"）、`agent.ts:253`。
- `file-mutation-queue.ts:32`（withFileMutationQueue）、调用点 `edit.ts:163` 与 `write.ts:67`（笔记 edit.ts:312 行号漂移；且 HEAD write 也走队列）。

**5.6 工具内部主动识别已知错误并包装具体描述（两层分工）：read 越界附总行数、edit 附路径、bash 把已产出输出附在 aborted/timeout 错误里、识别不了 throw err 原样透传** —— ✅（bash 非零退出码处理形态 ⚠️ 微差）
- `read.ts:143`：`Offset ${offset} is beyond end of file (${allLines.length} lines total)`——与笔记引文逐字一致（行号 275→143）。
- `edit.ts:181`：`Could not edit file: ${path}. ${errorMessage}.`——逐字一致（330→181）。
- `bash.ts:361-388`：appendStatus 闭包 + aborted / `timeout:` 识别包装 + 识别不了 `throw err`——逐字一致（390-407→361-388）。
- ⚠️ 微差：笔记引 bash "exitCode!==0 → throw new Error(appendStatus(...))"——HEAD 是**返回** `isError: true` 的 result（:403-408），不再 throw；语义等价（错误仍是一条消息），形态不同。

**5.7 onUpdate 进度推送包装成 tool_execution_update 事件流向 UI** —— ✅
- `agent-loop.ts:778-787`（emitToolExecutionUpdate）→ types.ts:528（tool_execution_update 事件）→ agent.ts:571-573（更新 streamingMessage）。

**5.8 Operations 抽象：工具不直接调 fs/child_process，每个工具定义最小接口（ReadOperations=readFile/access(+detectImageMimeType) 等 7 套）** —— ✅（行号漂移）
- `read.ts:35-42`（ReadOperations 三方法，与笔记吻合）、`write.ts:27`、`edit.ts:83`、`bash.ts:78`（BashOperations 单 exec 方法，注释明说 "Override these to delegate command execution to remote systems (for example SSH)"）、`grep.ts:53`、`find.ts:52`、`ls.ts:34`。defaultReadOperations 闭包默认实现（read.ts:44-48）。

### 映射到我们的 SPEC

| Pi 机制 | SPEC 模块 |
| --- | --- |
| 五步管道（prepare→validate→before→execute→after） | 04_TOOL_RUNTIME.md §2 Tool Contract、§5 Validation（INVALID_ARGUMENT 回给模型自我纠错——Pi 与 SPEC 逐句同义） |
| 错误=消息（isError:true ToolResultMessage） | 04 §4 ToolResult（ok/message/error_code 结构化）；02 §8 |
| 一票否决 + file-mutation-queue | 04 §7 Dependency-aware Scheduler（Pi 是其极简前身：显式声明 + 资源 key 串行化） |
| beforeToolCall 拦截点 | 04 §8 Permission / Risk（runtime 拦截点的位置参考） |
| Operations 接口（注入执行环境） | 05_SANDBOX_CODING_TOOLS.md（Sandbox 边界 = 执行环境注入，不靠 prompt） |
| runToolCall（工具内嵌调用走同管道） | 不变量 #7 "Tool 只有一条统一执行路径" 的正面示范 |

### 对 Python Core 项目可借鉴的设计点

- **错误即消息 + 两层分工**（工具内识别已知错误包装具体描述；框架兜底只透传 error.message，不创造统一描述）——直接可写进我们 Tool Contract 的错误规范；"错误描述越具体模型纠错越强"对应我们 ToolResult.error_code + message 的设计。
- acceptingUpdates 闸门 + "先发完进度事件再编码错误" 的乱序防御，是 Python asyncio 侧同样需要的细节（Task settle 后的孤儿回调）。
- withFileMutationQueue：按资源路径的进程内互斥队列，是我们 "资源冲突不只看 READ/WRITE"（不变量 #10）的最小可运行示例，可作为 resource_keys 调度的第一步实现。
- runToolCall 导出复用同管道（权限钩子对工具内嵌调用同样生效）——我们的 ToolExecutor 应提供同等入口，防止绕路。
- Operations 最小接口注入（每工具只声明自己要的方法）是 Sandbox/Docker 执行环境切换的干净缝。

### 与冻结不变量可能冲突的点

- **Pi 没有框架级 Tool Retry**：错误变成消息后由模型自行重试，框架不重试。这与我们 "ToolExecutor 是唯一 retry 责任域"（04 §6）是两种策略——借鉴 Pi 不等于照抄：我们的 Executor 仍要对 transient 错误做受控重试（含 Operation Ledger reconcile），Pi 的"模型重试"只能作为 deterministic 错误（INVALID_ARGUMENT 类）的补充路径，且要配 Repeated Tool Guard（02 §6）防模型打转。
- beforeToolCall 是**可选钩子**而默认不存在（YOLO 默认）：我们移植管道时，Permission 拦截必须是 Executor 内的必经步骤（04 §8），不能是"扩展可装可不装"的选项。

---

## 版本漂移总备注（引用笔记时必读）

1. 笔记基于 v0.80.2；上游 HEAD 1b34779 为 v0.99.1。第 1–2 章的"包清单/不做清单"（无 MCP、有 orchestrator、五包）已整体过时：HEAD 13 包、有 pi-mcp/durable/codemode/protocol/client/session-backends、无 orchestrator。
2. 最大的架构变化是 **transcript 化**：系统提示词与工具声明改为由 transcript 的 SystemMessage 携带（Message 联合加了 SystemMessage、AgentContext 去掉 systemPrompt 字段、StreamFn 收 branded TranscriptContext、convertToLlm 保留 system）。第 3 章笔记中所有 "context.systemPrompt/tools" 的叙述需按此修正。
3. 钩子面重构：`shouldStopAfterTurn` → `finishTurn({action})` + 新增 `prepareRequest`；队列新增 QueueMode（all / one-at-a-time，默认 one-at-a-time，agent.ts:247-248）。
4. 行号系统性漂移约 +5%~+40%（agent-loop.ts 940 行、compat.ts 302 行、ai/types.ts 1168 行）；引用笔记行号一律以上游实测为准，本卡片 file:line 均为 1b34779 实测。
5. 未发现笔记**凭空捏造**的机制——所有 ⚠️ 均为版本漂移或简化表述，无 ❓ 项（所有声称机制都在上游找到了对应实现）。
