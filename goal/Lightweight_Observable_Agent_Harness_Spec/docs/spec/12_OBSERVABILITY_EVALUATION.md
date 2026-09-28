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
