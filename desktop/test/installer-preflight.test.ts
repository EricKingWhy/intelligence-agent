/**
 * #361 [W-16]: manual-update preflight unit tests.
 * Reuses the W-12 `client-exit` seam: listing in-flight Tasks and safe-pausing
 * them is the same signal the quit path uses. Any failure aborts the update
 * (fail-closed; never replace the install under unknown work).
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import {
  runUpdatePreflight,
  UpdatePreflightAbortedError,
  type UpdatePreflightDeps,
} from '../src/installer/preflight.ts'

function makeDeps(
  answers: Record<string, string | Error>,
  clock: { now: number },
): UpdatePreflightDeps {
  return {
    signalClientExit: (sessionId: string, _timeoutMs: number) => {
      const answer = answers[sessionId]
      if (answer instanceof Error) return Promise.reject(answer)
      return Promise.resolve({ status: answer ?? 'ignored_already_settled' })
    },
    now: () => clock.now,
  }
}

describe('runUpdatePreflight', () => {
  it('reports no in-flight work when there are no sessions', async () => {
    const result = await runUpdatePreflight([], makeDeps({}, { now: 0 }))
    assert.deepEqual(result.inFlight, [])
    assert.deepEqual(result.idle, [])
  })

  it('partitions paused sessions from idle ones', async () => {
    const deps = makeDeps(
      { s1: 'paused', s2: 'ignored_not_managed', s3: 'ignored_already_settled' },
      { now: 0 },
    )
    const result = await runUpdatePreflight(['s1', 's2', 's3'], deps)
    assert.deepEqual(result.inFlight, ['s1'])
    assert.deepEqual(result.idle, ['s2', 's3'])
  })

  it('aborts when a client-exit signal fails (ledger not settled)', async () => {
    const deps = makeDeps({ s1: new Error('connection reset') }, { now: 0 })
    await assert.rejects(
      () => runUpdatePreflight(['s1'], deps),
      UpdatePreflightAbortedError,
    )
  })

  it('aborts on an unknown status instead of guessing', async () => {
    const deps = makeDeps({ s1: 'weird_status' }, { now: 0 })
    await assert.rejects(
      () => runUpdatePreflight(['s1'], deps),
      UpdatePreflightAbortedError,
    )
  })

  it('aborts when the overall deadline is exceeded', async () => {
    const clock = { now: 0 }
    const deps: UpdatePreflightDeps = {
      signalClientExit: (_id: string, _t: number) => {
        clock.now += 10_000 // each call burns the whole budget
        return Promise.resolve({ status: 'ignored_already_settled' })
      },
      now: () => clock.now,
    }
    await assert.rejects(
      () => runUpdatePreflight(['s1', 's2'], deps, 5_000),
      UpdatePreflightAbortedError,
    )
  })
})
