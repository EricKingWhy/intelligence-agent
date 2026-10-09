# Installer guard test harness (#901 / #905)

Opt-in checks that `npm test` does not run: the test script only globs
`test/*.test.ts` and `test/*.test.mjs`, and these mutate files in place.

Both teeth scripts mutate one source file, run
`node --test test/installer-scripts.test.mjs`, restore the original bytes and
verify the restore byte-for-byte. Every mutation must turn the suite red; a
survivor is a hole in the assertions.

| script | mutates | what it pins | last run |
| --- | --- | --- | --- |
| `teeth-check.mjs` | `installer/installer-directories.nsh` | the shape assertions bite the product: 6 mutated nsh shapes | 6/6 red, 0 survivors, restore identical |
| `teeth-check-guards.mjs` | `scripts/build-windows-installer.mjs` | every guard rule, one mutation per rule | 27/27 red, 0 survivors, restore identical |

Run from `desktop/`:

```sh
node test/harness/teeth-check.mjs
node test/harness/teeth-check-guards.mjs
```

The mutations in `teeth-check-guards.mjs` are keyed to the reproductions
registered in #905 — R1 (case-insensitive LogicLib scans: `${iF}` … `${eNdIf}`),
R2 (branch dividers between the delete and its checks), R3/R5 (never-taken
branches and the read forms), R4 (macro bodies off the delete's path), R6
(case-insensitive block boundaries), R7 (`#` comments glued to a quote, the
three quote styles), R8 (the exported LogicLib block lists the tests reuse) —
plus items 1–4 (read forms, case-insensitive variable/define names, quote
styles, macro insertion).

## nsis-probes/

The makensis 3.0.4.1 measurements the guard's rules rest on. Each `.nsi` is
self-contained; `run-probes.ps1` compiles them with a given makensis, runs the
silent ones in a temp directory and dumps `HKCU\Software\p905`.

| probe | question | measured |
| --- | --- | --- |
| `var-case.nsi`, `unknown-var.nsi` | variable-name case | `$IABackupDirectory` resolves `Var iaBackupDirectory` (compiles, no box); undeclared `$iDontExistAnywhere` fails: `Usage: StrCpy $(user_var: output) str …` + `Error in script`, exit 1 |
| `errors-forms.nsi` | `${Errors}` inside a string literal | fails: `DetailPrint expects 1 parameters, got 3` |
| `quote-forms.nsi` | single-quoted / backtick delete argument | both compile |
| `quotes-runtime.nsi` | do those forms delete, and expand `$vars`? | `single_deleted=yes`, `backtick_deleted=yes`, `sq_expand=preVALUE1post`, `bt_expand=preVALUE1post` |
| `quotes-semi.nsi` | `;` inside `'…'` / `` `…` ``, and `$\'` | `sq_semi=a;b`, `bt_semi=c;d`, `sq_escaped_quote=x'y` |
| `hash-comment.nsi` | is `#` a comment when glued? | `DetailPrint "x"#glued` compiles (comment), `WriteRegStr … foo#bar` compiles as a 4-parameter call (mid-token `#` is text) |
| `hash-runtime.nsi` | runtime values | `hash_val=foo#bar`, `glued_val=x` |
| `hash-glued.nsi` | `#` glued to `'…'` / `` `…` `` | compiles; `sq_glued=x`, `bt_glued=y` |

```powershell
# makensis is auto-discovered from the electron-builder cache (the cache
# directory suffix is machine-specific); override with -Makensis <path>.
powershell -File run-probes.ps1
```

The probes write only to their own `HKCU\Software\p905` key, which the runner
deletes afterwards (the application's own key is never touched). The two
expected compile failures (`errors-forms`, `unknown-var`) are the measurement,
so their non-zero makensis exit is a pass.

Last reproduced 2026-10-10 from a clean checkout: all nine probes matched the
table, the key was deleted (`reg delete` exit 0).

## r1-drivers/

The #901 / R1 real-machine drivers, imported from that round's out-of-tree
harness so the runs are reproducible from the checkout. Scratch state lives
under `%TEMP%\ia-r1-drivers` unless `-EvidenceDir` / `-Keep` say otherwise.

| file | role |
| --- | --- |
| `drive-r1-update.ps1` | end-to-end update driver: install #1 → plant payload → install #2 → assert. `-Case success` plants a >MAX_PATH tree (only a working long-path prefix can remove it); `-Case failure` plants a DELETE-denied child (promote must record the leftover instead of orphaning it). Installs only under `%LOCALAPPDATA%\Programs\IA Installer Test*` and asserts the real product dirs are untouched |
| `leftover-probe.nsi` | compiled with `/DPROBE_TARGET=<long dir>` / `/DPROBE_CONTROL=<short dir>`; reports both `${FileExists}` forms on the long target, the short control, and a deep write attempt |
| `run-leftover-probe.ps1` | builds the >MAX_PATH tree, compiles and runs the probe from it, prints the reading, removes the scratch tree |

```powershell
# test artifact first (prints the installer path it built):
node scripts/test-windows-installer.mjs --compile-only
powershell -File r1-drivers/drive-r1-update.ps1 -Installer <installer-test.exe> -Tag t1 -Case success -Expect fixed
powershell -File r1-drivers/run-leftover-probe.ps1
```

`drive-r1-update.ps1` is the driver the #901 evidence logs came from (logs stay
out of tree; the driver writes them under its evidence dir). Last run
2026-10-10 from the checkout: `run-leftover-probe.ps1` printed
`target_len=327 unprefixed=[false] prefixed=[true] control_short=[true] deepwrite=[failed]`
and removed its scratch tree.
