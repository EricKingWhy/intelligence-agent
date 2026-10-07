/**
 * Desktop-owned lifecycle of the local Python service (`agent-harness serve`, W-11 / #355).
 *
 * PORT DESIGN from DeepSeek Harness (MIT License):
 *   apps/desktop/src/host-process.ts (one child owned by the controller: start it,
 *   await readiness, observe failure, and escalate SIGTERM -> SIGKILL on stop)
 *   https://github.com/deepseek-ai/deepseek-harness
 *   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
 * Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.
 *
 * The DSH Node-host and account coupling is not reproduced. The child here is
 * this repository's Python service; readiness is discovered through the W-11
 * endpoint file and validated with the two-level health check (src/health.ts).
 * The shell never mints a second service: `agent-harness serve` itself attaches
 * to a running service or starts the single instance under `InstanceLock`.
 */

import { spawn as nodeSpawn } from 'node:child_process'
import { isAbsolute } from 'node:path'
import type { DesktopBackendHost } from './backend-controller.ts'
import { defaultHealthProbeDeps, readHostEndpoint } from './host-client.ts'
import { waitForBackendReady, type HealthProbeDeps, type ReadinessOptions } from './health.ts'
import { WORKSPACE_DIR_ENV, type HostEndpointInfo } from './host-protocol.ts'

/** Command the shell spawns; the service owns attach-or-start. */
export function buildServeArgs(): readonly string[] {
  return ['-m', 'agent_harness.cli', 'serve', '--host', '127.0.0.1']
}

/** A child process surface small enough to fake in tests. */
export interface ManagedChild {
  readonly pid: number | undefined
  readonly stderr: NodeJS.ReadableStream | null
  readonly stdout: NodeJS.ReadableStream | null
  onExit(listener: (code: number | null, signal: NodeJS.Signals | null) => void): void
  onError(listener: (error: Error) => void): void
  kill(signal: NodeJS.Signals): void
  readonly exitCode: number | null
}

/** Allocates the service child. */
export type SpawnChild = (
  pythonPath: string,
  args: readonly string[],
  options: { cwd: string; env: NodeJS.ProcessEnv },
) => ManagedChild

/** Default allocator over `node:child_process.spawn`. */
export function defaultSpawnChild(): SpawnChild {
  return (pythonPath, args, options) => {
    const child = nodeSpawn(pythonPath, [...args], { cwd: options.cwd, env: options.env, stdio: ['ignore', 'pipe', 'pipe'] })
    return {
      pid: child.pid,
      stderr: child.stderr,
      stdout: child.stdout,
      onExit: (listener) => { child.once('exit', listener) },
      onError: (listener) => { child.once('error', listener) },
      kill: (signal) => { child.kill(signal) },
      get exitCode() { return child.exitCode },
    }
  }
}

/** Readiness collaborators, injectable for tests. */
export interface EndpointReadinessDeps {
  readonly readEndpoint: (root: string) => Promise<HostEndpointInfo | undefined>
  readonly healthDeps: HealthProbeDeps
  readonly sleep: (ms: number) => Promise<void>
  readonly now: () => number
}

/** Wait for the endpoint file and then for the service behind it to be healthy. */
export async function awaitServiceReady(
  root: string,
  deps: EndpointReadinessDeps,
  options: ReadinessOptions = {},
): Promise<{ endpoint: HostEndpointInfo; version: string }> {
  const endpointTimeoutMs = options.portTimeoutMs ?? 20_000
  const intervalMs = options.intervalMs ?? 300
  const deadline = deps.now() + endpointTimeoutMs
  for (;;) {
    const endpoint = await deps.readEndpoint(root)
    if (endpoint !== undefined) {
      const health = await waitForBackendReady(endpoint.port, deps.healthDeps, options)
      return { endpoint, version: health.version }
    }
    if (deps.now() >= deadline) {
      throw new Error('Timed out waiting for the local service endpoint file')
    }
    await deps.sleep(intervalMs)
  }
}

/** Options for one shell-owned service child. */
export interface DesktopServiceHostOptions {
  /** Absolute data root; handed to the child as WORKSPACE_DIR and read back for the endpoint file. */
  readonly root: string
  readonly pythonPath: string
  readonly env?: NodeJS.ProcessEnv
  /** Extra variables for the child only, merged over `env` (e.g. WEB_DIST_DIR, W-21 D3). */
  readonly childEnv?: NodeJS.ProcessEnv
  readonly spawnChild?: SpawnChild
  readonly readiness?: Partial<EndpointReadinessDeps> & { portTimeoutMs?: number; readyTimeoutMs?: number; intervalMs?: number; probeTimeoutMs?: number }
  readonly onFailure?: (error: Error) => void
  readonly stopGraceMs?: number
  readonly stopKillMs?: number
}

const MAX_DIAGNOSTIC_CHARS = 16 * 1024

/** One Python service child whose readiness the controller awaits. */
export class DesktopServiceHost implements DesktopBackendHost {
  private child: ManagedChild | undefined
  private stderr = ''
  private exited = false
  private exitCode: number | null = null
  private stopRequested = false
  private failureReported = false
  private ready: { endpoint: HostEndpointInfo; version: string } | undefined

  private readonly options: DesktopServiceHostOptions

  constructor(options: DesktopServiceHostOptions) {
    this.options = options}

  /**
   * Readiness of the running child (endpoint file + health payload), or undefined
   * before a successful start. W-21 D3: the shell needs the port to load the
   * packaged UI from the service it just started.
   */
  get readiness(): { endpoint: HostEndpointInfo; version: string } | undefined {
    return this.ready
  }

  /** Spawn once and await readiness; rejects on spawn failure, exit, or readiness timeout. */
  async start(): Promise<{ endpoint: HostEndpointInfo; version: string }> {
    if (this.child !== undefined) throw new Error('desktop service host already started')
    const root = this.options.root
    // W-21 D4 (#813): both sides must agree on one absolute data root — the child
    // resolves `Settings.workspace_dir` (where it publishes `.host-service.json`)
    // from this variable, and `awaitServiceReady` reads the file from the same
    // path. A relative root would have the child resolve it against its own cwd.
    if (!isAbsolute(root)) {
      throw new Error(`desktop data root must be an absolute path: ${root}`)
    }
    const spawnChild = this.options.spawnChild ?? defaultSpawnChild()
    const child = spawnChild(this.options.pythonPath, buildServeArgs(), {
      cwd: root,
      env: {
        ...(this.options.env ?? process.env),
        ...this.options.childEnv,
        [WORKSPACE_DIR_ENV]: root,
      },
    })
    this.child = child
    child.stderr?.setEncoding?.('utf8')
    child.stderr?.on('data', (chunk: Buffer | string) => {
      this.stderr = (this.stderr + chunk.toString()).slice(-MAX_DIAGNOSTIC_CHARS)
    })
    child.onError((error) => { this.fail(error) })
    child.onExit((code) => { this.exited = true; this.exitCode = code })

    const readiness: EndpointReadinessDeps = {
      readEndpoint: this.options.readiness?.readEndpoint ?? readHostEndpoint,
      healthDeps: this.options.readiness?.healthDeps ?? defaultHealthProbeDeps(),
      sleep: this.options.readiness?.sleep ?? defaultSleep,
      now: this.options.readiness?.now ?? Date.now,
    }
    try {
      const ready = await awaitServiceReady(root, readiness, {
        ...(this.options.readiness?.portTimeoutMs === undefined ? {} : { portTimeoutMs: this.options.readiness.portTimeoutMs }),
        ...(this.options.readiness?.readyTimeoutMs === undefined ? {} : { readyTimeoutMs: this.options.readiness.readyTimeoutMs }),
        ...(this.options.readiness?.intervalMs === undefined ? {} : { intervalMs: this.options.readiness.intervalMs }),
        ...(this.options.readiness?.probeTimeoutMs === undefined ? {} : { probeTimeoutMs: this.options.readiness.probeTimeoutMs }),
      })
      this.ready = ready
      return ready
    } catch (error) {
      const suffix = this.stderr.trim() === '' ? '' : `: ${this.stderr.trim()}`
      const failure = new Error(`${error instanceof Error ? error.message : String(error)}${suffix}`)
      this.fail(failure)
      throw failure
    }
  }

  /** Request teardown and await child exit, escalating SIGTERM -> SIGKILL. */
  async stop(): Promise<void> {
    const child = this.child
    if (child === undefined) return
    this.stopRequested = true
    if (!this.exited) {
      child.kill('SIGTERM')
      if (!await this.exitsWithin(this.options.stopGraceMs ?? 10_000)) {
        child.kill('SIGKILL')
        if (!await this.exitsWithin(this.options.stopKillMs ?? 5_000)) {
          throw new Error('desktop service host did not exit after SIGKILL')
        }
      }
    }
    this.child = undefined
  }

  private async exitsWithin(milliseconds: number): Promise<boolean> {
    const deadline = Date.now() + milliseconds
    while (!this.exited) {
      if (Date.now() >= deadline) return false
      await defaultSleep(50)
    }
    return true
  }

  private fail(error: Error): void {
    if (this.failureReported || this.stopRequested) return
    this.failureReported = true
    try { this.options.onFailure?.(error) } catch (listenerError) {
      console.error('desktop service host failure listener failed', listenerError)
    }
  }
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise<void>((resolve) => {
    const timer = setTimeout(resolve, ms)
    timer.unref()
  })
}
