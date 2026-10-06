/**
 * Client side of the W-11 host attach protocol: endpoint discovery, the
 * credential-channel token, and the loopback HTTP/JTCP probes the shell drives.
 *
 * Server-side authority: `src/agent_harness/host_service.py`. Nothing here is
 * copied from upstream; it mirrors the server's constants and wire shapes.
 *
 * Token channel boundary (honest): the file credential backend
 * (`AGENT_HARNESS_HOST_CREDENTIALS=file:<path>`, the server's cross-process seam)
 * is read directly. The production Windows keyring backend is a native OS
 * facility the shell cannot open without an additional native module; no provider
 * is returned for it, so the shell reports the credential gap instead of guessing.
 */

import { createHash } from 'node:crypto'
import { realpathSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { createConnection } from 'node:net'
import { join } from 'node:path'
import {
  ENDPOINT_FILENAME,
  HOST_CREDENTIALS_ENV,
  TOKEN_USERNAME_PREFIX,
  parseHostEndpoint,
  type HostEndpointInfo,
} from './host-protocol.ts'
import type { ClientExitOutcome } from './quit-inspection.ts'
import type { HealthProbeDeps } from './health.ts'

/** The credential-channel username for one data root (`host_service.host_token_username`). */
export function hostTokenUsername(root: string): string {
  const real = realpathSync(root)
  const normalized = process.platform === 'win32' ? real.toLowerCase() : real
  const digest = createHash('sha256').update(normalized, 'utf8').digest('hex').slice(0, 16)
  return `${TOKEN_USERNAME_PREFIX}${digest}`
}

/** Supplies the host token for a data root, or undefined when the channel has none. */
export interface HostTokenProvider {
  getToken(root: string): Promise<string | undefined>
}

/** Reads the server's file credential backend (`FileCredentialStore` JSON shape). */
export function fileCredentialsTokenProvider(path: string): HostTokenProvider {
  return {
    async getToken(root: string): Promise<string | undefined> {
      try {
        const payload: unknown = JSON.parse(await readFile(path, 'utf8'))
        if (typeof payload !== 'object' || payload === null) return undefined
        const token = (payload as Record<string, unknown>)[hostTokenUsername(root)]
        return typeof token === 'string' && token.length > 0 ? token : undefined
      } catch {
        return undefined
      }
    },
  }
}

/**
 * Build the token provider the environment selects.
 * @param env - process environment.
 * @returns a provider for a recognised channel, or undefined when the channel is
 *   the OS keyring (not reachable from the shell).
 */
export function tokenProviderFromEnvironment(env: NodeJS.ProcessEnv): HostTokenProvider | undefined {
  const value = env[HOST_CREDENTIALS_ENV]?.trim() ?? ''
  if (value.startsWith('file:')) return fileCredentialsTokenProvider(value.slice('file:'.length).trim())
  return undefined
}

/** Read and validate the endpoint state file for a data root. */
export async function readHostEndpoint(root: string): Promise<HostEndpointInfo | undefined> {
  try {
    const text = await readFile(join(root, ENDPOINT_FILENAME), 'utf8')
    return parseHostEndpoint(JSON.parse(text))
  } catch {
    return undefined
  }
}

/** Minimal fetch surface used for the loopback calls (injected for tests). */
export type LoopbackFetch = (
  url: string,
  init: { method: 'GET' | 'POST'; headers: Record<string, string>; body?: string; signal: AbortSignal },
) => Promise<{ status: number; json(): Promise<unknown> }>

/** Loopback endpoint + optional token. */
export interface LoopbackTarget {
  readonly port: number
  readonly token?: string | undefined
}

function authHeaders(target: LoopbackTarget): Record<string, string> {
  return target.token === undefined ? {} : { Authorization: `Bearer ${target.token}` }
}

function loopbackUrl(target: LoopbackTarget, path: string): string {
  return `http://127.0.0.1:${String(target.port)}${path}`
}

/**
 * Notify the service that this desktop is leaving one Task (`POST /api/sessions/{id}/client-exit`).
 * @returns the parsed `{status}`; rejects on network failure, non-2xx, or the deadline.
 */
export async function signalClientExit(
  target: LoopbackTarget,
  fetchImpl: LoopbackFetch,
  sessionId: string,
  timeoutMs: number,
): Promise<ClientExitOutcome> {
  const response = await fetchImpl(loopbackUrl(target, `/api/sessions/${encodeURIComponent(sessionId)}/client-exit`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(target) },
    body: JSON.stringify({ client_id: 'desktop' }),
    signal: AbortSignal.timeout(timeoutMs),
  })
  if (response.status !== 200) throw new Error(`client-exit responded HTTP ${String(response.status)}`)
  const body: unknown = await response.json()
  if (typeof body !== 'object' || body === null || typeof (body as Record<string, unknown>).status !== 'string') {
    throw new Error('client-exit response has no status')
  }
  return { status: (body as Record<string, unknown>).status as string }
}

/** Constrained GET used by the level-2 health probe. */
export async function fetchHealth(
  fetchImpl: LoopbackFetch,
  target: LoopbackTarget,
  timeoutMs: number,
): Promise<{ status: number; body: unknown }> {
  const response = await fetchImpl(loopbackUrl(target, '/api/health'), {
    method: 'GET',
    headers: authHeaders(target),
    signal: AbortSignal.timeout(timeoutMs),
  })
  let body: unknown
  try {
    body = await response.json()
  } catch {
    body = undefined
  }
  return { status: response.status, body }
}

/** Default probes backed by `node:net` and the global `fetch` (loopback only). */
export function defaultHealthProbeDeps(): HealthProbeDeps {
  return {
    probePort: (port: number, timeoutMs: number): Promise<boolean> => new Promise((resolve) => {
      const socket = createConnection({ host: '127.0.0.1', port })
      let settled = false
      const finish = (ok: boolean): void => {
        if (settled) return
        settled = true
        socket.destroy()
        resolve(ok)
      }
      socket.setTimeout(timeoutMs, () => { finish(false) })
      socket.once('connect', () => { finish(true) })
      socket.once('error', () => { finish(false) })
    }),
    fetchHealth: async (port: number, timeoutMs: number) => {
      const response = await fetch(`http://127.0.0.1:${String(port)}/api/health`, {
        signal: AbortSignal.timeout(timeoutMs),
      })
      let body: unknown
      try {
        body = await response.json()
      } catch {
        body = undefined
      }
      return { status: response.status, body }
    },
    sleep: (ms: number) => new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, ms)
      timer.unref()
    }),
    now: () => Date.now(),
  }
}
