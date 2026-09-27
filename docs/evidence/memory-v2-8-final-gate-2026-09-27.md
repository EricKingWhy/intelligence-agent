# Memory V2 #304 final gate — 2026-09-27

**Overall: BLOCKED.** The full code and UI gates are green, but the real-model gold gate fails its PRD thresholds, and the configured fallback provider rejects authentication. Do not merge or close #304 on this evidence.

## Tested tree

- Branch: `codex/mem-v2-8-final-gate`
- Commit: `b0b543ac2b2f56492545eff1ba2a047af6b96ab8`
- Tree: `1269df0d21214c50d499e7911e8daa18e80a8936`
- `origin/main` is the same commit; no tracked source files changed during this gate.
- Gate-0 record: [`docs/gate/b0b543ac2b2f56492545eff1ba2a047af6b96ab8.json`](../gate/b0b543ac2b2f56492545eff1ba2a047af6b96ab8.json)

## Passed lanes

- Gate-0: 6/6 lanes passed, including review coverage. The JSON above records the SHA, tree, lane commands, and results.
- Backend: `uv run pytest -q` — 4,447 passed, 2 skipped, 51 deselected. The default marker selection excludes integration and Qiniu cases; selected live lanes are listed below.
- Frontend: `pnpm --dir web exec vitest run --maxWorkers=2` — 73 files / 1,157 tests passed. The default worker setting timed out on this machine; the bounded-worker rerun passed.
- Frontend build: `pnpm --dir web run build` passed (2,122 modules). Existing bundle-size warning remains.
- Browser E2E: Playwright passed 460 tests (230 at 1280px and 230 at 1920px) in 14.2 minutes. Port 5173 belonged to the active backend clone, so the run used a temporary config and the repo Vite server on 5174; the temporary config was removed and the port was released.
- Production tools live gate: 3/3 attempts passed; `uv run python scripts/live_gate.py validate docs/live_gate/20260927T135401-b0b543ac2b2f-smoke-production-tools/evidence.json --require-pass` passed. Evidence is under [`docs/live_gate/20260927T135401-b0b543ac2b2f-smoke-production-tools/`](../live_gate/20260927T135401-b0b543ac2b2f-smoke-production-tools/).
- Real Milvus memory integration passed; the configured `memory_gate_test` collection retained its two pre-existing rows. Real cross-session Recall@6 passed its frozen threshold; the test did not emit an exact score.
- Real Langfuse privacy trace test passed: 10 traces, 12 observations, 9 memory observations, content omitted, synthetic inputs only.
- Current-tree live integration: 4 passed — Knowledge/Milvus retrieval with tenant cleanup, Qiniu save/load with prefix cleanup, Langfuse upload/fetch/audit, and idempotent dataset seed plus experiment.

## Blocking real-model gold result

Detailed run: [`docs/evidence/memory-v2-real-gold-v1.1.0-b0b543ac-2026-09-27.json`](memory-v2-real-gold-v1.1.0-b0b543ac-2026-09-27.json). It is bound to the tested commit/tree above and contains only synthetic case IDs, aggregate metrics, model aliases, attempt roles/stages, and safe error types; no prompt, model response, or credential is recorded.

- 17 cases total; 16 executed; 1 failed; no skips.
- Cross-session Recall@6: 2/2 (1.0), threshold 0.85.
- Contradiction handling: 0/2 (0.0), threshold 0.95.
- Kind accuracy: 1/9 (0.111), threshold 0.90.
- No-op accuracy: 8/8 (1.0); secret writes, unauthorized recalls/mutations, and replay duplicates were zero.
- Fallback case injected three transient primary failures. The real `memory.fallback` request returned `PermissionDeniedError`; `fallback_success=false` in the detailed report. A working Qwen credential is required to satisfy AC4.

### Evaluation caveats

The run used a one-off `uv run python -` harness that adapted the existing test helper to call `ChatModelInvoker`; the harness source was not retained as an executable script. Treat this JSON as diagnostic evidence, not a repeatable release gate, until the runner is checked in or otherwise preserved and rerun.

The aggregate `transient_primary_fallback` metric says 1.0, but that is a false pass for this run: `evaluation/memory_v2_quality.py` counts `fallback_used` alone as success (lines 248–255), which proves an attempt, not a successful fallback. The detailed report therefore records the observed provider failure separately; AC4 is not met.

The test helper also defines observed eligibility as trigger eligibility **and** zero selection rejections (`tests/evaluation/test_memory_v2_quality.py`, line 393). Three eligibility mismatches must be interpreted as a measurement issue until that definition is separated. The aggregate report schema does not retain the failed case ID or exception type.

## Browser tool choice

Playwright was used for the formal E2E acceptance lane because the repo already has a repeatable test suite with viewport projects and assertions. Chrome DevTools MCP is stronger for live network/console inspection and performance traces, and it can automate Chrome through Puppeteer; it complements the acceptance suite. This Codex session does not expose a `chrome-devtools-mcp` tool, even though the package may be installed elsewhere. See [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) and [Playwright Test](https://playwright.dev/docs/intro).

## Disposition

No product source changes were made. The report and generated gate evidence are local and uncommitted on the ticket branch. #304 remains open because the real-model quality thresholds and fallback success are not met. Do not publish or merge this branch until those blockers and the evaluation false-pass are resolved and the gates are rerun.
