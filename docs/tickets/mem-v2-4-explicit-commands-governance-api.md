# #300 / MEM-V2-4 — Explicit commands and governance API

**Priority:** P1

**Parent GitHub issue:** #296

**Parent PRD:** `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`

## Objective

Give an authenticated user complete, authoritative control over V2 memories through explicit remember/forget behavior and a backward-compatible governance API.

## Context

The product has `remember_this`, memory retrieval/forget tooling, and authenticated list/delete endpoints. V2 requires exact command semantics, searchable typed records, source/version inspection, authoritative edits, bulk deletion, independent extraction/recall settings, and immediate content erasure with a 30-day content-free tombstone.

## Current Behavior

- Explicit remember can write generic content directly through the old consolidation path.
- List/delete endpoints expose a limited summary and hard-delete behavior.
- There is no V2 edit/version API, bulk delete, separate extraction/recall setting, or session recall explanation endpoint.
- Ambiguous natural-language forget behavior is not a fixed product contract.

## Desired Behavior

Explicit commands and HTTP operations use the same V2 authority/lifecycle boundary as automatic memory. Users can inspect and govern all visible memories. User edits are authoritative. Deletion erases content from authoritative and derived stores immediately, retains only approved tombstone fields for 30 days, and cannot be undone by replay.

## Scope

### Must Do

- Adapt explicit remember to V2 typed validation and explicit-command source authority.
- Implement exact turn opt-out, ambiguous forget selection, and global enable/disable semantics.
- Extend list/search/filter/detail/version APIs and add edit, bulk-delete, settings, and session recall-explanation APIs.
- Implement immediate content erasure, content-free tombstone creation, 30-day purge, and replay protection.
- Preserve trusted identity derivation and non-disclosing cross-identity behavior.
- Emit redacted audit/events for logical changes without memory content.
- Update/supersede the existing memory lifecycle and tool ADR contracts.

### Must Not Do

- Do not accept tenant/user/project ownership from request bodies or model output.
- Do not allow “remember” to bypass secret or sensitive-data rules.
- Do not silently pick one memory when “forget X” has multiple matches.
- Do not delete stored data when memory is merely disabled.
- Do not retain deleted content in tombstones, Milvus, outbox payloads, logs, traces, caches, or API responses.
- Do not build the Web UI in this ticket.

## Requirements

- **R1:** “Remember X” bypasses only the durable-value threshold; it still requires valid type/scope/payload, blocks secrets, and requires explicit consent for sensitive content.
- **R2:** “Do not remember this chat” suppresses Formation for that turn and does not change global settings.
- **R3:** “Forget X” deletes only an unambiguous authorized active match. Multiple matches return a selection set and mutate nothing until one is selected.
- **R4:** Disabling memory disables automatic extraction and recall independently according to the two stored settings and never deletes records.
- **R5:** User edits create a new `user_edit` version. Automatic assistant/tool evidence cannot later supersede or invalidate it.
- **R6:** List supports `q`, kind, status, scope, project, limit, and offset. Detail and version endpoints expose only authorized redacted provenance.
- **R7:** Single and bulk deletion erase content/evidence excerpts from SQLite and Milvus immediately and retain only ID, scope identifiers, timestamps, deletion reason, and hashes for 30 days.
- **R8:** Deletion is idempotent. Outbox replay, job replay, stale Milvus results, or delayed Formation cannot resurrect deleted content.
- **R9:** Bulk delete requires an explicit confirmation token and returns an affected count. It supports one kind or all visible memories.
- **R10:** Existing clients that only list and delete continue to receive compatible success/error behavior while gaining additive V2 fields.

## Contracts

The API paths and operations in PRD §6.4 are FIXED. Durable lifecycle, source authority, deletion, event privacy, and isolation in §§5.4, 5.6–5.7, and 6.5 are FIXED. Internal request/response type names are free; field meanings are not.

## Implementation Freedom

The agent may decide how natural-language command intent is connected to existing memory tools, how tombstone purge is scheduled, and how filters are expressed internally. It may reuse current endpoints/tools and add compatible fields. It must not create a second storage or authorization path.

## Acceptance Criteria

- **AC1:** Explicit remember creates a valid V2 `explicit_command` record and cannot store a credential/secret or unconsented sensitive record.
- **AC2:** Turn opt-out results in zero formation jobs for that turn and leaves settings unchanged.
- **AC3:** Ambiguous forget returns at least two selectable authorized candidates and deletes zero before selection; selection deletes exactly one.
- **AC4:** Extraction and recall settings can be changed independently, survive restart, affect subsequent runs, and do not delete data.
- **AC5:** Editing creates a new authoritative user-edit version; injected automatic assistant/tool contradiction cannot replace it.
- **AC6:** API tests cover every filter, pagination boundary, malformed payload, stale version, missing record, authorization boundary, and capability-degraded response.
- **AC7:** Single and bulk delete remove content from SQLite and Milvus immediately; tombstone inspection proves only allowed fields remain.
- **AC8:** After delete, forced replay of stale job/outbox/index work does not recreate or return content.
- **AC9:** Tombstones purge after 30 days under a controllable clock and leave no content-bearing residue.
- **AC10:** Existing list/delete consumers and tests remain compatible.

## Dependencies

```text
blocked_by: #297 (MEM-V2-1)
blocks: #301 (MEM-V2-5), #302 (MEM-V2-6), #303 (MEM-V2-7), #304 (MEM-V2-8)
parallelizable: with #298 and #299 after #297
```

## Verification

- Memory tool/command contract tests with secret, sensitive, ambiguous, and scope cases.
- FastAPI contract tests for all endpoints, filters, error shapes, and authorization boundaries.
- Real SQLite/Milvus deletion and 30-day clock-controlled purge tests.
- Crash/replay tests around delete versus pending write/index operations.
- Backward-compatibility tests for current Web client behavior.
- Security probes for forged identity, path/ID handling, content leakage, and prompt-granted permission.

## Definition of Done

- Every API and command behavior is independently testable and documented.
- Deleted content is absent from every content-bearing persistence/observability surface tested.
- ADR conflicts are explicitly superseded rather than left contradictory.
- Review coverage, tracker, and PHASE_STATUS are updated after integration.
