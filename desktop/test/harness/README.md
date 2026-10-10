# Installer guard test harness (#901 / #905)

Opt-in checks that `npm test` does not run: the test script only globs
`test/*.test.ts` and `test/*.test.mjs`, and these mutate files in place.

Both teeth scripts mutate their source files in place, restore the original
bytes and verify the restore byte-for-byte. An interrupted run (Ctrl-C, or an
exception inside the run) restores through the SIGINT/SIGTERM handlers and a
`finally`; a forced kill — `taskkill /F`, Task Manager, closing the console —
runs no handler at all and can leave the mutated text behind as the next run's
baseline (measured — it happened once in the #919 review round and once to a
probe, and both times the left-behind mutation was only noticed afterwards),
which is why the byte-for-byte check runs at the end of every run **and**
`teeth-check-guards.mjs` refuses a baseline that is not the committed guard: it
hashes the text it reads at startup against the recorded `GUARD_BASELINE_SHA256`
and exits 1 with a `guard baseline mismatch` line when they differ (update that
constant in the same change that edits the guard's text). Every mutation must
turn the suite red; a survivor is a hole in the assertions, and the run exits
non-zero when
one remains (or when the restore is not byte-identical). `teeth-check.mjs`
walks its list once per installer script it covers and runs
`node --test test/installer-scripts.test.mjs`.
`teeth-check-guards.mjs` runs both guard test files —
`test/installer-scripts.test.mjs` and `test/installer-node-runtime.test.mjs` —
because the node-runtime lock rules (#919 Q11) live in the second one.

| script | mutates | what it pins | last run |
| --- | --- | --- | --- |
| `teeth-check.mjs` | `installer/installer-directories.nsh` (7 mutations) and `installer.nsh` (5) | the promote site's leftover record (#904): long-path probe replaced by the unprefixed one, no probe at all, swapped branches, record never read back, the record block in a dead branch, the whole block in a dead branch — plus the #919 rules: the failed delete's exit code (Q6) and the sweep's arming, declined branch, prompt and ask-before-delete (Q8) | 12/12 red, 0 survivors, restores identical (sha256 `edbf02d4…`, `f99f887a…`) |
| `teeth-check-guards.mjs` | `scripts/build-windows-installer.mjs` | every guard rule, one mutation per rule — among them the #919 rules: the backup-site arming requirement (Q1/Q2/Q3), the sweep exit-code and prompt rules (Q3/Q8), the rollback record's place on the failure branch (Q7), the prepared-target delete options and the stray scan reading code (Q4), and the node engines minor floor (Q11) — plus the #919 review dispositions: the candidate/arming quote spellings and the fail-closed reads (F1), the `!insertmacro` in the arming window (F1/A4), the declined branch's reach to the delete pass (F3), the unclosed restore window (F2), the declined exit in the dead-branch scan (S2), and the engines floor anchor and patch (S4) — the disposition review's rules: the window's insert/define/include/other-statement readings (R1/R2), the whole-window candidate source (N1), and the delete loop's tail as part of the pass (R3) — and the fix round's review: the loop span read from any label reference, the hop chase into the pass, and the `${...}` token taking dotted names — and the disposal rounds: the failure leg read by the read's own form and the label-name alphabet, the depth-2 define token, the quoted-span operand set, the define-carried recursive delete and the `!include` set, the two messages, and the divider-less `${Switch}` body | 111/111 red, 0 survivors, restore identical (sha256 `482a65e0…`) |

The #919 batch added five mutations to `teeth-check.mjs` (the failed delete's
exit code plus the four installer.nsh sweep rules — the first entries that do
not rewrite `installer-directories.nsh`) and thirteen to
`teeth-check-guards.mjs`; the #919 review disposition added ten more to the
guard harness (the candidate and arming spellings, the values the guard cannot
read, the `!insertmacro` in the arming window, the declined branch's reach, the
unclosed restore window, the declined exit in the dead-branch scan, the engines
floor anchor and patch), the disposition review's own round added six more
(one per reading the window gained: the insertion bodies, the `${define}`
expansions, the `!include`, any other statement using the delete state, the
candidate source as the whole window, and the loop tail in the delete pass),
and the fix round's own review added three (the delete-loop span read from any
statement, the hop chase into the pass, and the dotted-define token). The #919
residuals disposal then closed the review findings it inherited with its own
additions (the F-1/N1/F-2 readings and the dash and quoted spellings), which the
review of that disposal measured at 90 guard mutations and 12 for the installer
scripts, and its own two fix rounds added fourteen and then seven (the
failure-leg forms and the label-name alphabet; the depth-2 define token, the
quoted-span operand set, the define-carried delete, the `!include` set, the two
messages, the `${Switch}` body and the baseline digest) — the harnesses now walk
111 guard mutations and 12 installer ones. Per protocol §8.3 #4 the disposal
round's new mutations share test-level failure sets (five single-test sets plus
one that fails seven tests; the assertion diffs inside a shared test still
differ, measured), so they are not cited as per-rule discrimination.
Both were re-run from the checkout on 2026-10-10
with 0 survivors and byte-identical restores. The disposition's first
`teeth-check-guards.mjs` run also caught a real gap in the disposition itself:
the candidate-copy mutation survived (every fixture armed with `"1"`), which is
why the exemption's four spellings are asserted with the flag off.

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

The round-6 additions (2026-10-10 review of #904) are the sweep's armed shape
check (`StrCpy $iaDeleteShapeCheck "1"` above its prepare call — with it off,
every `"$INSTDIR.old-*"` sibling is deleted), the sweep's exit code pinned to
`SetErrorLevel 2` *below* the delete, and the stray-delete scan (any recursive
delete that is not the prepared target is a problem).

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

### the #919 limits probe (self-driving)

```powershell
powershell -File limits-probe/run-limits-probe.ps1
```

`limits-probe/limits-probe.nsi` runs in two modes, one invocation each:
`readings` takes every measurement once, into a fresh file, and `mimic`
recurses the scan's per-level runtime load (4 pushes + one Call — the shape of
`iaScanReparsePointsBody`) to the depth given by `/DPROBE_CALL_MAX` and writes
a single line once the ladder returns. The runner runs the readings once and
the mimic once per ladder depth, and grades each mimic run by its exit code,
because a run whose stack dies cannot write its line afterwards. (A first
version wrote progress from inside the recursion with `FileOpen "a"`; the
append writes landed at the file's beginning and overwrote the first reading,
which is why this one writes nothing during the recursion. The result file is
opened before the recursion, so a crashed rung reads back empty rather than
absent.) Each completed rung's exit 0 must come with the depth it printed
(`mimic_completed=<depth>`): exit 0 alone would also pass a recursion that
returned early, and the rung only measures the real per-level load when the
ladder ran to the depth.

| reading | value | what it decides |
| --- | --- | --- |
| `long_findfirst_1..4` | `len=419/826/1603/3009 handle=[…] first=[.] entries=3` | `FindFirst` through `\\?\` walks a tree past `MAX_PATH` at every depth the leftover record keeps |
| `unc_plain_findfirst`, `unc_long_findfirst` | `handle=[…] first=[.]` for both spellings | both UNC forms are readable by `FindFirst` |
| `unc_plain_rmdir` | `flag=clear dir_gone=yes inner_gone=yes` | the plain UNC sibling is the writability control: the share works and the tree goes |
| `unc_long_rmdir` | `flag=set dir_gone=no inner_gone=no` | **`RMDir /r` on the `\\?\UNC\` form sets the error flag and deletes nothing**, on the tree its own `FindFirst` read a line earlier. `iaBuildLongPath` builds that form for a UNC target, so a prepared delete there only reaches the site's failure branch (sweep: candidate kept, exit 2; rollback: leftover recorded, rename fails on the surviving tree). Measured; registered on #919 as a new finding, not fixed in that batch |
| mimic ladder `512 / 1024 / 1300` | `exit=0`, `mimic_completed=<depth>` | the 512 valve in `iaScanReparsePointsBody` is reached long before the stack runs out |
| mimic ladder `1400` | `exit=-1073741571` (`STATUS_STACK_OVERFLOW`) | the only depth limit above the valve is the stack itself, between 1300 and 1400 levels — no NSIS call-depth cap; the valve has ≥2.5x headroom on this frame shape |

Last run 2026-10-10 from the checkout: readings run exit 0, ladder `VERDICT:
mimic ladder matched expectations`.

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
