# #304 / MEM-V2-8 — Final real Gate and release evidence

**Priority:** P1

**Parent GitHub issue:** #296

**Parent PRD:** `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`

## Objective

Prove the fully integrated V2 memory product works with real configured models and services, satisfies every blocking quality/security contract, leaves required temporary stores clean, and produces durable release evidence.

## Context

The user requires the final real Gate only after all implementation tickets are integrated. The approved project-gold gate uses the configured Mimo primary and Cline-compatible fallback for this evaluation lane only; it does not change production `memory.primary` / `memory.fallback` aliases or add a dedicated memory model. Real Milvus uses a dedicated temporary Memory collection. Langfuse test dataset/experiment/trace evidence is retained and checked for accidental duplicates. Milvus, Knowledge, and Qiniu temporary test data must be cleaned and verified at zero.

Credentials exist only in ignored local configuration. This ticket may check whether required keys are configured but must never print values.

## Current Behavior

Before this ticket, individual components may have focused real-service evidence but there is no single integrated release verdict for the V2 lifecycle after clean-slate cutover.

## Desired Behavior

One frozen integrated tree passes static/full regression gates, project memory quality/security gates, real model fallback, real cross-session formation/recall/governance, browser workflows, public baseline capture, privacy inspection, and external-data cleanup. Missing credentials or unexecuted cases are reported as limitations, not success.

## Scope

### Must Do

- Freeze and record code SHA/tree before Gate execution.
- Run backend and frontend full repository gates required by project protocol.
- Run the complete project memory gold set and blocking thresholds.
- Use real configured primary and fallback memory models without logging values.
- Exercise real SQLite, dedicated Milvus Memory collection, optional Knowledge context used by the scenario, Langfuse, and Qiniu/artifact lane where configured.
- Exercise automatic Formation, NO_MEMORY, ADD/UPDATE/INVALIDATE/NOOP, cross-session recall, explicit remember/forget, user edit authority, settings, deletion, and Web UI.
- Inject a transient primary failure and prove fallback behavior.
- Inspect Langfuse privacy fields and accidental duplicate counts.
- Retain approved Langfuse dataset/experiment/trace evidence.
- Clean and verify zero temporary Milvus/Knowledge/Qiniu data.
- Publish a machine-readable and human-readable verdict with executed/skipped/failed counts.

### Must Not Do

- Do not modify implementation to make a failing Gate pass inside this ticket; failures return to the owning implementation ticket/new approved issue.
- Do not treat missing credentials, all-skipped cases, or all-failed awaited work as success.
- Do not print or copy credential values.
- Do not delete approved Langfuse evidence.
- Do not rerun the clean-slate deletion against unrelated data.
- Do not move quality thresholds after seeing results without user approval.

## Requirements

- **R1:** Gate evidence binds every result to the frozen SHA/tree, exact command/lane, tool version, start/end time, and environment capability presence without values.
- **R2:** Backend lanes include Ruff, full pytest, required Docker/Phase gates, review coverage, and any memory-specific kill/security/eval suites.
- **R3:** Frontend lanes include TypeScript, Vitest, Oxlint, production build, and full Playwright at both supported viewports.
- **R4:** Real lifecycle creates valuable memory in conversation A and recalls it in distinct conversation B; NO_MEMORY creates no record.
- **R5:** Real primary success and injected-primary-failure/fallback success are separately evidenced with attempt counts and aliases.
- **R6:** Project blocking metrics meet every threshold in PRD §8.2. Public baseline results are recorded separately and remain non-blocking for the first accepted run.
- **R7:** Langfuse trace inspection shows approved metadata/hashes only and zero unexpected duplicate dataset/experiment/trace records.
- **R8:** Cleanup queries prove zero temporary records/objects in Milvus, Knowledge, and Qiniu. A cleanup failure makes the Gate fail.
- **R9:** The tracked working tree remains clean except for intentional committed Gate evidence; secrets and generated private artifacts remain untracked/ignored.

## Contracts

All PRD acceptance criteria and blocking thresholds are release contracts. Gate-0 readings must come from the repository's machine-written gate artifact. Heavy/live lane evidence must record reproducible command, frozen tree, and result rather than hand-copied terminal counts.

## Implementation Freedom

The agent may choose execution order, test data wording, uniquely generated resource prefixes, and evidence formatting. It must minimize external side effects, use dedicated targets, preserve approved Langfuse evidence, and clean every other temporary external object.

## Acceptance Criteria

- **AC1:** All required static, backend, frontend, browser, review-coverage, and repository protocol gates pass on the frozen tree.
- **AC2:** Every blocking memory metric meets its threshold with non-zero executed denominators and a versioned corpus.
- **AC3:** Real cross-session lifecycle demonstrates Formation, an adjudicated write, automatic recall, why-recalled, authoritative edit/version, and deletion.
- **AC4:** Real `memory.primary` succeeds in its lane; injected transient primary failure proves `memory.fallback` and exact retry/call budgets.
- **AC5:** Security probes produce zero secret writes, unauthorized recalls/mutations, privileged prompt effects, and ineligible-trigger writes.
- **AC6:** Kill/replay evidence produces zero lost eligible jobs and zero duplicate active logical memories.
- **AC7:** Langfuse evidence is retained, metadata-only, and free of unexpected duplicate records.
- **AC8:** Final service queries show zero temporary Milvus Memory records/collection residue as required, zero Knowledge test records, and zero Qiniu test objects/prefixes.
- **AC9:** Missing/unavailable external lanes are explicitly marked not executed and prevent a claim of full real-Gate success.
- **AC10:** Final report states verdict, SHA/tree, every lane result, residual risks, retained evidence identifiers, cleanup results, and any environment limitations.

## Dependencies

```text
blocked_by: #301 (MEM-V2-5), #302 (MEM-V2-6), #303 (MEM-V2-7), transitively #297 through #300
blocks: production release declaration for Memory V2
parallelizable: no; run only on the fully integrated frozen tree
```

## Verification

- Repository Gate-0 machine artifact and replay check.
- Full backend/frontend gates and review coverage.
- Project gold-set blocking evaluation plus public baseline adapters.
- Real model/Milvus/Langfuse/Knowledge/Qiniu scenarios with unique test identifiers.
- Browser user journey at both viewports.
- External cleanup read-back queries and clean tracked-tree check.

## Definition of Done

- A complete real-Gate report exists and all blocking lanes pass.
- Required Langfuse evidence remains available and all other temporary external data is verified clean.
- No credential value appears in logs, issues, documents, traces, screenshots, or terminal output.
- Tracker and PHASE_STATUS point to the frozen SHA, machine evidence, and final verdict.

## Approved evaluator and runner corrections (2026-09-30)

- The project gold corpus is versioned as `1.9.0` and gives each expected ADD/UPDATE one synthetic content anchor. Reports retain only content-free match counts. The existing 95% write-precision threshold is computed over persisted ADD/UPDATE records, including writes observed on cases that expected NOOP; a separate 95% target-coverage metric prevents missing expected writes from disappearing from the denominator. Matching is value-preserving rather than verbatim (ADR-0044 D13): after case and punctuation normalization every gold content word must survive into the stored record — in any order and with extra record context allowed, tokens compared exactly without stemming or plural folding — and the gold polarity must survive with it. Polarity is two-way, so a record may neither drop a gold negation nor add one the gold fact does not have, and `before` / `after` count as content words. Matching is token-set containment, not sequence matching (numeric punctuation is split). Action, kind, scope, and source authority must also match. No PRD threshold was lowered.
- Recall@6 is measured from the first six results returned by the real hybrid search, before profile/collection token-budget filtering. Final context injection remains a separate lifecycle check; report identifiers are allowlisted labels, not record text.
- The runner must require the caller-supplied `--env-file`, load that file explicitly, and fail closed when it is absent; it must not silently read the checkout's default `.env`.
- A Milvus client close error must not skip temporary SQLite cleanup or report generation. Any client-close or local-cleanup error fails the gate.
- Sampling temperature is set to zero in the real-gold evaluation runner only; production Memory V2 calls retain their configured temperature.
- API secret-write probes require the exact secret-policy rejection after a successful ordinary edit. Replay idempotency is measured separately and is not described as replay rerunning secret policy.
- `scripts/run_memory_v2_real_gold_gate.py` is only the project-memory-gold lane. It cannot satisfy AC1/AC7/AC8 or be reported as the complete #304 release gate by itself.
