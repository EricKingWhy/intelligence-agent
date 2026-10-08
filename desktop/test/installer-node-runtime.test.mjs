/**
 * W-21 D5 (#817): the terminal client runs on the artifact's own Node runtime.
 *
 * Pure unit tests for the pin, the afterPack assertion and the launcher: the
 * runner is injected, so no Windows/node staging is needed here. The real
 * red→green run (packaged artifact, interactive TUI in a console) is in the
 * #817 evidence; the reason a second runtime is shipped at all is measured in
 * installer/ia-tui.cmd's header.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  assertNodeRuntime,
  GOOD_NODE_LOCK,
  validateNodeRuntimeLockfile,
} from '../scripts/build-windows-installer.mjs'

const desktopDir = join(dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = join(desktopDir, '..')
const lockfile = JSON.parse(
  readFileSync(join(desktopDir, 'installer', 'node-runtime.lock.json'), 'utf8'),
)
const tuiPackage = JSON.parse(readFileSync(join(repoRoot, 'tui', 'package.json'), 'utf8'))
const launcher = readFileSync(join(desktopDir, 'installer', 'ia-tui.cmd'), 'utf8')

describe('node runtime lockfile pin', () => {
  it('accepts the lockfile shipped in desktop/installer/', () => {
    validateNodeRuntimeLockfile(lockfile)
  })

  it('pins an archive that matches the pinned version and target', () => {
    assert.equal(lockfile.node.archive, `node-v${lockfile.node.version}-win-x64.zip`)
    assert.ok(lockfile.node.url.endsWith(`/${lockfile.node.archive}`))
    assert.equal(lockfile.node.size, 37618919)
  })

  it('stays at or above the TUI engines floor (tui/package.json)', () => {
    const floor = /(\d+)/.exec(tuiPackage.engines.node)?.[1]
    assert.equal(lockfile.requirements.engines, tuiPackage.engines.node)
    assert.equal(lockfile.requirements.minimumMajor, Number(floor))
    const [major] = lockfile.node.version.split('.').map(Number)
    assert.ok(major >= Number(floor), `${lockfile.node.version} < engines ${tuiPackage.engines.node}`)
  })

  it('agrees with the launcher: the same executable path under the install root', () => {
    const expected = `"%~dp0resources\\${lockfile.layout.executable.replace('/', '\\')}"`
    assert.ok(launcher.includes(expected), `ia-tui.cmd does not run ${expected}`)
  })

  it('rejects a runtime below the TUI engines floor', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_NODE_LOCK))
    bad.node.version = '20.11.0'
    assert.throws(() => validateNodeRuntimeLockfile(bad), /below requirements\.minimumMajor 22/)
  })

  it('rejects a plaintext url and a malformed hash', () => {
    const http = JSON.parse(JSON.stringify(GOOD_NODE_LOCK))
    http.node.url = 'http://nodejs.org/node.zip'
    assert.throws(() => validateNodeRuntimeLockfile(http), /node\.url must be https/)

    const badHash = JSON.parse(JSON.stringify(GOOD_NODE_LOCK))
    badHash.node.sha256 = 'deadbeef'
    assert.throws(() => validateNodeRuntimeLockfile(badHash), /node\.sha256 must be 64 lowercase hex/)
  })

  it('rejects a layout whose executable is not <resourcesDir>/node.exe', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_NODE_LOCK))
    bad.layout.executable = 'node/node'
    assert.throws(() => validateNodeRuntimeLockfile(bad), /layout\.executable must be node\/node\.exe/)
  })
})

describe('assertNodeRuntime', () => {
  it('passes the staged binary its --version and returns it', () => {
    const calls = []
    const run = (exe, args) => {
      calls.push({ exe, args })
      return { status: 0, stdout: 'v24.21.0\n', stderr: '' }
    }
    assert.equal(
      assertNodeRuntime({ nodeExe: 'C:\\stage\\node.exe', version: '24.21.0', run }),
      'v24.21.0',
    )
    assert.deepEqual(calls, [{ exe: 'C:\\stage\\node.exe', args: ['--version'] }])
  })

  it('throws with the staging recipe when the runtime drifts from the pin', () => {
    const run = () => ({ status: 0, stdout: 'v22.11.0\n', stderr: '' })
    assert.throws(
      () => assertNodeRuntime({ nodeExe: 'C:\\stage\\node.exe', version: '24.21.0', run }),
      /bundled node reports v22\.11\.0 .*pins v24\.21\.0/s,
    )
  })

  it('throws when the staged binary cannot start', () => {
    const run = () => ({ error: new Error('ENOENT'), status: null, stdout: '', stderr: '' })
    assert.throws(
      () => assertNodeRuntime({ nodeExe: 'C:\\stage\\node.exe', version: '24.21.0', run }),
      /bundled node failed to start .*prepare_node_runtime\.py/s,
    )
  })
})
