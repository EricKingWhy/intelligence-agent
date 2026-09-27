# Agent 产品形态与用户需求调研（2026-09-27）

> 范围：公开的第一方产品文档、官方仓库、官方 issue 中的用户报告、原始调查。以下“限制”区分已由厂商文档确认的边界与用户报告；单个 issue 不等于普遍缺陷。本文件是产品研究，不修改冻结规格或实施路线。

## 结论摘要

1. **浏览器 UI 本身并未过时。** 领先产品已经形成“Web/手机负责派单与审阅，实际执行在本机、隔离 VM 或云环境”的混合形态。OpenHands 明确让同一个 Agent Canvas 连接本机、Docker、远端及云端；Cursor 的云 Agent 可从 Web/手机启动；Claude Cowork 可从 Web、桌面、手机派单，在云端运行，并通过桌面桥接本地文件。问题是执行位置、权限和连续性是否符合任务，而非窗口是否原生。[OpenHands 官方仓库](https://github.com/OpenHands/OpenHands)、[Cursor Cloud Agents](https://cursor.com/docs/cloud-agent)、[Claude Cowork 入门](https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork)。
2. **DeepSeek Harness 的官方桌面端本质上仍是 Web 应用。** 官方仓库将 Electron 描述为完整 dsh Web 应用的壳，共用 Web Host、认证 API 与插件管理；原生目录选择、打包、更新和应用生命周期属于桌面壳。推断：本项目若要桌面形态，可保留现有 Web UI/后端，增加窄的原生宿主，不必重造前端或 Core。[DeepSeek 官方 desktop README](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)。
3. **可验证的长任务连续性是更有希望的突破口。** Codex 与 OpenHands 官方 issue 中有用户报告：压缩后遗失执行进度、重复工作；长会话在崩溃后虽有 event log 却无法顺利恢复；大 Tool 输出被截断后不可回取。这些是具体产品报告，不证明所有版本仍有相同问题。它们表明用户在意“现在做到哪一步、证据在哪、能否安全继续”，不只是更长上下文。[Codex #25900](https://github.com/openai/codex/issues/25900)、[Codex #34095](https://github.com/openai/codex/issues/34095)、[OpenHands #13349](https://github.com/OpenHands/OpenHands/issues/13349)、[OpenHands #12353](https://github.com/OpenHands/OpenHands/issues/12353)。
4. **用户对可靠性、控制和省去重复劳动有明确需求。** Stack Overflow 2025 开发者调查中，46% 受访者不信任 AI 输出准确性（33% 信任）；66% 遇到“差一点就对”的方案；87% 担心 Agent 准确性，81% 担心数据安全和隐私。Anthropic 对 80,508 名 Claude 用户的开放式访谈中，26.7% 提到不可靠，约 19% 希望 AI 处理例行工作，让自己做更有价值的工作。两份样本分别偏开发者和 Claude 用户，不能直接代表所有潜在客户。[Stack Overflow 原始调查](https://survey.stackoverflow.co/2025/ai)、[Anthropic 原始访谈分析](https://www.anthropic.com/features/81k-interviews)。

## 产品形态与可证实的边界

| 产品 | 官方描述的运行/交互形态 | 明确边界或开放机会 |
| --- | --- | --- |
| Claude Code / Cowork | Claude Code 有 CLI、桌面、本地/云会话；桌面可预览应用、看 diff、跟踪 PR，CLI 可把会话带到桌面，桌面可续到 Web/手机。Cowork 可从 Web/桌面/手机启动，云端运行，在需要本地文件/浏览器/电脑操作时经桌面应用连接。[桌面工作流](https://claude.com/blog/preview-review-and-merge-with-claude-code)、[桌面并行会话](https://claude.com/blog/claude-code-desktop-redesign)、[Cowork 运行说明](https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork) | 本地文件、浏览器与电脑操作需要桌面应用打开并连接；网络连接必需。桌面不是纯 UI 包装，承担本地资源边界。关于 Cowork 本地 VM 与云端执行，部分较早帮助页描述不同；此处以当前明确写着“cloud (in beta)”的入门页为准。[Cowork 运行说明](https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork) |
| OpenAI Codex | 桌面 app 作为多 Agent 控制台，提供隔离 worktree、技能、定时自动化；同时有 CLI、IDE、Web、云端。近期官方说明可通过手机接入本机/远端执行主机，文件与凭证留在执行主机。[Codex app](https://openai.com/index/introducing-the-codex-app/)、[跨设备执行](https://openai.com/index/work-with-codex-from-anywhere/) | 厂商也承认需要管理并行 Agent 和长任务监督；自动化结果进入审阅队列。官方 issue 报告自动压缩后执行前沿丢失、重复工作；属用户报告，根因/覆盖面未证实。[Codex app](https://openai.com/index/introducing-the-codex-app/)、[Codex #25900](https://github.com/openai/codex/issues/25900) |
| Cursor | IDE 前台 Agent + 独立云 VM 中的 Cloud Agents，可由 IDE、Web/手机、Slack/GitHub/Linear 派单；云 Agent 可运行测试、浏览器/桌面、生成截图/视频/日志和 PR，用户可接管远端桌面。[Cloud Agents](https://cursor.com/docs/cloud-agent)、[Cloud Agent 帮助](https://prod.cursor.com/help/ai-features/background-agents) | 云端需要 Git 仓库连接、可用开发环境、付费计划；仓库/环境在云 VM 中，存在明确的数据驻留和外网控制问题。官方文档指出环境配置是完成端到端任务的关键。[Cloud Agents](https://cursor.com/docs/cloud-agent)、[安全说明](https://prod.cursor.com/docs/cloud-agent/security) |
| Manus | 常规任务使用临时云沙箱；Cloud Computer 是持久 Ubuntu VM；桌面 My Computer 可通过本机 CLI 读写用户授权的文件夹并启动应用，手机可派任务给保持在线的本机。[Cloud Computer](https://help.manus.im/en/articles/15392111-what-is-the-cloud-computer)、[My Computer](https://help.manus.im/en/articles/14178443-what-is-the-my-computer-feature-capable-of) | 网页版看不到 My Computer；本机任务要求受支持的桌面端；Cloud Computer 没有图形桌面，且持续性需要单独云资源。厂商也提供人工接管浏览器以处理登录/CAPTCHA。[桌面限制](https://help.manus.im/en/articles/14320382-troubleshooting-the-my-computer-feature-not-appearing-or-connecting)、[云电脑限制](https://help.manus.im/en/articles/15392094-troubleshooting-cloud-computer-access-and-uptime)、[浏览器接管](https://help.manus.im/en/articles/11711218-how-can-i-take-over-manus-browser-or-vs-code) |
| OpenHands | 当前官方定位为 Agent Canvas，可运行 OpenHands、Claude Code、Codex 等，前端连接本机、Docker、VM、私有基础设施或 OpenHands Cloud；提供自动化。[官方仓库](https://github.com/OpenHands/OpenHands) | 官方文档警告无沙箱本机模式拥有完整文件系统权限。官方 issue 有用户报告长期会话恢复、Web 端上下文压缩入口、大 Tool 输出丢失等问题；其中部分 issue 已关闭，不能据此断言当前版本仍未修复。[官方仓库](https://github.com/OpenHands/OpenHands)、[恢复 issue](https://github.com/OpenHands/OpenHands/issues/13349)、[上下文 UI issue](https://github.com/OpenHands/OpenHands/issues/14611)、[输出 offload issue](https://github.com/OpenHands/OpenHands/issues/12353) |
| DeepSeek Harness | 官方桌面端是 Electron 外壳，内含 Web 入口、共享 Host/API/插件管理；桌面负责安装更新、本地目录选择、应用生命周期和受限浏览器桥。[官方桌面 README](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md) | 这验证“浏览器 UI → 桌面宿主”的可行性，但只加 Electron 壳并不能形成产品差异。官方文档说明桌面默认不启用 scheduled tasks，提醒仅在加载的会话中触发；长任务常驻与跨设备续接仍需按具体机制验证。[官方桌面 README](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md) |
| Browser Use | 提供托管 browser agent、云浏览器/CDP 基础设施和本地开源库；它是可接入的浏览器能力层。[官方 Quickstart](https://docs.browser-use.com/cloud/quickstart) | 浏览器能力并不能替代通用 Agent 的任务/事件/恢复/权限语义。推断：本项目若扩充 Browser 能力，应作为现有 Tool/Capability 的实现，与 Core 分离。[官方 Quickstart](https://docs.browser-use.com/cloud/quickstart)、[本项目 00 Project Vision](../../goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md) |

## 用户在意什么：证据与设计推断

| 原始证据 | 可作出的有限推断 |
| --- | --- |
| Stack Overflow 2025：Agent 使用者中约 70% 同意特定开发任务省时，69% 同意提高生产力；只有 17% 同意改善团队协作。[调查](https://survey.stackoverflow.co/2025/ai) | 单人效率已有价值，跨人交接、审阅和责任归属仍可能有空白；不能仅凭该调查判定用户愿意购买团队协作产品。 |
| 87% 担忧 Agent 准确性、81% 担忧安全/隐私；66% 抱怨输出“差一点就对”。[调查](https://survey.stackoverflow.co/2025/ai) | 价值主张宜从“证明做对了、哪里需要人决策、失败可继续”出发，而非声称全自动。 |
| Anthropic 访谈：最常见担忧是不可靠（26.7%，包括幻觉、假引用、核验负担）；约 19% 希望 AI 承担例行工作以专注高价值工作。[访谈](https://www.anthropic.com/features/81k-interviews) | Agent 应交付可审阅的成品和证据，尽量让用户少重述上下文、少重复检查。样本为 Claude 用户，不能外推成市场份额。 |
| OpenHands 用户报告曾请求在保留 runtime 的情况下清除陈旧对话；另有报告本地容器重启后 event 已存但会话不能顺利续接。[`/new` issue](https://github.com/OpenHands/OpenHands/issues/12564)、[恢复 issue](https://github.com/OpenHands/OpenHands/issues/13349) | 产品体验要把“历史保留”和“模型本轮需要读什么”分开，把“状态可恢复”和“用户确实能接着做”分开。 |

## 对本项目的候选突破口（研究推断，非已批准方向）

**候选定位：可验证、可接管、可恢复的长任务工作台。** 同一个任务可由 Web UI 派单与审阅，在选定的本机或隔离执行端运行。任务的事实、工具副作用、证据引用和当前继续点可追溯；中断后先对账，再继续。这个方向紧扣项目已冻结的 SessionEvent、Operation Ledger、Artifact、Context、Resume/Replay/Fork 边界，因此可能比转做纯桌面 UI 更有独特性；但仍需目标客户验证。[本项目 Vision](../../goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md)、[System Architecture](../../goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/01_SYSTEM_ARCHITECTURE.md)、[Codex issue 中的执行点诉求](https://github.com/openai/codex/issues/25900)。

最小可验证差异化实验：选一个真实的 2–4 小时工作流，要求它跨多次 compaction/进程中断，最终让新会话或另一名用户通过可见证据继续；测量重复步骤、丢失约束、不可回取输出、错误副作用、交付物核验耗时。先证明上述指标，再决定是否增加桌面宿主。此实验建议来自用户报告与本项目规格，**不是**已证实市场需求或新的实施 ticket。[Codex 长任务 issue](https://github.com/openai/codex/issues/34095)、[OpenHands 输出 offload issue](https://github.com/OpenHands/OpenHands/issues/12353)、[本项目 Phase 5/16 Gate](../../goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/14_IMPLEMENTATION_ROADMAP.md)。

## 本仓上下文机制核验（最终审阅基线 `origin/main` commit `1774f4fb`）

- 正式规格把完整 `SessionEvent` 历史、单次模型调用的 Runtime Context、可回读的 Artifact 分开；`ContextBuilder` 从事件投影消息，额外选择 Memory/Skill 等 Provider 内容。生产装配从 `Settings` 取全局 200,000 token 上限、70% 自动压缩、85% 硬护栏；`ContextBuilder` 类的直接构造默认值却是 80%/90%，须区分生产路径与测试/独立调用路径。[规格 §1–§5](../../goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/06_CONTEXT_ARTIFACT_MEMORY.md)、[builder](../../src/agent_harness/context/builder.py)、[config](../../src/agent_harness/config.py)、[assembly](../../src/agent_harness/assembly.py)。
- 大 Tool 输出经 `ArtifactOverflowHandler` 外置，模型拿摘要和引用，可用 `read_artifact`/`inspect_artifact` 局部回读。压缩器保留最新用户轮次，把早期轮次交模型生成摘要；摘要模型出错时改用机械提取，仍超硬护栏则停止。当前 `keep_recent_tokens=20_000` 和 `reserve` 虽被定义，却未参与 `compact()` 的切分或模型输出预留。估算器固定使用 `cl100k_base`，并非所有模型的精确计数。[overflow](../../src/agent_harness/tooling/overflow.py)、[compactor](../../src/agent_harness/context/compactor.py)、[tokens](../../src/agent_harness/context/tokens.py)。
- **已复现的高优先级连续性缺陷**：让摘要模型抛出合成异常，早期用户消息含唯一约束、当前消息正常。第一次 `ContextBuilder.build()` 返回的机械摘要含该约束；它随后把 `context/compacted.summary` 记为空字符串，同时把原始事件的 seq 区间标记为 shadowed。再次 `session.derive_messages()` 时旧约束消失。探针输出：`first_has_constraint=True`、`recovered_has_constraint=False`、`fallback_used=True` 且持久摘要为空。原因在 `ContextCompactor` fallback 的 `summary_text=None`、`ContextBuilder` 写入 `result.summary or ""`、`derive_messages` 跳过 shadowed 区间的组合。此处尚未修改实现，避免未经产品/票面决策扩大施工范围。[compactor](../../src/agent_harness/context/compactor.py)、[builder](../../src/agent_harness/context/builder.py)、[derive](../../src/agent_harness/session/derive.py)。
- 设计上应先固定“可恢复的事实层”：当前目标、用户约束及授权范围、确切标识/金额、未完成事项、关键决策、工具副作用状态和出处引用。确定性去重、旧 Tool Result 外置/裁剪、按任务相关性选取、分段摘要依次发生；摘要只能是投影，不能取代事实和账本。压缩提交前验证摘要非空、缩小幅度、引用可回取、Tool call/result 配对和关键事实覆盖；失败则保留原投影或安全暂停，不能提交空替代。长任务还需要独立于对话摘要的可检查任务检查点与端到端恢复 Gate。[Anthropic 长任务研究](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)、[Claude Context Editing](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context)、[Pi Compaction](https://pi.dev/docs/latest/compaction)。

### 压缩成熟实现与本仓边界（2026-09-27 补充）

- [Pi 压缩参考](https://pi.dev/docs/latest/compaction)在完整轮次边界保留近期消息，并追加带 `summary` 和 `firstKeptEntryId` 的持久压缩记录；结构化摘要包含目标、约束、进度、决策、下一步与关键上下文。可 `PORT DESIGN` 到 Python，不嵌入其 TS Agent Runtime。
- [oh-my-pi 压缩实现](https://github.com/can1357/oh-my-pi/blob/main/docs/compaction.md)先以确定性规则修剪过时 Tool Output，包括被后续读取取代的旧文件内容；其底层会改写会话存储，不能原样复用到本仓 append-only SessionEvent。只移植裁剪判断思想。
- [DeepSeek Harness Tool Result Pruner](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/README.md)用无模型的头尾保留和 source event 引用裁剪旧工具输出，保留原始事件并重新测量 token。其 TS 包依赖 DSH Session/Token Meter，适合借鉴投影语义，不适合装入 Core。
- [OpenAI API compaction](https://developers.openai.com/api/docs/guides/compaction)返回供应商专属的不透明 compaction item，不能作为本仓跨模型 Provider 的唯一可恢复状态；本仓仍应保存可审阅的结构化事实与引用。以上是架构推断，不代表这些产品都已经解决长任务约束丢失。
- 当前机械 fallback 只截取每条 User/System 前 200 字、Tool 前 100 字；LLM 摘要校验主要验证类型、缩短和窗口目标，没有机械检查六段结构或必须保留的精确信息。[compactor](../../src/agent_harness/context/compactor.py)、[prompt](../../src/agent_harness/prompt/builtin.py)。已有 ArtifactOverflowHandler、Local/MinIO/S3 Provider 与只读 inspect 工具，不能再建第二套 ArtifactStore。Artifact 保存失败目前会降级保留原始大 Tool Result；是否在压缩压力下安全暂停是待决策略，不应把现状写成已严格 fail-closed。[overflow](../../src/agent_harness/tooling/overflow.py)。

## 第二轮：语言分工与本机形态（用户 2026-09-27 回答后的核查）

用户已选择开发者/小团队，保留通用 Core，把首款产品聚焦于可恢复、可审阅且兼顾长短任务的工作台；CLI 和桌面均可，本机文件、登录态、常驻任务与桌面应用操作是桌面诉求。本段只记录技术选项与证据，不修改冻结的 Python/Async-first 规格。

| 可选分工 | 成熟产品证据 | 对本仓的含义 |
| --- | --- | --- |
| Python Agent Core + TS/React UI + TS 桌面宿主 | [OpenHands](https://github.com/OpenHands/OpenHands/blob/main/AGENTS.md) 明确把 Python SDK/Agent Server、TS 客户端与 React/TS Canvas 分开；[DeepSeek Harness 桌面端](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md) 证明现有 Web UI 可复用为 Electron 桌面窗口。两者是两种结构证据，不表示 OpenHands 使用 Electron。 | **建议**。现有 Python 事件、权限、恢复与账本仍唯一掌权；现有 TS/React 前端继续使用；桌面宿主只承担进程生命周期、受控本机资源、窗口/更新与安全桥。CLI 直接走同一 Core。 |
| TS/Node 重写 Agent Core | [Pi](https://github.com/badlogic/pi-mono)、[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 是 TS/Node Agent 的现成例子。 | 适合从零建设 Node 插件/桌面一体化产品；对本仓意味着重建已完成的 SessionEvent、ToolExecutor、Operation Ledger 与恢复语义。没有本仓同工作负载 A/B 数据证明重写能改善用户等待时间；与[冻结 Vision](../../goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md)冲突，需要用户另行决定。 |
| Python Core + 经测量后局部原生加速 | [Codex CLI](https://github.com/openai/codex/blob/main/docs/install.md) 的主实现使用 Rust；[OpenAI tiktoken](https://github.com/openai/tiktoken) 提供 Python API 与 Rust 原生核心。它们证明热点可用原生代码解决，不证明本仓此刻需要自写 Rust。 | 仅在某个 Python CPU 热点经 profiling、缓存/算法优化后仍不达目标时再考虑；数据库、磁盘、模型网络等待通常应先诊断调用次数、阻塞与并发。 |

[TypeScript 官方文档](https://www.typescriptlang.org/docs/handbook/typescript-from-scratch)明确类型在编译后擦除，TS 本身不提供运行时加速。Node [事件循环文档](https://nodejs.org/en/learn/asynchronous-work/dont-block-the-event-loop)也提醒 CPU 重活会阻塞回调；Python 官方把 [`asyncio`](https://docs.python.org/3/library/asyncio.html) 定位为适合 I/O 密集型网络工作。对于 Agent，模型响应、Sandbox 执行、数据库/磁盘与前端渲染都可能主导延迟，必须按实测分开看。

本仓已有[性能基线](../PERF_BASELINE.md)：一次 Ledger 状态迁移的 `_connect` 次数 3→1 时中位耗时 32.7→13.7 ms（§B6）；大 Artifact 的同步 save/load 曾在 6MB 中文内容下测得最长 73.86/61.46 ms，属于阻塞事件循环的 I/O 路径（§B7）；Web 大会话的投影与滚动另有[10k 基准](../STREAMING_UI_10K_BENCHMARK.md)。这些数字不能推出“Python Core 整体太慢”，但说明应先优化哪条可复现路径。

**登录态边界**：桌面内置浏览器与用户日常 Chrome 是不同会话；[OpenAI 桌面浏览器说明](https://help.openai.com/en/articles/20001277-using-the-built-in-browser-in-the-chatgpt-desktop-app)明确：要复用既有 Chrome 登录态需专门扩展/连接，不能假定 Electron 窗口自动拥有 Cookie。桌面操作同样是独立权限边界，应通过受限 Tool/Capability 和审批进入统一执行链；[DeepSeek 桌面端](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)限制 Renderer 直接访问文件系统和裸 IPC，是可复用的安全设计证据。

### Windows 薄宿主补查

本仓已有 [React/Vite 构建](../../web/package.json)、[开发期 API/WS 代理](../../web/vite.config.ts)和 [FastAPI 静态资源挂载](../../src/agent_harness/web/app.py)，首版桌面可启动本地 Python 服务后加载同源 Web 页面。[DeepSeek Harness 官方 Desktop](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)以 Electron 包装同一 Web app 并管理 Host 子进程、单实例和托盘；[OpenHands Agent Canvas Windows 发行](https://github.com/OpenHands/agent-canvas/releases/tag/v1.6.0)证明 Electron + 捆绑运行时的安装包先例。Electron 的 [单实例 API](https://www.electronjs.org/docs/latest/api/app)和[托盘教程](https://www.electronjs.org/docs/latest/tutorial/tray)可直接复用。竞争备选 [Tauri sidecar](https://v2.tauri.app/develop/sidecar/)支持封装 Python 服务，但会加入 Rust Host 工具链和目标平台 sidecar 打包；现阶段无本仓现成 Tauri 资产。Electron 本身不会打包 Python，正式票仍须明确 Python runtime、静态资源定位和与已有 `InstanceLock` 的协同。
