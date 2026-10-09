/**
 * W-21 D3 (#815): the packaged app must carry the renderer build the shell
 * loads its window from. Without `resources/web/index.html` the window renders
 * nothing (the frozen defect); the assertion fails the build instead.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { assertBundledWebAssets } from '../scripts/build-windows-installer.mjs'

describe('assertBundledWebAssets', () => {
  it('accepts a resources dir that carries web/index.html', () => {
    const index = assertBundledWebAssets({
      resourcesDir: 'C:\\app\\resources',
      existsSync: (path) => path === 'C:\\app\\resources\\web\\index.html',
    })
    assert.equal(index, 'C:\\app\\resources\\web\\index.html')
  })

  it('fails the build when the renderer build was not shipped', () => {
    assert.throws(
      () => assertBundledWebAssets({ resourcesDir: 'C:\\app\\resources', existsSync: () => false }),
      /bundled renderer build missing: .*web.index\.html/,
    )
  })

  it('names the remediation in the error', () => {
    assert.throws(
      () => assertBundledWebAssets({ resourcesDir: 'C:\\app\\resources', existsSync: () => false }),
      /npm run build/,
    )
  })
})
