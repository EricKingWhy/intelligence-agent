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
  validateCleanupHelpers,
  malformedLongPathPrefixes,
  unguardedBackupDelete,
  stripNsisComments,
  DELETE_SITE_POLICIES,
  LOGICLIB_OPENS,
  LOGICLIB_CLOSES,
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
    // The three-file loop reads the cleanup helper before it reaches
    // installer-directories.nsh, so the helper is the shipped one.
    writeFileSync(
      join(dir, 'installer-cleanup.nsh'),
      readFileSync(join(installerDir, 'installer-cleanup.nsh'), 'utf8'),
    )
    assert.throws(() => validateInstallerScripts(dir), /installer-directories\.nsh/)
  })

  it('validateInstallerScripts asserts the cleanup helpers (#904)', () => {
    // The delete-site rules are only as strong as the helpers behind them, and
    // the site scan cannot see into the included helper file: without the
    // primitives every site still reads as clean while the target is neither
    // validated nor scanned. The two delete-site files are the shipped ones, so
    // the failure can only come from validateCleanupHelpers.
    const dir = mkdtempSync(join(tmpdir(), 'ia-nsh-cleanup-'))
    for (const file of ['installer.nsh', 'installer-directories.nsh']) {
      writeFileSync(join(dir, file), readFileSync(join(installerDir, file), 'utf8'))
    }
    writeFileSync(join(dir, 'installer-cleanup.nsh'), '; nothing here\n')
    assert.throws(() => validateInstallerScripts(dir), /installer-cleanup\.nsh: cleanup helpers incomplete/)
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
  // #904: every delete site prepares its target and passes the status gate;
  // the fixtures carry both lines so a rule under test is never satisfied by a
  // fixture that ignores the prepared target.
  const PREPARE = ['Call iaPrepareDelete', 'StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped']
  // The checks that make a guarded delete's window complete: the one
  // `${Errors}` read, the leftover record, and the clear call.
  const GUARDED_TAIL = [
    '${If} ${Errors}',
    '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
    '${EndIf}',
    '!insertmacro iaClearBackupDir',
  ]
  const BROKEN_DELETE =
 'RMDir /r "\\\\?$iaBackupDirectory"'
  const FIXED_DELETE = 'RMDir /r "$iaDeleteTarget"'

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
    // NSIS also takes `#` comments — at the start of a line, after a complete
    // statement, or glued to the closing quote of one (measured on makensis
    // 3.0.4.1: `DetailPrint "x" # trailing` AND `DetailPrint "x"# trailing`
    // compile and show the line, while `DetailPrint "x" stray` fails with "Error
    // in script", so the `#` starts a comment rather than being ignored) — but a
    // `#` inside a string is text, and one glued to a non-quote token is left
    // alone: measured, `StrCpy $0 foo#bar` stores `foo#bar`, so cutting there
    // would eat code. Leaving that case alone can only over-report, which is the
    // fail-closed direction.
    assert.equal(stripNsisComments('# whole line'), '')
    assert.equal(stripNsisComments('  # indented'), '  ')
    assert.equal(stripNsisComments('DetailPrint "x" # trailing'), 'DetailPrint "x" ')
    assert.equal(stripNsisComments('DetailPrint "a # b"'), 'DetailPrint "a # b"')
    assert.equal(stripNsisComments('StrCpy $0 "a"#$1'), 'StrCpy $0 "a"')
    assert.equal(stripNsisComments("StrCpy $0 'a'#$1"), "StrCpy $0 'a'")
    assert.equal(stripNsisComments('StrCpy $0 `a`#$1'), 'StrCpy $0 `a`')
    assert.equal(stripNsisComments('StrCpy $0 foo#bar'), 'StrCpy $0 foo#bar')
    // All three string forms are strings: measured, the single-quoted and
    // backtick forms compile AND delete, and a `;` inside one is text.
    assert.equal(stripNsisComments("RMDir /r 'a;b' ; trailing"), "RMDir /r 'a;b' ")
    assert.equal(stripNsisComments('RMDir /r `a;b` ; trailing'), 'RMDir /r `a;b` ')
    // `$\'` is an escape inside a single-quoted string (measured: it stores a
    // literal `'`), so the quote after it does not close the string and the
    // trailing `;` is still content; a bare `'` does close it.
    assert.equal(stripNsisComments("x 'a$\\' ; keep"), "x 'a$\\' ; keep")
    assert.equal(stripNsisComments("x 'a$\\'b' ; gone"), "x 'a$\\'b' ")
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
    assert.deepEqual(malformedLongPathPrefixes('StrCpy $iaDeleteTarget "\\\\?\\$iaPlainPath"'), [])
    assert.deepEqual(malformedLongPathPrefixes(BROKEN_DELETE), [{ line: 1, text: BROKEN_DELETE }])
    assert.throws(() => validateLongPathPrefixes(BROKEN_DELETE, 'installer-directories.nsh'), /long-path prefix/)
  })

  it('does not flag the same text inside a comment', () => {
    assert.deepEqual(malformedLongPathPrefixes(`; upstream: ${BROKEN_DELETE}`), [])
  })

  it('requires ClearErrors, one ${Errors} read and the leftover record around the delete', () => {
    assert.deepEqual(unguardedBackupDelete([...PREPARE, FIXED_DELETE, '!insertmacro iaClearBackupDir'].join('\n')), [
      { line: 3, what: 'missing ClearErrors before the delete' },
      { line: 3, what: 'missing ${Errors} check before iaClearBackupDir' },
      { line: 3, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
    const guarded = [
      'ClearErrors',
      ...PREPARE,
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
      ...PREPARE,
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
      ...PREPARE,
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(farAbove), [
      { line: 7, what: 'missing ClearErrors before the delete' },
    ])
  })

  it('flags a second ${Errors} read: it cannot see the failed delete', () => {
    // Measured on NSIS 3.0.4.1: LogicLib's ${Errors} is IfErrors, which consumes
    // the error flag — the first read after a failing delete reports it, an
    // immediate second read reports none. A marker read ahead of the outcome
    // test would silently disable the leftover record.
    const twoReads = [
      'ClearErrors',
      ...PREPARE,
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
        line: 4,
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
      ...PREPARE,
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
      ...PREPARE,
      FIXED_DELETE,
      '${If} ${Errors}',
      '  DetailPrint "could not remove $iaBackupDirectory"',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(loggedOnly), [
      { line: 4, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
  })

  it('tests Errors before clearing the pointer, even across comment lines', () => {
    const withoutTest = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      '; the backup is gone now',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(withoutTest), [
      { line: 4, what: 'missing ${Errors} check before iaClearBackupDir' },
      { line: 4, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
  })

  it('flags a delete whose iaClearBackupDir call is gone (the window cannot run on)', () => {
    const noClear = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(noClear), [
      { line: 4, what: 'no !insertmacro iaClearBackupDir after the delete' },
    ])
  })

  it('does not let another function satisfy the check (fail-closed window)', () => {
    const acrossFunctions = [
      'Function iaPromoteApplication',
      '  ClearErrors',
      ...PREPARE.map((line) => `  ${line}`),
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
      { line: 5, what: 'no !insertmacro iaClearBackupDir after the delete' },
      { line: 5, what: 'missing ${Errors} check before iaClearBackupDir' },
      { line: 5, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
  })

  it('flags a ClearErrors between the delete and the read', () => {
    const interposed = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      'ClearErrors',
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(interposed), [
      { line: 4, what: 'ClearErrors between the delete and the ${Errors} read discards the failed delete' },
    ])
  })

  it('does not let another macro satisfy the check (fail-closed window)', () => {
    const acrossMacros = [
      '!macro promote',
      '  ClearErrors',
      ...PREPARE.map((line) => `  ${line}`),
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
      { line: 5, what: 'no !insertmacro iaClearBackupDir after the delete' },
      { line: 5, what: 'missing ${Errors} check before iaClearBackupDir' },
      { line: 5, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
      {
        line: 5,
        what: "the delete sits inside !macro promote, which the file never !insertmacro's — the delete never runs",
      },
    ])
  })

  it('is fail-closed: no delete site at all is a problem', () => {
    assert.deepEqual(unguardedBackupDelete('Function nothing\nFunctionEnd'), [
      { line: 0, what: 'no recursive RMDir of $iaDeleteTarget (a prepared target) found' },
    ])
  })

  it('flags text that satisfies the checks but can be compiled out', () => {
    // Every check below is textual, so text that never compiles would pass it.
    // Verified against this guard before the rule existed: wrapping the whole
    // guarded block in `!ifdef NOPE` (or `!if 0`) returned [] — a build-time
    // guard a dead branch can satisfy is not a guard.
    const deadBranch = [
      ...PREPARE,
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
        line: 5,
        what: 'the delete sits inside 1 open conditional compilation directive(s) — the delete and its checks can be compiled out',
      },
    ])
    // The wrapping form is the common one, and the one a window-scoped rule
    // missed: `!ifdef` above the delete, `!endif` after the clear. Every line
    // the window checks is present, and none of them compiles without NOPE.
    const wrapped = [
      'Function iaPromoteApplication',
      ...PREPARE.map((line) => `  ${line}`),
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
        line: 6,
        what: 'the delete sits inside 1 open conditional compilation directive(s) — the delete and its checks can be compiled out',
      },
    ])
    // Directives are case-insensitive: makensis 3.0.4.1 skips a block wrapped in
    // `!IFDEF NOPE` … `!ENDIF` (probe: `!ifdef`/`!endif` and their upper-case
    // spellings both compile), so the scan cannot be case-sensitive either.
    const cased = [
      'Function iaPromoteApplication',
      ...PREPARE.map((line) => `  ${line}`),
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
        line: 6,
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
      ...PREPARE.map((line) => `  ${line}`),
      '  ClearErrors',
      '  ${If} ${FileExists} "$INSTDIR\\app.exe"',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      '  ${EndIf}',
      '!MACROEND',
      // A macro body runs where the macro is inserted, so the file has to insert
      // it or the guard reports the delete as never running (#905 item 4) — this
      // fixture is a call site as well as a definition.
      '!insertmacro iaPromoteApplication',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(casedBlock), [])
    // The closing directive is case-insensitive for the block scan too (#919
    // review F1): `fUNCTIONEND` ends the block, so a directive below it is not
    // part of the delete's block. Measured: with CLOSES_FUNCTION made
    // case-sensitive the closer is not recognized, the block runs on, and this
    // fixture reported the `!ifdef` below it as covering the delete.
    const recasedCloser = [
      'Function iaPromoteApplication',
      ...PREPARE.map((line) => `  ${line}`),
      '  ClearErrors',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      '  fUNCTIONEND',
      '!ifdef NOPE',
      '!endif',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(recasedCloser), [])
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
      ...PREPARE.map((line) => `  ${line}`),
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
      '!endif',
      'Function iaPromoteApplication',
      '  ClearErrors',
      ...PREPARE.map((line) => `  ${line}`),
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(outerWrap), [
      {
        line: 6,
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
      ...PREPARE.map((line) => `  ${line}`),
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
        line: 7,
        what: 'the delete sits 2 LogicLib level(s) inside the block — a branch above it can skip the delete and its checks',
      },
      {
        line: 7,
        what:
          'the constant-false condition `${If} 1 == 0` covers the delete and the ${Errors} read and the leftover record and iaClearBackupDir — that code can never run',
      },
    ])
    // Control: the promote guard itself is one level, and nothing is reported.
    const promoteGuardOnly = [
      'Function iaPromoteApplication',
      ...PREPARE.map((line) => `  ${line}`),
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
        ...PREPARE,
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
      ...PREPARE,
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  !insertmacro iaClearBackupDir',
      '${EndIf}',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(nested), [
      {
        line: 4,
        what: 'iaClearBackupDir sits 1 LogicLib level(s) deeper than the delete — a path skips the clear',
      },
    ])
    const balanced = [
      'ClearErrors',
      ...PREPARE,
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
      ...PREPARE,
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
      ...PREPARE,
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
      ...PREPARE,
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
          line: 4,
          what: 'iaClearBackupDir sits 1 LogicLib level(s) deeper than the delete — a path skips the clear',
        },
      ])
    }
  })

  it('flags a record that names a different directory', () => {
    const wrongValue = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $INSTDIR',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(wrongValue), [
      { line: 4, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
    // A value built from the backup path names a directory nothing will find.
    const suffixed = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory-tmp',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(suffixed), [
      { line: 4, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
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
        ...PREPARE,
        FIXED_DELETE,
        '${If} ${Errors}',
        record,
        '${EndIf}',
        '!insertmacro iaClearBackupDir',
      ].join('\n')
      assert.deepEqual(
        unguardedBackupDelete(layout),
        [{ line: 4, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' }],
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
      ...PREPARE.map((line) => `  ${line}`),
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
      ...PREPARE.map((line) => `  ${line}`),
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    for (const layout of [previousFunction, uninsertedMacro]) {
      assert.deepEqual(unguardedBackupDelete(layout), [
        { line: 7, what: 'missing ClearErrors before the delete' },
      ])
    }
  })

  it('recognizes a re-cased delete instead of reporting it as missing', () => {
    // NSIS directives are case-insensitive, so `rmdir /r` is the same delete:
    // reporting it as "no delete found" would blame a correct layout.
    const lower = [
      'ClearErrors',
      ...PREPARE,
      'rmdir /r "$iaDeleteTarget"',
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(lower), [])
  })

  it('counts LogicLib nesting case-insensitively (R1: `${iF}` … `${eNdIf}`)', () => {
    // Define and macro names are case-insensitive (measured on 3.0.4.1:
    // `!define Foo` answers `!ifdef foo`), so a mixed-case opener nests exactly
    // like `${If}` — a case-sensitive scan reads the guarded block as one level
    // away from the checks it belongs to.
    const casedCloser = [
      'Function iaPromoteApplication',
      ...PREPARE.map((line) => `  ${line}`),
      '  ClearErrors',
      '  ${If} ${FileExists} "$INSTDIR\\app.exe"',
      `    ${FIXED_DELETE}`,
      '    ${If} ${Errors}',
      '      WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '    ${EndIf}',
      '    !insertmacro iaClearBackupDir',
      '  ${eNdIf}',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(casedCloser), [])
    // And a never-taken mixed-case branch is a never-taken branch: the constant
    // condition is decided the same way, with the file's own spelling in the
    // message.
    const casedDeadBranch = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      '${iF} 1 == 0',
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${eNdIf}',
      '${eNdIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(casedDeadBranch), [
      {
        line: 4,
        what:
          'the constant-false condition `${iF} 1 == 0` covers the ${Errors} read and the leftover record — that code can never run',
      },
    ])
  })

  it('does not accept a ClearErrors from another branch (R5)', () => {
    // The flag is sticky, so a clear in a branch this delete is not on reads as
    // "cleared" while the delete's own path has none (round-5 R5, verified
    // against this guard).
    const otherBranch = [
      'Function iaPromoteApplication',
      ...PREPARE.map((line) => `  ${line}`),
      '  ${If} $0 == "a"',
      '    ClearErrors',
      '  ${EndIf}',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(otherBranch), [
      {
        line: 7,
        what: 'the ClearErrors above the delete sits in a branch the delete is not on — it does not clear this path',
      },
    ])
    // The same block with one side each: the `${Else}` is the divider that makes
    // the clear and the delete different paths.
    const siblingBranch = [
      'Function iaPromoteApplication',
      ...PREPARE.map((line) => `  ${line}`),
      '  ${If} $0 == "a"',
      '    ClearErrors',
      '  ${Else}',
      `    ${FIXED_DELETE}`,
      '    ${If} ${Errors}',
      '      WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '    ${EndIf}',
      '    !insertmacro iaClearBackupDir',
      '  ${EndIf}',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(siblingBranch), [
      {
        line: 7,
        what: 'the ClearErrors above the delete sits in a sibling branch — it does not clear this path',
      },
    ])
  })

  it('flags a clear call on the other side of a branch divider (R2)', () => {
    // Delete and clear call inside the same block, but the clear sits after the
    // `${Else}`: a path that takes the delete branch never reaches the clear, so
    // a finished install still reads as incomplete.
    const siblingClear = [
      ...PREPARE,
      'ClearErrors',
      '${If} $0 == "a"',
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '${Else}',
      '  !insertmacro iaClearBackupDir',
      '${EndIf}',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(siblingClear), [
      {
        line: 5,
        what: 'iaClearBackupDir sits in a sibling branch of the delete — a path skips the clear',
      },
    ])
  })

  it('flags a never-taken branch that covers the checks (R3/R5)', () => {
    // A literal comparison that cannot hold is the `!ifdef NOPE` bypass in
    // LogicLib syntax. The guard decides literal comparisons only — this is not
    // reachability analysis — so a dead branch written that way has to be
    // reported, and a condition that does hold has to stay clean.
    const deadBranch = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      '${If} 1 == 0',
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(deadBranch), [
      {
        line: 4,
        what:
          'the constant-false condition `${If} 1 == 0` covers the ${Errors} read and the leftover record — that code can never run',
      },
    ])
    // Control: `${IfNot} 1 == 0` always holds, so its body runs and nothing is
    // reported. Deciding this the other way round would flag live code.
    const aliveBranch = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      '${IfNot} 1 == 0',
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(aliveBranch), [])
  })

  it('ignores statements in a macro body the delete is not in (R4)', () => {
    // A macro body does not run at its definition site: a `ClearErrors` defined
    // just above the delete clears nothing here, and checks parked in a macro
    // body after the delete do not run with it (round-5 R4, both verified
    // against this guard).
    const clearInMacro = [
      'Function iaPromoteApplication',
      '  !macro helper',
      '    ClearErrors',
      '  !macroend',
      ...PREPARE.map((line) => `  ${line}`),
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(clearInMacro), [
      { line: 7, what: 'missing ClearErrors before the delete' },
    ])
    const checksInMacro = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      '!macro recordLeftover',
      '  ${If} ${Errors}',
      '  ${EndIf}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '!macroend',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(checksInMacro), [
      { line: 4, what: 'no !insertmacro iaClearBackupDir after the delete' },
      { line: 4, what: 'missing ${Errors} check before iaClearBackupDir' },
      { line: 4, what: 'failed delete is not recorded (no IaLeftoverDir write of $iaBackupDirectory)' },
    ])
  })

  it('flags a delete in a macro the file never inserts (item 4, fail-closed)', () => {
    // The guard's window ends at the enclosing `!macroend`, so a delete inside a
    // macro body used to look complete while nothing ran it: without an
    // `!insertmacro` for that macro anywhere after its definition, the delete
    // never runs.
    const body = [
      '  ClearErrors',
      ...PREPARE.map((line) => `  ${line}`),
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
    ]
    const notInserted = ['!macro promote', ...body, '!macroend'].join('\n')
    assert.deepEqual(unguardedBackupDelete(notInserted), [
      {
        line: 5,
        what: "the delete sits inside !macro promote, which the file never !insertmacro's — the delete never runs",
      },
    ])
    // Control: with a call site the same body is clean, and the call site may sit
    // anywhere after the definition and in any case — macro names are
    // case-insensitive (measured).
    const inserted = [notInserted, '!INSERTMACRO Promote'].join('\n')
    assert.deepEqual(unguardedBackupDelete(inserted), [])
  })

  it('accepts a delete and a record in any of the three quote styles (item 3)', () => {
    // Measured on 3.0.4.1: `RMDir /r '…'` and `RMDir /r `…`` compile and delete
    // the same directory as the double-quoted form, so a single-quoted call is
    // the same delete and not a missing one.
    const singleQuoted = [
      'ClearErrors',
      ...PREPARE,
      "RMDir /r '$iaDeleteTarget'",
      '${If} ${Errors}',
      "  WriteRegStr HKCU '${INSTALL_REGISTRY_KEY}' 'IaLeftoverDir' '$iaBackupDirectory'",
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(singleQuoted), [])
    const backtickQuoted = [
      'ClearErrors',
      ...PREPARE,
      'RMDir /r `$iaDeleteTarget`',
      '${If} ${Errors}',
      '  WriteRegStr HKCU `${INSTALL_REGISTRY_KEY}` `IaLeftoverDir` `$iaBackupDirectory`',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(backtickQuoted), [])
  })

  it('accepts case variants of the variable and define names (item 2)', () => {
    // Variable and define names are case-insensitive (measured on 3.0.4.1:
    // `StrCmp $IABackupDirectory` compiles against `Var iaBackupDirectory`, and
    // `!define Foo` answers `!ifdef foo`), so a re-cased spelling is the same
    // delete, read and record — reported as missing, it would blame correct code.
    const cased = [
      'ClearErrors',
      ...PREPARE.map((line) => line.replace(/ia/g, 'IA')),
      'rmdir /r "$IADeleteTarget"',
      '${IF} ${errors}',
      '  WriteRegStr HKCU "${install_registry_key}" "IaLeftoverDir" $IABackupDirectory',
      '${ENDIF}',
      '!INSERTMACRO iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(cased), [])
  })

  it('reads the flag only from the forms that really read it (item 1)', () => {
    // `IfErrors` consumes the flag whatever spelling reaches it, so a raw
    // `IfErrors` and the negated `${IfNot}`/`${Unless}` conditions all count as
    // the read.
    const rawIfErrors = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      'IfErrors lblFailed lblDone',
      'lblFailed:',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      'lblDone:',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(rawIfErrors), [])
    for (const read of ['${IfNot} ${Errors}', '${Unless} ${Errors}']) {
      const layout = [
        'ClearErrors',
        ...PREPARE,
        FIXED_DELETE,
        read,
        '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
        '${EndIf}',
        '!insertmacro iaClearBackupDir',
      ].join('\n')
      assert.deepEqual(unguardedBackupDelete(layout), [], read)
    }
    // A `${Errors}` the guard cannot name is reported instead of counted — a
    // usage in a string literal does not even compile as a statement (measured:
    // "DetailPrint expects 1 parameters, got 3"), and a read that cannot be
    // recognized must not satisfy a rule about reads.
    const inString = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      'DetailPrint "flag ${Errors} seen"',
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(inString), [
      {
        line: 5,
        what:
          'an unrecognized ${Errors} usage between the delete and iaClearBackupDir — only a ${If}/${IfNot}/${Unless} condition or a raw IfErrors reads the flag',
      },
    ])
  })

  it('does not count or report a `${Errors}` in a comment (R7)', () => {
    // `#` glued to a closing quote starts a comment (measured), and the comment
    // is stripped before the window is scanned: it neither inflates the read
    // count nor shows up as an unrecognized usage.
    const gluedComment = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      'DetailPrint "x"# ${Errors} is sticky, read it once',
      '${If} ${Errors}',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(gluedComment), [])
  })

  it('stops the delete window at a mixed-case FunctionEnd (R6)', () => {
    // `functionend` closes the function exactly like `FunctionEnd` does
    // (directives are case-insensitive — measured), so a clear call after it is
    // outside the delete's block and does not satisfy the check.
    const cased = [
      'Function iaPromoteApplication',
      '  ClearErrors',
      ...PREPARE.map((line) => `  ${line}`),
      `  ${FIXED_DELETE}`,
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  functionend',
      '  !insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(cased), [
      { line: 5, what: 'no !insertmacro iaClearBackupDir after the delete' },
    ])
  })

  it('validateBackupDeleteGuards names the file', () => {
    assert.throws(
      () => validateBackupDeleteGuards('Function nothing\nFunctionEnd', 'installer-directories.nsh'),
      /^Error: installer-directories\.nsh: unguarded backup delete/,
    )
  })

  it('the shipped scripts carry the corrected, guarded delete', () => {
    const directories = readFileSync(join(installerDir, 'installer-directories.nsh'), 'utf8')
    assert.deepEqual(malformedLongPathPrefixes(directories), [])
    assert.deepEqual(unguardedBackupDelete(directories, { sitePolicies: DELETE_SITE_POLICIES }), [])
    // The uninstaller sweep lives in installer.nsh; it is a delete site too, so
    // the same guard reads it with the same policy table.
    const base = readFileSync(join(installerDir, 'installer.nsh'), 'utf8')
    assert.deepEqual(malformedLongPathPrefixes(base), [])
    assert.deepEqual(unguardedBackupDelete(base, { sitePolicies: DELETE_SITE_POLICIES }), [])
    validateCleanupHelpers(
      readFileSync(join(installerDir, 'installer-cleanup.nsh'), 'utf8'),
      'installer-cleanup.nsh',
    )
  })

  it('keeps the leftover record exactly while a path form can still see it', () => {
    // Existence-only regexes let a branch swap pass (keep when the directory
    // exists, drop when it is gone), and searching the whole file lets a decoy
    // copy satisfy them, so this pins the shape inside the promote body only:
    // read the record, hand it to iaProbePath — measured on NSIS 3.0.4.1, an
    // unprefixed >MAX_PATH path answers "false" to ${FileExists}, and the bare
    // "\\?\" prefix cannot name a UNC path (that needs "\\?\UNC\"), so one form
    // on its own is not a probe — and drop it as the first statement of the
    // innermost negative branch, where no form can see the directory any more.
    const body = promoteApplicationBody(
      readFileSync(join(installerDir, 'installer-directories.nsh'), 'utf8'),
    )
    const lines = body.map((line) => line.trim()).filter((line) => line !== '')
    const del = lines.findIndex((line) => /^RMDir\s+\/r\b/.test(line))
    const read = lines.findIndex((line) => /^!insertmacro\s+iaReadLeftoverDir\b/.test(line))
    const plain = lines.findIndex((line) => /^StrCpy\s+\$iaPlainPath\s+"\$iaLeftoverDirectory"$/.test(line))
    const probe = lines.findIndex((line) => /^Call\s+iaProbePath\b/.test(line))
    const guarded = lines.findIndex((line) => /^\$\{If\}\s+\$iaProbeFound\s+!=\s+"1"$/.test(line))
    const drop = lines.findIndex((line) => /^DeleteRegValue\b[^\n]*"IaLeftoverDir"/.test(line))
    const clear = lines.findIndex((line) => /!insertmacro\s+iaClearBackupDir\b/.test(line))
    for (const [name, index] of [
      ['the delete', del],
      ['the record read', read],
      ['the plain-path copy', plain],
      ['the probe call', probe],
      ['the probe guard', guarded],
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
      del < read && read < plain && plain < probe && probe < guarded && guarded < drop && drop < clear,
      `promote body out of order: ${JSON.stringify({ del, read, plain, probe, guarded, drop, clear })}`,
    )
    // And the record handling has to run on the delete's own path: a
    // same-shaped block parked inside an extra never-taken branch (`${If} 1 == 0`)
    // satisfies every assertion above while the record handling never runs.
    // Depth is counted with the SAME lists the build guard counts with, so the
    // two notions of nesting cannot drift again (#905 / R8: this copy was missing
    // three closers and the case-insensitivity, and read the guard's own layouts
    // differently).
    const opensBlock = new RegExp(LOGICLIB_OPENS, 'gi')
    const closesBlock = new RegExp(LOGICLIB_CLOSES, 'gi')
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
    // moves delete, probe, record and clear down together, so every
    // comparison above still holds while nothing runs. The delete's own depth
    // is what pins it to the promote path — one branch, the "new files are in
    // place" guard — and the build guard reports more than that.
    assert.equal(depthAt(del), 1, 'the delete is not one branch deep inside the promote body')
    assert.equal(depthAt(read), 1, 'the record read is not one branch deep inside the promote body')
    // iaProbePath runs inside the record's non-empty branch, the probe guard on
    // that same path, and the drop inside the guard's negative branch.
    assert.deepEqual(
      [depthAt(plain), depthAt(probe), depthAt(guarded), depthAt(drop)],
      [depthAt(del) + 1, depthAt(del) + 1, depthAt(del) + 1, depthAt(del) + 2],
      'the record is not dropped inside the probe, with a copy the probe can read',
    )
    // Exactly one probe and one drop: a second probe site would be a second
    // guess about the same record, and a second drop a second guess about when
    // it is gone.
    const probes = lines.filter((line) => /^Call\s+iaProbePath\b/.test(line)).length
    assert.equal(probes, 1, 'the promote body probes the leftover record more than once')
    const drops = lines.filter((line) => /^DeleteRegValue\b[^\n]*"IaLeftoverDir"/.test(line)).length
    assert.equal(drops, 1)
    // Drop as the first statement after the guard, and the probe reads the
    // record's own value: a copy of anything else would decide about a path the
    // record does not name.
    assert.equal(drop, guarded + 1)
    assert.equal(lines[drop], 'DeleteRegValue HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir"')
    assert.equal(lines[plain], 'StrCpy $iaPlainPath "$iaLeftoverDirectory"')
  })

  it('requires the prepare call and the status gate in front of the delete (#904)', () => {
    // The idempotence gate: only iaPrepareDelete may build the target (it is
    // where the shape check and the reparse scan live), and only the status gate
    // may let the delete run — a refused target is never deleted.
    const bare = ['ClearErrors', FIXED_DELETE, ...GUARDED_TAIL].join('\n')
    assert.deepEqual(unguardedBackupDelete(bare), [
      {
        line: 2,
        what: 'no Call iaPrepareDelete before the delete — the target is not shape-checked or scanned',
      },
    ])
    const noGate = ['Call iaPrepareDelete', 'ClearErrors', FIXED_DELETE, ...GUARDED_TAIL].join('\n')
    assert.deepEqual(unguardedBackupDelete(noGate), [
      {
        line: 3,
        what: 'no StrCmp $iaDeleteStatus "ok" gate between iaPrepareDelete and the delete — a refused target would still be deleted',
      },
    ])
    // A prepare in a branch the delete is not on, and one behind an `${Else}`:
    // both build a target for a path the delete does not run on.
    const otherBranch = [
      'Function iaPromoteApplication',
      '  ${If} $0 == "a"',
      '    Call iaPrepareDelete',
      '    StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped',
      '  ${EndIf}',
      '  ClearErrors',
      `  ${FIXED_DELETE}`,
      ...GUARDED_TAIL.map((line) => `  ${line}`),
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(otherBranch), [
      {
        line: 7,
        what: 'the Call iaPrepareDelete above the delete sits in a branch the delete is not on — it does not validate this target',
      },
    ])
    const sibling = [
      'Function iaPromoteApplication',
      '  ${If} $0 == "a"',
      '    Call iaPrepareDelete',
      '    StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped',
      '  ${Else}',
      '    ClearErrors',
      `    ${FIXED_DELETE}`,
      ...GUARDED_TAIL.map((line) => `    ${line}`),
      '  ${EndIf}',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(sibling), [
      {
        line: 7,
        what: 'the Call iaPrepareDelete above the delete sits in a sibling branch — it does not validate this target',
      },
    ])
    // A prepare that runs somewhere else entirely (another function) is not a
    // prepare for this delete, and the delete then has none at all.
    const elsewhere = [
      'Function other',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped',
      'FunctionEnd',
      'Function iaPromoteApplication',
      '  ClearErrors',
      `  ${FIXED_DELETE}`,
      ...GUARDED_TAIL.map((line) => `  ${line}`),
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(elsewhere), [
      {
        line: 7,
        what: 'no Call iaPrepareDelete before the delete — the target is not shape-checked or scanned',
      },
    ])
  })

  it('reports a kept backup with a non-zero exit code, refusals and failures alike (#919 Q6)', () => {
    // A delete that fails (a denied child, a sharing violation) keeps the
    // backup for the same reason a refusal does: the directory is still there,
    // so the run must not read as a success. All three kept-leftover sites
    // report alike — the uninstaller sweep and the rollback rename failure set
    // `SetErrorLevel 2` — while the promote site used to exempt `failed` and
    // return 0 (measured on the real machine: the driver's `failure` case read
    // rc 0 with the backup kept and recorded).
    const body = promoteApplicationBody(
      readFileSync(join(installerDir, 'installer-directories.nsh'), 'utf8'),
    )
    const lines = body.map((line) => line.trim()).filter((line) => line !== '')
    // No status value may be carved out of the exit-code path: the exemption
    // was `${If} $iaDeleteStatus != "failed"` around the single SetErrorLevel.
    assert.ok(
      !lines.some((line) => /\$iaDeleteStatus\s*!=\s*"failed"/i.test(line)),
      'a status value is exempted from the kept-backup exit code',
    )
    const report = lines.findIndex((line) => /\$\(iaStaleBackup\)/.test(line))
    assert.notEqual(report, -1, 'the stale-backup report is missing from the promote body')
    // Report and exit code sit together and unbranched: a conditional between
    // them is the exemption again, and a `SetErrorLevel` further down is not
    // necessarily on this path.
    assert.equal(lines[report + 1], 'SetErrorLevel 2')
  })

  it('validateInstallerScripts fails on a malformed prefix and on an unguarded delete', () => {
    const dir = mkdtempSync(join(tmpdir(), 'ia-nsh901-'))
    const okLang = '!ifdef LANG_ENGLISH\nLangString a ${LANG_ENGLISH} "x"\n!endif\n'
    // The cleanup helper is the shipped one, so a failure below can only come
    // from the file under test — and the three-file loop needs it to be present
    // before the delete guard ever runs.
    writeFileSync(
      join(dir, 'installer-cleanup.nsh'),
      readFileSync(join(installerDir, 'installer-cleanup.nsh'), 'utf8'),
    )
    writeFileSync(join(dir, 'installer.nsh'), `${okLang}${BROKEN_DELETE}\n`)
    writeFileSync(join(dir, 'installer-directories.nsh'), okLang)
    assert.throws(() => validateInstallerScripts(dir), /long-path prefix/)

    writeFileSync(join(dir, 'installer.nsh'), okLang)
    writeFileSync(join(dir, 'installer-directories.nsh'), `${okLang}${FIXED_DELETE}\n!insertmacro iaClearBackupDir\n`)
    assert.throws(() => validateInstallerScripts(dir), /unguarded backup delete/)
  })

  it('the site policies are the only thing that lets the sweep and the rollback delete pass (#904)', () => {
    // The two deviating sites ship in these files: the strict default reports
    // them (their record is the report/restore path, not the promote record), so
    // the table — and not a weaker rule — is what makes the files clean.
    const directories = readFileSync(join(installerDir, 'installer-directories.nsh'), 'utf8')
    const base = readFileSync(join(installerDir, 'installer.nsh'), 'utf8')
    assert.deepEqual(unguardedBackupDelete(directories, { sitePolicies: DELETE_SITE_POLICIES }), [])
    assert.deepEqual(unguardedBackupDelete(base, { sitePolicies: DELETE_SITE_POLICIES }), [])
    for (const source of [directories, base]) {
      const strict = unguardedBackupDelete(source)
      assert.ok(strict.length > 0, 'a deviating site passed the strict default — the policy table is untested')
    }
    // The names are matched case-insensitively, like every other NSIS name.
    const renamed = [
      '!MACRO CUSTOMUNINSTALL',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  ${GetParent} "$INSTDIR" $2',
      '  StrCpy $iaDeleteCandidate "$INSTDIR.old-x"',
      '  StrCpy $iaDeleteShapeCheck "1"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaSweepDone',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      '  IfErrors 0 iaSweepDone',
      '  SetErrorLevel 2',
      '  MessageBox MB_OKCANCEL "$(iaLeftoverSweep)" IDOK iaSweepDone IDCANCEL iaSweepDeclined',
      'iaSweepDeclined:',
      '  SetErrorLevel 2',
      '  Goto iaSweepDone',
      'iaSweepDone:',
      '!MACROEND',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(renamed, { sitePolicies: DELETE_SITE_POLICIES }), [])
  })

  it('the sweep has to enumerate by shape, ask, and report what it kept (#904)', () => {
    const sweep = [
      '!macro customUnInstall',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  StrCmp $0 "" iaSweepDone',
      '  FindClose $0',
      '  MessageBox MB_OKCANCEL "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDeclined',
      'iaSweepDeclined:',
      '  SetErrorLevel 2',
      '  Goto iaSweepDone',
      'iaSweepDelete:',
      '  ${GetParent} "$INSTDIR" $2',
      '  StrCpy $iaDeleteCandidate "$2\\$1"',
      '  StrCpy $iaDeleteBase "$INSTDIR"',
      '  StrCpy $iaDeleteShapeCheck "1"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaSweepKept',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      '  IfErrors 0 iaSweepNext',
      'iaSweepKept:',
      '  SetErrorLevel 2',
      'iaSweepNext:',
      '  FindClose $0',
      'iaSweepDone:',
      '!macroend',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(sweep, { sitePolicies: DELETE_SITE_POLICIES }), [])
    // Each requirement on its own: a sweep that finds its leftovers some other
    // way, deletes without asking, or keeps them silently is not the sweep.
    const swapped = (from, to) => sweep.replace(from, to)
    assert.deepEqual(
      unguardedBackupDelete(swapped('FindFirst $0 $1 "$INSTDIR.old-*"', 'StrCpy $0 ""'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 17, what: 'the sweep does not enumerate "$INSTDIR.old-*" by name shape' }],
    )
    assert.deepEqual(
      unguardedBackupDelete(swapped('"($9) $(iaLeftoverSweep)"', '"old leftovers?"'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 17, what: 'the sweep does not ask before deleting (no $(iaLeftoverSweep) prompt)' }],
    )
    assert.deepEqual(
      unguardedBackupDelete(swapped('iaSweepKept:\n  SetErrorLevel 2', 'iaSweepKept:\n  StrCpy $9 ""'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 17, what: 'nothing sets the exit code to 2 when leftovers are kept (SetErrorLevel 2)' }],
    )
    // A label may carry the statement on its own line (measured, probes r1/r2),
    // so `iaSweepKept: SetErrorLevel 2` is the same kept reading — the scan
    // graded the span line-by-line and read the inline spelling as a missing
    // exit code (the fix round's review, F-4).
    assert.deepEqual(
      unguardedBackupDelete(swapped('iaSweepKept:\n  SetErrorLevel 2', 'iaSweepKept: SetErrorLevel 2'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [],
    )
    // The value is the contract the caller reads (rc 2: the launch-form probe
    // and the real-machine driver assert it), and it has to sit on the kept
    // path below the delete — a `SetErrorLevel 0` next to it, or a `SetErrorLevel
    // 2` above the delete that runs before anything was kept, both used to
    // satisfy the earlier presence check (round-6 review, P3).
    assert.deepEqual(
      unguardedBackupDelete(swapped('iaSweepKept:\n  SetErrorLevel 2', 'iaSweepKept:\n  SetErrorLevel 0'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 17, what: 'nothing sets the exit code to 2 when leftovers are kept (SetErrorLevel 2)' }],
    )
    const aboveOnly = sweep
      .replace('iaSweepKept:\n  SetErrorLevel 2', 'iaSweepKept:\n  StrCpy $9 ""')
      .replace('iaSweepDelete:\n', 'iaSweepDelete:\n  SetErrorLevel 2\n')
    assert.deepEqual(unguardedBackupDelete(aboveOnly, { sitePolicies: DELETE_SITE_POLICIES }), [
      { line: 18, what: 'nothing sets the exit code to 2 when leftovers are kept (SetErrorLevel 2)' },
    ])
    // The flag is what makes iaPrepareDelete run the name check at all: with it
    // off, the delete pass removes every "$INSTDIR.old-*" sibling — foreign or
    // unbraced names included — and the reparse scan is the only refusal left
    // (round-6 review, P2: this exact mutation passed every check).
    assert.deepEqual(
      unguardedBackupDelete(swapped('  StrCpy $iaDeleteShapeCheck "1"', '  StrCpy $iaDeleteShapeCheck "0"'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [
        {
          line: 17,
          what: 'the sweep has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built',
        },
      ],
    )
    // #919 Q2: the flag is read when iaPrepareDelete runs, so the LAST
    // assignment above the call is the one that decides — arming "1" and
    // turning it off again right after used to satisfy the presence check
    // (measured).
    assert.deepEqual(
      unguardedBackupDelete(
        swapped('  Call iaPrepareDelete', '  StrCpy $iaDeleteShapeCheck "0"\n  Call iaPrepareDelete'),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [
        {
          line: 18,
          what: 'the sweep has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built',
        },
      ],
    )
    // FileFunc's argument order is "[path]" $result. Swapped, the macro's last
    // Pop lands on $INSTDIR: on the real uninstaller the delete pass then
    // enumerated ".old-*" relative, found nothing, and deleted nothing while the
    // exit code stayed 0 (measured on the #904 sweep case). Both rules have to
    // fire — the required form and the fail-closed "first argument is a bare
    // variable" rejection.
    const swappedOrder = unguardedBackupDelete(
      swapped('  ${GetParent} "$INSTDIR" $2', '  ${GetParent} $2 "$INSTDIR"'),
      { sitePolicies: DELETE_SITE_POLICIES },
    )
    assert.equal(swappedOrder.length, 2, JSON.stringify(swappedOrder))
    assert.ok(
      swappedOrder.some((p) => /does not cut the parent off \$INSTDIR in FileFunc's/.test(p.what)),
      JSON.stringify(swappedOrder),
    )
    assert.ok(
      swappedOrder.some((p) => /passes a variable as its first argument/.test(p.what)),
      JSON.stringify(swappedOrder),
    )
    // The delete's own error still has to be read: without the read the failure
    // branch is unreachable and a kept directory looks like a success.
    assert.deepEqual(
      unguardedBackupDelete(swapped('  IfErrors 0 iaSweepNext', '  StrCpy $9 ""'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 17, what: "missing ${Errors} check before the end of the delete's block" }],
    )
    // #919 Q8: the declined prompt is a third "kept" outcome — the leftovers
    // are still there — and it is UI-only (a silent run takes the /SD IDOK
    // default), so nothing else in this suite reads it. The prompt's own
    // IDCANCEL target names the branch: it has to exist and it has to set the
    // exit code. Landing the cancel on the success label used to pass, and so
    // did a prompt with no way to say no (measured).
    assert.deepEqual(
      unguardedBackupDelete(swapped('IDCANCEL iaSweepDeclined', 'IDCANCEL iaSweepDone'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [
        {
          line: 23,
          what: 'the declined sweep branch does not set the exit code to 2 — a declined sweep reports success',
        },
      ],
    )
    assert.deepEqual(
      unguardedBackupDelete(swapped(' IDCANCEL iaSweepDeclined', ''), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [
        {
          line: 5,
          what: 'the sweep prompt has no IDCANCEL branch — a UI uninstall cannot decline the sweep',
        },
      ],
    )
    // A chain declares two names at one position (`A: B:` — the delta's own
    // measured shape, probe r8), so the prompt's target may be either name, and
    // the branch's first statement is what follows the last name. Reading only
    // the first name reported the legal chain as a missing exit code (the fix
    // round's review, F-5); the statement may also sit on the chain's own line.
    assert.deepEqual(
      unguardedBackupDelete(
        swapped('iaSweepDeclined:\n  SetErrorLevel 2', 'iaSweepHopA: iaSweepDeclined:\n  SetErrorLevel 2'),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
    assert.deepEqual(
      unguardedBackupDelete(
        swapped('iaSweepDeclined:\n  SetErrorLevel 2', 'iaSweepHopA: iaSweepDeclined: SetErrorLevel 2'),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
    // #919 review (F3): setting the exit code is not enough — the branch has to
    // stop there. `Goto` into the prompt's IDOK target walks a declined sweep
    // straight into the delete pass, and a branch whose last meaningful line
    // just falls through to the next label does the same when that label is the
    // target (the shipped layout). Both used to pass every check (measured).
    assert.deepEqual(
      unguardedBackupDelete(swapped('  Goto iaSweepDone', '  Goto iaSweepDelete'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [
        {
          line: 6,
          what: 'the declined sweep branch can reach the delete pass, or does not end in a jump out of it — a declined sweep must only keep',
        },
      ],
    )
    assert.deepEqual(
      unguardedBackupDelete(swapped('  Goto iaSweepDone\n', ''), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [
        {
          line: 6,
          what: 'the declined sweep branch can reach the delete pass, or does not end in a jump out of it — a declined sweep must only keep',
        },
      ],
    )
    // #919 disposition review (R3): the delete pass is the loop the delete sits
    // in, not just the lines above it. `iaSweepKept` and `iaSweepNext` sit
    // below the `RMDir` and inside the loop, and the loop back-jumps to
    // `iaSweepLoop` — so a declined branch that lands on either walks into the
    // loop tail with the enumeration handle closed (measured on the real
    // uninstaller: `FindNext` on a closed handle is a 0xC0000005 crash, and
    // the loop's `Goto` runs it again). The loop's own exit labels, below the
    // back-jump, are the kept path and stay green.
    const looped = [
      '!macro customUnInstall',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  StrCmp $0 "" iaSweepDone',
      '  MessageBox MB_OKCANCEL "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDeclined',
      'iaSweepDeclined:',
      '  SetErrorLevel 2',
      '  Goto iaSweepDone',
      'iaSweepDelete:',
      '  ${GetParent} "$INSTDIR" $2',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  StrCmp $0 "" iaSweepDone',
      'iaSweepLoop:',
      '  StrCmp $1 "" iaSweepLoopEnd',
      '  StrCpy $iaDeleteCandidate "$2\\$1"',
      '  StrCpy $iaDeleteBase "$INSTDIR"',
      '  StrCpy $iaDeleteShapeCheck "1"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaSweepKept',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      '  IfErrors 0 iaSweepNext',
      '  StrCpy $iaDeleteStatus "failed"',
      'iaSweepKept:',
      '  SetErrorLevel 2',
      'iaSweepNext:',
      '  FindNext $0 $1',
      '  Goto iaSweepLoop',
      'iaSweepLoopEnd:',
      '  FindClose $0',
      'iaSweepDone:',
      '!macroend',
    ].join('\n')
    const loopSwapped = (from, to) => looped.replace(from, to)
    assert.deepEqual(unguardedBackupDelete(looped, { sitePolicies: DELETE_SITE_POLICIES }), [])
    const declinedMessage =
      'the declined sweep branch can reach the delete pass, or does not end in a jump out of it — a declined sweep must only keep'
    for (const landing of ['iaSweepKept', 'iaSweepNext']) {
      assert.deepEqual(
        unguardedBackupDelete(loopSwapped('  Goto iaSweepDone', `  Goto ${landing}`), {
          sitePolicies: DELETE_SITE_POLICIES,
        }),
        [{ line: 5, what: declinedMessage }],
        landing,
      )
    }
    assert.deepEqual(
      unguardedBackupDelete(loopSwapped('  Goto iaSweepDone', '  Goto iaSweepLoopEnd'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [],
    )
    // The fix round's own review (F1) measured two constructions on the real
    // `installer.nsh` that this rule read as clean, both on its new surface:
    // the loop's back-jump spelled as a two-target `StrCmp` (no bare `Goto`
    // below the delete, so the span collapsed onto the delete and the tail legs
    // fell outside it), and a hop label declared below the loop whose body
    // jumps back into the tail (the declined `Goto` landed outside the span and
    // the hop was never followed). Both are reported now; the hop whose body
    // stays outside the pass is still the kept path.
    const twoTarget = looped.replace(
      '  Goto iaSweepLoop',
      '  StrCmp $1 "" iaSweepLoopEnd iaSweepLoop',
    )
    assert.deepEqual(unguardedBackupDelete(twoTarget, { sitePolicies: DELETE_SITE_POLICIES }), [])
    assert.deepEqual(
      unguardedBackupDelete(twoTarget.replace('  Goto iaSweepDone', '  Goto iaSweepKept'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 5, what: declinedMessage }],
    )
    const withHop = (hopBody) =>
      looped
        .replace('  Goto iaSweepDone', '  Goto iaSweepHop')
        .replace('!macroend', `iaSweepHop:\n  ${hopBody}\n!macroend`)
    assert.deepEqual(
      unguardedBackupDelete(withHop('Goto iaSweepKept'), { sitePolicies: DELETE_SITE_POLICIES }),
      [{ line: 5, what: declinedMessage }],
    )
    assert.deepEqual(
      unguardedBackupDelete(withHop('Goto iaSweepLoopEnd'), { sitePolicies: DELETE_SITE_POLICIES }),
      [],
    )
    // The measured operand forms of the same rule (disposition review R5, read
    // on makensis 3.0.4.1 with a declared variable: `"1" 1`, `"1" 2` and
    // `"1" 1 0` all store "1", so they ARM — the earlier reading reported the
    // maxlen spelling although it arms — while `"1" 0` and `"1" 2 1` store ""
    // and stay reported). A `[maxlen]`/`[startoffset]` the scan cannot read as a
    // number is not evidence of an arming either.
    assert.deepEqual(
      unguardedBackupDelete(swapped('  StrCpy $iaDeleteShapeCheck "1"', '  StrCpy $iaDeleteShapeCheck "1" 2'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [],
    )
    assert.deepEqual(
      unguardedBackupDelete(
        swapped('  StrCpy $iaDeleteShapeCheck "1"', '  StrCpy $iaDeleteShapeCheck "1" 1 0'),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
    for (const truncating of ['  StrCpy $iaDeleteShapeCheck "1" 0', '  StrCpy $iaDeleteShapeCheck "1" 2 1', '  StrCpy $iaDeleteShapeCheck "1" $0']) {
      assert.deepEqual(
        unguardedBackupDelete(swapped('  StrCpy $iaDeleteShapeCheck "1"', truncating), {
          sitePolicies: DELETE_SITE_POLICIES,
        }),
        [
          {
            line: 17,
            what: 'the sweep has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built',
          },
        ],
        truncating,
      )
    }
    // The tail test runs LogicLib's own block structure (disposition review R6):
    // a branch closing with `${EndIf}` over arms that each jump out ends in a
    // jump like a bare trailing `Goto` does, so it is not reported — and an arm
    // that can fall through still is, `${Switch}`'s `${Case}` dividers included
    // (the text before a switch's first `${Case}` is its discriminant, not a
    // path). A `Goto` carrying a trailing comment ends in a jump too: the
    // comment writes nothing.
    assert.deepEqual(
      unguardedBackupDelete(
        swapped(
          '  SetErrorLevel 2\n  Goto iaSweepDone',
          '  SetErrorLevel 2\n  ${If} $9 == ""\n    Goto iaSweepDone\n  ${Else}\n    Goto iaSweepDone\n  ${EndIf}',
        ),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
    for (const falling of [
      '  SetErrorLevel 2\n  ${If} $9 == ""\n    Goto iaSweepDone\n  ${Else}\n    DetailPrint "kept"\n  ${EndIf}',
      '  SetErrorLevel 2\n  ${Switch} $9\n    ${Case} 1\n      Goto iaSweepDone\n    ${CaseElse}\n      DetailPrint "kept"\n  ${EndSwitch}',
      // A case-less `${Switch}` is the third falling shape, and the one this
      // suite's own reading got wrong: with no `${Case}`/`${CaseElse}` at all
      // there is no arm to run — LogicLib jumps straight to the `${EndSwitch}`
      // label, so the body (its `Goto` included) is dead text and control
      // leaves the block. Measured on 3.0.4.1: the body's marker file was never
      // written while the one after `${EndSwitch}` was (probes t7/t8). Reading
      // the dead tail as the branch's ending jump accepted this fall-through
      // (the fix round's review, F-1 regression: the base guard reported it).
      '  SetErrorLevel 2\n  ${Switch} $9\n    Goto iaSweepDone\n  ${EndSwitch}',
    ]) {
      assert.deepEqual(
        unguardedBackupDelete(swapped('  SetErrorLevel 2\n  Goto iaSweepDone', falling), {
          sitePolicies: DELETE_SITE_POLICIES,
        }),
        [{ line: 6, what: declinedMessage }],
        falling,
      )
    }
    assert.deepEqual(
      unguardedBackupDelete(swapped('  Goto iaSweepDone', '  Goto iaSweepDone ; keep the leftovers'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [],
    )
    // #919 review (S2): the declined branch's exit code is a guarded statement
    // too. Inside a constant-false branch it never runs, and without the
    // dead-branch scan it satisfied the declined-exit rule (measured) — the
    // same hole Q3 closed for the main exit code.
    assert.deepEqual(
      unguardedBackupDelete(
        swapped(
          '  SetErrorLevel 2\n  Goto iaSweepDone',
          '  ${If} 1 == 0\n    SetErrorLevel 2\n  ${EndIf}\n  Goto iaSweepDone',
        ),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [
        {
          line: 19,
          what: 'the constant-false condition `${If} 1 == 0` covers the declined exit code — that code can never run',
        },
      ],
    )
    const armingMessage =
      'the sweep has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built'
    // #919 Q3: the arming has to run on the path the prepare call (and the
    // delete) is on. Inside a branch that closes above the call the flag is
    // never set when the delete runs, and inside the other side of an
    // `${Else}` the call runs without it — the branch handling the prepare and
    // gate rules already have (measured in #919: both passed).
    assert.deepEqual(
      unguardedBackupDelete(
        swapped(
          '  StrCpy $iaDeleteShapeCheck "1"',
          '  ${If} $9 == 0\n    StrCpy $iaDeleteShapeCheck "1"\n  ${EndIf}',
        ),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 19, what: armingMessage }],
    )
    assert.deepEqual(
      unguardedBackupDelete(
        sweep
          .replace('  StrCpy $iaDeleteShapeCheck "1"', '  ${If} $9 == 0\n  StrCpy $iaDeleteShapeCheck "1"\n  ${Else}')
          .replace('iaSweepDone:\n!macroend', 'iaSweepDone:\n  ${EndIf}\n!macroend'),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 19, what: armingMessage }],
    )
    // #919 Q3: the exit code is a guarded statement too. A `SetErrorLevel 2`
    // inside another macro body in the same span does not run at this site, and
    // one inside a constant-false branch never runs at all — both used to
    // satisfy the rule (measured). The guard names its sites by the enclosing
    // block, so the sweep's text-level shape here is a Function: in a `!macro`
    // body the scan reads an inner `!macroend` as the outer close, and this
    // case cannot be written at all.
    const foreignExit = [
      'Function customUnInstall',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  StrCmp $0 "" iaSweepDone',
      '  FindClose $0',
      '  MessageBox MB_OKCANCEL "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDeclined',
      'iaSweepDeclined:',
      '  SetErrorLevel 2',
      '  Goto iaSweepDone',
      'iaSweepDelete:',
      '  ${GetParent} "$INSTDIR" $2',
      '  StrCpy $iaDeleteCandidate "$2\\$1"',
      '  StrCpy $iaDeleteBase "$INSTDIR"',
      '  StrCpy $iaDeleteShapeCheck "1"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaSweepKept',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      '  IfErrors 0 iaSweepNext',
      'iaSweepKept:',
      '  StrCpy $9 ""',
      'iaSweepNext:',
      '  FindClose $0',
      'iaSweepDone:',
      '  !macro iaNotTheSweep',
      '    SetErrorLevel 2',
      '  !macroend',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(foreignExit, { sitePolicies: DELETE_SITE_POLICIES }), [
      { line: 17, what: 'nothing sets the exit code to 2 when leftovers are kept (SetErrorLevel 2)' },
    ])
    assert.deepEqual(
      unguardedBackupDelete(
        sweep
          .replace('iaSweepKept:\n  SetErrorLevel 2', 'iaSweepKept:\n  StrCpy $9 ""')
          .replace('iaSweepNext:', '  ${If} 1 == 0\n    SetErrorLevel 2\n  ${EndIf}\niaSweepNext:'),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [
        {
          line: 17,
          what: 'the constant-false condition `${If} 1 == 0` covers the exit code — that code can never run',
        },
      ],
    )
  })

  it('the promote delete arms the shape check like the sweep does (#919 Q1)', () => {
    // The flag is what makes iaPrepareDelete run the name check, and the promote
    // site's candidate is the other name that has to pass it: $iaBackupDirectory
    // comes from HKCU, and only the ".old-{guid}" shape separates the
    // installer's own backup from any other directory a user-writable value can
    // name. The rule used to gate on the sweep policy alone, so flipping the
    // promote arming to "0" passed the whole guard and suite (measured in #919).
    const promote = [
      'Function iaPromoteApplication',
      '  StrCpy $iaDeleteCandidate "$iaBackupDirectory"',
      '  StrCpy $iaDeleteBase "$iaFinalDirectory"',
      '  StrCpy $iaDeleteShapeCheck "1"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      ...GUARDED_TAIL.map((line) => `  ${line}`),
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(promote), [])
    // Disarming the arming is the only change, and it leaves the target
    // unchecked: whatever name the pointer holds is deleted by name alone.
    assert.deepEqual(unguardedBackupDelete(promote.replace('"1"', '"0"')), [
      {
        line: 8,
        what: 'the delete of a $iaBackupDirectory candidate has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built',
      },
    ])
    // The rollback's candidate is the partial install, a different contract —
    // its deliberate "0" is #904 item 3 — so the rule keys on the candidate
    // rather than on every non-sweep site.
    assert.deepEqual(
      unguardedBackupDelete(
        promote
          .replace('StrCpy $iaDeleteCandidate "$iaBackupDirectory"', 'StrCpy $iaDeleteCandidate "$iaFinalDirectory"')
          .replace('"1"', '"0"'),
      ),
      [],
    )
  })

  it('reads every StrCpy spelling of the candidate and the arming (#919 review F1)', () => {
    // Measured on makensis 3.0.4.1: double quotes, single quotes, backticks and
    // no quotes at all store the same value, so the candidate copy and the
    // arming are the same statement in any of the four forms. The rule knew only
    // the double-quoted spelling, and a site that wrote either line another
    // legal way read as "no candidate, nothing to arm" — the promote fixture
    // below passed with the flag off (measured).
    const spelling = (quote) =>
      [
        'Function iaPromoteApplication',
        `  StrCpy $iaDeleteCandidate ${quote}$iaBackupDirectory${quote}`,
        '  StrCpy $iaDeleteBase "$iaFinalDirectory"',
        `  StrCpy $iaDeleteShapeCheck ${quote}1${quote}`,
        '  Call iaPrepareDelete',
        '  StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped',
        '  ClearErrors',
        `  RMDir /r "$iaDeleteTarget"`,
        ...GUARDED_TAIL.map((line) => `  ${line}`),
        'FunctionEnd',
      ].join('\n')
    for (const quote of ['"', "'", '`', '']) {
      assert.deepEqual(unguardedBackupDelete(spelling(quote)), [], `quote form ${JSON.stringify(quote)}`)
    }
    // The miss the widening closes: both lines single-quoted, flag off — the old
    // match saw no candidate and no assignment, so the delete passed unchecked.
    assert.deepEqual(unguardedBackupDelete(spelling("'").replace("'1'", "'0'")), [
      {
        line: 8,
        what: 'the delete of a $iaBackupDirectory candidate has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built',
      },
    ])
    // The read's other half: $iaFinalDirectory is the one candidate source that
    // may skip the arming (#904 item 3), and that reading only survives while
    // every legal spelling of the copy is recognized. With the double-quoted
    // spelling alone the other three read as a copy the guard cannot recognize —
    // fail-closed — and the rollback's own deliberate "0" is reported as a
    // missing arming.
    const exempt = (quote) =>
      spelling(quote)
        .replace(
          `StrCpy $iaDeleteCandidate ${quote}$iaBackupDirectory${quote}`,
          `StrCpy $iaDeleteCandidate ${quote}$iaFinalDirectory${quote}`,
        )
        .replace(
          `StrCpy $iaDeleteShapeCheck ${quote}1${quote}`,
          `StrCpy $iaDeleteShapeCheck ${quote}0${quote}`,
        )
    for (const quote of ['"', "'", '`', '']) {
      assert.deepEqual(unguardedBackupDelete(exempt(quote)), [], `exempt source spelled with ${JSON.stringify(quote)}`)
    }
    // The insertion spelling of the same bypass (#919 review F1, A4): #905's
    // ruling is to fail closed on macro insertion, and the window is where it
    // matters — an `!insertmacro` on the prepare call's path expands statements
    // the window's text does not show, so a body that writes the flag leaves the
    // delete running disarmed while every line above still reads like an arming.
    const withInsert = (defs, name) =>
      [
        defs.join('\n'),
        spelling('"').replace('  Call iaPrepareDelete', `  !insertmacro ${name}\n  Call iaPrepareDelete`),
      ].join('\n')
    assert.deepEqual(
      unguardedBackupDelete(
        withInsert(
          ['!macro iaForeignDisarm', "  StrCpy $iaDeleteShapeCheck '0'", '!macroend'],
          'iaForeignDisarm',
        ),
      ),
      [
        {
          line: 12,
          what: "!insertmacro iaForeignDisarm at line 8 expands inside the arming window and touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
      ],
    )
    // A nested insertion and an insertion of a macro this text does not define
    // (the include boundary the guard does not follow) are the same unreadable
    // state, so both fail closed.
    assert.deepEqual(
      unguardedBackupDelete(
        withInsert(
          [
            '!macro iaOuterStep',
            '  !insertmacro iaInnerDisarm',
            '!macroend',
            '!macro iaInnerDisarm',
            '  StrCpy $iaDeleteShapeCheck "0"',
            '!macroend',
          ],
          'iaOuterStep',
        ),
      ),
      [
        {
          line: 15,
          what: "!insertmacro iaOuterStep at line 11 expands inside the arming window and touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
      ],
    )
    assert.deepEqual(
      unguardedBackupDelete(
        spelling('"').replace('  Call iaPrepareDelete', '  !insertmacro iaFromAnInclude\n  Call iaPrepareDelete'),
      ),
      [
        {
          line: 9,
          what: "!insertmacro iaFromAnInclude at line 5 expands inside the arming window and touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
      ],
    )
    // The rule is keyed to what the body writes, not to insertions as such: the
    // shipped rollback window inserts iaClearBackupDir (a DeleteRegValue) above
    // its prepare call and has to stay green.
    assert.deepEqual(
      unguardedBackupDelete(
        withInsert(
          ['!macro iaClearBackupDirLike', '  DeleteRegValue HKCU "Software\\\\x" "IaBackupDir"', '!macroend'],
          'iaClearBackupDirLike',
        ),
      ),
      [],
    )
    // A copy or a value the guard cannot read is not evidence of anything: a
    // register source may hold the backup path or the disarm, so both fail
    // closed (#919 review F1: these register spellings passed the whole guard
    // before — measured).
    assert.deepEqual(
      unguardedBackupDelete(
        spelling('"').replace('  Call iaPrepareDelete', '  StrCpy $iaDeleteShapeCheck $0\n  Call iaPrepareDelete'),
      ),
      [
        {
          line: 9,
          what: 'the delete of a $iaBackupDirectory candidate has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built',
        },
      ],
    )
    assert.deepEqual(
      unguardedBackupDelete(spelling('"').replace('"$iaBackupDirectory"', '$0').replace('"1"', '"0"')),
      [
        {
          line: 8,
          what: 'the delete of a $iaBackupDirectory candidate has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built',
        },
      ],
    )
    // A copy line the guard cannot even read (a trailing operand makes it a
    // different statement) is the same fail-closed direction, not "no copy".
    assert.deepEqual(
      unguardedBackupDelete(
        spelling('"')
          .replace('"$iaBackupDirectory"', '"$iaBackupDirectory" 3')
          .replace('"1"', '"0"'),
      ),
      [
        {
          line: 8,
          what: 'the delete of a $iaBackupDirectory candidate has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built',
        },
      ],
    )
    // #919 disposition review (N1): the source reading is the whole window, not
    // its last line. Two branch-divergent copies can each be the one the taken
    // branch wrote, so the delete's candidate is not the exempt
    // "$iaFinalDirectory" just because that copy came last — reading only the
    // last copy passed this shape with the flag off (measured), which is the
    // Q1 bypass the widened spellings exist to keep closed.
    const branchDivergent = [
      'Function iaPromoteApplication',
      '  ${If} $0 == "1"',
      '    StrCpy $iaDeleteCandidate "$iaBackupDirectory"',
      '  ${Else}',
      '    StrCpy $iaDeleteCandidate "$iaFinalDirectory"',
      '  ${EndIf}',
      '  StrCpy $iaDeleteShapeCheck "0"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      ...GUARDED_TAIL.map((line) => `  ${line}`),
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(branchDivergent), [
      {
        line: 11,
        what: 'the delete of a $iaBackupDirectory candidate has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built',
      },
    ])
    // #919 disposition review (R1): the state the delete runs with is not only
    // what the two copies say. `Pop`, `IntOp` or a registry read can write
    // either variable after an otherwise green window, and only lines matching
    // the two `StrCpy` prefixes were read at all — so a legal instruction
    // undid the arming (or re-sourced the candidate) invisibly. Any other
    // statement on the window's path that uses one of the variables is
    // reported; a mention inside a string is not, because string contents are
    // blanked first (the disposition review's N3/R4 negative).
    const withStatement = (statement) =>
      spelling('"').replace('  Call iaPrepareDelete', `  ${statement}\n  Call iaPrepareDelete`)
    const blanketMessage =
      'a statement at line 5 uses $iaDeleteCandidate or $iaDeleteShapeCheck outside the two copies the guard reads (StrCpy $iaDeleteCandidate <source>, StrCpy $iaDeleteShapeCheck "[01]") — the state the delete runs with cannot be read from this window (fail-closed)'
    assert.deepEqual(unguardedBackupDelete(withStatement('Pop $iaDeleteShapeCheck')), [
      { line: 9, what: blanketMessage },
    ])
    assert.deepEqual(
      unguardedBackupDelete(
        spelling('"')
          .replace('"$iaBackupDirectory"', '"$iaFinalDirectory"')
          .replace('"1"', '"0"')
          .replace(
            '  Call iaPrepareDelete',
            '  ReadRegStr $iaDeleteCandidate HKCU "Software\\\\x" "IaBackupDir"\n  Call iaPrepareDelete',
          ),
      ),
      [{ line: 9, what: blanketMessage }],
    )
    assert.deepEqual(
      unguardedBackupDelete(withStatement('DetailPrint "arming state: $iaDeleteShapeCheck"')),
      [],
    )
    // #919 disposition review (R2): the insertion rule follows `${define}`
    // expansions and an `!include` too — a define can carry the insertion or
    // the write itself, and the literal-`!insertmacro` rule alone read them as
    // clean. Measured on makensis 3.0.4.1: the carried insertion does expand,
    // and its body then sits inside the value's own quotes, which is a compile
    // error rather than a silent disarm — the guard reads the spelling anyway
    // (fail-closed, one quote form away from compiling).
    const withUse = (defs, use) =>
      [
        defs.join('\n'),
        spelling('"').replace('  Call iaPrepareDelete', `  ${use}\n  Call iaPrepareDelete`),
      ].join('\n')
    assert.deepEqual(
      unguardedBackupDelete(
        withUse(
          [
            '!macro iaDisarmShape',
            '  StrCpy $iaDeleteShapeCheck "0"',
            '!macroend',
            '!define IA_RUN_HIDDEN "!insertmacro iaDisarmShape"',
          ],
          '${IA_RUN_HIDDEN}',
        ),
      ),
      [
        {
          line: 13,
          what: "${IA_RUN_HIDDEN} at line 9 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
      ],
    )
    assert.deepEqual(
      unguardedBackupDelete(
        withUse(["!define IA_DISARM_NOW \"StrCpy $iaDeleteShapeCheck '0'\""], '${IA_DISARM_NOW}'),
      ),
      [
        {
          line: 10,
          what: "${IA_DISARM_NOW} at line 6 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
      ],
    )
    assert.deepEqual(unguardedBackupDelete(withUse([], '!include ia-extra.nsh')), [
      {
        line: 10,
        what: '!include at line 6 sits in the arming window — the included text is not read here (round-5 item 4) and can write the candidate or the flag (fail-closed)',
      },
    ])
    // A define that carries no statement and no insertion is not that state:
    // the rule keys on what the expanded text touches, not on `${}` as such.
    assert.deepEqual(
      unguardedBackupDelete(withUse(['!define IA_PROMPT "old leftovers?"'], 'MessageBox MB_OK "${IA_PROMPT}"')),
      [],
    )
    // #919 fix-round review (F3): dot- and dash-spelled define names are legal
    // on makensis 3.0.4.1 (`!define IA.X "…"` + `${IA.X}` compiles and expands,
    // measured), and the token used to read `${…}` only took word characters —
    // so the same carried state was invisible under those spellings.
    assert.deepEqual(
      unguardedBackupDelete(
        withUse(['!define IA.DISARM "!insertmacro iaDisarmShape"'], '${IA.DISARM}'),
      ),
      [
        {
          line: 10,
          what: "${IA.DISARM} at line 6 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
      ],
    )
    // The dash-spelled twin of the case above, because the comment above claims
    // both spellings (the fix round's review, F-6: the claim had no assertion).
    assert.deepEqual(
      unguardedBackupDelete(
        withUse(['!define IA-DISARM "!insertmacro iaDisarmShape"'], '${IA-DISARM}'),
      ),
      [
        {
          line: 10,
          what: "${IA-DISARM} at line 6 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
      ],
    )
  })

  it('reads the label declarations the sweep branch lands on (#919 fix round F-1/N1/F-2)', () => {
    // A label is a position, and NSIS 3.0.4.1 accepts the statement on the
    // label's own line — in a Section, in a `!macro` body and at a loop head
    // (measured: `Hop: Goto Target` and a chain `L1: L2: Goto X` both compiled
    // and jumped). Reading only the stand-alone spelling was the review's F-1:
    // an inline-declared declined label was invisible, so the whole declined
    // contract went unchecked for it, and an inline-declared hop into the
    // delete pass laundered the reach check. A jump target may also be quoted —
    // `Goto "L"` and `IfErrors 0 "L"` compile and jump (measured) — which the
    // token scan used to blank away (N1).
    const sweepLoop = [
      '!macro customUnInstall',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  StrCmp $0 "" iaSweepDone',
      '  MessageBox MB_OKCANCEL "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDeclined',
      'iaSweepDeclined:',
      '  SetErrorLevel 2',
      '  Goto iaSweepDone',
      'iaSweepDelete:',
      '  ${GetParent} "$INSTDIR" $2',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  StrCmp $0 "" iaSweepDone',
      'iaSweepLoop:',
      '  StrCmp $1 "" iaSweepLoopEnd',
      '  StrCpy $iaDeleteCandidate "$2\\$1"',
      '  StrCpy $iaDeleteBase "$INSTDIR"',
      '  StrCpy $iaDeleteShapeCheck "1"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaSweepKept',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      '  IfErrors 0 iaSweepNext',
      '  StrCpy $iaDeleteStatus "failed"',
      'iaSweepKept:',
      '  SetErrorLevel 2',
      'iaSweepNext:',
      '  FindNext $0 $1',
      '  Goto iaSweepLoop',
      'iaSweepLoopEnd:',
      '  FindClose $0',
      'iaSweepDone:',
      '!macroend',
    ].join('\n')
    const loopSwapped = (from, to) => sweepLoop.replace(from, to)
    assert.deepEqual(unguardedBackupDelete(sweepLoop, { sitePolicies: DELETE_SITE_POLICIES }), [])
    const declinedMessage =
      'the declined sweep branch can reach the delete pass, or does not end in a jump out of it — a declined sweep must only keep'
    const declinedExitMessage =
      'the declined sweep branch does not set the exit code to 2 — a declined sweep reports success'
    // The inline label with its statement read: the same branch as the shipped
    // one, spelled on the label's line.
    assert.deepEqual(
      unguardedBackupDelete(
        loopSwapped(
          'iaSweepDeclined:\n  SetErrorLevel 2\n  Goto iaSweepDone',
          'iaSweepDeclined: SetErrorLevel 2\n  Goto iaSweepDone',
        ),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
    // The bypass the old reading let through: with the label invisible there
    // was no declined branch to check, so a declined sweep carrying no exit
    // code at all read as clean.
    assert.deepEqual(
      unguardedBackupDelete(
        loopSwapped(
          'iaSweepDeclined:\n  SetErrorLevel 2\n  Goto iaSweepDone',
          'iaSweepDeclined: DetailPrint "kept $9"\n  Goto iaSweepDone',
        ),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 5, what: declinedExitMessage }],
    )
    // An inline hop whose own body enters the pass, and the same hop kept out
    // of it — the F-1 reach reading, both directions.
    const withHop = (hop) =>
      loopSwapped('  Goto iaSweepDone', '  Goto iaSweepHop').replace('!macroend', `${hop}\n!macroend`)
    assert.deepEqual(
      unguardedBackupDelete(withHop('iaSweepHop: Goto iaSweepKept'), { sitePolicies: DELETE_SITE_POLICIES }),
      [{ line: 5, what: declinedMessage }],
    )
    // Gap A-1 (this round's own probe): a label name's measured alphabet is a
    // letter, `_`, `.`, `%` or `@` to start and letters, digits, `_`, `.`, `+`,
    // `!`, `%`, `#`, `@`, `$`, `-` or `:` inside (measured on 3.0.4.1, probes
    // n8/n9, re-measured this round), so the declaration read takes that class.
    // With the narrow class the dashed hop was not a declaration at
    // all, so `Goto iaSweepHop-A` resolved to nothing and this same reach into
    // the delete pass read clean where its plain-name twin reports.
    assert.deepEqual(
      unguardedBackupDelete(
        withHop('iaSweepHop-A: Goto iaSweepKept').replace('  Goto iaSweepHop', '  Goto iaSweepHop-A'),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 5, what: declinedMessage }],
    )
    assert.deepEqual(
      unguardedBackupDelete(withHop('iaSweepHop: Goto iaSweepLoopEnd'), { sitePolicies: DELETE_SITE_POLICIES }),
      [],
    )
    // A chain declares two names at one position: the jump to the second one is
    // the same jump, kept out of the pass here and into it in the next case.
    assert.deepEqual(
      unguardedBackupDelete(
        withHop('iaSweepHopA: iaSweepHopB: Goto iaSweepLoopEnd').replace('  Goto iaSweepHop', '  Goto iaSweepHopB'),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
    assert.deepEqual(
      unguardedBackupDelete(
        withHop('iaSweepHopA: iaSweepHopB: Goto iaSweepKept').replace('  Goto iaSweepHop', '  Goto iaSweepHopB'),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 5, what: declinedMessage }],
    )
    // Quoted targets (N1): the delete pass and its loop head are both reached
    // through the quoted spelling, and the kept label through it stays kept.
    assert.deepEqual(
      unguardedBackupDelete(loopSwapped('  Goto iaSweepDone', '  Goto "iaSweepKept"'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 5, what: declinedMessage }],
    )
    assert.deepEqual(
      unguardedBackupDelete(loopSwapped('  Goto iaSweepDone', '  Goto "iaSweepLoop"'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 5, what: declinedMessage }],
    )
    assert.deepEqual(
      unguardedBackupDelete(loopSwapped('  Goto iaSweepDone', '  Goto "iaSweepDone"'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [],
    )
    // A jump to a label this block does not declare but the file does (F-2):
    // sibling `!macro` bodies share the label namespace (measured: a cross-body
    // `Goto` compiled and jumped), so the target's own body can enter the pass
    // beyond this block's text — fail-closed. A target no label in the file
    // declares cannot compile at all, so it is not reported here.
    const sibling = (name) => `!macro iaElsewhere\n${name}:\n  DetailPrint "kept"\n!macroend\n`
    assert.deepEqual(
      unguardedBackupDelete(sibling('iaSweepElsewhere') + loopSwapped('  Goto iaSweepDone', '  Goto iaSweepElsewhere'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [
        {
          line: 9,
          what: 'the declined sweep branch references iasweepelsewhere, a label the file declares outside this block — where that jump lands cannot be read here (fail-closed)',
        },
      ],
    )
    assert.deepEqual(
      unguardedBackupDelete(loopSwapped('  Goto iaSweepDone', '  Goto iaSweepNowhere'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [],
    )
  })

  it('reads the quoted-output copy and the arming operands (#919 fix round N2/R5)', () => {
    // Measured on makensis 3.0.4.1 with declared variables: `StrCpy` stores the
    // same value when its output is quoted (`StrCpy "$vFlag" "0"` wrote the 0 —
    // probe r9), and its optional `[maxlen] [startoffset]` decide what is left
    // of the value (`"1" 1`, `"1" 2` and `"1" 1 0` store "1"; `"1" 0` and
    // `"1" 2 1` store "" — probe r6). Reading only the bare-output,
    // two-operand spelling was the review's N2 (a quoted-output copy hid the
    // candidate, which skipped the arming requirement entirely) and the
    // disposition review's R5 (a maxlen that arms was reported as no arming).
    const promote = [
      'Function iaPromoteApplication',
      '  StrCpy $iaDeleteCandidate "$iaBackupDirectory"',
      '  StrCpy $iaDeleteBase "$iaFinalDirectory"',
      '  StrCpy $iaDeleteShapeCheck "1"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      ...GUARDED_TAIL.map((line) => `  ${line}`),
      'FunctionEnd',
    ].join('\n')
    const armingMessage =
      'the delete of a $iaBackupDirectory candidate has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built'
    assert.deepEqual(unguardedBackupDelete(promote), [])
    const quoted = (from, to) => promote.replace(from, to)
    // The quoted-output copy now counts as a copy of the backup name, so the
    // site is held to a readable arming — and the quoted-output disarm is that
    // arming, read as "0": before, both lines were invisible and the delete ran
    // unchecked.
    assert.deepEqual(
      unguardedBackupDelete(
        quoted('StrCpy $iaDeleteCandidate "$iaBackupDirectory"', 'StrCpy "$iaDeleteCandidate" "$iaBackupDirectory"').replace(
          'StrCpy $iaDeleteShapeCheck "1"',
          'StrCpy "$iaDeleteShapeCheck" "0"',
        ),
      ),
      [{ line: 8, what: armingMessage }],
    )
    // The same two quoted spellings with the flag on are the same armed window,
    // and a quoted-output copy whose source the scan cannot read is still a copy
    // — the arming is what protects the delete then (fail-closed).
    assert.deepEqual(
      unguardedBackupDelete(
        quoted('StrCpy $iaDeleteCandidate "$iaBackupDirectory"', 'StrCpy "$iaDeleteCandidate" "$iaBackupDirectory"').replace(
          'StrCpy $iaDeleteShapeCheck "1"',
          'StrCpy "$iaDeleteShapeCheck" "1"',
        ),
      ),
      [],
    )
    assert.deepEqual(
      unguardedBackupDelete(quoted('StrCpy $iaDeleteCandidate "$iaBackupDirectory"', 'StrCpy "$iaDeleteCandidate" $0')),
      [],
    )
    // A quoted output whose *value* cannot be read is not an arming, and a
    // quoted `$var` parameter of another instruction is the same write one
    // quote form away (measured: `Pop "$vA"` after `Push "hidden"` stored it,
    // and `IntOp "$vB" 2 + 3` stored 5 — probes w3/w2), so both report.
    assert.deepEqual(
      unguardedBackupDelete(
        quoted('StrCpy $iaDeleteShapeCheck "1"', 'StrCpy "$iaDeleteShapeCheck" $0'),
      ),
      [{ line: 8, what: armingMessage }],
    )
    assert.deepEqual(
      unguardedBackupDelete(
        quoted('  Call iaPrepareDelete', '  Pop "$iaDeleteShapeCheck"\n  Call iaPrepareDelete'),
      ),
      [
        {
          line: 9,
          what: 'a statement at line 5 uses $iaDeleteCandidate or $iaDeleteShapeCheck outside the two copies the guard reads (StrCpy $iaDeleteCandidate <source>, StrCpy $iaDeleteShapeCheck "[01]") — the state the delete runs with cannot be read from this window (fail-closed)',
        },
      ],
    )
  })

  it('pairs nested tokens and joins continued defines in the window scans (#919 fix round F-3/N3)', () => {
    // Measured on makensis 3.0.4.1 (probe r5): `!define IA.SEL "IA.DISARM"` +
    // `${${IA.SEL}}` with `!define IA.DISARM "StrCpy $iaDeleteShapeCheck 0"`
    // expands to the disarm and executes it, and a `!define` value continued
    // with `\` compiles and its `${NAME}` insertion executes (probe u4). The
    // old token reading stopped at the first `}` — it saw `${${IA.SEL` and no
    // map holds that name, so the disarm counted as clean (the review's F-3) —
    // and the single-line define body cut a continued value at the backslash
    // (N3).
    const promote = [
      'Function iaPromoteApplication',
      '  StrCpy $iaDeleteCandidate "$iaBackupDirectory"',
      '  StrCpy $iaDeleteBase "$iaFinalDirectory"',
      '  StrCpy $iaDeleteShapeCheck "1"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      ...GUARDED_TAIL.map((line) => `  ${line}`),
      'FunctionEnd',
    ].join('\n')
    const armingMessage =
      'the delete of a $iaBackupDirectory candidate has no readable StrCpy $iaDeleteShapeCheck "1" above its prepare call — an arming whose value, maxlen or offset truncates the flag, or whose shape this scan cannot read, counts as not armed (fail-closed), and iaPrepareDelete then skips the name check and deletes whatever name the caller built'
    const withDefs = (defs, use) =>
      [defs.join('\n'), promote.replace('StrCpy $iaDeleteShapeCheck "1"', use)].join('\n')
    const disarm = ['!define IA.SEL "IA.DISARM"', '!define IA.DISARM "StrCpy $iaDeleteShapeCheck 0"']
    // The hole F-3 closed first, told apart from the arming rule's own report:
    // with an explicit arming above, the nested token is the only thing that
    // disarms — reading the token as `${${IA.SEL` (a name no map holds) left
    // this window green and the delete ran disarmed.
    assert.deepEqual(
      unguardedBackupDelete([
        disarm.join('\n'),
        promote.replace('StrCpy $iaDeleteShapeCheck "1"', 'StrCpy $iaDeleteShapeCheck "1"\n  ${${IA.SEL}}'),
      ].join('\n')),
      [
        {
          line: 11,
          what: "${${IA.SEL}} at line 7 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
      ],
    )
    // The hole N3 closed: the write may sit entirely on a `\`-continued line
    // (`!define D "\` + `StrCpy $iaDeleteShapeCheck 0"` compiled and ran the
    // statement — measured, probe w4), and the single-line reading kept only
    // the backslash, so this window read as clean with the token as its only
    // disarm.
    assert.deepEqual(
      unguardedBackupDelete([
        '!define D "\\',
        'StrCpy $iaDeleteShapeCheck 0"',
        promote.replace('StrCpy $iaDeleteShapeCheck "1"', 'StrCpy $iaDeleteShapeCheck "1"\n  ${D}'),
      ].join('\n')),
      [
        {
          line: 11,
          what: '${D} at line 7 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window\'s text (fail-closed)',
        },
      ],
    )
    // The nested token, resolved the way the preprocessor resolves it.
    assert.deepEqual(unguardedBackupDelete(withDefs(disarm, '${${IA.SEL}}')), [
      {
        line: 10,
        what: "${${IA.SEL}} at line 6 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
      },
      { line: 10, what: armingMessage },
    ])
    // Gap A-2: the inner *value* may carry a dash too — measured on 3.0.4.1,
    // `!define IA.SEL "IA-DISARM"` + `!define IA-DISARM "StrCpy
    // $iaDeleteShapeCheck 0"` expands and runs the disarm (probe n6), where
    // the value-name tests' old `[A-Za-z0-9_.]` class resolved the token back
    // to the inner name (no map holds it as a write) and read this window
    // clean — the dashed twin of the case above, with the same readings.
    assert.deepEqual(
      unguardedBackupDelete(
        withDefs(['!define IA.SEL "IA-DISARM"', '!define IA-DISARM "StrCpy $iaDeleteShapeCheck 0"'], '${${IA.SEL}}'),
      ),
      [
        {
          line: 10,
          what: "${${IA.SEL}} at line 6 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
        { line: 10, what: armingMessage },
      ],
    )
    // The same carried state one define further out, and inside a macro body
    // the window inserts.
    assert.deepEqual(
      unguardedBackupDelete(withDefs([...disarm, '!define IA.OUTER "${${IA.SEL}}"'], '${IA.OUTER}')),
      [
        {
          line: 11,
          what: '${IA.OUTER} at line 7 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window\'s text (fail-closed)',
        },
        { line: 11, what: armingMessage },
      ],
    )
    assert.deepEqual(
      unguardedBackupDelete(
        withDefs(
          ['!macro iaNested', ...disarm.map((line) => `  ${line}`), '  ${${IA.SEL}}', '!macroend'],
          '!insertmacro iaNested',
        ),
      ),
      [
        {
          line: 13,
          what: '!insertmacro iaNested at line 9 expands inside the arming window and touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window\'s text (fail-closed)',
        },
        { line: 13, what: armingMessage },
      ],
    )
    // The pre-existing reading of the same carried state: a single-line define
    // value that names the variable inside a multi-line statement.
    assert.deepEqual(
      unguardedBackupDelete(withDefs(['!define D "StrCpy $iaDeleteShapeCheck \\', '0"'], '${D}')),
      [
        {
          line: 10,
          what: '${D} at line 6 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window\'s text (fail-closed)',
        },
        { line: 10, what: armingMessage },
      ],
    )
    assert.deepEqual(
      unguardedBackupDelete(withDefs(['!define D "StrCpy $iaDeleteShapeCheck 0"'], '${D}')),
      [
        {
          line: 9,
          what: '${D} at line 5 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window\'s text (fail-closed)',
        },
        { line: 9, what: armingMessage },
      ],
    )
    // The pairing reads braces, not names: a token whose inner name is defined
    // nowhere resolves to nothing here and is harmless text — only the arming
    // rule fires (its line is the arming).
    assert.deepEqual(unguardedBackupDelete(withDefs(['!define IA.HARMLESS "DetailPrint \\"x\\""'], '${${NOPE}}')), [
      { line: 9, what: armingMessage },
    ])
  })

  it('follows the failed delete to the kept accounting (#919 fix round Q9 follow-up)', () => {
    // Measured gap (the fix round's own probe, before the rule below): the
    // ${Errors} rule pins that the delete's own error is *read*, not where the
    // failure path goes. A `Goto iaSweepNext` between the failure-branch status
    // write and the kept label kept the whole suite green — every failed delete
    // skipped the kept accounting, so a sweep whose deletes all failed reported
    // nothing and left the exit code at its 0 default. That is the silent
    // direction of the kept contract, and it is the path a UNC target takes
    // (measured: `RMDir /r` refuses the `\\?\UNC\` spelling iaBuildLongPath
    // builds, so the failure branch is all a UNC sweep can reach).
    const sweepKeep = [
      '!macro customUnInstall',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  StrCmp $0 "" iaSweepDone',
      '  MessageBox MB_OKCANCEL "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDeclined',
      'iaSweepDeclined:',
      '  SetErrorLevel 2',
      '  Goto iaSweepDone',
      'iaSweepDelete:',
      '  ${GetParent} "$INSTDIR" $2',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  StrCmp $0 "" iaSweepDone',
      'iaSweepLoop:',
      '  StrCmp $1 "" iaSweepLoopEnd',
      '  StrCpy $iaDeleteCandidate "$2\\$1"',
      '  StrCpy $iaDeleteBase "$INSTDIR"',
      '  StrCpy $iaDeleteShapeCheck "1"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaSweepKept',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      '  IfErrors 0 iaSweepNext',
      '  StrCpy $iaDeleteStatus "failed"',
      'iaSweepKept:',
      '  SetErrorLevel 2',
      'iaSweepNext:',
      '  FindNext $0 $1',
      '  Goto iaSweepLoop',
      'iaSweepLoopEnd:',
      '  FindClose $0',
      'iaSweepDone:',
      '!macroend',
    ].join('\n')
    const keepSwapped = (from, to) => sweepKeep.replace(from, to)
    const failedMessage =
      "the failed delete's path does not reach the kept accounting — it jumps away, loops back, or leaves the block before the label the status gate jumps to (or a counter bump this scan can read), so a failed delete is neither counted nor reported: the sweep can finish with the leftovers on disk and the exit code still at 0"
    assert.deepEqual(unguardedBackupDelete(sweepKeep, { sitePolicies: DELETE_SITE_POLICIES }), [])
    // The measured hole itself: the failure path jumps past the kept label.
    assert.deepEqual(
      unguardedBackupDelete(
        keepSwapped(
          '  StrCpy $iaDeleteStatus "failed"\niaSweepKept:',
          '  StrCpy $iaDeleteStatus "failed"\n  Goto iaSweepNext\niaSweepKept:',
        ),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 20, what: failedMessage }],
    )
    // Leaving the sweep entirely is the same silent outcome: `Goto iaSweepDone`
    // is the exit that runs when `$9` never moved.
    assert.deepEqual(
      unguardedBackupDelete(
        keepSwapped(
          '  StrCpy $iaDeleteStatus "failed"\niaSweepKept:',
          '  StrCpy $iaDeleteStatus "failed"\n  Goto iaSweepDone\niaSweepKept:',
        ),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 20, what: failedMessage }],
    )
    // A terminal statement on the failure path ends it before the counter too.
    assert.deepEqual(
      unguardedBackupDelete(
        keepSwapped(
          '  StrCpy $iaDeleteStatus "failed"\niaSweepKept:',
          '  StrCpy $iaDeleteStatus "failed"\n  Return\niaSweepKept:',
        ),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 20, what: failedMessage }],
    )
    // The forms that do keep: the explicit jump to the same label, and a sweep
    // that keeps its own books with a counter bump of its own.
    assert.deepEqual(
      unguardedBackupDelete(
        keepSwapped(
          '  StrCpy $iaDeleteStatus "failed"',
          '  StrCpy $iaDeleteStatus "failed"\n  Goto iaSweepKept',
        ),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
    assert.deepEqual(
      unguardedBackupDelete(
        keepSwapped(
          '  StrCpy $iaDeleteStatus "failed"',
          '  StrCpy $iaDeleteStatus "failed"\n  IntOp $9 $9 + 1\n  Goto iaSweepNext',
        ),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
    // The read rule owns the missing-read report; this rule must not add a
    // second problem to the same line.
    assert.deepEqual(
      unguardedBackupDelete(keepSwapped('  IfErrors 0 iaSweepNext', '  StrCpy $0 ""'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 20, what: "missing ${Errors} check before the end of the delete's block" }],
    )
  })

  it('reads the one-argument IfErrors form as the failure leg (#919 gap A-3)', () => {
    // The raw read the rules accept has two spellings, and they jump opposite
    // ways: `IfErrors 0 label` jumps when there is *no* error (so the
    // fall-through is the failure path), while the one-argument
    // `IfErrors label` jumps *on* error (the jump is the failure path). The
    // walk used to read the fall-through either way, which is wrong in both
    // directions — measured before the fix (a one-argument sweep compiles:
    // probe n10): a failure path that jumps straight past the kept count
    // stayed green, and a correct one that jumps *to* the kept branch was
    // reported.
    const oneArgSweep = (read, middle) =>
      [
        '!macro customUnInstall',
        '  FindFirst $0 $1 "$INSTDIR.old-*"',
        '  StrCmp $0 "" iaSweepDone',
        '  MessageBox MB_OKCANCEL "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDeclined',
        'iaSweepDeclined:',
        '  SetErrorLevel 2',
        '  Goto iaSweepDone',
        'iaSweepDelete:',
        '  ${GetParent} "$INSTDIR" $2',
        '  FindFirst $0 $1 "$INSTDIR.old-*"',
        '  StrCmp $0 "" iaSweepDone',
        'iaSweepLoop:',
        '  StrCmp $1 "" iaSweepLoopEnd',
        '  StrCpy $iaDeleteCandidate "$2\\$1"',
        '  StrCpy $iaDeleteBase "$INSTDIR"',
        '  StrCpy $iaDeleteShapeCheck "1"',
        '  Call iaPrepareDelete',
        '  StrCmp $iaDeleteStatus "ok" 0 iaSweepKept',
        '  ClearErrors',
        `  RMDir /r "$iaDeleteTarget"`,
        `  ${read}`,
        ...middle,
        'iaSweepKept:',
        '  SetErrorLevel 2',
        'iaSweepNext:',
        '  FindNext $0 $1',
        '  Goto iaSweepLoop',
        'iaSweepLoopEnd:',
        '  FindClose $0',
        'iaSweepDone:',
        '!macroend',
      ].join('\n')
    const failedMessage =
      "the failed delete's path does not reach the kept accounting — it jumps away, loops back, or leaves the block before the label the status gate jumps to (or a counter bump this scan can read), so a failed delete is neither counted nor reported: the sweep can finish with the leftovers on disk and the exit code still at 0"
    // The silent direction: on error the read jumps to `iaSweepNext` — past
    // the kept count — and only the success fall-through reaches the kept
    // label. Reading the fall-through called this path kept.
    assert.deepEqual(
      unguardedBackupDelete(oneArgSweep('IfErrors iaSweepNext', ['  StrCpy $iaDeleteStatus "failed"']), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 20, what: failedMessage }],
    )
    // The over-report direction: a correct one-argument sweep — the failure
    // leg jumps to a label that records "failed" and falls into the kept
    // accounting, the success leg jumps to the next entry — reads clean.
    assert.deepEqual(
      unguardedBackupDelete(
        oneArgSweep('IfErrors iaSweepFail', [
          '  Goto iaSweepNext',
          'iaSweepFail:',
          '  StrCpy $iaDeleteStatus "failed"',
        ]),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
  })

  it("reads the failure leg the read's own form names (#919 review P1)", () => {
    // The walk used to seed at the read and follow the fall-through whatever
    // the read said. `IfErrors label` jumps *on* error, so the leg it walked
    // was the success path (gap A-3's fixtures cover that silent direction),
    // and the `${IfNot}`/`${Unless}` `${Errors}` spellings put the failure leg
    // in the `${Else}` arm. The review's four legal spellings (P1) are
    // `IfErrors 0 label` (the fall-through), `IfErrors label` and `IfErrors
    // label 0` (the target), `${If} ${Errors}` (its own arm) and `${IfNot}` /
    // `${Unless} ${Errors}` (the `${Else}` arm). The cases below pin each
    // reading, the dead-branch exclusion, the divider continuation, the
    // chain's last name and the counter's alphabet.
    const sweep = (read) =>
      [
        '!macro customUnInstall',
        '  FindFirst $0 $1 "$INSTDIR.old-*"',
        '  StrCmp $0 "" iaSweepDone',
        '  MessageBox MB_OKCANCEL "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDeclined',
        'iaSweepDeclined:',
        '  SetErrorLevel 2',
        '  Goto iaSweepDone',
        'iaSweepDelete:',
        '  ${GetParent} "$INSTDIR" $2',
        '  FindFirst $0 $1 "$INSTDIR.old-*"',
        '  StrCmp $0 "" iaSweepDone',
        'iaSweepLoop:',
        '  StrCmp $1 "" iaSweepLoopEnd',
        '  StrCpy $iaDeleteCandidate "$2\\$1"',
        '  StrCpy $iaDeleteBase "$INSTDIR"',
        '  StrCpy $iaDeleteShapeCheck "1"',
        '  Call iaPrepareDelete',
        '  StrCmp $iaDeleteStatus "ok" 0 iaSweepKept',
        '  ClearErrors',
        `  RMDir /r "$iaDeleteTarget"`,
        ...read,
        'iaSweepKept:',
        '  SetErrorLevel 2',
        'iaSweepNext:',
        '  FindNext $0 $1',
        '  Goto iaSweepLoop',
        'iaSweepLoopEnd:',
        '  FindClose $0',
        'iaSweepDone:',
        '!macroend',
      ].join('\n')
    const failedMessage =
      "the failed delete's path does not reach the kept accounting — it jumps away, loops back, or leaves the block before the label the status gate jumps to (or a counter bump this scan can read), so a failed delete is neither counted nor reported: the sweep can finish with the leftovers on disk and the exit code still at 0"
    const forkedMessage =
      "the failed delete's path leaves the read through an arm this scan cannot read — a second divider (`${ElseIf}`) forks the failure leg, so whether a failed delete is counted or reported cannot be read here (fail-closed)"
    // `${IfNot} ${Errors}`: the failure leg is the `${Else}` arm, and its bump
    // is the only kept accounting — the then-arm (the success path) jumps out,
    // so reading the fall-through finds nothing and reports where the sweep is
    // in fact correct.
    assert.deepEqual(
      unguardedBackupDelete(
        sweep([
          '  ${IfNot} ${Errors}',
          '    Goto iaSweepDone',
          '  ${Else}',
          '    IntOp $9 $9 + 1',
          '  ${EndIf}',
        ]),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
    // `IfErrors label 0`: the error leg is the target and the trailing `0` is
    // the no-error leg, so it reads like the one-argument form.
    assert.deepEqual(
      unguardedBackupDelete(sweep(['  IfErrors iaSweepKept 0', '  Goto iaSweepDone']), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [],
    )
    // An arm a second divider forks cannot be read: the report names the
    // forking line instead of guessing which arm of the fork runs (the
    // fail-closed direction).
    assert.deepEqual(
      unguardedBackupDelete(
        sweep([
          '  ${IfNot} ${Errors}',
          '    Goto iaSweepDone',
          '  ${ElseIf} $0 == ""',
          '    IntOp $9 $9 + 1',
          '  ${EndIf}',
        ]),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 23, what: forkedMessage }],
    )
    // A bump in a branch that cannot run is not the kept accounting: the dead
    // arm is skipped, so the counter never moves on this path.
    assert.deepEqual(
      unguardedBackupDelete(
        sweep([
          '  IfErrors 0 iaSweepNext',
          '  StrCpy $iaDeleteStatus "failed"',
          '  ${If} 1 == 0',
          '    IntOp $9 $9 + 1',
          '  ${EndIf}',
          '  Goto iaSweepDone',
        ]),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 20, what: failedMessage }],
    )
    // Reaching an `${Else}` from inside its own block means the block's end
    // (LogicLib jumps to after the `${EndIf}`), so the failure leg does not run
    // the sibling arm: the sibling's bump keeps, and a leg that does not may
    // not borrow it.
    assert.deepEqual(
      unguardedBackupDelete(
        sweep([
          '  IfErrors 0 iaSweepNext',
          '  StrCpy $iaDeleteStatus "failed"',
          '  ${If} $0 == ""',
          '    StrCpy $iaDeleteStatus "kept"',
          '  ${Else}',
          '    IntOp $9 $9 + 1',
          '  ${EndIf}',
          '  Goto iaSweepDone',
        ]),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [{ line: 20, what: failedMessage }],
    )
    // A chain declares its names at one position, so the statement is what
    // follows the *last* name: the hop below jumps to the kept label, while
    // reading the first name's rest as the statement made it look like the leg
    // fell out of the sweep.
    assert.deepEqual(
      unguardedBackupDelete(
        sweep([
          '  IfErrors 0 iaSweepNext',
          'iaSweepHop: iaSweepHop2: Goto iaSweepKept',
          '  Goto iaSweepDone',
        ]),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
    // The counter bump is any positive integer — `IntOp $9 $9 + 2` moves the
    // same counter the exit code reads.
    assert.deepEqual(
      unguardedBackupDelete(
        sweep([
          '  IfErrors 0 iaSweepNext',
          '  StrCpy $iaDeleteStatus "failed"',
          '  IntOp $9 $9 + 2',
          '  Goto iaSweepDone',
        ]),
        { sitePolicies: DELETE_SITE_POLICIES },
      ),
      [],
    )
  })

  it('flags a recursive delete that is not the prepared target (#904, round 6)', () => {
    // Every other rule here binds `RMDir /r "$iaDeleteTarget"`; a second
    // recursive delete of anything else — a path read from the registry, say —
    // used to pass the whole guard while the delete that really runs is the
    // one the rules cannot say anything about (round-6 review, P3).
    const source = [
      'StrCpy $iaDeleteShapeCheck "1"',
      'Call iaPrepareDelete',
      'StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped',
      'ClearErrors',
      FIXED_DELETE,
      'RMDir /r "$iaBackupDirectory"',
      ...GUARDED_TAIL,
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(source), [
      {
        line: 6,
        what: 'a recursive delete of something other than $iaDeleteTarget — only a target iaPrepareDelete built may be deleted',
      },
    ])
    // #919 Q4: NSIS takes options between `/r` and the target, and the
    // /REBOOTOK spelling is the same delete — it used to be reported both as a
    // stray recursive delete and as a missing site. And a `RMDir /r` inside a
    // message string is not a delete at all.
    const withOption = ['ClearErrors', ...PREPARE, 'RMDir /r /REBOOTOK "$iaDeleteTarget"', ...GUARDED_TAIL].join('\n')
    assert.deepEqual(unguardedBackupDelete(withOption), [])
    const inString = [
      'ClearErrors',
      ...PREPARE,
      FIXED_DELETE,
      ...GUARDED_TAIL,
      'DetailPrint "run: RMDir /r $iaBackupDirectory would remove it"',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(inString), [])
  })

  it('the rollback delete records the backup instead of reading its own error (#904)', () => {
    const rollback = [
      'Function iaRollbackApplication',
      '  StrCpy $iaDeleteCandidate "$iaFinalDirectory"',
      '  StrCpy $iaDeleteShapeCheck "0"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaRollbackRefused',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      '  ClearErrors',
      '  Rename $iaBackupDirectory $iaFinalDirectory',
      '  ${If} ${Errors}',
      '    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  ${EndIf}',
      '  !insertmacro iaClearBackupDir',
      '  Return',
      'iaRollbackRefused:',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(rollback, { sitePolicies: DELETE_SITE_POLICIES }), [])
    // Without the record the restore-failure path has nothing to report, and
    // without the prepare the target is unvalidated — the two things the policy
    // insists on beyond the remove itself.
    const silent = rollback
      .split('\n')
      .filter((line) => !/IaLeftoverDir|iaPrepareDelete|iaDeleteStatus/.test(line))
      .join('\n')
    const problems = unguardedBackupDelete(silent, { sitePolicies: DELETE_SITE_POLICIES })
    assert.ok(problems.some((p) => /does not record a leftover/.test(p.what)), JSON.stringify(problems))
    assert.ok(problems.some((p) => /no Call iaPrepareDelete before the delete/.test(p.what)))
    // #919 Q7: the record has to sit on the path where the RESTORE fails — the
    // rename is the operation whose failure matters, and the refused branch
    // carries its own record. Pinning the record to the function at large left
    // the restore-failure branch free to stop recording: keeping the refused
    // record alone passed every check (measured).
    const refusedOnly = rollback
      .replace('    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory\n', '')
      .replace(
        'iaRollbackRefused:\n',
        'iaRollbackRefused:\n  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory\n',
      )
    assert.deepEqual(unguardedBackupDelete(refusedOnly, { sitePolicies: DELETE_SITE_POLICIES }), [
      {
        line: 7,
        what: 'the rollback site does not record a leftover on the restore-failure path (no IaLeftoverDir write of $iaBackupDirectory under the post-Rename ${Errors} read)',
      },
    ])
    // A record in the `${Else}` half of that branch is not on the failure path
    // either: the rename succeeded there.
    const elseOnly = rollback
      .replace('    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory\n', '')
      .replace(
        '  ${EndIf}',
        '  ${Else}\n    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory\n  ${EndIf}',
      )
    assert.deepEqual(unguardedBackupDelete(elseOnly, { sitePolicies: DELETE_SITE_POLICIES }), [
      {
        line: 7,
        what: 'the rollback site does not record a leftover on the restore-failure path (no IaLeftoverDir write of $iaBackupDirectory under the post-Rename ${Errors} read)',
      },
    ])
    // #919 review (F2): the window has to name its own end. The raw `IfErrors`
    // jump form has no `${EndIf}` to close it, and the old fallback to the
    // site's end left the window unbounded — the refused branch's own record
    // anywhere below satisfied the rule the window exists for. The fix refuses
    // the form (fail-closed); the disposition review's R7 pinned the reason:
    // this fixture does record on the failure path, so "does not record" was
    // not true — the report has to name the read it cannot measure.
    const rawJump = [
      'Function iaRollbackApplication',
      '  StrCpy $iaDeleteCandidate "$iaFinalDirectory"',
      '  StrCpy $iaDeleteShapeCheck "0"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaRollbackRefused',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      '  ClearErrors',
      '  Rename $iaBackupDirectory $iaFinalDirectory',
      '  IfErrors 0 iaRollbackRestored',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      'iaRollbackRestored:',
      '  !insertmacro iaClearBackupDir',
      '  Return',
      'iaRollbackRefused:',
      '  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(rawJump, { sitePolicies: DELETE_SITE_POLICIES }), [
      {
        line: 7,
        what: "the rollback site's restore-failure read is not in a form this scan can measure (a raw IfErrors/StrCmp jump leaves no readable branch end), so the record that path has to carry cannot be verified here (fail-closed)",
      },
    ])
  })

  it('validateCleanupHelpers is fail-closed about the #904 primitives', () => {
    const helper = readFileSync(join(installerDir, 'installer-cleanup.nsh'), 'utf8')
    validateCleanupHelpers(helper, 'installer-cleanup.nsh')
    // Each primitive by name: with the shape check renamed or gone, every delete
    // site would still read as clean while the target is neither validated nor
    // scanned.
    const renamed = helper.replace(/Function\s+iaCheckBackupShape\b/, 'Function iaCheckBackupShapeV2')
    assert.throws(
      () => validateCleanupHelpers(renamed, 'installer-cleanup.nsh'),
      /missing Function iaCheckBackupShape/,
    )
    const wrongMask = helper.replaceAll('0x400', '0x4')
    assert.throws(
      () => validateCleanupHelpers(wrongMask, 'installer-cleanup.nsh'),
      /missing the 0x400 FILE_ATTRIBUTE_REPARSE_POINT mask/,
    )
    const noUnc = helper.replaceAll('UNC', 'unc-replaced')
    assert.throws(() => validateCleanupHelpers(noUnc, 'installer-cleanup.nsh'), /UNC/)
    // The braces are the product's own format (System::Call's "g" GUID form),
    // so a predicate built from the unbraced spelling refuses every backup the
    // installer writes -- measured on a real update (R1 driver, case junction).
    // The literal is pinned here so the shape cannot drift back.
    const noBraces = helper.replaceAll('".old-{"', '".old-"')
    assert.throws(
      () => validateCleanupHelpers(noBraces, 'installer-cleanup.nsh'),
      /missing the "\.old-\{" literal/,
    )
    // And the helper file may not grow a recursive delete of its own: the
    // window checks (clear, read, record, clear call) only exist at the sites.
    assert.throws(
      () => validateCleanupHelpers(`${helper}\nRMDir /r "$iaDeleteTarget"\n`, 'installer-cleanup.nsh'),
      /a recursive delete inside the helper file/,
    )
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


describe('installer delete-site reads: fix round H (#919)', () => {
  // The sweep builder is the round-2 one (`sweep(read)` in the Q9 fixtures):
  // the failure leg is green unless a case replaces it, so every expectation
  // below is the feature's own delta. The line numbers are the reads' own.
  const greenDeclined = ['  SetErrorLevel 2', '  Goto iaSweepDone']
  const greenRead = [
    '  ${IfNot} ${Errors}',
    '    Goto iaSweepNext',
    '  ${Else}',
    '    IntOp $9 $9 + 1',
    '  ${EndIf}',
  ]
  const sweep = (declinedRead = greenDeclined, tailRead = greenRead) =>
    [
      '!macro customUnInstall',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  StrCmp $0 "" iaSweepDone',
      '  MessageBox MB_OKCANCEL "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDeclined',
      'iaSweepDeclined:',
      ...declinedRead,
      'iaSweepDelete:',
      '  ${GetParent} "$INSTDIR" $2',
      '  FindFirst $0 $1 "$INSTDIR.old-*"',
      '  StrCmp $0 "" iaSweepDone',
      'iaSweepLoop:',
      '  StrCmp $1 "" iaSweepLoopEnd',
      '  StrCpy $iaDeleteCandidate "$2\\$1"',
      '  StrCpy $iaDeleteBase "$INSTDIR"',
      '  StrCpy $iaDeleteShapeCheck "1"',
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaSweepKept',
      '  ClearErrors',
      '  RMDir /r "$iaDeleteTarget"',
      ...tailRead,
      'iaSweepKept:',
      '  SetErrorLevel 2',
      'iaSweepNext:',
      '  FindNext $0 $1',
      '  Goto iaSweepLoop',
      'iaSweepLoopEnd:',
      '  FindClose $0',
      'iaSweepDone:',
      '!macroend',
    ].join('\n')
  const inWindow = (source, use) =>
    source.replace(
      '  StrCpy $iaDeleteShapeCheck "1"\n',
      `  StrCpy $iaDeleteShapeCheck "1"\n  ${use}\n`,
    )
  const withDefs = (defines, source) => `${defines.join('\n')}\n${source}`
  const sweepProblems = (source) =>
    unguardedBackupDelete(source, { sitePolicies: DELETE_SITE_POLICIES })

  it('reads a depth-2 define token in the arming window (P2)', () => {
    // The fix round's review measured the hole: `${${${A}}}` with A -> B -> C
    // -> disarm resolved one nesting level and read as the name `${a}`, which
    // no map holds — so the window stayed green while the token executes the
    // disarm the delete then runs with. The `IA-DISARM`-style single level and
    // the define body carrying one read the same way and are pinned by the
    // F-2/N3 fixtures above.
    assert.deepEqual(
      sweepProblems(
        withDefs(
          ['!define A "B"', '!define B "C"', '!define C "StrCpy $iaDeleteShapeCheck 0"'],
          inWindow(sweep(), '${${${A}}}'),
        ),
      ),
      [
        {
          line: 24,
          what: "${${${A}}} at line 20 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
      ],
    )
  })

  it('reads a define body that carries a depth-2 token (P2)', () => {
    assert.deepEqual(
      sweepProblems(
        withDefs(
          [
            '!define A "B"',
            '!define B "C"',
            '!define C "StrCpy $iaDeleteShapeCheck 0"',
            '!define WRAP "${${${A}}}"',
          ],
          inWindow(sweep(), '${WRAP}'),
        ),
      ),
      [
        {
          line: 25,
          what: "${WRAP} at line 21 expands to a define that touches $iaDeleteCandidate or $iaDeleteShapeCheck — the state the delete runs with is not in this window's text (fail-closed)",
        },
      ],
    )
  })

  it('does not read message text as a label reference (P3)', () => {
    // `DetailPrint "iaSweepNext"` is correct code: the message names a label of
    // this text, and the earlier read took every quoted span on every line as a
    // label candidate — so the message was read as a jump into the delete pass
    // (measured: one problem for the message, two for a string naming two
    // labels). A quoted operand where one is real is still read, so the third
    // read below reports the `Goto`.
    assert.deepEqual(
      sweepProblems(sweep(['  SetErrorLevel 2', '  DetailPrint "iaSweepNext"', '  Goto iaSweepDone'])),
      [],
    )
    assert.deepEqual(
      sweepProblems(
        sweep([
          '  SetErrorLevel 2',
          '  DetailPrint "iaSweepKept"',
          '  DetailPrint "iaSweepNext"',
          '  Goto iaSweepDone',
        ]),
      ),
      [],
    )
    assert.deepEqual(
      sweepProblems(sweep(['  SetErrorLevel 2', '  Goto "iaSweepNext"', '  Goto iaSweepDone'])),
      [
        {
          line: 5,
          what: 'the declined sweep branch can reach the delete pass, or does not end in a jump out of it — a declined sweep must only keep',
        },
      ],
    )
  })

  it('flags a recursive delete that a define carries (P3)', () => {
    // The body sits inside the define's own quotes, so reading the line as a
    // statement blanks it with them — measured: the define-carried delete
    // compiled, deleted the directory, and passed every scan.
    assert.deepEqual(
      sweepProblems(
        withDefs(
          ["!define IAHIDDEN 'RMDir /r \"$INSTDIR.old-hidden\"'"],
          sweep().replace('  ClearErrors\n', '  ClearErrors\n  ${IAHIDDEN}\n'),
        ),
      ),
      [
        {
          line: 1,
          what: 'the define IAHIDDEN carries a recursive delete (`RMDir /r`) in its body — the delete sites this scan reads are statements of this file, and this one is text the define inserts wherever it is used',
        },
      ],
    )
  })

  it('names the unquoted target instead of calling it another delete (P4)', () => {
    // `RMDir /r $iaDeleteTarget` compiles and deletes. The old pair of messages
    // called it "something other than $iaDeleteTarget" and then "no recursive
    // RMDir of $iaDeleteTarget … found" — both false for this text.
    assert.deepEqual(
      sweepProblems(sweep().replace('  RMDir /r "$iaDeleteTarget"\n', '  RMDir /r $iaDeleteTarget\n')),
      [
        {
          line: 20,
          what: "the recursive delete's target is not in the quoted form the rules bind — `RMDir /r $iaDeleteTarget` compiles and deletes, but no rule reads an unquoted target, so this delete is neither validated nor scanned here",
        },
        {
          line: 0,
          what: 'no recursive RMDir of the prepared target in the quoted form the rules bind ("$iaDeleteTarget") was found, though this text does carry recursive deletes — the deletes it carries are the statements reported above',
        },
      ],
    )
  })

  it('reports an unresolved IDCANCEL target as unresolved (P4)', () => {
    // `IDCANCEL +4` is a relative jump, so no label in the block carries the
    // target and the branch cannot be read. Reported as "does not set the exit
    // code to 2" it said the branch had been read and found wanting.
    assert.deepEqual(
      sweepProblems(sweep().replace('IDCANCEL iaSweepDeclined', 'IDCANCEL +4')),
      [
        {
          line: 4,
          what: "the sweep prompt's IDCANCEL target is not a label this block declares — a relative or `$`-built target cannot be followed, so whether the declined branch keeps cannot be read here (fail-closed)",
        },
      ],
    )
  })

  it('reports a divider-less ${Switch} branch that falls into the delete pass (R6)', () => {
    // LogicLib's `${Switch}` emits a `Goto` into the `${EndSwitch}` label and
    // the body is skipped, so control continues at the line after
    // `${EndSwitch}` — `iaSweepDelete:` here. Reading the skipped body's `Goto
    // iaSweepDone` as the branch's exit made the shape read clean (the
    // disposition review's R6); the F-1 mutation reverts the reading.
    assert.deepEqual(
      sweepProblems(sweep(['  SetErrorLevel 2', '  ${Switch} $9', '    Goto iaSweepDone', '  ${EndSwitch}'])),
      [
        {
          line: 5,
          what: 'the declined sweep branch can reach the delete pass, or does not end in a jump out of it — a declined sweep must only keep',
        },
      ],
    )
  })

  it('asserts the !include set of the scanned script files (P3)', () => {
    // The three scanned files are the whole surface of this guard: an
    // `!include` that can point at a fourth file is a recursive delete none of
    // the scans sees (measured: a file holding its own unguarded `RMDir /r
    // "$iaDeleteTarget"`, included from installer.nsh, passed).
    const installerDir = fileURLToPath(new URL('../installer', import.meta.url))
    const dir = mkdtempSync(join(tmpdir(), 'ia-nsh-include-'))
    for (const file of ['installer.nsh', 'installer-cleanup.nsh', 'installer-directories.nsh']) {
      writeFileSync(join(dir, file), readFileSync(join(installerDir, file), 'utf8'))
    }
    assert.doesNotThrow(() => validateInstallerScripts(dir))
    writeFileSync(join(dir, 'installer-extra.nsh'), 'Section\n  RMDir /r "$iaDeleteTarget"\nSectionEnd\n')
    writeFileSync(
      join(dir, 'installer.nsh'),
      `${readFileSync(join(dir, 'installer.nsh'), 'utf8')}\n!include "installer-extra.nsh"\n`,
    )
    assert.throws(
      () => validateInstallerScripts(dir),
      /installer\.nsh: !include "installer-extra\.nsh" is not one of the scanned installer files/,
    )
  })
})
