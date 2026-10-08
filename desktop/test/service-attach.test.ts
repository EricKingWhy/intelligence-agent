/**
 * W-21 D5 (#817): attach-or-spawn decision for the shell.
 *
 * The service is a per-data-root singleton that outlives its clients (the quit
 * path signals `client-exit` but never kills the child), so a second client must
 * attach to it instead of starting a second writer on the same storage tree.
 * A stale endpoint file — the killed-host case of #365 Run B — must fail open to
 * "start a fresh service", never to "attach to a dead port".
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { attachRunningService } from '../src/service-attach.ts'
import type { HostEndpointInfo } from '../src/host-protocol.ts'
import type { HealthProbeDeps } from '../src/health.ts'

const ROOT = 'C:\\Users\\u\\AppData\\Roaming\\intelligence-agent\\workspace'

const ENDPOINT: HostEndpointInfo = {
  pid: 4321,
  port: 60942,
  protocol_version: 1,
  auth_required: true,
  owner: 'user',
  started_at: '2026-10-07T00:00:00Z',
  service_uuid: 'uuid-1',
}

/** Probes with a monotonic clock (a frozen clock would poll forever). */
function health(answer: { port: boolean; status?: number; body?: unknown }): HealthProbeDeps {
  let clock = 0
  return {
    probePort: async () => answer.port,
    fetchHealth: async () => ({
      status: answer.status ?? 200,
      body: answer.body ?? { status: 'ok', protocol_version: 1, version: '1.0.0' },
    }),
    sleep: async () => { /* no real waiting */ },
    now: () => (clock += 1_000),
  }
}

describe('attachRunningService', () => {
  it('reads the endpoint file from the data root it is given', async () => {
    const seen: string[] = []
    const endpoint = await attachRunningService(
      { root: ROOT },
      {
        readEndpoint: async (root) => {
          seen.push(root)
          return ENDPOINT
        },
        healthDeps: health({ port: true }),
      },
    )
    assert.deepEqual(seen, [ROOT])
    assert.equal(endpoint?.port, 60942)
  })

  it('does not attach when no service ever published an endpoint', async () => {
    const endpoint = await attachRunningService(
      { root: ROOT },
      { readEndpoint: async () => undefined, healthDeps: health({ port: true }) },
    )
    assert.equal(endpoint, undefined)
  })

  it('does not attach to a stale endpoint file (killed host)', async () => {
    let probed = false
    const endpoint = await attachRunningService(
      { root: ROOT },
      {
        readEndpoint: async () => ENDPOINT,
        healthDeps: {
          ...health({ port: false }),
          probePort: async () => {
            probed = true
            return false
          },
        },
      },
    )
    assert.equal(probed, true, 'the port must be probed, not trusted from the file')
    assert.equal(endpoint, undefined)
  })

  it('does not attach to a port that accepts connections but never becomes ready', async () => {
    const endpoint = await attachRunningService(
      { root: ROOT },
      { readEndpoint: async () => ENDPOINT, healthDeps: health({ port: true, status: 503 }) },
    )
    assert.equal(endpoint, undefined)
  })

  it('does not attach across protocol versions', async () => {
    const endpoint = await attachRunningService(
      { root: ROOT },
      {
        readEndpoint: async () => ENDPOINT,
        healthDeps: health({ port: true, body: { status: 'ok', protocol_version: 2 } }),
      },
    )
    assert.equal(endpoint, undefined)
  })
})
