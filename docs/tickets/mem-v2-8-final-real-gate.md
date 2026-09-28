# #304 / MEM-V2-8 — Final real Gate and release evidence

**Priority:** P1

**Parent GitHub issue:** #296

**Parent PRD:** `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`

## Objective

Prove the fully integrated V2 memory product works with real configured models and services, satisfies every blocking quality/security contract, leaves required temporary stores clean, and produces durable release evidence.

## Context

The user requires the final real Gate only after all implementation tickets are integrated. Frozen production aliases remain `memory.primary → senseaudio` and `memory.fallback → qwen`. The #304 real-gate override is recorded under the approved model amendments below and does not change those runtime defaults. Real Milvus uses a dedicated temporary Memory collection. Langfuse test dataset/experiment/trace evidence is retained and checked for accidental duplicates. Milvus, Knowledge, and Qiniu temporary test data must be cleaned and verified at zero.

Credentials exist only in ignored local configuration. This ticket may check whether required keys are configured but must never print values.

## Approved gate-local model amendment (2026-09-29)

After the configured qwen primary produced zero successful calls across 23 attempts, the user approved promoting `mimo/mimo-v2.6-flash` to this ticket's `memory.primary`. The former configured qwen model (`qwen/cline-pass/glm-5.3-flash`) remains `memory.fallback`, preserving AC4's successful-fallback requirement. The runner maps the configured chain only for #304 and validates both provider/model pairs before live calls. No PRD threshold, production role default, or credential value changes.

## Current Behavior

Before this ticket, individual components may have focused real-service evidence but there is no single integrated release verdict for the V2 lifecycle after clean-slate cutover.

## Desired Behavior

One frozen integrated tree passes static/full regression gates, project memory quality/security gates, real model fallback, real cross-session formation/recall/governance, browser workflows, public baseline capture, privacy inspection, and external-data cleanup. Missing credentials or unexecuted cases are reported as limitations, not success.

## Scope

### Must Do

- Freeze and record code SHA/tree before Gate execution.
- Run backend and frontend full repository gates required by project protocol.
- Run the complete project memory gold set and blocking thresholds.
- Keep run-end trigger eligibility separate from candidate-policy rejection; a selection rejection must not rewrite whether the run was eligible.
- Count memory.fallback as successful only after a successful fallback model call and completed job; an attempted call alone does not satisfy AC4.
- Provide a reproducible real-model gold runner with per-case diagnostics, retry-budget evidence, isolated temporary Milvus routing, and verified cleanup.
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

### Approved Gate-Driven Repair Scope (2026-09-28)

The user explicitly approved fixing blockers found by the real quality gate and rerunning #304. This authorizes the minimum implementation and evaluation-fixture corrections required by those failures, overriding the first Must Not Do bullet for this run only; thresholds and security requirements remain fixed. The first 1.3.0 run on `c9cd67a8` exposed that project-scope candidates could omit or guess `project_id`; runtime now replaces it from trusted job identity before validation and again when building the write draft. It also showed that the isolation fixture was phrased as a durable prohibition instead of a retrieval question, so that sample was clarified and the corpus incremented to 1.4.0. The 1.4.0 run then showed that `positive_episode` describes a project release decision tied to integration-test state, but expected `user_global`; PRD §4.3 makes that `project`, so the fixture is corrected in 1.5.0 without changing thresholds. A targeted real Milvus reproduction passed both affected isolation/recall cases twice and observed one retryable `unavailable` during collection cleanup; safe reports now retain only the adapter's allowlisted error category. Both configured model gateways accepted a small JSON-mode probe, so Memory V2 requests JSON object output. The exact outcome and evidence SHA/tree will be recorded after rerun.

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
- **AC11:** The evaluator reports trigger eligibility independently of candidate-policy rejection and reports PRD fallback safety separately from strict AC4 fallback-model success.
- **AC12:** scripts/run_memory_v2_real_gold_gate.py can rerun the versioned project gold set against real configured models and a dedicated Milvus collection; its report records safe per-case diagnostics and call budgets, and cleanup verifies the temporary collection is absent.

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
- Reproducible real lane: uv run python scripts/run_memory_v2_real_gold_gate.py --env-file <local-ignored-env-file> --output-dir docs/evidence.
- Real model/Milvus/Langfuse/Knowledge/Qiniu scenarios with unique test identifiers.
- Browser user journey at both viewports.
- External cleanup read-back queries and clean tracked-tree check.

## Definition of Done

- A complete real-Gate report exists and all blocking lanes pass.
- Required Langfuse evidence remains available and all other temporary external data is verified clean.
- No credential value appears in logs, issues, documents, traces, screenshots, or terminal output.
- Tracker and PHASE_STATUS point to the frozen SHA, machine evidence, and final verdict.
