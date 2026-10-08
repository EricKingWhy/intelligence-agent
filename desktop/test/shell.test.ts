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
import {
  SERVICE_ORIGIN_SWITCH,
  isOwnedRendererOrigin,
  parseServiceOriginArg,
} from '../src/ipc.ts'

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
  // W-21 D3 (#815): the shell's own page is the local service's loopback origin.
  const policy: LocalPagePolicy = { origin: 'http://127.0.0.1:60942' }

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
    const d = decideNavigation('http://127.0.0.1:60942/index.html', policy)
    assert.equal(d.kind, 'allow-local')
  })

  it('another loopback port is not the shell page', () => {
    assert.equal(decideNavigation('http://127.0.0.1:60943/', policy).kind, 'open-external')
  })

  it('host token only for local pages', () => {
    assert.equal(shouldAttachHostToken('http://127.0.0.1:60942/', policy), true)
    assert.equal(shouldAttachHostToken('https://example.com/', policy), false)
  })

  it('dev server origin stays allowed while developing', () => {
    const dev: LocalPagePolicy = { ...policy, devServerOrigin: 'http://127.0.0.1:5173' }
    assert.equal(decideNavigation('http://127.0.0.1:5173/', dev).kind, 'allow-local')
    assert.equal(shouldAttachHostToken('http://127.0.0.1:5173/', policy), false)
  })
})

describe('preload ownership (W-21 D3 #815)', () => {
  const origin = 'http://127.0.0.1:60942'

  it('reads the origin from the renderer argv switch', () => {
    assert.equal(parseServiceOriginArg([`${SERVICE_ORIGIN_SWITCH}${origin}`, '--other']), origin)
    assert.equal(parseServiceOriginArg(['--other']), undefined)
    assert.equal(parseServiceOriginArg([`${SERVICE_ORIGIN_SWITCH}not-a-url`]), undefined)
  })

  it('owns only the exact origin in the main frame', () => {
    assert.equal(isOwnedRendererOrigin({ currentOrigin: origin, expectedOrigin: origin, isMainFrame: true }), true)
    assert.equal(isOwnedRendererOrigin({ currentOrigin: origin, expectedOrigin: origin, isMainFrame: false }), false)
    assert.equal(isOwnedRendererOrigin({ currentOrigin: 'http://127.0.0.1:60943', expectedOrigin: origin, isMainFrame: true }), false)
    assert.equal(isOwnedRendererOrigin({ currentOrigin: origin, expectedOrigin: undefined, isMainFrame: true }), false)
  })
})
