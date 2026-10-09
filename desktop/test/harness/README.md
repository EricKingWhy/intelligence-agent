# Installer guard test harness (#901 / #905)

Opt-in checks that `npm test` does not run: the test script only globs
`test/*.test.ts` and `test/*.test.mjs`, and these mutate files in place.

Both teeth scripts mutate one source file, run
`node --test test/installer-scripts.test.mjs`, restore the original bytes and
verify the restore byte-for-byte. Every mutation must turn the suite red; a
survivor is a hole in the assertions, and the run exits non-zero when one
remains (or when the restore is not byte-identical).

| script | mutates | what it pins | last run |
| --- | --- | --- | --- |
| `teeth-check.mjs` | `installer/installer-directories.nsh` | the promote site's leftover record (#904): 6 mutated shapes — long-path probe replaced by the unprefixed one, no probe at all, swapped branches, record never read back, the record block in a dead branch, the whole block in a dead branch | 6/6 red, 0 survivors, restore identical (sha256 `47261c9d…`) |
| `teeth-check-guards.mjs` | `scripts/build-windows-installer.mjs` | every guard rule, one mutation per rule | 40/40 red, 0 survivors, restore identical (sha256 `94841cf9…`) |

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
styles, macro insertion) and the #904 rules (the prepare call and status gate in
front of every recursive delete of a prepared target, the delete-site policy
table, the sweep's enumerate-ask-report requirements, the sweep's `${GetParent}`
argument order — FileFunc takes the path first — plus the fail-closed rejection
of a bare-variable first argument, the rollback site's leftover record,
fail-closed missing-site detection, and the cleanup-helper primitive list).

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

### the #904 probes (self-driving)

`junction-probe/`, `semantics-probe/`, `shape-probe/` and
`launch-form-probe/` need scratch directories, so each carries its own runner:
it builds the tree under `%TEMP%\ia-<name>-*`, compiles the `.nsi` with the
paths as `/D` defines, runs it and removes the tree (`-Keep` retains it).
`run-probes.ps1` copies `*.nsi` from this directory only, not the
subdirectories, so the families do not interfere.

```powershell
powershell -File junction-probe/run-junction-probe.ps1
powershell -File semantics-probe/run-semantics-probe.ps1
powershell -File shape-probe/run-shape-probe.ps1
powershell -File launch-form-probe/run-launch-form-probe.ps1
```

| probe | question | measured |
| --- | --- | --- |
| `junction-probe/` | #904 item 5: does `RMDir /r` follow a junction? | **yes, and without an error**: `errors_after=[false] dir_exists=[false]` while the junction TARGET's payload is gone — the recursive delete walked through the reparse point. This is why a prepared delete refuses a tree that contains one |
| `semantics-probe/` | the semantics the reparse refusal and the leftover sweep rest on | table below |
| `shape-probe/` | #904 item 4: does the shipped shape predicate accept exactly `iaStageApplication`'s own backup name? | **yes, and nothing else**: 11/11 cases against the shipped `installer-cleanup.nsh` (included from its real path; one case derives its candidate from `CoCreateGuid`), and the red proof — `make-unbraced-predicate.mjs`'s earlier form, `sha256 dfa4756d…` — fails exactly the three braced-name cases and the UNC base (`failures=4`). That first predicate expected `old-<8-4-4-4-12>` without braces and so refused the installer's OWN backup name (its GUID lives in `System::Call`'s braces); the unit fixtures shared the assumption, and the R1 driver's junction case caught it on a real update |
| `launch-form-probe/` | #904 item 4: which uninstaller launch form carries the section's exit code? | **only an in-place launch does**; readings below |

`semantics-probe/` readings (2026-10-10, reproduced twice):

| reading | value | what it decides |
| --- | --- | --- |
| `vars_across_call`, `regs_across_call` | `clobbered` | a `Call` may not assume the callee preserves `$0`/`$1` or `$R0`/`$R1`; the helpers document their work set and callers rebuild. The probe first shipped section 7 with the path pieces in `$1`/`$2` across the helper call and measured a garbage path (`\\?\UNC\localhost\16$0`) |
| `empty_findfirst` | `handle=[…] name=[.] after_next=[..]` | `FindFirst` yields the dot entries, so the sweep has to skip them |
| `missing_findfirst` | `handle=[] name=[]` | enumerating a directory that is not there leaves the handle empty and the loop body cannot run (fail-closed) |
| `attrs_plain`, `attrs_junction`, `attrs_missing` | `16`, `1040`, `-1` | the raw `GetFileAttributesW` values the shape check reads |
| the `0x400` mask on each | `no`, `yes`, `yes` | `-1 & 0x400 != 0`: an unreadable or missing path reads as a reparse point, so the refusal fails closed |
| `prefixed_findfirst` | `handle=[…] first=[.]` | the `\\?\` prefix works for `FindFirst`, not only for the attribute call |
| `unc_plain_attrs`, `unc_long_attrs`, `unc_bare_attrs`, `drive_long_attrs` | `16`, `16`, `-1`, `16` | measured on `%WINDIR%` through its admin share: `\\localhost\C$\WINDOWS`, `\\?\UNC\localhost\C$\WINDOWS`, `\\?\localhost\C$\WINDOWS` (invalid), `\\?\C:\WINDOWS`. `\\?\UNC\` resolves like the plain path and the bare `\\?\` + UNC form is rejected, so `iaBuildLongPath`'s UNC form is the only usable prefix for a UNC target. A P/Invoke comparison of the A and W entry points measured the same four values |
| `empty_rmdir_flag` | `set keep_after=yes` | `RMDir /r ""` removes nothing and SETS the error flag: a caller that ignores the status cannot turn a refusal into a delete |
| `missing_rmdir_flag`, `missing_long_rmdir_flag` | `clear`, `clear` | deleting a path that is not there is not an error, so only a real failure reaches the "failed" branch and a missing backup directory has to be refused by the prepare check |

`launch-form-probe/` readings (2026-10-10, both path variants — with and
without a space — reproduced twice):

| reading | value | what it decides |
| --- | --- | --- |
| `plain_*` | `rc 0`, `EXEDIR=%TEMP%\~nsu.tmp`, the dir removed | the stub copies itself and runs the copy, so a plain launch (double-click, Start menu, `Uninstall.exe /S`) reports 0 for every artifact: the sweep's exit code cannot be read from it |
| `self_raw_*` | `rc 2`, `EXEDIR=<install dir>`, `keep.txt` removed, uninstaller and dir kept | `/S _?=<dir>` with the value last and **unquoted** runs the file in place and returns the section's `SetErrorLevel` — the driver's in-place step, and the form the stock updater's own call uses (`installUtil.nsh`: `ExecWait '"$uninstallerFileNameTemp" /S ... _?=$installationDir' $R0`) |
| `self_quoted_*` | `rc 2`, **no marker, nothing deleted** | a quoted value is not a usable invocation: it returns 2 while the script never runs. A reading that must not be taken for the sweep's report — an earlier probe run mistook exactly this for a confirmed in-place launch |
| `self_array_spaced_*` / `self_array_nospace_*` | spaced: `rc 0`, dir removed; nospace: `rc 2`, in place | an array element goes through PowerShell's native argument quoting: with a space the token comes out quoted, the leading `"` hides `_?=` from NSIS and the stub silently falls back to the copy form. This is why the driver builds its in-place line as one string |
| `copy_raw_*` | `rc 2`, dir removed whole | electron-builder's primary shape (a copy outside the install dir, then `_?=<dir>`) also carries the code, and nothing runs from inside the directory so the section removes it completely |

## r1-drivers/

The #901 / R1 real-machine drivers, imported from that round's out-of-tree
harness so the runs are reproducible from the checkout. Scratch state lives
under `%TEMP%\ia-r1-drivers` unless `-EvidenceDir` / `-Keep` say otherwise.

| file | role |
| --- | --- |
| `drive-r1-update.ps1` | end-to-end update driver: install #1 → plant payload → install #2 → assert. `-Case success` plants a >MAX_PATH tree (only a working long-path prefix can remove it); `-Case failure` plants a DELETE-denied child (promote must record the leftover instead of orphaning it); `-Case junction` (#904 item 5) plants a junction and asserts promote refuses the backup, records it and leaves the link and its target intact; `-Case sweep` (#904 item 4) drives the uninstaller with four hand-built siblings (deletable / junction-bearing / unbraced / shape-invalid) through two launches: a plain launch (rc 0 for every artifact) and an in-place launch (rc 2 with the sweep on the fixed artifact, rc 0 with no sweep on the pre-#904 one). Installs only under `%LOCALAPPDATA%\Programs\IA Installer Test*` and asserts the real product dirs are untouched |
| `leftover-probe.nsi` | compiled with `/DPROBE_TARGET=<long dir>` / `/DPROBE_CONTROL=<short dir>`; reports both `${FileExists}` forms on the long target, the short control, and a deep write attempt |
| `run-leftover-probe.ps1` | builds the >MAX_PATH tree, compiles and runs the probe from it, prints the reading, removes the scratch tree |

```powershell
# test artifact first (prints the installer path it built):
node scripts/test-windows-installer.mjs --compile-only
powershell -File r1-drivers/drive-r1-update.ps1 -Installer <installer-test.exe> -Tag t1 -Case success -Expect fixed
powershell -File r1-drivers/run-leftover-probe.ps1
```

`drive-r1-update.ps1` is the driver the #901 evidence logs came from (logs stay
out of tree; the driver writes them under its evidence dir). Last runs
2026-10-10 from the checkout, all four #904 cases `assertions_failed=0`:
`-Case sweep` fixed and baseline (plain: rc 0 with the sweep's deletions on the
fixed artifact, deletable sibling kept on the baseline; in place: rc 2 fixed /
rc 0 baseline) and `-Case junction` fixed and baseline (fixed: backup kept,
leftover recorded, junction target intact; baseline: backup removed, target
destroyed). `run-leftover-probe.ps1` printed
`target_len=327 unprefixed=[false] prefixed=[true] control_short=[true] deepwrite=[failed]`
and removed its scratch tree. The `success` and `failure` cases were last run
from the checkout on 2026-10-09 with the same driver.
