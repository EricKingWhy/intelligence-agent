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

import { DesktopServiceHost, awaitServiceReady, type EndpointReadinessDeps, type ManagedChild } from '../src/service-host.ts'
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

/**
 * Child whose pipes carry fixed text. `on('data')` hands the text over synchronously,
 * which is all the host uses (it subscribes and accumulates).
 */
function childWithOutput({ stdout = '', stderr = '' }: { stdout?: string; stderr?: string }): ManagedChild {
  const pipe = (text: string): NodeJS.ReadableStream => ({
    setEncoding: () => { /* the fake delivers strings already */ },
    on: (event: string, listener: (chunk: string) => void) => {
      if (event === 'data') listener(text)
    },
  }) as unknown as NodeJS.ReadableStream
  return {
    ...idleChild(),
    stdout: stdout === '' ? null : pipe(stdout),
    stderr: stderr === '' ? null : pipe(stderr),
  }
}

/** A record left behind by a service that has since died (dead port). */
const STALE_ENDPOINT: HostEndpointInfo = { ...ENDPOINT, pid: 1111, port: 64937 }

/** Health probes over a clock that advances with each probe, so only the fake decides readiness. */
function probingHealth(freshPort: number): { deps: HealthProbeDeps; clock: () => number } {
  let clock = 0
  return {
    clock: () => clock,
    deps: {
      probePort: async (port, timeoutMs) => {
        clock += timeoutMs
        return port === freshPort
      },
      fetchHealth: async (_port, timeoutMs) => {
        clock += timeoutMs
        return { status: 200, body: { status: 'ok', protocol_version: 1, version: '1.0.0' } }
      },
      sleep: async (ms) => { clock += ms },
      now: () => clock,
    },
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

/** Child whose exit and error events the test fires itself. */
function controllableChild(): ManagedChild & { emitExit: (code: number | null) => void; emitError: (error: Error) => void } {
  const exits: ((code: number | null, signal: NodeJS.Signals | null) => void)[] = []
  const errors: ((error: Error) => void)[] = []
  return {
    pid: 4321,
    stderr: null,
    stdout: null,
    onExit: (listener) => { exits.push(listener) },
    onError: (listener) => { errors.push(listener) },
    kill: () => { /* idle */ },
    exitCode: null,
    emitExit: (code) => { for (const listener of exits) listener(code, null) },
    emitError: (error) => { for (const listener of errors) listener(error) },
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

describe('awaitServiceReady over a stale endpoint record (W-21 D8 / #834)', () => {
  it('re-reads the file and attaches to the child that rewrote it', async () => {
    // The file outlives a killed service (and a reboot); the child this shell just
    // spawned republishes it with its own pid/port. The first record must not be
    // terminal — before this fix the shell probed the dead port until the whole
    // deadline ran out and then killed its own healthy child.
    const fresh: HostEndpointInfo = { ...ENDPOINT, pid: 4321, port: 53090 }
    let reads = 0
    const probes: number[] = []
    const { deps } = probingHealth(fresh.port)
    const ready = await awaitServiceReady(DATA_ROOT, {
      readEndpoint: async () => { reads += 1; return reads === 1 ? STALE_ENDPOINT : fresh },
      healthDeps: {
        ...deps,
        probePort: async (port, timeoutMs) => { probes.push(port); return deps.probePort(port, timeoutMs) },
      },
      sleep: deps.sleep,
      now: deps.now,
    }, { portTimeoutMs: 60_000, intervalMs: 1, attemptTimeoutMs: 1_500 })

    assert.equal(reads, 2)
    assert.ok(probes.includes(STALE_ENDPOINT.port))
    assert.equal(ready.endpoint.port, fresh.port)
    assert.equal(ready.version, '1.0.0')
  })

  it('keeps the pinned timeout message and reports the last attempt', async () => {
    const { deps } = probingHealth(-1)
    await assert.rejects(
      () => awaitServiceReady(DATA_ROOT, {
        readEndpoint: async () => STALE_ENDPOINT,
        healthDeps: deps,
        sleep: deps.sleep,
        now: deps.now,
      }, { portTimeoutMs: 3_000, intervalMs: 1, attemptTimeoutMs: 1_000 }),
      (error: Error) => {
        assert.match(error.message, /Timed out waiting for the local service endpoint file/)
        assert.match(error.message, new RegExp(`last attempt: .*127\\.0\\.0\\.1:${String(STALE_ENDPOINT.port)}`))
        return true
      },
    )
  })

  it('still aborts immediately on an incompatible protocol version', async () => {
    let reads = 0
    await assert.rejects(
      () => awaitServiceReady(DATA_ROOT, {
        readEndpoint: async () => { reads += 1; return ENDPOINT },
        healthDeps: {
          probePort: async () => true,
          fetchHealth: async () => ({
            status: 200,
            body: { status: 'ok', protocol_version: 99, version: '9.9.9' },
          }),
          sleep: async () => { /* no waiting */ },
          now: () => 0,
        },
        sleep: async () => { /* no waiting */ },
        now: () => 0,
      }, { portTimeoutMs: 60_000, attemptTimeoutMs: 1_500 }),
      /协议版本不符/,
    )
    assert.equal(reads, 1)
  })
})

describe('DesktopServiceHost diagnostics (W-21 D8 / #834)', () => {
  it('reports what the child printed on stdout, not only stderr', async () => {
    // `agent-harness serve` reports "already attached, this process exits" on stdout
    // before exiting — the one line that explains why no endpoint file appeared.
    const attachLine = '已有本机服务在运行：http://127.0.0.1:53090（已附着，本进程退出）'
    const { deps } = probingHealth(-1)
    const host = new DesktopServiceHost({
      root: DATA_ROOT,
      pythonPath: PYTHON,
      spawnChild: () => childWithOutput({ stdout: attachLine }),
      readiness: {
        readEndpoint: async () => undefined,
        healthDeps: deps,
        sleep: deps.sleep,
        now: deps.now,
        portTimeoutMs: 2_000,
      },
    })

    await assert.rejects(
      () => host.start(),
      (error: Error) => {
        assert.match(error.message, /Timed out waiting for the local service endpoint file/)
        assert.ok(error.message.includes(attachLine), `message lacks the child's stdout: ${error.message}`)
        return true
      },
    )
  })
})

describe('readiness bounded by the child, not by the clock (W-21 D11 / #837)', () => {
  /** Readiness over fake time, so a test never sleeps for real. */
  function fakeBudget(readEndpoint: () => Promise<HostEndpointInfo | undefined>): {
    deps: EndpointReadinessDeps
    reads: () => number
    elapsedMs: () => number
  } {
    let clock = 0
    let reads = 0
    return {
      reads: () => reads,
      elapsedMs: () => clock,
      deps: {
        readEndpoint: async () => { reads += 1; return readEndpoint() },
        healthDeps: {
          probePort: async (_port, timeoutMs) => { clock += timeoutMs; return false },
          fetchHealth: async () => ({ status: 503, body: undefined }),
          sleep: async () => { /* the clock moves in the loop's sleep below */ },
          now: () => clock,
        },
        sleep: async (ms) => { clock += ms },
        now: () => clock,
      },
    }
  }

  it('stops at the child instead of waiting out the budget', async () => {
    // The child is gone, so no amount of waiting can publish an endpoint. Measured
    // cold starts take 30-60 s, which is why the old 20 s clock failed (D11); a dead
    // child must not cost the whole (now 90 s) budget either.
    const child = controllableChild()
    const budget = fakeBudget(async () => undefined)
    const host = new DesktopServiceHost({
      root: DATA_ROOT,
      pythonPath: PYTHON,
      spawnChild: () => child,
      readiness: {
        readEndpoint: budget.deps.readEndpoint,
        healthDeps: budget.deps.healthDeps,
        sleep: async (ms) => {
          budget.deps.sleep(ms)
          child.emitExit(3)
        },
        now: budget.deps.now,
      },
    })

    await assert.rejects(
      () => host.start(),
      /the service process exited before it was ready \(exit code 3\)/,
    )
    assert.equal(budget.reads(), 2)
    assert.ok(budget.elapsedMs() < 90_000, `waited ${String(budget.elapsedMs())}ms`)
  })

  it('reports a spawn failure at once, not as a timeout', async () => {
    // Node delivers a spawn failure on a later tick, i.e. while the host is already
    // waiting. That is the case the shell must not report as "服务无法启动 / timed out".
    const child = controllableChild()
    const budget = fakeBudget(async () => undefined)
    let delivered = false
    const host = new DesktopServiceHost({
      root: DATA_ROOT,
      pythonPath: PYTHON,
      spawnChild: () => child,
      readiness: {
        readEndpoint: budget.deps.readEndpoint,
        healthDeps: budget.deps.healthDeps,
        sleep: async (ms) => {
          budget.deps.sleep(ms)
          if (!delivered) { delivered = true; child.emitError(new Error('spawn ENOENT')) }
        },
        now: budget.deps.now,
      },
    })

    await assert.rejects(() => host.start(), /spawn ENOENT/)
    assert.ok(budget.elapsedMs() < 90_000, `waited ${String(budget.elapsedMs())}ms`)
  })

  it('still fails after the budget when the child stays alive without publishing', async () => {
    // The safety net for a hung child: the budget is 90 s (measured worst case plus
    // margin), not the 20 s that failed healthy starts.
    const child = controllableChild()
    const budget = fakeBudget(async () => undefined)
    const host = new DesktopServiceHost({
      root: DATA_ROOT,
      pythonPath: PYTHON,
      spawnChild: () => child,
      readiness: {
        readEndpoint: budget.deps.readEndpoint,
        healthDeps: budget.deps.healthDeps,
        sleep: budget.deps.sleep,
        now: budget.deps.now,
      },
    })

    await assert.rejects(
      () => host.start(),
      /Timed out waiting for the local service endpoint file/,
    )
    assert.ok(budget.elapsedMs() >= 90_000, `budget was ${String(budget.elapsedMs())}ms`)
    assert.ok(budget.reads() > 100, `only ${String(budget.reads())} polls — not the 20 s budget`)
  })
})
