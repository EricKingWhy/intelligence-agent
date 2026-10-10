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
// #919 fix round (P4): the run restores the guard in a `finally`, but a
// force-kill (SIGPIPE from a piped `head`, a closed terminal) leaves the
// mutation in the file, and the next run then reads the *mutated* text as its
// baseline: `restored … identical=true` and every count are self-consistent
// about the wrong file (measured once by the review; a probe left a mutation in
// the worktree the same way). The recorded digest of the committed text makes
// that state loud instead of vacuous. Update it in the same change that edits
// the guard's text.
const GUARD_BASELINE_SHA256 = '3ea817b5099c84b9210d1dc2aa02dfd8ba57be73130d6f5f2fbe5105b8d973bc'
if (sha(original) !== GUARD_BASELINE_SHA256) {
  process.stderr.write(
    `guard baseline mismatch: ${sha(original)} != ${GUARD_BASELINE_SHA256} — the file is not the text this list was written against (a left-behind mutation?); restore it and re-run\n`,
  )
  process.exit(1)
}

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

/**
 * Drop the trailing case-insensitive flag of a regex literal line.
 *
 * The trailing `/i` is the closing slash plus the flag: removing both leaves an
 * unterminated literal, and the suite then dies of a SyntaxError instead of the
 * rule under test (measured in the #919 review — three mutations did exactly
 * that). Removing only the flag keeps the literal terminated.
 */
const dropCaseFlag = (line) => line.replace(/\/i$/, '/')
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
    '    if (deleteLine.test(lines[i])) continue',
    '    if (true) continue',
  ),
  // Q4: the prepared-target delete's option-carrying spelling (`/REBOOTOK`
  // between `/r` and the target) is the same delete, and a `RMDir /r` inside a
  // message string is not a delete statement. Each reverted alone makes its
  // fixture report (measured in #919).
  'prepared-target delete options no longer recognized (#919 Q4)': swap(
    '(?:\\s+\\/[A-Za-z]+)*\\s+',
    '\\s+',
  ),
  'stray scan reads string contents again (#919 Q4)': swap(
    'outsideStrings(lines[i])',
    'lines[i]',
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
    '        lastArmed.armed &&',
    '        arming.some((entry) => entry.armed) &&',
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
    '    if (minor < floorMinor || (minor === floorMinor && patch < floorPatch)) {',
    '    if (false) {',
  ),

  // --- rules the #919 two-axis review added or tightened ---
  // F1: the candidate copy and the arming value take any of the three quote
  // forms or none (measured on 3.0.4.1), so each regex accepts all four. The
  // mutations re-narrow one of them to the double-quoted spelling; the
  // re-spelled fixtures are what has to catch it. (The fix round moved the
  // value-reading half into the `CANDIDATE_COPY`/`SHAPE_ARMING` pair and left
  // the bare-or-quoted output gate in the `_OUT` pair: the mutation targets the
  // value pattern, whose quote classes are what the fixtures re-spell.)
  'candidate copy accepts one quote style only (#919 review F1)': onLine(
    (line) => line.startsWith('  const CANDIDATE_COPY ='),
    oneQuoteStyle,
  ),
  'arming value accepts one quote style only (#919 review F1)': onLine(
    (line) => line.startsWith('  const SHAPE_ARMING ='),
    oneQuoteStyle,
  ),
  // F1, fail-closed half: an assignment whose value the guard cannot read must
  // not pass as "no assignment" (the register spelling passed the whole guard
  // before, measured).
  'arming value the guard cannot read ignored again (#919 review F1)': swap(
    '        const assignment = SHAPE_ARMING.exec(lines[j])\n        const value = assignment === null ? null : (assignment[3] ?? assignment[4])',
    '        const assignment = SHAPE_ARMING.exec(lines[j])\n        if (assignment === null) continue\n        const value = assignment[3] ?? assignment[4]',
  ),
  'non-backup candidate sources arms nothing again (#919 review F1)': swap(
    "        return !/^\\$iaFinalDirectory$/i.test(readable[0])",
    "        return /^\\$iaBackupDirectory$/i.test(readable[0])",
  ),
  // F1/A4 + R1/R2: the arming window is a text the guard has to refuse wherever
  // it cannot read the state — an `!insertmacro`, a `${define}` expansion, an
  // `!include`, or any other statement that uses either variable. One switch per
  // reading, so the fixtures that pin each rule die on their own.
  'macro insertions in the arming window ignored again (#919 review F1/A4)': swap(
    '      const windowHazard = windowHazards[0]\n      if (windowHazard !== undefined) problems.push({ line: i + 1, what: windowHazard })',
    '      const windowHazard = windowHazards[0]\n      if (false) problems.push({ line: i + 1, what: windowHazard })',
  ),
  'insertion bodies no longer read for the delete state (#919 review F1/A4)': swap(
    '        if (insert !== null && touchesDeleteVars(insert[1].toLowerCase())) {',
    '        if (insert !== null && false) {',
  ),
  'define expansions no longer resolved (#919 disposition review R2)': swap(
    '          return substitutions.has(name) && touchesDeleteVars(name)',
    '          return false',
  ),
  'an include in the arming window no longer refused (#919 disposition review R2)': swap(
    '        if (/^\\s*!include\\b/i.test(code)) {',
    '        if (false) {',
  ),
  'any other statement using the delete state ignored again (#919 disposition review R1)': swap(
    '          /\\$iaDeleteCandidate\\b|\\$iaDeleteShapeCheck\\b/i.test(mention) &&',
    '          false &&',
  ),
  // N1: the candidate source is the whole window. Reading only its last copy
  // let a branch-divergent layout hide the backup copy behind the exempt
  // `$iaFinalDirectory` one (the Q1 bypass the widened spellings kept closed).
  'candidate source read as the last copy again (#919 disposition review N1)': swap(
    "        if (readable.length !== sources.length || new Set(readable).size > 1) return true\n        return !/^\\$iaFinalDirectory$/i.test(readable[0])",
    "        return !/^\\$iaFinalDirectory$/i.test(sources[sources.length - 1] ?? '')",
  ),
  // F3: the declined branch may not reach the delete pass — a `Goto` into the
  // prompt's IDOK target, or a fall-through into it.
  'declined branch may reach the delete pass again (#919 review F3)': swap(
    '            if (reaches || fallsThrough || coversDelete) {',
    '            if (false) {',
  ),
  // R3: the delete pass is the labelled loop the delete sits in, so a landing
  // on a label between the `RMDir` and the loop's back-jump is in it. The
  // one-sided reading (above the delete only) let `Goto iaSweepKept` and
  // `Goto iaSweepNext` re-enter the loop tail with the handle closed.
  'landing in the delete loop tail no longer counts (#919 disposition review R3)': swap(
    '            const landsInDeletePass = (landing) =>\n              landing !== -1 && (landing <= deleteOffset || (landing >= passStart && landing <= passEnd))',
    '            const landsInDeletePass = (landing) => landing !== -1 && landing <= deleteOffset',
  ),
  // F2: an unclosed read (the raw `IfErrors` form has no `${EndIf}`) must make
  // the window unreadable and report, not fall back to the site's end.
  'unclosed restore window falls back to the site end again (#919 review F2)': swap(
    '  const close = trace.closeOf.get(read)\n  if (close === undefined) return null',
    '  const close = trace.closeOf.get(read) ?? end',
  ),
  // S2: the declined branch's exit code joins the dead-branch scan like the
  // main one, so a `SetErrorLevel 2` it never reaches does not count.
  'declined exit left out of the dead-branch scan (#919 review S2)': swap(
    "      ...(declinedExitIndex === -1 ? [] : [{ line: declinedExitIndex, label: 'the declined exit code' }]),\n",
    '',
  ),
  // S4: the engines floor is anchored and may carry a patch; unanchored, a
  // `>=22.1.5` floor was read as `>=22.1` and `>=22x` as `>=22`.
  'engines floor unanchored again (#919 review S4)': swap(
    '\\s*$/.exec(String(engines ?? \'\'))',
    '/.exec(String(engines ?? \'\'))',
  ),
  'engines patch floor dropped (#919 review S4)': swap(
    "const floor = /^\\s*>=\\s*(\\d+)(?:\\.(\\d+))?(?:\\.(\\d+))?\\s*$/",
    "const floor = /^\\s*>=\\s*(\\d+)(?:\\.(\\d+))?\\s*$/",
  ),
  // F1 (fix round): the delete-pass span is read from any statement naming a
  // label above the delete, not only from a bare `Goto` — the two-target
  // `StrCmp $1 "" iaSweepLoopEnd iaSweepLoop` back-jump collapsed the span onto
  // the delete, and the tail legs (`iaSweepKept`, `iaSweepNext`) then read as
  // outside it (measured on the real installer.nsh by the fix round's review).
  'delete loop span read from bare Goto only again (#919 fix-round review F1)': swap(
    '            const labelTokens = (line) => lineTokens(line).filter((token) => labelOffsets.has(token))',
    "            const labelTokens = (line) =>\n              (/^\\s*Goto\\s+(\\S+)\\s*$/i.exec(outsideStrings(line))?.[1] ?? '')\n                .trim()\n                .split(/\\s+/)\n                .map((token) => token.toLowerCase())\n                .filter((token) => labelOffsets.has(token))",
  ),
  // F1 (fix round): a label whose own span jumps back into the pass counts as
  // reaching it — without the walk, a hop label declared below the loop
  // laundered `Goto iaSweepHop` into a silent green.
  'hop labels no longer followed into the delete pass (#919 fix-round review F1)': swap(
    '                const hit = span.some((line) =>\n                  labelTokens(line).some((token) => visit(token, walk)),\n                )',
    '                const hit = false',
  ),

  // --- rules the #919 fix round added or tightened (round-4 findings) ---
  // F-1: a label is a position — `iaSweepDeclined: SetErrorLevel 2` declares the
  // label the prompt jumps to, and the statement on its line is the branch's
  // first (measured legal in a Section and in a `!macro` body). Reading only the
  // stand-alone spelling left the whole declined contract unchecked for that
  // shape. Three mutations, one per half of the reading: the label lookup, the
  // statement on the label's line (the exit code), and that statement inside the
  // branch body / span walk.
  // The F-5 fix rewrote the prompt target lookup and the chain tail reads, so
  // these three anchors name the new text with their old meaning: a label
  // counts only when it is alone on its line, and the branch reads only what
  // follows the first name / drops the inline statement.
  'inline label declarations invisible again (#919 fix round F-1)': swap(
    "            labelDeclarations(line).some((declaration) => declaration.name === cancelTarget.toLowerCase())",
    "            labelDeclarations(line).some((declaration) => declaration.name === cancelTarget.toLowerCase() && declaration.rest === '')",
  ),
  'inline label statement dropped from the exit-code read (#919 fix round F-1)': swap(
    "                  labelDeclarations(block[labelOffset]).at(-1)?.rest ?? '',\n",
    "                  '',\n",
  ),
  'inline label statement dropped from the branch body (#919 fix round F-1)': swap(
    "            const restOf = (at) => labelDeclarations(block[at]).at(-1)?.rest ?? ''",
    "            const restOf = () => ''",
  ),
  // The label chain `A: B: Goto X` is one position and two names; reading only
  // the first name left a jump through the second one unread.
  'label chains read as the first name only (#919 fix round F-1)': swap(
    '  let text = stripNsisComments(line)\n  for (;;) {',
    '  let text = stripNsisComments(line)\n  for (let once = 0; once < 1; once += 1) {',
  ),
  // N1: a jump target may be quoted (`Goto "L"` compiles and jumps), and
  // `outsideStrings` blanks quoted contents away — the token read has to take
  // the quoted spans as candidates of their own.
  'quoted label references invisible again (#919 fix round N1)': swap(
    '            const labelTokens = (line) => lineTokens(line).filter((token) => labelOffsets.has(token))',
    '            const labelTokens = (line) =>\n              outsideStrings(line)\n                .trim()\n                .split(/\\s+/)\n                .map((token) => token.toLowerCase())\n                .filter((token) => labelOffsets.has(token))',
  ),
  // F-2: sibling `!macro` bodies share the label namespace (measured: a
  // cross-body `Goto` compiled and jumped), so a jump to a label the file
  // declares outside this block is a path the scan cannot follow.
  'labels outside the block no longer reported (#919 fix round F-2)': swap(
    '            if (foreignJump !== undefined) {',
    '            if (false) {',
  ),
  // R6: the branch's tail is LogicLib structure, not just text — an `${EndIf}`
  // over arms that each jump out ends in a jump, and an arm that can fall
  // through does not.
  'branch tail read as the last text line again (#919 fix round R6)': swap(
    '              !endsInJump(statements)',
    "              !/^\\s*(Goto|Return|Abort|Quit)\\b/i.test(statements[statements.length - 1] ?? '')",
  ),
  // N2: the output of the two window statements may be quoted (`StrCpy "$vFlag"
  // "0"` compiles and stores the 0 — measured, probe r9), so the gates that
  // collect the copies read both spellings.
  'quoted-output candidate copy invisible again (#919 fix round N2)': onLine(
    (line) => line.startsWith('  const CANDIDATE_COPY_OUT ='),
    () => '  const CANDIDATE_COPY_OUT = /^\\s*StrCpy\\s+\\$iaDeleteCandidate\\b/i',
  ),
  'quoted-output arming invisible again (#919 fix round N2)': onLine(
    (line) => line.startsWith('  const SHAPE_ARMING_OUT ='),
    () => '  const SHAPE_ARMING_OUT = /^\\s*StrCpy\\s+\\$iaDeleteShapeCheck\\b/i',
  ),
  // R5: `StrCpy`'s `[maxlen] [startoffset]` decide what is stored (measured,
  // probe r6), so the operands are part of the rule.
  'arming operands ignored again (#919 fix round R5)': swap(
    '          armed:\n            value === \'1\' &&\n            (maxlen === undefined || Number(maxlen) >= 1) &&\n            (offset === undefined || Number(offset) === 0),',
    "          armed: value === '1',",
  ),
  // N3: a trailing `\` continues the value on the next line and the
  // preprocessor joins the pieces (measured, probes u4/w4), so the join is what
  // makes the body readable.
  'continued define values read as the first fragment again (#919 fix round N3)': swap(
    '        while (/\\\\\\s*$/.test(value) && j + 1 < lines.length) {',
    '        while (false) {',
  ),
  // F-3: `${...}` tokens pair their braces — `${${X}}` is one token whose name is
  // X's value (measured, probe r5), and the earlier reading stopped at the first
  // `}` (`${${X` — a name no map holds), which hid the carried write.
  'nested ${...} tokens read to the first brace again (#919 fix round F-3)': swap(
    'function defineTokens(code) {\n  const tokens = []\n  for (let i = 0; i + 1 < code.length; i += 1) {\n    if (code[i] !== \'$\' || code[i + 1] !== \'{\') continue\n    let depth = 0\n    let end = i + 1\n    for (; end < code.length; end += 1) {\n      if (code[end] === \'{\') depth += 1\n      else if (code[end] === \'}\') {\n        depth -= 1\n        if (depth === 0) break\n      }\n    }\n    if (depth !== 0) break\n    tokens.push(code.slice(i, end + 1))\n    i = end\n  }\n  return tokens\n}',
    'function defineTokens(code) {\n  return code.match(/\\$\\{[^}\\s]+\\}/g) ?? []\n}',
  ),
  // Q9 follow-up: the failed delete's path must reach the kept accounting. The
  // probe measured the gap this pins: a `Goto iaSweepNext` between the failure
  // status write and the kept label left the suite green while every failed
  // delete went uncounted (no report, exit code 0).
  'failed-delete keep-accounting walk dropped (#919 fix round Q9)': swap(
    '        if (!keeps) {',
    '        if (false) {',
  ),
  // Gap A-1 (own review): a label name's measured alphabet is a letter, `_`,
  // `.`, `%` or `@` to start and letters, digits, `_`, `.`, `+`, `!`, `%`, `#`,
  // `@`, `$`, `-` or `:` inside (measured: each compiles and every jump lands —
  // probes n8/n9), so the declaration read takes that class. The narrow class
  // left `ia-hop-into-delete:` unread, and a declined branch jumping through
  // such a hop into the delete pass read clean.
  'label alphabet narrowed back (#919 gap A-1)': swap(
    '    const match = /^\\s*([A-Za-z_.%@][A-Za-z0-9_.+!%#@$:-]*)\\s*:/.exec(text)',
    '    const match = /^\\s*([A-Za-z_][A-Za-z0-9_.]*)\\s*:/.exec(text)',
  ),
  // Gap A-2 (own review): a define's bare value may carry a dash
  // (`!define IA.SEL "IA-DISARM"` + `${${IA.SEL}}` disarms — probe n6), so
  // both value-name tests read one non-whitespace token. Narrowed back, the
  // dashed twin of the nested-define fixture resolves to the inner name and
  // the fixture's window reads clean.
  // Both value-name sites in one mutation: `defineName`'s value test and the
  // chain-follow test inside `touchesDeleteVars` are the same rule reachable
  // twice (a single-site revert is closed by the other site, so it would
  // survive as a false alarm — measured), and the dashed nested fixture is red
  // only when both read the old class.
  'dash-spelled define values read as inner names again (#919 gaps A-2/F-2)': (source) =>
    swap(
      "          if (!/\\s/.test(only) && substitutions.has(only.toLowerCase())) {",
      "          if (/^[A-Za-z_][A-Za-z0-9_.]*$/.test(only) && substitutions.has(only.toLowerCase())) {",
    )(
      swap(
        "    if (value === undefined || /\\s/.test(value)) return name",
        "    if (value === undefined || !/^[A-Za-z_][A-Za-z0-9_.]*$/.test(value)) return name",
      )(source),
    ),
  // Gap A-3 (own review): the one-argument `IfErrors label` jumps *on* error,
  // so the failure leg is the jump. Read as the fall-through again, the
  // fail-open fixture goes green and the correct one is over-reported.
  'one-argument IfErrors read as the fall-through again (#919 gap A-3)': swap(
    '          if (reads.has(at)) {',
    '          if (false && reads.has(at)) {',
  ),
  // #919 fix round H: the review's P2/P3/P4 items, each pinned by the fixture
  // that measured it (the test file's "fix round H" describe). The R6 fixture
  // is pinned by the F-1 mutation above (a case-less `${Switch}` read as an
  // arm), which the fixture goes red under as well.
  'depth-2 define token resolved one level again (#919 fix round H)': swap(
    '    const wrapped = /^\\$\\{([\\s\\S]*)\\}$/.exec(name)',
    '    const wrapped = null',
  ),
  'quoted spans read as label candidates on every line again (#919 fix round H)': swap(
    '  /^\\s*(?:Goto|GotoIf|IfErrors|IfSilent|IfAbort|IfFileExists|StrCmp|StrCmpS|IntCmp|IntCmpU|MessageBox|MessageBoxEx)\\b/i',
    '  /^/i',
  ),
  'include set not asserted again (#919 fix round H)': swap(
    '      if (!KNOWN_INSTALLER_INCLUDES.has(base)) {',
    '      if (false) {',
  ),
  'define-carried recursive delete invisible again (#919 fix round H)': swap(
    '        if (/RMDir\\s+\\/r/i.test(unquoted)) {',
    '        if (false) {',
  ),
  'unquoted target called another delete again (#919 fix round H)': swap(
    '      what: /\\$iaDeleteTarget\\b/i.test(outsideStrings(lines[i]))',
    '      what: false',
  ),
  'seen-but-unmatched delete called none again (#919 fix round H)': swap(
    '      what: sawRecursiveDelete',
    '      what: false',
  ),
  'unresolved prompt target called an unset exit code again (#919 fix round H)': swap(
    '              what:\n                labelOffset === -1\n',
    '              what:\n                false\n',
  ),
  // F-1 (review): a case-less `${Switch}` has no arm that runs — LogicLib jumps
  // to the `${EndSwitch}` label and control leaves the block (measured: probes
  // t7/t8). Reading the dead body as an arm accepted the fall-through.
  'case-less Switch read as an arm again (#919 review F-1)': swap(
    '    if (isSwitch && firstDivider === undefined) return false\n',
    '    if (false && isSwitch && firstDivider === undefined) return false\n',
  ),
  // F-4 (review): a statement on a label's own line anywhere in the span is the
  // same reading (`iaSweepKept: SetErrorLevel 2`); whole-line scanning reported
  // it as missing.
  'inline label statements invisible to the kept-exit scan again (#919 review F-4)': swap(
    '    labelDeclarations(line).some((declaration) => /^\\s*SetErrorLevel\\s+2\\s*$/i.test(declaration.rest))',
    '    false',
  ),
  // F-5 (review): a chain declares two names at one position, so the prompt's
  // target may be either; matching only the first reported the legal chain as a
  // missing exit code.
  'prompt target matched by the chain\'s first name only again (#919 review F-5)': swap(
    '            labelDeclarations(line).some((declaration) => declaration.name === cancelTarget.toLowerCase())',
    '            labelDeclarations(line)[0]?.name === cancelTarget.toLowerCase()',
  ),
  'branch tail read from the chain\'s first name again (#919 review F-5)': swap(
    "                  labelDeclarations(block[labelOffset]).at(-1)?.rest ?? '',",
    "                  labelDeclarations(block[labelOffset])[0]?.rest ?? '',",
  ),
  // #919 review (P1): the failure leg is now read by the read's own form. Each
  // mutation below puts one reading back the way the review found it, and the
  // P1 fixtures hold it down: the operand position, the inverted read's
  // `${Else}` arm, the forked-arm report, the dead-branch exclusion, the
  // divider continuation, the chain's last name and the counter's alphabet.
  'the read jump ignored and the fall-through walked again (#919 review P1)': swap(
    "          if (jump !== null && jump[1] !== '0') {",
    "          if (false) {",
  ),
  'inverted Errors read walked as the fall-through again (#919 review P1)': swap(
    "          if (!/^\\s*\\$\\{(?:IfNot|Unless)\\}\\s+\\$\\{Errors\\}/i.test(text)) {",
    "          if (true) {",
  ),
  'forked failure arm walked into again (#919 review P1)': swap(
    "          if (!/^\\s*\\$\\{Else\\}/i.test(lines[divide])) return { blocked: divide - blockStart }",
    "          if (false) return { blocked: divide - blockStart }",
  ),
  'dead-branch exclusion dropped from the failure-leg walk (#919 review P1)': swap(
    "          if (deadBranchText(block[k]) === undefined) continue",
    "          if (true) continue",
  ),
  'divider continuation dropped from the failure-leg walk (#919 review P1)': swap(
    "          if (divider !== undefined) {",
    "          if (false) {",
  ),
  'chain statement read from the first name again (#919 review P1)': swap(
    "          const declaration = labelDeclarations(block[at]).at(-1) ?? null",
    "          const declaration = labelDeclarations(block[at])[0] ?? null",
  ),
  'counter bump alphabet narrowed back to +1 (#919 review P1)': swap(
    "        const countBump = /^\\s*IntOp\\s+(\\$\\S+)\\s+\\1\\s*\\+\\s*(?:[1-9]\\d*)\\s*$/i",
    "        const countBump = /^\\s*IntOp\\s+(\\$\\S+)\\s+\\1\\s*\\+\\s*1\\s*$/i",
  ),
}

let survivors = 0
// #919 review (F5): the restore is the harness's documented guarantee: a
// Ctrl-C or an exception inside the run between the write and the restore
// would otherwise leave the guard mutated, and the next run would read that
// text as its baseline and "restore" to it. A forced kill (`taskkill /F`, Task
// Manager, closing the console) runs no handler at all — the end-of-run
// byte-for-byte check is what catches that case (measured; disposition review
// N4/R8).
const restore = () => {
  if (readFileSync(guardPath, 'utf8') !== raw) writeFileSync(guardPath, raw)
}
process.on('SIGINT', () => {
  restore()
  process.exit(130)
})
process.on('SIGTERM', () => {
  restore()
  process.exit(143)
})
try {
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
} finally {
  restore()
}

const restored = readFileSync(guardPath, 'utf8')
process.stdout.write(`\nrestored sha256 = ${sha(restored)} identical=${restored === raw}\n`)
process.stdout.write(`surviving mutations = ${survivors}\n`)
// A survivor is a fixture that does not pin its rule (or a mutation that no
// longer applies to the shipped text), so the run fails: a harness that only
// prints them is easy to read as green.
if (survivors > 0 || restored !== raw) process.exitCode = 1
