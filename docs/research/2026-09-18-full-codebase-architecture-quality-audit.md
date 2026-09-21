# 全仓架构与代码质量审计

> 审计日期：2026-09-18
> 仓库：`D:\intelligence-agent-backend`
> 性质：只读架构/质量审计；本文不代表已执行任何修复。
> 审计对象：`src/agent_harness/`、`tests/`、`web/`、装配/配置/观测/恢复边界，以及现行 Engineering Specification。
> 基线：审计开始时工作树为 `main`，`HEAD=5efe2b7e35297d626cc1da1abaf4b932812cf8d`；这是审计快照，不是本次交付后的 HEAD。
> 本次交付验证：`4dbe7db`（cwd 缺失目录保护 + ticket 口径修正）；专项 42 passed；全量 `2453 passed / 10 skipped / 42 deselected / 0 failed`；`ruff` clean。

## 1. 执行摘要

### 1.1 总评分：79/100

| 维度 | 得分（100 制） | 判断 |
|---|---:|---|
| 架构方向 | 82 | Core/Capability/Provider/Adapter 方向正确；router seam 与少数类型边界仍可收敛。 |
| 运行时正确性 | 82 | SessionEvent、ToolExecutor、Operation Ledger、Recovery 语义强；provider marker 与部分写入入口仍有维护风险。 |
| 可替换性 | 78 | Memory/Artifact/Model/Observability 已有 seam；failure taxonomy 仍有字符串耦合。 |
| 可观察性 | 84 | JSONL、RunTracer、Langfuse 旁路、trace_id/trace_url 回填优秀；显式 NullTracer 仍可降低 Runtime 分支。 |
| 可测试性 | 79 | Gate、kill/resume、ToolExecutor 测试扎实；router/运行策略局部回归成本高。 |
| 复杂度/维护性 | 66 | `runtime.py`、`web/app.py` 有明确 GLM 风格低质量工件；但不能把所有长文件或正确重复都判作缺陷。 |
| 规格一致性 | 88 | 冻结不变量大部分落地；整改重点是守护第二路径，不是改写产品方向。 |
| **总评** | **79/100** | **基线可继续演进；先处理可验证的入口/契约债务，再最后处理 `_drive`。** |

评分是对轻量、透明、可恢复、可替换的长期工程质量评分，不是“代码能否运行”的分数。Phase 0–16 Gate 证明功能基线强，但不能反推结构债务不存在；同样，存在 GLM 工件也不等于项目需要推倒重来。

### 1.2 最重要结论

1. **最强资产是语义不变量**：SessionEvent append-only、`ToolExecutor` 唯一执行路径、Ledger-first Recovery、UNKNOWN 不盲重跑、Capability 显式 wiring、Langfuse 故障隔离，均与 `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md:93-118` 对齐。
2. **当前最值得先做的是低到中风险的契约收敛**：router seam、`tool_scope` 对账、NullTracer、provider failure marker 下沉，以及 cwd/Session 写漏斗红证；这些改动可二值验证，且能为后续核心重构减枝。
3. **`_drive` 确实复杂，但必须最后做**：它的 blast radius 覆盖模型、工具、流式、checkpoint、memory 与终态；在前置 seam 未稳定前把它列为 Top1 会放大风险。
4. **没有 env 配置散乱问题，也没有 session→web 运行时循环依赖**：全仓只有 6 个直接环境读取表达式，服务不同原生职责且无变量被两个模块重复读取；`AppState` 只在 `TYPE_CHECKING` 下出现，问题仅是 P2 类型所有权。
5. **最容易复发的是小型第二路径和复制粘贴工件**：手写 tool scope、runtime provider markers、事件镜像、artifact 读取双工具、重复 CSP 常量/回调闭包等，应该用表驱动、单一 helper 和红证收敛。

## 2. 审计方法与判定口径

### 2.1 规格基线

- 总体原则：`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/00_PROJECT_VISION.md:93-118`。
- 分层和依赖方向：`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/01_SYSTEM_ARCHITECTURE.md:36-58`。
- 统一 Tool 链：`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/01_SYSTEM_ARCHITECTURE.md:134-161`。
- Open-source 复用矩阵：`goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/13_OPEN_SOURCE_REUSE_MATRIX.md:63-135`。
- 当前 Phase 状态：`docs/PHASE_STATUS.md:16-36`，Phase 0–16 已完成，基线为全量 `1222 passed / 1 skipped / ruff clean`，后续巡检又已进入更高测试规模。

### 2.2 “GLM 风格低质量代码”与本仓具体工件

这里的“GLM 风格”不是对模型或作者的标签，而是对可重复的低质量代码形状的工程称呼。以下是本仓可精确定位的工件，不把所有长函数或 defense-in-depth 误判为问题：

| 工件 | 证据 | 问题 | 最小处理 |
|---|---|---|---|
| CSP 常量重复 | `src/agent_harness/web/app.py:746-771` 定义并消费模块级 `_CSP_POLICY`；同一函数内又在 `app.py:955-962` 重写同名字面量 | 复制粘贴缓存，改策略可能只改一处 | 删除函数内重定义，只保留模块级 owner |
| `_launched_response` 嵌在巨型 factory | `web/app.py:1843-1859,1895-1918` | helper 本身正确地统一两入口，但被闭包困在 `create_app`，不可独立测试/复用 | 随 router seam 移到模块级/route adapter |
| approval callback 两个重复闭包 | `src/agent_harness/assembly.py:255-265` | approve/deny 仅返回值不同，却重复 async def 且使用 `type: ignore[no-redef]` | 一个 factory/闭包按布尔返回 |
| fallback 重复 `return stream` | `src/agent_harness/model/fallback.py:143-160`，`return stream` 连续两次 | 明确 dead statement | 删除不可达重复行并加 lint/覆盖 |
| coordinator 死条件/重复 branch | `_RECONCILE_STATES` 含 `NEED_RECONCILE`（`recovery/coordinator.py:103-107`），但 `_reconcile_one` 只分别处理 RUNNING/UNKNOWN（`:476-486`）；tool/call + tool/result 合成在 `:272-314` 与 `:536-548` 两处重复 | 状态前提靠调用方暗示，事件镜像重复，后续加字段易漂移 | 提取状态推进与合成 helper；明确 NEED_RECONCILE no-op 分支 |
| 事件镜像维护面 | `agent/runtime.py` 的每次 `session.append` 后手动 `yield to_agent_event`，集中见 `runtime.py:722-1215` | durable append 与 stream mirror 两步靠人工配对 | 先加 event-sequence red test，再考虑 helper；不改变持久化/广播分离原则 |
| `read_artifact` / `inspect_artifact` clone | `tools/read_artifact.py:1-81` 与 `tools/inspect_artifact.py:23-72` 的参数、描述、局部读取与 reconcile hint 同形；选择器见 `storage/artifact_select.py:87-117` | provider 历史命名导致实现克隆 | 共享参数/执行 core，保留两个工具名兼容面 |
| ProviderStore 局部重复 import | `assembly.py:209-212`、`session/service.py:506-509,1513-1517`，同时 `web/app.py:52` 已模块级 import | 循环依赖防线与随手局部 import 混在一起，所有权不清 | 只保留有依赖理由的局部 import；其余统一 composition seam |
| compactor magic caps | `context/compactor.py:43-60,153-187`：`20_000`、`30.0`、`0.15`、`16384`、200/100/300 | 上限各有合理性但没有统一命名/配置口径，难以实验和审计 | 命名常量/预算对象；不机械全部暴露为 env |
| provider failure strings 在 Runtime | `agent/runtime.py:101-183` 的 `_PROVIDER_FAILURE_MARKERS` 与固定文案 | 模型 adapter 语义泄漏进 Loop；Runtime 用 `str(error)` 分类 | 下沉到 `model` failure classifier，Runtime 只消费稳定 reason |
| DSML marker 在 Runtime | `agent/runtime.py:86-100,899-905` | provider wire-protocol 泄漏守卫污染 Agent Loop | 下沉到 model adapter/normalizer，保留同一失败语义 |

这些工件的共同特征是“能跑，但知识在错误层或有第二份”。修复完成标准是减少知识副本和漂移路径，不是单纯减少行数。

## 3. Top 10 架构/质量发现总览

| 排名 | 发现 | 严重度 | 质量判断 | 是否现在值得改 |
|---:|---|---|---:|---|
| 1 | FastAPI router 仍大量闭包化在 `create_app` | P1 | 6.6/10 | 是；先迁一个低风险 router，保持 Contract。 |
| 2 | `tool_scope` 是手写集合，已有 `forget_memory` 被静默剔除真事故 | P1 | 6.8/10 | 是；表驱动对账，收益高、风险低。 |
| 3 | Tracer 缺少显式 Null Object，Runtime 保留大量观测判空 | P1 | 7.0/10 | 是；先建 Protocol + NullTracer。 |
| 4 | provider failure/DSML 的 provider 知识停留在 Runtime | P1 | 6.7/10 | 是；下沉 model 层，保持 reason Contract。 |
| 5 | cwd 续聊归属与 Session durable 写入口仍需机械对账 | P1 | 7.1/10 | 是；主代理实施 cwd，写漏斗分步推进。 |
| 6 | Capability wiring 已表驱动但 metadata 双表可漂移 | P2 | 7.5/10 | 是；合并 typed spec，不做反射发现。 |
| 7 | JSONL 读取、Bash timeout、Web git 等边界仍需合同化 | P2 | 7.2/10 | 是；拆成独立小票。 |
| 8 | AppState 仅存在 `TYPE_CHECKING` 类型所有权问题 | P2 | 7.5/10 | 可改；不是运行时循环，不应夸大。 |
| 9 | Recovery 锁内人工裁决有可用性耦合 | P2/P1-risk | 7.2/10 | 先测量与加 deadline；两阶段重排需后置。 |
| 10 | `_drive` 复杂度高但受强 Gate 保护 | P2/high-risk | 6.5/10 | 最后做；前置 seam 完成后只抽一刀。 |

**明确非 finding**：env 配置散乱。全仓直接环境读取只有 6 个表达式，分布在 `instance_lock.py:75,159`、`mcp/config.py:82`、`mcp/client.py:71`、`sandbox/local.py:137,155`；它们分别服务共享根逃生门、`${VAR}` 秘密解析、MCP stdio allowlist、sandbox allowlist 与 Windows `COMSPEC`，没有同一个变量被两个模块重复读取。用户配置仍由 `Settings` 拥有；因此当前不值得为 env 新造 port 或迁移。

## 4. Top 5 必须优先处理

### Top 1：FastAPI router seam

- **证据**：`src/agent_harness/web/app.py:836+` 的 `create_app` 仍内联大量 Session/Run/Queue route；正例是 `web/workspace_files.py:277-284`、`web/model_providers.py:144-149` 的 `register_*_routes`。`_launched_response` 虽正确统一 `/messages` 与 `/queue/flush`，却被困在 factory 闭包（`app.py:1843-1859,1895-1918`）。
- **耦合**：HTTP schema、AppState、SessionService、SSE helper 和 route registration 同处一个函数。
- **后果**：新增/审查端点容易冲突；闭包 helper 难以独立测试；重复 `_CSP_POLICY`（`app.py:746-771,955-962`）表明 factory 内知识副本已经出现。
- **修法**：先迁一组低风险 route 到 `register_*_routes`；依赖以参数/Protocol 注入；保留 path/status/schema/Depends/OpenAPI。
- **文件**：`src/agent_harness/web/app.py:836-1951`、既有 `web/workspace_files.py`/`model_providers.py` 模式；`tests/web/`。
- **Blast radius**：中高；Web adapter，不触碰 Core 语义。
- **风险**：中；闭包捕获和 monkeypatch seam 可能漂移。
- **现在是否值得改**：是；这是可逐路由施工且可用 OpenAPI diff 二值验证的最高收益项。
- **测试**：OpenAPI before/after、端点错误矩阵、router fake-state 单测、Web 全量。

### Top 2：`tool_scope` 表驱动对账

- **证据**：`src/agent_harness/agent/profiles.py:58-108` 用 `_CODING_TOOLS`、`_RESEARCH_TOOLS`、`_MAIN_TOOLS` 三组手写集合；`profiles.py:75-80` 明记 #202：`forget_memory` 曾“不在任何 tool_scope、被非 main 档位 `registry.filtered()` 静默剔除”的真实事故。`profiles.py:126-151` 又证明声明面与实际注册面会双向不一致。
- **耦合**：内置 profile、Capability wiring、`registry.filtered`、Web catalog、SubAgent grantable scope。
- **后果**：新工具可存在但对所有档位不可用；UI 声明开放数量可能高报；权限/可用性靠人记得同步。
- **修法**：用单一 table/fixture 对账 profile→tool declaration；测试 `effective = declared ∩ registered`、dropped tools、越权拒绝、新工具未归属即红。不要把声明面伪装成部署实际面。
- **文件**：`agent/profiles.py:58-159`、`assembly.py:276-287`、`agent/factory.py:81-97`、相关 profile/wiring tests。
- **Blast radius**：中高；权限、UI、SubAgent。
- **风险**：低到中；测试先行可避免扩大权限。
- **现在是否值得改**：是；已有真事故且改动可表驱动。
- **测试**：参数化三 profile、optional capability 缺席、fake 新 tool、child 越权。

### Top 3：Tracer + NullTracer

- **证据**：`observability/tracer.py:85-159` 的 `RunTracer` 已让缺席 sink 安全 no-op，但 `agent/runtime.py:686,738-746` 仍以 `tracer=None` 起步，后续在 model/context/terminal 路径大量 `if tracer is not None`。
- **耦合**：Runtime 控制流知道 observability 是否启用；ToolExecutor 的 span 句柄也需判空。
- **后果**：每加一条运行路径都要记得观测判空与对称收尾；这扩大 `_drive`，也阻碍替换非 Langfuse tracer。
- **修法**：最小 `Tracer` Protocol + `NullTracer`；Runtime 始终拿到对象；RunTracer 继续作为 Langfuse adapter，Sink 熔断不动。
- **文件**：`observability/tracer.py`、`observability/__init__.py`、`agent/runtime.py`、`tooling/executor.py`、observability/runtime tests。
- **Blast radius**：中；观测是旁路，外部 Contract 不应变化。
- **风险**：中低；空 handle 形状须覆盖 tool/context/model。
- **现在是否值得改**：是；这是 `_drive` 前置减枝。
- **测试**：NullTracer 全生命周期、failing sink 不污染 run、trace_id/trace_url golden、Phase 15 Gate。

### Top 4：provider failure strings 与 DSML marker 下沉到 model

- **证据**：`src/agent_harness/agent/runtime.py:101-183` 定义 `_PROVIDER_FAILURE_MARKERS`、reason/message 和 `_classify_provider_failure(str(error))`；同文件 `runtime.py:86-100,899-905` 直接知道 DeepSeek DSML wire marker。`model/fallback.py:44-67` 已是正确的模型失败分类层。
- **耦合**：Agent Loop 直接知道厂商错误码词片、账户/鉴权文案和 DSML wire protocol；provider 适配知识跨 Runtime/model 两层。
- **后果**：新增 provider failure 或 wire guard 必须改 Agent Loop；Runtime 的字符串匹配难复用；Provider substitutability 被削弱。
- **修法**：把 marker/message/classifier 与 DSML 规范化/拒绝逻辑移入 `agent_harness.model`；Runtime 只消费稳定 `reason/readable_message` 或 typed failure。Web provider connection test 的 `_classify_failure` 是另一个 adapter 消费者，但不是本 finding 的核心。
- **文件**：`agent/runtime.py:86-183,899-905`、候选 `model/failure.py`/adapter、`model/fallback.py`、runtime/model tests。
- **Blast radius**：中高；失败归因、fallback、终态事件。
- **风险**：中；旧 reason/message 与脱敏边界必须逐字兼容。
- **现在是否值得改**：是；先搬 owner，不改分类行为。
- **测试**：全部 marker 参数化、未知错误保持类型名兜底、DSML 不产生 run/completed、敏感原文不进 SessionEvent。

### Top 5：cwd 对账 + Session 单一写漏斗

- **证据**：cwd 已有正确锚点 `session/cwd.py:1-57` 与唯一创建参数 `session/session.py:150-184`，但需证明 Web/CLI/fork/续聊全入口一致；durable 写入核心 `Session.append` 在 `session.py:278-330`，旁路/历史移植入口还包括 `Session.append_event`（`:187-218`）和 `adopt_history`（`:332-352`）。
- **耦合**：Session header、WorkspaceIndex/Registry、Web/CLI、fork、Recovery、memory writeback。
- **后果**：cwd 冲突可能造成会话归属不可信；新增 durable writer 可能绕过词汇表/seq/硬删守卫。
- **修法**：主代理先完成 cwd create→resume→follow-up 对账；随后建立 durable append 调用图和 typed writer command，分步迁移，不把 Ledger 合入 SessionEvent。
- **文件**：`session/cwd.py`、`session/session.py`、`session/store.py`、`session/service.py`、`session/fork.py`、Recovery/Session tests。
- **Blast radius**：高。
- **风险**：中高；必须拆票，不一次迁完。
- **现在是否值得改**：是；以红证和调用图为先。
- **测试**：cwd 不可变/冲突/历史兼容；seq conflict、硬删迟到写、fork 父不变、Recovery pair consistency。

## 5. Top 10 逐项审计

以下每项均给出证据、耦合、后果、修法、文件、Blast radius、风险、是否现在值得改和测试要求。

### 5.1 `_drive` 巨型运行策略函数（最后做）

`runtime.py:657-1355` 的每一段都触碰不同事实源，复杂度是真问题；但已有 Agent/Recovery/Phase 16 强 Gate，且任何移动都会影响 yield/discard、checkpoint、fallback、Tool pair 和 memory。因此它排 Top10 第 10，不是当前 Top1。

- **证据/耦合**：`runtime.py:778-1215` 同时处理 steer、context、model stream/fallback、tool、checkpoint、熔断和 memory；`:1223-1337` 的取消/异常两臂有不同 yield 约束。
- **后果**：未来新增终态容易漏臂，但现在贸然拆解的预期风险高于立即收益。
- **修法**：先完成 router/tool_scope/Tracer/provider/write seam；最后只提取一个高内聚阶段，并锁事件序列 golden。
- **文件**：`agent/runtime.py:657-1355`、`agent/streaming.py`、`model/fallback.py`、Agent/Phase16 tests。
- **Blast radius/风险**：极高/高。
- **现在是否值得改**：否，后置到 Roadmap 第四阶段。
- **保留**：`AgentRuntime.run()`/`run_stream()` 仍是 Core 唯一 Loop；SubAgent 继续复用它；不引入 LangGraph 取代 Loop。

### 5.2 FastAPI `AppState` 与 router seam

见 Top 1。需要准确区分两件事：`session/service.py:128-132` 已把 `from agent_harness.web.app import AppState` 限制在 `TYPE_CHECKING`，因此**没有运行时 session→web 循环依赖**；剩余问题是 P2 类型所有权——`SessionService.__init__(state: AppState)`（`service.py:271-279`）仍以 Web 容器作为名义类型，且路由大量内联在 `app.py:create_app`。

- **修法优先级**：先建立 router seam；再视实际属性访问把构造参数抽成最小 Protocol/application port。不要把它描述成已发生的 runtime cycle。
- **保留**：`create_app(settings)` 作为唯一 FastAPI factory；AppState 仍是 Web 生命周期 owner。
- **测试**：`TYPE_CHECKING` import graph、router registration smoke、OpenAPI snapshot；类型反转是 P2，不能阻塞 router 小步迁移。

### 5.3 Session 写入单一漏斗

`Session.append` 的现状是优秀基线，不应被审计误判为不存在。问题是入口面和“何时使用哪个入口”没有一个类型级 command taxonomy。

- **耦合**：`session.py`、`store.py`、`fork.py`、`recovery/coordinator.py`、`memory/writeback.py`、CLI/Web。
- **修法**：统一 durable append port，保留底层 Store IO 和 Recovery 的特定编排。
- **现在是否值得改**：是，先只增加审计/静态约束，避免一次全迁移。

### 5.4 Recovery 锁内人工裁决

当前安全性是对的：`coordinator.py:240-269` 在没有 callback 时整体拒绝，不写伪造结果；`coordinator.py:303-314` 明确处理人工裁决。问题是锁的时间域。

- **耦合**：数据库级互斥 + 人类 latency。
- **后果**：可用性/吞吐问题而不是“会盲重跑”的正确性问题。
- **修法**：两阶段 reconcile，必须保留 verdict 与 operation identity。

### 5.5 provider failure 与 DSML 的层级错误

本 finding 的核心不在 Web 连接测试，而在 Agent Runtime 持有 provider 知识：`agent/runtime.py:101-183` 的 `_PROVIDER_FAILURE_MARKERS`/文案/classifier，以及 `runtime.py:86-100,899-905` 的 DSML wire marker。`model/fallback.py:44-67` 已证明 model 层是合适 owner。

- **耦合**：Loop 知道厂商错误词片和 wire protocol。
- **后果**：Provider substitutability 降低，新增 provider 需要修改 Runtime。
- **修法**：行为等价地下沉 `model` classifier/normalizer；Runtime 只消费稳定 reason/readable message。
- **测试**：旧 reason/message golden、全部 marker、unknown fallback、DSML 不完成 run、脱敏边界。

### 5.6 `cwd` 续聊和工作区归属

- **证据**：`src/agent_harness/session/cwd.py:1-17,29-57` 已将 cwd 写入首个 `session/started`，只读第一条，旧日志返回 `None`；`session/session.py:150-184` 只接受显式 `cwd` 参数并规范化；`session/service.py:128-132` 仅 TYPE_CHECKING 导入 AppState；工作区 Index 的 SessionHeaders Protocol 位于 `workspace/index.py:38-47`。
- **耦合**：创建入口 Web/CLI/Fork、Session header、WorkspaceIndex、WorkspaceRegistry、续聊 `resume_and_launch`。
- **后果**：若续聊只按 sandbox mapping 恢复而不把 cwd 作为 session 事实校验，会出现“会话能跑但归属不可信”；旧会话必须保留未分组语义，不能猜测回填。
- **修法**：`356c531` 已完成 durable cwd 续聊基础修复；`4dbe7db` 增加外部 cwd 缺失时 fail-closed（不重建空目录）。仍待后续票验证真实 Registry mapping/cache 冲突与跨入口矩阵。
- **文件**：`session/cwd.py:29-57`、`session/session.py:150-184,221-274`、`session/service.py:600+`、`workspace/index.py:38-97`、`tests/session/test_session_cwd.py`、权限/续聊测试。
- **Blast radius**：高；会话、项目、sandbox、Web/CLI。
- **风险**：中；错误修法会把 cwd 变成可变配置或误删历史归属。
- **现在是否值得改**：基础 bug 已修复；mapping/cache 对账与跨入口矩阵仍由 #237 后续范围跟进，不能宣称全部闭合。
- **测试**：create→resume→follow-up cwd 不变；fork 子会话按明确 policy；历史无 cwd 返回 None；相对/不存在/空 cwd 拒绝或保持既有语义；Web/CLI 同源。

### 5.7 `tool_scope` 声明面与注册面不一致

- **证据**：`src/agent_harness/agent/profiles.py:58-108` 的三个工具面是手写 `frozenset`；`profiles.py:75-80` 明确记录 `forget_memory` 曾不在任何 scope、被非 main 档位静默剔除的 #202 真事故。`profiles.py:111-159` 的 `tool_scope_summary` 又诚实说明声明面与注册面双向不一致；`assembly.py:276-287` 只在运行时计算 `dropped_tools`。
- **耦合**：AgentProfile 常量、Capability wiring、registry.filtered、Web catalog、run/started/run_config。
- **后果**：UI 若把声明开放说成实际开放就误导用户；新增 tool 可能不在任何 profile 或被静默剔除；权限收窄的安全语义不应由 tooltip 猜。
- **修法**：table-driven 定义 profile→tools，生成 declaration universe、catalog、运行时对账 fixture；所有 profile 至少在测试中验证 `effective = declared ∩ registered`、被丢弃工具可追溯、未知 scope 不升级权限。
- **文件**：`agent/profiles.py:58-159`、`assembly.py:276-287`、`capability/manifest.py`、`web/app.py:129-138`、`tests/capability/test_wiring.py`、`tests/agent/test_profiles_factory.py`。
- **Blast radius**：中高；权限、UI、SubAgent。
- **风险**：中；错误地把 union 当 effective 会造成安全/可用性错误。
- **现在是否值得改**：是；小范围、收益高。
- **测试**：新增 tool 时缺 profile 自动红；声明多于注册为降级；注册多于声明可见 dropped；coding/research 不可越权；动态 child 只能收窄。

### 5.8 wiring 的显式表与重复 shim

- **证据**：`src/agent_harness/capability/wiring.py:97-479` 有多个 `_wire_*` 函数；`wiring.py:469-512` 已有 `_BUILTIN_WIRING` 与 provider 白名单二张按 capability 索引表；`wiring.py:564-576` 有统一贡献收集循环。
- **耦合**：wiring table、provider allowlist、failure degradation、Lifecycle owner、tool contributors。
- **后果**：新增 capability 需改两表甚至多处 wrapper；漏一处可能从“配置错”变成“未知 provider”或静默缺席；不过显式装配本身符合规格，不应换成反射扫描。
- **修法**：保留显式 table-driven wiring；把 handler、degradation、provider names、生命周期策略合成一个 typed `WiringSpec`；保留特殊 provider（memory factory）显式 seam。统一工具 contributor 接口。
- **文件**：`capability/wiring.py:42-76,469-576`、`capability/factories.py`、`tests/capability/test_wiring.py`。
- **Blast radius**：中；所有 optional capability。
- **风险**：中低；过度泛化会隐藏 capability-specific failure 语义。
- **现在是否值得改**：是，但只做 table 对账，不做动态插件框架。
- **测试**：每个 table entry 可发现、provider 白名单同键；OPTIONAL/REQUIRED 失败分类；tool contributor 只进 ToolRegistry；aclose 逐项隔离。

### 5.9 JSONL、Bash timeout、git 工具路径协议分散

- **证据 JSONL**：`session/store.py:92-179,236+` 定义 JSONL store、同进程 seq 锁和 fsync；但 `cli.py:393,474`、`multiagent/provider.py:226`、`recovery/scan.py:157`、`session/fork.py:154`、`session/lineage.py:27` 等多处直接调用 `read_events`。
- **证据 Bash**：`tools/bash.py:86-132` 通过 `asyncio.to_thread`、`cancel_event`、sink；真正 timeout/kill 契约在 Sandbox 后端，不在 `_BashArgs` 中显式表达。
- **证据 git**：`tools/git.py:52-101` 已有命令构造与执行单点；`web/workspace_files.py:253-274,351-403` 复用它并添加 scope/边界，这部分是优秀修复基线，但 scope、边界、Web 解析仍分三层。
- **耦合/后果**：基础契约分散时，新入口会绕过 fsync、timeout、workspace scope 或错误映射；跨 shell 的安全白名单更容易被第二实现破坏。
- **修法**：JSONL 读取统一成 typed `SessionReader`（summary/header/full/recovery mode 明确）；Bash 为 Sandbox/ToolResult 规定 timeout、cancel、exit_code 三值契约；git 保持 `tools/git.py` 为唯一命令路径，Web 只传 checked scope。
- **文件**：`session/store.py`、`tools/bash.py`、`sandbox/base.py`、`sandbox/local.py:280-330`、`tools/git.py`、`web/workspace_files.py`。
- **Blast radius**：中高；IO、恢复、安全、Web。
- **风险**：中；不能把“命令 exit 非零”误化为 Tool failure，也不能丢 stdout/stderr artifact 语义。
- **现在是否值得改**：是，按三个独立 ticket 做。
- **测试**：JSONL 半行/坏行/seq；Bash timeout 杀进程树且 no late side effect；git 嵌套仓库 scope、path traversal、shell meta；Web 与 Tool 结果同形。

### 5.10 env 无散乱问题；LangChain 是 accepted coupling

- **env 实测结论**：全仓只有 6 个直接读取表达式：`instance_lock.py:75,159`、`mcp/config.py:82`、`mcp/client.py:71`、`sandbox/local.py:137,155`。没有同一个变量被两个模块重复读取：`ALLOW_SHARED_ROOT_ENV` 同模块同职责；`${VAR}` 是 MCP 显式秘密引用；MCP/Sandbox 各自读取不同 allowlist；`COMSPEC` 是 Windows shell 原生选择。用户配置仍集中在 `config.py:14-138` 的 `Settings`。因此**没有配置散乱 finding，当前不值得收敛或新增 environment port**。
- **LangChain 证据**：`model/provider.py:8` 使用 `langchain_openai.ChatOpenAI`；`model/fallback.py:29` 使用 `langchain_core.messages.AnyMessage`。规格 `goal/.../13_OPEN_SOURCE_REUSE_MATRIX.md:77-80` 明确允许 `ADAPT`。
- **LangChain 后果**：只要 concrete 类型留在 model adapter/消息兼容层，不拥有 Loop、不做 Tool retry，就不违反架构。
- **修法**：env 不改；仅保留现有安全 allowlist 测试。LangChain 不替换，只补 provider substitution/import-boundary 测试。
- **Blast radius**：env 无整改；LangChain 边界测试低风险。
- **现在是否值得改**：env 否；LangChain 只值得加护栏，不值得迁移。
- **测试**：6 个读取点 inventory；同变量重复读取静态断言；MCP/Sandbox allowlist；Fake ModelProvider 不要求修改 Agent Loop。

## 6. 必须保留的优秀设计

审计不是只找坏处。以下设计是项目的核心资产，整改时不得回退。

### 6.1 SessionEvent / JSONL / derive projection

- `session/session.py:278-330` 先校验事件类型、拒绝 stream-only、落盘成功后推进 seq，且 listener 异常不破坏 append。
- `session/store.py:164-179` 以 flush + fsync 为耐久性底线，并明确同进程与跨进程边界，不虚假宣称跨进程安全。
- `session/cwd.py:45-57` 读取第一条 `session/started`，旧日志返回 `None`，不猜测回填。
- 这与规格的“Persistent History ≠ Runtime Context”“Event ≠ Log”完全一致。

### 6.2 Tool Runtime 和 `_ToolFailure`

- `goal/.../01_SYSTEM_ARCHITECTURE.md:147-161` 要求统一 Tool 路径，当前本地 Coding、Capability、MCP、Memory、SubAgent 都进入 ToolRegistry/ToolExecutor；`assembly.py:266-275` 是统一注册点。
- `src/agent_harness/tooling/executor.py:83-171` 的 `_ToolFailure` 把 `error_code/retryable/message` 收成单一事实，使用**异常类型**而不是字符串分类；尤其 `executor.py:124-155` 按 side effect 决定 timeout 是否可重试，避免教模型盲重跑 MUTATING 工具。
- `profiles.py:7-11` 与 `agent/factory.py:81-97` 对 scope 做收窄/越权拒绝。Prompt 不是权限边界，当前方向正确。

### 6.3 RunManager detached-run 生命周期

`src/agent_harness/web/runmanager.py:220-345` 把 launch/subscribe/cancel/孤儿回收/关停收进一个 owner；`runmanager.py:267-292` 在 finally 中移除 listener、finish、抓 context snapshot、释放 runtime、触发终态接力；`runmanager.py:314-332` 明确终态回调失败不污染已落盘 run。这个分离保证 Web 订阅不是 Session 第二真相源，必须保留。

### 6.4 StreamDecoder 与 Sandbox 路径边界

- `src/agent_harness/sandbox/decoding.py:50-175` 的 `StreamDecoder` 以 UTF-8 strict probe、OEM fallback、ASCII 前缀即时放行、粘性判定和通用换行归一解决 Windows/容器真实字节流问题；这是经过现场 bug 驱动的可靠设计，不应被“直接 decode(errors=replace)”替代。
- `src/agent_harness/sandbox/base.py:148+` 的 `resolve_within_workspace` 是统一路径边界；Local/Docker/Web git 都复用该判定。Web 早拒绝与 Service/Sandbox 信任边界属于 defense-in-depth，不是无意义重复。

### 6.5 PromptRegistry 的 fail-fast 设计

`src/agent_harness/prompt/registry.py:55-159` 对 section 名、重复注册、scope、模板变量、profile identity、target 分区做集中校验；`registry.py:177-193` 启动时自检所有 scope。它避免 persona/tool/aux prompt 混装和静默覆盖，是“配置错误启动期响亮失败”的优秀实现。

### 6.6 Recovery / Ledger-first / UNKNOWN

`recovery/coordinator.py:16-31` 清楚保存 8 步顺序；`coordinator.py:240-269` 没有 callback 时安全拒绝；`coordinator.py:291-301` 修正 PENDING 的 Ledger 状态，避免 Session 与 Ledger 永久不一致。这个设计比“失败就重新跑”高一个数量级，必须优先保护。

### 6.7 Capability Provider 可替换

`capability/wiring.py:102-143` 通过 factory/provider 参数构造 memory；`wiring.py:354-373` 特别避免把工具塞进 LangMem concrete class；`PHASE_STATUS.md:24-27` 记录了 Fake Provider、真实向量库和 graceful degradation Gate。Memory = Capability + Context Provider 的边界已经是本项目资产。

### 6.8 Observability 旁路故障隔离

`observability/sink.py:1-15,59-107` 实现 key 缺失零 import、初始化失败永久禁用、SDK 异常吞掉并进诊断账本、熔断丢弃；`tracer.py:85-159` 让缺席 sink 时 RunTracer 安全 no-op；`tracer.py:226-258` 对 completed/failed 对称回填 trace_url。不得把 Langfuse 变成 Core 必须依赖。

### 6.9 配置与 provider 边界

`src/agent_harness/model/provider_store.py:1-19,66-79,176-224` 将非密 provider 配置放 JSON、密钥走 Credentials/keyring seam，并支持 `MemoryCredentialStore` 替换；局部 import 是为避免 `model/config.py` 与 provider store 成环的明确边界，不应误判为无条件循环依赖。

### 6.10 Artifact 选择、统一 envelope 与安全 git 路径

`storage/artifact_select.py:87-117` 将 `read_artifact`/`inspect_artifact` 的 provider 配对集中在选择器，避免写入 A、读取 B；`web/serialization.py:23-67` 统一 live/replay envelope；`tools/git.py:52-101` + `web/workspace_files.py:253-274` 统一命令构造、pathspec 白名单和 workspace scope。Artifact 两工具仍有 clone 债务，但配对选择与安全边界是正确资产。

### 6.11 规格与开源复用纪律

项目明确把 LangChain/官方 provider clients 设为 `ADAPT`，MCP SDK 设为 `REUSE + ADAPT`，而将 Agent Loop、Operation Ledger、SessionEvent、ContextBuilder integration 设为 `BUILD`（`goal/.../13_OPEN_SOURCE_REUSE_MATRIX.md:63-135`）。这不是保守，而是保持差异化语义所有权的正确决策。

## 7. Provider substitutability 审计

### 7.1 当前状态

| 能力 | seam | 现状 | 可替换性 |
|---|---|---|---:|
| Model | `ModelProvider`/`create_chat_model`/fallback policy | 兼容 OpenAI 协议，LangChain adapter；failure 词汇仍需统一 | 7/10 |
| Memory | `MemoryCapability`、factory、ContextProvider、Fake | seam 真实存在，LangMem 不写死 Core | 8.5/10 |
| Artifact | Local/S3/MinIO selection + read tool | 选择器已有单点，需继续守住读写配对 | 8/10 |
| Vector/Knowledge | Knowledge store protocol + Milvus adapter | Knowledge 与 Memory 分域，正确；外部失败需统一 taxonomy | 7.8/10 |
| Web Search | `RetrievalProvider`/Tavily adapter | provider 薄，fallback 由策略管理 | 8/10 |
| MCP | official SDK + `MCPToolAdapter` | transport/protocol 复用，ToolExecutor 统一 | 8.3/10 |
| Observability | `LangfuseSink` + `RunTracer` | optional、旁路、故障隔离优秀；建议 NullTracer 显式化 | 8/10 |
| Orchestration | `OrchestrationAdapter` | protocol-only，LangGraph optional | 8.5/10 |

### 7.2 可替换性的硬判据

一个 Provider 只有满足以下条件才算可替换，而不是“有个 Protocol”：

1. Core 不 import concrete provider class。
2. provider failure 能翻译为稳定的 typed error，不靠 message string。
3. 缺失/故障有明确 `NOT_CONFIGURED`、`OPTIONAL_RUNTIME`、`REQUIRED_CORE` 或 `OPTIONAL_OBSERVABILITY` 语义。
4. 替换为 Fake Provider 后，不修改 Agent Loop/SessionEvent/ToolExecutor。
5. 外部副作用进入 Operation Ledger，Provider 自己不能绕过统一执行路径。
6. Provider 的生命周期由 wiring owner 管，初始化半失败可关闭，shutdown 故障隔离。
7. Provider 的大对象输出走 Artifact，不将具体 SDK response 直接塞进 Model Context。

### 7.3 LangChain accepted coupling

本审计**不把 LangChain 依赖列为必须移除的缺陷**。接受理由：

- 规格已经批准 `ADAPT`（`goal/.../13_OPEN_SOURCE_REUSE_MATRIX.md:77-80`）。
- `model/provider.py:8-131` 的职责是把 `ModelConfig` 变成 ChatModel，不拥有 Agent Loop、不做 retry/cache。
- `model/fallback.py:1-19` 明确 Model Fallback 与 Tool Retry 分离。
- 真实风险是类型泄漏和异常分类泄漏，而不是“用了 LangChain”。

**边界要求**：LangChain 可以存在于 `model` adapter 和兼容消息转换层；Core Contract 不能要求 `ChatOpenAI`、`AnyMessage` 或 LangChain agent executor；不能用 prebuilt agent 隐藏自己的 Loop。整改票只守边界，不做框架迁移。

## 8. 目标架构

```text
Surface adapters
  CLI / FastAPI routers / SSE / Web
          │ typed request + typed response
          ▼
Application ports
  SessionApplication / RunManager / ApprovalPort / RecoveryPort
          │
          ▼
Core runtime
  AgentRuntime (thin phase driver)
  Session + SessionEvent + ContextBuilder
  ToolRegistry → Permission → Scheduler → ToolExecutor
          │                         │
          │                         └── Operation Ledger / Artifact ref
          ▼
Capability ports
  ModelProvider / Memory / Knowledge / Web / MCP / SubAgent / Observability
          │
          ▼
Adapters/providers
  LangChain-compatible model adapter, LangMem, Milvus, MinIO/S3,
  MCP SDK, Tavily, Langfuse, Local/SQLite
```

### 8.1 依赖方向

- `core` 只依赖 contracts、标准库和抽象 port。
- `session` 不运行时依赖 `web.app.AppState`；如果需要类型名，只允许 `TYPE_CHECKING` 或 Protocol。
- `web` 依赖 application ports，不反向成为领域服务的类型 owner。
- `assembly` 是 composition root；Provider 具体实现只在 adapter/provider/assembly 进入。
- `LangGraph` 只能包裹 `AgentRuntime.run()`，不能替代 ToolExecutor/Ledger。
- `SessionEvent`、Diagnostic Log、Operation Ledger、Artifact Store 四种事实源逻辑分离。

### 8.2 单一真相源

| 事实 | 唯一 owner |
|---|---|
| durable conversation fact | SessionEvent/JsonlSessionStore |
| runtime model context | ContextBuilder projection |
| external side effect | Operation Ledger |
| large output | ArtifactStore |
| provider failure category | typed ProviderFailure taxonomy |
| capability registration/degradation | typed WiringSpec + CapabilityWiring |
| tool permission | Tool Contract/PermissionPolicy/Executor |
| trace identity | RunTracer + terminal SessionEvent fields |
| HTTP presentation | router serializer；不产生第二套 Session state |

## 9. 四阶段整改 Roadmap

### 阶段一：止血与契约守护（1–2 个短批次）

目标：先处理证据明确、blast radius 可控的 Top5。

- FastAPI router seam：先迁一个低风险 route group，并消除重复 CSP owner。
- `tool_scope` table-driven 对账，锁住 `forget_memory` 同类事故。
- `Tracer + NullTracer` 明确 no-op 端口。
- provider failure markers 与 DSML guard 下沉 model，保留旧 reason/message。
- cwd 续聊对账（由主代理实施）+ Session 写入调用图/静态护栏。
- wiring table key/provider/degradation 一致性测试。

Gate：OpenAPI 无未披露变化；新增 profile/tool/provider 漏登记时测试变红；Null/failing observability 不影响 Core；provider marker/DSML 事件序列不变；全量 `pytest` 与 `ruff` 绿。

### 阶段二：入口与边界合同（2–4 个短批次）

目标：把事实读写与 transport 入口变窄。

- Session 单一写漏斗，迁移低风险旁路。
- AppState 最小 Protocol（P2 类型级反转；不宣称修 runtime cycle）。
- JSONL 读取 API 分层：header/summary/full/recovery。
- web git 继续统一到 `tools/git.py`，不再内联命令。
- Bash timeout/cancel/late side effect contract 固化。

Gate：所有 durable append 通过允许的 typed writer；Web/Tool git 输出同形；Bash 取消后无迟到副作用；既有 HTTP path 不变。

### 阶段三：Recovery 与可替换性护栏

目标：先度量并收敛高风险恢复边界，不提前动 Loop。

- Recovery callback deadline、锁持有时长诊断与并发红证；证据支持后再做两阶段 reconcile。
- Fake Model/Memory/Artifact/Observability provider 套件。
- LangChain adapter contract tests，Core 不引入 prebuilt agent。
- 统一 failure/error code contract 的 model-owner，不跨层复制 marker。

Gate：UNKNOWN 不盲重跑；duplicate confirmed side effect=0；替换 optional provider 不改 AgentRuntime；optional failure 不拖垮 Core。

### 阶段四：最后收敛 `_drive`

目标：在 router/tool_scope/tracer/provider/write/recovery seam 稳定后，只提取一个高内聚阶段。

- 先建立 success/tool/fallback/max_steps/context exceeded/cancel/error 的事件序列 golden。
- `_drive` 保留唯一 Agent Loop owner；只抽 model turn 或 terminal phase，不引入 LangGraph/第二 Loop。
- 顺手清除已被前置 seam 消掉的 `None`/provider 分支，不做功能改动。
- 性能基线：JSONL append、Session projection、ToolExecutor、SSE backlog、Recovery lock hold time。

Gate：Phase 16 6/6 指标不退化；取消臂不 yield、异常臂事件镜像、checkpoint/fallback/tool pair/memory 次数与基线一致；全量/真实 Gate 可重复。

## 10. 风险与测试矩阵

| 风险 | 触发形状 | 影响 | 防线/测试 | 阶段 |
|---|---|---|---|---|
| Tool call/result 不配对 | `_drive`/Recovery/旁路写入顺序变化 | Context、Resume、UI 错乱 | `tests/session/test_derive_messages.py`、Recovery pair invariant、Phase 16 G2 | 1/3 |
| UNKNOWN 被盲重跑 | Recovery 迁移误将 verdict 当 retry | 重复副作用 | `tests/recovery/test_reconcile.py`、kill/resume、Operation Ledger | 3 |
| 人工裁决旧 verdict | 锁外两阶段后 operation 已变化 | 错写结果 | operation version/id 红证 | 3 |
| 取消臂 yield | GeneratorExit/CancelledError 收尾改动 | RuntimeError/假事件 | `tests/agent/test_run_finalizer.py`、disconnect recovery | 3 |
| Fallback 错切 | provider failure 分类过宽 | 成本/错误归因 | 参数化 401/422/429/5xx/timeout matrix | 1 |
| optional provider 拖垮 Core | wiring catch 或 shutdown 泄漏 | 启动/运行失败 | capability Gate、fake broken provider | 1/4 |
| tool_scope 越权 | declared vs registry 混淆 | 安全边界扩大 | profile factory + table-driven coverage | 1 |
| cwd 错归属 | resume/fork 重新写 started | 项目/沙箱串线 | session cwd + Web/CLI resume matrix | 1 |
| env 配置散乱 | **实测不存在**：6 个读取表达式且无变量跨模块重复 | 无整改 blast radius | inventory/allowlist 现状守护即可 | 非 roadmap |
| JSONL 半行/跨进程写 | reader/writer 重构 | 历史不可恢复 | fsync、坏行、seq、并发测试 | 2 |
| Bash 超时未杀进程 | 只取消 await | 迟到副作用 | child process kill + workspace marker test | 2/3 |
| git path scope 绕过 | 第二命令路径或 pathspec 并集 | 越界数据泄露 | nested repo/path traversal/meta char test | 2 |
| router seam 破坏 patch | module-level symbol 删除 | 测试/运行时崩溃 | router smoke + compatibility re-export | 2 |
| Langfuse 影响业务 | sink 异常穿透 | Core 不可用 | Null/failing sink + flush timeout | 1/4 |

## 11. 三项明确纠错

### 11.1 wiring 必须 table-driven，但不能变成反射式发现

当前 `wiring.py:469-512` 的显式 `_BUILTIN_WIRING` 和 provider allowlist 是方向正确但有双表重复。修法是一个 typed table 同时承载 capability 名、factory、degradation、provider validator/lifecycle policy；由 table 生成校验和诊断。**不要**改为扫描模块、按字符串 `getattr` 自动发现所有 capability。规格 `08` 要求显式边界，安全/失败语义不能隐藏在反射里。

### 11.2 env 不散乱

`Settings`（`config.py:14-18`）应是用户配置的唯一入口；业务 capability、model、web route 不得自行 `os.getenv`。例外要显式标成“进程继承环境/安全 allowlist”：MCP stdio 的 `mcp/config.py:82`、`mcp/client.py:71`、sandbox 的 `sandbox/local.py:137-155`、instance lock 的 `instance_lock.py:75-159` 不应被机械地塞进 Settings，但应通过命名 port/allowlist 解释边界。这样既不泄漏秘密，也不破坏 MCP 子进程与 shell 原生语义。

### 11.3 session → web 只允许 `TYPE_CHECKING`，并进一步转为 Protocol

`session/service.py:128-132` 当前已把 `from agent_harness.web.app import AppState` 放入 `TYPE_CHECKING`，这是避免运行时循环依赖的正确第一步；但 `SessionService.__init__(state: AppState)`（`service.py:271-279`）仍让 web 容器成为领域类型的名义 owner。纠正目标不是删除类型，而是把所需字段/方法定义成 session/application port，由 AppState 实现。保持 `web → session/application`，不允许 `session → web` 的运行时依赖。

## 12. 现在不应做的事

1. **不因 `_drive` 长就把 Agent Loop 交给 LangGraph**；它是最后阶段的高风险结构收敛项。
2. **不把 env 读取点机械收进 Settings**：6 个点各有不同原生职责，审计没有发现配置散乱问题。
3. 不因 LangChain coupling 存在就替换全部消息模型；当前是规格批准的 `ADAPT`。
4. 不把 SessionEvent、Diagnostic Log、Operation Ledger、Artifact 混成一个万能 store。
5. 不将 Path validation defense-in-depth 当成重复 bug；Web 早拒绝 + Service/Sandbox 信任边界是有意双层。
6. 不把 `ConversationState`/所有 DTO 一次性拆成十几个对象；先找真实摩擦和测试红证。
7. 不用“通用 registry”吞掉 capability-specific failure、生命周期和权限语义。

## 13. 审计结论

当前仓库不是需要推倒重来的低质量系统。它已经把最难的持久化、恢复、权限、工具统一执行和 optional observability 做到了可验证的程度。质量债务主要集中在**入口与策略聚合过度**，以及少量**复制粘贴/字符串推断/第二路径**。

最安全的施工策略是：先保留优秀语义，建立 typed seam 和红证，再做小步结构收敛。所有整改都必须以“行为等价或已披露行为变化”为契约，不能以“文件变短”作为完成标准。

## 14. 结尾自审：我是否只是搬运代码？

不是。本文没有把已有文件目录或函数列表重新抄一遍作为结论，而是做了四个额外判断：

1. 将规格不变量与当前实现逐项对照，区分“已有优秀设计”和“真正 gap”。
2. 对每个热点给出耦合、后果、Blast radius、风险、是否现在值得改和可执行测试，而不是只说“应该重构”。
3. 明确保留 LangChain 的 accepted coupling，同时指出真正需要隔离的是 concrete 类型和 failure 语义，避免错误的框架迁移。
4. 把修法收敛成四阶段和二值验收边界，并明确三项纠错：wiring table-driven、env 单一入口、session→web 仅 `TYPE_CHECKING`/Protocol。

仍然保留的审计限制：本文是静态全仓审计，不替代真实并发压力、真实 provider 网络故障、Windows/容器双平台和人工审批长等待的现场 Gate。上述限制已转成测试矩阵和 tickets，而没有被“全量 pytest 通过”掩盖。
