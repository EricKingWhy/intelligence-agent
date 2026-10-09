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
      '  Call iaPrepareDelete',
      '  StrCmp $iaDeleteStatus "ok" 0 iaSweepDone',
      '  ClearErrors',
      `  RMDir /r "$iaDeleteTarget"`,
      '  IfErrors 0 iaSweepDone',
      '  SetErrorLevel 2',
      '  MessageBox MB_OK "$(iaLeftoverSweep)"',
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
      '  MessageBox MB_OKCANCEL "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDone',
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
      [{ line: 14, what: 'the sweep does not enumerate "$INSTDIR.old-*" by name shape' }],
    )
    assert.deepEqual(
      unguardedBackupDelete(swapped('"($9) $(iaLeftoverSweep)"', '"old leftovers?"'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 14, what: 'the sweep does not ask before deleting (no $(iaLeftoverSweep) prompt)' }],
    )
    assert.deepEqual(
      unguardedBackupDelete(swapped('  SetErrorLevel 2', '  StrCpy $9 ""'), {
        sitePolicies: DELETE_SITE_POLICIES,
      }),
      [{ line: 14, what: 'nothing sets a non-zero exit code when leftovers are kept' }],
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
      [{ line: 14, what: "missing ${Errors} check before the end of the delete's block" }],
    )
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
