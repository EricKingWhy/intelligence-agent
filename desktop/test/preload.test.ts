/**
 * W-21 D3 (#815): guards for the sandboxed preload artifact.
 *
 * Measured on Electron 44 (2026-10-07, /d/w21-work/preload-probe):
 *   - `sandbox: true` parses the preload as CommonJS; an ESM preload (`.js` or
 *     `.mjs`) fails with "Cannot use import statement outside a module";
 *   - a sandboxed preload has no module resolution: `require('./sibling.cjs')`
 *     fails with "module not found";
 *   - a self-contained CJS preload loads and exposes its bridge.
 *
 * So the preload must stay (1) self-contained and (2) CJS-only. PI-Desktop states
 * the same constraint and emits CJS
 * (`apps/desktop/electron.vite.config.ts:85-97`, commit 1e07bad3); this repo has
 * no bundler, so the source itself must be single-file.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { DESKTOP_IPC, SERVICE_ORIGIN_SWITCH } from '../src/ipc.ts'

const desktopDir = join(import.meta.dirname, '..')
const preloadSource = readFileSync(join(desktopDir, 'src', 'preload.cts'), 'utf8')

describe('preload source (W-21 D3 #815)', () => {
  it('is self-contained: no relative import or require', () => {
    assert.equal(/from\s+['"]\./.test(preloadSource), false, 'a relative import cannot resolve in a sandboxed preload')
    assert.equal(/require\(\s*['"]\./.test(preloadSource), false, 'a relative require cannot resolve in a sandboxed preload')
  })

  it('uses no ESM import syntax', () => {
    // `import electron = require('electron')` is CommonJS TS syntax and stays
    // allowed; `import x from 'y'` would compile to a live import in .cts output
    // only by accident, so pin its absence.
    assert.equal(/^\s*import\s+(?!electron\s*=\s*require)/m.test(preloadSource), false)
  })

  it('names the channels ipc.ts actually handles', () => {
    for (const [name, channel] of Object.entries(DESKTOP_IPC)) {
      assert.ok(preloadSource.includes(`'${channel}'`), `preload must use DESKTOP_IPC.${name} (${channel})`)
    }
  })

  it('reads the same origin switch main appends to argv', () => {
    assert.ok(preloadSource.includes(`'${SERVICE_ORIGIN_SWITCH}'`))
  })

  it('compiles to CJS, which requires .cts in the tsconfig include set', () => {
    // Discovered while fixing this ticket: `src/**/*.ts` does not match `.cts`,
    // so the preload silently vanished from dist/ until the pattern was added.
    const tsconfig = JSON.parse(readFileSync(join(desktopDir, 'tsconfig.json'), 'utf8')) as {
      include?: string[]
    }
    assert.ok(
      (tsconfig.include ?? []).some((pattern) => pattern.includes('.cts')),
      'tsconfig include must cover .cts so `preload.cts` emits `dist/src/preload.cjs`',
    )
  })
})
