; Compile the PRODUCTION iaRollbackApplication function into this focused probe
; (#1201 item 1).
;
; What the probe proves: the rollback path used to walk straight into
; `RMDir /r "$iaFinalDirectory"` + `Rename $iaBackupDirectory $iaFinalDirectory`
; with no predicate at all, so a process running from the directory it is about
; to remove turned the restore into a partial delete plus a rename that fails on
; a non-empty directory. The #1201 change reuses the #914 gate there instead of
; deriving a second rule: the bundled runtime is stopped and re-asked (bounded),
; anything else aborts the restore. A refusal is deliberately NOT terminal — the
; user closes the product and runs the installer again — so the backup pointer
; must survive it, which is what the client leg reads back out of the registry.
;
; Why the production text is harvested rather than paraphrased: a paraphrase
; tests the paraphrase. The runner substitutes the production marker slots below
; with text taken verbatim from installer-directories.nsh and
; installer-cleanup.nsh, so the readings describe the shipped function — and
; -DirectoriesSource can point this same driver at another revision of the file,
; which is how the red reading that shows these legs discriminate is produced.
;
; The state each leg runs against is the one iaRecoverInterruptedInstall leaves
; behind after a hard kill: $INSTDIR present but damaged (no app executable, so
; that macro recognises it and offers recovery), a real backup directory named
; "$INSTDIR.old-{guid}" (the braces are System::Call's "g" GUID form, and
; iaCheckBackupShape refuses a name without them), and IaBackupDir in HKCU
; pointing at it. The runner builds exactly that for every leg.
;
; The one-line reading is written in two pieces: `leg=... before=...` before the
; call, `after=...` after it. iaRollbackApplication returns on its normal paths,
; but the gate it now calls can `Quit` (the answer that means "something else is
; running"), and a Quit truncates this file mid-line. That truncation is itself a
; reading — it is the client leg's whole point — so a missing `after=` must be
; reported by the runner, never treated as a harness failure.
;
; Only ${INSTALL_REGISTRY_KEY} and ${APP_EXECUTABLE_FILENAME} differ from the
; shipped build: the probe points the registry at a scratch key so a run never
; touches the product's own HKCU entry. The rollback function, the gate, the
; existence probe, the backup macros and the cleanup helpers are the shipped
; text, unchanged.
Unicode true
RequestExecutionLevel user
Name "IA rollback gate probe"
OutFile "rollback-gate-probe.exe"
SilentInstall silent
ShowInstDetails hide

!include "LogicLib.nsh"

!ifndef PROBE_INSTALL
  !error "PROBE_INSTALL is required"
!endif
!ifndef PROBE_LEG
  !error "PROBE_LEG is required"
!endif
!ifndef PROBE_RESULT
  !error "PROBE_RESULT is required"
!endif

!define INSTALL_REGISTRY_KEY "Software\IA Probe 1201 Rollback"
!define APP_EXECUTABLE_FILENAME "app.exe"

; Stand-ins for the production LangStrings (installer.nsh); a silent run never
; shows them, they only keep the injected production text compiling. These are
; the three the harvested blocks reference — measured by grepping the injected
; text for "$(" — so a future gate message that adds a fourth is a makensis
; error here rather than a silently missing string.
LangString iaAppRunning ${LANG_ENGLISH} "probe stand-in: still running"
LangString iaProcessCheckFailed ${LANG_ENGLISH} "probe stand-in: could not tell"
LangString iaRollbackFailed ${LANG_ENGLISH} "probe stand-in: rollback failed"

; The variables the injected production text writes. The cleanup helpers bring
; their own (installer-cleanup.nsh declares the shape/scan/delete state:
; iaShapeCandidate, iaShapeBase, iaShapeOk, iaPlainPath, iaDeleteCandidate,
; iaDeleteBase, iaDeleteShapeCheck, iaDeleteTarget, iaDeleteStatus, iaScanDir,
; iaScanDepth, iaScanReparse), and declaring one of those again is a makensis
; error.
Var iaFinalDirectory
Var iaBackupDirectory
Var iaLeftoverDirectory
Var iaProbeFound
Var iaRunningProduct
Var iaRuntimeStopStatus
Var iaRuntimeExitWaits
Var iaPowerShellPath
Var iaRollbackStatus
; Probe-only: the pre-call reading, held across the call. It is a dedicated Var
; because the production text uses every one of $0-$9 and $R0-$R9 on the way
; through (iaBuildLongPath clobbers $0, iaCheckBackupShape clobbers $R1-$R7), so
; anything the probe keeps in a user variable is gone by the time the rollback
; returns.
Var iaProbeBefore
; Probe-only: the armed pointer as the probe itself saw it, captured before the
; call. iaRollbackApplication re-reads it through the same macro; capturing the
; value here is what turns an empty-pointer leg into a named reading (ptr=)
; instead of an unattributed one — an empty read reaches the function's early
; Return and shows as shape= / rc=failed, which looks like a gate failure
; rather than the setup failure it is.
Var iaPointerBefore

; installer-cleanup.nsh: iaCheckBackupShape / iaBuildLongPath /
; iaScanReparsePoints / iaPrepareDelete, instantiated in the plain name space by
; the file's own !ifdef BUILD_UNINSTALLER / !else (this build does not define it).
@@PRODUCTION_IA_CLEANUP@@

; installer-directories.nsh: the existence probe, the backup-pointer macros, the
; two gate functions, and the rollback function under test.
@@PRODUCTION_IA_PROBE_PATH@@

@@PRODUCTION_IA_BACKUP_MACROS@@

@@PRODUCTION_IA_CLASSIFY@@

@@PRODUCTION_IA_REQUIRE@@

@@PRODUCTION_IA_ROLLBACK@@

Section
  StrCpy $INSTDIR "${PROBE_INSTALL}"

  ; iaRollbackApplication reads $iaBackupDirectory from the registry and checks
  ; its name against $iaFinalDirectory through iaCheckBackupShape, and
  ; iaRecoverInterruptedInstall is what normally fills $iaFinalDirectory in. This
  ; probe drives the rollback function on the state that macro leaves behind, so
  ; it performs the same StrCpy first — otherwise the shape check would compare
  ; the backup name against an empty base and refuse.
  StrCpy $iaFinalDirectory $INSTDIR

  ; before= is the production existence probe against the damaged tree, not a
  ; driver-side guess: 0 is the state iaRecoverInterruptedInstall recognises
  ; ($INSTDIR exists but holds no app executable), which is how a real run
  ; reaches the rollback path at all.
  StrCpy $iaPlainPath "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
  Call iaProbePath
  StrCpy $iaProbeBefore "$iaProbeFound"

  ; ptr= is the armed pointer as this probe read it, with the production macro.
  ; Written before the call so a Quit still carries it: an empty value here says
  ; the leg never armed its precondition (a harness failure), an armed value says
  ; the function saw exactly what the driver intended.
  !insertmacro iaReadBackupDir
  StrCpy $iaPointerBefore "$iaBackupDirectory"

  ; The reading is written here and then written again after the call, rather
  ; than written once and appended to: mode "a" was measured to TRUNCATE the file
  ; on this NSIS build (3.0.4.1 — a two-write probe left only the second chunk),
  ; so an append would silently drop "before=". The value survives the call in
  ; its own Var because the production text owns every register ($0-$9 through
  ; iaBuildLongPath, $R1-$R7 through iaCheckBackupShape). The first write has no
  ; terminator, so a Quit below leaves a half-line, and the presence of "after="
  ; stays the reading that says the function returned.
  FileOpen $9 "${PROBE_RESULT}" w
  FileWrite $9 "leg=${PROBE_LEG} ptr=$iaPointerBefore before=$iaProbeBefore"
  FileClose $9

  ; Either returns (rc=restored, or a refused delete) or Quits from the gate
  ; (exit code 2) — in which case this Section never reaches the second write.
  Call iaRollbackApplication

  ; The error state the call left behind. No NSIS instruction reads the error
  ; LEVEL back — SetErrorLevel only writes the process exit code, which the
  ; driver reports as exit= — so this echoes the one error state a script can
  ; read. Read this as a trace, not as a verdict: on the restore path it is
  ; measured to be "set", because the production delete path recurses through
  ; iaScanReparsePoints, whose FindFirst/FindNext/FindClose loop leaves the flag
  ; set once the enumeration is exhausted (measured on this NSIS 3.0.4.1 with a
  ; bare enumeration loop). The probe reads it here, immediately after the
  ; production call returns, so no probe instruction has contributed to it yet.
  ; IfErrors clears the flag as it tests, which happens after the call has
  ; already returned and cannot change what it did or the exit code it set.
  StrCpy $5 "clear"
  IfErrors 0 iaProbeErrorFlagDone
  StrCpy $5 "set"
iaProbeErrorFlagDone:

  ; Read back after the call, because that is where the decision lands:
  ; running/stop/waits are the gate's own state (empty when the call never ran),
  ; backup/final are real filesystem existence checks on the two directories, and
  ; pointer is the state of the single backup slot the refusal must not clear.
  ; The existence checks use iaBackupDirectory rather than its registry value
  ; because a restore rename and the slot's bookkeeping are separate facts.
  StrCpy $1 "0"
  ${If} ${FileExists} "$iaBackupDirectory"
    StrCpy $1 "1"
  ${EndIf}
  StrCpy $2 "0"
  ${If} ${FileExists} "$iaFinalDirectory"
    StrCpy $2 "1"
  ${EndIf}
  StrCpy $3 "gone"
  ReadRegStr $4 HKCU "${INSTALL_REGISTRY_KEY}" "IaBackupDir"
  ${If} $4 != ""
    StrCpy $3 "present"
  ${EndIf}

  FileOpen $9 "${PROBE_RESULT}" w
  FileWrite $9 "leg=${PROBE_LEG} ptr=$iaPointerBefore before=$iaProbeBefore after=1 running=$iaRunningProduct stop=$iaRuntimeStopStatus waits=$iaRuntimeExitWaits shape=$iaShapeOk rc=$iaRollbackStatus backup=$1 delete=$iaDeleteStatus final=$2 pointer=$3 errflag=$5$\r$\n"
  FileClose $9
SectionEnd
