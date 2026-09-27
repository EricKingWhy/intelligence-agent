# Issue #378 Gate-0 baseline evidence

Date: 2026-09-28

Ticket branch: `codex/378-playwright-flakes`

Baseline: `origin/main` at `2cda77f819883210caaf5e462d4342b892595fc4`

Measured ticket tree: `8859756d0eeda92aa2ecbd083aeb3ac8f6dbea0e` (`3c5439c6a6cd01b32afe2bb09934cc0a11b49ccc`)

## Ticket acceptance

The final ticket tree passed the full Playwright suite three consecutive times with the required command, `pnpm exec playwright test --workers=2`: each run passed 460/460 tests. The T12p/T12r targeted repeat passed 40/40 executions. No retry, single-worker substitution, or timeout increase was used.

## Gate-0 result

The recorded full Gate-0 run for the measured tree is `docs/gate/8859756d0eeda92aa2ecbd083aeb3ac8f6dbea0e.json`: diff-check, ruff, oxlint, tsc, and review coverage passed; guards failed. The guard lane reported 35 passed and these 3 failures:

- `tests/test_event_types_generated.py::test_generated_event_types_artifact_is_current`
- `tests/test_event_vocabulary_generated.py::test_generated_event_vocabulary_artifact_is_current`
- `tests/test_vocabulary_event_mapping.py::test_artifact_names_match_event_module_both_directions`

The vocabulary artifact states 43 types (41 persisted + 2 broadcast-only), while the source registers 47 (45 + 2); the artifact also contains unregistered `guard/stuck`, `model/request`, `run/paused`, and `run/resumed` entries.

## Pristine-file reproduction

For the baseline check, the three E2E files changed by #378 were temporarily restored from `origin/main`; the same six-lane command was rerun with `python scripts/gate0.py --no-record`. It again produced 5/6 lanes and the same 3 guard failures (35 passed, 3 failed). The ticket versions were restored immediately afterward and verified identical to `HEAD`.

SHA-256 of the restored ticket files:

- `web/e2e/multiturn-queue.spec.ts`: `28EA6B0A1A046A2D8BFB6B83BF12AB41116F714286FFF8E5DFEA54F7C93B5578`
- `web/e2e/stream-fallback.spec.ts`: `721D39008C030CAC3681042FC7F13AC5FB0DCA556E2870C21BEA31B3505ECA97`
- `web/e2e/y-inspector-peek.spec.ts`: `024841DABB330E4DAF3C18B824C60B4759E80C60B060A4DC719277EEDEC75484`

The three guard test files, `src/agent_harness/session/event.py`, `docs/EVENT_VOCABULARY.md`, and the generated event type files are unchanged from `origin/main`. The repeated failure is therefore pre-existing and outside #378's E2E-only code scope.

## Follow-up

Do not describe Gate-0 as green. A user decision is pending on whether to open a separate fix for the generated event vocabulary drift. The tracker, current phase index, and September archive need this baseline exception recorded after the active integration line releases those same shared files; this branch deliberately does not edit them concurrently.
