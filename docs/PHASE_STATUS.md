# Phase Status — 实施进度追踪

> **单一事实源**：任何 agent 进入项目前先读本文件，判断"当前做到哪里"。
> 规格文件（`SPEC_ROOT/14_IMPLEMENTATION_ROADMAP.md` 等，`SPEC_ROOT` 见 `AGENTS.md` §1）保持冻结，不被进度修改。
> 每完成一个 Phase，更新本文件对应行的状态 + commit hash + Gate 证据。

更新规则：
- `✅ COMPLETED`：Phase 全部交付物落地 + Gate 达成 + 测试通过。
- `🔄 IN PROGRESS`：部分交付物已落地，剩余项明确。
- `🔄 PARTIAL`：跨 Phase 的能力部分提前落地（如 Phase 3 工具在 Phase 1 阶段已部分实现）。
- `⬜ NOT STARTED`：未涉及。

---

## 进度表

| Phase | 名称 | 状态 | 关键 commit | Gate 证据 / 备注 |
|---|---|---|---|---|
| **0** | Repo Foundation | ✅ COMPLETED | `21b421d` 起 | Python 3.11+ / uv / pytest-asyncio / ruff / pydantic-settings / JSONL Diagnostic Logger。`config.py` + `logging.py` + 测试 harness 就位。 |
| **1** | SessionEvent + Model + Minimal Agent Loop | ✅ COMPLETED | `f789aac`（AgentRuntime）<br>`8ecb202`（bind_tools）<br>`87c3a72`（event-sourced） | SessionEvent DTO + 10 种 event type + JsonlSessionStore（崩溃安全）+ derive_messages（配对+dangling 合成）+ Session 聚合根（start/resume/append/derive/begin_run/end_run）+ AgentRuntime event-sourced 改造 + Resume 集成测试。Gate 达成：进程重启后可从 JSONL 恢复完整对话历史。135 passed。 |
| **2** | Tool Runtime | ✅ COMPLETED | `8572fd8` → `00f3753` | Tool Contract / Registry / ToolResult / Validation-first ToolExecutor / 单 Retry Layer / 批次调度 + 严格 ID 配对。Gate 达成：INVALID_ARGUMENT 不重试、transient 可重试、配对 100%。Permission interface 薄，留待 Phase 7 Capability seam 深化。 |
| **3** | Docker Sandbox + Coding Tools | ✅ COMPLETED | `2bfa457` → `ed96f5a` | Sandbox ABC（含 list_files）+ LocalSubprocessSandbox + DockerSandbox（懒加载 + 确定性命名 + 跨进程恢复）+ 9 个 Coding Tools（read/write/bash/edit/grep/glob/apply_patch/git_status/git_diff）+ 批次调度验证 ✅。**后续独立 spec 全部落地**：Approval / REQUIRE_APPROVAL 机制（PermissionPolicy + ToolPermission + ToolExecutor 审批关卡 stage 2.5 + per-call scoping，默认安全拒绝 danger）；Session-scoped Sandbox 生命周期（WorkspaceRegistry 映射持久化 + Session.start/resume 自动绑定 sandbox + Docker 后端跨进程恢复）。Gate 达成：edit 多匹配明确失败、pytest exit_code=1 不被 retry、两个无冲突文件操作可并发、冲突写被串行化、路径越界统一 PERMISSION_DENIED、DANGER 工具在非 full-access policy 下被审批关卡拦截。252 passed。 |
| **4** | Storage + Operation Ledger + Recovery | ✅ COMPLETED | `5b85e36`（#27 Ledger）<br>`6c94398`（#28 Checkpoint）<br>`fbcf599`（#29 RecoveryCoordinator）<br>`596c53b`（#30 Reconcile）<br>`778c1da`（#32 Kill 集成测试） | 全部 6 个 Ticket 落地：durable Operation Ledger（SQLite + aiosqlite，Ledger-first 写入顺序）+ 稳定边界 Checkpoint 持久化（CheckpointPolicy seam，checkpoint 绝不进事件流）+ RecoveryCoordinator（07 §9 冻结 8 步唯一入口，决策与写入分离，BEGIN EXCLUSIVE sidecar 恢复锁）+ UNKNOWN 人工裁决（ReconcileHint / ReconcileVerdict / ReconcileCallback，RUNNING→UNKNOWN→NEED_RECONCILE 两步状态机强制，RETRY 只能来自用户裁决，operation/reconcile-required 落盘）+ 真实子进程 Kill 恢复集成测试。Gate 达成：duplicate confirmed side effect = 0、dangling tool call = 0、Workspace 按原映射恢复、5 个独立 Kill 场景全过、PENDING 默认 skip 不盲跑。345 passed, 8 skipped；全仓 ruff clean。 |
| **5** | Artifact + S3 + Context Compaction（ADR-0006） | ✅ COMPLETED | `fd7439a`（#45）<br>`c98c9a5`（#46）<br>`de6a894`（#47）<br>`80086a9`（#48）<br>`e19905d`（#49）<br>`c0a4b0f`（#50） | ArtifactStore / Fake / S3 / inspect_artifact、Overflow Handler、ContextBuilder、三层 Compaction、Runtime/Web 装配全部落地。真实七牛 5000 行 Bash 溢出、局部回读、历史重建 Gate 通过。全量 424 passed；真实七牛 2 passed；ruff clean。证据：docs/PHASE5_GATE.md。 |
| **6** | Memory Capability / Context Provider | ✅ COMPLETED | `21a9e2f`（#51）+ `93d2e18`（#52）+ `62db7de`（#53）+ `e2a3a92`（#54）+ `df2b3e8`（#55）+ `5c6d40f`（deps）+ `96d02bf`（#56 Web 接线 + Review 修复）+ Gap 收口 commit | IdentityContext + SQLite MemoryRecordStore + Milvus 向量适配 + Outbox Relay + LangMem Capability + Extractor 三层降级 + Writeback + Web 入口接线全部落地；USER/SESSION 隔离、事务 outbox。**真实 Zilliz + SiliconFlow 嵌入 Gate 3 passed（连续两轮）**：真实 CRUD 闭环、语义检索排序、多租户隔离、schema mismatch、清理全过。503 passed、8 skipped、8 deselected，ruff clean。证据：docs/PHASE6_GATE.md。 |
| **7** | Capability / Plugin Foundation + Skills | ✅ COMPLETED | `4548cc2`（ADR+术语）<br>`fe94fab`（#59 config+wiring）<br>`e9b29b3`（#60 discovery）<br>`f589868`（#61 skills 闭环）<br>`2a84c0e`（#62 demo+Gate）<br>`984f4c9`（review 修复） | CapabilityRegistry 命名注册 + CapabilityDescriptor（08 §5 字段 + degradation 三分类 + enabled）+ CapabilityError 四码词汇表 + CAPABILITIES env JSON（pydantic strict）+ wire_capabilities 显式装配表（factory 失败按档降级 / REQUIRED_CORE 上抛 / 未知 provider 显式失败）+ Memory 迁入 Registry + SKILL.md 发现（frontmatter name/description 必填 + 可选 when_to_use 注入目录行，用户批准扩展）+ SkillCatalogContextProvider 只注目录 + load_skill READ_ONLY 统一 Executor + TickerCapability demo 纯 config 注册。Gate 达成：demo capability 全链路 Agent Loop 零改动（runtime.py 零 diff + ScriptedModel 闭环）、Skill 全文不默认进 Context（模型请求边界断言）、Optional 故障降级、插件不绕过 Permission/Ledger。585 passed、8 skipped、8 deselected，ruff clean。证据：docs/PHASE7_GATE.md。 |
| **9** | Streaming Surfaces | ✅ COMPLETED | `c4261b6`（SSE+run_stream 核心）<br>`d4a7709`（review 修复）<br>`db8ea96`/`daa03ef`（前端 Gap 收口）<br>本轮（CLI Renderer 补建） | 精简版提前施工（ADR-0005，用户授权跳序）+ 逐项收口。交付：AgentEvent stream（run_stream 逐 chunk delta + 持久化事件镜像，test_run_stream）；CLI Renderer（StreamRenderer 事件流纯函数渲染：delta 直写/工具状态行/结果预览/时长徽章/用量页脚，渲染约定借鉴 pi-mono & oh-my-pi 均 MIT，ascii 符号路线规避 GBK 编码崩；会话持久化 + SystemExit(1) 失败码）；FastAPI SSE + REST POST + Session query API（list/events/recover）；disconnect cleanup（断连取消臂补 run/failed(reason=cancelled)，无 task 泄漏，test_stream_disconnect_recovery）。Gate：ModelDelta/Tool 事件实时 ✅；SSE 断连无 queue/task 泄漏 ✅。 |
| **8** | MCP Client Capability | ✅ COMPLETED | `5b8e765`（#63 config+fake）<br>`0600738`（#64/65 transports）<br>`943d4e3`（#66 adapter）<br>`508371f`（#67 wiring）<br>`723f32e`（#68 Gate） | ADR-0012（grill 逐项拍板）驱动：MCP Client only（官方 SDK 2.1.1 REUSE+ADAPT）+ tools 原语 only；stdio + Streamable HTTP 双 transport；capability 接入（配置解析 extra=forbid 响亮失败 / ${VAR} 秘密间接引用 / stdio 白名单 env / 单 server 隔离降级 / 全下线跳过）；MCPToolAdapter（mcp__{server}__{tool} 命名、readOnlyHint→READ_ONLY else DANGER + 配置覆写、inputSchema→动态 pydantic、50KB 输出预算、MCP isError→failure(retryable=False)）；生命周期（wiring 时连接、重连只恢复连接不隐式重执行、lifecycle 通道挂 AppState.shutdown）；预设四 server（GitHub/chrome-devtools/Sentry/Context7）opt-in 模板。Gate 达成：① remote MCP tool 经统一 ToolExecutor（Ledger 记录 + DANGER 审批拦截 + SessionEvent 镜像）② 无双重 retry（SDK retry 关闭、死亡调用零隐式重放、isError 单次尝试）。测试 fake server 全程 in-process（真实大厂 server 手动验收清单 docs/PHASE8_MANUAL_ACCEPTANCE.md）。全量 761 passed、8 skipped、8 deselected，ruff clean。 |

| **10** | Lightweight Web Session Inspector | ✅ COMPLETED (UI 重设计) | `6183d3f` → `38fc775` | UI 重设计七阶段全部交付（Phase 1-7，docs/UI_DESIGN_DECISIONS.md 冻结决策落地）：Phase 1 设计系统基座（CSS 变量 token + 液态玻璃 + Apple 缓动）；Phase 2 Application Shell（三栏单帧 + Run Pulse 信号 #1）；Phase 3 Trace Ladder（Chat Refinement + 执行链投影签名 #2）；Phase 4 Trace Density 四档（localStorage 持久化）；Phase 5 Run Inspector 五 tab + 事件级钻取（InspectorFocus 判别联合）；Phase 6 Session Model E 轮 + DSH 工具四态（running/success/failed/stopped）+ UnknownSurface 兜底；Phase 7 Accessibility（WCAG 对比度 + focus-visible 全覆盖）。纯函数 Vitest 73/73，tsc clean，生产构建通过。前端单一投影源 lib/projection.ts 不变量 #22 守护。**后端集成 Gap 见 docs/INTEGRATION_NOTES.md**：usage/model/cost/trace_id/checkpoint API + 首条用户消息 payload 待后端 Phase 9 扩展。 |
| **11** | Knowledge / RAG | ✅ COMPLETED | `fc4eca0`（ADR-0013+术语）<br>`99883c2`（T1 服务+fake）<br>`958e887`（T2 Milvus）<br>`5367d57`（T3/T4 工具+CLI）<br>`b4d4cd3`（T5 接线）<br>本轮（T6 真实 Gate） | ADR-0013 grill 逐项拍板：Agentic RAG（检索=工具，模型自主决策，无自动注入）；Knowledge 与 Memory 平行域、独立 knowledge_chunks collection（tenant partition）+ 域专用协议（不泛化 memory 接口）；source 级增量（hash 跳过/原子重建、registry 提交点最后写崩溃自愈）；citation `kb:<source>#<idx>` 可追溯；sufficient 阈值 0.6（真实分数分布验证：相关 0.66–0.73 / 噪声 ≤0.29 干净分离）；2MB/2000 防呆；删除不暴露给模型。真实 Zilliz Gate 三条全过（restart persistent search / 不足如实 false / citation 链路）。证据：docs/PHASE11_GATE.md。 |
| **12** | Web Search / Reliability | ✅ COMPLETED | `fd8ede5`（#76-#81 全套）<br>`7358897`（review 修复） | ADR-0014（grill 逐项拍板）驱动：同错熔断 RepeatedToolFailureGuard（指纹=工具名+规范化 args，软 3 注入纠正 → 硬 6 end_run(failed)，#69）；统一 RetrievalProvider 窄协议（KB 与 Web 平行策略类）+ TavilyWebSearchProvider（手写 httpx 零新依赖，#77/#78）；web_search 工具（citation `web:<url>` + 防注入声明 + Retrieval Fallback Policy agentic hint，#79）；Model Fallback（is_transient_model_error 共享分类 + FallbackPolicy seam + ModelFallbackCoordinator never-切回 + runtime per-run 编排 + model/fallback 白盒事件，#80）；websearch capability 接线（TAVILY_API_KEY 未配 = OPTIONAL_RUNTIME 降级缺席）。真实 Gate 三条全过（Tavily 联网 citation 实测 / 死端点→真实 fallback 切换 reason=APIConnectionError / 真实模型 8 并行同参失败→硬熔断 identical_tool_failure_loop）。证据：docs/PHASE12_GATE.md。后续加固：DSML 泄漏守卫 + 流式卡流看门狗（idle 60s/total 600s → ModelStallError transient）+ MODEL_MAX_CONCURRENCY 进程级并发闸门。 |
| **13** | Multi-Agent | ✅ COMPLETED | `79a0014`（ADR-0015+术语）<br>`c72ec4c`（#82 profiles+factory）<br>`2096743`（#89 并发闸门）<br>`32a1593`（#83 capability+delegate）<br>`e2d03ef`（#84 事件+lineage）<br>`021ee6e`（#85 result 字段）<br>`97b4c9b`（#86 溢出）<br>`756602a`（#87 预算）<br>`e7b8360`（#88 熔断+#92 seam）<br>`0fc5057`（#90 并行）<br>`0f1d01d`（#91 取消恢复）<br>本轮（#93 Gate+文档） | ADR-0015（grill 逐项拍板，组织原则=「一切皆可是插件」）：multiagent 为 CAPABILITIES opt-in capability（ADR-0010 模式，未配置=单代理零感知）；AgentProfile/AgentSpec 核心域对象 + AgentFactory（scope 收窄生成新 registry、越权拒绝防提升，决策 11）；supervisor = main profile + delegate 工具（零新编排组件，DelegationDecision 即工具参数，经统一 ToolExecutor，决策 4）；child = 独立 Session + depth=1（父流只有 delegation-started/finished 白盒事件，child 完整历史在 child JSONL，决策 6/7/8）；SubAgentResult 真实来源字段（citations/artifacts/changed_files/unresolved 绝不伪造，决策 12）+ summary 溢出 store-backed/truncate+pointer；预算三旋钮 max_delegations=8/max_active_children=4/child max_steps=10 + 超限明确回填（决策 13）；重复委派熔断复用 RepeatedToolFailureGuard（#88 零新机器）；阻塞并行委派（executor 既有 gather 管线，#90）；取消/恢复语义（父 cancel → child 收尾 + 事件对账，#91）；OrchestrationAdapter Protocol-only seam（LangGraph 未引入，import 边界契约测试，#92）。真实 Gate 八条全过（research/coding 路由、动态第四 Agent、child 不倾倒、mixed 双 child 协作、熔断/预算真实触发、capability 关闭单代理回归）。证据：docs/PHASE13_GATE.md。 |
| **S-UI** | Streaming UI 生产级改造（detached-run / reasoning / 工具输出流 / 重连 / 多模型） | ✅ COMPLETED | `1b04445`（T1+T2 词汇+合帧+reasoning）<br>`849a901`（T3 工具输出流+预持久化）<br>`6337b8c`（T4 RunManager+detached-run）<br>`6a3ba27`（T5+T6 cancel+after_seq）<br>`961d8ec`（T7 多模型） | ADR-0016（用户经前端 prompt 拍板 D-A/D-B/D-C/D-D，数值与细节后端落定；与 feat/phase14 并行开发，ADR 分账 0016/0017，session/event.py 双方只做加法）。交付：① detached-run（web RunManager 订阅制广播 + seq 幂等合并 + 有界队列丢旧自愈 + 孤儿 300s 回收 reason=orphaned + POST /cancel 显式取消——Phase 9 取消臂语义有意修订，断连不再杀 run）；② reasoning 事件族（ReasoningChatOpenAI 接出网关 reasoning_content、30ms 合帧落盘、块生命周期 + interrupted 保部分内容、零伪造）；③ text/delta 合帧 durable（model/delta 词汇退役不发射）；④ 工具输出真流式（sandbox on_output → 线程安全 sink → tool/output_delta，64KB/channel 上限，tool/call 预持久化对齐规格 02 §8.3）；⑤ after_seq 重连续传（先订阅后取游标，重放+接续无缝无重复；backlog>1000 发 stream/truncated 控制帧；终态/悬空语义分明）；⑥ 多模型（AGENT_MODELS catalog + GET /api/models + 会话级 model 参数，fallback 链不动 ADR-0014）。契约回传前端：docs/BACKEND_CONTRACT_STREAMING_UI.md（text/delta 切换 / cancel 端点 / 重连协议 / 迁移清单）。离线全量 **1093 passed**、9 skipped、20 deselected，ruff clean。 |
| **14** | Resume / Replay / Fork 完整化 | ✅ COMPLETED | `bcaed59`（ADR-0017+术语）<br>`1685ed3`（#107 schema）<br>`c8f45b2`（#108 fork 核心）<br>`36edd1b`（#109 copy-on-fork）<br>`5ded3b6`（#110 tail summary）<br>`2e12074`（#111 CLI fork）<br>`0f70b98`（#112 lineage 树）<br>`3f8673e`（#113 Web API）<br>`2f476d4`（#114 replay）<br>本轮（#115 Gate+文档） | ADR-0017（grill 两轮，上游调研 pi/Claude Code/LangGraph/oh-my-pi）：file-per-lineage（session 线性 append-only 宪法不动，树在 SessionMetaStore 索引层）；fork boundary = run 完整边界（UX 按第 N 条用户消息）；seed = 前缀逐字复制（重编 seq 留原 event_id，child 自包含）；`session/forked` provenance 事件（parent/fork_point/boundary/tail_summary?）；lineage 双层（事件=真相 + meta=索引，fork|delegation 统一 origin，存量惰性回填）；copy-on-fork（物理策略独立，artifact ref 复用）；tail summary（pi branch_summary/oh-my-pi rewind-report 精神，失败降级不变量 #21）；CLI `fork` / `replay`（冻结 tool result 契约）/ `sessions --tree`；Web 只读 lineage API（独立 router，躲 ADR-0016 重刀）。真实 Gate 5/5 单轮全过（fork 全链/摘要/隔离/树/回放零副作用），Gate 抓到并修复 zhipu preset 缺册真 bug。证据：docs/PHASE14_GATE.md。重新执行式 replay / Web 发起 fork / 树内导航 DEFER。 |
| **15** | Observability + Evaluation | ✅ COMPLETED | `5b8e765`（#117 sink 骨架）<br>`…`（#118-#125 全套）<br>`86adedc`（合入 main）<br>`c29a4f9`（smoke + Gate 收尾） | ADR-0018（grill 两轮逐项拍板）：LangfuseSink 旁路骨架（settings 门控 + 故障隔离 + 熔断器，key 空=零 import 完全缺席）、RunTracer（trace=run + generation=model call + 六条终态臂 trace_id 回填）、tool span（attempt 链）+ SubAgent 嵌套 trace + context-build span、flush 生命周期、reconcile verdict JSONL 诊断行、评测骨架（datasets 真相源 + P0 金标 cases + 真实模型 smoke + Langfuse seed/Experiment）、ScriptedModel 提升进 src、真云 Gate（jp 区）。D7 DEFER 批（`dbb48a5`，待集成）：environment/release 透传 + tool_calls 轮 output 标记。trace_url 契约（`2d7f87a`，已合入 main）：官方 SDK 可点击 URL 与 trace_id 并列对称下发。证据：docs/PHASE15_GATE.md。 |
| **16** | Final Full E2E | ✅ COMPLETED + 集成后验证闭合 | `1c09677`（ADR-0019+术语）<br>`93174f1`（#126 T1）<br>`bad588a`（#127 T2）<br>`1a38339`（#128 T3）<br>`8bbbbe3`（#129 T4）<br>`3b308bd`（#130 T5 测试）<br>`87080dd`（Gate 收尾文档）<br>`cefdbe1`（合入 main）<br>`7a2b8db`（集成后验证修复：DockerSandbox bug + 文档精度） | ADR-0019（grill 三轮收敛，12 项决策）：分段独立断言 + 薄编排层（D1）、双层模型（D2）、全 fake 进 CI（D3）、真子进程 kill + Ledger reconcile（D4）、Docker probe-gated integration（D5）、mutating tool = coding 副作用对账（D6）、单 gate 文件 + helpers（D7）、Gate 6 指标精确化（D8）、范围边界不重复测已覆盖能力（D9）、fixture 混合（D10）、性能只量不裁（D11）、trace + report 是 Gate（D12）。Tickets #126-#130 全部交付。**Gate 6/6 PASS**：duplicate confirmed=0（G1）、dangling=0（G2）、core recovery all-or-nothing（G3）、citation validity 格式+可回捞（G4）、permission violation 阻断+放行（G5）、Full E2E reproducible 薄编排确定性（G6）。**集成后验证闭合**：Gate **12/12 passed（含 Docker 容器恢复 PASS，daemon 在场）**；真实模型 smoke Langfuse trace 结构与 Gate 断言一致；DockerSandbox `read_text` NotFound→FileNotFoundError 真 bug 已修。最终基线：全量 **1222 passed / 1 skipped / ruff clean**。上游调研：pi-mono `describe.skipIf(!API_KEY)` + mock model 双层模式同构。详见 `docs/PHASE16_GATE.md`。 |

---

## 当前工作焦点

**Phase 14 已完成（Resume / Replay / Fork 完整化，ADR-0017 + tickets #107-#115）**：file-per-lineage fork（session 线性 JSONL 宪法不动）+ seed 逐字复制 + `session/forked` provenance + lineage 双层索引（fork|delegation 同树）+ copy-on-fork + tail summary（失败降级）+ CLI `fork`/`replay`/`sessions --tree` + Web 只读 lineage API（独立 router）。真实 Gate 5/5 单轮全过（docs/PHASE14_GATE.md）。离线全量 1089 passed、9 skipped、25 deselected，ruff clean。**本 Phase 在独立 worktree `D:\intelligence-agent-phase14`（feat/phase14）交付**——与并行流式改造（ADR-0016，feat/backend）零文件冲突；集成顺序：流式改造先、Phase 14 后（§14.9）。

**Streaming UI 生产级改造已完成（S-UI，ADR-0016）**：detached-run + 显式取消端点 + reasoning 事件族 + 工具输出真流式 + after_seq 重连续传 + 多模型 catalog 全部落地；断连不再取消 run（Phase 9 取消臂语义经 ADR-0016 有意修订），前端契约回执见 docs/BACKEND_CONTRACT_STREAMING_UI.md（关键迁移点：live 文本流 model/delta → text/delta、Esc 走 POST /cancel、seq gap 触发 after_seq 重连）。

前序：Phase 13 完成（Multi-Agent，ADR-0015）；Phase 12 完成（Web Search / Reliability，ADR-0014）；Phase 8 完成（MCP，ADR-0012）；Phase 11 完成（Knowledge，ADR-0013）。fallback 链已异构化（senseaudio primary + zhipu fallback，gate2 实测真实切换）。

## 更新日志（索引）

> **本文件不再内联历史明细。** 查批次 / 集成 / 审查记录：按「按日定位」用 `Read` + `offset/limit` 只读那一段，
> 或先 `grep -n "关键词" docs/phase_status/2026-09.md` 定位。**不要整文件读。**
> ⚠ 不要用 `tail` / `sed` / `cat` 读这些中文大文件：Windows Git Bash 的 GBK 控制台会把 UTF-8 显示成乱码（实测踩过）。

**单条 bullet 上限 2000 字符**（防止本文件再长回 500 KB）：明细超出就只在这里留一行索引 + 正文进当月归档文件。

### 最近条目（最新在上）

- 2026-09-17（本批）：**#222 失败归因补齐**——`run/failed` 在每条失败路径上都落 `reason`（未分类退到异常类型名，`max_steps` 那条此前一个键都没有）+ 可读 `message` 兜底；真机修的过程中又抓到 Overview「失败原因」行"只有码没有文案 ⇒ 渲染空值"的前端 bug；两轴共识两条（ADR/契约未随合同更新、票面 AC 的可读兜底未做）全处置，机制收进 ADR-0033 §2.4
- 2026-09-17（本批）：**#221 慢链路上迟到的非 2xx 被静默吞掉**——`/messages` 结局改为单一分派表（窗内窗外同表同呈现）+ 迟到落定必须消费；两轴抓到我自己漏的 P1（纠正接错流不推进代际 ⇒ 假「连接中断」盖掉真原因），机制收进 ADR-0030 §13；收尾自检又补 T12q（纠正后回退重投的失败必须报出），并对本批早先写下的 playwright「410 passed」做了**自我勘误**（那是推算值，实测 412+4 → 归因满载抖动，终版 418 全绿）
- 2026-09-17（本批）：**#220 失败归因投影进 UI**——「失败原因」行 + Timeline 摘要（reason-only 兜底、长文案换行、取消不算失败）；两轴 findings 全修（6×P2）+ 红证 8 条 + 机制收进新 ADR-0033
- 2026-09-17（本批）：**真机巡检批 #218（后端可归因性）+ #219（Ctrl+Enter 静默丢输入）**——两轴 findings 全修（含我引入的两条 P2 回归）；另开 #220（失败文案未投影进界面）/ #221（迟到非 2xx 丢消息）
- 2026-09-17（本批）：#217 图标名集跨端对账（issue 前提经实测订正：真正开着的是前端侧）
- 2026-09-17（流程）：**审查覆盖机械闸门 + 规则落点整理**（`dda2b64`，两轴 findings 全修：P1 白名单可放行 `docs/` 下脚本等）
- 2026-09-17（复验）：集成交付后的两轴复验修复（`44f48c7`）
- 2026-09-17（本批）：#214 / #215 / #216 三票收批（`6f81c6e` → `9f2a8f8`）
- 2026-09-17（本批）：#213「增量重写发生过信号」收尾 = 决议不改契约（`089524a`）
- 2026-09-17（集成）：批 ③ + 批 ④ 共 5 票（#212 / #208 / #209 / #201 / #199）已集成并 push
- 2026-09-17（补记）：批 ④ 的最终全量审查（两轴独立，fixed point = `origin/main`）
- 2026-09-17：批 ④（#201 剩余 AC + #199 剩余 AC）——档位收窄披露的数据面与呈现，两轴审查各一轮
- 2026-09-16：复审第二轮（对本批自审，独立 subagent 两轴）

### 归档文件

| 文件 | 覆盖日期 | 条目数 | 说明 |
| --- | --- | --- | --- |
| `docs/phase_status/2026-09.md` | 2026-09-03 .. 2026-09-17 | 223 | 原「更新日志」整段（条目正文逐字未改，按日期重排） |

### 按日定位（归档内行号，日期降序）

| 日期 | 条目 | 位置 |
| --- | --- | --- |
| 2026-09-17 | 18 | `2026-09.md` L412-430 |
| 2026-09-16 | 4 | `2026-09.md` L408-411 |
| 2026-09-15 | 11 | `2026-09.md` L387-407 |
| 2026-09-14 | 11 | `2026-09.md` L317-386 |
| 2026-09-13 | 14 | `2026-09.md` L289-316 |
| 2026-09-12 | 23 | `2026-09.md` L255-288 |
| 2026-09-11 | 25 | `2026-09.md` L220-254 |
| 2026-09-10 | 6 | `2026-09.md` L209-219 |
| 2026-09-09 | 9 | `2026-09.md` L193-208 |
| 2026-09-08 | 14 | `2026-09.md` L165-192 |
| 2026-09-07 | 18 | `2026-09.md` L136-164 |
| 2026-09-06 | 33 | `2026-09.md` L78-135 |
| 2026-09-05 | 20 | `2026-09.md` L45-77 |
| 2026-09-04 | 15 | `2026-09.md` L17-44 |
| 2026-09-03 | 4 | `2026-09.md` L13-16 |
