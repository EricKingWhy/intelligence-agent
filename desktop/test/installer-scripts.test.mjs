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

  it('strips `;` comments but not `;` inside a string', () => {
    assert.equal(stripNsisComments('RMDir /r "a;b" ; trailing'), 'RMDir /r "a;b" ')
    assert.equal(stripNsisComments('; whole line'), '')
    assert.equal(stripNsisComments('x $" ; still in string'), 'x $" ; still in string')
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
      { line: 1, what: 'failed delete is not recorded (no IaLeftoverDir write)' },
    ])
    const guarded = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "k" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(guarded), [])
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
      '  WriteRegStr HKCU "k" "IaLeftoverDir" $iaBackupDirectory',
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
      { line: 2, what: 'failed delete is not recorded (no IaLeftoverDir write)' },
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
      { line: 2, what: 'failed delete is not recorded (no IaLeftoverDir write)' },
    ])
  })

  it('flags a delete whose iaClearBackupDir call is gone (the window cannot run on)', () => {
    const noClear = [
      'ClearErrors',
      FIXED_DELETE,
      '${If} ${Errors}',
      '  WriteRegStr HKCU "k" "IaLeftoverDir" $iaBackupDirectory',
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
      '  WriteRegStr HKCU "k" "IaLeftoverDir" $iaBackupDirectory',
      '  !insertmacro iaClearBackupDir',
      'FunctionEnd',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(acrossFunctions), [
      { line: 3, what: 'no !insertmacro iaClearBackupDir after the delete' },
      { line: 3, what: 'missing ${Errors} check before iaClearBackupDir' },
      { line: 3, what: 'failed delete is not recorded (no IaLeftoverDir write)' },
    ])
  })

  it('flags a ClearErrors between the delete and the read', () => {
    const interposed = [
      'ClearErrors',
      FIXED_DELETE,
      'ClearErrors',
      '${If} ${Errors}',
      '  WriteRegStr HKCU "k" "IaLeftoverDir" $iaBackupDirectory',
      '${EndIf}',
      '!insertmacro iaClearBackupDir',
    ].join('\n')
    assert.deepEqual(unguardedBackupDelete(interposed), [
      { line: 2, what: 'ClearErrors between the delete and the ${Errors} read discards the failed delete' },
    ])
  })

  it('is fail-closed: no delete site at all is a problem', () => {
    assert.deepEqual(unguardedBackupDelete('Function nothing\nFunctionEnd'), [
      { line: 0, what: 'no long-path RMDir of $iaBackupDirectory found' },
    ])
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
    // The record must not outlive the directory it names: a promote drops it
    // when the recorded path is gone, and keeps it while the directory exists.
    assert.match(source, /\$\{IfNot\}\s+\$\{FileExists\}\s+"\$iaLeftoverDirectory"/)
    assert.match(source, /\$\{IfNot\}[\s\S]*?DeleteRegValue[^\n]*"IaLeftoverDir"[\s\S]*?\$\{EndIf\}/)
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
