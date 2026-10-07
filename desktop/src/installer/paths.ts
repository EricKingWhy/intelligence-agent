// #361 [W-16] Windows installer path resolution.
//
// Pure functions with injectable platform values so they are unit-testable
// on any OS. Windows paths are joined with backslashes by hand (no
// node:path): the values are consumed by the NSIS installer and by
// app.setPath('userData', ...) on win32 only.
// Exception: resolveDesktopDataRoot feeds a child-process environment variable,
// so it uses node:path and native separators on every platform.

import { join } from 'node:path'

/** Stable application id: NSIS GUID seed, uninstall registry key, mutex names. */
export function installerAppId(): string {
  return 'com.intelligence-agent.desktop'
}

/** Display name used for shortcuts, install dir and window titles. */
export function installerProductName(): string {
  return 'Intelligence Agent'
}

/**
 * User data home for the installed app: `%APPDATA%\intelligence-agent`.
 * Always outside the install directory (#361: uninstall never deletes it).
 */
export function resolveUserDataDir(appData: string | undefined): string {
  if (appData === undefined || appData === '') {
    throw new Error('resolveUserDataDir: APPDATA is not set')
  }
  return `${appData}\\intelligence-agent`
}

/**
 * Default per-user install location: `%LOCALAPPDATA%\Programs\<product>`.
 * Per-user install needs no admin rights (matches DSH / OpenHands / VS Code).
 */
export function defaultInstallDir(
  localAppData: string | undefined,
  productName: string,
): string {
  if (localAppData === undefined || localAppData === '') {
    throw new Error('defaultInstallDir: LOCALAPPDATA is not set')
  }
  return `${localAppData}\\Programs\\${productName}`
}

/** HKCU uninstall entry key (per-user install ⇒ current-user hive). */
export function uninstallRegistryKey(appId: string): string {
  return `Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${appId}`
}

/** Injectable platform surface for python resolution (unit-testable). */
export interface PythonPathDeps {
  readonly platform: string
  readonly execPath: string
  readonly resourcesPath: string
  readonly existsSync: (path: string) => boolean
}

/**
 * Locate the Python interpreter for the backend child.
 *
 * Packaged app (#361): the installer bundles the locked runtime at
 * `<resources>/python/python.exe`; use it when present so the installed app
 * never needs a system Python. Otherwise keep the historical dev heuristic
 * (sibling of the electron binary).
 */
export function resolvePythonPath(deps: PythonPathDeps): string {
  if (deps.platform === 'win32') {
    const bundled = `${deps.resourcesPath}\\python\\python.exe`
    if (deps.existsSync(bundled)) return bundled
  }
  return deps.execPath.replace(/electron(.exe)?$/i, 'python')
}

/**
 * Data root the shell gives the Python service (W-21 defect D4 / #813).
 *
 * `agent-harness serve` publishes the endpoint state file, the instance lock and
 * `harness.db` under `Settings.workspace_dir`; the shell passes this directory to
 * the child as an absolute `WORKSPACE_DIR` and reads the endpoint file from the
 * same absolute path. It is deliberately not `process.cwd()`: in the packaged app
 * that is the install directory, which an update replaces, and the child's cwd is
 * not necessarily the shell's (PI-Desktop `data-paths.ts`: "the result is absolute,
 * because it reaches host-core as a child-process environment variable from a
 * working directory that need not be this one").
 *
 * @param userDataDir - Electron `app.getPath('userData')` (already absolute).
 * @returns an absolute directory under user data, outside the install dir.
 */
export function resolveDesktopDataRoot(userDataDir: string): string {
  if (userDataDir.trim() === '') {
    throw new Error('resolveDesktopDataRoot: userData dir is empty')
  }
  return join(userDataDir, 'workspace')
}
