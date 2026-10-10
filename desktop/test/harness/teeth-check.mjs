/**
 * Teeth check for the #901 repair-round test assertions.
 *
 * Mutates desktop/installer/installer-directories.nsh in place, runs the guard
 * test file, restores the original bytes (verified byte-for-byte), and reports
 * which mutation the tests caught. Every mutation is expected to fail, so a
 * surviving mutation is a gap in the assertions.
 *
 * The working tree may carry CRLF (core.autocrlf=true and .gitattributes does
 * not pin .nsh), so mutations are applied to the LF form and written back with
 * the file's own EOL.
 *
 * Usage (from desktop/): node test/harness/teeth-check.mjs
 */
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const execute = promisify(execFile)
const desktop = fileURLToPath(new URL('../../', import.meta.url))
const nsh = join(desktop, 'installer', 'installer-directories.nsh')
const raw = readFileSync(nsh, 'utf8')
const original = raw.replace(/\r\n/g, '\n')
const eol = raw.includes('\r\n') ? '\r\n' : '\n'
const sha = (text) => createHash('sha256').update(text, 'utf8').digest('hex')
process.stdout.write(`original sha256 = ${sha(raw)} eol=${JSON.stringify(eol)}\n`)

const unprefixedProbe = '${IfNot} ${FileExists} "$iaLeftoverDirectory"'
const dropLine = 'DeleteRegValue HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir"'
const lf = '\n'

const readLine = `    !insertmacro iaReadLeftoverDir${lf}`
// The shipped shape (#904): the record is dropped only after iaProbePath, the
// helper that tries every form that can name the path. The pre-#904 shape (the
// unprefixed probe alone) is one of the mutations below: it reads a
// longer-than-MAX_PATH or UNC record as "gone" and drops it.
const shippedProbeBlock = [
  '    ${If} $iaLeftoverDirectory != ""',
  '      StrCpy $iaPlainPath "$iaLeftoverDirectory"',
  '      Call iaProbePath',
  '      ${If} $iaProbeFound != "1"',
  `        ${dropLine}`,
  '      ${EndIf}',
  '    ${EndIf}',
].join(lf)
const unprefixedOnlyBlock = [
  '    ${If} $iaLeftoverDirectory != ""',
  `      ${unprefixedProbe}`,
  `        ${dropLine}`,
  '      ${EndIf}',
  '    ${EndIf}',
].join(lf)
const unprobedBlock = [
  '    ${If} $iaLeftoverDirectory != ""',
  `      ${dropLine}`,
  '    ${EndIf}',
].join(lf)
const swappedBlock = [
  '    ${If} $iaLeftoverDirectory != ""',
  '      StrCpy $iaPlainPath "$iaLeftoverDirectory"',
  '      Call iaProbePath',
  '      ${If} $iaProbeFound == "1"',
  `        ${dropLine}`,
  '      ${EndIf}',
  '    ${EndIf}',
].join(lf)

const mutations = {
  'probe without the long-path helper (B-1)': (source) => {
    if (!source.includes(shippedProbeBlock)) throw new Error('probe block not found')
    return source.replace(shippedProbeBlock, unprefixedOnlyBlock)
  },
  'drop without probing the path at all (B-P2-2)': (source) => {
    if (!source.includes(shippedProbeBlock)) throw new Error('probe block not found')
    return source.replace(shippedProbeBlock, unprobedBlock)
  },
  'branches swapped: drop the record while the path is still there (P3-A5)': (source) => {
    if (!source.includes(shippedProbeBlock)) throw new Error('probe block not found')
    return source.replace(shippedProbeBlock, swappedBlock)
  },
  'record never read back (P3-A5)': (source) => {
    if (!source.includes(readLine)) throw new Error('read line not found')
    return source.replace(readLine, '')
  },
  'record handling parked in a never-taken branch (B-P2-1)': (source) => {
    if (!source.includes(shippedProbeBlock)) throw new Error('probe block not found')
    return source.replace(
      shippedProbeBlock,
      [
        '    ${If} 1 == 0',
        ...shippedProbeBlock.split(lf).map((line) => `  ${line}`),
        '    ${EndIf}',
      ].join(lf),
    )
  },
  'whole record handling parked in a never-taken branch (F1)': (source) => {
    // What relative-depth assertions cannot see: read, probe, drop and clear all
    // move one level down together, so every comparison between them still
    // holds. Only the block's absolute place (and the guard's absolute bound)
    // catches it.
    const clearCall = '    !insertmacro iaClearBackupDir'
    const anchor = ['    !insertmacro iaReadLeftoverDir', shippedProbeBlock, clearCall].join(lf)
    if (!source.includes(anchor)) throw new Error('record-handling anchor not found')
    return source.replace(anchor, `    \${If} 1 == 0${lf}${anchor}${lf}    \${EndIf}`)
  },
  'failed delete exempted from the exit code (Q6, #919)': (source) => {
    // The promote delete's own failure keeps the backup exactly like a refusal
    // does, so it reports rc 2 too (measured: the real-machine driver's
    // `failure` case read rc 0 before #919 Q6, rc 2 after). This mutation
    // restores the exemption the pre-fix build shipped.
    const anchor = [
      '      MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaStaleBackup) $iaBackupDirectory" /SD IDOK',
      '      SetErrorLevel 2',
    ].join(lf)
    if (!source.includes(anchor)) throw new Error('stale-backup report anchor not found')
    return source.replace(
      anchor,
      [
        '      MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaStaleBackup) $iaBackupDirectory" /SD IDOK',
        '      ${If} $iaDeleteStatus != "failed"',
        '        SetErrorLevel 2',
        '      ${EndIf}',
      ].join(lf),
    )
  },
}

let survivors = 0
for (const [name, mutate] of Object.entries(mutations)) {
  let mutated
  try {
    mutated = mutate(original)
  } catch (error) {
    process.stdout.write(`\n[${name}] MUTATION FAILED TO APPLY: ${error.message}\n`)
    survivors += 1
    continue
  }
  writeFileSync(nsh, mutated.replace(/\r?\n/g, eol))
  let failed = false
  let tail = ''
  try {
    await execute(process.execPath, ['--test', 'test/installer-scripts.test.mjs'], { cwd: desktop })
  } catch (error) {
    failed = true
    tail = String(error.stdout || '')
      .split('\n')
      .filter((line) => /^not ok|^# fail/.test(line))
      .slice(0, 4)
      .join(' | ')
  }
  writeFileSync(nsh, raw)
  if (failed === false) survivors += 1
  process.stdout.write(`\n[${name}]\n  tests red = ${failed}\n  ${tail}\n`)
}

const restored = readFileSync(nsh, 'utf8')
process.stdout.write(`\nrestored sha256 = ${sha(restored)} identical=${restored === raw}\n`)
process.stdout.write(`surviving mutations = ${survivors}\n`)
// A survivor is a hole in the assertions (or a mutation that no longer applies
// to the shipped text), so the run fails: a harness that only prints them is
// easy to read as green.
if (survivors > 0 || restored !== raw) process.exitCode = 1
