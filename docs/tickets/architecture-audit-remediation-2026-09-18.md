# 架构审计整改 Tickets — 本地执行索引

> 日期：2026-09-18
> 来源审计：`docs/research/2026-09-18-full-codebase-architecture-quality-audit.md`
> 用途：12 张可由独立 Coding Agent 领取的本地执行票；对应 GitHub Issues #237–#248。本文是执行索引，不替代 Engineering Specification、ADR 或 GitHub issue。
> 共同约束：Reuse First；Scope Lock；不改变外部 Contract，除非票面明确披露；门禁全绿后才可提交；每票完成后按仓库 SDD 审查协议进入批量 review。
> **裁决规则**：GitHub issue 正文是每票 Scope/AC 的权威；本文只补充证据、依赖和测试。若本文与对应 issue 冲突，以 issue 为准，额外建议不构成关单条件。

## 0. 统一执行协议

每张票按以下顺序执行：

1. 读取相关 Engineering Specification、本文票面证据和候选文件。
2. 先写/确认红证：必须能在基线证明缺口、漂移或不可替换性；纯结构票则先建立行为 golden。
3. 最小修改；不顺手改相邻热点。
4. 运行票面专项测试、相关目录测试、ruff；高风险票追加 Phase 16 Gate/全量。
5. `git diff --check`；确认只改票面范围。
6. DoD 证据记录测试命令、结果、关键文件和任何已披露行为变化。

**优先级与依赖顺序**：T01–T04 可先行；T12 与 T07 串行；T05/T08/T09/T10 按文件避让；T06 与 T11 是高风险票，T11 最后实施。

---

## T01 — cwd 续聊归属对账（GitHub #237）

> 状态说明：当前 cwd issue 将由主代理实施。本票仍写成独立、完整、可验证的执行单，供 review、回归或接力使用；领取者不得与主代理并行修改同一文件。

### 证据

- `src/agent_harness/session/cwd.py:1-17`：cwd 是首条 `session/started` 的不可变会话锚，resume/replay/fork/续聊不追加第二条 started。
- `src/agent_harness/session/cwd.py:29-57`：写入规范化；读取只取第一条，旧日志返回 `None`。
- `src/agent_harness/session/session.py:150-184`：`Session.start(..., cwd=...）` 是 cwd 唯一写入口，`started_data['cwd']` 被拒绝绕过。
- `src/agent_harness/workspace/index.py:38-47`：WorkspaceIndex 只依赖 SessionHeaders 读取 header。
- `tests/session/test_session_cwd.py` 已覆盖部分基础语义，但须对续聊和跨入口做完整矩阵。

### 根因

会话归属过去寄生在 Sandbox WorkspaceRegistry 映射；后来加入 SessionEvent cwd 锚，但创建、续聊、fork、CLI/Web、WorkspaceIndex 的消费路径仍需证明同源。缺口不一定是单个函数错误，而是“session 事实”和“sandbox mapping”两份数据在入口处缺少机械对账。

### 范围

- 对账 Web create/resume/follow-up、CLI create/resume、fork。
- 明确 cwd 与 WorkspaceRegistry mapping 冲突时的失败语义。
- 保持旧会话无 cwd → `None`/未分组，不猜测回填。
- 增补专项测试和最小修复。

### 不做

- 不允许续聊修改 cwd。
- 不迁移/回填历史 JSONL。
- 不重构 WorkspaceIndex 或 Sandbox Registry 全部实现。
- 不更改目录选择 UX。

### 依赖

无代码依赖；协调依赖：主代理拥有当前 cwd 修改权，其他 Agent 只能 review/测试，除非明确接管。

### 候选文件

- `src/agent_harness/session/cwd.py`
- `src/agent_harness/session/session.py`
- `src/agent_harness/session/service.py`
- `src/agent_harness/session/fork.py`
- `src/agent_harness/workspace/index.py`
- `tests/session/test_session_cwd.py`
- 相关 `tests/web/test_web_multiturn.py`、CLI resume 测试

### 二值 AC

- [ ] 创建会话后，create→resume→follow-up 的首条 `session/started.data.cwd` 逐字不变且始终只有一条 started。
- [ ] Web 与 CLI 对同一输入使用同一规范化结果。
- [ ] 旧会话无 cwd 可 resume，结果仍为 `None`，无隐式补写。
- [ ] mapping 与 session cwd 冲突时明确失败，不能静默选任一侧。
- [ ] fork 的 cwd 行为由现有 policy 明确测试，不修改父会话。

### 红证/测试

- 红证：构造 mapping 指向 B、session header cwd=A，当前路径若静默继续则测试必须红。
- 红证：续聊后出现第二条 started 或 cwd 改变即红。
- 运行：`pytest tests/session/test_session_cwd.py tests/session/test_fork.py tests/web/test_web_multiturn.py -q`。
- 高风险路径追加相关 CLI 测试和全量。

### 风险

高。错误修复会把不可变 cwd 变成每轮可变配置，或破坏旧日志兼容。

### DoD / 关单证据

- 列出四入口矩阵及结果。
- 给出冲突红证测试名。
- 证明旧日志未被改写、父 fork 未变化。
- 附专项测试、ruff、全量结果；若只完成 review，不关单。

---

## T02 — `tool_scope` 声明面/注册面对账（GitHub #238）

### 证据

- `src/agent_harness/agent/profiles.py:58-108`：三内置 profile 手写工具集合。
- `profiles.py:111-159`：`tool_scope_summary` 明确声明面与实际 registry 两个方向都可能不同。
- `src/agent_harness/assembly.py:276-287`：运行时才计算收窄前后差集 `dropped_tools`。
- `src/agent_harness/agent/factory.py:81-97`：child 申请越权/不存在工具的判断和 registry.filtered。

### 根因

profile 声明、capability 实际注册、Web catalog 文案、child grantable scope 是不同阶段的数据，目前靠注释和测试记忆维持，没有一个 table-driven 对账合同。

### 范围

- 建立 profile→tool scope 的参数化测试表。
- 对每个 profile 验证声明、实际 effective、dropped、越权四类结果。
- 新 tool 未归属任何 profile 时测试显式失败或要求登记。
- 保持“声明开放”与“实际开放”文案口径不同。

### 不做

- 不把所有 profile 都改成全量工具。
- 不从前端常量反向生成后端权限。
- 不在 catalog 端点为数数而 `build_runtime` 或连接 MCP。

### 依赖

无；所需 wiring key/provider 对账作为本票测试护栏，不另立主票。

### 候选文件

- `src/agent_harness/agent/profiles.py`
- `src/agent_harness/agent/factory.py`
- `src/agent_harness/assembly.py`
- `tests/agent/test_profiles_factory.py`
- `tests/capability/test_multiagent_wiring.py`
- `tests/test_assembly_agent_profile.py`

### 二值 AC

- [ ] 每个内置 profile 都有参数化 case，断言 exact declared set。
- [ ] optional capability 缺席时 effective scope 是交集，不报虚假实际数量。
- [ ] 注册了但未声明的工具出现在 `dropped_tools`。
- [ ] child 的 effective scope 只能等于或小于 source registry；越权申请明确拒绝。
- [ ] 新增 builtin tool 而未更新对账表时测试变红。

### 红证/测试

- 临时注册 `read_artifact`/fake tool 但不加入 scope，验证 dropped。
- 临时 spec 申请 `write` 而 source 只给 read，验证拒绝。
- `pytest tests/agent/test_profiles_factory.py tests/capability/test_multiagent_wiring.py tests/test_assembly_agent_profile.py -q`。

### 风险

中。把 declaration universe 误当 runtime registry 会产生权限或 UX 错误。

### DoD / 关单证据

参数表、两方向差异红证、越权红证、专项和全量结果齐全。

---

## T03 — Provider failure 统一分类（GitHub #239）

### 证据

- `src/agent_harness/model/fallback.py:44-67`：typed/结构化地识别 stall、httpx status、transport 和 SDK 类名。
- `src/agent_harness/web/model_providers.py:118-137`：通过错误字符串包含 `timeout`、`429`、`401`、`404` 分类。
- `web/model_providers.py:266-277`：连接测试消费字符串分类并写 `last_test`。

### 根因

runtime fallback 和 provider connection test 分别发明 failure 词汇；Web 层知道 SDK 文案，模型层知道网络/状态语义，导致同错异名。

### 范围

- 定义 provider-neutral `ProviderFailureKind` 和 classification result。
- 统一 runtime/fallback/test-provider 对状态码和错误类型的判定。
- 保持现有外部 reason 字符串兼容。
- safe_message 与 raw detail 分离；detail 继续脱敏/截断。

### 不做

- 不把 Tool failure 与 Model provider failure 合并。
- 不在 Core import 每个具体 SDK 的异常类。
- 不改变 fallback 次数和 never-switch-back 策略。

### 依赖

无；建议在 T11 `_drive` 之前完成。

### 候选文件

- `src/agent_harness/model/failure.py`（若新增最小模块确有必要）
- `src/agent_harness/model/fallback.py`
- `src/agent_harness/web/model_providers.py`
- `tests/test_model_fallback.py`
- `tests/web/test_model_providers_api.py` 或现有 provider 测试

### 二值 AC

- [ ] 429/5xx/timeout/transport = transient/retryable。
- [ ] 401/403/400/404/422 = 非 transient，且 reason 稳定。
- [ ] provider test 与 runtime 对同一异常得到同一 kind。
- [ ] 外部 API 旧 reason 字符串不破坏。
- [ ] raw exception/token 不进入 safe message。

### 红证/测试

参数化异常矩阵，包含伪异常 `status_code`、httpx response、类名异常、消息带 token。运行 model + web provider 专项、全量。

### 风险

中。分类过宽会错误 fallback，过窄会降低可用性。

### DoD / 关单证据

附 failure matrix、兼容 reason 清单、脱敏测试、专项/全量结果。

---

## T04 — `Tracer + NullTracer` 显式端口（GitHub #240）

### 证据

- `src/agent_harness/observability/tracer.py:85-159`：`RunTracer` 依赖 `LangfuseSink`，sink 缺席时方法安全 no-op。
- `src/agent_harness/agent/runtime.py:686,738-746`：tracer 变量初始为 `None`，后续大量 `if tracer is not None`。
- `src/agent_harness/observability/sink.py:89-107,125-150`：未配置/失败时 sink 自身降级。

### 根因

no-op 语义分散在 Runtime 的 `None` 分支和 Sink 的 null 行为中；Core 仍需知道“观测是否存在”，使 `_drive` 分支增多，也让替换 tracer 只能伪装 LangfuseSink。

### 范围

- 定义最小 `Tracer` Protocol，覆盖 Runtime/Executor 当前调用的方法。
- 提供 `NullTracer`，所有方法无副作用且返回适配的空 handle。
- Runtime 始终持有 tracer 对象，逐步删除 `if tracer is not None`。
- `RunTracer` 继续做 Langfuse adapter，不改变 trace 数据。

### 不做

- 不删除 LangfuseSink 熔断/故障隔离。
- 不改变 trace/span 层级、trace_id/trace_url Contract。
- 不把 Diagnostic Log 合入 Tracer。

### 依赖

建议在 T11 前完成；与 observability 其他开发文件级避让。

### 候选文件

- `src/agent_harness/observability/tracer.py`
- `src/agent_harness/observability/__init__.py`
- `src/agent_harness/agent/runtime.py`
- `src/agent_harness/tooling/executor.py`
- `tests/observability/test_tracer.py`
- `tests/agent/test_runtime_observability.py`

### 二值 AC

- [ ] Runtime 未配置 observability 时使用 `NullTracer`，无 `None` 特判。
- [ ] NullTracer 的每个方法都可调用且不抛异常。
- [ ] RunTracer 输出与基线逐字段一致。
- [ ] failing sink 不改变 SessionEvent/ToolResult/run 终态。
- [ ] trace_id/trace_url 缺席时仍为 `None`，不伪造。

### 红证/测试

- 用抛异常 fake tracer/sink 验证 Core 完成 run。
- NullTracer 覆盖 model/tool/context/run 生命周期。
- 跑 observability、runtime observability、Phase 15 Gate。

### 风险

中。空 handle 的形状若不兼容，可能在工具 span 收尾处漏异常。

### DoD / 关单证据

Protocol 方法清单、NullTracer 全生命周期测试、与 RunTracer golden 对比、专项/全量结果。

---

## T05 — Session durable 写入单一漏斗（GitHub #241）

### 证据

- `src/agent_harness/session/session.py:278-330`：核心 append 统一事件词表、seq、store 和 listener。
- `session.py:187-218`：静态 `append_event` 用 read→construct→append 处理旁路。
- `session.py:332-352`：fork seed 的 `adopt_history` 直接 store append。
- `src/agent_harness/recovery/coordinator.py:272-316`：Recovery 合成事件通过 Session.append。
- `src/agent_harness/session/store.py:164-179`：底层物理 append 负责 fsync、seq 和硬删守卫。

### 根因

物理 IO、业务 append、旁路 append、历史移植分别有入口；虽然大多数正确，但没有 typed writer/command 明确区分正常事实、Recovery 合成和 fork seed，新增调用者容易选错入口。

### 范围

- 先生成所有 `append_event`/`store.append_event`/`Session.append` 调用图。
- 定义单一 durable append port；明确 normal append、recovery synthesis、history adoption 三种 command。
- 迁移最少一个低风险旁路并建立静态/测试护栏。
- 保持 Store 为 IO owner、Session 为 seq/domain owner。

### 不做

- 不把 Operation Ledger 合入 SessionEvent。
- 不把 stream-only AgentEvent 持久化。
- 不一次性迁移所有调用点。
- 不取消 fsync 或硬删拒写。

### 依赖

先完成调用图；T01 cwd 正在改 Session 文件时避免并行。

### 候选文件

- `src/agent_harness/session/session.py`
- `src/agent_harness/session/store.py`
- `src/agent_harness/session/fork.py`
- `src/agent_harness/recovery/coordinator.py`
- `src/agent_harness/memory/writeback.py`
- `tests/session/`、`tests/recovery/`

### 二值 AC

- [ ] 新业务 durable event 只有一个公开 writer 入口。
- [ ] stream-only/未知 event 在写盘前拒绝。
- [ ] Recovery/Fork 通过显式 command，不冒充 normal append。
- [ ] fsync、seq conflict、硬删拒写行为不变。
- [ ] Tool call/result pairing、Fork 父不变、Recovery 无重复副作用 Gate 绿。

### 红证/测试

- 静态测试/grep allowlist：禁止新业务模块直接调 `JsonlSessionStore.append_event`。
- seq 冲突、硬删迟到写、unknown type、stream-only、fork、recovery 专项。
- Phase 14/16 Gate + 全量。

### 风险

高。必须小步提交，任何行为变化都需披露。

### DoD / 关单证据

调用图、迁移名单、保留例外及理由、Gate 指标、全量结果。

---

## T06 — Recovery 锁内人工裁决边界（GitHub #242）

### 证据

- `src/agent_harness/recovery/coordinator.py:214-323`：整个 recover 在 `_recovery_lock(）` 内。
- `coordinator.py:247-269`：收集人工裁决项；无 callback 整体拒绝。
- `coordinator.py:303-314`：callback 在写入段/锁内执行。
- `coordinator.py:327-361`：sidecar SQLite `BEGIN EXCLUSIVE`。

### 根因

Recovery 为保证幂等和一致性，将人工交互放入同一数据库级互斥；安全但锁持有时间被用户/网络延迟支配。

### 范围

- 锁内完成 scan + 状态推进 + reconcile-required，释放锁等待 callback，重抢锁后按 operation identity/version 提交 verdict。
- callback deadline、锁持有时长诊断可作为前置测量，但不能替代上述交付，也不能据此关闭 #242。

### 不做

- 不自动验证/重跑 UNKNOWN。
- 不让 RETRY 直接执行原副作用。
- 不删除 `ReconcileRequired` 安全拒绝。
- 不使用进程内 asyncio.Lock 代替跨进程锁。

### 依赖

可独立实施；若与 T05 写入漏斗改到同一文件，必须串行。

### 候选文件

- `src/agent_harness/recovery/coordinator.py`
- `src/agent_harness/recovery/reconcile.py`
- `src/agent_harness/storage/sqlite.py`
- `tests/recovery/test_recovery_coordinator.py`
- `tests/recovery/test_reconcile.py`
- `tests/integration/test_kill_resume.py`

### 二值 AC

- [ ] 无 callback 时仍零写入并抛 `ReconcileRequired`。
- [ ] 人工等待期间第二个不相关 session 的 recovery 不被同一长锁无界阻塞（方案 A）。
- [ ] verdict 只提交到扫描时同一 operation identity/version。
- [ ] callback 失败/超时后重试不产生重复 reconcile-required/result。
- [ ] UNKNOWN 绝不自动重跑；duplicate confirmed side effect=0。

### 红证/测试

两个并发 recovery 任务/进程 + 可控阻塞 callback；旧 verdict；callback timeout；crash between phases；Phase 16 kill/resume。

### 风险

极高。竞态、幂等和副作用安全均在此票，必须独立 code review。

### DoD / 关单证据

状态机图或 ADR 指针、并发时序红证、kill test、Ledger/SessionEvent 最终对账、全量结果。

---

## T07 — FastAPI router seam（GitHub #243）

### 证据

- `src/agent_harness/web/app.py:1-98` 同时 import FastAPI、所有领域服务和 adapters。
- `app.py:423-575` 定义 AppState。
- `app.py:841+` 的 create_app 继续注册大量 inline routes。
- 既有正例：`web/workspace_files.py:277-284`、`web/model_providers.py:144-149` 使用 `register_*_routes(app）`。

### 根因

项目早期把所有路由留在 app factory，后续部分模块已抽出，但主 Session/Run/Approval routes 仍闭包化，导致 app.py 是冲突热点和不可局部测试单元。

### 范围

- 选择一组低风险、内聚、无行为变化的 routes（建议 catalog/read-only）迁到独立 router module。
- 注册函数接收明确 dependency seam，而不是 import AppState concrete。
- 保持 path、status code、schema、Depends、OpenAPI 不变。
- 建立 router 注册和 Contract snapshot 测试。

### 不做

- 不一次拆完整 app.py。
- 不改认证/CORS/JWT 策略。
- 不改 SessionService 行为。
- 不引入新的 DI 框架。

### 依赖

与 T12 都可能触及 AppState/app.py，不并行写同文件；先做 T12 的窄 interface，再迁 router。

### 候选文件

- `src/agent_harness/web/app.py`
- `src/agent_harness/web/<new_or_existing_router>.py`
- `src/agent_harness/web/domain_errors.py`
- `tests/test_web_api.py`
- `tests/web/`

### 二值 AC

- [ ] 迁移 routes 的 HTTP method/path/status/schema/OpenAPI operation 不变。
- [ ] create_app 只用一行/一处注册该 router。
- [ ] router module 可用 fake dependency 单测，不需构造完整 AppState。
- [ ] 旧测试 monkeypatch seam 保持或有兼容 re-export。
- [ ] app.py 行数减少且没有把同等复杂度复制两份。

### 红证/测试

OpenAPI before/after snapshot；端点错误矩阵；router fake dependency；Web 全量。

### 风险

中高。闭包捕获依赖、测试 monkeypatch 和 FastAPI schema 命名易漂移。

### DoD / 关单证据

列出迁移端点、OpenAPI diff=空或已解释、专项/全量结果、app.py 无旧重复实现。

---

## T08 — Bash timeout/cancel 契约（GitHub #244）

### 证据

- `src/agent_harness/tools/bash.py:86-132`：BashTool 使用 `to_thread`、`cancel_event`、output sink；CancelledError 通知 Sandbox 杀进程树。
- `src/agent_harness/tooling/contract.py:192-196`：Tool 默认 timeout 是 `10.0s`；`src/agent_harness/tooling/executor.py:801` 在 ToolExecutor 外层强制使用该预算。
- `src/agent_harness/sandbox/local.py:31-32`：Local Sandbox 另有 `DEFAULT_EXEC_TIMEOUT=60.0s`；BashTool 未覆写前者，故当前有效预算是 10 秒而非 60 秒。
- Bash 契约要求非零 exit_code 仍 `ok=True`（`bash.py:5-7,121-132`）。

### 根因

Tool 层、Sandbox 层、Executor 取消/超时有三个时间域；没有一个跨后端的 typed timeout result contract，容易出现只取消 await、子进程继续写 workspace，或把 timeout 错映为工具 transient retry。

### 前置决策 Gate

GitHub #244 列出的默认预算、配置入口、Local/Docker 一致性、唯一 deadline owner、取消传播、MUTATING 超时后 UNKNOWN/不可自动重试六项，必须先由用户/产品批准并记录。未批准前只能补红证和测量，不得修改默认值。

### 范围

- 明确 Sandbox.exec 的 timeout/cancel 结果字段和异常语义。
- Local/Docker 后端一致：超时/取消后进程树终止，无迟到输出/写入。
- BashTool 将结果映射为稳定 ToolResult data，不触发 ToolExecutor 自动 retry。
- timeout 参数来源使用 Settings/Tool policy，不散落 magic number。

### 不做

- 不增加任意 Host command 权限。
- 不把非零 exit_code 变为 Tool failure。
- 不实现交互式 PTY。
- 不将 timeout 视为 UNKNOWN 副作用自动重跑。

### 依赖

无；若 T11 同时改 Runtime cancellation，先完成本票契约。

### 候选文件

- `src/agent_harness/tools/bash.py`
- `src/agent_harness/sandbox/base.py`
- `src/agent_harness/sandbox/local.py`
- `src/agent_harness/sandbox/docker.py`
- `src/agent_harness/config.py`
- `tests/sandbox/`、`tests/tools/test_coding_tools.py`、`tests/integration/test_bash_reconcile.py`

### 二值 AC

- [ ] 默认 BashTool 的**实际有效预算**等于已批准值，测试能区分旧的 10s 外层预算与 60s Sandbox 默认，不再存在双真相。
- [ ] timeout/cancel 后子进程树终止，延迟 marker 文件不会出现。
- [ ] Local/Docker 返回同形结果字段。
- [ ] exit_code!=0 仍 `ToolResult.ok=True`。
- [ ] timeout/cancel 不由 ToolExecutor 自动重试。
- [ ] stdout/stderr 已产生部分按既有 artifact/output-stream 语义保留。

### 红证/测试

启动 child/grandchild 延迟写 marker；触发 timeout/cancel；等待超过原延迟后 marker 仍不存在。双后端（Docker 可 probe-gated）。运行 sandbox/tools/reconcile/Phase16。

### 风险

高。进程树清理跨 Windows/POSIX/Docker 差异大。

### DoD / 关单证据

平台矩阵、marker 红证、结果 shape、retry 次数断言、专项/全量结果。

---

## T09 — JSONL 读取统一（GitHub #245）

### 证据

- `src/agent_harness/session/store.py:1-15,92-179,236+`：`read_events` 是底层全量读取。
- 直接调用分散在 `cli.py:393,474`、`multiagent/provider.py:226`、`recovery/coordinator.py:220`、`recovery/scan.py:157`、`session/fork.py:154`、`session/lineage.py:27`、`session/session.py:210,234`。
- Store 已有 summary/header 的不同性能语义（`store.py:39-42,58-89`）。

### 根因

所有消费者都拿 `list[SessionEvent]`，但真实需求不同：header、summary、full projection、recovery raw、lineage；调用方自行决定空日志/坏行/seq 的处理，容易漂移，也导致列表路径误读全量。

### 范围

- 盘点并分类全部读取调用。
- 定义 typed reader methods/port：`read_started_header`、`read_session_summary`、`read_events`（full）、`read_for_recovery`（如需要不同容错）。
- 迁移至少一类只需要 header/summary 的调用，避免全量解析。
- 统一坏行、半行、空 session、invalid seq 的语义和诊断。

### 不做

- 不换数据库后端。
- 不删除 JSONL。
- 不做跨进程写锁（另票/架构问题）。
- 不将 Persistent History 截断成 summary。

### 依赖

建议 T05 writer 票前后均可；如都改 store.py，串行。

### 候选文件

- `src/agent_harness/session/store.py`
- `src/agent_harness/session/header.py`
- 上述所有 read_events 调用文件
- `tests/session/test_event_store.py`
- `tests/session/test_session_robustness.py`
- `tests/workspace/test_workspace_index.py`

### 二值 AC

- [ ] 每个读取调用点被标为 header/summary/full/recovery 之一。
- [ ] 列表/Workspace bootstrap 不读事件正文。
- [ ] full/recovery 读取仍保留完整历史，不丢 durable event。
- [ ] 半行/坏行/空日志/seq 冲突语义由单点测试锁住。
- [ ] 无第二个手写 JSONL parser。

### 红证/测试

大 session 计数 spy 证明 header/summary 不全量 parse；尾部半行；中间坏行；重复 seq；历史兼容。运行 session/workspace/recovery 专项和全量。

### 风险

中高。容错差异若被机械统一，可能把坏数据静默吞掉或让历史会话不可读。

### DoD / 关单证据

调用点分类表、迁移列表、性能/解析计数证据、坏日志矩阵、全量结果。

---

## T10 — Web git 统一工具路径（GitHub #246）

### 证据

- `src/agent_harness/tools/git.py:52-101`：命令构造、pathspec 白名单和 `run_git_command` 是工具/Web 共用点。
- `src/agent_harness/web/workspace_files.py:253-274`：Web 仍负责 scope 与 Sandbox 边界。
- `workspace_files.py:351-403`：status/diff 两路分别重复 try/except、command build、result mapping。

### 根因

核心命令已统一，但 Web adapter 仍组合 `_boundary_checked`、`_scope_for`、command helper 和 result mapping；增加第三个 git 查询或修改规则时仍可能在 Tool/Web 两条路径漂移。

### 硬 Gate

必须满足 GitHub #246 的统一路径：Permission → ToolExecutor → timeout/telemetry → Operation Ledger → 可对账审计。HTTP 请求没有自然 session 时的 ledger/session/run 关联键未冻结前，不得实施；共用 command helper 不能替代统一执行路径。

### 范围

- 将“checked scope + command + execute + normalized result”收成 tooling 层窄 service/helper。
- Tool 与 Web 都调用同一执行函数；Web 仅负责 HTTP query/错误映射。
- 保持 workspace 嵌套在更大 repo 时只返回 workspace 范围。

### 不做

- 不新增 commit/push/reset。
- 不改变 READ_ONLY 权限。
- 不允许任意 git 子命令字符串。
- 不移除 Web 层 HTTP 422/403 翻译。

### 依赖

无；与 workspace 文件路由开发文件级避让。

### 候选文件

- `src/agent_harness/tools/git.py`
- `src/agent_harness/web/workspace_files.py`
- `tests/tools/test_coding_tools.py`
- `tests/web/test_workspace_files_api.py`

### 二值 AC

- [ ] Web git 请求经过统一 Permission、ToolExecutor、timeout、telemetry 与 Operation Ledger 路径。
- [ ] ledger/session/run 关联键已由 ADR 或 issue 决策冻结，审计事实可对账。
- [ ] Tool/Web 使用同一 command+execute 路径。
- [ ] path traversal、shell meta、git pathspec magic 均拒绝。
- [ ] 无 path 时 scope=`.`，有 path 时单一 pathspec 不被 `.` 并集扩宽。
- [ ] 不是 git repo 仍 200/ok + 非零 exit_code。
- [ ] Web 与 Tool 的 exit_code/stdout/stderr 同形。

### 红证/测试

在大 repo 内嵌 workspace，外部文件有 diff；两端都不得泄漏。空格路径、`..`、绝对路径、`&`/`|`、staged diff 矩阵。

### 风险

中。错误组合 pathspec 会直接造成越界内容泄漏。

### DoD / 关单证据

嵌套 repo 红证、Tool/Web parity、命令构造唯一性 grep、专项/全量结果。

---

## T11 — `_drive` 逐步收敛（GitHub #247）

### 证据

- `src/agent_harness/agent/runtime.py:657-1355`：`_drive` 超过 690 行。
- `runtime.py:778-1215`：主循环同时处理 steer、context、model stream/fallback、tool、checkpoint、熔断和 memory。
- `runtime.py:1223-1337`：取消/异常两套收尾有不同 yield 约束。
- 已有可复用边界：`agent/streaming.py`、`model/fallback.py`、`_TerminalContext`。

### 根因

Phase 叠加以“在主循环加一臂”演进，正确性被丰富测试守住，但状态和生命周期没有同步提取为可命名阶段。

### 范围

本票只做第一刀：

- 建立 `_drive` 事件序列 golden。
- 第一切片固定提取 terminal arms；第二切片再收敛 telemetry scope。方法参数使用 typed context，不传十几个散参。
- `_drive` 保留唯一 loop 和状态推进；提取函数不自行创建第二 loop。
- 行为零变化；无新事件类型/API。

### 不做

- 不一次拆完 `_drive`。
- 不引入 LangGraph/状态机框架。
- 不改变 fallback/tool retry/checkpoint/memory 语义。
- 不改取消臂 yield/discard 规则。

### 依赖

建议 T03 provider failure、T04 tracer、T08 timeout 先完成，减少移动中的条件分支；与任何 runtime.py 任务串行。

### 候选文件

- `src/agent_harness/agent/runtime.py`
- 可能新增 `src/agent_harness/agent/drive.py` 或小型内部模块（仅在确有必要时）
- `tests/agent/test_runtime_failure_paths.py`
- `tests/agent/test_run_finalizer.py`
- `tests/agent/test_model_fallback_runtime.py`
- `tests/agent/test_run_stream.py`

### 二值 AC

- [ ] 选定阶段从 `_drive` 提取，`_drive` 仍是唯一循环 owner。
- [ ] 所有 durable/ephemeral 事件序列与基线 golden 一致。
- [ ] 取消臂不 yield；异常臂可 yield 收尾事件。
- [ ] checkpoint 顺序、fallback transitions、tool pair、memory submit 次数不变。
- [ ] Phase 16 Gate 指标不退化。

### 红证/测试

覆盖 success/tool/fallback/max_steps/context exceeded/CancelledError/GeneratorExit/provider error/tool error/checkpoint error。运行 `tests/agent/`、Phase 16、全量、ruff。

### 风险

极高。必须单独 code review，禁止与功能改动同 commit。

### DoD / 关单证据

before/after event golden、提取边界说明、零行为变化证明、Phase 16 和全量结果。

---

## T12 — AppState 类型级反转（GitHub #248）

### 证据

- `src/agent_harness/session/service.py:128-132`：`AppState` 已仅在 `TYPE_CHECKING` 导入。
- `session/service.py:271-299`：`SessionService` 仍声明 `state: AppState` 并透传 store/run_manager/approval_queues/workspaces_root。
- `src/agent_harness/web/app.py:423-575`：AppState 是 Web 生命周期容器，拥有很多 SessionService 不需要的资源。

### 根因

为避免运行时 import cycle 做了 `TYPE_CHECKING` 修正，但领域服务构造契约仍以 Web concrete type 命名；这阻碍 CLI/纯单测最小构造，并让未来 AppState 属性变化隐式影响 session 层。

### 范围

- 先盘点真实 consumer 和 SessionService 实际访问的最小字段集。
- 只有确认至少两个真实 consumer/implementation 并能形成真实 seam 时，才在 session/application 边界定义最小 Protocol；否则使用显式 collaborators，不能把 AppState 换成同样宽的 Protocol。
- `SessionService.__init__` 接受该窄 interface 或显式 collaborators。
- AppState 结构化满足 Protocol，不需要继承。
- CLI/测试可用最小 fake state 构造。
- 保留 `TYPE_CHECKING`，session 模块运行时不能 import web。

### 不做

- 不把 AppState 移到 session。
- 不把所有 AppState 属性都塞进 Protocol。
- 不重写 SessionService 方法。
- 不同时拆 router（T07 独立）。

### 依赖

建议先于 T07；与 T01/T05 修改 `session/service.py` 时串行。

### 候选文件

- `src/agent_harness/session/service.py`
- 可选 `src/agent_harness/session/ports.py` 或既有合适模块
- `src/agent_harness/web/app.py`
- `tests/session/`
- `tests/web/`

### 二值 AC

- [ ] `session` 运行时 import graph 不包含 `agent_harness.web`。
- [ ] `SessionService` 构造参数是 session/application-owned Protocol，而非 AppState。
- [ ] Protocol 只包含实际使用成员，删掉任一必需成员类型/测试变红。
- [ ] FakeState 不依赖 FastAPI 即可完成核心 service 测试。
- [ ] Web/CLI 行为不变。

### 红证/测试

- import spy/静态 grep：导入 `agent_harness.session.service` 不加载 `agent_harness.web.app`。
- 最小 FakeState 测试。
- Session/Web/CLI 专项、全量、ruff；若有 type check 一并执行。

### 风险

中。Protocol 漏成员会在少见路径运行时出错；必须从真实属性访问生成清单。

### DoD / 关单证据

Protocol 成员清单、import graph 红证、FakeState 测试、Web/CLI 回归和全量结果。

---

## 13. GitHub 映射与推荐批次

| 本地票 | GitHub | 主题 |
|---|---:|---|
| T01 | #237 | cwd 续聊 |
| T02 | #238 | tool_scope 对账（含 wiring 对账护栏） |
| T03 | #239 | provider failure classification |
| T04 | #240 | Tracer + Null implementation |
| T05 | #241 | Session 单一写漏斗 |
| T06 | #242 | Recovery 锁内人工裁决 |
| T07 | #243 | FastAPI router seam |
| T08 | #244 | Bash timeout 契约 |
| T09 | #245 | JSONL 读取统一 |
| T10 | #246 | Web git 统一工具路径 |
| T11 | #247 | `_drive` 逐步收敛（最后实施） |
| T12 | #248 | AppState 类型级反转 |

建议批次：

1. 契约守护：T01–T04。
2. 入口收敛：T12 → T07，再做 T09/T10。
3. 耐久性边界：T05、T08，各自独立审查。
4. 高风险核心：T06、T11；`_drive` 最后实施。

## 14. 全局完成定义

全部 tickets 完成不等于“文件更短”，而必须同时满足：

- Agent Runtime 仍由本项目掌控。
- SessionEvent append-only、ToolExecutor 单一执行路径、Operation Ledger 独立、UNKNOWN 不盲重跑。
- Provider 可替换，optional failure 不拖垮 Core。
- Web 不维护第二套 Session 真相。
- 每个缺口均有二值红证。
- 所有行为变化有 ADR/Contract 指针；纯结构票的 event/API golden 不变。
- 专项、ruff、全量和高风险 Phase Gate 有可复制的关单证据。
