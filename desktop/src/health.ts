/**
 * Two-level backend readiness before the packaged web page is loaded.
 *
 * PORT DESIGN from OpenHands (MIT License) `electron/main.mjs`:
 *   waitForUrl (lines 262-274): poll until the endpoint answers at all
 *     (`res.status < 500`) — this only proves a listener is bound.
 *   waitForAgentServer (lines 287-310): poll the business route until it returns
 *     HTTP 200 (or 401, an auth surface) — this proves the server itself serves.
 *   https://github.com/OpenHands/OpenHands
 *   commit b0a1a2d1368a50a890b69ef45c54e1a74140b677
 * Full provenance: desktop/THIRD_PARTY_NOTICES.md.
 *
 * Level 1 here is a loopback TCP connect (the same first step as the server-side
 * `attach_probe`); level 2 is `GET /api/health` classified by `classifyHealth`.
 * The OpenHands Python/`uvx` download path is deliberately not reproduced — this
 * repository's Python service is owned by W-11.
 */

import { classifyHealth, type HostHealthPayload } from './host-protocol.ts'

/** Injected loopback probes and clock, so the polling logic is unit-testable. */
export interface HealthProbeDeps {
  /** Level 1: resolves true when a TCP listener accepts a connection on 127.0.0.1:port. */
  probePort(port: number, timeoutMs: number): Promise<boolean>
  /** Level 2: `GET /api/health`; rejects on network failure. */
  fetchHealth(port: number, timeoutMs: number): Promise<{ status: number; body: unknown }>
  sleep(ms: number): Promise<void>
  now(): number
}

/** Deadlines for the two levels. */
export interface ReadinessOptions {
  /** How long a bound listener may take to appear. */
  readonly portTimeoutMs?: number
  /** How long the service may take to answer health after a listener is bound. */
  readonly readyTimeoutMs?: number
  /** Delay between polls. */
  readonly intervalMs?: number
  /** Per-request timeout. */
  readonly probeTimeoutMs?: number
}

const DEFAULTS = {
  portTimeoutMs: 20_000,
  readyTimeoutMs: 60_000,
  intervalMs: 600,
  probeTimeoutMs: 2_000,
} as const

/**
 * Wait until the local service is ready to serve the web application.
 *
 * Level 1 must succeed before level 2 is attempted (a listener that never binds
 * is a different failure from a service that binds but never becomes healthy).
 * An incompatible `protocol_version` aborts immediately — waiting cannot fix it.
 *
 * @param port - loopback port from the endpoint file.
 * @param deps - probes and clock.
 * @param options - deadline overrides.
 * @returns the health payload of the ready service.
 * @throws Error when either level exceeds its deadline or the protocol is incompatible.
 */
export async function waitForBackendReady(
  port: number,
  deps: HealthProbeDeps,
  options: ReadinessOptions = {},
): Promise<HostHealthPayload> {
  const portTimeoutMs = options.portTimeoutMs ?? DEFAULTS.portTimeoutMs
  const readyTimeoutMs = options.readyTimeoutMs ?? DEFAULTS.readyTimeoutMs
  const intervalMs = options.intervalMs ?? DEFAULTS.intervalMs
  const probeTimeoutMs = options.probeTimeoutMs ?? DEFAULTS.probeTimeoutMs

  const portDeadline = deps.now() + portTimeoutMs
  for (;;) {
    if (await deps.probePort(port, probeTimeoutMs)) break
    if (deps.now() >= portDeadline) {
      throw new Error(`Timed out waiting for 127.0.0.1:${String(port)} to accept connections`)
    }
    await deps.sleep(intervalMs)
  }

  const readyDeadline = deps.now() + readyTimeoutMs
  for (;;) {
    try {
      const { status, body } = await deps.fetchHealth(port, probeTimeoutMs)
      const verdict = classifyHealth(status, body)
      if (verdict.kind === 'ready') return verdict.payload
      if (verdict.kind === 'incompatible') throw new Error(verdict.reason)
    } catch (error) {
      // An incompatibility is terminal; a transient transport/health failure is not.
      if (error instanceof Error && error.message.includes('协议版本不符')) throw error
    }
    if (deps.now() >= readyDeadline) {
      throw new Error(`Timed out waiting for 127.0.0.1:${String(port)}/api/health to become ready`)
    }
    await deps.sleep(intervalMs)
  }
}
