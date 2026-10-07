/**
 * Assets the Electron main process loads from its own directory (W-21 D6 / #814).
 *
 * The main process is ESM (`package.json` `"type": "module"`, tsconfig
 * `module: NodeNext`), where the CommonJS directory global does not exist; the
 * frozen build crashed with a ReferenceError before it could create a window.
 * Paths are therefore derived from the module's own location
 * (`import.meta.dirname`) through these pure helpers, never from `process.cwd()`
 * (PI-Desktop: `apps/desktop/electron/main/module-path.ts`, commit 1e07bad3).
 *
 * Packaged layout (`asar: false`, the builder ships all of dist): both the
 * compiled main and its assets sit in `<install>/resources/app/dist/src/`, so
 * one resolution covers development and packaging.
 */

import { join } from 'node:path'

/**
 * Validate the module directory `import.meta.dirname` provides.
 *
 * Fail-closed with an actionable message: an undefined value means the file was
 * not loaded as ESM (or Node < 20.11), where asset paths cannot be derived.
 *
 * @param moduleDir - `import.meta.dirname` of the calling module.
 * @returns the same directory.
 */
export function resolveShellAssetsDir(moduleDir: string | undefined): string {
  if (moduleDir === undefined || moduleDir.trim() === '') {
    throw new Error(
      'shell assets: import.meta.dirname is unavailable — the main process must run as ESM on Node >= 20.11',
    )
  }
  return moduleDir
}

/** Absolute path of the compiled sandboxed preload script. */
export function resolvePreloadPath(moduleDir: string): string {
  return join(moduleDir, 'preload.js')
}

/** Absolute path of the multi-size tray icon (Windows picks the scale bitmap). */
export function resolveTrayIconPath(moduleDir: string): string {
  return join(moduleDir, 'tray-icon.ico')
}
