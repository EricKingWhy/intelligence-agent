/**
 * Electron thin-host entry (#359 W-15).
 *
 * Assembly order (PORT DESIGN from DSH `apps/desktop/src/main.ts`,
 * commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc):
 *   single-instance claim → backend controller (Python service child) →
 *   hardened window → tray → quit confirmation.
 *
 * The shell never mints a second Python service and never kills tools in
 * flight: quitting only revokes the desktop presence registration; the
 * service keeps running while any client still manages a task (W-12).
 */

import { app, BrowserWindow, dialog, Menu, shell } from 'electron'
import { existsSync } from 'node:fs'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import { claimDesktopSingleInstance } from './single-instance.ts'
import {
  hostCredentialsEnvValue,
  resolveDesktopDataRoot,
  resolveHostCredentialPath,
  resolvePythonPath,
  resolveUserDataDir,
} from './installer/paths.ts'
import { DesktopBackendController } from './backend-controller.ts'
import { DesktopTray } from './tray.ts'
import { DesktopQuitConfirmation } from './quit-confirmation.ts'
import { DesktopBackgroundNotice } from './background-notice.ts'
import { desktopWebPreferences, decideNavigation, type LocalPagePolicy } from './security.ts'
import { SERVICE_ORIGIN_SWITCH } from './ipc.ts'
import { DesktopServiceHost } from './service-host.ts'
import {
  resolvePreloadPath,
  resolveShellAssetsDir,
  resolveTrayIconPath,
  resolveWebAssetsDir,
} from './shell-paths.ts'
import { startServiceProxy, type ServiceProxy } from './service-proxy.ts'
import {
  fileCredentialsTokenProvider,
  readHostEndpoint,
  defaultHealthProbeDeps,
  signalClientExit,
  type HostTokenProvider,
  type LoopbackFetch,
} from './host-client.ts'
import { attachRunningService } from './service-attach.ts'
import { inspectManagedSessions, type QuitInspectionDeps } from './quit-inspection.ts'
import { en, zh, resolveDesktopLocale, type DesktopLocale } from './messages.ts'
import type { HostEndpointInfo } from './host-protocol.ts'

function currentLocale(): DesktopLocale {
  return resolveDesktopLocale(app.getLocale())
}

let mainWindow: BrowserWindow | undefined
let tray: DesktopTray | undefined
let quitting = false
let serviceProxy: ServiceProxy | undefined

function focusPrimaryWindow(): void {
  const win = mainWindow
  if (win === undefined || win.isDestroyed()) return
  if (win.isMinimized()) win.restore()
  win.show()
  win.focus()
}

/**
 * Read the host token the service child just published (W-21 D3 / #815).
 *
 * The child writes the credential channel before it publishes the endpoint file,
 * so one read after readiness normally suffices; the short retry covers a
 * filesystem that has not caught up yet.
 *
 * @param provider - channel selected by the environment, or undefined.
 * @param root - data root the service was started with.
 * @returns the token, or undefined when the channel has none.
 */
async function readHostToken(
  provider: HostTokenProvider | undefined,
  root: string,
): Promise<string | undefined> {
  if (provider === undefined) return undefined
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const token = await provider.getToken(root)
    if (token !== undefined) return token
    await new Promise<void>((resolve) => { setTimeout(resolve, 100) })
  }
  return undefined
}

async function main(): Promise<void> {
  // #361 [W-16]: user data lives outside the install dir
  // (%APPDATA%\intelligence-agent) and survives uninstall/updates.
  if (process.platform === 'win32' && process.env.APPDATA !== undefined && process.env.APPDATA !== '') {
    app.setPath('userData', resolveUserDataDir(process.env.APPDATA))
  }

  // 1. Single instance: a second launch only wakes the existing window.
  if (!claimDesktopSingleInstance(app, () => { focusPrimaryWindow() })) return

  await app.whenReady()

  const { messages } = currentLocale()

  // 2. Backend: one Python service child via the controller; readiness is
  // owned by DesktopServiceHost (two-level health check inside start()).
  // #361 [W-16]: prefer the installer-bundled runtime when present, so the
  // installed app never needs a system Python.
  // W-21 D3 (#815): the same service serves the packaged renderer build; the
  // shell tells it where the build lives and loads the window from that origin.
  const webAssetsDir = resolveWebAssetsDir({
    packaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
    appPath: app.getAppPath(),
    existsSync,
  })
  // W-21 D3 (#815): the shell selects the server's cross-process credential
  // backend so it can present the host token the service mints; the OS keyring is
  // unreachable from the shell (see installer/paths.ts). Both ends are configured
  // from this one value, so the token the child writes is the token this process
  // reads back.
  const credentialPath = resolveHostCredentialPath(app.getPath('userData'))
  const dataRoot = resolveDesktopDataRoot(app.getPath('userData'))
  const backend = new DesktopBackendController(
    (onFailure) => new DesktopServiceHost({
      // W-21 D4 (#813): an absolute data root under user data, not process.cwd()
      // (the install dir in a packaged app). The shell hands it to the child as
      // WORKSPACE_DIR and reads the endpoint file back from the same path.
      root: dataRoot,
      pythonPath: resolvePythonPath({
        platform: process.platform,
        execPath: process.execPath,
        resourcesPath: process.resourcesPath,
        existsSync,
      }),
      childEnv: {
        ...(webAssetsDir === undefined ? {} : { WEB_DIST_DIR: webAssetsDir }),
        AGENT_HARNESS_HOST_CREDENTIALS: hostCredentialsEnvValue(credentialPath),
      },
      onFailure,
    }),
    () => { /* state published to a loading window; minimal */ },
  )

  let endpoint: HostEndpointInfo | undefined
  let token: string | undefined
  try {
    if (webAssetsDir === undefined) {
      // Fail closed: pointing the window at a missing build renders an empty
      // page with no explanation (the W-21 D3 failure mode).
      throw new Error(
        'renderer build not found (index.html): run `npm run build` in web/, or reinstall — '
        + 'the packaged build is shipped as <resources>/web',
      )
    }
    // W-21 D5 (#817): one service per data root. A service is normally already
    // running here — started by the TUI, or left behind by an earlier desktop
    // run (the quit path signals client-exit but never kills the child) — and
    // attaching is the only way to keep a single writer on one storage tree.
    endpoint = await attachRunningService({ root: dataRoot }, {
      readEndpoint: readHostEndpoint,
      healthDeps: defaultHealthProbeDeps(),
    })
    if (endpoint === undefined) {
      // W-21 D4/D3: the child is spawned with the data root as its working
      // directory, so that directory must exist first — `spawn` fails with ENOENT
      // on a missing cwd, before the service (which would create it) ever runs.
      await backend.start(async () => { await mkdir(dataRoot, { recursive: true }) })
      endpoint = backend.host?.readiness?.endpoint
    }
    if (endpoint === undefined) {
      throw new Error('desktop backend reports ready without a service endpoint')
    }
    // The credential channel is read after readiness: the service writes the
    // token before it publishes the endpoint file.
    token = await readHostToken(fileCredentialsTokenProvider(credentialPath), dataRoot)
    if (endpoint.auth_required && token === undefined) {
      // Fail closed with the reason instead of loading a page that every request
      // 401s: no token means the attached service was started without the file
      // credential channel this shell can read.
      throw new Error(
        `the running service requires a credential that is not in ${credentialPath} — `
        + 'start the service through the desktop or the TUI so both ends share the channel',
      )
    }
  } catch (error) {
    // Startup failure: retry / open redacted log / exit (ticket acceptance).
    const detail = `${messages.startupSummary}${error instanceof Error ? `\n\n${error.message}` : ''}`
    const choice = await dialog.showMessageBox({
      type: 'error',
      title: messages.startupFailed,
      message: messages.startupFailed,
      detail,
      buttons: [messages.startupRetry, messages.startupOpenLog, messages.startupExit],
      defaultId: 0,
      cancelId: 2,
    })
    if (choice.response === 0) return main()
    if (choice.response === 1) {
      await dialog.showMessageBox({ message: messages.startupNoLog })
    }
    app.exit(1)
    return
  }

  // 3. Hardened window.
  // W-21 D6 (#814): ESM has no CommonJS directory global; assets are resolved
  // from this module's own directory (see shell-paths.ts), never from cwd.
  const assetsDir = resolveShellAssetsDir(import.meta.dirname)
  const preload = resolvePreloadPath(assetsDir)
  if (endpoint === undefined) {
    // Unreachable: the catch above returns. Kept as the invariant it is.
    throw new Error('desktop backend reports ready without a service endpoint')
  }
  // W-21 D3 (#815): the window loads the shell's own loopback proxy, which
  // attaches the host token on the way to the service. The page therefore keeps
  // addressing its API and live channel relative to its own origin (no token, no
  // second origin) while the service stays fail-closed.
  serviceProxy = await startServiceProxy({
    serviceOrigin: `http://127.0.0.1:${String(endpoint.port)}`,
    ...(token === undefined ? {} : { token }),
  })
  serviceTarget = { port: endpoint.port, ...(token === undefined ? {} : { token }) }
  const pagePolicy: LocalPagePolicy = { origin: serviceProxy.origin }
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    show: false,
    webPreferences: {
      ...desktopWebPreferences(preload),
      // The renderer learns its own origin from this switch (preload bridge
      // ownership) instead of trusting a hardcoded scheme (W-21 D3).
      additionalArguments: [`${SERVICE_ORIGIN_SWITCH}${pagePolicy.origin}`],
    },
  })
  mainWindow = win
  win.once('ready-to-show', () => { win.show() })

  // External links → system browser, never carrying the host token.
  win.webContents.setWindowOpenHandler(({ url }) => {
    const decision = decideNavigation(url, pagePolicy)
    if (decision.kind === 'open-external') void shell.openExternal(url)
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (event, url) => {
    if (decideNavigation(url, pagePolicy).kind !== 'allow-local') event.preventDefault()
  })

  // Packaged web build served through the shell's loopback proxy (W-21 D3).
  // #891 P2: the window opens through the launch URL, whose per-start token
  // exchanges for the session cookie before the page itself loads; the token
  // never reaches the renderer (最终文档的 URL 干净 via the 302 to `./`).
  await win.loadURL(serviceProxy.launchUrl)

  // 4. Tray: only "open" and "quit".
  tray = new DesktopTray({
    iconPath: resolveTrayIconPath(assetsDir),
    locale: currentLocale,
    open: () => { focusPrimaryWindow() },
    quit: () => { void requestQuit(quitConfirmation) },
  })

  // 5. Quit: ×/Alt+F4 hides to tray (first time prompts); explicit quit
  // inspects desktop-managed tasks, conservative on unknown. No force-quit
  // bypass exists anywhere in this file.
  const backgroundNotice = new DesktopBackgroundNotice({
    markerPath: join(app.getPath('userData'), 'background-ack'),
    locale: currentLocale,
    show: (options) => dialog.showMessageBox(options),
    focus: () => { focusPrimaryWindow() },
  })
  const quitConfirmation = new DesktopQuitConfirmation({
    locale: currentLocale,
    // Sessions this desktop opened, most-recent first. No sessions →
    // undefined (nothing the desktop could have started → quit proceeds).
    // Otherwise signal client-exit per session under one deadline;
    // 'unknown' (failure/timeout) → conservative prompt (DSH fail-safe).
    inspect: () => desktopSessionIds.length === 0
      ? undefined
      : inspectManagedSessions(desktopSessionIds, quitDeps),
    show: (options) => dialog.showMessageBox(options),
    focus: () => { focusPrimaryWindow() },
  })

  win.on('close', (event) => {
    if (quitting) return
    event.preventDefault()
    backgroundNotice.close(() => { win.hide() })
  })

  Menu.setApplicationMenu(null)
}

/** Sessions this desktop instance opened (most-recent first). */
const desktopSessionIds: string[] = []

/**
 * Loopback target of the service child, learned from its readiness (W-21 D3).
 *
 * The port is chosen by the OS at startup (`port=0`), so it cannot be a
 * constant: a hardcoded port would send the quit inspection to a dead endpoint
 * and every teardown would look "unknown".
 */
let serviceTarget: { readonly port: number; readonly token?: string | undefined } | undefined

/** Transport for the quit inspection: POST /api/sessions/{id}/client-exit. */
const quitDeps: QuitInspectionDeps = {
  async signalClientExit(sessionId: string, timeoutMs: number) {
    const target = serviceTarget
    if (target === undefined) throw new Error('desktop: service endpoint is not known yet')
    return signalClientExit(target, loopbackFetch, sessionId, timeoutMs)
  },
  now: () => Date.now(),
}

/** The global fetch narrowed to the loopback surface `host-client.ts` consumes. */
const loopbackFetch: LoopbackFetch = async (url, init) => fetch(url, init)

/** Explicit quit path: the confirmation owns the decision. */
async function requestQuit(quitConfirmation: DesktopQuitConfirmation): Promise<void> {
  if (await quitConfirmation.confirm()) {
    quitting = true
    tray?.dispose()
    try {
      await serviceProxy?.close()
    } catch { /* the service child owns the session; a proxy close failure must not block quit */ }
    app.quit()
  }
}

void main()
