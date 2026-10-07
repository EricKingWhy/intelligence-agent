// #361 [W-16] Manual-update preflight.
//
// Before a new version replaces the installed app, the updater must list
// in-flight Tasks / other clients and safe-pause them; if pausing or ledger
// settlement fails, the replacement is aborted.
//
// PORT DESIGN from DeepSeek Harness (MIT License):
//   apps/desktop/README.md L39 — ask the Host about running tasks before
//   exit/update; any "yes" answer needs user confirmation.
//   https://github.com/deepseek-ai/deepseek-harness
//   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
// Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.
//
// The transport reuses this repo's W-12 seam instead of inventing a new one:
// `POST /api/sessions/{id}/client-exit` already reports whether the Task had
// live work (`paused`) or was a no-op (`ignored_*`), and the service settles
// the ledger before answering. A rejected signal or an unknown status aborts
// the update (fail-closed, same fail-safe as quit-inspection).

import {
  CLIENT_EXIT_PAUSED,
  CLIENT_EXIT_IGNORED_NOT_MANAGED,
  CLIENT_EXIT_IGNORED_ALREADY_SETTLED,
  type ClientExitOutcome,
} from '../quit-inspection.ts'

/** Per-call deadline for one client-exit signal, mirroring quit-inspection. */
export const UPDATE_PREFLIGHT_CALL_TIMEOUT_MS = 2_000

/** Default overall budget for the whole preflight sweep. */
export const UPDATE_PREFLIGHT_DEADLINE_MS = 10_000

/** Thrown when the update must not proceed: work state is unknown or unsettled. */
export class UpdatePreflightAbortedError extends Error {
  constructor(reason: string) {
    super(`update preflight aborted: ${reason}`)
    this.name = 'UpdatePreflightAbortedError'
  }
}

/** Collaborators of the preflight; injected so the abort logic is unit-testable. */
export interface UpdatePreflightDeps {
  /** POST /api/sessions/{id}/client-exit. Must reject on network failure or non-2xx. */
  signalClientExit(sessionId: string, timeoutMs: number): Promise<ClientExitOutcome>
  /** Monotonic-enough clock in milliseconds. */
  now(): number
}

/** What the update would interrupt; the UI lists `inFlight` for confirmation. */
export interface UpdatePreflightResult {
  /** Sessions that had live work; the service safe-paused each of them. */
  readonly inFlight: readonly string[]
  /** Sessions with nothing running; safe to replace under. */
  readonly idle: readonly string[]
}

/**
 * Signal client-exit for every desktop-managed session under one deadline.
 *
 * @param sessionIds - sessions this desktop opened, most-recent first.
 * @param deps - exit-signal transport and clock.
 * @param deadlineMs - overall budget for the whole sweep.
 * @returns the partitioned session lists for the confirmation UI.
 * @throws UpdatePreflightAbortedError when any signal fails, returns an
 *   unknown status, or the deadline is exceeded — the installer must not
 *   replace the app while work state is unknown.
 */
export async function runUpdatePreflight(
  sessionIds: readonly string[],
  deps: UpdatePreflightDeps,
  deadlineMs: number = UPDATE_PREFLIGHT_DEADLINE_MS,
): Promise<UpdatePreflightResult> {
  const startedAt = deps.now()
  const inFlight: string[] = []
  const idle: string[] = []
  for (const sessionId of sessionIds) {
    if (deps.now() - startedAt >= deadlineMs) {
      throw new UpdatePreflightAbortedError(
        `deadline of ${deadlineMs}ms exceeded before signalling ${sessionId}`,
      )
    }
    let outcome: ClientExitOutcome
    try {
      outcome = await deps.signalClientExit(sessionId, UPDATE_PREFLIGHT_CALL_TIMEOUT_MS)
    } catch (error) {
      throw new UpdatePreflightAbortedError(
        `client-exit signal failed for ${sessionId}: ${error instanceof Error ? error.message : String(error)}`,
      )
    }
    if (outcome.status === CLIENT_EXIT_PAUSED) {
      inFlight.push(sessionId)
    } else if (
      outcome.status === CLIENT_EXIT_IGNORED_NOT_MANAGED ||
      outcome.status === CLIENT_EXIT_IGNORED_ALREADY_SETTLED
    ) {
      idle.push(sessionId)
    } else {
      throw new UpdatePreflightAbortedError(
        `unknown client-exit status for ${sessionId}: ${outcome.status}`,
      )
    }
  }
  return { inFlight, idle }
}
