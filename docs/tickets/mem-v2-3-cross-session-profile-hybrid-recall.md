# #299 / MEM-V2-3 — Cross-session Profile and hybrid recall

**Priority:** P1

**Parent GitHub issue:** #296

**Parent PRD:** `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`

## Objective

Recall useful V2 memories across conversations through a bounded global Profile plus project-scoped hybrid retrieval, with deterministic ranking and a redacted explanation of every automatic recall.

## Context

The product already has a Memory Context Provider and Memory Search Tool. Current retrieval primarily uses vector similarity with a simple importance/recency formula. V2 separates a compact trusted profile from a searchable collection, combines dense and keyword evidence, and treats recalled text as non-privileged data.

## Current Behavior

- USER and SESSION namespaces blur long-term and short-term scope.
- There is no approved user-global plus current-project merge contract.
- Profile and collection injection budgets are not enforced.
- Keyword retrieval and redacted “why recalled” evidence are absent.
- Existing memories may be injected without the V2 status/type/authority checks.

## Desired Behavior

A new conversation can use durable memories created in earlier conversations for the same user and project. A small user-global Profile is injected within its own budget. Collection retrieval combines dense and keyword signals, applies deterministic filters/ranking, and injects only complete active records within fixed limits. Every automatic recall can be explained without exposing raw evidence or unauthorized data.

## Scope

### Must Do

- Build Profile selection/injection for active user-global Semantic profile records.
- Build collection retrieval over user-global plus current-project active records.
- Combine dense and keyword retrieval before deterministic filtering/reranking.
- Enforce status, identity, project, kind, importance, strength, source authority, and retrieval-decay rules.
- Enforce Profile and Collection token/count/per-kind budgets.
- Keep the Memory Search Tool for explicit deeper retrieval through the same authorized search seam.
- Record a redacted recall explanation and expose it through the approved session API/event contract.
- Degrade optional recall failures without failing AgentRuntime.

### Must Not Do

- Do not use SessionEvent transcript search as long-term-memory retrieval.
- Do not retrieve another user/tenant/project's records.
- Do not use an online LLM reranker.
- Do not inject partial/truncated memory entries.
- Do not place recalled text into a privileged instruction channel.
- Do not introduce Graphiti or make LangMem/Milvus visible to AgentRuntime.

## Requirements

- **R1:** Profile contains only active user-global Semantic profile records and consumes at most 500 model-input tokens.
- **R2:** Collection recall consumes at most 800 model-input tokens, at most six complete records, and at most three of any one kind.
- **R3:** Retrieval candidates combine dense similarity and keyword evidence, then use deterministic status/type/scope/importance/strength/decay rules. Exact weights are internal but must be versioned or observable for evaluation.
- **R4:** Superseded, invalidated, deleted, unauthorized, or wrong-project records are excluded even if Milvus or keyword search returns them.
- **R5:** Retrieved text is clearly delimited as untrusted memory data and cannot override system policy, permissions, tool policy, or user instructions.
- **R6:** On-demand Memory Search Tool and automatic context use one provider-neutral authorization/search truth, while applying their different result budgets.
- **R7:** Recall explanation includes memory ID, kind, scope, source type, version/source reference, and deterministic score contributions without raw evidence or invisible content.
- **R8:** Retrieval failure emits/records a redacted degraded reason and allows the run to continue without memory.

## Contracts

Scope/tier and injection limits are fixed by PRD §§4.2–4.3 and 5.5. `memory/recalled` and `/api/sessions/{session_id}/memory-recalls` follow PRD §§6.4–6.5. The implementation consumes only the V2 provider seam from MEM-V2-1.

## Implementation Freedom

The agent may choose keyword index technology already available in the repository/platform, score normalization, fusion method, deterministic weights, and token-counting internals. No new infrastructure dependency should be added when SQLite/native capabilities or installed dependencies satisfy the gate.

## Acceptance Criteria

- **AC1:** A memory formed/seeded in conversation A is automatically recalled in separate conversation B for the same identity and project.
- **AC2:** User-global Profile is available in another project for the same user; project Collection memories are not.
- **AC3:** Cross-tenant, cross-user, and wrong-project recall count is zero across dense, keyword, forged-index, and Memory Search Tool paths.
- **AC4:** Profile and Collection outputs never exceed their token/count/per-kind budgets and never truncate an entry.
- **AC5:** Superseded, invalidated, and deleted records are absent from automatic and tool retrieval immediately after authoritative status changes, including while derived-index work is pending.
- **AC6:** A lexical-only match and a semantic-only match are both retrievable in the hybrid lane; deterministic reranking is reproducible.
- **AC7:** Prompt-injection text stored as memory is visibly data-delimited and cannot grant a tool permission or change runtime policy.
- **AC8:** “Why recalled” returns the correct authorized record and ranking factors without raw evidence, prompt content, secret values, or another user's existence.
- **AC9:** Injected Memory provider/Milvus failure does not fail the agent run and produces one redacted degradation record.
- **AC10:** Frozen project cross-session evaluation reaches Recall@6 ≥85% before release.

## Dependencies

```text
blocked_by: #297 (MEM-V2-1)
blocks: #302 (MEM-V2-6), #303 (MEM-V2-7), #304 (MEM-V2-8)
parallelizable: with #298 and #300 after #297
```

## Verification

- Provider-fake ranking/filter/budget tests.
- Real SQLite plus fake and real Milvus hybrid retrieval integration tests.
- Real AgentRuntime cross-session context tests.
- Memory Search Tool parity and authorization tests.
- Prompt-injection and forged-index security probes.
- Token budget/property tests and frozen Recall@6 corpus.
- Existing Context Provider failure-isolation regression tests.

## Definition of Done

- Cross-session recall is demonstrated at the real runtime seam.
- All privacy, budget, status, and injection-boundary ACs have discriminating tests.
- Ranking configuration and evaluation evidence are reproducible.
- Review coverage, tracker, and PHASE_STATUS are updated after integration.
