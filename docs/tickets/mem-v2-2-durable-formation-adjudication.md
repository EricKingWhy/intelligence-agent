# #298 / MEM-V2-2 — Durable Formation and Adjudication

**Priority:** P1

**Parent GitHub issue:** #296

**Parent PRD:** `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`

## Objective

Turn an eligible completed run into either a high-quality typed V2 memory change or an explicit no-write result through a recoverable two-stage background job with bounded retry and model fallback.

## Context

Current run-end extraction is process-owned work. It can use heuristic fallback content after model failure and can treat final answers or transient errors as memory candidates. V2 requires LLM-only Formation followed by Adjudication, strict runtime validation, no-write abstention, durable recovery, and a dedicated model role.

The memory-model aliases are fixed: `memory.primary` resolves to the locally configured `senseaudio` provider and `memory.fallback` resolves to `qwen`. This ticket must use the existing model-provider abstraction and must never expose local credentials or endpoint values.

## Current Behavior

- Every completed/controlled-failure run can feed the extractor too broadly.
- Parse/provider failure may degrade to rule-based extraction.
- Background tasks are owned by the current process and are not proven recoverable after restart.
- Memory extraction uses general model configuration instead of a dedicated primary/fallback role.
- Formation and consolidation do not share a single strict, auditable contract.

## Desired Behavior

Only eligible runs create one durable idempotent job. The job executes Formation and, if needed, Adjudication. Models may abstain. Runtime validates every result and applies only authorized actions through the V2 capability. Transient primary failures consume the fixed retry budget and then use fallback. Exhausted or invalid work ends degraded with no memory write and cannot write later unless a future approved pending-queue feature is implemented.

## Scope

### Must Do

- Define exact eligibility for normal completion and controlled terminal failure with genuine user input.
- Exclude cancellation, startup failure, no-model-call, no-user-input, explicit turn opt-out, and globally disabled extraction.
- Persist a recoverable memory job before user-visible run finalization releases ownership.
- Implement strict Formation and Adjudication result validation from the PRD.
- Apply candidate caps, content/evidence limits, per-kind rules, provenance checks, sensitivity policy, and user-fact source rules.
- Implement the approved primary/fallback attempt, time, call, token, per-user serialization, and global concurrency budgets.
- Remove heuristic/final-answer fallback from the V2 path.
- Emit redacted committed update/degraded events with stable reason codes.
- Make crash/restart and replay idempotent across job creation, stage transitions, and store commit.

### Must Not Do

- Do not delay the visible answer until memory formation finishes.
- Do not keep exhausted candidates in a future durable pending-candidate queue.
- Do not store raw prompts, provider responses, hidden reasoning, credentials, large tool output, or artifact content.
- Do not let model output supply trusted identity, permission, project ownership, or deletion authority.
- Do not make Langfuse availability a job success dependency.
- Do not implement retrieval/UI/cutover concerns owned by later tickets.

## Requirements

- **R1:** One eligible terminal run maps to one stable job idempotency key. Repeated finalization/recovery cannot create multiple active jobs.
- **R2:** Model input contains current-run safe projection, at most eight earlier user/assistant messages, at most ten similar active memories, tool names/status/structured summaries, and artifact references only.
- **R3:** Formation returns `CANDIDATES` or `NO_MEMORY`; parse/schema failure is a failed attempt, never an abstention.
- **R4:** At most five candidates proceed: Semantic ≤3, Episodic ≤2, Procedural ≤1, ranked by durable value before truncation.
- **R5:** Procedural automatic memory needs two independent success/correction events unless the user explicitly states the rule.
- **R6:** USER/profile facts require direct user evidence or explicit user confirmation. Assistant/tool evidence cannot independently create them.
- **R7:** Credentials/secrets are always rejected. Sensitive categories require an explicit remember request. Runtime enforcement is independent of model classification.
- **R8:** Adjudication returns exactly one of ADD/UPDATE/INVALIDATE/NOOP per candidate and passes provider-neutral authority/version validation before commit.
- **R9:** Primary receives initial + two transient retries; fallback receives initial + one transient retry. Non-transient authentication, permission, schema, and policy errors are not retried.
- **R10:** One job is bounded by 120 seconds, five calls, 32k cumulative input tokens, and 4k output tokens per call; exhaustion produces one terminal degraded result and no write.
- **R11:** Jobs serialize per user. Default global concurrency is four and configurable.
- **R12:** Committed changes emit `memory/updated`; terminal failure emits `memory/degraded`; NO_MEMORY/NOOP completes quietly without pretending to be an error.

## Contracts

Formation/adjudication schemas and reason enums are fixed by PRD §§6.2–6.3. Event privacy and fields are fixed by §6.5. Model aliases and budgets are fixed by §5.3. V2 records are written only through the provider contract delivered by MEM-V2-1.

## 票面更正（T6b · 2026-09-24，用户已批准）

T6 交付（`2f5a32e`）后的独立审查发现本票的一条**跨切片 P0**：证据引用链是断的。

- `projection.py` 按 R2 刻意不向模型投影任何事件信封字段（含真实 `event_id`，有测试钉着），
  而候选契约的 `evidence[].event_id` 与 `policy._reject` 的 provenance 判据都要求它是**本轮真实
  event id**。⇒ 模型在**结构上**无法产出可解析的证据，生产路径上每个候选都会落
  `unsupported_source`（自动记忆零写入），而观测上只显示"模型没给出可解析的证据"。
- 既有用例之所以全绿，是因为 fixture 把真实 `event_id` 直接写进了候选——**测试编码了一个模型
  到不了的世界**。这是本票反复惩罚的缺陷类型里最难自查的一种。

**批准后的修法（用户裁决"候选 A"）**：投影按出现顺序给本轮可被引用的事件发**运行时别名**
`e1`…`eN`（放进载荷的 `ref` 字段），`别名 → 真实 event_id` 的对照表留在
`FormationInput.refs`（**不进载荷**，只在运行时侧）；`policy.select_candidates` 以它为键空间；
执行器在**写盘前**把别名翻回真实 id（`executor._draft_from`）。

由此产生的票面级结论：

1. **`ref` 是本票引入的一个 wire 字段**，PRD §6.2 的字段表里没有它。判为 **Implementation
   Freedom 之内**（§7.3 明确"精确排序公式属实现自由"，本票的 Implementation Freedom 亦写明
   可自行决定 *prompt assembly internals*）：它不新开 R2 排除清单里的任何一类材料，真实
   `event_id` / `session_id` / `seq` 仍然不进模型输入，**AC9 的判据不变**。
2. `CandidateEvidence.event_id` **保留字段名**（契约冻结，改名要动 §6.2/§6.3 与 governance）；
   代价是它在生产路径上装的是别名，该语义差已在字段自身的 docstring 与本条里写明。
3. 别名对照表是**单射**（同一事件永远只拿一个别名，即使它被投影两次），否则反向建键会折叠、
   让载荷广告过的 `ref` 解析不到——同一类静默失败。判据用例：
   `test_the_same_event_never_gets_two_aliases`。
4. `refs=None` 时政策行为与改动前**逐字不变**，显式命令路径与既有纯函数用例不受影响。

**留给 T8 / ADR-0043 的事**（不在本切片范围）：别名机制的完整叙述（新 wire 字段 `ref`、
`CandidateEvidence.event_id` 的语义、`refs` 作为"模型能引什么"的唯一开关、以及"键空间的门
开在 `select_candidates` 的调用点而非 `_evidence_keys`"这一事实）必须写进 **ADR-0043**——
本票多处 docstring 已在引用该 ADR，而它尚未落盘；另，"重复 `tool_call_id` 是否该在事件准入侧
就堵掉"是 T6 之前既有行为，属范围外。

## Implementation Freedom

The agent must reuse the repository's existing SQLite/Event/Outbox persistence substrate and may add a memory-specific table or record type within it. It may choose lease/claim mechanics, stage persistence layout, and prompt assembly internals. It must not introduce a separate queue service or an in-memory-only owner. The chosen design must prove crash recovery, single-owner execution, idempotency, and terminal no-write behavior.

## Acceptance Criteria

- **AC1:** Normal completion and each approved controlled failure enqueue exactly one job; all excluded terminal shapes enqueue zero jobs and write zero memories.
- **AC2:** A `NO_MEMORY` result and an adjudication `NOOP` produce no record, no degraded event, and a terminal successful job state.
- **AC3:** Malformed, over-limit, mismatched, forged-identity, unsupported-source, secret, and unauthorized-sensitive outputs write nothing and follow the fixed retry/terminal rules.
- **AC4:** Injected transient primary failures prove exactly three primary attempts followed by at most two fallback attempts; successful fallback commits once.
- **AC5:** Injected non-transient failure proves no inappropriate retry or fallback and no write.
- **AC6:** Kill tests at job-persisted, Formation-completed, Adjudication-completed, and SQLite-committed/outbox-pending windows recover to one correct terminal outcome.
- **AC7:** Replaying a committed job creates zero duplicate active logical memories and does not emit a second logical update.
- **AC8:** A single event cannot create Procedural memory without explicit user rule evidence; two qualifying independent events can.
- **AC9:** Tests prove no secret reaches model input, persisted record, event, log, or trace, and stored content cannot grant runtime permission.
- **AC10:** The user-visible run answer completes without waiting for Formation/Adjudication under a deliberately slow memory model.

## Dependencies

```text
blocked_by: #297 (MEM-V2-1)
blocks: #302 (MEM-V2-6), #303 (MEM-V2-7), #304 (MEM-V2-8)
parallelizable: with #299 and #300 after #297
```

## Verification

- Provider-fake contract tests for deterministic Formation/Adjudication outcomes.
- Real AgentRuntime + SessionEvent completion and controlled-failure tests.
- Clock/call/token/concurrency budget tests with deterministic fake models.
- Process kill/restart tests against a real SQLite job store.
- Primary/fallback injected-failure tests without printing configuration values.
- Security probes for secrets, prompt injection, forged identity, and unauthorized source roles.
- Existing run finalization, memory, replay, and full backend regression suites.

## Definition of Done

- All terminal paths are explicit, persisted, observable, and tested.
- No heuristic V2 write path remains reachable.
- Exact attempt/budget and crash-window evidence is attached to the issue.
- Review coverage, tracker, and PHASE_STATUS are updated after integration.
