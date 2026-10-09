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
 *
 * #831 (W-21 D7) added the third rule: a `LangString` for a language the build
 * does not load is `warning 7025`, fatal under electron-builder — the W-16
 * smoke builds one language per run and aborted there.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  assertNsisIncludePlacement,
  assertNsisLangStringGuards,
} from '../scripts/build-windows-installer.mjs'

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

const BROKEN_UNGUARDED_LANGSTRINGS = [
  '!macro customHeader',
  '  LangString iaPerUserOnly ${LANG_ENGLISH} "english text"',
  '  LangString iaPerUserOnly ${LANG_SIMPCHINESE} "中文文案"',
  '!macroend',
].join('\n')

const GUARDED_LANGSTRINGS = [
  '!macro customHeader',
  '  !ifdef LANG_ENGLISH',
  '    LangString iaPerUserOnly ${LANG_ENGLISH} "english text"',
  '  !endif',
  '  !ifdef LANG_SIMPCHINESE',
  '    LangString iaPerUserOnly ${LANG_SIMPCHINESE} "中文文案"',
  '  !endif',
  '!macroend',
].join('\n')

describe('assertNsisLangStringGuards', () => {
  it('rejects a LangString whose language is not guarded', () => {
    assert.throws(
      () => assertNsisLangStringGuards(BROKEN_UNGUARDED_LANGSTRINGS),
      /line 3: LANG_SIMPCHINESE.*!ifdef LANG_<NAME>/s,
    )
  })

  it('rejects a LangString guarded by a different language', () => {
    const wrongGuard = [
      '!ifdef LANG_ENGLISH',
      '  LangString iaPerUserOnly ${LANG_SIMPCHINESE} "中文文案"',
      '!endif',
    ].join('\n')
    assert.throws(() => assertNsisLangStringGuards(wrongGuard), /LANG_SIMPCHINESE/)
  })

  it('does not accept a guard that a sibling !else opened up', () => {
    const elseBranch = [
      '!ifdef LANG_ENGLISH',
      '  ; english branch',
      '!else',
      '  LangString iaPerUserOnly ${LANG_SIMPCHINESE} "中文文案"',
      '!endif',
    ].join('\n')
    assert.throws(() => assertNsisLangStringGuards(elseBranch), /LANG_SIMPCHINESE/)
  })

  it('accepts per-language guards', () => {
    assert.doesNotThrow(() => assertNsisLangStringGuards(GUARDED_LANGSTRINGS))
  })

  it('ignores commented-out declarations', () => {
    const commented = ['; LangString iaPerUserOnly ${LANG_SIMPCHINESE} "中文文案"', GUARDED_LANGSTRINGS].join('\n')
    assert.doesNotThrow(() => assertNsisLangStringGuards(commented))
  })

  it('accepts the shipped installer.nsh', () => {
    const source = readFileSync(new URL('../installer/installer.nsh', import.meta.url), 'utf8')
    assert.doesNotThrow(() => assertNsisLangStringGuards(source))
  })
})
