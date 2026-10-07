/**
 * W-21 D4 (#813): the shell and the Python child must agree on one absolute
 * data root — the child publishes `.host-service.json` under
 * `Settings.workspace_dir` (fed by WORKSPACE_DIR), the shell reads it from the
 * same path. Before this fix the shell read `<process.cwd()>/.host-service.json`
 * while the child wrote `<cwd>/.agent/workspace/.host-service.json`, so startup
 * always ended in "Timed out waiting for the local service endpoint file".
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { isAbsolute } from 'node:path'

import { DesktopServiceHost, awaitServiceReady, type ManagedChild } from '../src/service-host.ts'
import { ENDPOINT_FILENAME, WORKSPACE_DIR_ENV, type HostEndpointInfo } from '../src/host-protocol.ts'
import { resolveDesktopDataRoot } from '../src/installer/paths.ts'
import type { HealthProbeDeps } from '../src/health.ts'

const WINDOWS_USER_DATA = 'C:\\Users\\u\\AppData\\Roaming\\intelligence-agent'
const DATA_ROOT = resolveDesktopDataRoot(WINDOWS_USER_DATA)
const PYTHON = 'C:\\Program Files\\Intelligence Agent\\resources\\python\\python.exe'

const ENDPOINT: HostEndpointInfo = {
  pid: 4321,
  port: 60942,
  protocol_version: 1,
  auth_required: true,
  owner: 'user',
  started_at: '2026-10-07T00:00:00Z',
  service_uuid: 'uuid-1',
}

/** Child surface that never emits exit/error on its own. */
function idleChild(): ManagedChild {
  return {
    pid: 4321,
    stderr: null,
    stdout: null,
    onExit: () => { /* idle */ },
    onError: () => { /* idle */ },
    kill: () => { /* idle */ },
    exitCode: null,
  }
}

/** Health probes that answer immediately with a ready service. */
function readyHealth(): HealthProbeDeps {
  return {
    probePort: async () => true,
    fetchHealth: async () => ({
      status: 200,
      body: { status: 'ok', protocol_version: 1, version: '1.0.0' },
    }),
    sleep: async () => { /* no waiting */ },
    now: () => 0,
  }
}

describe('resolveDesktopDataRoot', () => {
  it('returns an absolute directory under user data, never the cwd', () => {
    const root = resolveDesktopDataRoot(WINDOWS_USER_DATA)
    assert.equal(root, `${WINDOWS_USER_DATA}\\workspace`)
    assert.ok(isAbsolute(root))
    assert.notEqual(root, process.cwd())
  })

  it('rejects an empty user data dir', () => {
    assert.throws(() => resolveDesktopDataRoot('  '), /userData dir is empty/)
  })
})

describe('DesktopServiceHost data root', () => {
  it('passes the absolute root as WORKSPACE_DIR and reads the endpoint from it', async () => {
    const spawns: { cwd: string; env: NodeJS.ProcessEnv }[] = []
    const readRoots: string[] = []
    const host = new DesktopServiceHost({
      root: DATA_ROOT,
      pythonPath: PYTHON,
      spawnChild: (_pythonPath, _args, options) => {
        spawns.push({ cwd: options.cwd, env: options.env })
        return idleChild()
      },
      readiness: {
        readEndpoint: async (root) => { readRoots.push(root); return ENDPOINT },
        healthDeps: readyHealth(),
      },
    })

    const ready = await host.start()

    assert.equal(ready.endpoint.port, ENDPOINT.port)
    assert.equal(ready.version, '1.0.0')
    assert.equal(spawns.length, 1)
    const spawn = spawns[0]
    assert.ok(spawn !== undefined)
    // The shell's own cwd is irrelevant: the child cwd, the child's
    // WORKSPACE_DIR and the endpoint read root are one and the same value.
    assert.equal(spawn.cwd, DATA_ROOT)
    assert.equal(spawn.env[WORKSPACE_DIR_ENV], DATA_ROOT)
    assert.ok(isAbsolute(String(spawn.env[WORKSPACE_DIR_ENV])))
    assert.deepEqual(readRoots, [DATA_ROOT])
    assert.equal(`${String(spawn.env[WORKSPACE_DIR_ENV])}/${ENDPOINT_FILENAME}`,
      `${DATA_ROOT}/${ENDPOINT_FILENAME}`)
  })

  it('keeps unrelated environment variables intact', async () => {
    let seen: NodeJS.ProcessEnv = {}
    const host = new DesktopServiceHost({
      root: DATA_ROOT,
      pythonPath: PYTHON,
      env: { PATH: 'C:\\Windows', AGENT_HARNESS_HOST_CREDENTIALS: 'file:C:\\creds.json' },
      spawnChild: (_pythonPath, _args, options) => { seen = options.env; return idleChild() },
      readiness: { readEndpoint: async () => ENDPOINT, healthDeps: readyHealth() },
    })

    await host.start()

    assert.equal(seen.PATH, 'C:\\Windows')
    assert.equal(seen.AGENT_HARNESS_HOST_CREDENTIALS, 'file:C:\\creds.json')
    assert.equal(seen[WORKSPACE_DIR_ENV], DATA_ROOT)
  })

  it('merges childEnv over the inherited environment (W-21 D3)', async () => {
    let seen: NodeJS.ProcessEnv = {}
    const host = new DesktopServiceHost({
      root: DATA_ROOT,
      pythonPath: PYTHON,
      env: { PATH: 'C:\\Windows' },
      childEnv: { WEB_DIST_DIR: 'C:\\Program Files\\Intelligence Agent\\resources\\web' },
      spawnChild: (_pythonPath, _args, options) => { seen = options.env; return idleChild() },
      readiness: { readEndpoint: async () => ENDPOINT, healthDeps: readyHealth() },
    })

    await host.start()

    assert.equal(seen.WEB_DIST_DIR, 'C:\\Program Files\\Intelligence Agent\\resources\\web')
    assert.equal(seen.PATH, 'C:\\Windows')
    assert.equal(seen[WORKSPACE_DIR_ENV], DATA_ROOT)
  })

  it('exposes readiness for the shell to load the UI from (W-21 D3)', async () => {
    const host = new DesktopServiceHost({
      root: DATA_ROOT,
      pythonPath: PYTHON,
      spawnChild: () => idleChild(),
      readiness: { readEndpoint: async () => ENDPOINT, healthDeps: readyHealth() },
    })
    // Compare through a boolean: asserting on the getter itself would pin its
    // type for the rest of the scope.
    assert.equal(host.readiness === undefined, true)

    await host.start()

    const after = host.readiness
    assert.ok(after !== undefined)
    assert.equal(after.endpoint.port, ENDPOINT.port)
    assert.equal(after.version, '1.0.0')
  })

  it('refuses a relative root before spawning anything', async () => {
    let spawned = false
    const host = new DesktopServiceHost({
      root: '.agent/workspace',
      pythonPath: PYTHON,
      spawnChild: () => { spawned = true; return idleChild() },
      readiness: { readEndpoint: async () => ENDPOINT, healthDeps: readyHealth() },
    })

    await assert.rejects(() => host.start(), /data root must be an absolute path/)
    assert.equal(spawned, false)
  })
})

describe('awaitServiceReady', () => {
  it('times out on the endpoint file it was pointed at', async () => {
    let now = 0
    await assert.rejects(
      () => awaitServiceReady(DATA_ROOT, {
        readEndpoint: async () => undefined,
        healthDeps: readyHealth(),
        sleep: async () => { now += 1_000 },
        now: () => now,
      }, { portTimeoutMs: 2_000 }),
      /Timed out waiting for the local service endpoint file/,
    )
  })

  it('finds an externally started service at the same root', async () => {
    const readRoots: string[] = []
    const ready = await awaitServiceReady(DATA_ROOT, {
      readEndpoint: async (root) => { readRoots.push(root); return ENDPOINT },
      healthDeps: readyHealth(),
      sleep: async () => { /* no waiting */ },
      now: () => 0,
    })
    assert.deepEqual(readRoots, [DATA_ROOT])
    assert.equal(ready.endpoint.port, ENDPOINT.port)
  })
})
