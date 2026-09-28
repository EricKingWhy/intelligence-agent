# PRD: Production Long-Term Memory V2

**Status:** Approved for ticketing

**Planning date:** 2026-09-23

**Scope:** Phase 6 Memory Capability / Context Provider hardening

**GitHub spec issue:** #296

**Source of truth:** this PRD plus its linked GitHub implementation issues

**Research basis:** `docs/research/2026-09-22-production-long-term-memory-systems.md`

## 1. Problem Statement

### 1.1 Current state

The product already has a replaceable Memory Capability, SQLite record storage, a Milvus vector index, an outbox relay, automatic run-end extraction, automatic context injection, memory tools, and basic list/delete Web UI.

The current automatic path does not meet a production long-term-memory bar:

- a completed run's final answer or a tool/error summary can become a memory candidate even when it has no durable value;
- extraction failure can fall back to heuristic content generation, turning uncertainty into stored noise;
- the current USER/SESSION split allows session-scoped records to be treated as long-term memory even though SessionEvent history already owns short-term conversation state;
- run-end writeback is process-owned work and is not proven recoverable after a crash;
- formation, quality adjudication, consolidation, retry, fallback, provenance, sensitivity, lifecycle, and user governance are not expressed by one strict contract;
- retrieval is primarily dense-vector based and does not provide the approved profile/collection budgets, cross-session project scoping, or a user-visible explanation of why a record was recalled;
- the current management API/UI is list/delete only;
- existing long-term-memory data is acknowledged to be polluted and must not be migrated into the new store.

SQLite remains the authoritative record store. Milvus remains a derived, rebuildable retrieval index. SessionEvent history, checkpoints, artifacts, and Knowledge are separate domains and must not be reclassified as long-term memory.

### 1.2 Problem

The system stores too much low-value material and cannot prove that an automatically stored memory is durable, attributable, safe, cross-session reusable, correctly superseded, or recoverable. This reduces retrieval precision and risks placing untrusted or sensitive material into future model context.

### 1.3 Target state

The product automatically forms only durable, reusable Semantic, Episodic, or Procedural memories through a two-stage model contract. It may abstain without writing. Accepted memories are versioned, attributable to source events, scoped to either the user or the current project, searchable across conversations, and governed by the user.

Formation is background work backed by durable project state. A primary memory model and a fallback memory model operate under explicit retry, deadline, call, and token budgets. Failure produces an observable degraded result and no memory write; it never becomes heuristic content.

The final cutover starts with an empty V2 long-term-memory store. Old long-term-memory records and indexes are deleted without preserving their content, while conversations, SessionEvents, checkpoints, artifacts, Knowledge data, evaluation datasets, and credentials remain untouched.

## 2. Goals

1. Store only information with durable cross-conversation value.
2. Support three memory kinds: Semantic, Episodic, and Procedural.
3. Support cross-conversation recall using global user memory plus current-project memory.
4. Keep short-term SessionEvent/checkpoint/runtime context separate from long-term memory.
5. Make formation and adjudication strict, typed, retryable, recoverable, and capable of abstention.
6. Preserve provenance, version history, supersession, invalidation, and deletion evidence without retaining deleted content.
7. Prevent credentials, secrets, unauthorized sensitive data, cross-user data, and cross-project data from entering or leaving the memory boundary.
8. Provide automatic recall, an explicit Memory Search Tool, explicit remember/forget commands, full API governance, and a usable Web UI.
9. Measure write precision, abstention quality, classification quality, contradiction handling, cross-session recall, fallback, idempotency, latency, token use, and cost.
10. Replace the polluted existing long-term-memory dataset with an empty V2 store after the new path is ready.

## 3. Non-Goals / Out of Scope

1. Introducing Graphiti or a graph database in this delivery.
2. Sharing memories across different users or teams.
3. Treating full conversation transcripts, SessionEvents, checkpoints, tool logs, or artifacts as long-term memories.
4. Retaining a content backup of existing long-term-memory records during the clean-slate reset.
5. Using an online LLM reranker during recall.
6. Building a durable pending-candidate queue that keeps retrying after both memory models exhaust their budgets. This is a later evolution; this delivery records a degraded terminal job result and writes nothing.
7. Allowing a memory provider, LangMem, Milvus, or Langfuse to own the Agent Runtime, identity, permission, retry, or persistence contract.
8. Guaranteeing a target score on public LoCoMo or LongMemEval during the first release. The first run establishes a non-blocking baseline; project-specific quality and security gates are blocking.

## Solution

Introduce a V2 long-term-memory lifecycle behind the existing Memory Capability/provider boundary. Eligible completed runs create a durable background job. Formation first decides whether durable information exists and emits bounded typed candidates. Adjudication then compares each candidate with authorized active memory and selects ADD, UPDATE, INVALIDATE, or NOOP. Runtime code validates every result, owns identity/policy/budgets, and persists accepted changes in SQLite; Milvus is updated as a derived index.

Recall combines a small trusted user Profile with hybrid retrieval from the same user's global memory and current-project collection. Users retain explicit search/remember/forget tools and gain full API/Web governance. Observability is metadata-only. After the V2 path is integrated and verified, a targeted clean-slate operation removes old Memory data and activates V2 exclusively.

## User Stories

1. As a user, I want stable preferences I state to be available in later conversations without repeating them.
2. As a user, I want reusable project facts and successful operating procedures to be recalled only inside the correct project.
3. As a user, I want one-off answers, transient errors, raw tool output, and ordinary conversation filler to be skipped rather than polluting my memory.
4. As a user, I want the system to remember automatically without asking me to approve every ordinary memory.
5. As a user, I want secrets never stored, and sensitive information stored only when I explicitly ask for it.
6. As a user, I want to say “remember,” “forget,” “do not remember this chat,” or disable extraction/recall and receive predictable behavior.
7. As a user, I want to inspect why a memory was recalled and where it came from without seeing hidden reasoning or unsafe raw evidence.
8. As a user, I want to search, edit, version, and delete my memories, including deleting a whole kind or everything.
9. As a user, I want edits I make directly to remain authoritative over later assistant/tool claims.
10. As a user, I want changed facts to replace or invalidate older active facts without losing authorized version history.
11. As a user, I want deletion to remove content immediately and prevent a delayed job or stale index from bringing it back.
12. As an operator, I want model extraction failures to retry and fall back within hard budgets, then fail visibly without writing guesses.
13. As an operator, I want memory work to survive process crashes and replay exactly once logically.
14. As an operator, I want quality/security thresholds and real-service evidence bound to a frozen code tree.
15. As an operator, I want Langfuse evidence for cost, latency, fallback, and decisions without real memory content or credentials.

## Implementation Decisions

1. Reuse/adapt LangMem only behind the replaceable provider seam; project runtime code owns identity, policy, retries, persistence, and observability.
2. Keep SQLite authoritative and Milvus rebuildable. No retrieval hit is trusted until authorized active SQLite state confirms it.
3. Treat Formation and Adjudication as distinct model stages with strict runtime-validated contracts and explicit abstention/no-op outcomes.
4. Use an expand-then-contract delivery: establish V2 while legacy behavior still runs, integrate all V2 consumers, then perform the approved destructive clean-slate cutover.
5. Keep Profile and Collection as access tiers, not separate truths. Both use the same V2 records and lifecycle.
6. Keep public benchmark results separate from blocking project gates so benchmark optimization cannot weaken product-specific precision or security.

## 4. Product Model

### 4.1 Memory kinds

| Kind | Durable meaning | Required payload |
| --- | --- | --- |
| `semantic` | Stable user preference, profile fact, project fact, constraint, or accepted correction | `subject`, `fact`, `category` (`preference`, `profile`, `project_fact`, `constraint`) |
| `episodic` | A reusable account of a situation, action, outcome, and lesson | `situation`, `action`, `outcome`, `lesson` |
| `procedural` | A reusable operating rule or successful procedure | `trigger`, `procedure`, `success_condition` |

Each record also contains a self-contained canonical `content` string used for display and retrieval. `content` must be no longer than 500 Unicode characters and must not rely on the originating conversation for interpretation.

### 4.2 Tiers

- `profile`: a compact trusted user profile that may be automatically injected on every eligible run. Only active `semantic` memories in `user_global` scope may enter this tier.
- `collection`: the searchable long-term archive for all three kinds.

### 4.3 Scope

- `user_global`: reusable across all conversations and projects for the same tenant and user. Intended for user preferences and profile facts.
- `project`: reusable across conversations only within the same tenant, user, and project/workspace identity.
- `source_session_id` is provenance only. It must never restrict recall and must not recreate SESSION-scoped long-term memory.
- No request may read or mutate another tenant's or user's records. Project-scoped records must not be recalled outside their project.

### 4.4 Trust and source

`source_type` is one of:

- `automatic`: accepted by the two-stage background pipeline;
- `explicit_command`: created by an explicit user “remember” request;
- `user_edit`: created or changed through the governance API/UI.

A USER/profile fact requires a direct user statement or an explicit user confirmation. Assistant and tool output may support project facts, episodes, or procedures, but cannot independently manufacture a user preference or profile fact. `user_edit` is authoritative and cannot be overwritten or invalidated solely by assistant/tool evidence.

## 5. Expected Behavior

### 5.1 Eligibility and abstention

1. **Given** a run reaches normal completion or a controlled terminal failure such as max-steps or repeated-error guard, and the run contains genuine user input, **when** run finalization occurs, **then** one durable memory-formation job is enqueued without delaying the user-visible answer.
2. **Given** a run was cancelled, failed before a model call, contains no genuine user input, or the user said “do not remember this chat,” **when** finalization occurs, **then** no formation job is created.
3. **Given** the formation model finds no durable reusable information, **when** it returns `NO_MEMORY`, **then** the job succeeds without creating or changing a memory.
4. **Given** parsing, provider, timeout, or budget failures exhaust the approved primary/fallback policy, **when** the job terminates, **then** no memory is written and one redacted degraded result is recorded.
5. **Given** a model call fails, **when** the system handles the failure, **then** it must not create a rule-based or final-answer fallback memory.

### 5.2 Formation and adjudication

1. **Given** an eligible run, **when** formation executes, **then** the model receives only the approved safe projection: the current run, at most eight prior user/assistant messages, at most ten similar active memories, tool names/status/structured summaries, and artifact references.
2. **Given** raw large tool output, artifact content, credentials, hidden reasoning, or unrestricted full history exists, **when** the formation prompt is built, **then** that material is absent.
3. **Given** formation returns candidates, **when** runtime validation executes, **then** no more than five candidates are accepted: Semantic at most three, Episodic at most two, Procedural at most one, total at most five.
4. **Given** a candidate exceeds a per-kind or total cap, **when** candidates are selected, **then** candidates are ranked by durable value and the lower-ranked excess candidates are discarded before adjudication.
5. **Given** an accepted candidate, **when** adjudication compares it with bounded relevant active memory, **then** it produces exactly one of `ADD`, `UPDATE`, `INVALIDATE`, or `NOOP`.
6. **Given** a single observed event, **when** a Procedural candidate is proposed, **then** it is rejected unless the user explicitly stated the rule. Otherwise Procedural memory requires at least two independent successful or corrective source events.

### 5.3 Model retry and fallback

1. **Given** a transient primary-model failure, **when** the memory job executes, **then** `memory.primary` receives one initial attempt plus at most two retries.
2. **Given** primary attempts are exhausted, **when** budget remains, **then** `memory.fallback` receives one initial attempt plus at most one retry.
3. `memory.primary` maps to the locally configured `senseaudio` provider alias. `memory.fallback` maps to the locally configured `qwen` provider alias. Credentials and endpoint values remain local ignored configuration and never enter repository documents, issues, logs, or traces.
4. Non-transient validation, authentication, permission, and policy failures are not retried.
5. One job has a 120-second wall-clock deadline, at most five model calls, at most 32,000 cumulative input tokens, and at most 4,000 output tokens per call.
6. Jobs are serialized per user. Global formation concurrency defaults to four and is configurable.

### 5.4 Versioning, contradiction, and deletion

1. **Given** a new fact supersedes an active memory, **when** `UPDATE` is applied, **then** a new version becomes active and the previous version becomes `superseded`; the previous content remains available in version history.
2. **Given** a fact is no longer true without a replacement, **when** `INVALIDATE` is applied, **then** the active record becomes `invalidated` and is excluded from recall.
3. **Given** the user deletes a memory, **when** deletion commits, **then** content and evidence excerpts are removed immediately from SQLite and Milvus. A content-free tombstone retains only IDs, scope identifiers, timestamps, deletion reason, and hashes for 30 days, after which it is purged.
4. **Given** an outbox relay or background worker restarts after deletion, **when** it replays outstanding operations, **then** deleted or pre-cutover content cannot be recreated.

### 5.5 Recall

1. **Given** an authenticated run with memory enabled, **when** context is assembled, **then** it may combine the same user's global profile with active memories from the current project.
2. **Given** a new conversation B, **when** a relevant memory was formed in conversation A, **then** B can recall it without using A's SessionEvent history as context.
3. **Given** candidate memories are retrieved, **when** ranking occurs, **then** dense similarity and keyword matching are combined, followed by deterministic filtering/reranking using status, type, importance, strength, scope, and retrieval decay. No online LLM reranker is called.
4. The always-visible Profile budget is at most 500 model-input tokens. Retrieved Collection memory is at most 800 model-input tokens, at most six complete records, and at most three records of any one kind.
5. Retrieved memory is injected as non-privileged data, not as instructions. The Memory Search Tool remains available for on-demand retrieval beyond automatic budgets.
6. Every automatically recalled memory exposes a redacted explanation containing its memory ID, kind, scope, source type, source/version reference, and deterministic ranking contributions. It does not expose another user's data, hidden prompt content, raw evidence, or secrets.

### 5.6 Explicit commands and controls

1. “Remember X” bypasses the durable-value threshold but not schema validation, scope rules, secret blocking, or sensitive-data consent rules.
2. “Do not remember this chat” suppresses formation for the current turn only.
3. “Forget X” deletes an unambiguous match; if multiple active memories match, the user must select the target and no candidate is deleted before selection.
4. Disabling memory turns off both automatic extraction and automatic recall but does not delete stored data.
5. Users can list, search, filter, inspect source/version, edit, delete one, delete by kind, delete all, and independently enable/disable automatic extraction and recall.
6. After an automatic write, the default UI notification is `已更新 N 条记忆`; memory content is shown only when the user expands the notification.

### 5.7 Sensitive information

1. Credentials, authentication tokens, private keys, passwords, secret keys, session cookies, and equivalent secrets are never stored, even if the user asks to remember them.
2. Sensitive categories may be stored only after an explicit user request to remember that content. Ordinary durable information may be stored automatically.
   Sensitive categories are: health/medical information, financial information, government or identity-document data, precise location, biometric data, intimate/sexual information, political or religious beliefs, legal matters, and information about minors.
3. Secret/sensitive policy is enforced outside the memory model as a runtime boundary. Model classification alone is insufficient.
4. Stored/recalled text is treated as untrusted external input and cannot grant permissions, change tool policy, or become privileged instructions.

## 6. Contracts

### 6.1 Durable memory envelope

Every V2 memory record has these required fields:

| Field | Contract |
| --- | --- |
| `id` | Stable opaque identifier |
| `schema_version` | Literal `2` |
| `root_id` / `version` | Stable logical-memory identifier and monotonically increasing positive version |
| `kind` | `semantic`, `episodic`, or `procedural` |
| `tier` | `profile` or `collection` |
| `scope` | `user_global` or `project` |
| `tenant_id`, `user_id` | Required trusted identity fields |
| `project_id` | Required only for `project`; absent for `user_global` |
| `content` | Self-contained canonical text, 1–500 Unicode characters |
| `payload` | Discriminated per-kind payload from §4.1 |
| `status` | `active`, `superseded`, `invalidated`, or `deleted` |
| `importance`, `strength` | Numeric values in inclusive range 0.0–1.0 |
| `source_type` | `automatic`, `explicit_command`, or `user_edit` |
| `source_session_id` | Origin-only session identifier |
| `source_event_ids` | Non-empty ordered set for automatic memories; may be empty only for direct UI edits without a session |
| `evidence` | One or more `{role, excerpt, hash}` items; excerpt max 300 characters; deleted tombstones retain hash only |
| `valid_at`, `invalidated_at`, `superseded_by` | Temporal/version fields; nullable when not applicable |
| `created_at`, `updated_at` | Server-owned timestamps |

Unrecognized enum values, missing required fields, over-limit content, mismatched payloads, untrusted identity fields, or invalid scope combinations fail closed and do not write.

### 6.2 Formation result

The model must return one JSON object validated by the runtime:

```text
decision: CANDIDATES | NO_MEMORY
candidates: [] | list[FormationCandidate]
skip_reason: null | no_durable_value | transient_only | unsupported_evidence |
             explicit_opt_out | no_user_input | sensitive_without_consent |
             secret_detected
```

`CANDIDATES` requires at least one valid candidate and a null `skip_reason`. `NO_MEMORY` requires an empty candidate list and a non-null `skip_reason`. A parse or schema error is a failed attempt, not `NO_MEMORY`.

Each candidate contains kind, tier, scope, canonical content, matching typed payload, importance, strength, evidence references, `sensitivity` (`ordinary`, `sensitive`, or `secret`), an applicable sensitive-category enum from §5.7, and the proposed project identifier when scope is `project`. Runtime replaces all identity fields with trusted request/session identity and independently enforces secret/sensitive policy.

### 6.3 Adjudication result

The model must return one JSON object per candidate:

```text
action: ADD | UPDATE | INVALIDATE | NOOP
target_memory_id: string | null
result: complete MemoryRecordV2 candidate | null
reason_code: durable_new | enrich_existing | contradicts_existing |
             user_authority_wins | duplicate | insufficient_evidence |
             procedural_threshold_not_met | policy_rejected
```

`ADD` requires no target and a complete result. `UPDATE` requires an active target and a complete result. `INVALIDATE` requires an active target and no replacement result. `NOOP` writes nothing. Runtime validates target ownership, version, source authority, and scope before applying the action.

### 6.4 API

Existing authenticated `/api/memories` list/delete behavior remains compatible while responses gain V2 fields.

- `GET /api/memories`: supports `q`, `kind`, `status`, `scope`, `project_id`, `limit`, and `offset`; returns only records visible to trusted identity.
- `GET /api/memories/{id}`: returns the visible current record and provenance summary.
- `GET /api/memories/{id}/versions`: returns visible versions in descending version order, excluding deleted content.
- `PATCH /api/memories/{id}`: accepts editable content plus the matching typed payload and creates a new `user_edit` version.
- `DELETE /api/memories/{id}`: performs the approved content-erasing deletion and returns an idempotent deletion receipt.
- `POST /api/memories/bulk-delete`: body is `{kind: <kind-or-null>, confirmation: "DELETE"}`; null kind selects all visible memories and any other confirmation value is rejected; returns affected count.
- `GET /api/memory-settings`: returns tenant/user-global `extraction_enabled` and `recall_enabled` for the current user.
- `PATCH /api/memory-settings`: updates one or both settings without deleting records.
- `GET /api/sessions/{session_id}/memory-recalls`: returns redacted “why recalled” records for that session.

All mutation endpoints derive tenant/user/project authority from trusted server context, never request-body identity. Cross-identity access returns the existing non-disclosing authorization/not-found behavior and never reveals whether another user's record exists.

### 6.5 Events and UI behavior

- `memory/updated`: emitted only after a committed logical change; contains count, memory IDs, action counts, and job ID, but no memory content.
- `memory/degraded`: preserves the existing event family and adds stable `stage`, `reason_code`, `job_id`, attempt count, and fallback-used fields; it contains no prompts, content, evidence excerpt, credential, or provider response body.
- `memory/recalled`: records IDs and redacted ranking explanation for a run; it contains no raw evidence or content from invisible records.
- A replayed job may re-emit no logical update when its idempotency key was already committed.
- Web UI uses the API as the only memory truth. It does not maintain a second durable memory state.

### 6.6 Persistence ownership

- SQLite is authoritative for records, versions, tombstones, settings, durable formation jobs, and outbox operations.
- Milvus stores derived vectors plus the minimum filter/projection fields needed for retrieval. It is rebuildable from SQLite.
- LangMem is an adapter used for typed formation/consolidation; it does not own persistence, identity, retry, fallback, policy, or lifecycle.
- Langfuse is observational only and cannot be required for Core success.

## 7. Constraints

### 7.1 FIXED

1. All 36 approved product decisions represented in this PRD.
2. Memory remains a Capability + Context Provider behind the existing provider seam.
3. SQLite authority, Milvus-derived-index architecture, and tenant/user/project isolation.
4. Two-stage Formation then Adjudication with strict runtime validation.
5. No heuristic memory fallback and no write after exhausted extraction/adjudication failure.
6. Automatic background writes, cross-conversation recall, and separate short-term/long-term domains.
7. The retry, fallback, token, deadline, concurrency, candidate, content, evidence, and injection budgets in this PRD.
8. Secret prohibition, sensitive-data consent, non-privileged recall, and user-edit authority.
9. Versioning, supersession/invalidation, deletion tombstones, and complete user governance.
10. Clean-slate deletion of old long-term-memory data without a content backup.
11. Real final Gate only after all implementation tickets are integrated.

### 7.2 BOUNDED

1. Existing modules may be refactored and new internal modules may be introduced, but AgentRuntime must not acquire provider-specific branches.
2. Existing APIs may gain backward-compatible fields and filters; breaking changes require an approved replacement contract and migration.
3. The durable job must reuse the existing SQLite/Event/Outbox persistence substrate. A memory-specific table or record type inside that substrate is permitted; a separate queue service or in-memory-only owner is not. It must meet recovery, idempotency, ownership, and observability acceptance criteria.
4. Dense/keyword fusion and deterministic ranking weights are implementation choices, but the fixed filters, budgets, quality gates, and no-online-LLM-reranker constraint apply.
5. LangMem internals may be reused or adapted with license attribution. Equivalent behavior may be implemented behind the provider seam if LangMem cannot satisfy a contract.
6. The Web UI may choose local component organization and interaction details while preserving the fixed API behavior, accessibility, confirmation, and visible states.

### 7.3 FREE

1. Internal function, class, helper, and module names.
2. Internal table names and repository layout where they are not externally persisted contracts.
3. Exact deterministic rank formula and index implementation within fixed gates.
4. Visual styling and non-contractual copy beyond the required generic update message.
5. Test fixture organization and private implementation patterns.

## 8. Testing Decisions

### 8.1 Approved test seams

1. **Memory Capability/provider seam:** typed lifecycle, versioning, identity/scope, SQLite authority, Milvus derivation, and provider replacement.
2. **Real AgentRuntime + SessionEvent recovery seam:** trigger eligibility, durable background job, retry/fallback, crash/restart, idempotency, and event contracts.
3. **FastAPI + Web UI seam:** governance API contracts plus accessible user workflows at both supported viewport sizes.
4. **Real integration/evaluation seam:** real configured memory models, dedicated Milvus collection, metadata-only Langfuse, quality corpus, public baseline, and cleanup proof.

### 8.2 Blocking project gates

| Gate | Pass condition |
| --- | --- |
| Secret write | 0 secret records across direct, automatic, fallback, replay, API edit, and explicit remember cases |
| Isolation | 0 cross-tenant, cross-user, or cross-project unauthorized recalls/mutations |
| Ineligible trigger | 0 writes for cancellation, startup failure, no model call, no genuine user input, and explicit turn opt-out |
| NOOP accuracy | At least 95% on the frozen project gold set |
| Write precision | At least 95% accepted memories judged durable and correctly attributable on the frozen project gold set |
| Kind accuracy | At least 90% correct Semantic/Episodic/Procedural classification |
| Contradiction handling | At least 95% correct old-version supersession/invalidation |
| Cross-session Recall@6 | At least 85% on the frozen project cross-session set |
| Fallback | 100% of injected transient primary failures use the approved fallback path or terminate degraded without a write |
| Idempotency | Replaying a committed job creates 0 duplicate active logical memories |
| Deletion | Deleted content absent from SQLite and Milvus immediately; only allowed tombstone fields remain |
| Cutover | V2 SQLite and Milvus memory count 0 after reset; no old job/outbox replay can repopulate them |

### 8.3 Non-blocking initial public baseline

Run LoCoMo- and LongMemEval-compatible evaluation and record answer quality, stored-record count, pollution rate, injected tokens, latency, and cost. The first accepted run freezes a baseline; later releases must not introduce a statistically or operationally significant regression without an approved explanation.

### 8.4 Real Gate hygiene

- Use a dedicated, uniquely named Milvus memory collection and clean it after verification.
- Preserve the Langfuse test dataset, experiment, and trace as release evidence; verify no unexpected duplicate records.
- Langfuse records metadata and hashes only. Synthetic evaluation inputs may be retained; real user content may not.
- Credentials are checked only for presence. Values are never printed.
- The clean-slate reset targets only Memory data. Knowledge collections and data must be shown unchanged.

## 9. Acceptance Criteria

1. Every Given/When/Then behavior in §5 has an automated test at one of the approved seams.
2. All schemas and APIs in §6 are validated with positive, boundary, malformed, and authorization tests.
3. The blocking gates in §8.2 pass on a frozen, versioned project gold set.
4. A real run in conversation A forms an approved memory and a distinct conversation B recalls it under the correct user/project scope.
5. Injected primary-model transient failure proves the `senseaudio` alias attempt budget and the `qwen` fallback path without exposing configuration values.
6. Kill/restart tests prove that an eligible background job is not lost, a committed job is not duplicated, and an exhausted job cannot write later.
7. API and Web UI tests prove list/search/filter/source/version/edit/delete/bulk-delete/settings/why-recalled workflows, including authorization and both supported viewports.
8. Secret and prompt-injection probes prove stored memory cannot grant permission or enter privileged instruction channels.
9. Clean-slate cutover proves old SQLite memory/Profile/job/outbox/projection data and only the Milvus Memory collection are removed; SessionEvents, chats, checkpoints, artifacts, Knowledge, datasets, and credentials remain intact.
10. The real Gate leaves Milvus/Knowledge/Qiniu temporary test data at zero while preserving the approved Langfuse evidence.
11. Relevant ADRs are added or superseded so the new scope, tombstone semantics, provider ownership, retrieval behavior, and clean-slate decision do not conflict with ADR-0008, ADR-0009, ADR-0024, ADR-0026, or ADR-0031.
12. Ruff, backend full pytest, review coverage, frontend typecheck, Vitest, Oxlint, production build, and full two-viewport Playwright gates pass on the integrated tree.

## 10. Ticket Map

1. #297 / MEM-V2-1 — Typed Memory lifecycle vertical slice.
2. #298 / MEM-V2-2 — Durable Formation and Adjudication.
3. #299 / MEM-V2-3 — Cross-session Profile and hybrid recall.
4. #300 / MEM-V2-4 — Explicit commands and governance API.
5. #301 / MEM-V2-5 — Memory management Web UI.
6. #302 / MEM-V2-6 — Privacy observability and quality evaluation.
7. #303 / MEM-V2-7 — Clean-slate cutover and legacy-path retirement.
8. #304 / MEM-V2-8 — Final real Gate and release evidence.

Dependency order is defined in the ticket documents and their GitHub issue bodies.

## 11. Reuse and Reference Decisions

- **LangMem — REUSE + ADAPT:** typed schemas and bounded consolidation operations.
- **Hermes — PORT DESIGN:** aggressive save/skip curation, bounded profile concepts, and background review precedent; do not copy flat-file storage.
- **Graphiti — PORT DESIGN only:** provenance and temporal invalidation fields; defer graph backend.
- **Letta — PORT DESIGN:** compact profile versus searchable archive separation.
- **Mem0 — PORT DESIGN:** role-aware evidence discipline, lifecycle operations, hybrid retrieval ideas, and public benchmark harnesses; do not adopt high-recall ADD-all behavior.

License and pinned-source references are recorded in the research document. Substantial copied code must retain required notices.

## 12. Further Notes

- This PRD intentionally supersedes the old assumption that SESSION-scoped records are a long-term-memory tier. Session-scoped conversation state remains available through SessionEvent history and runtime context.
- This PRD changes memory deletion from immediate no-tombstone hard delete to immediate content erasure plus a 30-day content-free tombstone. The implementation ticket must record the ADR supersession explicitly.
- The future durable pending-candidate queue is not hidden in this release. Exhausted jobs end degraded/no-write and require a later approved ticket to be reconsidered automatically.
- No implementation ticket may silently relax a threshold or contract because a model/provider cannot meet it. New evidence that invalidates a ticket must follow the project's Analyze → Report → Ask workflow.
