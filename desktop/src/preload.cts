/**
 * Preload: expose only the restricted bridge to the shell's own local page.
 *
 * Security instruction (W-15): `contextIsolation` + `sandbox` on, Node integration
 * off; the page gets directory picking, window state, a non-secret service
 * bootstrap, and (#826 / MM-05) a one-method host-path lookup — never arbitrary
 * file access, raw `ipcRenderer`, or credentials.
 * The host token never reaches the renderer at all: main injects it into the
 * loopback proxy it owns (src/service-proxy.ts), so the local page never holds it.
 *
 * ADAPTED from DeepSeek Harness (MIT License):
 *   apps/desktop/src/preload-app.ts:76-84 (the one-method host-path bridge:
 *   `pathFor(file)` delegating to `webUtils.getPathForFile`)
 *   https://github.com/deepseek-ai/deepseek-harness
 *   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
 * Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.
 * Changes: the bridge is gated by `isOwnLocalPage` (exact origin + main frame)
 * instead of the upstream `dsh-app:` scheme, an unowned frame gets no host-path
 * global at all, and the global name is this repo's `__IA_HOST_PATHS__`.
 *
 * W-21 D3 (#815) — why this file is `.cts` and import-free:
 * a sandboxed renderer parses its preload as CommonJS and provides no module
 * resolution, so an ESM preload fails to load (measured: "Cannot use import
 * statement outside a module" for both `preload.js` and `preload.mjs` under
 * `sandbox: true`, and "module not found: ./sibling.cjs" for a relative
 * require). PI-Desktop states the same constraint and emits CJS
 * (`apps/desktop/electron.vite.config.ts:85-97`, commit 1e07bad3: "The preload
 * must be a fully bundled CJS file so it can run in a sandboxed renderer
 * without Node module resolution"); this repo has no bundler, so the file is
 * self-contained by construction and the emitted artifact is
 * `dist/src/preload.cjs`. No PI-Desktop code is copied — see
 * desktop/THIRD_PARTY_NOTICES.md. `desktop/test/preload.test.ts` pins both
 * properties, and `desktop/src/ipc.ts` keeps the channel names it must match.
 * The canonical (unit-tested) copies of the ownership helpers live in `ipc.ts`;
 * the few lines below are the inline copy this runtime requires.
 */

import electron = require('electron')

/**
 * Global names this preload writes. The restricted bridge keeps its own global
 * and the host-path lookup gets a second, one-method one (#826 / MM-05): the
 * capability this ticket adds stays auditable in isolation instead of being
 * folded into `iaDesktop`. Guarded by src/ipc.ts (`HOST_PATHS_GLOBAL`) and, on
 * the consuming side, by the web app reading the very same literal
 * (preload.test.ts / preload-host-paths.test.ts).
 */
const GLOBALS = {
  bridge: 'iaDesktop',
  hostPaths: '__IA_HOST_PATHS__',
} as const

/** Channel names; guarded to equal `DESKTOP_IPC` in src/ipc.ts by preload.test.ts. */
const CHANNELS = {
  bootstrap: 'ia-desktop:bootstrap',
  directoryPick: 'ia-desktop:directory-pick',
  windowState: 'ia-desktop:window-state',
} as const

/** Switch main appends to `additionalArguments`; guarded against `SERVICE_ORIGIN_SWITCH`. */
const SERVICE_ORIGIN_SWITCH = '--ia-service-origin='

/** Value of `--<name>=<value>` in this process's argv, or undefined. */
function argvValue(switchName: string): string | undefined {
  const argument = process.argv.find((value) => value.startsWith(switchName))
  if (argument === undefined) return undefined
  const value = argument.slice(switchName.length)
  return value.trim() === '' ? undefined : value
}

/**
 * Ownership is an exact origin match with the origin main created the window for,
 * plus "this is the top frame" — not a hardcoded scheme (W-21 D3). This inline
 * comparison is the only gate that runs today: the main-side re-check exists
 * (`assertDesktopSender` in ipc.ts, used by the directory-picker handler) but
 * that handler is not installed in main, and web/src does not consume this
 * bridge yet. Whoever wires the picker must install it, or the re-check stays
 * dead code and this line remains the whole boundary.
 */
const expectedOrigin = argvValue(SERVICE_ORIGIN_SWITCH)
const isOwnLocalPage = process.isMainFrame && expectedOrigin !== undefined
  && location.origin === expectedOrigin

/** The page-facing bridge (`window.iaDesktop`), defined structurally — no imports. */
interface IaDesktopBridge {
  readonly protocolVersion: 1
  bootstrap(): Promise<unknown>
  pickDirectory(): Promise<string | null>
  windowState(): Promise<unknown>
}

function createBridge(): IaDesktopBridge {
  return {
    protocolVersion: 1,
    bootstrap: () => electron.ipcRenderer.invoke(CHANNELS.bootstrap) as Promise<unknown>,
    pickDirectory: () => electron.ipcRenderer.invoke(CHANNELS.directoryPick) as Promise<string | null>,
    windowState: () => electron.ipcRenderer.invoke(CHANNELS.windowState) as Promise<unknown>,
  }
}

/** The narrow host-path lookup (`window.__IA_HOST_PATHS__`), defined structurally — no imports. */
interface HostPathsBridge {
  pathFor(file: File): string
}

/**
 * #826 (MM-05): the one capability this ticket adds to the renderer. A single
 * method delegating straight to Electron's `webUtils.getPathForFile`, which
 * answers a real path only for a `File` the user actually picked or dropped and
 * `''` for a JS-constructed one (clipboard bytes, a `File` built in page script
 * — no disk backend). It reads no file, lists no directory, and exposes neither
 * `webUtils` nor any filesystem module, so the renderer cannot turn it into
 * arbitrary reads (AC1/AC3).
 */
function createHostPathsBridge(): HostPathsBridge {
  return { pathFor: (file: File) => electron.webUtils.getPathForFile(file) }
}

if (isOwnLocalPage) {
  electron.contextBridge.exposeInMainWorld(GLOBALS.bridge, createBridge())
  electron.contextBridge.exposeInMainWorld(GLOBALS.hostPaths, createHostPathsBridge())
} else {
  // An unowned renderer (a stray frame or a foreign page) gets a version marker
  // only — not even the host-path lookup, so it has no path oracle at all.
  electron.contextBridge.exposeInMainWorld(GLOBALS.bridge, { protocolVersion: 1 })
}
