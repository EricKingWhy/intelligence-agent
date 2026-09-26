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
- **AC9:** Each public smoke selects the smallest answerable sample whose expected-answer word sequence appears verbatim after case and punctuation normalization in an authoritative user turn. LoCoMo must be a single-hop factual case (dataset category `4`), with evidence in an annotated relevant user turn. LongMemEval must be a `single-session-user` case and its evidence must occur in a `user`-role turn of a gold `answer_session_ids` session. Selection fails closed if no case qualifies. This source-evidence rule is separate from the ≥0.5 normalized token F1 threshold for the real generated answer and is used only for choosing one smoke case, not for answering or ranking memories.

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
- Selection tests cover the exact user-answer span, dataset-specific evidence scope and role mapping, LoCoMo single-hop and LongMemEval `single-session-user` restrictions, smallest-eligible selection, and fail-closed behavior.
- An authorized SiliconFlow reranker trial used the same LoCoMo selected-case hash in both runs. On the reranked run's same 14 authorized candidates, deterministic and reranked Recall@6 were both 1.0 (delta 0); the end-to-end answer F1 was 0.1 with reranking and 0.2 without, but formation produced 19 vs. 15 records, so that answer difference is not attributable to ranking. Both real chains verified, and both answer scores remained below 0.5. No reranker integration was retained because this trial did not demonstrate a quality gain. See `docs/evidence/memory-v2-public-smoke-locomo-41b256f4.json` and `docs/evidence/memory-v2-public-smoke-locomo-control-79ab6ce8.json`.
- Expanded LongMemEval user-turn evidence selection has one real smoke report: the eligible sample passed the selection F1 gate (0.631579), but formation degraded on `embedding_unavailable`; no memory was committed or injected, Recall@6=0, answer F1=0.421053, and `chain_verified=false`. Runner cleanup and an independent Milvus query confirmed zero temporary smoke collections. See `docs/evidence/memory-v2-public-smoke-longmemeval-6224d862.json`; this is failed evidence and does not satisfy the real-chain acceptance.
- An adapted-reader retry with working embedding/Milvus services still failed: LoCoMo Recall@6=1.0, chain verified, answer F1=0; LongMemEval Recall@6=0, chain unverified, answer F1=0.45. Both reports confirm temporary collection cleanup. The LongMemEval selected `single-session-assistant` case's matching user turn repeats the question rather than providing its answer; this exposes a false positive in the previous token-F1 evidence selector. See `docs/evidence/memory-v2-public-smoke-locomo-711d4b5c.json` and `docs/evidence/memory-v2-public-smoke-longmemeval-511bd604.json`. Neither satisfies AC9. The user then approved the stricter exact-span and `single-session-user` source rule now stated in AC9; it needs a new real run.
- The first real smoke on the strictly selected cases completed with working services but still failed generated-answer quality: LoCoMo hybrid Recall@6=0 / answer F1=0 / chain verified, with all expected answer tokens present in an injected authoritative profile; LongMemEval Recall@6=0 / answer F1=0 / chain unverified, with the authoritative answer tokens retained in active memory but not injected. Temporary collections were confirmed absent. Reports: `docs/evidence/memory-v2-public-smoke-locomo-071fbde5.json` and `docs/evidence/memory-v2-public-smoke-longmemeval-5daa9284.json`. A content-free diagnostic now records relevant hit rank and profile candidate/injection counts.
- Official-reader review found the adapter dropped LongMemEval `question_date` and added an abstention instruction to positive LoCoMo examples. Both compatibility gaps are corrected; the real smoke must be rerun on a clean committed tree before judging the change. No production ranking or the 0.5 answer-F1 threshold was changed.
- A retry with another authorized local embedding configuration failed the Milvus initialization probe with `embedding_unavailable` before formation and produced no report. Independent Milvus verification again found zero temporary smoke collections; the real-chain gate remains open pending a stable embedding service.
- On committed clean code tip `aff3e46f438d0d11ec6e4439f1e85f739f26bda7` (tree `045b396cab5feaaef1e4dce26f2a5cf109f25a76`), separate real public smokes passed for the strictly selected LoCoMo category-4 single-hop case and LongMemEval `single-session-user` case. Both had Recall@6=1.0, relevant hit rank 1, an injected relevant profile, `chain_verified=true`, and confirmed temporary collection cleanup; answer F1 was 0.666667 and 0.5 respectively against the 0.5 threshold. Reports: `docs/evidence/memory-v2-public-smoke-locomo-f22ae031.json` and `docs/evidence/memory-v2-public-smoke-longmemeval-6374b2d9.json`. Reports are content-free; cost is null because no trusted rate source is configured. These are non-blocking smoke artifacts, not frozen baselines.
- Real public smoke uses the same configurable recall timeout as production wiring.
- Eligible cases sort by total turn text length, then turn count, session count, and stable case ID; ties do not prefer stronger user evidence. Smoke reports content-free active memory tier/kind counts and uses a bounded 1,200-second formation drain.
- Mutation tests proving the gate detects below-threshold, duplicate, and unawaited/all-failed conditions.

## Definition of Done

- All blocking gates are machine-enforced and mutation-proven.
- Metadata-only privacy is demonstrated from captured trace payloads.
- Public baselines are reproducible and explicitly non-blocking for this release.
- Review coverage, tracker, and PHASE_STATUS are updated after integration.
