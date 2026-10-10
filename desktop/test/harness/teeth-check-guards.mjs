/**
 * Teeth check for the guard rules themselves (#901 / R1, round 5 = #905).
 *
 * The nsh mutation check (teeth-check.mjs) shows the shape assertions bite the
 * product. This one shows the *guard* assertions bite the guard: each mutation
 * reverts one rule to its earlier form, and the guard test files
 * (test/installer-scripts.test.mjs and test/installer-node-runtime.test.mjs)
 * must then fail. A survivor means a fixture that does not pin its rule.
 *
 * Each mutation is applied to the guard module in place, the guard test files
 * are run, and the original bytes are restored and verified. The working tree
 * may carry CRLF (core.autocrlf=true and .gitattributes does not pin .mjs), so
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

  // --- rules #904 added ---
  'prepare requirement dropped (#904)': swap(
    '        if (prepareCall.test(lines[j])) return j',
    '        if (false) return j',
  ),
  'prepare call without the uninstaller name space (#904)': swap(
    '  const prepareCall = /^\\s*Call\\s+(?:un\\.)?iaPrepareDelete\\b/i',
    '  const prepareCall = /^\\s*Call\\s+iaPrepareDelete\\b/i',
  ),
  'prepare same-branch check dropped (#904)': swap(
    '    } else if (!onSameBranch(trace, prepareIndex, i)) {',
    '    } else if (false) {',
  ),
  'prepare sibling-divider check dropped (#904)': swap(
    '    } else if (branchDividersBetween(trace, prepareIndex, i).length > 0) {',
    '    } else if (false) {',
  ),
  'status gate requirement dropped (#904)': swap('      if (gateIndex === -1) {', '      if (false) {'),
  'site policy table ignored — strict everywhere (#904)': swap(
    [
      '    const policy =',
      '      Object.entries(sitePolicies).find(',
      '        ([name]) => name.toLowerCase() === siteName,',
      "      )?.[1] ?? 'backup'",
    ].join('\n'),
    "    const policy = 'backup'",
  ),
  'sweep read-count rule dropped (#904)': swap(
    "      readCount: policy === 'backup' || policy === 'sweep',",
    "      readCount: policy === 'backup',",
  ),
  'sweep macro-insertion exemption dropped (#904)': swap("      policy !== 'sweep' &&", '      true &&'),
  'rollback leftover-record requirement dropped (#904)': swap(
    '    if (wants.deferredRecord) {',
    '    if (false) {',
  ),
  'sweep report requirements dropped (#904)': swap('    if (wants.sweepReport) {', '    if (false) {'),
  // FileFunc's ${GetParent} takes ("[path]" $result): with the arguments swapped
  // the macro's last Pop lands on $INSTDIR and the sweep silently deletes
  // nothing (measured on the real uninstaller). The required form and the
  // fail-closed rejection of a bare-variable first argument are separate rules,
  // so each gets its own mutation.
  'sweep ${GetParent} order requirement dropped (#904)': swap(
    '/^\\s*\\$\\{GetParent\\}\\s+"\\$INSTDIR"\\s+\\$\\d/im,',
    '/^\\s*\\$\\{GetParent\\}\\s+"\\$INSTDIRX"\\s+\\$\\d/im,',
  ),
  'sweep ${GetParent} bare-variable rejection dropped (#904)': swap(
    '/\\$\\{GetParent\\}\\s+\\$/',
    '/\\$\\{GetParent\\}\\s+\\$NOPE/',
  ),
  'fail-closed on a missing site dropped (#904)': swap('  if (sites === 0 && requireSite) {', '  if (false) {'),
  'cleanup primitive list ignored (#904)': swap(
    '  const problems = required.filter(({ re }) => !re.test(code)).map(({ what }) => `missing ${what}`)',
    '  const problems = []',
  ),
  'cleanup helper recursive-delete ban dropped (#904)': swap(
    '  if (/RMDir\\s+\\/r/im.test(code)) {',
    '  if (false) {',
  ),

  // --- rules round 6 added (2026-10-10 review) ---
  // The sweep's shape gate (P2: `"1"`→`"0"` at installer.nsh:174 survived the
  // whole guard and the unit suite), the exit-code value and its position on the
  // kept path (P3), and the stray second recursive delete (P3).
  'sweep shape-check arming requirement dropped (#904)': swap(
    "      const mustArm = policy === 'sweep' || armsBackupName",
    '      const mustArm = false',
  ),
  'sweep exit-code value pin dropped (#904)': swap(
    '/^\\s*SetErrorLevel\\s+2\\s*$/i.test(lines[j])',
    '/^\\s*SetErrorLevel\\b/i.test(lines[j])',
  ),
  'sweep exit-code position weakened to the whole block (#904)': swap(
    'keptExitLevel(lines, i, blockEnd, onDeletePath)',
    'keptExitLevel(lines, blockStart - 1, blockEnd, onDeletePath)',
  ),
  'stray recursive-delete rule dropped (#904)': swap(
    '    if (!/RMDir\\s+\\/r/i.test(lines[i]) || deleteLine.test(lines[i])) continue',
    '    if (true) continue',
  ),

  // --- rules #919 added ---
  // Q1: the arming rule covered the sweep policy alone, so a promote site that
  // disarmed the shape check deleted an HKCU-writable name unvalidated and the
  // whole suite stayed green (measured in #919). The mutation limits the
  // requirement back to the sweep; the promote fixture is what has to catch it.
  'backup-site arming requirement dropped (#919 Q1)': swap(
    "      const mustArm = policy === 'sweep' || armsBackupName",
    "      const mustArm = policy === 'sweep'",
  ),
  // Q2: the arming window was satisfied by any `"1"` in it, so a second
  // assignment that turned the flag off again just below the arming passed the
  // whole guard and suite (measured in #919). The mutation restores that
  // presence reading; the sweep fixture's two-assignment case catches it.
  'arming presence check instead of the last assignment (#919 Q2)': swap(
    "        lastArmed.value === '1' &&",
    "        arming.some((entry) => entry.value === '1') &&",
  ),
  // Q3: the arming only counts on the path the prepare call is on. Each branch
  // clause gets its own mutation, and each is caught by its own fixture: an
  // arming inside a branch that closes above the call, and one on the other
  // side of an `${Else}` the call does not reach (both measured green in #919).
  'arming branch-chain check dropped (#919 Q3)': swap(
    '        onSameBranch(trace, lastArmed.index, prepareIndex) &&',
    '        true &&',
  ),
  'arming sibling-divider check dropped (#919 Q3)': swap(
    '        branchDividersBetween(trace, lastArmed.index, prepareIndex).length === 0',
    '        true',
  ),
  // Q3: the exit code is a guarded statement — it has to sit on the delete's
  // path, and it joins the dead-branch scan, so a `2` that never runs (inside
  // another macro body, or inside a constant-false branch) no longer reports a
  // kept sweep (both measured green in #919).
  'sweep exit-code path filter dropped (#919 Q3)': swap(
    '    if (!onPath(j)) continue',
    '    if (false) continue',
  ),
  'sweep exit-code left out of the dead-branch scan (#919 Q3)': swap(
    /      \.\.\.\(exitCodeIndex === -1 \? \[\] : \[\{ line: exitCodeIndex, label: 'the exit code' \}\]\),\n/,
    '',
  ),

  // Q7: the rollback's record was pinned to the restore-failure branch instead
  // of the block. Each hole gets a mutation and its own fixture: a record that
  // lives only in the refused branch, and one that sits in the `${Else}` half
  // of the failure branch (both measured green in #919).
  'rollback record counted anywhere in the block again (#919 Q7)': swap(
    '          .slice(failure.from, failure.to)',
    '          .slice(blockStart, blockEnd + 1)',
  ),
  'rollback record free to sit past the failure branch (#919 Q7)': swap(
    '  return { from: read + 1, to: divide === -1 ? close : Math.min(close, divide) }',
    '  return { from: read + 1, to: close }',
  ),

  // Q8: declining the sweep prompt is its own "kept" outcome, and the prompt's
  // IDCANCEL target names the branch that has to carry the exit code. Each half
  // gets a mutation and a fixture: the cancel landing on the success label, and
  // a prompt with no IDCANCEL at all (both measured green in #919).
  'declined-sweep exit-code rule dropped (#919 Q8)': swap(
    '          if (declinedExit === -1) {',
    '          if (false) {',
  ),
  'sweep prompt IDCANCEL requirement dropped (#919 Q8)': swap(
    '        if (cancelTarget === undefined) {',
    '        if (false) {',
  ),

  // The node runtime lock validator compared the pin's major against
  // minimumMajor only; a 22.0.x pin (below the TUI's ">=22.1" floor) passed the
  // validator and the whole suite (measured in #919). The mutation reverts the
  // engines tuple comparison to that shape.
  'node engines minor floor dropped (#919 Q11)': swap(
    '    if (minor < floorMinor) {',
    '    if (false) {',
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
    await execute(
      process.execPath,
      ['--test', 'test/installer-scripts.test.mjs', 'test/installer-node-runtime.test.mjs'],
      { cwd: desktop },
    )
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
// A survivor is a fixture that does not pin its rule (or a mutation that no
// longer applies to the shipped text), so the run fails: a harness that only
// prints them is easy to read as green.
if (survivors > 0 || restored !== raw) process.exitCode = 1
