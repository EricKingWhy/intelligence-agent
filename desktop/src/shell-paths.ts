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

/**
 * Absolute path of the compiled sandboxed preload script.
 *
 * W-21 D3 (#815): the artifact is `preload.cjs`, compiled from `src/preload.cts`.
 * A sandboxed renderer parses its preload as CommonJS and provides no module
 * resolution (measured: an ESM preload fails with "Cannot use import statement
 * outside a module", a relative require with "module not found"), so the
 * extension is part of the contract rather than a style choice.
 */
export function resolvePreloadPath(moduleDir: string): string {
  return join(moduleDir, 'preload.cjs')
}

/** Absolute path of the multi-size tray icon (Windows picks the scale bitmap). */
export function resolveTrayIconPath(moduleDir: string): string {
  return join(moduleDir, 'tray-icon.ico')
}

/** Injectable platform surface for renderer asset resolution (unit-testable). */
export interface WebAssetsDeps {
  readonly packaged: boolean
  /** `process.resourcesPath` in the packaged app (the builder copies web/dist there). */
  readonly resourcesPath: string
  /** `app.getAppPath()`: the desktop package directory during development. */
  readonly appPath: string
  readonly existsSync: (path: string) => boolean
}

/**
 * Locate the built renderer UI (W-21 D3 / #815).
 *
 * Packaged: `<resources>/web/index.html` (installer `extraResources`, asserted in
 * afterPack). Development: `<repo>/web/dist/index.html` (the same directory the
 * service mounts when `WEB_DIST_DIR` is unset).
 *
 * @returns the directory that holds `index.html`, or undefined when neither
 *   candidate exists — the caller fails closed instead of loading a 404 page.
 */
export function resolveWebAssetsDir(deps: WebAssetsDeps): string | undefined {
  const candidates = deps.packaged
    ? [join(deps.resourcesPath, 'web')]
    : [join(deps.appPath, '..', 'web', 'dist')]
  return candidates.find((candidate) => deps.existsSync(join(candidate, 'index.html')))
}
