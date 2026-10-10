/**
 * Teeth check for the #901 repair-round test assertions.
 *
 * Mutates the shipped installer scripts in place — installer-directories.nsh
 * (the update protocol) and installer.nsh (the uninstaller sweep; #919 Q8: the
 * sweep rules had no product mutation at all) — runs the guard test file,
 * restores the original bytes (verified byte-for-byte), and reports which
 * mutation the tests caught. Every mutation is expected to fail, so a surviving
 * mutation is a gap in the assertions.
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
const sha = (text) => createHash('sha256').update(text, 'utf8').digest('hex')
const lf = '\n'

const unprefixedProbe = '${IfNot} ${FileExists} "$iaLeftoverDirectory"'
const dropLine = 'DeleteRegValue HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir"'
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

/** Run a mutation that rewrites `from` to `to`, throwing when `from` is gone. */
const replace = (from, to) => (source) => {
  if (!source.includes(from)) throw new Error(`anchor not found: ${String(from).split('\n')[0]}`)
  return source.replace(from, to)
}

const directoriesMutations = {
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

// #919 Q8: the sweep had no product mutation before — every teeth-check entry
// above rewrites installer-directories.nsh, so a weakened installer.nsh sweep
// (the delete pass and the prompt the user answers) went unnoticed by this
// harness. Each mutation below reverts one pinned rule of that file.
const baseMutations = {
  'sweep arming flipped to "0" (#919 Q8)': replace(
    ['  StrCpy $iaDeleteBase "$INSTDIR"', '  StrCpy $iaDeleteShapeCheck "1"'].join(lf),
    ['  StrCpy $iaDeleteBase "$INSTDIR"', '  StrCpy $iaDeleteShapeCheck "0"'].join(lf),
  ),
  'declined sweep branch returns success (#919 Q8)': replace(
    '  DetailPrint "customUnInstall: leftover sweep declined: $9 kept"' + lf + '  SetErrorLevel 2',
    '  DetailPrint "customUnInstall: leftover sweep declined: $9 kept"',
  ),
  'sweep prompt cannot be declined (#919 Q8)': replace(
    'IDCANCEL iaSweepDeclined',
    'IDCANCEL iaSweepDone',
  ),
  'sweep asks nothing before deleting (#919 Q8)': replace(
    '  MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDeclined',
    '  StrCpy $9 $9',
  ),
  // #919 fix round (Q9 follow-up): the failed delete has to reach the kept
  // accounting. The guard's walk follows the failure path through this block's
  // labels; measured before the rule, this exact edit kept the whole suite
  // green while every failed delete went uncounted (the silent direction — no
  // report, exit code left at 0).
  'failed delete skips the kept count (#919 fix round Q9)': replace(
    ['  StrCpy $iaDeleteStatus "failed"', 'iaSweepKept:'].join(lf),
    ['  StrCpy $iaDeleteStatus "failed"', '  Goto iaSweepNext', 'iaSweepKept:'].join(lf),
  ),
}

const targets = [
  { name: 'installer-directories.nsh', mutations: directoriesMutations },
  { name: 'installer.nsh', mutations: baseMutations },
]

// A survivor is a hole in the assertions (or a mutation that no longer applies
// to the shipped text), so the run fails: a harness that only prints them is
// easy to read as green.
let survivors = 0
// #919 review (F5): the restore is the harness's documented guarantee: a
// Ctrl-C or an exception inside the run between the write and the restore
// would otherwise leave the mutated text behind, and the next run would read it
// as its baseline and "restore" to it. A forced kill (`taskkill /F`, Task
// Manager, closing the console) runs no handler at all — the end-of-run
// byte-for-byte check is what catches that case (measured; disposition review
// N4/R8).
let current = null
const restoreCurrent = () => {
  if (current !== null && readFileSync(current.path, 'utf8') !== current.raw) {
    writeFileSync(current.path, current.raw)
  }
}
process.on('SIGINT', () => {
  restoreCurrent()
  process.exit(130)
})
process.on('SIGTERM', () => {
  restoreCurrent()
  process.exit(143)
})
try {
  for (const target of targets) {
    const nsh = join(desktop, 'installer', target.name)
    const raw = readFileSync(nsh, 'utf8')
    const original = raw.replace(/\r\n/g, '\n')
    const eol = raw.includes('\r\n') ? '\r\n' : '\n'
    current = { path: nsh, raw }
    process.stdout.write(`\n=== ${target.name} sha256 = ${sha(raw)} eol=${JSON.stringify(eol)} ===\n`)
    for (const [name, mutate] of Object.entries(target.mutations)) {
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
    process.stdout.write(`restored ${target.name} sha256 = ${sha(restored)} identical=${restored === raw}\n`)
    if (restored !== raw) survivors += 1
  }
} finally {
  restoreCurrent()
}
process.stdout.write(`\nsurviving mutations = ${survivors}\n`)
if (survivors > 0) process.exitCode = 1
