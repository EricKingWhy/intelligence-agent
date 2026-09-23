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
