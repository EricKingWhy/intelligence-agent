# 12 — Observability / Evaluation

## 1. 三层观测

### Durable SessionEvent
业务事实、Replay/Resume/Fork/UI 的基础。

### Diagnostic JSONL Log
Debug、性能、异常细节。

### Langfuse
Trace / Span / UI Observability，可开可关。

它们互补，不能互相替代。

## 2. Diagnostic Log

开发模式默认完整记录：
- model request/response metadata；
- tool input/output；
- attempt；
- error_code；
- duration；
- provider latency；
- token usage；
- cost；
- stack trace；
- fallback reason；
- reconcile reason。

大输出转 Artifact，不直接把数百 KB/MB 写成单行日志。

默认不做敏感信息脱敏是当前开发环境选择，但实现 SHOULD 保留未来 redaction hook。

## 3. Trace 层级

推荐：

```text
Agent Run
├─ supervisor
├─ research agent
│  ├─ model
│  ├─ retrieve_knowledge
│  └─ web_search
├─ coding agent
│  ├─ model
│  ├─ read/edit/apply_patch
│  └─ bash
└─ final synthesis
```

统一 metadata：
- session_id
- run_id
- agent_id
- step_id
- tool_call_id
- operation_id

## 4. Langfuse Adapter

REUSE 官方 SDK。

要求：
- graceful no-op；
- tracing failure 不影响 Agent；
- Session/Run/Agent/Tool ID 映射一致；
- 不发明另一套 trace identity。

## 5. Evaluation

项目自己拥有 Golden Dataset：

```text
evaluation/
├─ cases.jsonl
├─ runner.py
├─ assertions.py
└─ reports/
```

Eval Runner 调真实 AgentRuntime，不为了 EvalScope 重写 Runtime。

## 6. Deterministic First

优先代码判断：

- tool_selected_accuracy
- citation_valid_rate
- task_success_rate
- recovery_success_rate
- dangling_tool_call_count
- unnecessary_tool_rate
- permission_violation_count
- duplicate_side_effect_count

LLM Judge 只用于：
- 答案完整性；
- evidence support；
- review quality；
- 难以完全 deterministic 的语义质量。

不得把所有指标揉成一个无法解释的总分。

## 7. EvalScope

EvalScope 作为 thin Adapter：

```text
project-owned runner
→ real Agent Runtime
→ normalized result
→ EvalScope adapter
→ report
```

不是 Core 依赖。

## 8. Regression Metadata

每次 Eval 记录：

```text
app_version
prompt_version
model_provider
model_name
knowledge_version
memory_provider/version
eval_dataset_version
git_commit
timestamp
```

否则分数不可解释。

## 9. 初始 Gate

建议：
- P0 deterministic cases = 100%
- dangling tool call = 0
- duplicate confirmed side effects = 0
- core recovery = 100%
- citation validity = 100%
- permission violations = 0
- task success 达到经真实数据校准的阈值
- 每类暂停原因（预算 / deadline / stuck）都不伴随完成 / 失败事件
- 恢复前后 consumed 快照对账相等（不重置）
- replay 的预算消耗为 0

### 9.1 强制 Live Gate（真实 Provider + 生产工具）

长任务预算 / 暂停恢复 / stuck / 完成判定 MUST 由**真实链路**证据证明，并与默认无凭证测试套件**分离**：

- **链路**：部署中配置的**真实 Primary Provider**（测 fallback 时另加真实 Fallback Provider）、
  生产 Tool Registry 与 ToolExecutor 实现、真实 Local 或 Docker Sandbox 内的**一次性工作区**、
  真实文件 / 命令 / Git 操作。MUST NOT 引入仅供测试的「返回下一步」工具，MUST NOT 用 Fake Provider 兜底。
- **五个场景**：① 真实模型 + 生产工具完成一个**必须超过旧 10 轮 / 10 工具**实际限制的任务；
  ② 低显式预算 ⇒ 暂停 ⇒ 提高绝对 ceiling 后**同 `run_id`** 恢复并完成、消耗不重置；
  ③ 真实重复工具失败 ⇒ 恰好一次 replan ⇒ 同模式持续则 `PAUSED_STUCK`；
  ④ 真实 deadline 且存在在途 mutating 工具 ⇒ 不启动新工作，正常收尾或 `NEED_RECONCILE`；
  ⑤ 真实父子委派树共享预算并强制 `max_delegations=8`。
- **重复口径**：每个场景在**同一** commit / Git tree / 配置上**连跑三次**，**3/3** 才算通过；
  **所有失败尝试必须保留**（禁止重跑后只留成功者）；任何代码变更即作废先前的矩阵。
- **证据字段**：`schema_version`、`scenario_id` / `scenario_version`、`sha`、`tree`、
  Provider / model 标识（**无** token / 密钥 / 授权头）、Sandbox 类型与一次性工作区身份、
  三次尝试的起止与状态、Session / Run 身份、事件与 trace 引用、
  `verdict ∈ {PASS, FAIL, BLOCKED, SKIPPED}`。
- **缺条件不伪装**：缺凭据、Provider 或 Sandbox 不可用、场景未执行 ⇒ **BLOCKED / SKIPPED，永不 PASS**；
  受影响的实现票**不得关单**。
- **凭证零泄漏**：MUST NOT 打印、持久化或提交任何凭证值；`.env` 只可列 key **名**。

### 9.2 W-30 增补判据（压缩接班 · 清单 · 失败方案，#384）

本节**只增补**，不改 §9.1 与 W-21 既有判据。4 条判据的机器定义见
`docs/live_gate/w30/criteria.json`（机器定义；JSON Schema 见同目录
`criteria.schema.json`），集成契约见 `docs/live_gate/w30/00-integration-contract.md`；
判据实现（真实模型判定逻辑）在 `intelligence-agent-backend` 仓库。

#### 9.2.1 四条增补判据

- **W30-C1 压缩接班后清单不丢**：触发至少一次真实压缩后，UI 与事件投影中的
  进度清单与压缩前逐字一致（重放验证）。判定点：存在 `compaction/end` 事件后，
  比对压缩前清单（`progress.md` 快照 + 事件投影）与压缩后 UI 清单 + 事件投影；
  重放同一事件流必须得到同一结论。此逐字比对限定压缩本身无 plan 更新
  （R4）：`task/plan_updated` 有对应更新的合法更新不判违规。
- **W30-C2 约束不丢**：压缩前植入精确约束（参照 W-01 的「不得删除 old_rows」+
  精确 ID 「R-042」模式）；压缩后模型后续行为仍遵守，以行为证据判定
  （`tool/call` + `tool/result` 事件对），不以摘要文本目测。「违反」只判直接违反
  （tool call 参数直接违反约束）；经代理链 / 写脚本再执行的间接违反（参数不直接显形）
  本判据不自动判定，留人工复核。
- **W30-C3 失败方案不重走**：任务中人为制造一次失败并记录失败方案 fact
  （含 `source_event_id`）；压缩后继续任务，模型不得重走该路径
  （以 tool call 序列 / 事件流判定）。相同条件 = 相同工具 + 相同关键参数
  （工具名 + 业务关键参数键值，排除时间戳 / 请求 ID 等易变元数据）；
  带合理修正输入或新证据（以 `new_evidence_refs` / `input_diff_summary` 取证）
  的重试不违规。
- **W30-C4 UI 无丢项**：压缩触发瞬间 UI 清单无闪烁、无丢项（真实操作证据，
  接入 W-20 夹具；中间态丢项后恢复也算失败）。采样窗口（`sample_window_ms`）
  内项数最小值（`min_item_count_during_window`）低于触发前项数即检出中间态。

每条判据的证据 MUST 绑定 `sha` + `tree`，`verdict ∈ {PASS, FAIL, BLOCKED, SKIPPED}`
（与 §9.1 同态）；MUST 来自真实模型运行，mock / 替身 / Fake Provider /
UI 绿勾的输出不得计入 PASS（上限锁 FAIL）。

#### 9.2.2 用户拍板口径（2026-10-11，冻结）

- **R1 判定器顺序**：判定器在最终发布 Gate 执行**前**合入，消费 W-21 harness
  而非要求 W-21 先 passed（解决"先发版再补判据"顺序问题）。
- **R2 #365 依赖边**：区分"真阻塞"（W-21 harness 产物存在，判定器消费的输入）
  和"非阻塞"（W-21 的 passed 状态，#384 不等待它）两种边含义。
- **R3 失败重试限定**：失败路径禁止重走**限定**相同条件 / 无新证据——合理修正
  输入后的重试不违规。
- **R4 清单一致限定**：清单逐字一致**限定**压缩本身无 plan 更新——实时合法更新
  不能判失败。

#### 9.2.3 成熟产品依据

- R3：OpenHands `StuckDetector`（4x 相同 action/observation 或 3x 相同
  action→error → 一次 corrective nudge，之后仍重复则终止/暂停）与 LangGraph
  `RetryPolicy`（相同输入重试只给 transient 错误；逻辑错误零系统重试，须带修正
  参数）——`PORT DESIGN`。
- R1：GitHub merge queue（gate 消费 harness 组装的 merge-group commit）、
  LaunchDarkly（flag + 发布策略在代码上线前配好）、SWE-bench（FAIL_TO_PASS
  测试在 agent 动手前冻结）——`PORT DESIGN`。
- R4 / W30-C1：调研确认 Claude Code、Codex CLI、Cursor、Cline、OpenHands 均无
  「后继验证接班清单」机制——本判据为本项目自创（`BUILD`），按 R4 限定口径执行，
  不擅自扩大。
- R2：本仓特有（`BUILD`），无上游可复用。

#### 9.2.4 不做

不改 W-21 已有判据、不改压缩算法、不扩大 Gate 范围到 Memory V2（#304 独立）。

## 10. Final Full E2E

至少覆盖：

```text
existing Session
→ Main
→ Research
→ retrieve_knowledge insufficient
→ rewrite
→ web_search + citation
→ Main
→ Coding
→ grep/read/edit/apply_patch
→ pytest fail
→ Agent debug
→ modify again
→ Tool running
→ kill Python
→ restart/resume
→ restore Sandbox
→ Operation reconcile
→ restore ToolResult consistency
→ pytest pass
→ Review
→ final answer
→ SessionEvent replay
→ Langfuse Trace
→ Eval report
```

## 11. Acceptance Criteria

- JSONL 可 tail/grep；
- Langfuse 可定位耗时和 Tool retry；
- Langfuse 关闭时 Core 正常；
- Eval 可重复运行；
- Kill/Resume case 纳入回归；
- Citation 有真实来源；
- Regression report 可对比版本；
- §9.1 五个场景各有**同树 3/3** 证据，且证据字段齐全、可指认到 sha 与 tree；
- 缺凭据 / Provider / Sandbox 时返回 BLOCKED / SKIPPED 并以非零结果退出，MUST NOT 输出 PASS；
- 失败尝试被保留且可枚举；任何代码变更后矩阵重跑；
- 输出、事件证据与提交物中不含任何凭证值（自动扫描）。
