# #297 / MEM-V2-1 — Typed Memory lifecycle vertical slice

**Priority:** P1

**Parent GitHub issue:** #296

**Parent PRD:** `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`

**Research:** `docs/research/2026-09-22-production-long-term-memory-systems.md`

## Objective

Deliver one complete V2 path that can create, read, version, invalidate, and search an authorized typed long-term memory through the existing Memory Capability/provider boundary, with SQLite as authority and Milvus as a derived index.

## Context

The current system stores a generic `MemoryEntry` with USER/SESSION scope. It has SQLite record storage, Milvus vector indexing, an outbox relay, a replaceable provider seam, and LangMem consolidation. V2 must distinguish Semantic, Episodic, and Procedural records, support user-global and project scope, retain superseded version history, and prevent SESSION history from becoming long-term-memory scope.

This is an expand step. It establishes the V2 contract without deleting current real memory data or switching run-end writeback. The destructive cutover is owned by MEM-V2-7.

## Current Behavior

- Records have generic content/metadata rather than a strict typed envelope.
- Implemented namespaces are USER and SESSION.
- Hard delete has no V2 content-free tombstone contract.
- Version/supersession/invalidation behavior is not exposed as the V2 lifecycle.
- Milvus and SQLite already synchronize through an outbox, but the V2 schema and replay invariants are absent.

## Desired Behavior

A caller can use the provider-neutral memory boundary to persist a valid V2 record, retrieve it under the correct identity/scope, create a successor version, invalidate it, and obtain consistent search results. Invalid or unauthorized records fail closed. Existing V1 operation remains runnable until the cutover ticket.

## Scope

### Must Do

- Add the PRD's V2 durable envelope and discriminated payload validation.
- Implement `semantic`, `episodic`, and `procedural` records.
- Implement `profile` and `collection` tiers with the rule that only user-global Semantic records may be Profile.
- Implement `user_global` and `project` scope using trusted tenant/user/project identity.
- Persist versions, active/superseded/invalidated status, provenance, source authority, timestamps, importance, and strength in SQLite.
- Derive Milvus index records from committed SQLite state through the project-owned outbox path.
- Support provider-neutral create/read/search/update/invalidate operations for V2.
- Preserve an adapter boundary so fake and non-LangMem providers can satisfy the same contract.
- Add or supersede the ADR decisions needed to resolve USER/SESSION, lifecycle, and provider-contract conflicts.

### Must Not Do

- Do not delete or transform the user's current real memory data.
- Do not make V2 run-end automatic writeback active.
- Do not implement the final deletion/tombstone API, Web UI, background model pipeline, or final recall ranking.
- Do not introduce Graphiti or a graph database.
- Do not let Milvus become the source of truth.
- Do not add provider-specific branches to AgentRuntime.

## Requirements

- **R1:** Runtime validation rejects missing fields, mismatched kind/payload, content over 500 characters, invalid score ranges, invalid tier/scope combinations, and request-supplied identity that conflicts with trusted context.
- **R2:** Semantic payload contains `subject`, `fact`, and category; Episodic contains `situation`, `action`, `outcome`, and `lesson`; Procedural contains `trigger`, `procedure`, and `success_condition`.
- **R3:** Every logical memory has a stable root identifier and monotonically increasing versions. At most one version of a logical memory is active.
- **R4:** Updating creates a new active version and marks the previous active version superseded. Invalidating removes a record from active retrieval without erasing its historical content.
- **R5:** A user-global record is visible only to the same tenant/user. A project record additionally requires the same trusted project/workspace identity.
- **R6:** `source_session_id` and source events are provenance, not retrieval scope.
- **R7:** Milvus write/delete failure cannot roll back an already committed SQLite fact; it remains a recoverable derived-index operation. Search must not return a Milvus hit whose SQLite record is unauthorized, missing, or inactive.
- **R8:** User-edit authority is represented in the record contract so later tickets can prevent assistant/tool evidence from overriding it.

## Contracts

The fixed record schema, enums, per-kind payloads, length/range limits, identity rules, persistence ownership, and provider ownership are defined in PRD §§4 and 6.1/6.6. This ticket may add internal fields needed for indexing or migration, but they cannot weaken or replace those fields.

V1 compatibility is temporary and bounded: both paths may coexist only until MEM-V2-7. New V2 code must not write SESSION-scoped long-term memory.

## Implementation Freedom

The execution agent may choose internal table layout, model/repository names, SQL migration organization, Milvus projection layout, and adapter decomposition. It may reuse or adapt existing stores and outbox types. It must preserve the provider seam and the fixed observable contracts.

## Acceptance Criteria

- **AC1:** Each valid kind round-trips through the real SQLite adapter with exact typed payload, scope, tier, provenance, importance, strength, and status.
- **AC2:** Boundary tests reject all invalid kind/payload, over-limit, invalid range, tier/scope, and untrusted-identity combinations without a partial SQLite or Milvus write.
- **AC3:** A project memory created in session A is readable in session B for the same user/project, and is unreadable in a different project, user, or tenant.
- **AC4:** Updating produces version N+1, leaves only N+1 active, marks N superseded, and preserves N in authorized version history.
- **AC5:** Invalidated and superseded versions never appear in active search.
- **AC6:** An injected Milvus failure leaves a committed SQLite record plus a recoverable outbox operation; replay converges the index exactly once.
- **AC7:** A forged/stale Milvus hit cannot expose missing, inactive, cross-project, cross-user, or cross-tenant content.
- **AC8:** The existing V1 test suite remains green before cutover, and an ADR explicitly records which old decisions will be superseded at MEM-V2-7.

## Dependencies

```text
blocked_by: none
blocks: #298 (MEM-V2-2), #299 (MEM-V2-3), #300 (MEM-V2-4)
parallelizable: no; this is the contract foundation
```

## Verification

- Focused model/schema and provider-contract tests.
- Real SQLite lifecycle, version, concurrency, authorization, and outbox-replay tests.
- Fake vector store fault injection plus the existing real-Milvus integration seam where available.
- Existing memory suite and API compatibility tests.
- Ruff and static/type checks applicable to changed code.

## Definition of Done

- All ACs have discriminating automated tests, including failure-path tests.
- ADR references and license notices for substantially reused upstream code are present.
- No V1 real data was deleted and no final cutover was performed.
- Review coverage identifies this issue and the implementation commit.
- Tracker/PHASE_STATUS record the commit and exact verification results after integration.

## Approved follow-up addendum: full derived-index recovery (2026-09-30)
The approved #297 follow-up adds an operator recovery path for total loss of the configured
Memory V2 index. The authoritative contract, design sources, reuse/build decision, fence
lifecycle, writer-stop precondition, and recovery semantics are defined once in ADR-0042 D7.1.
Scope is V2-only as approved; no live Milvus rebuild is claimed by this ticket addendum.

Implementation and fake-index tests are on `codex/mem-v2-297-index-rebuild`. On 2026-09-30,
independent Correctness/Security and Standards reviews passed after one P3 error-message fix;
the standards-source review also passed after adding official references and exact local source
citations. Review ranges are recorded in `docs/review_ledger.d/421-mem-v2-1-index-rebuild.tsv`
and the merge attribution in `docs/review_ledger.d/422-mem-v2-297-merge-8daf6b62.tsv`.
The branch is synchronized through `origin/main` `89bf106a` (latest merge `3e8e925a`). On the
combined code tree, the focused Python regression set passed 788 tests; the web projection test
passed 212 tests; and `npm run build` succeeded. A clean full-suite equivalent on commit
`49308fd9` (source tree unchanged by the later docs-only main sync; cleared `PYTHONPATH`, project
`.venv` interpreter, `pytest tests/ -q --no-header -p no:cacheprovider`) passed 4,836 tests, with
31 skipped, 51 deselected, and 9 existing warnings. The repository
clean-runner script could not start because this machine's `bash` resolves to WSL without
`/bin/bash`; the PowerShell invocation used the same environment and pytest arguments. The latest
`scripts/check_review_coverage.py` exits 0. The older `8daf6b62` merge report remains as historical
5/6 coverage-failure evidence. Final Gate-0 on commit `0f8b1ea5` / tree
`59e55a378acad91e0236466cc19397c1f7ab0f41` passed 6/6 in 26.0 seconds; its machine record is
`docs/gate/0f8b1ea5941fff7b91408c667234386fbde0ade6.json`. No live Milvus rebuild is claimed.
Actual execution remains an operator action after all writers to the configured collection are
stopped, including writers in other clones.

## 2026-09-30 follow-up review and regression closure

The branch was synchronized with `origin/main` `5b6af76a`. Full-range independent Spec and
Standards review covered the rebuild implementation and merge; Standards found a P2 in the
maintenance-lock flags, while Spec found no P0–P2 issue. The fix at `6d149d6a` makes cutover
and index-rebuild maintenance modes mutually exclusive. A new test failed before the fix
(`DID NOT RAISE`) and passed afterward. The combined lock, rebuild, and clean-slate regression
set passed 94 tests; Ruff and `git diff --check` passed. Review ranges and the merge analysis
are in `docs/review_ledger.d/424-mem-v2-297-full-review.tsv` and
`docs/review_ledger.d/425-mem-v2-297-maintenance-lock-fix.tsv`. The quoted-collation `where`
identifier observation is fail-closed and remains a low-priority compatibility limitation.
This evidence is local to `codex/mem-v2-297-index-rebuild`; it does not claim a live Milvus
rebuild or GitHub integration.

The follow-up Gate-0 run on commit `849c03d2` / tree `b1e30761` passed 6/6 in 28.7 seconds;
its machine record is `docs/gate/849c03d26d6260077ae92362505e2bf0bac60de4.json`. It covers the
six mechanical lanes only; the focused 94-test result above is recorded separately.
