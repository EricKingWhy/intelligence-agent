/**
 * W-21 D5 (#817): the installer artifact must carry a runnable TUI — the
 * compiled entry, the launcher at the install root, and the whole runtime
 * dependency closure of `@earendil-works/pi-tui`. Each check fails the build
 * instead of shipping a terminal client that cannot start.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  assertDesktopBuildFresh,
  assertFreshBuild,
  assertTuiRuntimeClosure,
  createWindowsInstallerConfig,
} from '../scripts/build-windows-installer.mjs'

const RESOURCES = 'C:\\app\\resources'
const APP_OUT = 'C:\\app'

/** Fake resources tree: launcher + TUI entry + node runtime + the given manifests. */
function fakeTree({ dependencies = {}, entry = true, launcher = true, node = true } = {}) {
  const files = new Map()
  if (launcher) files.set('C:\\app\\ia-tui.cmd', '')
  if (entry) files.set('C:\\app\\resources\\tui\\dist\\src\\index.js', '')
  if (node) files.set('C:\\app\\resources\\node\\node.exe', '')
  for (const [name, manifest] of Object.entries(dependencies)) {
    files.set(`C:\\app\\resources\\tui\\node_modules\\${name.split('/').join('\\')}\\package.json`, JSON.stringify(manifest))
  }
  return {
    existsSync: (path) => files.has(path),
    readJson: (path) => {
      if (!files.has(path)) throw new Error(`ENOENT ${path}`)
      return JSON.parse(files.get(path))
    },
  }
}

const PI_TUI = {
  '@earendil-works/pi-tui': { dependencies: { 'get-east-asian-width': '1.6.0', marked: '18.0.11' } },
  'get-east-asian-width': {},
  marked: {},
}

describe('assertTuiRuntimeClosure', () => {
  it('accepts a resources tree with the entry, launcher and dependency closure', () => {
    const tree = fakeTree({ dependencies: PI_TUI })
    assertTuiRuntimeClosure({ resourcesDir: RESOURCES, appOutDir: APP_OUT, ...tree })
  })

  it('fails when the compiled TUI was not shipped', () => {
    const tree = fakeTree({ dependencies: PI_TUI, entry: false })
    assert.throws(
      () => assertTuiRuntimeClosure({ resourcesDir: RESOURCES, appOutDir: APP_OUT, ...tree }),
      /bundled TUI missing: .*tui.dist.src.index\.js.*npm run build.*tui/s,
    )
  })

  it('fails when the launcher is not at the install root', () => {
    const tree = fakeTree({ dependencies: PI_TUI, launcher: false })
    assert.throws(
      () => assertTuiRuntimeClosure({ resourcesDir: RESOURCES, appOutDir: APP_OUT, ...tree }),
      /TUI launcher missing: .*ia-tui\.cmd/,
    )
  })

  it('fails when a transitive dependency of pi-tui is not shipped', () => {
    const tree = fakeTree({ dependencies: { '@earendil-works/pi-tui': PI_TUI['@earendil-works/pi-tui'] } })
    assert.throws(
      () => assertTuiRuntimeClosure({ resourcesDir: RESOURCES, appOutDir: APP_OUT, ...tree }),
      /bundled TUI dependency missing: .*(get-east-asian-width|marked).package\.json/,
    )
  })

  it('fails when the node runtime the TUI runs on was not staged', () => {
    const tree = fakeTree({ dependencies: PI_TUI, node: false })
    assert.throws(
      () => assertTuiRuntimeClosure({ resourcesDir: RESOURCES, appOutDir: APP_OUT, ...tree }),
      /bundled node runtime missing: .*resources.node.node\.exe.*prepare_node_runtime\.py/s,
    )
  })
})

describe('assertFreshBuild', () => {
  it('accepts an entry newer than its sources', () => {
    assertFreshBuild({ entry: 'dist/src/index.js', newestSourceMtimeMs: 100, entryMtimeMs: 200 })
  })

  it('fails when the compiled entry is older than the sources it was built from', () => {
    assert.throws(
      () => assertFreshBuild({ entry: 'dist/src/index.js', newestSourceMtimeMs: 300, entryMtimeMs: 200 }),
      /stale build: dist\/src\/index\.js is older than its sources/,
    )
  })
})

describe('assertDesktopBuildFresh (W-21 D10 / #836)', () => {
  const entry = 'D:\\repo\\desktop\\dist\\src\\main.js'
  const sourceDir = 'D:\\repo\\desktop\\src'

  it('accepts a compiled shell newer than desktop/src', () => {
    assertDesktopBuildFresh({ entry, sourceDir, newestSourceMtimeMs: 100, entryMtimeMs: 200 })
  })

  it('fails when desktop/dist predates desktop/src (skipped npm run build)', () => {
    assert.throws(
      () => assertDesktopBuildFresh({ entry, sourceDir, newestSourceMtimeMs: 300, entryMtimeMs: 200 }),
      /stale build: D:\\repo\\desktop\\dist\\src\\main\.js is older than its sources/,
    )
  })

  it('reads the real tree when no mtimes are injected', {
    skip: existsSync(fileURLToPath(new URL('../dist/src/main.js', import.meta.url)))
      ? false
      : 'desktop/dist is not built on this machine',
  }, () => {
    // Green here means this checkout's compiled shell is not older than its
    // sources; the installer build refuses to package when it is.
    assertDesktopBuildFresh({
      entry: fileURLToPath(new URL('../dist/src/main.js', import.meta.url)),
      sourceDir: fileURLToPath(new URL('../src', import.meta.url)),
    })
  })
})

describe('createWindowsInstallerConfig', () => {
  const config = createWindowsInstallerConfig({
    version: '0.1.0',
    appId: 'com.intelligence-agent.desktop',
    productName: 'Intelligence Agent',
    installerDir: 'C:\\repo\\desktop\\installer',
    runtimeProduct: { name: 'intelligence-agent', version: '1.0.0' },
    nodeVersion: '24.21.0',
  })

  it('ships the TUI dist and its runtime dependency closure as resources', () => {
    const tui = config.extraResources.filter((entry) => entry.to.startsWith('tui/'))
    assert.deepEqual(
      tui.map((entry) => [entry.from, entry.to]),
      [
        ['../tui/dist/', 'tui/dist/'],
        ['../tui/node_modules/', 'tui/node_modules/'],
      ],
    )
    const filter = tui[1].filter
    assert.ok(filter.includes('@earendil-works/**'), 'pi-tui itself')
    assert.ok(filter.includes('marked/**') && filter.includes('get-east-asian-width/**'), 'its dependencies')
    assert.ok(!filter.includes('**/*'), 'not the whole node_modules (devDependencies stay out)')
  })

  it('ships the staged node runtime the TUI runs on', () => {
    const node = config.extraResources.filter((entry) => entry.to === 'node/')
    assert.deepEqual(node, [
      { from: 'C:\\repo\\desktop\\installer/staging/node/', to: 'node/', filter: ['**/*'] },
    ])
  })

  it('ships the launcher at the install root (extraFiles, not resources)', () => {
    assert.deepEqual(config.extraFiles, [
      { from: 'C:\\repo\\desktop\\installer/ia-tui.cmd', to: 'ia-tui.cmd' },
    ])
  })
})

describe('ia-tui.cmd', () => {
  const launcher = readFileSync(new URL('../installer/ia-tui.cmd', import.meta.url), 'utf8')

  it('runs the shipped TUI with the bundled node runtime', () => {
    assert.match(launcher, /"%~dp0resources\\node\\node\.exe" "%~dp0resources\\tui\\dist\\src\\index\.js" %\*/)
  })

  it('does not fall back to the app exe as Node (no TTY there — W-21 D5)', () => {
    assert.ok(
      !/ELECTRON_RUN_AS_NODE/.test(launcher.replace(/^rem\b.*$/gm, '')),
      'the Electron binary cannot host a raw-mode TUI; the launcher must not run it as node',
    )
  })

  it('forwards arguments and keeps the environment local to the launcher', () => {
    assert.match(launcher, /%$|\s%\*$/m, 'forwards argv')
    assert.match(launcher, /^setlocal$/m)
    assert.match(launcher, /^endlocal & exit \/b %rc%$/m, 'endlocal then leave with the captured code')
  })

  it('propagates the child exit code (W-21 #847: a bare endlocal swallowed it)', () => {
    assert.match(launcher, /^set "rc=%ERRORLEVEL%"$/m, 'capture the node exit code before endlocal')
    assert.ok(
      !/^endlocal\s*$/m.test(launcher),
      'a bare endlocal resets the exit code to 0, so a failed TUI start reads as success',
    )
  })

  it('keeps comments parser-safe (cmd evaluates pipes/angles before rem)', () => {
    for (const line of launcher.split(/\r?\n/)) {
      if (!/^rem\b/i.test(line.trim())) continue
      assert.ok(!/[|&<>()^%]/.test(line), `cmd would parse this rem line: ${line}`)
      assert.ok(/^[\x20-\x7e]*$/.test(line), `rem line must be ASCII: ${line}`)
    }
  })
})
