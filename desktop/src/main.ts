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
import { join } from 'node:path'
import { claimDesktopSingleInstance } from './single-instance.ts'
import { resolvePythonPath, resolveUserDataDir } from './installer/paths.ts'
import { DesktopBackendController } from './backend-controller.ts'
import { DesktopTray } from './tray.ts'
import { DesktopQuitConfirmation } from './quit-confirmation.ts'
import { DesktopBackgroundNotice } from './background-notice.ts'
import { desktopWebPreferences, decideNavigation, type LocalPagePolicy } from './security.ts'
import { DesktopServiceHost } from './service-host.ts'
import { inspectManagedSessions, type QuitInspectionDeps } from './quit-inspection.ts'
import { en, zh, resolveDesktopLocale, type DesktopLocale } from './messages.ts'

const pagePolicy: LocalPagePolicy = { scheme: 'ia-app', host: 'app' }

function currentLocale(): DesktopLocale {
  return resolveDesktopLocale(app.getLocale())
}

let mainWindow: BrowserWindow | undefined
let tray: DesktopTray | undefined
let quitting = false

function focusPrimaryWindow(): void {
  const win = mainWindow
  if (win === undefined || win.isDestroyed()) return
  if (win.isMinimized()) win.restore()
  win.show()
  win.focus()
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
  const backend = new DesktopBackendController(
    (onFailure) => new DesktopServiceHost({
      root: process.cwd(),
      pythonPath: resolvePythonPath({
        platform: process.platform,
        execPath: process.execPath,
        resourcesPath: process.resourcesPath,
        existsSync,
      }),
      onFailure,
    }),
    () => { /* state published to a loading window; minimal */ },
  )

  try {
    await backend.start(async () => { /* profile preparation: none */ })
  } catch {
    // Startup failure: retry / open redacted log / exit (ticket acceptance).
    const choice = await dialog.showMessageBox({
      type: 'error',
      title: messages.startupFailed,
      message: messages.startupFailed,
      detail: messages.startupSummary,
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
  const preload = join(__dirname, 'preload.js')
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    show: false,
    webPreferences: desktopWebPreferences(preload),
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

  // Packaged web build served from disk via ia-app://.
  await win.loadURL('ia-app://app/index.html')

  // 4. Tray: only "open" and "quit".
  tray = new DesktopTray({
    iconPath: join(__dirname, 'tray-icon.ico'),
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

/** Transport for the quit inspection: POST /api/sessions/{id}/client-exit. */
const quitDeps: QuitInspectionDeps = {
  async signalClientExit(sessionId: string, timeoutMs: number) {
    const res = await fetch(`http://127.0.0.1:8000/api/sessions/${sessionId}/client-exit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: 'desktop' }),
      signal: AbortSignal.timeout(timeoutMs),
    })
    if (!res.ok) throw new Error(`client-exit ${res.status}`)
    return (await res.json()) as { status: string }
  },
  now: () => Date.now(),
}

/** Explicit quit path: the confirmation owns the decision. */
async function requestQuit(quitConfirmation: DesktopQuitConfirmation): Promise<void> {
  if (await quitConfirmation.confirm()) {
    quitting = true
    tray?.dispose()
    app.quit()
  }
}

void main()
