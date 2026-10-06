/**
 * Desktop quit inspection: ask the local service what quitting now would interrupt.
 *
 * PORT DESIGN from DeepSeek Harness (MIT License):
 *   apps/desktop-host/src/quit-inspection.ts (read-only exit impact query)
 *   apps/desktop/src/host-process.ts (the `quit-inspection` control request and
 *   `QUIT_INSPECTION_DEADLINE_MS = 2_000`; a slow/failed inspection counts as
 *   unknown work and the shell asks before quitting)
 *   https://github.com/deepseek-ai/deepseek-harness
 *   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
 * Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.
 *
 * The backend here is this repository's own Python service (W-11 / #355). Under
 * W-12 (#356) option B the desktop itself decides it is the last client for a
 * Task, and `POST /api/sessions/{id}/client-exit` is the existing seam: its
 * response status already says whether the Task had live work (`paused`) or was
 * a no-op (`ignored_*`). No DSH TypeScript is copied, and DSH's account/Host
 * coupling is not reproduced.
 */

/** A slower Host counts as unknown work and the shell asks before quitting. */
export const QUIT_INSPECTION_DEADLINE_MS = 2_000

/** `POST /api/sessions/{id}/client-exit` response `status` values (W-12 form). */
export const CLIENT_EXIT_PAUSED = 'paused'
export const CLIENT_EXIT_IGNORED_NOT_MANAGED = 'ignored_not_managed'
export const CLIENT_EXIT_IGNORED_ALREADY_SETTLED = 'ignored_already_settled'

/** What quitting now would affect, as derived from the per-session exit signals. */
export interface DesktopQuitInspection {
  readonly busy: boolean
}

/** One correlated answer from `POST /api/sessions/{id}/client-exit`. */
export interface ClientExitOutcome {
  readonly status: string
}

/** Collaborators of the inspection; injected so the deadline logic is unit-testable. */
export interface QuitInspectionDeps {
  /** POST /api/sessions/{id}/client-exit. Must reject on network failure, deadline, or non-2xx. */
  signalClientExit(sessionId: string, timeoutMs: number): Promise<ClientExitOutcome>
  /** Monotonic-enough clock in milliseconds. */
  now(): number
}

/**
 * Signal client-exit for every desktop-managed session under one overall deadline.
 *
 * A read failure, a rejected call, or an exceeded deadline yields `'unknown'`,
 * which the confirmation treats as "work may still be running" (DSH fail-safe).
 * With no managed sessions there is nothing the desktop could have started, so
 * the inspection reports not-busy.
 *
 * @param sessionIds - sessions this desktop opened, most-recent first.
 * @param deps - exit-signal transport and clock.
 * @param deadlineMs - overall deadline for the whole inspection.
 * @returns the inspection result, or `'unknown'` when any call failed or timed out.
 */
export async function inspectManagedSessions(
  sessionIds: readonly string[],
  deps: QuitInspectionDeps,
  deadlineMs: number = QUIT_INSPECTION_DEADLINE_MS,
): Promise<DesktopQuitInspection | 'unknown'> {
  const started = deps.now()
  let busy = false
  for (const sessionId of sessionIds) {
    const remaining = deadlineMs - (deps.now() - started)
    if (remaining <= 0) return 'unknown'
    try {
      const outcome = await deps.signalClientExit(sessionId, remaining)
      if (outcome.status === CLIENT_EXIT_PAUSED) busy = true
    } catch {
      // Network error, non-2xx, or the per-call deadline: the desktop cannot tell
      // whether work is live, so it counts as busy (DSH fail-safe).
      return 'unknown'
    }
  }
  return { busy }
}
