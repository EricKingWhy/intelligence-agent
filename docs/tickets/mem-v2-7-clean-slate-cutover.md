# #303 / MEM-V2-7 — Clean-slate cutover and legacy-path retirement

**Priority:** P1

**Parent GitHub issue:** #296

**Parent PRD:** `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`

## Objective

Switch production memory behavior exclusively to V2, irreversibly remove polluted old long-term-memory data without retaining its content, and prove that unrelated project data is untouched.

## Context

The user explicitly approved a clean-slate reset because the current Milvus/SQLite memory dataset is polluted. The new V2 paths are built alongside legacy behavior in earlier tickets to keep the repository testable. This ticket is the contract step: remove legacy writes/reads, perform a tightly targeted reset, and prevent stale jobs/outbox operations from recreating old content.

This ticket authorizes deletion only of the exact Memory targets listed below. It does not authorize broad database, collection, filesystem, session, artifact, Knowledge, dataset, credential, or Langfuse deletion.

## Current Behavior

- V1 and V2 may coexist after the expand tickets.
- Old SQLite memory records/Profile state, memory outbox/projection work, and Milvus Memory collection contain untrusted low-quality data.
- Legacy SESSION-scoped and heuristic write paths may still exist for compatibility.

## Desired Behavior

Only V2 formation, lifecycle, retrieval, tools, API, and UI remain active. Old long-term-memory content is gone. V2 starts empty. Only the Milvus Memory collection is deleted/recreated. SessionEvent/chat/checkpoint/artifact/Knowledge/evaluation/credential data remains byte-for-byte or logically unchanged as applicable. No stale worker can resurrect deleted V1 content.

## Scope

### Must Do

- Resolve and display target identities/counts without displaying content before deletion.
- Stop/disable memory writers and acquire the ownership boundary needed for a consistent reset.
- Delete old SQLite memory records and Profile data.
- Delete old memory job/outbox/projection state that can reproduce old content.
- Delete and recreate only the configured Milvus Memory collection with the V2 schema.
- Remove/deactivate heuristic extraction, unconditional failure insert, V1 SESSION long-term writes, and V1 read/index compatibility paths.
- Start V2 with zero records and verify workers/replay remain at zero until a new user interaction.
- Supersede conflicting ADR decisions and update operational/reset documentation.

### Must Not Do

- Do not retain a content backup/export of old long-term memories.
- Do not delete SessionEvents, chats, sessions, checkpoints, artifacts, workspace data, Knowledge records/collections, evaluation datasets, Langfuse evidence, model configuration, or credentials.
- Do not delete every Milvus collection or use an unresolved collection name/pattern.
- Do not run against a dirty tree or while another memory writer owns work.
- Do not change quality thresholds or reintroduce a fallback write to make cutover pass.

## Requirements

- **R1:** Reset has a dry-run/read-only plan that identifies the exact SQLite database/schema objects, Memory collection name, record/outbox/job counts, and excluded domains without printing content or secrets.
- **R2:** The execution path refuses broad/wildcard/unresolved targets and refuses a Memory collection identifier that equals the Knowledge collection.
- **R3:** Writers are quiesced or fenced so no legacy/new transaction races the reset.
- **R4:** SQLite Memory records, Profile state, memory jobs, outbox, and projections capable of replaying old content are empty after reset.
- **R5:** Only the Milvus Memory collection is deleted/recreated, and its V2 count is zero after initialization.
- **R6:** SessionEvent/chat/session/checkpoint/artifact and Knowledge before/after identity/count/hash checks demonstrate no reset-induced change.
- **R7:** Deliberately staged stale legacy job/outbox work cannot recreate a V1 record after cutover.
- **R8:** The running application has no reachable heuristic fallback, V1 SESSION long-term write, or V1 retrieval path.
- **R9:** The reset is auditable using identifiers/counts/timestamps/result hashes without retaining memory content.

## Contracts

The authorized deletion set is exactly:

```text
DELETE: old SQLite long-term-memory records and Profile state
DELETE: Memory-only background job/outbox/projection state capable of replaying old content
DELETE/RECREATE: configured Milvus Memory collection only
PRESERVE: SessionEvent/chat/session/checkpoint/artifact/workspace data
PRESERVE: all Knowledge data and collections
PRESERVE: evaluation datasets and approved Langfuse evidence
PRESERVE: credentials and local provider configuration
BACKUP OF OLD MEMORY CONTENT: forbidden
```

V2 lifecycle, APIs, and events from the parent PRD become the only active memory contract after this ticket.

## Implementation Freedom

The agent may choose a migration command, startup migration, or controlled administrative operation, and may choose transaction/fencing mechanics. It must provide a dry-run mode or equivalent read-only target proof and must satisfy every preservation assertion.

## Acceptance Criteria

- **AC1:** Dry run resolves exact targets/counts, shows every preserved domain, exposes no content/credential, and performs zero mutation.
- **AC2:** Safety tests reject wildcard, empty, unknown, Knowledge-equal, and broader-than-Memory collection targets.
- **AC3:** After execution, authoritative V2 Memory count is zero and Milvus V2 Memory count is zero.
- **AC4:** Old Profile/job/outbox/projection state capable of reproducing V1 content is zero.
- **AC5:** A staged stale writer/replay operation after reset cannot resurrect an old record or vector.
- **AC6:** Before/after proofs show SessionEvent/chat/session/checkpoint/artifact/workspace and Knowledge data unchanged.
- **AC7:** Repository search and behavior tests show no reachable heuristic/final-answer fallback or SESSION-scoped long-term-memory write/read path.
- **AC8:** New post-cutover interaction can create a valid V2 memory, proving the empty store is functional rather than disabled.
- **AC9:** ADR and operator documentation clearly record irreversibility, exact targets, preservation boundary, and rollback limitation (code rollback does not restore deleted memory content).

## Dependencies

```text
blocked_by: #298 (MEM-V2-2), #299 (MEM-V2-3), #300 (MEM-V2-4)
blocks: #304 (MEM-V2-8)
parallelizable: planning/tests may proceed with #301 and #302; destructive execution must occur only after dependencies are integrated
```

## Verification

- Dry-run target-resolution and rejection tests.
- Isolated disposable SQLite/Milvus destructive rehearsal with populated Memory and preserved non-Memory fixtures.
- Kill/race/replay tests around writer fencing and stale operations.
- Repository reachability tests for removed legacy behaviors.
- Real integration reset with before/after counts/hashes and no secret/content output.
- Full memory, Session, Artifact, Knowledge, and recovery regression suites.

## Definition of Done

- The exact approved clean-slate reset has executed once against the intended environment with preservation proof.
- V2 is the only reachable memory behavior and starts empty.
- No old memory content backup was created.
- Evidence, ADRs, review coverage, tracker, and PHASE_STATUS are updated after integration.
