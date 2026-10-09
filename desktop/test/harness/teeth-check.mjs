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

const prefixedProbe = '${IfNot} ${FileExists} "\\\\?\\$iaLeftoverDirectory"'
const unprefixedProbe = '${IfNot} ${FileExists} "$iaLeftoverDirectory"'
const dropLine = 'DeleteRegValue HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir"'
const lf = '\n'

const readLine = `    !insertmacro iaReadLeftoverDir${lf}`
// The shipped shape: the record is dropped only inside BOTH negative probes.
const shippedProbeBlock = [
  '    ${If} $iaLeftoverDirectory != ""',
  `      ${unprefixedProbe}`,
  `        ${prefixedProbe}`,
  `          ${dropLine}`,
  '        ${EndIf}',
  '      ${EndIf}',
  '    ${EndIf}',
].join(lf)
// Prefixed probe only: what the first repair round shipped, and what lets a UNC
// or otherwise inexpressible recorded path be read as "gone".
const prefixedOnlyBlock = [
  '    ${If} $iaLeftoverDirectory != ""',
  `      ${prefixedProbe}`,
  `        ${dropLine}`,
  '      ${EndIf}',
  '    ${EndIf}',
].join(lf)
const swappedBlock = [
  '    ${If} $iaLeftoverDirectory != ""',
  `      ${unprefixedProbe}`,
  '        ${Else}',
  `          ${dropLine}`,
  '        ${EndIf}',
  '      ${EndIf}',
  '    ${EndIf}',
].join(lf)

const mutations = {
  'probe without the long-path prefix at all (B-1)': (source) => {
    if (!source.includes(shippedProbeBlock)) throw new Error('probe block not found')
    return source.replace(
      shippedProbeBlock,
      [
        '    ${If} $iaLeftoverDirectory != ""',
        `      ${unprefixedProbe}`,
        `        ${dropLine}`,
        '      ${EndIf}',
        '    ${EndIf}',
      ].join(lf),
    )
  },
  'drop on one probe form only (B-P2-2)': (source) => {
    if (!source.includes(shippedProbeBlock)) throw new Error('probe block not found')
    return source.replace(shippedProbeBlock, prefixedOnlyBlock)
  },
  'branches swapped: drop the record while the directory exists (P3-A5)': (source) => {
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
  'whole guarded body parked in a never-taken branch (F1)': (source) => {
    // What relative-depth assertions cannot see: delete, checks and clear all
    // move one level down together, so every comparison between them still
    // holds. Only the delete's absolute depth (and the guard's absolute bound)
    // catches it.
    const shippedDelete = `    RMDir /r "\\\\?\\$iaBackupDirectory"`
    const clearCall = '    !insertmacro iaClearBackupDir'
    const anchor = ['    ClearErrors', shippedDelete].join(lf)
    if (!source.includes(anchor)) throw new Error('delete anchor not found')
    if (!source.includes([lf, clearCall].join(''))) throw new Error('clear anchor not found')
    return source
      .replace(anchor, ['    ${If} 1 == 0', '    ClearErrors', shippedDelete].join(lf))
      .replace([lf, clearCall].join(''), [lf, clearCall, '    ${EndIf}'].join(''))
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
