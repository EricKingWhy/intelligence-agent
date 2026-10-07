/**
 * #816 (W-21 D1): the NSIS custom include is checked structurally on every
 * build, because both failure modes only appear in the real makensis run (which
 * needs Windows) and a "file exists" check cannot see either:
 *
 *   - `!include "${__FILEDIR__}…"` inside a macro body resolved against the
 *     stock template directory (the frozen D1 failure);
 *   - the same include without `!ifndef BUILD_UNINSTALLER` broke the uninstaller
 *     build with `warning 6010: install function "iaPromoteApplication" not
 *     referenced` (the second failure, after the first was fixed).
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { assertNsisIncludePlacement } from '../scripts/build-windows-installer.mjs'

const INCLUDED = 'installer-directories.nsh'

const BROKEN_MACRO_INCLUDE = [
  '!include "LogicLib.nsh"',
  '!macro customHeader',
  `  !include "\${__FILEDIR__}\\${INCLUDED}"`,
  '!macroend',
].join('\n')

const BROKEN_UNGUARDED_INCLUDE = [
  '!define IA_INSTALLER_DIR "${__FILEDIR__}"',
  '!macro customHeader',
  `  !include "\${IA_INSTALLER_DIR}\\${INCLUDED}"`,
  '!macroend',
].join('\n')

const FIXED = [
  '!include "LogicLib.nsh"',
  '!define IA_INSTALLER_DIR "${__FILEDIR__}"',
  '!macro customHeader',
  '  !ifndef BUILD_UNINSTALLER',
  `    !include "\${IA_INSTALLER_DIR}\\${INCLUDED}"`,
  '  !endif',
  '!macroend',
].join('\n')

describe('assertNsisIncludePlacement', () => {
  it('rejects the include that resolved against the template directory', () => {
    assert.throws(() => assertNsisIncludePlacement(BROKEN_MACRO_INCLUDE, INCLUDED), /top-level !define/)
  })

  it('rejects an installer include the uninstaller build would also pull in', () => {
    assert.throws(
      () => assertNsisIncludePlacement(BROKEN_UNGUARDED_INCLUDE, INCLUDED),
      /BUILD_UNINSTALLER/,
    )
  })

  it('accepts the fixed placement', () => {
    assert.doesNotThrow(() => assertNsisIncludePlacement(FIXED, INCLUDED))
  })

  it('rejects a file that never includes the directories script', () => {
    assert.throws(() => assertNsisIncludePlacement('!include "LogicLib.nsh"', INCLUDED), /does not include/)
  })

  it('ignores commented-out includes', () => {
    const commented = ['; !include "${__FILEDIR__}\\installer-directories.nsh"', FIXED].join('\n')
    assert.doesNotThrow(() => assertNsisIncludePlacement(commented, INCLUDED))
  })
})
