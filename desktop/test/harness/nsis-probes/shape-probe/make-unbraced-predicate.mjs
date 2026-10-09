/**
 * Red proof helper for the #904 shape probe: writes a copy of the shipped
 * predicate with the braces taken back out of the shape check -- the predicate
 * as it was before the R1 driver's junction case caught it refusing the
 * installer's own backup name -- so run-shape-probe.ps1 can be pointed at it:
 *
 *   node make-unbraced-predicate.mjs D:\w21-work\unbraced.nsh
 *   powershell -File run-shape-probe.ps1 -Predicate D:\w21-work\unbraced.nsh
 *
 * The probe must then FAIL the braced cases (cocreated, literal-uppercase,
 * literal-lowercase, unc-base); an all-PASS run means the cases do not bite.
 * Usage: node make-unbraced-predicate.mjs <output path>
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const source = fileURLToPath(
  new URL('../../../../installer/installer-cleanup.nsh', import.meta.url),
)
const output = process.argv[2]
if (!output) {
  process.stderr.write('usage: node make-unbraced-predicate.mjs <output path>\n')
  process.exit(2)
}

const original = readFileSync(source, 'utf8')
const eol = original.includes('\r\n') ? '\r\n' : '\n'
let text = original.replace(/\r\n/g, '\n')

const braced = [
  '  StrCpy $R2 "$iaShapeCandidate" 6 $R1',
  '  StrCmp $R2 ".old-{" 0 iaShapeDone',
  '  IntOp $R3 $R1 + 42',
  '  StrCpy $R2 "$iaShapeCandidate" 1 $R3',
  '  StrCmp $R2 "}" 0 iaShapeDone',
].join('\n')
const unbraced = [
  '  StrCpy $R2 "$iaShapeCandidate" 5 $R1',
  '  StrCmp $R2 ".old-" 0 iaShapeDone',
].join('\n')

if (!text.includes(braced) || !text.includes('IntOp $R3 $R1 + 43')) {
  process.stderr.write('anchor not found: the braced shape block moved\n')
  process.exit(1)
}
text = text
  .replace('IntOp $R3 $R1 + 43', 'IntOp $R3 $R1 + 41')
  .replace(braced, unbraced)
writeFileSync(output, text.replace(/\n/g, eol))
process.stdout.write(
  text === original.replace(/\r\n/g, '\n')
    ? 'WARNING: the copy is identical to the source\n'
    : `unbraced predicate written to ${output}\n`,
)
