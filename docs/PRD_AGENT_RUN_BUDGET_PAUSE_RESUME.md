# PRD：Agent 长任务预算、暂停恢复与可靠完成判定

**状态**：Ready for Agent  
**日期**：2026-09-23  
**适用范围**：Agent Runtime、Session/Event、Tool Runtime、Multi-Agent、REST/SSE/WS、CLI、Web UI、Evaluation Gate  
**性质**：产品与工程交付契约；不替代冻结的 Engineering Specification。若两者出现实质冲突，执行者必须报告 Gap 并停止扩大范围。

## Problem Statement

### 当前状态

系统在多个入口把 `max_steps` 默认设为 10；Agent Runtime 的其他入口默认值又可能是 20。`max_steps` 实际统计的是模型作出决策的轮次，而不是工具调用次数；单轮可以包含多个工具调用。当前 Runtime 在模型返回工具调用后，会先等待整批工具执行完成，再发起下一次模型请求。

当第 10 个模型决策仍包含工具调用时，Runtime 会在执行这些调用前停止。因此，“调用 10 次工具就停”并不是准确的现状描述，但用户可观察结果相同：稍复杂的任务可能在尚未完成时被低位固定保险丝截断。

当前系统已经具备：

- append-only typed `SessionEvent`；
- 单一 `AgentRuntime` loop；
- 单一 `ToolExecutor` 执行路径；
- Tool retry、timeout、cancel、Operation Ledger 与 reconcile；
- 基于完全相同工具名与参数的重复失败护栏；
- detached run、SSE/WS 重连、resume、fork、replay；
- AgentProfile 与 Multi-Agent delegation。

当前系统尚不具备：

- 区分本地保险丝、Run 累计预算与 Session/委派树累计预算的统一模型；
- 命中预算后可恢复的暂停状态；
- 保留累计消耗和卡死指纹的同一 `run_id` 恢复；
- 基于行为模式和实际进展证据的 stuck detection；
- 在完成前检查所有在途工作已经静止的统一 CompletionPolicy；
- 覆盖后端、CLI、Web 的预算可见性和恢复交互；
- 真实模型与生产工具共同执行的强制 Live Gate。

### 问题

低位固定步数限制把安全保险丝变成了主要终止条件。它既不能表达 token、cost、deadline、工具配额等不同资源约束，也不能区分“已完成”“暂时没有预算”“卡住”“存在待 reconcile 副作用”。直接提高或删除一个数字，又会重新引入无限循环、无上限成本和失控委派风险。

此外，把“模型本轮没有请求工具”直接当作完成过于宽松。虽然当前单 Runtime loop 不会在上一批工具仍执行时再次调用模型，但系统还存在 Approval、Child Agent、Operation Ledger、reconcile 等跨组件在途状态。完成判定必须建立在 Runtime Quiescence 之上，而不能只检查最新模型消息。

### 目标状态

系统以“完成任务”为正常目标，以分层、多维预算和 stuck detection 作为安全边界：

- 默认允许复杂任务越过旧的 10 轮限制；
- 高位本地保险丝避免单个 AgentRuntime 无限运行；
- Run 与 Session/委派树预算累计、持久化、可观察；
- 命中可恢复边界时形成 continuation 并暂停，而不是伪造完成或失败；
- 恢复沿用同一 `run_id`，不重置累计消耗和失败指纹；
- 只有系统静止且 CompletionPolicy 通过时才能完成；
- 真实模型和生产工具 Live Gate 证明行为在真实链路成立。

## Goals

1. 取消低位 `max_steps=10` 对复杂任务的事实限制。
2. 建立 Deployment、AgentProfile、Session、Run、Tool policy 之间可解释的预算层级。
3. 为 agent turns、model requests、tool calls、tokens、cost、deadline、tool quota 和 delegation 提供独立计量。
4. 新增 durable、可恢复、非完成/非失败的暂停生命周期。
5. 使恢复、崩溃重建、fork 和 replay 的预算语义可对账。
6. 通过多模式 stuck detector 阻止重复错误、重复动作、无进展独白和交替循环。
7. 把 Runtime Quiescence 固化为完成判定的前置条件。
8. 在 REST、SSE/WS、CLI 和 Web UI 中提供一致的暂停、预算和恢复体验。
9. 通过确定性协议测试与真实 Live Gate 双重证明功能成立。
10. 保持 Provider、Tool、Sandbox、Memory、MCP 和 Multi-Agent 的既有边界不被绕过。

## Non-Goals

1. 不用 LangGraph、LangChain 或其他框架接管 Agent Loop。
2. 不新增第二条 Tool 执行路径。
3. 不把 Git、pytest、Todo 或某个具体 Coding Tool 的完成规则硬编码进 Core。
4. 不引入跨主机、分布式预算协调。
5. 不为所有工具设置任意的低默认调用次数。
6. 不改变 ToolExecutor 的 retry、timeout、cancel 和 Operation Ledger 责任边界。
7. 不让 replay 重新执行模型、工具或消耗预算。
8. 不在本 PRD 中实现代码，也不直接改写冻结的 Engineering Specification。
9. 不保证模型一定能完成本身不可完成、权限不足或外部依赖缺失的任务。
10. 不用 Mock/Fake 的成功替代真实模型与生产工具的交付证据。

## User Stories

1. As a user, I want a complex task to continue beyond ten model decisions, so that the agent can finish real project work.
2. As a user, I want the agent to stop safely when an explicit budget is exhausted, so that cost and runtime remain controlled.
3. As a user, I want a paused run to explain why it paused, so that I know what action is required.
4. As a user, I want to raise an absolute budget ceiling and resume the same run, so that prior work and accounting are preserved.
5. As a user, I want consumed budget never to reset during resume, so that a limit cannot be bypassed.
6. As a user, I want stuck runs to attempt one structured replan before pausing, so that recoverable mistakes do not require immediate intervention.
7. As a user, I want repeated stuck behavior to require a meaningful change before resume, so that “continue” cannot restart the same loop.
8. As a user, I want deadlines to stop new work at a stable boundary, so that in-flight side effects are not abandoned blindly.
9. As a user, I want uncertain side effects to enter `NEED_RECONCILE`, so that the agent does not replay a potentially completed mutation.
10. As a user, I want Web and CLI to show the same budget and pause truth, so that switching clients does not change reality.
11. As a user, I want refresh and stream reconnection to reconstruct pause state from SessionEvent, so that UI memory is not authoritative.
12. As a user, I want concurrent resume attempts to be rejected safely, so that two clients cannot spend the same budget version.
13. As an operator, I want Deployment hard ceilings, so that lower configuration layers cannot exceed platform policy.
14. As an operator, I want absent token or cost limits to remain unlimited but measured, so that observability does not require enforcement.
15. As an operator, I want an unenforceable explicit token or cost limit rejected before the first model request, so that the system never pretends to enforce it.
16. As an operator, I want provider and model identifiers recorded without credentials, so that live evidence is auditable and safe.
17. As an operator, I want Live Gate failures retained instead of rerun-selected away, so that stochastic failures remain visible.
18. As a runtime developer, I want separate counters for accepted agent decisions, actual provider requests and normalized tool calls, so that one metric is not overloaded.
19. As a runtime developer, I want Provider fallback requests counted individually, so that fallback does not hide real spend.
20. As a runtime developer, I want ToolExecutor retry attempts counted separately from logical tool calls, so that tool reliability and agent behavior remain distinguishable.
21. As a runtime developer, I want Runtime Quiescence checked before completion, so that dangling Approval, Child or Operation work cannot be mislabeled complete.
22. As a capability developer, I want a pluggable CompletionPolicy seam, so that domain-specific completion can be added without Core special cases.
23. As a multi-agent developer, I want every descendant to consume the same tree-wide SessionBudget, so that spawning a child cannot refresh limits.
24. As a multi-agent developer, I want each AgentRuntime to retain a local high fuse, so that one child cannot loop forever even when the shared budget is unlimited.
25. As a recovery developer, I want budget and stuck state rebuilt from durable events, so that process restarts do not enlarge authority.
26. As a recovery developer, I want replay to consume zero budget, so that observation is not mistaken for work.
27. As a fork user, I want a fork to receive a new SessionBudget with recorded parent snapshot and lineage, so that branches are independent but auditable.
28. As an API client, I want legacy `max_steps` requests to keep working during migration, so that existing integrations do not break immediately.
29. As an API client, I want contradictory legacy and new budget fields rejected with 422, so that the server never silently chooses one.
30. As a UI user, I want to see consumed versus allowed budget dimensions, so that I can decide whether to resume or change the task.
31. As a UI user, I want a stuck pause to require steer, environment change or policy change, so that a misleading one-click retry is unavailable.
32. As a test owner, I want deterministic state-machine regression tests, so that races and boundary conditions are reproducible.
33. As a test owner, I want mandatory real-model, real-tool Live Gates, so that mocks cannot certify a feature the product cannot execute.
34. As a reviewer, I want every gate tied to an exact commit/tree and preserved trace, so that the tested artifact is identifiable.
35. As a security reviewer, I want no credential value printed or persisted in evidence, so that real-provider tests do not leak secrets.

## Expected Behavior

### EB-1 — Completion-first default

**Given** no explicit token, cost, deadline, model-request or tool-call ceiling  
**And** the configured AgentProfile local fuse is not exhausted  
**When** a task requires more than ten decisions or tool calls  
**Then** the run continues until it completes, pauses for another defined reason, is cancelled, or fails  
**And** the former low default does not terminate it.

### EB-2 — Local high fuse

**Given** an AgentRuntime has made 500 accepted model decisions without completing  
**When** it reaches its local `max_agent_turns` ceiling  
**Then** it performs the reserved closeout flow and enters `run/paused`  
**And** it does not write `run/completed` or `run/failed` solely because the fuse was reached.

### EB-3 — Structured closeout

**Given** a budget boundary is approaching  
**When** sufficient reserved budget remains for closeout  
**Then** the model receives one bounded opportunity to describe completed work, remaining work, blockers and the next safe action  
**And** the resulting continuation is persisted.

**Given** the model closeout cannot run or does not produce a valid continuation  
**When** the Runtime pauses  
**Then** it persists a deterministic continuation assembled only from durable facts  
**And** it does not fabricate progress, success or tool results.

### EB-4 — Same-run resume

**Given** a run is paused with consumed counters and a budget version  
**When** an authorized client submits a higher absolute ceiling with the expected version  
**Then** the system appends `run/resumed` using the same `run_id`  
**And** preserves consumed counters, continuation and stuck fingerprints  
**And** increments the budget version.

### EB-5 — CAS conflict

**Given** two clients observed the same paused budget version  
**When** both attempt to update and resume it  
**Then** at most one succeeds  
**And** the other receives HTTP 409 without starting model or tool work.

### EB-6 — Explicit but unenforceable budget

**Given** a request specifies a token or cost ceiling  
**And** the selected Provider chain cannot supply the accounting required to enforce that ceiling  
**When** the client attempts to start or resume work  
**Then** the request is rejected before any model request  
**And** no budget-consuming event is written.

### EB-7 — Stuck detection

**Given** the Runtime observes one configured repeating behavior pattern  
**When** the pattern first reaches its threshold  
**Then** it emits a structured guard signal and allows exactly one corrective replan.

**Given** the same pattern persists after that replan without evidenced progress  
**When** the threshold is reached again  
**Then** the run enters `run/paused` with reason `stuck`  
**And** a plain resume without relevant steer, environment change or policy change is rejected with 409.

### EB-8 — Deadline

**Given** a configured deadline has passed  
**When** the Runtime reaches its next scheduling boundary  
**Then** it starts no new model request, tool call or child Agent  
**And** allows already-started tool work to finish only under its existing timeout/cancel policy  
**And** pauses at the next stable boundary.

**Given** an in-flight mutating operation cannot be proven completed or not started  
**When** deadline handling evaluates recovery  
**Then** the session enters `NEED_RECONCILE` instead of retrying it.

### EB-9 — Reliable completion

**Given** a model response contains no new tool calls  
**When** Tool, Approval, Child Agent, Operation Ledger or reconcile work remains in flight or dangling  
**Then** the run does not complete.

**Given** the Runtime is quiescent  
**And** the configured CompletionPolicy accepts the result  
**When** finalization runs  
**Then** and only then may the system append `run/completed`.

### EB-10 — Delegation tree budget

**Given** a root run delegates to children and grandchildren  
**When** any descendant consumes a shared budget dimension  
**Then** the same RunBudget and SessionBudget ledgers are updated atomically  
**And** creating or resuming a child does not reset or enlarge them  
**And** the tree-wide default `max_delegations` is 8.

### EB-11 — Fork and replay

**Given** a session is forked  
**When** the child session is created  
**Then** it receives a new SessionBudget ledger  
**And** records the parent lineage and parent budget snapshot.

**Given** events are replayed  
**When** a client reconstructs state  
**Then** no budget counter changes and no provider or tool is called.

### EB-12 — Backward compatibility

**Given** a client sends only `max_steps`  
**When** the request is valid under deployment policy  
**Then** it is interpreted as the root AgentRuntime local `max_agent_turns` ceiling.

**Given** a client sends both `max_steps` and the equivalent new field with different values  
**When** validation runs  
**Then** the request returns 422 before launching work.

### EB-13 — Cross-client truth

**Given** a run paused while the browser was disconnected  
**When** Web or CLI reconnects and replays SessionEvent  
**Then** it shows the same pause reason, continuation, consumed budget, absolute ceilings and version  
**And** neither client maintains a second authoritative state.

## Contracts

### 1. Counter semantics

| Counter | Fixed meaning |
| --- | --- |
| `agent_turns` | Count of accepted model decisions in one AgentRuntime. A decision is accepted after a provider response has been normalized and admitted to the loop. Rejected/transport-failed requests do not increment this counter. |
| `model_requests` | Every actual request sent to a Provider. Primary, fallback, closeout and child-agent requests each count independently. |
| `tool_calls` | Every normalized logical tool call admitted to ToolExecutor. One ToolExecutor retry remains one logical tool call. |
| `tool_attempts` | Every actual ToolExecutor attempt, including retry attempts. It is observable but is not an alias of `tool_calls`. |
| `total_tokens` | Sum of provider-reported input and output tokens included by the configured budget scope. |
| `cost_usd` | Sum of provider-attributed USD cost included by the configured budget scope. No price is fabricated when unavailable. |
| `delegations` | Count of admitted `delegate` calls across the delegation tree. |

### 2. Budget scopes and precedence

The runtime exposes three distinct controls:

1. **Local AgentRuntime fuse** — `max_agent_turns`; default 500. It applies to each AgentRuntime instance and is not pooled across siblings.
2. **RunBudget** — cumulative ledger for one logical `run_id`, including all descendants created by that run.
3. **SessionBudget** — cumulative ledger for the session and its delegation descendants across runs. Resume preserves it; fork creates a new ledger with lineage.

Configuration precedence is fixed:

```text
Deployment hard ceiling
  > AgentProfile default
    > Session/Run request override
      > Tool policy
```

Lower layers may narrow a ceiling but cannot exceed a higher layer. A request that attempts to enlarge authority above an applicable higher ceiling is rejected before work starts.

### 3. Budget input contract

Session creation, explicit resume and idle-session message launch accept an optional `budget` object. The public concepts are fixed; exact internal class organization is FREE.

```json
{
  "budget": {
    "expected_version": 3,
    "local": {
      "max_agent_turns": 500
    },
    "run": {
      "max_agent_turns_total": null,
      "max_model_requests": null,
      "max_tool_calls": null,
      "max_total_tokens": null,
      "max_cost_usd": null,
      "deadline_at": null,
      "tool_call_limits": {}
    },
    "session": {
      "max_agent_turns_total": null,
      "max_model_requests": null,
      "max_tool_calls": null,
      "max_total_tokens": null,
      "max_cost_usd": null,
      "deadline_at": null,
      "tool_call_limits": {},
      "max_delegations": 8
    }
  }
}
```

Fixed validation:

- Integer ceilings are positive integers or `null`; `null` means no ceiling at that scope.
- `max_cost_usd` is a non-negative decimal value or `null`; wire serialization must not require binary floating-point equality.
- `deadline_at` is an RFC 3339 UTC timestamp or `null`.
- `tool_call_limits` maps registered tool names to positive integer absolute ceilings.
- `expected_version` is required when updating a persisted paused budget and omitted for first creation.
- Omitted `budget` uses AgentProfile and deployment defaults.
- By default, model requests, tool calls, tokens, cost and deadline are observed but unlimited.
- SessionBudget defaults `max_delegations` to 8.
- `max_steps` remains a deprecated alias for `budget.local.max_agent_turns` during migration.
- If `max_steps` and `budget.local.max_agent_turns` are both present and equal, accept the request; if they differ, return 422.
- An active run cannot have ceilings lowered below already-consumed amounts.
- A resume update supplies absolute ceilings, never increments and never resets counters.

### 4. Budget projection contract

Session and active/paused run projections expose, for each scope:

- immutable identity;
- current `version`;
- configured absolute ceilings;
- consumed counters;
- remaining values when a ceiling exists;
- enforcement capability for token and cost dimensions;
- current deadline;
- last pause reason and continuation reference when paused.

Absent provider accounting is represented as unavailable, never as zero.

### 5. SessionEvent contract

Add these append-only durable event types:

#### `run/paused`

Required envelope fields: existing `session_id`, `run_id`, `seq`, timestamp and step identity conventions.

Required data:

- `reason`: `budget_exhausted | deadline | stuck`;
- `trigger_dimension`: budget dimension or stuck pattern that caused the pause;
- `budget_version`;
- `consumed` snapshot;
- `limits` snapshot;
- `continuation`: structured completed work, remaining work, blockers and next safe action;
- `closeout_source`: `model | deterministic`;
- `resume_requirements`: empty for ordinary budget/deadline pause; non-empty for stuck pause.

`run/paused` is durable and non-terminal: it stops active execution but does not close the logical `run_id`.

#### `run/resumed`

Required data:

- `from_pause_seq`;
- `previous_budget_version`;
- `budget_version`;
- `limits` snapshot after update;
- `consumed` snapshot, equal to the paused consumed snapshot before new work;
- `resume_basis`: `budget_increase | relevant_steer | environment_change | policy_change`.

#### Budget accounting events

The event model must make budget reconstruction deterministic after process loss. Implementers may use one event per admitted consumption, durable ledger events at existing stable boundaries, or another append-only representation, provided all Acceptance Criteria and replay equivalence hold. This representation is BOUNDED, not a mandated event name.

### 6. Run state contract

Projected run state includes:

```text
active | paused | completed | failed | interrupted | needs_reconcile
```

- `paused` is resumable and is not a terminal success/failure.
- `completed` and `failed` remain terminal.
- `needs_reconcile` takes precedence over budget resume until reconciliation is complete.
- Explicit user cancel retains existing immediate cancellation semantics and is not converted into budget pause.

### 7. CompletionPolicy contract

Core provides one pluggable CompletionPolicy seam.

Before invoking it, Runtime Quiescence must prove all of the following:

- no admitted tool call lacks its result or recovery classification;
- no ApprovalRequest is unresolved;
- no child Agent is active or awaiting recovery;
- no Operation Ledger record is pending or unknown without reconciliation state;
- no reconcile operation is pending;
- the latest accepted model decision requests no new tool call.

The default generic policy accepts a final model response after quiescence. Domain policies may add completion evidence but cannot bypass quiescence, permission or ledger checks.

### 8. Stuck detector contract

The default thresholds are:

| Pattern | Threshold |
| --- | ---: |
| same action and same error/result failure | 3 |
| same action and same observation | 4 |
| no-progress monologue/model decision | 3 |
| alternating two-pattern loop | 6 decisions |
| project-level no-progress window | 4 decisions |

Rules:

- Fingerprints are canonical and redact secret values.
- The first threshold hit produces one structured corrective replan.
- Persistence of the same pattern after replan produces `run/paused(reason="stuck")`.
- Relevant progress resets only the affected pattern. Cosmetic text changes, different call IDs and semantically equivalent argument churn do not count as progress.
- A stuck pause may resume only after relevant steer, environment change or policy change is durably recorded.
- The project-level no-progress threshold is an explicit project inference from mature stuck-detection designs, not a claimed upstream default.

### 9. REST behavior

- Existing create, resume and idle-message launch endpoints accept `budget` as defined above.
- Updating and resuming a paused run uses the existing session resume action surface; the request must identify the paused `run_id`, include `expected_version`, and state its `resume_basis`.
- Invalid shape or contradictory aliases return 422.
- Stale version, lowering below consumed usage, active-run conflict, missing required stuck change, and unreconciled side effects return 409.
- A request rejected with 409 or 422 starts no model/tool/child work.
- Existing clients that send only `max_steps` continue to work during the expand/migrate period.

### 10. SSE / WebSocket behavior

- `run/paused` and `run/resumed` are streamed using the same envelope and ordering guarantees as other durable SessionEvents.
- Reconnect/replay returns the same events from the append-only store; it does not synthesize a separate client state.
- A paused run closes its current live execution stream cleanly after the pause event is durable.
- Resuming the run opens/attaches to the normal run stream while retaining the same `run_id`.
- Duplicate frames are handled using existing sequence idempotency.

### 11. CLI behavior

- CLI shows pause reason, trigger dimension, consumed/limit values and continuation.
- CLI resume accepts a new absolute ceiling and the expected version.
- CLI does not offer plain continue for a stuck pause unless a valid resume basis is supplied.
- CLI obtains state from SessionEvent/projection, not process-local memory.

### 12. Web UI behavior

- Active and paused runs display configured ceilings and consumed counters.
- Paused state remains visible after refresh and reconnect.
- Budget/deadline pause offers an action to submit higher absolute ceilings and resume.
- Stuck pause requires the user to provide relevant steer or select a detected environment/policy change; a generic retry control is absent.
- 409 version conflict refreshes authoritative state and preserves the user's unsent input.
- UI must distinguish `paused`, `failed`, `completed` and `NEED_RECONCILE`.

## Implementation Decisions

1. Agent Runtime remains project-owned, Python and async-first.
2. The old low step cap is replaced by a layered budget model; it is not merely changed from 10 to another low number.
3. `max_agent_turns=500` is the default local safety fuse. Deployment may lower it; requests may not exceed Deployment or AgentProfile policy.
4. RunBudget and SessionBudget are distinct durable ledgers. Both cover the delegation tree for their scope.
5. SessionBudget has no default ceiling for turns, model requests, tool calls, tokens, cost or deadline, but usage is accumulated and displayed.
6. `max_delegations=8` is the only new default tool-specific quota fixed by this PRD.
7. Closeout capacity is reserved inside the applicable budget; it is not extra unaccounted work.
8. If model closeout is unavailable, deterministic continuation uses only persisted facts.
9. Deadline is cooperative at stable boundaries. Explicit cancel remains immediate under existing semantics.
10. Resume updates absolute ceilings with optimistic concurrency; it never resets consumed usage.
11. Fork creates a new SessionBudget and records the parent snapshot; replay consumes nothing.
12. Exact same-name/same-args failure detection expands into multi-pattern stuck detection while preserving one Runtime guard responsibility domain.
13. CompletionPolicy is optional and pluggable; Runtime Quiescence is mandatory and non-bypassable.
14. `max_steps` follows expand-contract migration. Removal is a later contract ticket after all in-repo callers and published clients have migrated.
15. Existing issue #287 owns durable tree-wide delegation budget/fingerprint mechanics. This feature must extend or depend on that work rather than create a competing tree context.
16. Web UI remains a projection of SessionEvent and server state.

## Constraints

### FIXED

- All 30 user decisions represented by this PRD.
- Contracts, counters, defaults, pause/resume semantics and thresholds in this document.
- Same `run_id` across pause/resume.
- Append-only typed SessionEvent truth.
- Single AgentRuntime and single ToolExecutor execution paths.
- Runtime permission and Sandbox boundaries; prompt text cannot grant authority.
- Tool retry remains owned by ToolExecutor; model fallback remains separate.
- Operation Ledger reconcile for uncertain side effects.
- Backward-compatible `max_steps` expand/migrate/contract sequence.
- Real-model and production-tool Live Gate requirements.
- Credentials must never be printed, persisted in evidence or committed.

### BOUNDED

- Internal budget ledger representation may be selected to match existing storage and checkpoint patterns, but must be atomically reconstructable and auditable.
- Existing modules may be refactored only where necessary to expose the budget/completion seams; unrelated behavior cannot change.
- UI layout and wording may follow the current design system, but all fixed states and actions must remain distinguishable.
- Stuck fingerprints may reuse or extend existing canonicalization, but must detect semantically equivalent churn and redact secrets.
- Test fixtures may create disposable workspaces and repositories; Live Gate must still use the configured real Provider and production Tool implementations.

### FREE

- Internal function and class names.
- Helper functions and local module organization.
- Choice of immutable/value-object techniques for internal budget arithmetic.
- Non-contractual UI spacing, icons and copy.
- Exact database/index optimization, provided append-only truth and replay equivalence remain intact.
- Test helper organization, provided the verification requirements are met.

## Testing Decisions

### Highest-value seams

1. **Primary seam: real Session/AgentRuntime execution through the public launch/resume surfaces.** This is the highest seam that simultaneously proves Provider, Agent Loop, ToolExecutor, durable events, streaming and projection behavior.
2. **Runtime state-machine seam:** deterministic tests cover exact budget boundaries, CAS races, restart reconstruction, quiescence and stuck fingerprints.
3. **Browser/CLI projection seam:** verify both clients render and act on the same persisted events.

No test-only execution path may be introduced to satisfy Live Gate.

### Deterministic regression requirements

- Boundary tests for every counter and precedence level.
- Alias compatibility and 422 conflict tests.
- Pause/resume event sequence and same-run identity tests.
- Budget version race tests proving one winner and one 409.
- Process-kill reconstruction of counters, pause state and stuck fingerprints.
- Deadline tests at model, tool-batch and child scheduling boundaries.
- Operation Ledger unknown-state tests proving `NEED_RECONCILE` precedence.
- Stuck pattern tests for thresholds, one replan, repeated pause and meaningful-reset rules.
- Completion tests for every quiescence blocker.
- Fork snapshot and replay-zero-consumption tests.
- SSE/WS reconnect and duplicate-frame projection tests.
- CLI and browser tests for paused, stuck, conflict and reconcile states.

Deterministic tests may use controlled doubles where required to force races and exact boundary states. They are regression evidence, not Live Gate evidence.

### Mandatory Live Gate

Live Gate uses:

- the deployment's configured real Primary Provider;
- the configured real Fallback Provider when testing fallback;
- production Tool Registry and ToolExecutor implementations;
- real file, command and Git operations inside a disposable Local or Docker Sandbox;
- no test-only “return the next step” tool;
- exact commit SHA and Git tree identity;
- provider/model identifiers without credentials;
- persisted SessionEvent/tool trajectory and machine-readable verdict.

Each scenario runs three consecutive times on the same commit/tree and configuration. Passing requires 3/3. Every failed attempt remains in evidence; rerunning and retaining only successful attempts is forbidden.

Required scenarios:

1. A real model completes a task that necessarily uses production tools beyond the former 10-turn/tool practical limit.
2. A low explicit budget causes `run/paused`; a higher absolute ceiling resumes the same `run_id` and completes without resetting consumption.
3. A real repeated tool failure causes exactly one replan and then `PAUSED_STUCK` if the same behavior persists.
4. A real deadline with an in-flight mutating tool starts no new work and resolves through normal completion or `NEED_RECONCILE`.
5. A real parent/child delegation tree shares cumulative budget and enforces `max_delegations=8`.

The Live Gate is a dedicated, reproducible command and is separate from the default credential-free test suite. Missing credentials, unavailable Provider/Sandbox or unexecuted scenarios are `BLOCKED` or `SKIPPED`, never `PASS`; affected implementation Issues cannot be closed.

### Existing prior art

Tests should extend the repository's existing seams for:

- Agent Runtime event-sequence golden tests;
- terminal-arm tests;
- real child-process kill/resume tests;
- Phase 13 Multi-Agent live tests;
- Phase 14 fork/resume live tests;
- Phase 16 full E2E gate;
- SSE disconnect/reconnect tests;
- Web browser E2E fixtures and real-backend integration lanes;
- review coverage and machine-written gate evidence.

## Acceptance Criteria

### AC-1 — Old limit removed

A mandatory Live Gate task performs more than ten accepted agent turns or more than ten production tool calls, completes successfully, and shows no `max_steps_exceeded` failure. It passes 3/3 on the same commit/tree.

### AC-2 — Defaults

With no explicit multidimensional budget, local `max_agent_turns` resolves to 500, SessionBudget dimensions remain unlimited except `max_delegations=8`, and all available usage counters are projected.

### AC-3 — Counter correctness

Tests prove the fixed meanings of `agent_turns`, `model_requests`, `tool_calls`, `tool_attempts`, tokens, cost and delegations across primary success, provider failure, fallback, tool retry and child Agent execution.

### AC-4 — Precedence

For every precedence boundary, a lower ceiling is accepted and an attempted enlargement beyond the effective higher ceiling is rejected before work starts.

### AC-5 — Unenforceable limits

When explicit token or cost enforcement is unsupported by the selected Provider chain, start/resume returns a validation error before the first provider request and before budget-consuming events.

### AC-6 — Pause is not completion/failure

Every budget, deadline or stuck boundary appends one `run/paused` and no `run/completed` or `run/failed` for that logical run at that point.

### AC-7 — Same-run resume

Resume appends `run/resumed` with the original `run_id`, unchanged consumed snapshot before new work, a higher budget version and a valid resume basis.

### AC-8 — CAS and idempotency

Concurrent identical resume submissions produce one effective update; stale or conflicting submissions return 409 and start no work.

### AC-9 — Crash durability

A real child-process kill after budget consumption and after pause reconstructs the same limits, consumption, version, continuation and stuck fingerprints without refresh.

### AC-10 — Stuck guard

Each fixed pattern threshold is covered. The first hit yields exactly one replan; persistence yields `run/paused(reason="stuck")`; plain continue returns 409; relevant change permits resume.

### AC-11 — Deadline and side effects

After deadline, no new model, tool or child work starts. In-flight work obeys existing timeout/ledger behavior, and uncertain mutations reach `NEED_RECONCILE` without blind retry.

### AC-12 — Completion quiescence

Completion is rejected independently for a dangling tool, unresolved approval, active child, pending/unknown operation and pending reconcile. A quiescent generic final response completes under the default CompletionPolicy.

### AC-13 — Delegation tree

Root, child, grandchild and racing siblings update one shared RunBudget/SessionBudget atomically; child creation and resume cannot enlarge or reset it; real delegation Live Gate passes 3/3.

### AC-14 — Fork and replay

Fork has a new SessionBudget identity plus parent snapshot/lineage. Replay produces byte-equivalent durable history and zero change to every budget counter.

### AC-15 — Compatibility

Legacy-only `max_steps` requests work as the alias; equal dual fields work; conflicting dual fields return 422. Removal does not occur until a separate contract ticket proves zero remaining callers.

### AC-16 — Streaming and projections

SSE/WS replay `run/paused` and `run/resumed` in durable sequence order. Web and CLI reconstruct identical projected state after refresh/reconnect and never invent pause truth.

### AC-17 — UI behavior

Browser verification proves budget display, pause reasons, continuation, absolute-ceiling resume, stuck-resume restriction, 409 refresh and distinction from completed/failed/reconcile states.

### AC-18 — Live evidence integrity

All five Live Gate scenarios have 3/3 evidence tied to the tested SHA/tree. No secret value appears in command output, event evidence, artifacts or committed files. A missing scenario prevents closure.

### AC-19 — Regression and gates

Relevant focused tests, full default tests, lint, type checks, frontend tests/build, review coverage, `git diff --check` and project Gate-0 pass on the integrated tree. Machine-written evidence is used wherever the project protocol requires it.

## Out of Scope

- Distributed/cross-host budget consensus.
- Organization billing, invoicing or prepaid credit systems.
- Automatic purchase of additional Provider credits.
- A general workflow DAG engine.
- Replacement of current Provider, Sandbox or Tool SDKs.
- Domain-specific completion policies for every future capability.
- Arbitrary default quotas for read, edit, bash, web or MCP tools.
- Silent auto-resume after budget/stuck pause.
- Deleting `max_steps` in the initial expand ticket.

## Risks and Required Mitigations

| Risk | Required mitigation |
| --- | --- |
| A higher fuse recreates expensive loops | Shared multidimensional budgets, stuck guard, deadline and operator ceilings remain independent. |
| Provider usage metadata is incomplete | Reject explicit unenforceable ceilings; never record missing usage as zero. |
| Resume races double-spend budget | Versioned CAS and idempotent resume admission. |
| Deadline interrupts side effects | Stable-boundary scheduling plus existing Tool timeout and Operation Ledger reconcile. |
| Child Agents reset limits | Reuse the durable tree context owned by #287. |
| Semantic argument churn bypasses exact fingerprinting | Canonical progress evidence and multi-pattern stuck detection. |
| UI invents state | Project exclusively from append-only SessionEvent/server projection. |
| Live tests are flaky or cherry-picked | Three consecutive attempts, preserve all failures, exact SHA/tree evidence. |
| Live tests leak credentials | Record identifiers only; redact canonical fingerprints and never print `.env` values. |

## Evidence and Mature-Product Basis

- [OpenAI Agents SDK run configuration](https://openai.github.io/openai-agents-python/ref/run/) — `max_turns` is an LLM-turn guard and can be handled or disabled.
- [OpenAI Codex engineering article](https://openai.com/index/unlocking-the-codex-harness/) — long-running conversations may include hundreds of tool calls and rely on compaction rather than a low universal tool cap.
- [Claude Agent SDK overview](https://platform.claude.com/docs/en/agent-sdk/overview) — independent turn and monetary controls support multidimensional limits.
- [Claude Code hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) — stop hooks may block completion but include loop protection.
- [LangGraph recursion limit](https://langchain-ai.github.io/langgraph/how-tos/graph-api/#impose-a-recursion-limit) — recursion limits are safety boundaries and explicit termination remains necessary.
- [Microsoft AutoGen termination](https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/termination.html) — termination conditions can combine message, token, timeout, handoff and external signals.
- [OpenHands stuck detection](https://github.com/All-Hands-AI/OpenHands) — mature Coding Agent prior art for repeated action/observation/error and alternating-loop detection.
- [Google ADK evaluation](https://adk.dev/evaluate/) — agent evaluation includes trajectory and task success.
- [Anthropic evaluation guidance](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests) — task-specific, automated, measurable and sufficiently repeated evaluation.
- WorkBuddy articles “Agent 长任务：怎么停得住又不忘事” and “Agent 跑几小时为什么不失忆” informed the layered-budget and continuation framing. Their unverified numeric suggestions are not adopted as product requirements.
- Repository research report “Agent Tool-Loop Termination Benchmark” contains the source-by-source comparison and caveats used for this decision.

## Further Notes

1. The current Runtime serializes model decisions and tool batches. The completion risk addressed here is therefore broader cross-component quiescence, not a claim that the same Runtime currently calls the model while its prior tool batch is still executing.
2. Existing issue #287 is a real dependency/overlap, not background reading. Ticket planning must establish one owner for the shared delegation-tree ledger.
3. The frozen Engineering Specification remains the higher project authority. Any required specification/ADR amendment must be an explicit first-class ticket and cannot be hidden inside implementation work.
4. This PRD specifies WHAT, WHY, BOUNDARY and DONE. Internal names and code organization remain with the executing Agent unless explicitly fixed above.
