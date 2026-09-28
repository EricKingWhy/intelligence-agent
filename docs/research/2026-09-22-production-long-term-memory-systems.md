# Production Long-Term Memory Systems: lifecycle benchmark and reuse plan

**Date:** 2026-09-22

**Scope:** LangMem, NousResearch Hermes Agent, Mem0, Letta/MemGPT, Zep Graphiti, and publicly documented ChatGPT Memory behavior.

**Method:** primary sources only: upstream source code, official documentation, official repositories, and first-party benchmark harnesses. Repository heads were sampled on 2026-09-22.

**Non-goal:** this is research evidence, not the product PRD or an implementation ticket.

## 1. Executive conclusion

No reviewed product offers a single drop-in implementation that satisfies this project's complete production contract. The strongest design is a deliberate composition:

1. **Keep project-owned storage sovereignty:** SQLite remains the authoritative memory record and operation/outbox state; Milvus remains a rebuildable vector index. None of the evidence justifies reversing ADR-0008.
2. **Reuse LangMem for schema-driven formation and consolidation**, particularly custom Pydantic schemas and insert/update/delete/no-op reconciliation against existing memories. Do not ask LangMem to own identity, persistence, retries, provider fallback, sensitivity policy, or quality gates.
3. **Port the Hermes curation policy, not its Markdown store:** use a sharp save/skip rubric, separate universally useful facts from procedures and raw history, make writes visible/reviewable, impose budgets, and scan stored text because recalled text re-enters a privileged prompt channel.
4. **Adopt Graphiti's provenance and temporal model selectively:** every durable claim should be traceable to source events and able to represent observation/effective/expiry or supersession time. A graph database is not required for V1 of this improvement.
5. **Borrow Letta's tiering:** a small curated profile/working set and a larger searchable archive have different selection and injection rules. Our existing automatic ContextProvider plus on-demand `retrieve_memory` already provides a seam for this distinction.
6. **Treat model execution as project infrastructure:** temporary extraction errors retry up to a configured limit; exhaustion switches to the configured fallback model; if both fail, the result is **zero candidates**, never a regex/heuristic memory and never an unconditional write. The upstream systems do not provide this exact contract.
7. **Quality is a gated product surface:** evaluate extraction precision, abstention, contradiction handling, temporal correctness, sensitivity refusal, retrieval, context usefulness, deletion, isolation, and crash consistency independently. End-to-end answer accuracy alone can hide a polluted memory store.

The current project violates item 6 in two places: `MemoryExtractor` falls back to heuristic extraction (including every `run/completed.final_text`), and consolidation failure falls back to unconditional insertion. Both maximize write recall at the cost of precision and directly conflict with a production-quality, value-filtered memory policy.

## 2. Source snapshots and licensing

| System | Evidence snapshot | License | Reuse implication |
| --- | --- | --- | --- |
| LangMem | [`9d033b4`](https://github.com/langchain-ai/langmem/tree/9d033b47d9ce53e37e92c92241b0496c0278932e) | [MIT](https://github.com/langchain-ai/langmem/blob/9d033b47d9ce53e37e92c92241b0496c0278932e/LICENSE) | Direct reuse and adaptation are permitted with copyright/license notice retention. |
| Hermes Agent | [`913c045`](https://github.com/NousResearch/hermes-agent/tree/913c045c53800a24956a74cc1fe6db0dd4421490) | [MIT](https://github.com/NousResearch/hermes-agent/blob/913c045c53800a24956a74cc1fe6db0dd4421490/LICENSE) | Prompt/rubric and bounded-curation designs can be ported with attribution; its file storage is not a fit for our current architecture. |
| Mem0 OSS | [`a39a802`](https://github.com/mem0ai/mem0/tree/a39a802bbc93e85b820078cd3c4dbaf53af25dbe) | [Apache-2.0](https://github.com/mem0ai/mem0/blob/a39a802bbc93e85b820078cd3c4dbaf53af25dbe/LICENSE) | Source reuse is possible with Apache notices and changed-file obligations; architecture is more useful than wholesale adoption. |
| Letta | [`5bcdd17`](https://github.com/letta-ai/letta/tree/5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a) | [Apache-2.0](https://github.com/letta-ai/letta/blob/5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a/LICENSE) | Reuse tiering and tool-contract ideas; do not import its full agent runtime. |
| Graphiti | [`16cdf70`](https://github.com/getzep/graphiti/tree/16cdf7045378c8d53ae01f94e2fa60d238cb0f68) | [Apache-2.0](https://github.com/getzep/graphiti/blob/16cdf7045378c8d53ae01f94e2fa60d238cb0f68/LICENSE) | Provenance/temporal schemas and search recipes are portable; adding a graph backend should remain optional and separately justified. |
| ChatGPT Memory | closed product; public behavior only | proprietary | Use as a UX/privacy benchmark, not an implementation source. |

License conclusions are engineering guidance, not legal advice. If source or prompt text is copied substantially, preserve the upstream license/copyright notice and record the source path and revision in the implementation.

## 3. Lifecycle comparison

### 3.1 Extraction eligibility and filtering

#### LangMem

LangMem's core abstraction takes conversation plus existing memory state, asks an LLM to expand or consolidate that state, and returns the updated state. Its default instruction prioritizes information that is surprising or persistent and asks for dense, complete, non-overlapping memories; its documentation warns that over-extraction reduces retrieval precision and recommends application-specific schemas/instructions. It supports semantic collections, profiles, episodic examples, and procedural prompt optimization rather than treating all memories as one undifferentiated list.

Primary evidence:

- [Conceptual guide: types, profiles vs collections, over/under-extraction](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)
- [`_MEMORY_INSTRUCTIONS` and `Memory` schema](https://github.com/langchain-ai/langmem/blob/9d033b47d9ce53e37e92c92241b0496c0278932e/src/langmem/knowledge/extraction.py)
- [Custom structured semantic-memory schemas](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/)

**What is reusable:** `create_memory_manager`/`create_memory_store_manager`, custom Pydantic schemas, and an instruction contract that permits no emitted tool calls when nothing qualifies.

**What is missing:** a production sensitivity policy, provenance fields, an externally enforced minimum-quality decision, provider retry/fallback, and quality metrics. The default unstructured `Memory(content: str)` is deliberately generic; using it without a stricter project schema delegates too much policy to prose.

#### Hermes Agent

Hermes uses intentionally small, curated, always-injected stores: `MEMORY.md` for durable environment/convention/learned facts and `USER.md` for the user profile. Its current guidance says memory is the narrow exception for facts applicable to **every** session, routes task-specific procedures and pitfalls to skills, rejects imperative phrasing, and uses a practical rule: facts stale within a week belong in session history. Official docs enumerate positive and negative examples, including skipping raw dumps, one-off debugging context, easily rediscovered facts, and facts already held in project context files.

Primary evidence:

- [Persistent Memory documentation: save/skip, fixed budgets, duplicate rejection, security scanning](https://github.com/NousResearch/hermes-agent/blob/913c045c53800a24956a74cc1fe6db0dd4421490/website/docs/user-guide/features/memory.md)
- [`build_memory_guidance`: universal scope, fact-not-instruction, one-week durability rule](https://github.com/NousResearch/hermes-agent/blob/913c045c53800a24956a74cc1fe6db0dd4421490/agent/prompt_builder.py)

Hermes also runs a post-turn background review that can use the main model or a separately configured auxiliary model. It is a useful precedent for LLM-only curation, but it is not an extraction service with a typed candidate schema.

**What is reusable:** the save/skip rubric, memory-vs-skill-vs-history routing, declarative-fact rule, small-budget pressure, and explicit no-op expectation.

**What should not be copied:** the two flat Markdown files, substring-based replacement, or always-injecting the entire store. Those work because Hermes deliberately caps the store at roughly 1,300 tokens total; they do not replace semantic search for a larger multi-tenant product.

#### Mem0

Mem0's public prompts make the first phase explicit: an LLM extracts facts/preferences and may return an empty list. Its user-specific prompt excludes assistant/system messages; the current V3 additive prompt, however, is deliberately high recall and treats both user and assistant statements as first-class memories. The current official README says the managed algorithm moved to single-pass, ADD-only extraction, entity linking, and multi-signal retrieval; it also clearly warns that managed-platform benchmark scores include proprietary optimizations not present in the OSS SDK.

Primary evidence:

- [Extraction and legacy ADD/UPDATE/DELETE/NONE prompts](https://github.com/mem0ai/mem0/blob/a39a802bbc93e85b820078cd3c4dbaf53af25dbe/mem0/configs/prompts.py)
- [Current V3 algorithm and benchmark qualification](https://github.com/mem0ai/mem0/blob/a39a802bbc93e85b820078cd3c4dbaf53af25dbe/README.md)
- [`Memory.add` and current storage pipeline](https://github.com/mem0ai/mem0/blob/a39a802bbc93e85b820078cd3c4dbaf53af25dbe/mem0/memory/main.py)

**What is reusable:** role-constrained extraction, explicit empty output, observation-date normalization, dedup context, and separation of newly observed messages from existing memories.

**Caution:** its current additive prompt says to capture every memorable piece and includes assistant recommendations. That maximizes benchmark recall but is not automatically compatible with our requested high-precision long-term store. We should reuse its evidence-bound and temporal-normalization techniques, not copy its "extract all" objective.

#### Letta / MemGPT

Letta does not depend on an automatic extractor for all memory. The agent maintains bounded, always-visible core memory blocks and explicitly inserts self-contained passages into searchable archival memory through tools. Official tool documentation tells the model to save self-contained facts/summaries, attach tags, and use semantic search later. Recall history and archival memory remain separate.

Primary evidence:

- [Core and archival tool implementations/documentation](https://github.com/letta-ai/letta/blob/5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a/letta/functions/function_sets/base.py)
- [Typed memory, archival insert and search schemas](https://github.com/letta-ai/letta/blob/5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a/letta/schemas/memory.py)
- [First-party memory architecture guide](https://github.com/letta-ai/skills/blob/main/letta/letta-api-client/memory-architecture.md)

**What is reusable:** tiering by access frequency and purpose; structured blocks for a compact user profile; self-contained archival passages; tags/time filters; explicit tools rather than silent bulk copying.

**What is missing for us:** automatic per-turn candidate extraction, strong consolidation of archival duplicates, and the project-specific SQLite/Milvus consistency model.

#### Graphiti / Zep

Graphiti ingests immutable episodes, extracts entities and facts, records the source episode UUIDs for each fact, and models changing truth with `valid_at`, `invalid_at`, and `expired_at`. When a new fact contradicts an older one, the old edge is invalidated rather than erased; raw episode provenance remains available.

Primary evidence:

- [Project architecture: temporal facts and episode provenance](https://github.com/getzep/graphiti/blob/16cdf7045378c8d53ae01f94e2fa60d238cb0f68/README.md)
- [Fact extraction, episode attribution, timestamp extraction, duplicate and contradiction resolution](https://github.com/getzep/graphiti/blob/16cdf7045378c8d53ae01f94e2fa60d238cb0f68/graphiti_core/utils/maintenance/edge_operations.py)

**What is reusable:** source-event attribution and temporal validity/supersession. These can be fields in SQLite without adopting a graph database.

**What should be deferred:** graph entity resolution and graph storage. They add operational cost and do not solve the immediate precision/fallback defect.

#### ChatGPT Memory

OpenAI publicly documents two layers: explicit saved memories and information referenced from chat history. It states that memory does not retain every detail, may update what is useful as context changes, and offers review/edit/delete, on/off controls, and Temporary Chat. It also says the product is trained not to proactively remember sensitive information such as health details unless explicitly asked.

Primary evidence:

- [Memory controls and saved-memory/chat-history behavior](https://help.openai.com/en/articles/8590148-memory-faq)
- [Product announcement and sensitive-information policy](https://openai.com/index/memory-and-new-controls-for-chatgpt/)
- [Privacy controls and Temporary Chat](https://openai.com/consumer-privacy/)

The extraction, ranking, and storage algorithms are not public. Therefore ChatGPT can justify UX and privacy requirements, but not an implementation claim.

### 3.2 Candidate schema

| System | Public candidate/record shape | Strength | Gap for this project |
| --- | --- | --- | --- |
| LangMem | unstructured `content`, or any caller-provided Pydantic model; stable ID for updates/removal | Best schema extension seam | No mandatory provenance, sensitivity, confidence, or temporal fields |
| Hermes | text entry routed to `memory` or `user` | Very strong curation pressure | No typed provenance/temporality; substring update API |
| Mem0 | fact text; current V3 adds linked memory IDs and temporal anchors; legacy manager emits operation and old memory | Evidence/dedup context and date grounding | High-recall default is too permissive; current managed/OSS behavior differs |
| Letta | core block `{label, description, value, limit}`; archival `{content,tags,created_at}` | Clear purpose and tier | Does not itself decide eligibility per turn |
| Graphiti | episode; entity nodes; fact edges with episode UUIDs, valid/invalid/expired time and attributes | Best provenance and changing-truth model | Heavy graph shape for simple preferences |
| ChatGPT | opaque saved item/summary | User-visible management | No public technical schema |

A production candidate schema should be richer than the current `{scope, content, importance}` but should not make every model-estimated number authoritative. Recommended minimum:

```text
MemoryCandidate
  kind: preference | stable_fact | constraint | goal | decision | reusable_lesson
  scope: user | session                 # current implemented scopes
  content: standalone declarative claim
  evidence_event_ids: non-empty list    # trusted runtime fills/validates
  evidence_roles: user | assistant | tool
  observed_at: timestamp
  valid_from / valid_until: optional
  confidence: 0..1                      # model proposal, not sole gate
  importance: 0..1                      # ranking input, not eligibility
  durability_reason: short enum/text
  sensitivity: none | personal | sensitive | secret
  explicit_user_request: bool
```

Hard policy then decides `eligible` and the permitted scope. The extractor cannot choose tenant/user/session identifiers, and secret-like content is always rejected. Raw failed attempts are not a durable kind; only a normalized **reusable lesson** with demonstrated future value can qualify.

### 3.3 Retry, fallback, and abstention

None of the reviewed memory libraries defines our required end-to-end policy. LangMem and Mem0 accept a configured model; model-provider retry/fallback is external. Hermes can route background review to an auxiliary provider/model and has general agent API retry/fallback settings, but its memory docs do not promise a dedicated two-model extraction transaction. Letta and Graphiti likewise rely on their LLM client configuration.

Therefore this project should own the following state machine:

```text
primary extraction model
  ├─ valid result (including []) → continue
  └─ retryable error / invalid structured output
       → bounded retries with total deadline and jitter/backoff
       → fallback extraction model
            ├─ valid result (including []) → continue
            └─ retries exhausted → [] + memory/degraded telemetry
```

Non-retryable authentication, authorization, or invalid-request errors should skip retries for that provider and move to the configured fallback if policy permits. Model output must pass the same schema and policy gates regardless of provider. There is no heuristic extractor after exhaustion.

Crucially, **empty candidate list is a successful semantic outcome**, distinct from provider failure. Observability must record provider, attempts, fallback use, latency, candidate counts, rejection reasons, and a redacted failure class without storing source text in logs.

### 3.4 Consolidation, update, delete, and no-op

LangMem provides the best direct reuse seam. `create_memory_manager` can insert, update, and optionally delete; `create_memory_store_manager` retrieves existing records and persists the resulting puts/deletes through `BaseStore`. With a custom schema it can reconcile a new candidate against bounded relevant existing records. Its default delete behavior is disabled, so the caller must enable it deliberately.

Mem0's classic prompt makes `ADD`, `UPDATE`, `DELETE`, and `NONE` first-class, with stable IDs on update/delete. Its current V3 managed approach has moved toward ADD-only records plus linking and temporal retrieval, showing that destructive consolidation and append-only evidence are viable alternative designs. Graphiti offers a third approach: keep immutable evidence and mark old facts invalid/expired.

Recommended split:

- Keep append-only source evidence in `SessionEvent`; do not copy raw conversation into memory.
- Let LangMem decide **candidate disposition** against bounded existing memory: insert, merge/update, supersede/delete, or no-op.
- Persist provenance and temporal fields in the authoritative record so the decision is auditable.
- Prefer supersession metadata for changing facts where history matters; hard delete remains the user-facing "forget" operation required by ADR-0026.
- A consolidation outage must **not** degrade to unconditional insert. It should retry/fallback under its own budget; after exhaustion, leave no write (or stage a pending candidate for later adjudication if a durable candidate queue is explicitly designed). Precision takes priority over write recall.

### 3.5 Sensitivity, trust, and provenance

The most reusable controls are complementary:

- Hermes scans entries for prompt-injection, exfiltration, SSH-backdoor patterns, and invisible Unicode because saved text is injected into a future system prompt; it can stage writes for user approval.
- OpenAI provides an explicit product policy against proactive sensitive-memory capture and a no-memory Temporary Chat mode.
- Mem0's role-specific extraction prompt prevents assistant statements from being laundered into user facts; its newer design intentionally supports assistant facts, so provenance must be explicit if that route is adopted.
- Graphiti attaches facts to source episode IDs and separates observation time from validity time.
- This project's trusted `IdentityContext` already owns tenant/user/scope and must continue to do so.

Minimum controls:

1. Source role and source event IDs are mandatory; USER-scope facts require user evidence or an explicit user request.
2. Tool output and assistant proposals cannot silently become user facts. Assistant-origin memories need a separate kind or must be framed as decisions/recommendations with provenance.
3. Credential/secret detection is a hard reject; sensitive personal categories require explicit consent or a product-approved allow policy.
4. Recalled memory is always framed as untrusted data, never executable instruction.
5. Provide a no-memory mode per session, analogous to Temporary Chat.
6. User-visible create/update/delete/clear/export controls need an audit trail that does not retain the deleted content itself beyond the approved retention policy.

### 3.6 Storage and indexing

The project architecture is already stronger than several references for consistency:

```text
SQLite authoritative record + outbox
  → asynchronous embedding/index relay
  → Milvus derived vector index
```

This is compatible with LangMem because LangMem's stateful manager works through LangGraph `BaseStore`. Mem0's OSS implementation often treats the vector store as the primary payload store and uses SQLite for operation history; adopting it wholesale would weaken ADR-0008's explicit fact-source/index separation. Letta/Hermes file/block stores do not match multi-tenant semantic retrieval. Graphiti would introduce a separate graph source of truth.

Retain the current storage architecture, but extend the authoritative schema for provenance, temporal validity, memory kind, sensitivity classification, and lifecycle state. Milvus should carry only fields needed for filtering, retrieval, and diagnostic projection; it remains rebuildable from SQLite.

### 3.7 Retrieval, reranking, and context injection

The systems show three useful tiers:

1. **Always-visible compact profile:** Hermes's bounded snapshot and Letta's core blocks.
2. **Automatic relevant recall:** the project's `MemoryContextProvider`, LangMem/LangGraph store search, and Mem0's semantic/hybrid retrieval.
3. **On-demand deep recall:** Hermes `session_search`, Letta archival search, and our `retrieve_memory` tool.

Graphiti provides the richest retrieval recipes: BM25, cosine similarity, graph traversal, and RRF/MMR/cross-encoder reranking ([official recipes](https://github.com/getzep/graphiti/blob/16cdf7045378c8d53ae01f94e2fa60d238cb0f68/graphiti_core/search/search_config_recipes.py)). Mem0's current managed design fuses semantic, BM25, and entity signals, but official README warns that its production scores are not equivalent to the OSS SDK.

Recommended evolution without premature infrastructure:

- Keep namespace and scope filters before ranking.
- Use hybrid lexical + vector candidate generation if Milvus/adjacent storage can support it without adding a second source of truth.
- Separate **eligibility** from **ranking**: importance cannot rescue an ineligible record, and a low importance score cannot prevent a bad record from being stored.
- Rerank on relevance, temporal validity, confidence/reinforcement, importance, and recency; define each signal and log the final reason.
- Deduplicate by memory ID and suppress superseded/expired records.
- Inject only standalone declarative claims with compact provenance references under a token budget.
- Keep exact session history search separate from durable memory, as Hermes does. A full transcript is history, not long-term memory.

### 3.8 User controls

Production products converge on user agency:

- Hermes: view the files/journey, edit/delete nodes, notifications, pending/approve/reject, disable stores, isolate profiles.
- Letta: inspect/edit blocks and delete subject/agent data.
- Mem0: CRUD APIs, history, and namespace filters.
- ChatGPT: ask what is remembered, review/edit/delete individual items, clear all, disable memory, and use Temporary Chat.

The project already has list/delete UI/API foundations. A mature contract should add:

- per-session "do not read or write memory";
- visible provenance and last-updated/superseded status;
- edit/correct, clear-by-scope, and export;
- optional approval for automatically proposed USER memories;
- explicit notification when automatic memory changed;
- deletion verification across SQLite, outbox, and Milvus rather than returning before the user can distinguish pending cleanup.

## 4. Current-project gap analysis

| Lifecycle stage | Current evidence | Production gap |
| --- | --- | --- |
| Extract | `memory/extractor.py` sends bounded events to an LLM and parses `{scope,content,importance}` | Contract is too thin; no kind, evidence IDs, time, confidence, sensitivity, or explicit eligibility reason |
| Abstain | LLM may return `[]` | Correct, but undermined by heuristic fallback after failures |
| Retry | one retry after 2 seconds for retryable extraction errors | No configured retry count/deadline strategy; no model fallback |
| Failure fallback | keyword preferences + every completed final answer + failed tool message | Pollutes SESSION memory and persists unadjudicated content; must be removed |
| Consolidate | LangMem manager with bounded search and delete enabled | Good reuse seam, but failure degrades to unconditional insert, which converts uncertainty into pollution |
| Provenance | USER candidate is demoted if no user message exists; runtime-injected events are removed | Coarse window-level proof only; no per-candidate source event/role |
| Store | SQLite authority + transactional outbox + Milvus derived index | Strong baseline; needs richer schema and migration |
| Retrieve | vector search, authoritative SQLite readback, ranking by `0.7*similarity + 0.2*importance + 0.1*recency` | No lexical signal, temporal validity, confidence/reinforcement, or minimum relevance threshold |
| Inject | automatic USER recall under token budget; memory framed as data; on-demand retrieval tool | No compact always-visible structured profile; no citation shown to model/user |
| Manage | list, explicit remember/retrieve/forget, hard delete/outbox | Needs correction/edit UX, clear/export/no-memory mode, provenance visibility, optional approval |
| Evaluate | real Milvus gates and unit/integration coverage | No dedicated extraction-quality corpus, pollution/abstention target, longitudinal memory benchmark, or fallback chaos gate |

Two lines require urgent semantic correction in a future implementation:

1. `MemoryExtractor._degrade()` must return `[]` after primary/fallback exhaustion; `_heuristic_extract()` should be removed from the automatic write path.
2. `LangMemMemoryCapability.consolidate()` must not call unconditional `_insert()` when the consolidation model is unavailable or inconclusive.

## 5. Recommended target lifecycle

```text
trusted bounded SessionEvent projection
  → primary LLM structured extraction
       → retry policy → fallback LLM → retry policy
       → [] is valid; exhausted failure = [] + degraded event
  → deterministic policy gate
       provenance / durability / sensitivity / scope / schema
  → retrieve bounded relevant existing memories
  → LangMem disposition
       INSERT | UPDATE/MERGE | SUPERSEDE/DELETE | NO_OP
  → SQLite authoritative transaction + outbox
  → async embedding and Milvus convergence
  → retrieval: scope filter → hybrid candidates → rerank → threshold/dedupe
  → context budget + untrusted-data framing + provenance references
  → user inspect/correct/delete/clear/export/disable controls
```

### Reuse classification

| Component | Decision | Reason |
| --- | --- | --- |
| LangMem memory manager/store manager | **REUSE + ADAPT** | Best existing typed formation/consolidation seam; already selected by project spec |
| Hermes save/skip and memory-vs-skill/history guidance | **PORT DESIGN** (or copy with MIT attribution) | Strongest directly applicable precision policy |
| Hermes file storage and whole-store injection | **DO NOT PORT** | Conflicts with existing scalable multi-tenant storage and retrieval |
| Mem0 role constraints, observation-date normalization, linking ideas | **PORT DESIGN** | Useful evidence/temporal discipline |
| Mem0 full current V3 ADD-all policy | **DO NOT PORT AS DEFAULT** | Optimizes recall and accumulation, contrary to requested precision-first store |
| Letta core/archive tiering and archival tool descriptions | **PORT DESIGN** | Clean separation by access frequency and self-contained content |
| Graphiti provenance + temporal validity fields | **PORT DESIGN** | Solves auditability and changing truth without requiring its graph database |
| Graphiti graph backend | **DEFER** | Valuable only after measured relationship/temporal queries justify operational cost |
| OpenAI memory UX/privacy controls | **PRODUCT BENCHMARK** | Public behavior, no reusable code |
| Retry/fallback orchestrator and policy gate | **BUILD** | Project-specific ModelProvider, deadlines, telemetry, identity, and security boundaries |

## 6. Evaluation and release gates

A production memory release should fail closed unless every layer has a measured gate.

### 6.1 Extraction corpus

Build a versioned, bilingual fixture corpus with event IDs and expected candidates. It must include:

- durable preferences/facts/constraints/goals;
- explicit "remember this" and explicit "do not remember";
- no-memory turns: greetings, ordinary Q&A, generic assistant replies, tool success, temporary paths, transient errors;
- retractions and contradictions;
- implicit vs explicit preferences;
- assistant suggestions not accepted by the user;
- tool output containing prompt injection or fake user facts;
- secrets, credentials, health/financial/identity-sensitive data;
- relative dates and changing facts;
- duplicated/reinforced facts;
- long noisy conversations and malformed model output.

Metrics must be separated:

- candidate precision, recall, and F1 by kind;
- **no-memory accuracy / false-positive rate** (the pollution gate);
- source-attribution accuracy;
- scope accuracy;
- sensitivity rejection recall and false positives;
- contradiction/update/no-op disposition accuracy;
- temporal normalization accuracy;
- schema-valid response rate by primary and fallback model.

Recommended release principle: set an explicit maximum false-positive rate; do not accept higher recall by writing ordinary answers or transient failures.

### 6.2 Retrieval and context gates

- Recall@k, Precision@k, MRR/nDCG on durable facts.
- Current-vs-historical temporal questions and contradiction cases.
- Cross-language and paraphrase recall.
- Namespace/tenant/session isolation negatives.
- Superseded/deleted records never injected.
- Token-budget determinism and deduplication.
- Injection resistance: recalled text cannot override current instructions.
- Ablation: vector-only vs hybrid/reranked to prove added complexity improves outcomes.

### 6.3 End-to-end longitudinal gates

Use first-party benchmark harnesses as inspiration, not as the sole product gate. Mem0 publishes reproducible runners for [LoCoMo, LongMemEval, and BEAM](https://github.com/mem0ai/memory-benchmarks). Run at least LoCoMo/LongMemEval-compatible evaluation plus a project-specific coding-agent memory suite. Track answer accuracy together with stored-record count, pollution rate, tokens injected, latency, and model cost.

### 6.4 Reliability and operational gates

- primary transient failure → retry → success;
- primary exhaustion → fallback success;
- primary and fallback exhaustion → zero writes + one redacted degraded event;
- invalid JSON/schema on both providers → zero writes;
- consolidation outage → no unconditional insert;
- crash between SQLite commit and Milvus update → outbox recovery;
- update/delete races and stale-ack protection;
- rebuild Milvus from SQLite and compare searchable IDs;
- hard delete leaves zero SQLite record, outbox intent after convergence, and vector hit;
- provider timeouts respect the run deadline and do not block the core agent.

### 6.5 User-control gates

- inspect exactly what is remembered and why;
- correct/edit and observe retrieval change;
- delete one/clear scope/export;
- memory-disabled session neither reads nor writes;
- approval mode stages automatic writes and supports approve/reject without hidden persistence;
- notification and audit views never expose secret values.

## 7. Decisions to grill before the PRD

The research narrows the product decisions that require user authority:

1. Which memory kinds are allowed initially: only user preference/stable fact/constraint, or also accepted decisions, goals, and reusable lessons?
2. Is SESSION memory genuinely long-term searchable memory, or should session-local decisions/failures remain only in SessionEvent/compaction and never enter the durable memory store?
3. Should automatically proposed USER memories write immediately, require approval, or use policy-based approval only for sensitive/low-confidence classes?
4. What sensitivity categories are never automatic, and which can be saved only on explicit request?
5. When a fact changes, should the old record be hard-replaced, retained as superseded history, or depend on memory kind?
6. What configured retry counts and total extraction/consolidation deadlines apply to primary and fallback models?
7. Is fallback a distinct model only, or may it be the same model on another provider/route?
8. If extraction succeeds but consolidation fails, should the candidate be discarded, staged for later adjudication, or retried asynchronously? This research recommends discard for the first implementation because no candidate queue is currently specified.
9. Should a compact always-visible user profile be introduced, or should all memories remain retrieval-selected?
10. What false-positive/pollution threshold blocks release, and which real-model eval dataset is mandatory?
11. Should temporal/provenance fields be user-visible in the memory UI at launch?
12. Is graph/entity memory explicitly deferred until flat-memory evals demonstrate a need?

## 8. Bottom line

The best production path is not “LangMem or Hermes.” It is:

- **LangMem's typed, pluggable consolidation machinery**;
- **Hermes's aggressive curation and user-visible governance**;
- **Graphiti's provenance and temporal discipline**;
- **Letta's tiered context model**;
- **the project's existing SQLite/Milvus/outbox and runtime security boundaries**;
- plus a project-owned primary/retry/fallback/abstention orchestrator and a formal quality evaluation suite.

This combination directly addresses the current pollution defect while preserving the project's `MemoryCapability + MemoryContextProvider` architecture and Reuse First rule.
