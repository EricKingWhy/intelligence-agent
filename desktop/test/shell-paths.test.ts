/**
 * W-21 D6 (#814): the main process is ESM, so window assets must come from the
 * module's own directory. The frozen build crashed before creating a window:
 *   ReferenceError: __dirname is not defined at main (file:///…/dist/src/main.js:90)
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { sep } from 'node:path'

import {
  resolvePreloadPath,
  resolveShellAssetsDir,
  resolveTrayIconPath,
} from '../src/shell-paths.ts'

const DEV_DIR = `${sep === '\\' ? 'C:\\repo\\desktop' : '/repo/desktop'}${sep}dist${sep}src`
const PACKAGED_DIR = `${sep === '\\' ? 'C:\\Program Files\\Intelligence Agent' : '/opt/ia'}${sep}resources${sep}app${sep}dist${sep}src`

describe('resolveShellAssetsDir', () => {
  it('accepts the module directory import.meta.dirname provides', () => {
    assert.equal(resolveShellAssetsDir(DEV_DIR), DEV_DIR)
  })

  it('fails closed when the module directory is unavailable', () => {
    assert.throws(() => resolveShellAssetsDir(undefined), /import\.meta\.dirname is unavailable/)
    assert.throws(() => resolveShellAssetsDir('  '), /import\.meta\.dirname is unavailable/)
  })
})

describe('window asset paths', () => {
  it('resolves the preload script next to the compiled main, dev and packaged', () => {
    assert.equal(resolvePreloadPath(DEV_DIR), `${DEV_DIR}${sep}preload.js`)
    assert.equal(resolvePreloadPath(PACKAGED_DIR), `${PACKAGED_DIR}${sep}preload.js`)
  })

  it('resolves the tray icon next to the compiled main, dev and packaged', () => {
    assert.equal(resolveTrayIconPath(DEV_DIR), `${DEV_DIR}${sep}tray-icon.ico`)
    assert.equal(resolveTrayIconPath(PACKAGED_DIR), `${PACKAGED_DIR}${sep}tray-icon.ico`)
  })

  it('never derives an asset path from the process cwd', () => {
    // The frozen defect produced <moduleDir> for the preload but the same
    // helper must not fall back to a cwd-relative path when the module moves.
    const fromOtherCwd = resolvePreloadPath(DEV_DIR)
    assert.ok(fromOtherCwd.startsWith(DEV_DIR))
    assert.ok(!fromOtherCwd.startsWith(process.cwd()))
    assert.ok(!fromOtherCwd.includes(`.${sep}.agent`))
  })
})
