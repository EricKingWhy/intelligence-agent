# #302 / MEM-V2-6 — Privacy observability and quality evaluation

**Priority:** P1

**Parent GitHub issue:** #296

**Parent PRD:** `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`

## Objective

Make the V2 memory lifecycle measurable without leaking content, and enforce release-blocking quality/security gates using a frozen project gold set plus non-blocking public benchmark baselines.

## Context

Memory quality cannot be inferred from test pass count alone. V2 has explicit write precision, NOOP, type, contradiction, recall, fallback, isolation, idempotency, and deletion thresholds. Langfuse must remain optional and metadata-only for real user memory. LoCoMo/LongMemEval are useful external baselines but do not replace project-specific security and agent-product cases.

## Current Behavior

- Existing Langfuse integration can record broad content depending on global configuration.
- There is no frozen memory-specific gold corpus enforcing the approved thresholds.
- Public memory benchmark results are not captured as a reproducible baseline.
- End-to-end duplicate, pollution, token, latency, and cost metrics are not one release gate.

## Desired Behavior

Each stage exposes stable redacted metrics and reason codes. Real user content/evidence/raw prompts never enter Langfuse. A versioned project corpus produces deterministic blocking reports. Public benchmark adapters produce comparable non-blocking baseline artifacts. Any gate failure is machine-detectable and cannot be reported as success when all cases fail.

## Scope

### Must Do

- Define metadata-only Langfuse spans/observations for job, stage, model alias, action, reason, counts, IDs, kind, scope, tokens, cost, latency, retry, fallback, schema, and safety outcomes.
- Record content/evidence hashes but not raw real-user content, evidence, prompts, tool output, credentials, or provider response bodies.
- Build and version a project gold set covering positive writes, NOOPs, kinds, contradictions, explicit commands, source-role constraints, secrets, sensitive consent, isolation, replay, and cross-session recall.
- Implement machine-readable blocking gate calculations from PRD §8.2.
- Add LoCoMo- and LongMemEval-compatible baseline execution and artifact capture.
- Capture stored-record count, pollution rate, injected tokens, latency, and model cost alongside answer/recall quality.
- Prove Langfuse outage cannot fail memory Core or AgentRuntime.
- Prove the evaluation runner fails when zero cases succeed or awaited work fails.

### Must Not Do

- Do not upload real user memory content to Langfuse or public benchmark services.
- Do not make the first public benchmark score a blocking threshold.
- Do not tune the product solely to public benchmark examples.
- Do not silently skip configured real cases and return success.
- Do not retain temporary Milvus/Knowledge/Qiniu test data from evaluation.

## Requirements

- **R1:** Langfuse real-user observations are limited to the approved metadata and hashes. Synthetic dataset inputs may be retained when explicitly marked synthetic.
- **R2:** Every gold item declares expected eligibility, action, kind, scope, source authority, recall target, and prohibited outcomes as applicable.
- **R3:** Gate reports include corpus version, code/tree SHA, configuration aliases without values, case counts, numerator/denominator, threshold, pass/fail, latency, token use, and cost.
- **R4:** Blocking thresholds are: secret writes 0; unauthorized recalls/mutations 0; ineligible-trigger writes 0; NOOP accuracy ≥95%; write precision ≥95%; kind accuracy ≥90%; contradiction handling ≥95%; cross-session Recall@6 ≥85%; injected transient-primary fallback behavior 100%; duplicate active memories after replay 0.
- **R5:** Cases skipped for absent optional external credentials are reported as not executed and cannot contribute to numerator or produce an all-green real Gate.
- **R6:** Public baseline results include tool/version/configuration and are comparable across later runs. First accepted run freezes the baseline.
- **R7:** Trace/dataset/experiment duplicate checks distinguish intended repeated runs from accidental duplicate records.
- **R8:** Langfuse delivery failure emits a redacted local diagnostic record but cannot roll back or fail a valid memory transaction.

## Contracts

Privacy fields and gates are fixed by PRD §§6.5, 8.2–8.4. `memory/degraded` remains a product event, not a substitute for diagnostics. Langfuse is optional observation, not persistence or job ownership.

## Implementation Freedom

The agent may choose dataset file formats, report layout, evaluation runner decomposition, statistical summaries, and Langfuse span hierarchy. It may adapt upstream benchmark harnesses subject to licenses and must keep synthetic/public data separate from real-user data.

## Acceptance Criteria

- **AC1:** A trace inspection test proves raw user text, memory content, evidence excerpts, prompts, tool output, and credentials are absent while approved metadata is present.
- **AC2:** Each blocking metric is computed from a frozen versioned corpus and fails under a deliberate below-threshold mutation.
- **AC3:** A runner with all cases failed, skipped, unawaited, or zero executed exits non-zero and cannot publish a passing Gate.
- **AC4:** Injected Langfuse failure leaves Formation, Adjudication, storage, recall, and AgentRuntime behavior unchanged except redacted local diagnostics.
- **AC5:** The report distinguishes primary/fallback attempts and proves 100% approved behavior under injected transient primary failure.
- **AC6:** Replay cases produce zero duplicate active logical memories and the report catches a deliberate duplicate mutation.
- **AC7:** LoCoMo and LongMemEval compatible runs produce non-blocking baseline artifacts with version, SHA, metrics, tokens, latency, and cost.
- **AC8:** Real-gate cleanup verification reports zero temporary Milvus/Knowledge/Qiniu records while retaining the approved Langfuse dataset/experiment/trace evidence.

## Dependencies

```text
blocked_by: #298 (MEM-V2-2), #299 (MEM-V2-3), #300 (MEM-V2-4)
blocks: #304 (MEM-V2-8)
parallelizable: with #301 and #303 after dependencies are integrated
```

## Verification

- Unit/contract tests for metric denominators, thresholds, zero-case handling, and report schema.
- Synthetic Langfuse capture inspection and failure injection.
- Project gold-set run with deterministic fake providers.
- Real configured model/Milvus evaluation dry run without exposing values.
- Public benchmark adapter smoke and license/attribution review.
- Mutation tests proving the gate detects below-threshold, duplicate, and unawaited/all-failed conditions.

## Definition of Done

- All blocking gates are machine-enforced and mutation-proven.
- Metadata-only privacy is demonstrated from captured trace payloads.
- Public baselines are reproducible and explicitly non-blocking for this release.
- Review coverage, tracker, and PHASE_STATUS are updated after integration.
