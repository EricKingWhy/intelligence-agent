/**
 * Teeth check for the guard rules themselves (#901 / R1, round 5 = #905).
 *
 * The nsh mutation check (teeth-check.mjs) shows the shape assertions bite the
 * product. This one shows the *guard* assertions bite the guard: each mutation
 * reverts one rule to its earlier form, and `test/installer-scripts.test.mjs`
 * must then fail. A survivor means a fixture that does not pin its rule.
 *
 * Each mutation is applied to the guard module in place, the guard test file is
 * run, and the original bytes are restored and verified. The working tree may
 * carry CRLF (core.autocrlf=true and .gitattributes does not pin .mjs), so
 * mutations are applied to the LF form and written back with the file's own EOL.
 *
 * Usage (from desktop/): node test/harness/teeth-check-guards.mjs
 */
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const execute = promisify(execFile)
const desktop = fileURLToPath(new URL('../../', import.meta.url))
const guardPath = join(desktop, 'scripts', 'build-windows-installer.mjs')
const raw = readFileSync(guardPath, 'utf8')
const original = raw.replace(/\r\n/g, '\n')
const eol = raw.includes('\r\n') ? '\r\n' : '\n'
const sha = (text) => createHash('sha256').update(text, 'utf8').digest('hex')
process.stdout.write(`guard sha256 = ${sha(raw)} eol=${JSON.stringify(eol)}\n`)

/** Replace `from` with `to` in the source, throwing if `from` is absent. */
const swap = (from, to) => (source) => {
  const present = from instanceof RegExp ? from.test(source) : source.includes(from)
  if (!present) {
    throw new Error(`anchor not found: ${String(from).split('\n')[0]}`)
  }
  return source.replace(from, to)
}
/** Apply several swaps in order. */
const all = (...swaps) => (source) => swaps.reduce((text, apply) => apply(text), source)

/** Run `mutate` on the one line of the source that satisfies `match`. */
const onLine = (match, mutate) => (source) => {
  const lines = source.split('\n')
  const indexes = lines.map((line, index) => (match(line) ? index : -1)).filter((index) => index >= 0)
  if (indexes.length !== 1) throw new Error(`line match hit ${indexes.length} lines`)
  const index = indexes[0]
  lines[index] = mutate(lines[index])
  if (lines[index] === source.split('\n')[index]) throw new Error('line mutation changed nothing')
  return lines.join('\n')
}
/** The next line after the one that contains `marker`. */
const lineAfter = (marker) => (source) => {
  const lines = source.split('\n')
  const index = lines.findIndex((line) => line.includes(marker))
  if (index === -1) throw new Error(`marker not found: ${marker}`)
  return lines[index + 1]
}
/** Replace the line after `marker` with `mutate(oldLine)`. */
const swapAfter = (marker, mutate) => (source) => {
  const old = lineAfter(marker)(source)
  const replaced = mutate(old)
  if (replaced === old) throw new Error(`mutation after ${marker} changed nothing`)
  if (!source.includes(old)) throw new Error(`line not found: ${old}`)
  return source.replace(old, replaced)
}

/** Drop the trailing case-insensitive flag of a regex literal line. */
const dropCaseFlag = (line) => line.replace(/\/i$/, '')
/** Analyze one quote style only, like a single-quote-blind scan would. */
const oneQuoteStyle = (line) => line.replace(/\(\['"`\]\)/g, '(")')

const mutations = {
  // --- rules that predate #905 (kept: they still pin what they always did) ---
  'block-bounded ClearErrors window → file-wide (F7/P2)': swap(
    '    for (let j = i - 1; j >= blockStart; j -= 1) {',
    '    for (let j = i - 1; j >= 0; j -= 1) {',
  ),
  'open-above conditional scan → block scan only (F1-ish/P2)': swap(
    [
      '    const openAbove = openConditionals(lines, i)',
      '    if (openAbove > 0) {',
      '      problems.push({',
      '        line: i + 1,',
      '        what: `the delete sits inside ${openAbove} open conditional compilation directive(s) — the delete and its checks can be compiled out`,',
      '      })',
      '    } else if (lines.slice(blockStart, blockEnd + 1).some((line) => CONDITIONAL.test(line))) {',
    ].join('\n'),
    '    if (lines.slice(blockStart, blockEnd + 1).some((line) => CONDITIONAL.test(line))) {',
  ),
  'case-insensitive directives → case-sensitive (F2/P2, F3/P2)': all(
    swap(
      'const CONDITIONAL = /^\\s*!(if\\b|ifdef\\b|ifndef\\b|ifmacrodef\\b|ifmacrondef\\b|else\\b|elseif\\b|endif\\b)/i',
      'const CONDITIONAL = /^\\s*!(if\\b|ifdef\\b|ifndef\\b|ifmacrodef\\b|ifmacrondef\\b|else\\b|elseif\\b|endif\\b)/',
    ),
    swap(
      'if (/^\\s*!(if|ifdef|ifndef|ifmacrodef|ifmacrondef)\\b/i.test(lines[i])) open += 1',
      'if (/^\\s*!(if|ifdef|ifndef|ifmacrodef|ifmacrondef)\\b/.test(lines[i])) open += 1',
    ),
    swap('else if (/^\\s*!endif\\b/i.test(lines[i])) open -= 1', 'else if (/^\\s*!endif\\b/.test(lines[i])) open -= 1'),
  ),
  'absolute depth bound (delete ≤ 1 branch) → none (F1/P1)': swap(
    '    if (deleteDepth > 1) {',
    '    if (false) {',
  ),
  'relative depth bound (clear at delete depth) → none': swap(
    '      if (clearDepth > deleteDepth) {',
    '      if (false) {',
  ),
  'per-line block counting → opener-only counting': swap(
    [
      '    depth += (line.match(OPENS_BLOCK) ?? []).length',
      '    depth -= (line.match(CLOSES_BLOCK) ?? []).length',
    ].join('\n'),
    [
      '    if (/\\$\\{(If|IfNot|Unless|While|Do|DoWhile|DoUntil|For|ForEach|Select|Switch)\\}/.test(line)) depth += 1',
      '    else if (/\\$\\{(EndIf|EndWhile|EndUnless|Loop|LoopWhile|LoopUntil|Next|EndSelect|EndSwitch)\\}/.test(line)) depth -= 1',
    ].join('\n'),
  ),

  // --- rules #905 added or extended ---
  'LogicLib scans case-insensitive → case-sensitive (R1)': all(
    swap("const OPENS_BLOCK = new RegExp(LOGICLIB_OPENS, 'gi')", "const OPENS_BLOCK = new RegExp(LOGICLIB_OPENS, 'g')"),
    swap("const CLOSES_BLOCK = new RegExp(LOGICLIB_CLOSES, 'gi')", "const CLOSES_BLOCK = new RegExp(LOGICLIB_CLOSES, 'g')"),
  ),
  'full LogicLib closer set → pre-round-5 set (R8)': swapAfter(
    'export const LOGICLIB_CLOSES',
    (line) => line.replace('EndUnless|', '').replace('LoopWhile|LoopUntil|', ''),
  ),
  'block-boundary directives case-insensitive → case-sensitive (R1/R6)': onLine(
    (line) => line.startsWith('const CLOSES_FUNCTION ='),
    dropCaseFlag,
  ),
  'block-open directives case-insensitive → case-sensitive (R1)': onLine(
    (line) => line.startsWith('const OPENS_FUNCTION ='),
    dropCaseFlag,
  ),
  'divider set → no dividers at all (R2)': onLine(
    (line) => line.startsWith('const DIVIDES_BLOCK ='),
    (line) => line.replace(/= .*$/, '= /\\u0000/gi'),
  ),
  'clear-call sibling-divider check removed (R2)': swap(
    'else if (branchDividersBetween(trace, i, i + 1 + clearIndex).length > 0) {',
    'else if (false) {',
  ),
  'clear-candidate sibling-divider check removed (R5)': swap(
    '} else if (branchDividersBetween(trace, clearCandidate, i).length > 0) {',
    '} else if (false) {',
  ),
  'clear-candidate branch-chain check removed (R5)': swap(
    '} else if (!onSameBranch(trace, clearCandidate, i)) {',
    '} else if (false) {',
  ),
  'macro-body statements count as on-path (R4)': all(
    swap('  const macroRanges = macroBodyRanges(lines)', '  const macroRanges = []'),
    swap('      if (lines[j].trim() === \'\' || !onDeletePath(j)) continue', '      if (lines[j].trim() === \'\') continue'),
    swap('      if (onDeletePath(i + 1 + offset)) window.push', '      if (true) window.push'),
    swap('(line, offset) => clearCall.test(line) && onDeletePath(i + 1 + offset),', '(line) => clearCall.test(line),'),
  ),
  'constant-false branch detector removed (R3/R5)': swap(
    '      const condition = deadBranchText(lines[j])',
    '      const condition = undefined',
  ),
  'negated openers treated like plain ones (R3 control)': onLine(
    (line) => line.startsWith('  return (negated ?'),
    (line) => line.replace('(negated ? !truth : truth)', 'truth'),
  ),
  'macro-insertion check removed (item 4)': swap(
    '      macroName !== undefined &&',
    '      false &&',
  ),
  'read-form recognition → any `${Errors}` counts (item 1)': onLine(
    (line) => line.startsWith('  const errorsRead ='),
    () => '  const errorsRead = /\\$\\{Errors\\}/i',
  ),
  'raw IfErrors no longer a read (item 1)': onLine(
    (line) => line.startsWith('  const errorsRead ='),
    (line) => line.replace('|IfErrors\\b', ''),
  ),
  'delete-site match case-insensitive → case-sensitive (P3-3)': onLine(
    (line) => line.startsWith('  const deleteLine ='),
    dropCaseFlag,
  ),
  'delete accepts one quote style only (item 3)': onLine(
    (line) => line.startsWith('  const deleteLine ='),
    oneQuoteStyle,
  ),
  'record accepts one quote style only (item 3)': swapAfter('const recordWrite =', oneQuoteStyle),
  'record hive/key pin dropped (F6/P3)': swapAfter('const recordWrite =', (line) => line.replace('HKCU', '\\S+')),
  '`#` comments stripped → `;` only (F5/P3)': swap("ch === '#' &&", 'false &&'),
  '`#` glued to a closing quote is no longer a comment (R7)': swap(
    / \|\| \/\["'`\]\$\/\.test\(out\)/,
    '',
  ),
  'only double-quoted strings recognized (R7)': onLine(
    (line) => line.startsWith('        } else if (ch === \'"\''),
    () => '        } else if (ch === \'"\') {',
  ),
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
  if (mutated === original) {
    process.stdout.write(`\n[${name}] MUTATION FAILED TO APPLY: replacement changed nothing\n`)
    survivors += 1
    continue
  }
  writeFileSync(guardPath, mutated.replace(/\r?\n/g, eol))
  let failed = false
  let tail = ''
  try {
    await execute(process.execPath, ['--test', 'test/installer-scripts.test.mjs'], { cwd: desktop })
  } catch (error) {
    failed = true
    tail = String(error.stdout || '')
      .split('\n')
      .filter((line) => /^not ok|^# fail/.test(line))
      .slice(0, 3)
      .join(' | ')
  }
  writeFileSync(guardPath, raw)
  if (failed === false) survivors += 1
  process.stdout.write(`\n[${name}]\n  tests red = ${failed}\n  ${tail}\n`)
}

const restored = readFileSync(guardPath, 'utf8')
process.stdout.write(`\nrestored sha256 = ${sha(restored)} identical=${restored === raw}\n`)
process.stdout.write(`surviving mutations = ${survivors}\n`)
