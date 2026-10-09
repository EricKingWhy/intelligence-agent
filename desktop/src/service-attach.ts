/**
 * Attach to the service that is already running for a data root (W-21 D5 / #817).
 *
 * The service is a per-data-root singleton: `DesktopServiceHost` never kills the
 * child on quit (the quit path only signals `client-exit`, see main.ts and
 * messages.quitActiveTasks), so a service started by the TUI — or by an earlier
 * desktop run — is normally still there. Attaching instead of spawning keeps one
 * writer on one storage tree; two services on the same root would be two
 * RunManagers appending to the same JSONL.
 *
 * A live service is decided by the same two-level probe the shell uses for its own
 * child: the endpoint file's port must accept a connection and answer
 * `GET /api/health` with the matching protocol version. A stale endpoint file
 * (crashed or killed service) is not attachable, and the caller starts a fresh
 * service instead.
 */

import type { HealthProbeDeps } from './health.ts'
import { waitForBackendReady } from './health.ts'
import type { HostEndpointInfo } from './host-protocol.ts'

/** Injected loopback surface, so the decision is unit-testable without a service. */
export interface AttachDeps {
  /** Read the endpoint state file for a data root (`host-client.readHostEndpoint`). */
  readonly readEndpoint: (root: string) => Promise<HostEndpointInfo | undefined>
  /** Probes (TCP connect + `GET /api/health`) and clock. */
  readonly healthDeps: HealthProbeDeps
}

/** Attach options. */
export interface AttachOptions {
  /** Data root the service was started with. */
  readonly root: string
  /**
   * Budget for each readiness level. Deliberately short: a service that does not
   * answer promptly is treated as not attachable, because the alternative (spawn
   * a fresh one) is always available.
   */
  readonly budgetMs?: number
}

const DEFAULT_BUDGET_MS = 3_000

/**
 * Return the running service for `root`, or undefined when there is none.
 * @param options - data root and probe budget.
 * @param deps - endpoint reader and health probes.
 */
export async function attachRunningService(
  options: AttachOptions,
  deps: AttachDeps,
): Promise<HostEndpointInfo | undefined> {
  const endpoint = await deps.readEndpoint(options.root)
  if (endpoint === undefined) return undefined
  const budgetMs = options.budgetMs ?? DEFAULT_BUDGET_MS
  try {
    await waitForBackendReady(endpoint.port, deps.healthDeps, {
      portTimeoutMs: budgetMs,
      readyTimeoutMs: budgetMs,
      intervalMs: 250,
      probeTimeoutMs: 1_500,
    })
    return endpoint
  } catch {
    return undefined
  }
}
