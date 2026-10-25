# PRD: Production Long-Term Memory V2

**Status:** Approved for ticketing

**Planning date:** 2026-09-23

**Scope:** Phase 6 Memory Capability / Context Provider hardening

**GitHub spec issue:** #296

**Source of truth:** this PRD plus its linked GitHub implementation issues

**Content authority (updated 2026-10-23):** ADR-0056 §3 fixes the memory content authority as Markdown files, with operational state under named owners. Where this PRD's original text said "SQLite owns everything" or described tenant/user/project memory routing, the ADR-0056 wording governs; the affected sections were revised in #1543 and the superseded wording is marked in place.

**Research basis:** `docs/research/2026-09-22-production-long-term-memory-systems.md`

## 1. Problem Statement

### 1.1 Current state

The product already has a replaceable Memory Capability, SQLite record storage, a Milvus vector index, an outbox relay, automatic run-end extraction, automatic context injection, memory tools, and basic list/delete Web UI. (Planning-time snapshot: the content authority has since moved to Markdown files — see §6.6 and §12. The Milvus vector index and the SQLite record store are post-pivot Knowledge/RAG and migration concerns, not memory content authority.)

The current automatic path does not meet a production long-term-memory bar:

- a completed run's final answer or a tool/error summary can become a memory candidate even when it has no durable value;
- extraction failure can fall back to heuristic content generation, turning uncertainty into stored noise;
- the current USER/SESSION split allows session-scoped records to be treated as long-term memory even though SessionEvent history already owns short-term conversation state;
- run-end writeback is process-owned work and is not proven recoverable after a crash;
- formation, quality adjudication, consolidation, retry, fallback, provenance, sensitivity, lifecycle, and user governance are not expressed by one strict contract;
- retrieval is primarily dense-vector based and does not provide the approved profile/collection budgets, cross-session recall quality, or a user-visible explanation of why a record was recalled;
- the current management API/UI is list/delete only;
- existing long-term-memory data is acknowledged to be polluted and must not be migrated into the new store.

Markdown files remain the authoritative memory-content store (`MEMORY.md` for collection content, `USER.md` for the profile), written only through the single `MemoryCommit` seam (target contract; built by #1494). FTS, the in-process index, and the outbox are derived and rebuildable from those files. Memory owns no vector index: Milvus stays a Knowledge/RAG vector store and is not a long-term-memory owner (ADR-0056 §2; #1496 AC1). SessionEvent history, checkpoints, artifacts, and Knowledge are separate domains and must not be reclassified as long-term memory.

### 1.2 Problem

The system stores too much low-value material and cannot prove that an automatically stored memory is durable, attributable, safe, cross-session reusable, correctly superseded, or recoverable. This reduces retrieval precision and risks placing untrusted or sensitive material into future model context.

### 1.3 Target state

The product automatically forms only durable, reusable Semantic, Episodic, or Procedural memories through a two-stage model contract. It may abstain without writing. Accepted memories are versioned, attributable to source events, placed in the profile or collection tier of the current Markdown files, searchable across conversations, and governed by the user.

Formation is background work backed by durable project state. A primary memory model and a fallback memory model operate under explicit retry, deadline, call, and token budgets. Failure produces an observable degraded result and no memory write; it never becomes heuristic content.

The final cutover makes the new Markdown-backed path the only runtime source. Old long-term-memory records and indexes are **not** deleted under this PRD's authority: they are retained as an encrypted, read-only archive to a defined retention date (ruling 6); destruction requires a separate ruling. Conversations, SessionEvents, checkpoints, artifacts, Knowledge data, evaluation datasets, and credentials remain untouched.

## 2. Goals

1. Store only information with durable cross-conversation value.
2. Support three memory kinds: Semantic, Episodic, and Procedural.
3. Support cross-conversation recall over the single local memory content (profile tier plus collection tier).
4. Keep short-term SessionEvent/checkpoint/runtime context separate from long-term memory.
5. Make formation and adjudication strict, typed, retryable, recoverable, and capable of abstention.
6. Preserve provenance, version history, supersession, invalidation, and deletion evidence without retaining deleted content.
7. Prevent credentials, secrets, unauthorized sensitive data, and data outside this deployment's own memory space from entering or leaving the memory boundary.
8. Provide automatic recall, an explicit Memory Search Tool, explicit remember/forget commands, full API governance, and a usable Web UI.
9. Measure write precision, abstention quality, classification quality, contradiction handling, cross-session recall, fallback, idempotency, latency, token use, and cost.
10. Retire the polluted existing long-term-memory data as a runtime source (read-only archive; destruction requires a separate ruling) after the new path is ready.

## 3. Non-Goals / Out of Scope

1. Introducing Graphiti or a graph database in this delivery.
2. Sharing memories across different users or teams.
3. Treating full conversation transcripts, SessionEvents, checkpoints, tool logs, or artifacts as long-term memories.
4. Deleting the existing long-term-memory data under this delivery's authority. Ruling 6 approved only an encrypted, read-only backup held to a defined retention date (2027-10-23); destruction needs a separate ruling.
5. Using an online LLM reranker during recall.
6. Building a durable pending-candidate queue that keeps retrying after both memory models exhaust their budgets. This is a later evolution; this delivery records a degraded terminal job result and writes nothing.
7. Allowing a memory provider, LangMem, Milvus, or Langfuse to own the Agent Runtime, identity, permission, retry, or persistence contract.
8. Guaranteeing a target score on public LoCoMo or LongMemEval during the first release. The first run establishes a non-blocking baseline; project-specific quality and security gates are blocking.

## Solution

Introduce a V2 long-term-memory lifecycle behind the existing Memory Capability/provider boundary. Eligible completed runs create a durable background job. Formation first decides whether durable information exists and emits bounded typed candidates. Adjudication then compares each candidate with authorized active memory and selects ADD, UPDATE, INVALIDATE, or NOOP. Runtime code validates every result, owns identity/policy/budgets, and commits accepted changes through the single `MemoryCommit` seam into the Markdown files; FTS, the in-process index, and the outbox are updated as derived state.

Recall combines a small trusted profile from `USER.md` with hybrid retrieval over `MEMORY.md` content. Users retain explicit search/remember/forget tools and gain full API/Web governance. Observability is metadata-only. After the V2 path is integrated and verified, a cutover makes it the only runtime source and the old store becomes a read-only archive.

## User Stories

1. As a user, I want stable preferences I state to be available in later conversations without repeating them.
2. As a user, I want reusable project facts and successful operating procedures to be recalled across my conversations, without the memory system partitioning or filtering them by project identity.
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
2. Keep Markdown files authoritative for memory content and every index rebuildable. No retrieval hit is trusted until the current approved file content (and its revision) confirms it.
3. Treat Formation and Adjudication as distinct model stages with strict runtime-validated contracts and explicit abstention/no-op outcomes.
4. Use an expand-then-contract delivery: establish V2 while legacy behavior still runs, integrate all V2 consumers, then cut over to the Markdown-only runtime path. No destructive deletion is performed under this delivery (see §3 item 4).
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

- `profile`: a compact trusted user profile (`USER.md`) that may be automatically injected on every eligible run. Only active `semantic` memories may enter this tier.
- `collection`: the searchable long-term archive (`MEMORY.md`) for all three kinds.

### 4.3 Scope

Memory content is a **single-user, local file asset**. It is not routed by tenant, user, or project (superseded by ruling 7 and ADR-0056 §3.1/§3.7):

- `profile` and `collection` are *tiers*, backed by two files, not isolation boundaries between identities.
- The former `user_global` and `project` scopes, and the rules that read/mutate them per tenant, user, and project/workspace, are no longer the current contract. Project directory and conversation grouping remain product organization — they do not filter, partition, or authorize memory.
- The former tenant/user/project values on existing records are retained only as **read-only migration source labels** (ADR-0056 §3.7) and never enter the runtime path. They must not be renamed to a replacement routing key and re-introduced.
- A semantic `project_fact` keeps its kind and its "belongs to one project's work" meaning in its payload; it is validated as a payload category, not by routing.
- `source_session_id` is provenance only. It must never restrict recall and must not recreate SESSION-scoped long-term memory.
- Isolation is enforced by the process/file boundary (single-user local deployment) plus the write-scan, read-scan, pin, and revision checks — not by identity routing. Today the identity-free path is the target contract being built by #1494/#1496; until it lands, this bullet states the contract the revision must reach, not a description of shipped behavior.

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

1. **Given** an eligible run, **when** formation executes, **then** the model receives only the approved safe projection: the current run, at most eight prior user/assistant messages, at most ten similar active memories, tool names/status/structured summaries, and artifact references. Session and event identifiers and any migration labels are runtime-owned and are not sent to the model. No identity, project, or routing field is part of the projection (ruling 7).
2. **Given** raw large tool output, artifact content, credentials, hidden reasoning, or unrestricted full history exists, **when** the formation prompt is built, **then** that material is absent.
3. **Given** formation returns candidates, **when** runtime validation executes, **then** no more than five candidates are accepted: Semantic at most three, Episodic at most two, Procedural at most one, total at most five.
4. **Given** a candidate exceeds a per-kind or total cap, **when** candidates are selected, **then** candidates are ranked by durable value and the lower-ranked excess candidates are discarded before adjudication.
5. **Given** an accepted candidate, **when** adjudication compares it with bounded relevant active memory, **then** it produces exactly one of `ADD`, `UPDATE`, `INVALIDATE`, or `NOOP`. For every non-null result, it preserves that candidate's `kind` and `tier`; Runtime rejects any mismatch and writes nothing.
6. **Given** a single observed event, **when** a Procedural candidate is proposed, **then** it is rejected unless the user explicitly opts in with `remember_as_procedural_rule: true` on that non-blank task/message. Runtime stores the signal only on the corresponding genuine `user/message`, and the candidate must cite that same event with a non-empty excerpt found verbatim in its content. A normal user message does not imply this signal. Without it, Procedural memory requires at least two distinct qualifying events: successful tool results (`ok=true`) or genuine user corrections whose `refutes_event_id` resolves to a persisted direct user input or valid tool attempt in the same session. Failed tool results do not count by themselves.

### 5.3 Model retry and fallback

1. **Given** a transient primary-model failure, **when** the memory job executes, **then** `memory.primary` receives one initial attempt plus at most two retries.
2. **Given** primary attempts are exhausted, **when** budget remains, **then** `memory.fallback` receives one initial attempt plus at most one retry.
3. `memory.primary` and `memory.fallback` map to locally configured provider aliases whose targets are operator-configurable (default mapping preserved: `memory.primary` → `senseaudio`, `memory.fallback` → `qwen`). An operator MAY point either role at any provider available through the existing model-provider configuration surface (a built-in preset or a user-registered OpenAI-compatible provider). The role mapping is application-level (deployer-configured, effective after restart). Credentials and endpoint values remain local ignored configuration and never enter repository documents, issues, logs, or traces.
4. Non-transient validation, authentication, permission, and policy failures are not retried. When such a failure is a deterministic provider-class failure of `memory.primary` (for example an HTTP 400 that is not a transient/network error) and the local memory fallback switch is enabled (default: enabled; one value for this deployment, not per identity; panel-configurable and effective at runtime), the job MAY switch to `memory.fallback` once (the fallback then follows its ordinary retry budget), provided the fallback role resolves to a different provider, endpoint, or model. Otherwise the job terminates degraded without switching to `memory.fallback` — this covers the switch being disabled, no fallback role being configured, and the fallback resolving to the same target. Schema/parse failures and post-parse semantic guard failures remain immediately terminal; when the switch is disabled, behavior is byte-for-byte the current policy.
5. One job has a 120-second wall-clock deadline, at most five model calls, at most 32,000 cumulative input tokens, and at most 4,000 output tokens per call.
6. Jobs are serialized within the single local memory space. Global formation concurrency defaults to four and is configurable.

### 5.4 Versioning, contradiction, and deletion

1. **Given** a new fact supersedes an active memory, **when** `UPDATE` is applied, **then** a new version becomes active and the previous version becomes `superseded`; the previous content remains available in version history.
2. **Given** a fact is no longer true without a replacement, **when** `INVALIDATE` is applied, **then** the active record becomes `invalidated` and is excluded from recall.
3. **Given** the user deletes a memory, **when** deletion commits, **then** content and evidence excerpts are removed immediately from the Markdown files and from every derived index (FTS/in-process index/outbox). A content-free tombstone retains only IDs, source labels, timestamps, deletion reason, and hashes for 30 days, after which it is purged. The tombstone has its own non-SQLite owner (ADR-0056 §3.5) and continues to reject stale-input replay.
4. **Given** an outbox relay or background worker restarts after deletion, **when** it replays outstanding operations, **then** deleted or pre-cutover content cannot be recreated.

### 5.5 Recall

1. **Given** an authenticated run with memory enabled, **when** context is assembled, **then** it may combine the `USER.md` profile tier with active memories from the `MEMORY.md` collection tier.
2. **Given** a new conversation B, **when** a relevant memory was formed in conversation A, **then** B can recall it without using A's SessionEvent history as context.
3. **Given** candidate memories are retrieved, **when** ranking occurs, **then** dense similarity and keyword matching are combined, followed by deterministic filtering/reranking using status, type, tier, importance, strength, and retrieval decay. No online LLM reranker is called.
4. The always-visible Profile budget is at most 500 model-input tokens. Retrieved Collection memory is at most 800 model-input tokens, at most six complete records, and at most three records of any one kind.
5. Retrieved memory is injected as non-privileged data, not as instructions. The Memory Search Tool remains available for on-demand retrieval beyond automatic budgets.
6. Every automatically recalled memory exposes a redacted explanation containing its memory ID, kind, tier, source type, source/revision reference, and deterministic ranking contributions. It does not expose another user's data, hidden prompt content, raw evidence, or secrets.

### 5.6 Explicit commands and controls

1. “Remember X” bypasses the durable-value threshold but not schema validation, tier rules, secret blocking, or sensitive-data consent rules.
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

Every V2 memory record has these fields. All are required unless the row marks one as conditional:

| Field | Contract |
| --- | --- |
| `id` | Stable opaque identifier |
| `schema_version` | Literal `2` |
| `root_id` / `version` | Stable logical-memory identifier and monotonically increasing positive version |
| `kind` | `semantic`, `episodic`, or `procedural` |
| `tier` | `profile` (in `USER.md`) or `collection` (in `MEMORY.md`) |
| `content` | Self-contained canonical text, 1–500 Unicode characters |
| `payload` | Discriminated per-kind payload from §4.1 |
| `status` | `active`, `superseded`, `invalidated`, or `deleted` |
| `importance`, `strength` | Numeric values in inclusive range 0.0–1.0 |
| `source_type` | `automatic`, `explicit_command`, or `user_edit` |
| `source_session_id` | Origin-only session identifier |
| `source_event_ids` | Non-empty ordered set for automatic memories; may be empty only for direct UI edits without a session |
| `evidence` | One or more `{role, excerpt, hash}` items; excerpt max 300 characters; deleted tombstones retain hash only |
| `migration_labels` | **Conditional** (migrated records only): read-only provenance labels inherited from the pre-pivot store (`tenant`/`user`/`project` values). Not a routing key, not written by runtime, not consulted for recall, and absent on newly formed records (ADR-0056 §3.7) |
| `revision` | **Conditional** (published records only): the committed file revision this record was published at, written by the `MemoryCommit` seam, used by its receipt to detect conflicts. Not a formation input |
| `valid_at`, `invalidated_at`, `superseded_by` | Temporal/version fields; nullable when not applicable |
| `created_at`, `updated_at` | Server-owned timestamps |

Identity fields (`tenant_id`, `user_id`, `project_id`) are no longer part of the runtime envelope. Records migrated from the old store carry those values only inside `migration_labels`. Settings are single-user local settings (extraction / recall / fallback / write_approval), not per-identity rows (ADR-0056 §3.4).

Unrecognized enum values, missing required fields, over-limit content, mismatched payloads, untrusted identity fields, or invalid tier combinations fail closed and do not write.

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

Each candidate contains kind, tier, canonical content, matching typed payload, importance, strength, evidence references, `sensitivity` (`ordinary`, `sensitive`, or `secret`), and an applicable sensitive-category enum from §5.7. The model supplies no identity, project, or routing field; Runtime determines the target file from `tier` and rejects any candidate it cannot place in a tier. Runtime independently enforces secret/sensitive policy.

### 6.3 Adjudication result

The model must return one JSON object per candidate:

```text
action: ADD | UPDATE | INVALIDATE | NOOP
target_memory_id: string | null
result: complete MemoryRecordV2 content proposal | null
reason_code: durable_new | enrich_existing | contradicts_existing |
             user_authority_wins | duplicate | insufficient_evidence |
             procedural_threshold_not_met | policy_rejected
```

`ADD` requires no target and a complete result. `UPDATE` requires an active target and a complete result. `INVALIDATE` requires an active target and no replacement result. `NOOP` writes nothing. A non-null result must preserve the corresponding Formation candidate's `kind` and `tier`; Adjudication chooses the action and may refine content, but cannot reclassify the candidate. Any mismatch is a contract failure and writes nothing. Model results supply no identity, project, or routing field; Runtime validates target existence, expected file revision, source authority, and tier before applying the action, and the single `MemoryCommit` seam performs the publish with a receipt.

### 6.4 API

Existing authenticated `/api/memories` list/delete behavior remains compatible while responses gain V2 fields.

- `GET /api/memories`: supports `q`, `kind`, `status`, `tier`, `limit`, and `offset`; returns the records readable from the current Markdown content. Version history is served from the record's own current/previous-version data; where it is persisted is decided by #1494 and is not asserted here.
- `GET /api/memories/{id}`: returns the visible current record and provenance summary.
- `GET /api/memories/{id}/versions`: returns visible versions in descending version order, excluding deleted content.
- `PATCH /api/memories/{id}`: accepts editable content plus the matching typed payload and creates a new `user_edit` version.
- `DELETE /api/memories/{id}`: performs the approved content-erasing deletion and returns an idempotent deletion receipt.
- `POST /api/memories/bulk-delete`: body is `{kind: <kind-or-null>, confirmation: "DELETE"}`; null kind selects all visible memories and any other confirmation value is rejected; returns affected count.
- `GET /api/memory-settings`: returns the local `extraction_enabled`, `recall_enabled`, `fallback_enabled`, and `write_approval` settings.
- `PATCH /api/memory-settings`: updates one or more of those settings without deleting records.
- `GET /api/sessions/{session_id}/memory-recalls`: returns redacted “why recalled” records for that session.

`POST /api/sessions`, task-bearing `POST /api/sessions/{session_id}/resume`, and
`POST /api/sessions/{session_id}/messages` accept the optional boolean
`remember_as_procedural_rule`. It defaults to false; true requires a non-blank task/message and is
stored on that user event for the R5 single-event rule exception. Empty-session creation and
same-run resume cannot set it.

All mutation endpoints take their subject from trusted server-side state (the deployment's single-user memory space), never from request-body identity. Endpoints also never accept a routing key from the body: a request cannot select a tenant, user, or project to read or mutate.

### 6.5 Events and UI behavior

- `memory/updated`: emitted only after a committed logical change; contains count, memory IDs, action counts, and job ID, but no memory content.
- `memory/degraded`: preserves the existing event family and adds stable `stage`, `reason_code`, `job_id`, attempt count, and fallback-used fields; it contains no prompts, content, evidence excerpt, credential, or provider response body.
- `memory/recalled`: records IDs and redacted ranking explanation for a run; it contains no raw evidence or content from invisible records.
- A replayed job may re-emit no logical update when its idempotency key was already committed.
- Web UI uses the API as the only memory truth. It does not maintain a second durable memory state.

### 6.6 Persistence ownership

Each fact class has one named owner; there is no single unified store and no default SQLite exception (ADR-0056 §3):

- **Content and revision**: the Markdown files `MEMORY.md` / `USER.md`, published only through the `MemoryCommit` seam (#1494). It is the only mutable content fact.
- **Job / lease / idempotency**: the Formation state owner; its non-SQLite persistence and restart-recovery entry are closed by #1494. Same idempotency key ⇒ at most one logical update; a lost lease commits nothing.
- **Approval / pin**: the Approval state owner (#1494). Pending stays pending; approved is written only after the target Markdown digest verifies; a missing pin or revision mismatch is rejected and re-queued for human review.
- **Settings**: local settings for the four switches (extraction / recall / fallback / write_approval) — one value per switch, not per identity. Old `PRIMARY KEY (tenant_id, user_id)` rows are merged by row, not by inventing a replacement routing key (#1493).
- **Tombstone**: the deletion-suppression owner keeps deletion reason, content/source hashes, and expiry; it continues to reject stale-input replay.
- **Receipt / reconcile**: the commit-recovery owner keeps operation id, per-target previous/new digests, and the PREPARED / PUBLISHED / COMMITTED / NEED_RECONCILE state machine (names finalized in #1494). Uncertainty ⇒ NEED_RECONCILE.
- **History / provenance**: a read-only migration archive (encrypted backup, retained until 2027-10-23) plus a mapping table; both are off the runtime path.
- **Derived index / outbox**: FTS, in-process index, and outbox are rebuildable from the current Markdown content only; they own no current fact, and their survival is decided with the retirement step (ruling 16; ADR-0056 §3.8). No vector index belongs to Memory: Milvus is a Knowledge/RAG store and is out of this owner table.
- LangMem is an optional adapter used for typed formation/consolidation; it does not own persistence, identity, retry, fallback, policy, or lifecycle.
- Langfuse is observational only and cannot be required for Core success.

## 7. Constraints

### 7.1 FIXED

1. All 36 approved product decisions represented in this PRD.
2. Memory remains a Capability + Context Provider behind the existing provider seam.
3. Markdown-file content authority through the single `MemoryCommit` seam, named owners for each operational-state class, and no tenant/user/project memory routing (ADR-0056 §3; ruling 7).
4. Two-stage Formation then Adjudication with strict runtime validation.
5. No heuristic memory fallback and no write after exhausted extraction/adjudication failure.
6. Automatic background writes, cross-conversation recall, and separate short-term/long-term domains.
7. The retry, fallback, token, deadline, concurrency, candidate, content, evidence, and injection budgets in this PRD.
8. Secret prohibition, sensitive-data consent, non-privileged recall, and user-edit authority.
9. Versioning, supersession/invalidation, deletion tombstones, and complete user governance.
10. Old long-term-memory data is retained as an encrypted, read-only archive to a defined retention date (2027-10-23). Destructive deletion is **not** authorized by this PRD: it requires a separate ruling (ruling 6). This supersedes the former clean-slate deletion constraint, whose authorization is not inherited.
11. Real final Gate only after all implementation tickets are integrated.

### 7.2 BOUNDED

1. Existing modules may be refactored and new internal modules may be introduced, but AgentRuntime must not acquire provider-specific branches.
2. Existing APIs may gain backward-compatible fields and filters; breaking changes require an approved replacement contract and migration.
3. The durable job must have a named non-SQLite persistence owner and a proven restart-recovery entry (ADR-0056 §3.2, closed by #1494). A separate queue service or in-memory-only owner is not acceptable. It must meet recovery, idempotency, ownership, and observability acceptance criteria. Reusing the existing outbox/persistence substrate is not a licence to keep operational state in SQLite beyond an owner-approved transition window (ruling 1).
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

1. **Memory Capability/provider seam:** typed lifecycle, versioning, tier semantics, Markdown content authority via the commit seam, derived-index rebuildability, and provider replacement.
2. **Real AgentRuntime + SessionEvent recovery seam:** trigger eligibility, durable background job, retry/fallback, crash/restart, idempotency, and event contracts.
3. **FastAPI + Web UI seam:** governance API contracts plus accessible user workflows at both supported viewport sizes.
4. **Real integration/evaluation seam:** real configured memory models, metadata-only Langfuse, quality corpus, public baseline, and cleanup proof. Memory owns no vector collection; Milvus is exercised only as a Knowledge/RAG store.

### 8.2 Blocking project gates

| Gate | Pass condition |
| --- | --- |
| Secret write | 0 secret records across direct, automatic, fallback, replay, API edit, and explicit remember cases |
| Isolation | 0 cross-process/foreign-data recalls or mutations; no request-body routing key can select content |
| Ineligible trigger | 0 writes for cancellation, startup failure, no model call, no genuine user input, and explicit turn opt-out |
| NOOP accuracy | At least 95% on the frozen project gold set |
| Write precision | At least 95% accepted memories judged durable and correctly attributable on the frozen project gold set |
| Kind accuracy | At least 90% correct Semantic/Episodic/Procedural classification |
| Contradiction handling | At least 95% correct old-version supersession/invalidation |
| Cross-session Recall@6 | At least 85% on the frozen project cross-session set |
| Fallback | 100% of injected transient primary failures use the approved fallback path or terminate degraded without a write |
| Idempotency | Replaying a committed job creates 0 duplicate active logical memories |
| Deletion | Deleted content absent from the Markdown files and from every derived index immediately; only allowed tombstone fields remain; stale replay still rejected |
| Cutover | No runtime path reads the pre-pivot store: retrieval and injection serve Markdown content only, and no old job/outbox replay can repopulate memory. Destructive deletion of old data is **not** covered by this PRD's original clean-slate authorization (ruling 6 approved only an encrypted backup to a defined retention period; destruction needs a separate ruling). |

### 8.3 Non-blocking initial public baseline

Run LoCoMo- and LongMemEval-compatible evaluation and record answer quality, stored-record count, pollution rate, injected tokens, latency, and cost. The first accepted run freezes a baseline; later releases must not introduce a statistically or operationally significant regression without an approved explanation.

### 8.4 Real Gate hygiene

- Memory creates and owns no vector collection. Knowledge/RAG Milvus collections are cleaned back to their baseline after verification.
- Preserve the Langfuse test dataset, experiment, and trace as release evidence; verify no unexpected duplicate records.
- Langfuse records metadata and hashes only. Synthetic evaluation inputs may be retained; real user content may not.
- Credentials are checked only for presence. Values are never printed.
- The cutover touches only Memory data. Knowledge collections and data must be shown unchanged.

## 9. Acceptance Criteria

1. Every Given/When/Then behavior in §5 has an automated test at one of the approved seams.
2. All schemas and APIs in §6 are validated with positive, boundary, malformed, and authorization tests.
3. The blocking gates in §8.2 pass on a frozen, versioned project gold set.
4. A real run in conversation A forms an approved memory into the Markdown content, and a distinct conversation B recalls it with no reliance on A's SessionEvent history.
5. Injected primary-model transient failure proves the `senseaudio` alias attempt budget and the `qwen` fallback path without exposing configuration values.
6. Kill/restart tests prove that an eligible background job is not lost, a committed job is not duplicated, and an exhausted job cannot write later.
7. API and Web UI tests prove list/search/filter/source/version/edit/delete/bulk-delete/settings/why-recalled workflows, including authorization and both supported viewports.
8. Secret and prompt-injection probes prove stored memory cannot grant permission or enter privileged instruction channels.
9. Cutover proves the runtime no longer reads the pre-pivot store: every write goes through the commit seam to Markdown, every recall is served from current Markdown content plus a rebuildable index, and the old store is preserved only as the approved encrypted read-only archive. SessionEvents, chats, checkpoints, artifacts, Knowledge, datasets, and credentials remain intact. Old SQLite/Profile/job/outbox data must not be deleted under the original clean-slate authorization.
10. The real Gate leaves Knowledge/RAG and Qiniu temporary test data at zero while preserving the approved Langfuse evidence.
11. Relevant ADRs are added or superseded so the memory content/operational-state owner boundary, tombstone semantics, provider ownership, retrieval behavior, and the retirement of identity routing do not conflict with ADR-0008, ADR-0009, ADR-0024, ADR-0026, ADR-0031, or ADR-0056.
12. Ruff, backend full pytest, review coverage, frontend typecheck, Vitest, Oxlint, production build, and full two-viewport Playwright gates pass on the integrated tree.

## 10. Ticket Map

1. #297 / MEM-V2-1 — Typed Memory lifecycle vertical slice.
2. #298 / MEM-V2-2 — Durable Formation and Adjudication.
3. #299 / MEM-V2-3 — Cross-session Profile and hybrid recall.
4. #300 / MEM-V2-4 — Explicit commands and governance API.
5. #301 / MEM-V2-5 — Memory management Web UI.
6. #302 / MEM-V2-6 — Privacy observability and quality evaluation.
7. #303 / MEM-V2-7 — Clean-slate cutover and legacy-path retirement. *(Historical label only: the clean-slate deletion authorization this ticket carried is superseded — see §7.1 item 10 and ruling 6.)*
8. #304 / MEM-V2-8 — Final real Gate and release evidence.

Dependency order is defined in the ticket documents and their GitHub issue bodies.

## 11. Reuse and Reference Decisions

- **LangMem — REUSE + ADAPT (optional adapter):** typed schemas and bounded consolidation operations. No longer the default provider; the default is the in-repo Markdown store (ADR-0056 §7).
- **Hermes — PORT DESIGN:** aggressive save/skip curation, bounded MEMORY/USER file concepts, freeze-on-session-start injection, and explicit write-approval precedent. The in-repo Markdown implementation is **ADAPT** (file content authority, expected revision, pre-write and on-read scanning, atomic single-file replace); do not copy upstream code.
- **Graphiti — PORT DESIGN only:** provenance and temporal invalidation fields; defer graph backend.
- **Letta — PORT DESIGN:** compact profile versus searchable archive separation.
- **Mem0 — PORT DESIGN:** role-aware evidence discipline, lifecycle operations, hybrid retrieval ideas, and public benchmark harnesses; do not adopt high-recall ADD-all behavior.

License and pinned-source references are recorded in the research document. Substantial copied code must retain required notices.

## 12. Further Notes

- This PRD intentionally supersedes the old assumption that SESSION-scoped records are a long-term-memory tier. Session-scoped conversation state remains available through SessionEvent history and runtime context.
- This PRD changes memory deletion from immediate no-tombstone hard delete to immediate content erasure plus a 30-day content-free tombstone. The implementation ticket must record the ADR supersession explicitly.
- The future durable pending-candidate queue is not hidden in this release. Exhausted jobs end degraded/no-write and require a later approved ticket to be reconsidered automatically.
- **Post-planning pivot (2026-10-19 决定，ADR-0056 定案)**: memory content moved to Markdown files and memory stopped being routed by tenant/user/project. Sections revised in #1543 carry the current contract; where an older sentence here still reads as "SQLite is the record authority" or as an identity scope, ADR-0056 §3 wins. The original clean-slate *destruction* authorization is not inherited: old data stays as an encrypted read-only archive to its retention date unless a separate ruling authorizes deletion.
- The receipt and recovery state machine replaces "the write returned OK" as the success criterion: success means a committed receipt exists; failure and recovery evidence must remain queryable and must never be deleted to make a gate pass.
- No implementation ticket may silently relax a threshold or contract because a model/provider cannot meet it. New evidence that invalidates a ticket must follow the project's Analyze → Report → Ask workflow.
