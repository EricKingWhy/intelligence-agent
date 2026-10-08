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
    for (const name of ['iaPerUserOnly', 'iaAppRunning', 'iaUpdateFailed', 'iaRollbackFailed']) {
      assert.match(installerNsh, new RegExp(`LangString ${name} \\$\\{LANG_ENGLISH\\}`))
      assert.match(installerNsh, new RegExp(`LangString ${name} \\$\\{LANG_SIMPCHINESE\\}`))
    }
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
