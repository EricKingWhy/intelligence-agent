/**
 * #359 W-15: single-instance / quit-inspection / quit-confirmation /
 * health / security unit tests. All Electron APIs are faked; no window opens.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { claimDesktopSingleInstance } from '../src/single-instance.ts'
import {
  inspectManagedSessions,
  QUIT_INSPECTION_DEADLINE_MS,
} from '../src/quit-inspection.ts'
import { resolveDesktopQuitPrompt } from '../src/quit-confirmation.ts'
import {
  desktopWebPreferences,
  decideNavigation,
  shouldAttachHostToken,
  type LocalPagePolicy,
} from '../src/security.ts'

describe('single-instance', () => {
  it('second launch quits and routes to the owner', () => {
    let quit = false
    let focused = 0
    const fakeApp = {
      requestSingleInstanceLock: () => false,
      quit: () => { quit = true },
      on: (_event: string, listener: () => void) => { listener(); focused++ },
    }
    assert.equal(claimDesktopSingleInstance(fakeApp, () => { focused++ }), false)
    assert.equal(quit, true)
  })

  it('first launch keeps running and arms second-instance', () => {
    let armed = false
    const fakeApp = {
      requestSingleInstanceLock: () => true,
      quit: () => { throw new Error('must not quit') },
      on: () => { armed = true },
    }
    assert.equal(claimDesktopSingleInstance(fakeApp, () => {}), true)
    assert.equal(armed, true)
  })
})

describe('quit-inspection', () => {
  const deps = {
    signalClientExit: async () => ({ status: 'paused' }),
    now: () => Date.now(),
  }

  it('no managed sessions → not busy', async () => {
    const result = await inspectManagedSessions([], deps)
    assert.deepEqual(result, { busy: false })
  })

  it('deadline exceeded → unknown (conservative)', async () => {
    // The transport must enforce the per-call timeout: a call that ignores it
    // and hangs past the deadline surfaces as a rejection → 'unknown'.
    const hanging = {
      signalClientExit: (_sid: string, timeoutMs: number): Promise<{ status: string }> =>
        new Promise((_resolve, reject) => {
          setTimeout(() => reject(new Error('timeout')), timeoutMs)
        }),
      now: () => Date.now(),
    }
    const result = await inspectManagedSessions(['s1'], hanging, 10)
    assert.equal(result, 'unknown')
  })

  it('rejected call → unknown (conservative)', async () => {
    const failing = {
      signalClientExit: async () => { throw new Error('network down') },
      now: () => Date.now(),
    }
    const result = await inspectManagedSessions(['s1'], failing)
    assert.equal(result, 'unknown')
  })
})

describe('quit-confirmation prompt', () => {
  it('unknown → warns about running tasks (never silent)', () => {
    assert.equal(resolveDesktopQuitPrompt('unknown'), 'quitActiveTasks')
  })

  it('no tasks → no prompt', () => {
    assert.equal(
      resolveDesktopQuitPrompt({ busy: false }),
      undefined,
    )
  })

  it('active tasks → prompt', () => {
    assert.equal(
      resolveDesktopQuitPrompt({ busy: true }),
      'quitActiveTasks',
    )
  })
})

describe('security', () => {
  const policy: LocalPagePolicy = { scheme: 'ia-app', host: 'app' }

  it('hardened web preferences', () => {
    const prefs = desktopWebPreferences('/abs/preload.js')
    assert.equal(prefs.nodeIntegration, false)
    assert.equal(prefs.contextIsolation, true)
    assert.equal(prefs.sandbox, true)
    assert.equal(prefs.webviewTag, false)
  })

  it('external links → system browser decision', () => {
    const d = decideNavigation('https://example.com/x', policy)
    assert.equal(d.kind, 'open-external')
  })

  it('local page → allow', () => {
    const d = decideNavigation('ia-app://app/index.html', policy)
    assert.equal(d.kind, 'allow-local')
  })

  it('host token only for local pages', () => {
    assert.equal(shouldAttachHostToken('ia-app://app/index.html', policy), true)
    assert.equal(shouldAttachHostToken('https://example.com/', policy), false)
  })
})
