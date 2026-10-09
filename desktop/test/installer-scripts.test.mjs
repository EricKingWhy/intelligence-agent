/**
 * #361 [W-16]: installer script unit tests (run with node --test; these are
 * .mjs so tsc ignores them — tsconfig only includes test/**.ts).
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, mkdirSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  createWindowsInstallerConfig,
  validateRuntimeLockfile,
  validateLangStringGuards,
  validateInstallerScripts,
  validateLongPathPrefixes,
  validateBackupDeleteGuards,
  malformedLongPathPrefixes,
  unguardedBackupDelete,
  stripNsisComments,
  unguardedLangStrings,
  sha256File,
  GOOD_LOCK,
} from '../scripts/build-windows-installer.mjs'
import {
  collectDataTargets,
  runCleanUserData,
} from '../scripts/clean-user-data.mjs'

describe('validateRuntimeLockfile', () => {
  it('accepts the bundled example lock', () => {
    validateRuntimeLockfile(GOOD_LOCK)
  })

  it('rejects a missing python pin', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_LOCK))
    delete bad.python
    assert.throws(() => validateRuntimeLockfile(bad), /python/)
  })

  it('rejects a malformed sha256', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_LOCK))
    bad.python.sha256 = 'not-a-hash'
    assert.throws(() => validateRuntimeLockfile(bad), /sha256/)
  })

  it('rejects a python version below the backend minimum (3.11)', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_LOCK))
    bad.python.version = '3.10.12'
    assert.throws(() => validateRuntimeLockfile(bad), /3\.11/)
  })

  it('rejects a non-https wheel url', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_LOCK))
    bad.wheels[0].url = 'http://example.com/x.whl'
    assert.throws(() => validateRuntimeLockfile(bad), /https/)
  })

  it('rejects a wheel filename that does not match name+version', () => {
    const bad = JSON.parse(JSON.stringify(GOOD_LOCK))
    bad.wheels[0].filename = 'something-else-1.0-py3-none-any.whl'
    assert.throws(() => validateRuntimeLockfile(bad), /filename/)
    const bad2 = JSON.parse(JSON.stringify(GOOD_LOCK))
    bad2.wheels[0].filename = 'pip-99.99.99-py3-none-any.whl'
    assert.throws(() => validateRuntimeLockfile(bad2), /version/)
  })

  it('accepts PEP 503 equivalent names (jaraco.classes == jaraco-classes)', () => {
    const lock = JSON.parse(JSON.stringify(GOOD_LOCK))
    lock.wheels.push({
      name: 'jaraco-classes',
      version: '3.4.0',
      filename: 'jaraco.classes-3.4.0-py3-none-any.whl',
      url: 'https://example.com/jaraco.classes-3.4.0-py3-none-any.whl',
      sha256: '0'.repeat(64),
    })
    validateRuntimeLockfile(lock)
  })
})

describe('createWindowsInstallerConfig', () => {
  const config = createWindowsInstallerConfig({
    version: '0.1.0',
    appId: 'com.intelligence-agent.desktop',
    productName: 'Intelligence Agent',
    installerDir: '/tmp/fake-installer',
  })

  it('targets win32 x64 nsis only', () => {
    assert.deepEqual(config.win.target, [{ target: 'nsis', arch: ['x64'] }])
  })

  it('is per-user (no admin), non-one-click, dir changeable', () => {
    assert.equal(config.nsis.perMachine, false)
    assert.equal(config.nsis.oneClick, false)
    assert.equal(config.nsis.allowToChangeInstallationDirectory, true)
  })

  it('never deletes app data on uninstall', () => {
    assert.equal(config.nsis.deleteAppDataOnUninstall, false)
  })

  it('wires the custom NSIS include with atomic rollback', () => {
    assert.equal(config.nsis.include, '/tmp/fake-installer/installer.nsh')
  })

  it('keeps asar off so spawned files stay real on disk', () => {
    assert.equal(config.asar, false)
  })

  it('bundles the staged python runtime as extraResources', () => {
    const python = config.extraResources.find((r) => r.to === 'python/')
    assert.ok(python)
    assert.equal(python.from, '/tmp/fake-installer/staging/python/')
  })

  it('ships a space-free artifact name with version macro', () => {
    assert.match(config.nsis.artifactName, /^Intelligence-Agent-Setup-\$\{version\}\.\$\{ext\}$/)
  })

  it('is bilingual (en_US + zh_CN)', () => {
    assert.deepEqual(config.nsis.installerLanguages, ['en_US', 'zh_CN'])
  })
})

describe('unguardedLangStrings / validateLangStringGuards (#831)', () => {
  it('accepts a LangString inside a matching !ifdef LANG_<NAME> guard', () => {
    const src = [
      '!ifdef LANG_ENGLISH',
      'LangString iaFoo ${LANG_ENGLISH} "en"',
      '!endif',
      '!ifdef LANG_SIMPCHINESE',
      'LangString iaFoo ${LANG_SIMPCHINESE} "zh"',
      '!endif',
    ].join('\n')
    assert.deepEqual(unguardedLangStrings(src), [])
    validateLangStringGuards(src, 'installer.nsh')
  })

  it('flags a LangString with no guard at all', () => {
    const src = 'LangString iaFoo ${LANG_SIMPCHINESE} "zh"'
    assert.deepEqual(unguardedLangStrings(src), [{ line: 1, symbol: 'SIMPCHINESE' }])
    assert.throws(() => validateLangStringGuards(src, 'installer.nsh'), /not guarded/)
  })

  it('does not treat a mismatched guard as covering another language', () => {
    // English loaded, but the Chinese string is only inside the ENGLISH guard.
    const src = [
      '!ifdef LANG_ENGLISH',
      'LangString iaFoo ${LANG_SIMPCHINESE} "zh"',
      '!endif',
    ].join('\n')
    assert.deepEqual(unguardedLangStrings(src), [{ line: 2, symbol: 'SIMPCHINESE' }])
  })

  it('is fail-closed: an !ifndef or !else branch does not count as a guard', () => {
    const ifndef = ['!ifndef LANG_SIMPCHINESE', 'LangString iaFoo ${LANG_SIMPCHINESE} "zh"', '!endif'].join('\n')
    assert.deepEqual(unguardedLangStrings(ifndef), [{ line: 2, symbol: 'SIMPCHINESE' }])

    const elseBranch = [
      '!ifdef LANG_ENGLISH',
      'LangString iaFoo ${LANG_ENGLISH} "en"',
      '!else',
      'LangString iaFoo ${LANG_SIMPCHINESE} "zh"',
      '!endif',
    ].join('\n')
    assert.deepEqual(unguardedLangStrings(elseBranch), [{ line: 4, symbol: 'SIMPCHINESE' }])
  })

  it('accepts a LangString nested under an outer matching guard', () => {
    const src = [
      '!ifdef LANG_ENGLISH',
      '!ifdef SOME_OTHER_FLAG',
      'LangString iaFoo ${LANG_ENGLISH} "en"',
      '!endif',
      '!endif',
    ].join('\n')
    assert.deepEqual(unguardedLangStrings(src), [])
  })

  it('ignores a numeric language id (never a ${LANG_*} symbol)', () => {
    assert.deepEqual(unguardedLangStrings('LangString iaFoo 1033 "en"'), [])
  })
  it('names the offending file in the error message', () => {
    assert.throws(
      () => validateLangStringGuards('LangString iaFoo ${LANG_ENGLISH} "en"', 'other.nsh'),
      /^Error: other\.nsh: LangString not guarded/,
    )
  })
})

describe('installer.nsh LangString guards (#831)', () => {
  const installerDir = fileURLToPath(new URL('../installer', import.meta.url))
  const installerNsh = readFileSync(join(installerDir, 'installer.nsh'), 'utf8')

  it('every LangString ${LANG_<NAME>} is wrapped in a matching !ifdef guard', () => {
    assert.deepEqual(unguardedLangStrings(installerNsh), [])
    validateLangStringGuards(installerNsh, 'installer.nsh')
  })

  it('validateInstallerScripts passes on the real installer directory', () => {
    validateInstallerScripts(installerDir)
  })

  it('validateInstallerScripts inspects installer-directories.nsh too', () => {
    const dir = mkdtempSync(join(tmpdir(), 'ia-nsh-'))
    writeFileSync(join(dir, 'installer.nsh'), '!ifdef LANG_ENGLISH\nLangString a ${LANG_ENGLISH} "x"\n!endif\n')
    writeFileSync(join(dir, 'installer-directories.nsh'), 'LangString b ${LANG_SIMPCHINESE} "y"\n')
    assert.throws(() => validateInstallerScripts(dir), /installer-directories\.nsh/)
  })

  it('still declares both English and Chinese strings (no text regression)', () => {
    for (const name of ['iaPerUserOnly', 'iaAppRunning', 'iaUpdateFailed', 'iaRollbackFailed', 'iaStaleBackup']) {
      assert.match(installerNsh, new RegExp(`LangString ${name} \\$\\{LANG_ENGLISH\\}`))
      assert.match(installerNsh, new RegExp(`LangString ${name} \\$\\{LANG_SIMPCHINESE\\}`))
    }
  })
})

describe('long-path prefix and backup-delete guards (#901 / R1)', () => {
  const installerDir = fileURLToPath(new URL('../installer', import.meta.url))
  // The pre-#901 shape: "\\?" with no separator backslash. NSIS strings have no
  // backslash escapes, so this builds "\\?C:\..." — a path Win32 cannot resolve.
  const BROKEN_DELETE = 'RMDir /r "\\\\?$iaBackupDirectory"'
  const FIXED_DELETE = 'RMDir /r "\\\\?\\$iaBackupDirectory"'

  // Lines of the promote body only: a decoy copy of the same shape elsewhere in
  // the file (say in the rollback function) must not satisfy an assertion about
  // what promote does, so the shape checks are anchored to this range.
  function promoteApplicationBody(source) {
    const lines = stripNsisComments(source).split(/\r?\n/)
    const start = lines.findIndex((line) => /^\s*Function\s+iaPromoteApplication\b/.test(line))
    assert.notEqual(start, -1)
    const end = lines.findIndex((line, index) => index > start && /^\s*FunctionEnd\b/.test(line))
    assert.ok(end > start)
    return lines.slice(start, end)
  }

  it('strips `;` and `#` comments but not either inside a string', () => {
    assert.equal(stripNsisComments('RMDir /r "a;b" ; trailing'), 'RMDir /r "a;b" ')
    assert.equal(stripNsisComments('; whole line'), '')
    assert.equal(stripNsisComments('x $" ; still in string'), 'x $" ; still in string')
    // NSIS also takes `#` comments — at the start of a line or after a complete
    // statement (measured on makensis 3.0.4.1: `DetailPrint "x" # trailing`
    // compiles while `DetailPrint "x" stray` fails with "Error in script", so
    // the `#` starts a comment rather than being ignored) — but a `#` inside a
    // string is text, and one glued to a token is left alone so a stray `#` can
    // only ever over-report, never eat code.
    assert.equal(stripNsisComments('# whole line'), '')
    assert.equal(stripNsisComments('  # indented'), '  ')
    assert.equal(stripNsisComments('DetailPrint "x" # trailing'), 'DetailPrint "x" ')
    assert.equal(stripNsisComments('DetailPrint "a # b"'), 'DetailPrint "a # b"')
    assert.equal(stripNsisComments('StrCpy $0 "a"#$1'), 'StrCpy $0 "a"#$1')
  })

  it('keeps a `;` that follows an escaped quote inside a string', () => {
    // Measured on makensis 3.0.4.1: `$\"` stores a literal quote and does not
    // end the string, so the `;` after it is still string content.
    assert.equal(stripNsisComments('x "a$\\" ; keep'), 'x "a$\\" ; keep')
    // A bare `$"` is not an escape — that quote closes the string, and the `;`
    // after it starts a comment ("WriteRegStr expects 4 parameters, got 5").
    assert.equal(stripNsisComments('x "a$" ; gone'), 'x "a$" ')
  })

  it('flags the pre-#901 prefix and accepts the corrected one', () => {
    assert.deepEqual(malformedLongPathPrefixes(FIXED_DELETE), [])
    assert.deepEqual(malformedLongPathPrefixes(BROKEN_DELETE), [{ line: 1, text: BROKEN_DELETE }])
    assert.throws(() => validateLongPathPrefixes(BROKEN_DELETE, 'installer-directories.nsh'), /long-path prefix/)
  })

  it('does not flag the same text inside a comment', () => {
    assert.deepEqual(malformedLongPathPrefixes(`; upstream: ${BROKEN_DELETE}`), [])
  })

  it('requires ClearErrors, one ${Errors} read and the leftover record around the delete', () => {
    assert.deepEqual(unguardedBackupDelete([FIXED_DELETE, '!insertmacro iaClearBackupDir'].join('\n')), [
      { line: 1, what: 'missing ClearErrors before the delete' },
      { line: 1, what: 'missing ${Errors} check before iaClearBackupDir' },
      { line: 1, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
    const guarded = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(guarded), [])
  })

  it('finds ClearErrors across blank lines, which do not consume the window', () => {
    // Blank lines are layout, not statements: letting them shorten the window
    // rejected a correct layout with three blank lines above the delete.
    const spaced = [
      'ClearErrors',
      '',
      '',
      '',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(spaced), [])
    // The window is still three statements: a clear call four statements up no
    // longer covers this delete.
    const farAbove = [
      'ClearErrors',
      'StrCpy $0 "1"',
      'StrCpy $1 "1"',
      'StrCpy $2 "1"',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(farAbove), [
      { line: 5, what: 'missing ClearErrors before the delete' },
    ])
  })

  it('flags a second ${Errors} read: it cannot see the failed delete', () => {
    // Measured on NSIS 3.0.4.1: LogicLib's ${Errors} is IfErrors, which consumes
    // the error flag — the first read after a failing delete reports it, an
    // immediate second read reports none. A marker read ahead of the outcome
    // test would silently disable the leftover record.
    const twoReads = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  StrCpy $4 "1"',
      '${EndIf}',
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(twoReads), [
      {
        line: 2,
        what: '2 ${Errors} reads between the delete and iaClearBackupDir — only the first can see the failed delete',
      },
    ])
  })

  it('does not count a `${Errors}` that only appears in a comment', () => {
    // The read count is what makes the failure branch reachable, so a comment
    // mentioning the macro must not inflate it — and `#` is a comment too, not
    // just `;` (a `;`-only stripper reported "2 reads" for this layout).
    const commented = [
      'ClearErrors',
      FIXED_DELETE,
      '# ${Errors} is sticky, so read it exactly once',
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '  # trailing note about ${Errors}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(commented), [])
  })

  it('flags a failure that is only logged, not recorded', () => {
    const loggedOnly = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  DetailPrint "could not remove $iaBackupDirectory"',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(loggedOnly), [
      { line: 2, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
  })

  it('tests Errors before clearing the pointer, even across comment lines', () => {
    const withoutTest = [
      'ClearErrors',
      FIXED_DELETE,
      '; the backup is gone now',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(withoutTest), [
      { line: 2, what: 'missing ${Errors} check before iaClearBackupDir' },
      { line: 2, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
  })

  it('flags a delete whose iaClearBackupDir call is gone (the window cannot run on)', () => {
    const noClear = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(noClear), [
      { line: 2, what: 'no !insertmacro iaClearBackupDir after the delete' },
    ])
  })

  it('does not let another function satisfy the check (fail-closed window)', () => {
    const acrossFunctions = [
      'Function iaPromoteApplication',
      '  ClearErrors',
      `  ${FIXED_DELETE}`,
      'FunctionEnd',
      'Function other',
      '  ${If} ${Errors}',
      '  ${EndIf}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(acrossFunctions), [
      { line: 3, what: 'no !insertmacro iaClearBackupDir after the delete' },
      { line: 3, what: 'missing ${Errors} check before iaClearBackupDir' },
      { line: 3, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
  })

  it('flags a ClearErrors between the delete and the read', () => {
    const interposed = [
      'ClearErrors',
      FIXED_DELETE,
      'ClearErrors',
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(interposed), [
      { line: 2, what: 'ClearErrors between the delete and the ${Errors} read discards the failed delete' },
    ])
  })

  it('does not let another macro satisfy the check (fail-closed window)', () => {
    const acrossMacros = [
      '!macro promote',
      '  ClearErrors',
      `  ${FIXED_DELETE}`,
      '!macroend',
      'Function other',
      '  ${If} ${Errors}',
      '  ${EndIf}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(acrossMacros), [
      { line: 3, what: 'no !insertmacro iaClearBackupDir after the delete' },
      { line: 3, what: 'missing ${Errors} check before iaClearBackupDir' },
      { line: 3, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
  })

  it('is fail-closed: no delete site at all is a problem', () => {
    assert.deepEqual(unguardedBackupDelete('Function nothing\nFunctionEnd'), [
      { line: 0, what: 'no long-path RMDir of $iaBackupDirectory found' },
    ])
  })

  it('flags text that satisfies the checks but can be compiled out', () => {
    // Every check below is textual, so text that never compiles would pass it.
    // Verified against this guard before the rule existed: wrapping the whole
    // guarded block in `!ifdef NOPE` (or `!if 0`) returned [] — a build-time
    // guard a dead branch can satisfy is not a guard.
    const deadBranch = [
      'ClearErrors',
      '!ifdef NOPE',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!endif',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(deadBranch), [
      {
        line: 3,
        what: 'the delete sits inside 1 open conditional compilation directive(s) — the delete and its checks can be compiled out',
      },
    ])
    // The wrapping form is the common one, and the one a window-scoped rule
    // missed: `!ifdef` above the delete, `!endif` after the clear. Every line
    // the window checks is present, and none of them compiles without NOPE.
    const wrapped = [
      'Function iaPromoteApplication',
      '  ClearErrors',
      '  !ifdef NOPE',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      '  !endif',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(wrapped), [
      {
        line: 4,
        what: 'the delete sits inside 1 open conditional compilation directive(s) — the delete and its checks can be compiled out',
      },
    ])
    // Directives are case-insensitive: makensis 3.0.4.1 skips a block wrapped in
    // `!IFDEF NOPE` … `!ENDIF` (probe: `!ifdef`/`!endif` and their upper-case
    // spellings both compile), so the scan cannot be case-sensitive either.
    const cased = [
      'Function iaPromoteApplication',
      '  ClearErrors',
      '  !IFDEF NOPE',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      '  !ENDIF',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(cased), [
      {
        line: 4,
        what: 'the delete sits inside 1 open conditional compilation directive(s) — the delete and its checks can be compiled out',
      },
    ])
    // The block-bounding directives are case-insensitive too (probe: `!MACRO`,
    // `!MACROEND` and `!INSERTMACRO` all compile on 3.0.4.1), so a scan that did
    // not recognise `!MACRO` would start the block at line 1 and read the
    // `${If} 1 == 1` above the macro as a branch above the delete — depth the
    // delete does not have.
    const casedBlock = [
      '${If} 1 == 1',
      '!MACRO iaPromoteApplication',
      '  ClearErrors',
      '  ${If} ${FileExists} "$INSTDIR\\app.exe"',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      '  ${EndIf}',
      '!MACROEND',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(casedBlock), [])
    // Fail-closed on the wrapper that a block-scoped scan cannot see: a
    // conditional opened above the enclosing Function and closed after its
    // `FunctionEnd` is indistinguishable from a branch that drops the block —
    // the same idiom the shipped file uses for `.onGUIEnd`. An intentional
    // wrapper around the guarded block therefore has to be recorded here.
    // The second copy of the same body sits outside that wrapper and stays
    // clean, so the rule keys on the directive actually being open — not on the
    // file merely mentioning one.
    const outerWrap = [
      '!ifndef BUILD_UNINSTALLER',
      'Function iaPromoteApplication',
      '  ClearErrors',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
      '!endif',
      'Function iaPromoteApplication',
      '  ClearErrors',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(outerWrap), [
      {
        line: 4,
        what: 'the delete sits inside 1 open conditional compilation directive(s) — the delete and its checks can be compiled out',
      },
    ])
  })

  it('flags a branch above the delete that can skip the whole guarded block', () => {
    // Absolute depth, not relative: wrapping delete, record and clear together
    // in one never-taken `${If} 1 == 0` inside the promote guard leaves the
    // delete exactly as deep as the clear and as the probes above it, so every
    // relative check passes while nothing runs (verified against this guard).
    const wrappedAll = [
      'Function iaPromoteApplication',
      '  ${If} ${FileExists} "$INSTDIR\\app.exe"',
      '    ClearErrors',
      '    ${If} 1 == 0',
      `    ${FIXED_DELETE}`,
      '      ${If} ${Errors}',
      '        WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '      ${EndIf}',
      '      !insertmacro iaClearBackupDir',
      '    ${EndIf}',
      '  ${EndIf}',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(wrappedAll), [
      {
        line: 5,
        what: 'the delete sits 2 LogicLib level(s) inside the block — a branch above it can skip the delete and its checks',
      },
    ])
    // Control: the promote guard itself is one level, and nothing is reported.
    const promoteGuardOnly = [
      'Function iaPromoteApplication',
      '  ${If} ${FileExists} "$INSTDIR\\app.exe"',
      '    ClearErrors',
      `    ${FIXED_DELETE}`,
      '    ${If} ${Errors}',
      '      WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '    ${EndIf}',
      '    !insertmacro iaClearBackupDir',
      '  ${EndIf}',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(promoteGuardOnly), [])
  })

  it('accepts every LogicLib closer documented for the openers it counts', () => {
    // `${Do} … ${LoopUntil}` / `${LoopWhile}` and `${Unless} … ${EndUnless}` are
    // documented LogicLib pairs (verified with makensis 3.0.4.1: all three
    // compile). Counting only `${Loop}`/`${EndIf}` read three correctly closed
    // layouts as nesting that is not there.
    for (const [open, close] of [
      ['${Do}', '${LoopUntil} $0 == "x"'],
      ['${DoWhile} $0 == ""', '${LoopWhile} $0 == ""'],
      ['${Unless} $0 == ""', '${EndUnless}'],
    ]) {
      const layout = [
        'ClearErrors',
        FIXED_DELETE,
        `${open}`,
        '  StrCpy $1 "n"',
        `${close}`,
        '${If} ${Errors}',
        '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
        '${EndIf}',
        '!insertmacro iaClearBackupDir',
      ].join('\n')
      assert.deepEqual(unguardedBackupDelete(layout), [], `${open} … ${close} reported`)
    }
  })

  it('flags a clear call nested deeper than the delete', () => {
    // The other verified bypass: keep the clear inside the failure branch, so a
    // path that does not take that branch never clears the pointer — a finished
    // install then still reads as incomplete and .onGUIEnd rolls it back.
    const nested = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  !insertmacro iaClearBackupDir',
      '${EndIf}',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(nested), [
      {
        line: 2,
        what: 'iaClearBackupDir sits 1 LogicLib level(s) deeper than the delete — a path skips the clear',
      },
    ])
    const balanced = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(balanced), [])
    // A line that opens and closes a block counts as neither: counting only the
    // opener made this balanced layout read as nesting.
    const oneLine = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  ${If} 1 == 0 ${EndIf}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(oneLine), [])
    // The loop and switch forms nest as much as `${If}` does, so a clear inside
    // one of them is just as skippable.
    const switchNested = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '${Switch} $0',
      '  ${Case} 1',
      '    !insertmacro iaClearBackupDir',
      '${EndSwitch}',
    ].join('\n')
    const forEachNested = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '${ForEach} $0 in $1',
      '  !insertmacro iaClearBackupDir',
      '${Next}',
    ].join('\n')
    for (const fixture of [switchNested, forEachNested]) {
      assert.deepEqual(unguardedBackupDelete(fixture), [
        {
          line: 2,
          what: 'iaClearBackupDir sits 1 LogicLib level(s) deeper than the delete — a path skips the clear',
        },
      ])
    }
  })

  it('flags a record that names a different directory', () => {
    const wrongValue = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $INSTDIR',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(wrongValue), [
      { line: 2, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
    // A value built from the backup path names a directory nothing will find.
    const suffixed = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory-tmp',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(suffixed), [
      { line: 2, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
    // Where the record lands matters as much as its value: the reader opens
    // HKCU under this application's own key, so a record written to another
    // hive or key is a silent orphan — the R1 symptom — and the guard has to
    // report it rather than accept any `IaLeftoverDir` write.
    const elsewhere = [
      '  WriteRegStr HKLM "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  WriteRegStr HKCU "Software" "IaLeftoverDir" $iaBackupDirectory',
      '  WriteRegStr HKCU "\\${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
    ]
    for (const record of elsewhere) {
      const layout = [
        'ClearErrors',
        FIXED_DELETE,
        '${If} ${Errors}',
        record,
        '${EndIf}',
        '!insertmacro iaClearBackupDir',
      ].join('\n')
      assert.deepEqual(
        unguardedBackupDelete(layout),
        [{ line: 2, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' }],
        record,
      )
    }
  })

  it('does not let a ClearErrors outside the delete block satisfy the check', () => {
    // The flag is sticky across the whole installer, so a clear in the previous
    // function reads as "this delete preceded by a clear" while the delete's own
    // path has none.
    const previousFunction = [
      'Function other',
      '  ClearErrors',
      'FunctionEnd',
      'Function iaPromoteApplication',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    const uninsertedMacro = [
      '!macro helper',
      '  ClearErrors',
      '!macroend',
      'Function iaPromoteApplication',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    for (const layout of [previousFunction, uninsertedMacro]) {
      assert.deepEqual(unguardedBackupDelete(layout), [
        { line: 5, what: 'missing ClearErrors before the delete' },
      ])
    }
  })

  it('recognizes a re-cased delete instead of reporting it as missing', () => {
    // NSIS directives are case-insensitive, so `rmdir /r` is the same delete:
    // reporting it as "no delete found" would blame a correct layout.
    const lower = [
      'ClearErrors',
      'rmdir /r "\\\\?\\$iaBackupDirectory"',
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(lower), [])
  })

  it('validateBackupDeleteGuards names the file', () => {
    assert.throws(
      () => validateBackupDeleteGuards('Function nothing\nFunctionEnd', 'installer-directories.nsh'),
      /^Error: installer-directories\.nsh: unguarded backup delete/,
    )
  })

  it('the shipped scripts carry the corrected, guarded delete', () => {
    const source = readFileSync(join(installerDir, 'installer-directories.nsh'), 'utf8')
    assert.deepEqual(malformedLongPathPrefixes(source), [])
    assert.deepEqual(unguardedBackupDelete(source), [])
  })

  it('keeps the leftover record exactly while the recorded directory exists', () => {
    // Existence-only regexes let a branch swap pass (keep when the directory
    // exists, drop when it is gone), and searching the whole file lets a decoy
    // copy satisfy them, so this pins the shape inside the promote body only:
    // read the record, probe it with BOTH forms — measured on NSIS 3.0.4.1, an
    // unprefixed >MAX_PATH path answers "false" while the prefixed form is the
    // one that cannot express a UNC path — and drop it as the first statement of
    // the innermost negative branch, where none of the probes can see the
    // directory any more.
    const body = promoteApplicationBody(
      readFileSync(join(installerDir, 'installer-directories.nsh'), 'utf8'),
    )
    const lines = body.map((line) => line.trim()).filter((line) => line !== '')
    const del = lines.findIndex((line) => /^RMDir\s+\/r\b/.test(line))
    const read = lines.findIndex((line) => /^!insertmacro\s+iaReadLeftoverDir\b/.test(line))
    const plain = lines.findIndex((line) => /^\$\{IfNot\}\s+\$\{FileExists\}\s+"\$iaLeftoverDirectory"$/.test(line))
    const prefixed = lines.findIndex((line) =>
      /^\$\{IfNot\}\s+\$\{FileExists\}\s+"\\\\\?\\\$iaLeftoverDirectory"$/.test(line),
    )
    const drop = lines.findIndex((line) => /^DeleteRegValue\b[^\n]*"IaLeftoverDir"/.test(line))
    const clear = lines.findIndex((line) => /!insertmacro\s+iaClearBackupDir\b/.test(line))
    for (const [name, index] of [
      ['the delete', del],
      ['the record read', read],
      ['the plain probe', plain],
      ['the prefixed probe', prefixed],
      ['the record drop', drop],
      ['the clear call', clear],
    ]) {
      assert.notEqual(index, -1, `${name} is missing from the promote body`)
    }
    // Record handling comes after the delete, and each step after the one it
    // depends on: same-shaped statements placed earlier in the body (in an
    // unreachable `${If} 1 == 0` block, say) satisfy every index check above
    // while the real site is gone — this order is what makes them unusable.
    assert.ok(
      del < read && read < plain && plain < prefixed && prefixed < drop && drop < clear,
      `promote body out of order: ${JSON.stringify({ del, read, plain, prefixed, drop, clear })}`,
    )
    // And the record handling has to run on the delete's own path: a
    // same-shaped block parked inside an extra never-taken branch (`${If} 1 == 0`)
    // satisfies every assertion above while the record handling never runs.
    // Depth is counted with the same block macros the build guard counts, so the
    // two notions of nesting agree.
    const opensBlock = /\$\{(If|IfNot|Unless|While|Do|DoWhile|DoUntil|For|ForEach|Select|Switch)\}/g
    const closesBlock = /\$\{(EndIf|EndWhile|Loop|Next|EndSelect|EndSwitch)\}/g
    const depthAt = (index) => {
      let depth = 0
      for (const line of lines.slice(0, index)) {
        depth += (line.match(opensBlock) ?? []).length
        depth -= (line.match(closesBlock) ?? []).length
      }
      return depth
    }
    assert.equal(depthAt(read), depthAt(del), 'the record read is not on the delete path')
    assert.equal(depthAt(clear), depthAt(del), 'the clear call is not on the delete path')
    // Absolute, not only relative: a wrapper around the whole guarded body
    // moves delete, probes, record and clear down together, so every
    // comparison above still holds while nothing runs. The delete's own depth
    // is what pins it to the promote path — one branch, the "new files are in
    // place" guard — and the build guard reports more than that.
    assert.equal(depthAt(del), 1, 'the delete is not one branch deep inside the promote body')
    assert.equal(depthAt(read), 1, 'the record read is not one branch deep inside the promote body')
    assert.deepEqual(
      [depthAt(plain), depthAt(prefixed), depthAt(drop)],
      [depthAt(del) + 1, depthAt(del) + 2, depthAt(del) + 3],
      'the record is not dropped inside both negative probes',
    )
    // Drop as the first statement after both probes, and exactly once: a second
    // drop site would be a second guess about the same record.
    const drops = lines.filter((line) => /^DeleteRegValue\b[^\n]*"IaLeftoverDir"/.test(line)).length
    assert.equal(drops, 1)
    assert.equal(drop, prefixed + 1)
    assert.equal(lines[drop], 'DeleteRegValue HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir"')
  })

  it('validateInstallerScripts fails on a malformed prefix and on an unguarded delete', () => {
    const dir = mkdtempSync(join(tmpdir(), 'ia-nsh901-'))
    const okLang = '!ifdef LANG_ENGLISH\nLangString a ${LANG_ENGLISH} "x"\n!endif\n'
    writeFileSync(join(dir, 'installer.nsh'), `${okLang}${BROKEN_DELETE}\n`)
    writeFileSync(join(dir, 'installer-directories.nsh'), okLang)
    assert.throws(() => validateInstallerScripts(dir), /long-path prefix/)

    writeFileSync(join(dir, 'installer.nsh'), okLang)
    writeFileSync(join(dir, 'installer-directories.nsh'), `${okLang}${FIXED_DELETE}\n!insertmacro iaClearBackupDir\n`)
    assert.throws(() => validateInstallerScripts(dir), /unguarded backup delete/)
  })
})

describe('sha256File', () => {
  it('hashes a file deterministically', () => {
    const dir = mkdtempSync(join(tmpdir(), 'ia-sha-'))
    const file = join(dir, 'a.txt')
    writeFileSync(file, 'hello')
    assert.equal(
      sha256File(file),
      '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824',
    )
  })
})

describe('clean-user-data', () => {
  function makeFixture() {
    const dir = mkdtempSync(join(tmpdir(), 'ia-data-'))
    const sessionDir = join(dir, 'sessions', 's1')
    const artifactDir = join(dir, 'artifacts', 'a1')
    const workspaceDir = join(dir, 'workspace')
    for (const d of [sessionDir, artifactDir, workspaceDir]) {
      mkdirSync(d, { recursive: true })
    }
    writeFileSync(join(sessionDir, 'events.jsonl'), '{"a":1}\n')
    writeFileSync(join(artifactDir, 'blob.bin'), 'x'.repeat(100))
    return dir
  }

  it('collects targets with sizes and evidence refs', () => {
    const dir = makeFixture()
    const targets = collectDataTargets(dir)
    const kinds = targets.map((t) => t.kind)
    assert.ok(kinds.includes('sessions'))
    assert.ok(kinds.includes('artifacts'))
    const sessions = targets.find((t) => t.kind === 'sessions')
    assert.ok(sessions.sizeBytes > 0)
    assert.ok(sessions.evidenceRefs.includes('s1'))
  })

  it('preview mode deletes nothing', async () => {
    const dir = makeFixture()
    const lines = []
    const report = await runCleanUserData({ dataDir: dir, mode: 'preview', stdout: { write: (s) => lines.push(s) } })
    assert.equal(report.deleted, false)
    assert.ok(existsSync(join(dir, 'sessions', 's1', 'events.jsonl')))
    assert.ok(lines.join('').includes('s1'))
  })

  it('confirm mode deletes file targets but never touches the credential store', async () => {
    const dir = makeFixture()
    const report = await runCleanUserData({ dataDir: dir, mode: 'confirm', stdout: { write: () => {} } })
    assert.equal(report.deleted, true)
    assert.ok(!existsSync(join(dir, 'sessions')))
    assert.ok(report.skipped.some((s) => s.kind === 'credentials'))
  })

  it('refuses to run without an explicit mode', async () => {
    await assert.rejects(
      () => runCleanUserData({ dataDir: '/tmp/x', mode: 'yolo', stdout: { write: () => {} } }),
      /--preview|--confirm/,
    )
  })
})
