# #304 / MEM-V2-8 — Final real Gate and release evidence

**Priority:** P1

**Parent GitHub issue:** #296

**Parent PRD:** `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md`

## Objective

Prove the fully integrated V2 memory product works with real configured models and services, satisfies every blocking quality/security contract, leaves required temporary stores clean, and produces durable release evidence.

## Context

The user requires the final real Gate only after all implementation tickets are integrated. Frozen production aliases remain `memory.primary → senseaudio` and `memory.fallback → qwen`. The #304 real-gate override is recorded under the approved model amendments below and does not change those runtime defaults. Real Milvus uses a dedicated temporary Memory collection. Langfuse test dataset/experiment/trace evidence is retained and checked for accidental duplicates. Milvus, Knowledge, and Qiniu temporary test data must be cleaned and verified at zero.

Credentials exist only in ignored local configuration. This ticket may check whether required keys are configured but must never print values.

## Approved gate-local model amendment (2026-09-29; supersedes D16)

After the Mimo-primary run missed the blocking quality thresholds and Qwen fallback failed, the user approved replacing both gate models: Cline `cline-pass/deepseek-v4.1-flash` is the #304 `memory.primary`, and Mimo `mimo-v2.6-flash` is `memory.fallback`. The configured Cline gateway uses an OpenAI-compatible endpoint but is not a production provider preset. For this runner only, its provider id maps to the existing generic OpenAI-compatible adapter while preserving the configured model name, base URL, and credential. A live probe found that Cline's non-streaming response wraps the completion in a `{\"success\": true, \"data\": ...}` envelope; the streaming response follows standard OpenAI SSE. The runner enables streaming only for this Cline primary. Before live calls it requires both configured base URLs to match the user-approved Cline and Mimo endpoints, allowing one trailing slash; mismatches fail closed without logging configured URLs. The observed gateway response mismatch is tracked in [Cline issue #12647](https://github.com/cline/cline/issues/12647). This gate-local mapping does not change production aliases, PRD thresholds, or credentials.

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

## 2026-09-29 Mimo-primary real-gate result (superseded attempt)

- Frozen source identity: commit `55aec704befab808d16a01404f33758693ab0c01`, tree `e82fc8bc7d22ae907586cdc83242dc89a878eb3c`. Gate-0 artifact: `docs/gate/55aec704befab808d16a01404f33758693ab0c01.json` (6/6 PASS).
- Real gate run `7d3a3645-b7aa-4ea1-bd66-be6c4545c76c`, repeated from `d25667d2-5ed8-4818-a023-b838eece9631`, executed 27/27 cases and failed 3; report: `docs/evidence/memory-v2-real-gold-v1.8.0-55aec704befa-7d3a3645.json`.
- Mimo primary recorded 25 successful calls across 15 cases (32 attempts). Three cases degraded: `positive_episode` (`ModelOutputError`); `secret_fallback_probe` and `primary_transient_fallback` (Qwen fallback `TypeError` after primary timeouts). Fallback completed 0/2 cases; AC4 is not met.
- Blocking metrics: write precision 8/10 = 0.80 (<0.95), kind accuracy 10/12 = 0.833 (<0.90), contradiction handling 2/3 = 0.667 (<0.95), and fallback model success 0/2 (<1.0). Recall@6 was 2/2 = 1.0; secret writes and unauthorized operations were 0.
- Temporary Milvus cleanup was verified absent; report credential-pattern scan was clean. This run is a failed gate, not release evidence for completion. Keep #304 open and preserve all thresholds.
- This command is the real-model gold lane only: it uses temporary SQLite per case and a dedicated Milvus collection; Langfuse is deliberately disabled for synthetic gold content, and this run does not produce Langfuse evidence or execute the Knowledge/Qiniu cleanup lanes. Do not treat it as completion of AC7/AC8 or the full release Gate.

## 2026-09-29 final Cline/Mimo real quality gate

- **Verdict:** all blocking project-gold thresholds and required real-service lanes passed on `codex/mem-v2-8-final-gate`. The final application/test code tip is `1e76bc6ae2b5272fc1a85f8457ed71de6a135066` / tree `133c73a5f7d82d77d91a9c2f8759143f3461e934`; later commits through the full-suite run added only evidence and review documents. Gate-0 is recorded after the final ticket documents are committed.
- **Real model gold:** `uv run python scripts/run_memory_v2_real_gold_gate.py --env-file "D:\intelligence-agent-backend\.env" --output-dir docs/evidence`. Report [`memory-v2-real-gold-v1.8.0-1c8382a709b1-52d6427d.json`](../evidence/memory-v2-real-gold-v1.8.0-1c8382a709b1-52d6427d.json), run `52d6427d-aafd-4e08-aef0-364d3e1436b9`, binds code `1c8382a709b1a3909779ded949eadcf50a973c7d` / tree `b7497c520f2278cadb89a932b5cf07818d1f6f43`. Result: **27/27 executed; 0 failed, degraded, skipped, unawaited, missing, or unexpected**; `worktree_clean=true`; temporary Milvus collection cleanup=`verified_absent`.
- **Blocking metrics:** write precision 11/11=1.0 (≥0.95); kind accuracy 12/12=1.0 (≥0.90); contradiction handling 3/3=1.0 (≥0.95); cross-session Recall@6 2/2=1.0 (≥0.85); fallback success 2/2=1.0 (≥1.0); NOOP 10/10=1.0 (≥0.95). Secret writes 0/27; secret-path coverage 6/6; unauthorized recalls/mutations 0/27; ineligible writes/jobs 0/5; duplicate active replay memories 0/2; lifecycle and non-privileged recall both passed.
- **Model-call evidence:** approved gate-local primary/fallback aliases both succeeded. Primary: 33 attempts, 27 successful calls across 17 successful cases. Fallback: 2 attempts / 2 successful calls, one completed fallback case. The injected transient-primary case used 3 primary attempts and 1 fallback attempt; all retry and total-call budgets passed.
- **Provenance diagnosis:** the first post-fix rerun stopped before executing cases because the prior successful report was still an untracked file. `worktree_divergence()` reported one risky untracked input and `capture_code_identity()` rejected the dirty evidence tree. That attempt made no model calls; the runner's `finally` completed temporary collection cleanup. The historical report was committed first, then the final run above passed on the clean tree. No model, threshold, or production-runtime change was used to make the rerun pass.
- **Backend full suite:** `uv run pytest -q` on `1cad46c02dc236c5b9df33156ae362fedd418767` / tree `a87f74226cc7555d3fd19a9804dac64ea17c2dba`: **4722 passed, 2 skipped, 51 deselected, 13 warnings**, exit 0, 493.64s. Warnings are reported rather than suppressed; no test failed.
- **Frontend:** on the same tree, Vitest **73 files / 1157 passed** (63.24s); `pnpm.cmd --dir web run build` (`tsc -b && vite build`) passed (1.38s). Existing Vite chunk-size warning remains. Full Playwright after the refresh-hint race fix: **460 passed / 0 failed** across Chromium 1280 and 1920, 10.4m, on code tip `1e76bc6a` / tree `133c73a5f7d82d77d91a9c2f8759143f3461e934`. The only later tree differences before these results were documentation evidence files.
- **Real external-service lanes:** re-run on `1cad46c0` / tree `a87f74226cc7555d3fd19a9804dac64ea17c2dba`, with backend `.env` loaded into the test process only and values never emitted: Milvus upsert/bulk delete 1 passed (13.24s; process-only collection override); Knowledge gate 1 passed (17.02s); Langfuse metadata/privacy trace 1 passed (20.01s); Phase 15 cloud trace upload/fetch/audit 1 passed (27.82s); Phase 15 seed-idempotency/experiment 1 passed (10.53s); Qiniu artifact save/load/inspect 1 passed (2.54s). Each test's cleanup/privacy/idempotency assertions passed. These exact Python test paths were unchanged from the earlier service verification; the only code-path delta from that tree was the Web Playwright test synchronization fix.
- **Non-blocking public baselines:** the separately reported LoCoMo and LongMemEval smoke runs are not part of the blocking project-gold verdict. Latest completed evidence remains LoCoMo answer quality 1/1 and Recall@6 1/1 in [`memory-v2-public-smoke-locomo-64bd21ec.json`](../evidence/memory-v2-public-smoke-locomo-64bd21ec.json), and LongMemEval 1/1 on both metrics in [`memory-v2-public-smoke-longmemeval-6374b2d9.json`](../evidence/memory-v2-public-smoke-longmemeval-6374b2d9.json); both reports mark `blocking=false` and verify temporary collection absence.
- **Close-out state:** the follow-up diagnosis, code repair, full backend suite, and 27-case real gold rerun are complete. Record the post-document Gate-0 result below; branch integration remains pending. No credential value appears in committed reports or this record.


### 2026-09-29 follow-up: `positive_procedure` output-contract diagnosis and final rerun

- **Trigger and reproduction:** the later full real-gold retry (`docs/evidence/memory-v2-real-gold-v1.8.0-8be9442627d3-d4d6ae22.json`) executed all 27 cases but degraded `positive_procedure` during formation. A one-case safe reproduction reduced the validation error to `candidates.0.payload.procedural.procedure: Input should be a valid string`; the model returned reusable steps in an array while the existing `ProceduralPayload` contract requires string fields. No prompt or model response body was recorded.
- **Root cause:** the formation prompt asked for reusable procedural steps but did not say `trigger`, `procedure`, and `success_condition` must be non-empty strings or explain how to encode ordered steps. This was an ambiguity between prompt instructions and the existing schema, not a transport, endpoint, or quota failure.
- **Minimal repair:** commit `ccc7c83ec22f696067a09fa565d4bf73c2d66e8e` (tree `e3a987075a65a6d7642ba0c9ffa6b52b270082f7`) clarifies that each procedural text field is a non-empty string and that ordered steps belong in one `procedure` string, never an array/object. A prompt-shape regression assertion was added. The runtime schema, providers, model selection, and frozen quality thresholds were unchanged.
- **Verification:** the new assertion failed before the prompt fix and passed after it. `uv run pytest tests/memory/v2/test_v2_executor.py tests/memory/v2/test_v2_formation.py -q` passed **140 tests**. Full `uv run pytest -q` on `69c2129fd2b4d02ac64ef819c34bc199e78ee844` / tree `eb104ece931fbad119d4eeb9f7dd321d29dd6893` passed **4766**, with 2 skipped, 51 deselected, 10 warnings, exit 0. Standards review reported PASS/no findings; Spec review reported no code findings or scope creep.
- **Final real-gold gate:** command: `uv run python scripts/run_memory_v2_real_gold_gate.py --env-file "D:\intelligence-agent-backend\.env" --output-dir docs/evidence --repeat-of d4d6ae22-9757-4e58-8286-a6b7152a5e7d`. Report [`memory-v2-real-gold-v1.8.0-84084e250b46-a79e70fc.json`](../evidence/memory-v2-real-gold-v1.8.0-84084e250b46-a79e70fc.json), run `a79e70fc-e0f5-4f6a-8d71-a6d1d7ce9505`, code SHA `84084e250b46ac23770afe4bb6b499c803940446`, tree `53b18572c4225eb3287961114e99cc037e63e056`. Result: **27/27 executed; 0 failed, degraded, skipped, unawaited, missing, or unexpected**. NOOP 10/10, write precision 11/11, kind accuracy 12/12, contradiction handling 3/3, cross-session Recall@6 2/2, fallback success 2/2; secret writes 0/27, secret paths 6/6, unauthorized recall/mutation 0/27, ineligible writes/jobs 0/5, replay duplicates 0/2. Lifecycle and non-privileged recall passed; transient-primary fallback passed 2/2. Temporary Milvus collection cleanup was `verified_absent`; report secret-pattern scan found 0.
- **Gate-0 / integration:** run a new bare Gate-0 after committing this addendum and record its machine artifact path and covered SHA. The short branch remains local and is 2 commits behind `origin/main` (`39d6470a`); no push, PR, merge, or issue close is claimed here.
