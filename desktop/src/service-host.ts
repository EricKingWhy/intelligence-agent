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
import {
  isProtocolIncompatible,
  waitForBackendReady,
  type HealthProbeDeps,
  type ReadinessOptions,
} from './health.ts'
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
  /**
   * Error that ends the wait before the budget does — the child this shell owns
   * died, so no amount of waiting can produce an endpoint. Same shape as the
   * TUI's own wait (`tui/src/host.ts` `waitForService(..., abortReason)`).
   */
  readonly abortReason?: () => Error | undefined
}

/**
 * Readiness budget for the shell's own child (W-21 D11 / #837).
 *
 * Measured on the W-21 gate machine with the installed artifact and exactly the
 * command the shell spawns: 8.7 s on an idle machine with a warm file cache,
 * 30.1 s and 33.6 s immediately after an install, and not within 60 s on the
 * very first run after one. The packaged service imports the whole harness
 * (~5.5 s of `agent_harness.cli` imports on their own) before it publishes the
 * endpoint file, so the old 20 s clock failed healthy starts, killed the child,
 * and left the stale record in place for the next attempt.
 *
 * 90 s covers the worst measured case with margin; a child that dies ends the
 * wait immediately (`abortReason`), so the budget only bounds a hung child.
 * The TUI budgets the same child at 30 s (`tui/src/host.ts` `START_BUDGET_MS`).
 */
const DEFAULT_START_BUDGET_MS = 90_000

/** Wait for the endpoint file and then for the service behind it to be healthy. */
export async function awaitServiceReady(
  root: string,
  deps: EndpointReadinessDeps,
  options: ReadinessOptions = {},
): Promise<{ endpoint: HostEndpointInfo; version: string }> {
  const endpointTimeoutMs = options.portTimeoutMs ?? DEFAULT_START_BUDGET_MS
  const intervalMs = options.intervalMs ?? 300
  const attemptTimeoutMs = options.attemptTimeoutMs ?? 1_500
  const deadline = deps.now() + endpointTimeoutMs
  let lastAttempt: string | undefined
  for (;;) {
    const endpoint = await deps.readEndpoint(root)
    if (endpoint !== undefined) {
      // W-21 D8 (#834): a record that fails its probe is NOT terminal here. The file
      // outlives a killed service and a reboot, and the child this shell just spawned
      // rewrites it — so re-read every pass and let only the overall deadline fail the
      // start. (The CLI's own cold start takes the same view: `attach_probe` reports a
      // dead record as STALE and `serve_once` keeps polling — src/agent_harness/
      // host_service.py.) An incompatible protocol_version still aborts immediately.
      try {
        const health = await waitForBackendReady(endpoint.port, deps.healthDeps, {
          ...options,
          portTimeoutMs: attemptTimeoutMs,
          readyTimeoutMs: attemptTimeoutMs,
        })
        return { endpoint, version: health.version }
      } catch (error) {
        if (isProtocolIncompatible(error)) throw error
        lastAttempt = error instanceof Error ? error.message : String(error)
      }
    }
    // A dead child is a terminal failure, not a slow one: report it with the
    // child's own output instead of waiting out the budget (W-21 D11 / #837).
    const abort = deps.abortReason?.()
    if (abort !== undefined) throw abort
    if (deps.now() >= deadline) {
      const diagnosis = lastAttempt === undefined ? '' : ` (last attempt: ${lastAttempt})`
      throw new Error(`Timed out waiting for the local service endpoint file${diagnosis}`)
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
  readonly readiness?: Partial<EndpointReadinessDeps> & {
    portTimeoutMs?: number
    readyTimeoutMs?: number
    intervalMs?: number
    probeTimeoutMs?: number
    attemptTimeoutMs?: number
  }
  readonly onFailure?: (error: Error) => void
  readonly stopGraceMs?: number
  readonly stopKillMs?: number
}

const MAX_DIAGNOSTIC_CHARS = 16 * 1024

/** One Python service child whose readiness the controller awaits. */
export class DesktopServiceHost implements DesktopBackendHost {
  private child: ManagedChild | undefined
  private stderr = ''
  private stdout = ''
  private spawnError: Error | undefined
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
    // W-21 D8 (#834): the child says useful things on stdout too — `agent-harness
    // serve` reports "已有本机服务在运行…（已附着，本进程退出）" there before it exits
    // (src/agent_harness/cli.py `_main_serve`), which is exactly the case where this
    // shell's Wait for the endpoint file would otherwise time out with no explanation.
    child.stdout?.setEncoding?.('utf8')
    child.stdout?.on('data', (chunk: Buffer | string) => {
      this.stdout = (this.stdout + chunk.toString()).slice(-MAX_DIAGNOSTIC_CHARS)
    })
    child.onError((error) => { this.spawnError = error; this.fail(error) })
    child.onExit((code) => { this.exited = true; this.exitCode = code })

    const readiness: EndpointReadinessDeps = {
      readEndpoint: this.options.readiness?.readEndpoint ?? readHostEndpoint,
      healthDeps: this.options.readiness?.healthDeps ?? defaultHealthProbeDeps(),
      sleep: this.options.readiness?.sleep ?? defaultSleep,
      now: this.options.readiness?.now ?? Date.now,
      // W-21 D11 (#837): readiness is bounded by this child's lifetime, not by a
      // clock alone — a child that failed to spawn or already exited can never
      // publish an endpoint, so waiting out the budget would only delay the
      // same failure (DeepSeek Harness rejects its host-ready promise from the
      // exit path the same way, apps/desktop/src/host-process.ts).
      abortReason: () => this.spawnError ?? (this.exited
        ? new Error(`the service process exited before it was ready (exit code ${String(this.exitCode)})`)
        : undefined),
    }
    try {
      const ready = await awaitServiceReady(root, readiness, {
        ...(this.options.readiness?.portTimeoutMs === undefined ? {} : { portTimeoutMs: this.options.readiness.portTimeoutMs }),
        ...(this.options.readiness?.readyTimeoutMs === undefined ? {} : { readyTimeoutMs: this.options.readiness.readyTimeoutMs }),
        ...(this.options.readiness?.intervalMs === undefined ? {} : { intervalMs: this.options.readiness.intervalMs }),
        ...(this.options.readiness?.probeTimeoutMs === undefined ? {} : { probeTimeoutMs: this.options.readiness.probeTimeoutMs }),
        ...(this.options.readiness?.attemptTimeoutMs === undefined ? {} : { attemptTimeoutMs: this.options.readiness.attemptTimeoutMs }),
      })
      this.ready = ready
      return ready
    } catch (error) {
      const output = [this.stderr.trim(), this.stdout.trim()].filter((part) => part !== '').join('\n')
      const suffix = output === '' ? '' : `: ${output}`
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
