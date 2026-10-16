; #361 [W-16] Atomic install-directory swap with rollback.
;
; ADAPTED from DeepSeek Harness (MIT License):
;   apps/desktop/scripts/installer-directories.nsh (lines 1-135)
;   https://github.com/deepseek-ai/deepseek-harness
;   commit 5badb15009ae1756c3afe0ae0cef1faafc290ccc
; Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.
;
; Protocol (same failure semantics as upstream, adapted to stock
; electron-builder NSIS hooks because the stock template owns file
; extraction and cannot redirect it into a staging directory):
;   iaStageApplication   (customInit, before the install section):
;                          refuse the update while the product is running from
;                          $INSTDIR (iaRequireNoRunningProduct, #914: stop the
;                          bundled runtime, abort for anything else — the rename
;                          alone does not stop a headless service, see §1.3 of
;                          docs/live_gate/w21/58-issue-914-service-in-use-gate.md).
;                          The gate runs on the same path as the move it guards,
;                          and whether or not the app executable is still
;                          present: a damaged installation keeps its live
;                          service, and the branch below would skip staging for
;                          it entirely. Then move the live $INSTDIR aside to
;                          $INSTDIR.old-{guid}
;                          (the braces are System::Call's "g" GUID form) and
;                          stash the backup path in the registry. A pointer
;                          an earlier run left behind is recorded as a leftover
;                          first (#904): the pointer is a single slot, and a
;                          live backup must not vanish from every record when
;                          the slot is overwritten.
;                          The move needs no lock, so a process that starts
;                          while the installer is staging is never classified
;                          by the first call; the gate is therefore called a
;                          second time inside that window, after the working
;                          directory is released and before the backup pointer
;                          is armed (#1066): a runtime that appeared in
;                          between is stopped there, and any other answer
;                          leaves the installation byte-identical, so the
;                          refusal needs no rollback. A damaged installation
;                          returns before that second call, so only the first
;                          one runs on that path.
;   iaPromoteApplication (customInstall, end of the install section):
;                          if the new files are in place, delete the backup;
;                          otherwise roll back. The delete runs only on a
;                          pointer that passed the backup-shape check, and only
;                          removes a tree without a reparse point (#904): a
;                          junction inside the backup made RMDir /r delete
;                          through it (measured), so a hit is refused, kept and
;                          recorded instead. A backup that cannot be deleted is
;                          left in place and recorded — always the
;                          IaLeftoverDir value in this application's own
;                          registry key and the DetailPrint line, plus
;                          $(iaStaleBackup) in a UI install (a silent one has no
;                          box to show it; the detail line only reaches a file
;                          in a log-enabled build). A kept backup also sets the
;                          exit code to 2 — a refusal and a failed delete alike
;                          (#919 Q6). That record names the most recent
;                          leftover; the authoritative discovery path is
;                          the uninstaller's name-shape sweep (installer.nsh),
;                          so a superseded record loses nothing. A promote that
;                          gets past the checks drops the record once no path
;                          form can see that directory, while one that returns
;                          early leaves it. The backup pointer is cleared either
;                          way, so a finished install can never be mistaken for
;                          an incomplete one below.
;   iaRollbackApplication: remove the partial install and rename the backup
;                          back. The pointer must pass the shape check before it
;                          may steer a delete or a rename (#904: HKCU-writable),
;                          the partial install is removed only when it holds no
;                          reparse point, and a restore that fails records the
;                          complete backup and sets a non-zero exit code (a
;                          silent install has no dialog to read).
;   Only directories this installer created or renamed are ever removed.
;
; Bootstrapping note: the backup is created by the NEW installer, so
; rollback-on-update-failure works for updates FROM this release onward.

!include "LogicLib.nsh"
!include "nsDialogs.nsh"

Var iaFinalDirectory
Var iaBackupDirectory
Var iaLeftoverDirectory
Var iaProbeFound
; #914 process gate (iaRequireNoRunningProduct): the last classification answer
; ("0" = nothing runs, "11" = only the bundled runtime, "12" = more than the
; runtime, anything else = undetermined), the stop call's exit code (log only),
; the bounded re-ask counter, and the PowerShell path the gate resolves itself —
; the template fills its own $PowerShellPath only inside the section, long after
; .onInit runs this gate.
Var iaRunningProduct
Var iaRuntimeStopStatus
Var iaRuntimeExitWaits
Var iaPowerShellPath
Var iaRollbackStatus
Var iaRecoveryPending
Var iaRecoveryChoice
Var iaRecoveryRestoreButton
Var iaRecoveryFreshButton

; ---------------------------------------------------------------------------
; #914 [W-16 R2] process gate: a directory holding a live installation must not
; be moved. Measured facts this is built on (probes and readings:
; docs/live_gate/w21/58-issue-914-service-in-use-gate.md §1):
;   - the atomic rename below is NOT a guard for the product: an exe image is
;     opened share-delete, so a headless process does not block a directory
;     rename — with only the bundled service running, the rename succeeded and
;     the service kept answering new clients out of "<install dir>.old-{guid}".
;     Only the Electron client's own handles block it (leg ① of #365's readings);
;   - the stock CHECK_APP_RUNNING cannot cover that case: its predicate ends in
;     `(... | ? {...}).Count -gt 0`, and a SINGLE CimInstance carries no synthetic
;     Count, so `$null -gt 0` is false and the guard answers "not running"
;     whenever exactly one process matches — the service-only case is exactly one
;     match (probe log: the same "not running" reading before and after a force
;     kill). The stock probe still runs in the section; it is the template's.
; So this gate asks with the cast forced (@(...)) and refuses the update for
; every answer it cannot prove clean (fail-closed) instead of inheriting that
; failure mode. It is called on every install that reaches staging, which is
; the same path as the move it guards — customInit wraps both the call and the
; staging in `${If} $iaRecoveryPending != "1"`, so a pending recovery skips
; both together (installer.nsh) — and not only while the app executable is
; still there: a damaged installation whose exe is gone
; (quarantined or deleted by hand) can still hold the live bundled service, and
; the stock section would install straight over that directory in place. While a
; process from this directory survives the stop attempt, this installer records
; nothing, stages nothing and renames nothing.
;
; iaClassifyRunningProduct — what runs from $iaFinalDirectory?
;   exit code 0  : nothing
;   exit code 11 : only the bundled runtime ($iaFinalDirectory\resources\python\)
;   exit code 12 : anything else, or the runtime plus anything else (desktop
;                  client, TUI, …)
;   anything else: undetermined — PowerShell missing or blocked, WMI/CIM failure,
;                  or a parse error. Windows PowerShell itself exits 1 in those
;                  cases, which is why the three answers above avoid 1 and 2.
; The path prefix carries its separator on purpose: the stock predicate's bare
; `StartsWith('$INSTDIR')` also matches sibling directories that merely share the
; text prefix ("…\Intelligence Agent 2", "…\Intelligence Agent.old-…").
Function iaClassifyRunningProduct
  nsExec::Exec `"$iaPowerShellPath" -NoProfile -NonInteractive -Command "$$ErrorActionPreference='Stop'; $$iaRoot='$iaFinalDirectory\'; $$iaRuntime='$iaFinalDirectory\resources\python\'; try { $$iaAll=@(Get-CimInstance -ClassName Win32_Process | Where-Object { $$_.Path -and $$_.Path.StartsWith($$iaRoot,'CurrentCultureIgnoreCase') }); if ($$iaAll.Count -eq 0) { exit 0 }; $$iaRuntimeProcs=@($$iaAll | Where-Object { $$_.Path.StartsWith($$iaRuntime,'CurrentCultureIgnoreCase') }); if ($$iaRuntimeProcs.Count -eq $$iaAll.Count) { exit 11 }; exit 12 } catch { exit 13 }"`
  Pop $iaRunningProduct
  DetailPrint "iaClassifyRunningProduct: answer $iaRunningProduct for $iaFinalDirectory"
FunctionEnd

; iaRequireNoRunningProduct — the policy: a proven-clean answer returns; the
; bundled runtime on its own is stopped and asked again (bounded); everything
; else refuses the update with exit code 2 and a message, before this installer
; has recorded or moved anything.
; The interactive client is never killed here: it holds the user's live session,
; the mature installers do not force-close a GUI on update either (Inno Setup
; documents the unsaved-work loss; DeepSeek Harness aborts instead of killing),
; and the user is standing in front of the wizard — $(iaAppRunning) tells them to
; close it. A refusal that cannot distinguish the cases reports the probe itself
; as the problem ($(iaProcessCheckFailed)).
Function iaRequireNoRunningProduct
  ; The template sets its own $PowerShellPath inside the section, long after this
  ; gate runs; resolve the same interpreter here.
  StrCpy $iaPowerShellPath "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"
  StrCpy $iaRuntimeExitWaits "0"
  Call iaClassifyRunningProduct
  ${If} $iaRunningProduct == "11"
    ; Only the headless runtime is up: stop it (#914 adjudication — try to stop
    ; the service first, refuse the update only when it does not stop). -Force,
    ; because a headless service has no message loop to close and the installer
    ; has no channel to ask it; work in flight is covered by the product's own
    ; crash recovery (src/agent_harness/recovery/coordinator.py), which is what
    ; the ticket's "safe pause + ledger settle" would otherwise rebuild.
    nsExec::Exec `"$iaPowerShellPath" -NoProfile -NonInteractive -Command "Get-CimInstance -ClassName Win32_Process | Where-Object { $$_.Path -and $$_.Path.StartsWith('$iaFinalDirectory\resources\python\','CurrentCultureIgnoreCase') } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }"`
    Pop $iaRuntimeStopStatus
    DetailPrint "iaRequireNoRunningProduct: stop bundled runtime (exit $iaRuntimeStopStatus)"
    ; Stop-Process returns before the process object is gone, so re-ask with a
    ; bounded wait (8 × 250 ms) before calling the runtime unstoppable — the same
    ; "give it a moment to exit" idea as the stock guard's Sleep/KILL retry loop
    ; and DeepSeek Harness's 10 s grace period.
    ${DoWhile} $iaRunningProduct == "11"
      IntOp $iaRuntimeExitWaits $iaRuntimeExitWaits + 1
      ${If} $iaRuntimeExitWaits > 8
        ${ExitDo}
      ${EndIf}
      Sleep 250
      Call iaClassifyRunningProduct
    ${Loop}
  ${EndIf}
  ${If} $iaRunningProduct == "0"
    DetailPrint "iaRequireNoRunningProduct: clean after $iaRuntimeExitWaits wait(s)"
    Return
  ${EndIf}
  DetailPrint "iaRequireNoRunningProduct: refusing the update (answer $iaRunningProduct after $iaRuntimeExitWaits wait(s))"
  ${If} $iaRunningProduct == "11"
  ${OrIf} $iaRunningProduct == "12"
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaAppRunning)" /SD IDOK
  ${Else}
    ; Not a proven-clean answer — PowerShell missing or blocked, WMI/CIM broken,
    ; or a parse error. Fail closed, the refusal DeepSeek Harness makes when its
    ; probe returns a value it cannot read.
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaProcessCheckFailed)" /SD IDOK
  ${EndIf}
  SetErrorLevel 2
  Quit
FunctionEnd

; Existence probe for a path this protocol recorded (#904). The unprefixed form
; cannot name a longer-than-MAX_PATH path and a UNC path has to be probed in its
; "\\?\UNC\" form, so the probe goes through iaBuildLongPath. Installer-only:
; the uninstaller seeks leftovers by name shape and never probes a record, and a
; variable that is never referenced is a fatal warning (6001) in that build.
; Clobbers $0 (through iaBuildLongPath).
Function iaProbePath
  StrCpy $iaProbeFound "0"
  StrCpy $0 "$iaPlainPath" 2
  StrCmp $0 "\\" iaProbePathLong
  IfFileExists "$iaPlainPath" 0 iaProbePathLong
  StrCpy $iaProbeFound "1"
  Return
iaProbePathLong:
  Call iaBuildLongPath
  IfFileExists "$iaDeleteTarget" 0 iaProbePathDone
  StrCpy $iaProbeFound "1"
iaProbePathDone:
FunctionEnd

; Stash the backup location where customInstall / .onGUIEnd can find it.
; Per-user installer: the current-user hive is always correct.
!macro iaStashBackupDir
  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaBackupDir" $iaBackupDirectory
!macroend

!macro iaClearBackupDir
  DeleteRegValue HKCU "${INSTALL_REGISTRY_KEY}" "IaBackupDir"
!macroend

!macro iaReadBackupDir
  ReadRegStr $iaBackupDirectory HKCU "${INSTALL_REGISTRY_KEY}" "IaBackupDir"
!macroend

; The record a failed delete leaves behind (#901). It is read back on every
; promote so it can be dropped once the directory it names is gone.
!macro iaReadLeftoverDir
  ReadRegStr $iaLeftoverDirectory HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir"
!macroend

; An earlier installer may have been hard-killed after moving the live install
; aside. Defer the choice to the custom page before the stock installer writes
; into the now-missing or partial $INSTDIR. Silent installs fail closed: they
; cannot choose between restoring an older version and continuing fresh.
!macro iaRecoverInterruptedInstall
  StrCpy $iaFinalDirectory $INSTDIR
  !insertmacro iaReadBackupDir
  ${If} $iaBackupDirectory != ""
    StrCpy $iaPlainPath "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
    Call iaProbePath
    ${If} $iaProbeFound != "1"
      StrCpy $iaShapeCandidate "$iaBackupDirectory"
      StrCpy $iaShapeBase "$iaFinalDirectory"
      Call iaCheckBackupShape
      ${If} $iaShapeOk != "1"
        DetailPrint "iaRecoverInterruptedInstall: refusing backup shape: $iaBackupDirectory"
        SetErrorLevel 2
        !insertmacro iaClearBackupDir
        Quit
      ${EndIf}
      StrCpy $iaPlainPath "$iaBackupDirectory"
      Call iaProbePath
      ${If} $iaProbeFound != "1"
        DetailPrint "iaRecoverInterruptedInstall: backup is missing: $iaBackupDirectory"
        SetErrorLevel 2
        !insertmacro iaClearBackupDir
        Quit
      ${EndIf}
      IfSilent iaInterruptedSilent
      StrCpy $iaRecoveryPending "1"
      Goto iaInterruptedDone
iaInterruptedSilent:
      DetailPrint "iaRecoverInterruptedInstall: rerun interactively to restore or continue fresh"
      SetErrorLevel 2
      Quit
iaInterruptedDone:
    ${EndIf}
  ${EndIf}
!macroend

; Show the explicit recovery choices before the stock welcome page. Pressing
; Enter or any hidden wizard control cannot bypass the user's choice.
Function iaRecoveryPageCreate
  ${If} $iaRecoveryPending != "1"
    Abort
  ${EndIf}
  StrCpy $iaRecoveryChoice ""
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    DetailPrint "iaRecoveryPageCreate: could not create recovery page"
    SetErrorLevel 2
    Quit
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "$(iaRecoveryTitle)" "$(iaRecoveryDescription)"
  ${NSD_CreateLabel} 0u 0u 100% 36u "$(iaInterruptedInstall)"
  Pop $0
  ${NSD_CreateLabel} 0u 38u 100% 34u "$iaBackupDirectory"
  Pop $0
  ${NSD_CreateButton} 0u 82u 100% 24u "$(iaRestorePrevious)"
  Pop $iaRecoveryRestoreButton
  ${NSD_OnClick} $iaRecoveryRestoreButton iaRecoveryRestoreClick
  ${NSD_CreateButton} 0u 114u 100% 24u "$(iaContinueFresh)"
  Pop $iaRecoveryFreshButton
  ${NSD_OnClick} $iaRecoveryFreshButton iaRecoveryFreshClick
  GetDlgItem $0 $HWNDPARENT 1
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 3
  ShowWindow $0 ${SW_HIDE}
  nsDialogs::Show
FunctionEnd

Function iaRecoveryPageLeave
  StrCmp $iaRecoveryChoice "continue" iaRecoveryPageLeaveContinue
  StrCmp $iaRecoveryChoice "restore" iaRecoveryPageLeaveRestore
  Abort
iaRecoveryPageLeaveContinue:
  Call iaContinueInterruptedFresh
  Return
iaRecoveryPageLeaveRestore:
  Call iaRollbackApplication
  StrCmp $iaRollbackStatus "restored" iaRecoveryPageLeaveRestoreSucceeded
  SetErrorLevel 2
  Quit
iaRecoveryPageLeaveRestoreSucceeded:
  !insertmacro quitSuccess
FunctionEnd

Function iaRecoveryRestoreClick
  Pop $0
  StrCpy $iaRecoveryChoice "restore"
  GetDlgItem $0 $HWNDPARENT 1
  SendMessage $0 ${BM_CLICK} 0 0
FunctionEnd

Function iaRecoveryFreshClick
  Pop $0
  StrCpy $iaRecoveryChoice "continue"
  GetDlgItem $0 $HWNDPARENT 1
  ShowWindow $0 ${SW_SHOW}
  SendMessage $0 ${BM_CLICK} 0 0
FunctionEnd

Function iaContinueInterruptedFresh
  ; Keep the old version discoverable while the new installation runs. Leave
  ; IaBackupDir armed until iaPromoteApplication commits so cancel and restart
  ; can still restore it. The uninstaller's shape sweep remains full discovery.
  ClearErrors
  !ifdef IA_TEST_INJECT_RECOVERY_METADATA_FAILURES
    ReadRegStr $0 HKCU "${INSTALL_REGISTRY_KEY}" "IaTestFailLeftoverRecord"
    ${If} $0 == "1"
      SetErrors
    ${Else}
      ClearErrors
      WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory
    ${EndIf}
  !else
    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory
  !endif
  ${If} ${Errors}
    DetailPrint "iaContinueInterruptedFresh: could not record backup directory"
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaRecoveryMetadataFailed)" /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
  ; Let the stock installer write the selected fresh version into $INSTDIR.
  ; Refuse a partial tree with reparse points or unreadable entries before
  ; allowing NSIS to write through it; the old backup stays armed on refusal.
  SetOutPath $TEMP
  ; #1201 item 1: "continue fresh" is the other half of the recovery protocol and
  ; it is the one that bypasses the staging gate entirely — .onInit skips
  ; !insertmacro iaStageApplication while $iaRecoveryPending is "1", and this
  ; function only clears that flag after the user pressed the button here, so
  ; neither of the gate's two call sites runs on this path. The stock section's
  ; own CHECK_APP_RUNNING then guards the write, and its predicate is the
  ; one-process blind spot #914 documents (`$null -gt 0`, so a single matching
  ; process reads as "not running"). Asking here, before the flag is cleared and
  ; before the section can write, is what makes the recovery choices share one
  ; predicate. Same placement rule as the rollback call and for the same measured
  ; reason: after SetOutPath $TEMP, so the PowerShell child does not inherit a
  ; working directory inside the tree.
  ; The refusal below keeps IaBackupDir armed, exactly like the two refusals
  ; further down that already say so: the backup has not been decided about, the
  ; recovery page comes back on the next run, and a cleared pointer would leave
  ; the backup on disk with nothing naming it.
  Call iaRequireNoRunningProduct
  StrCpy $iaPlainPath "$iaFinalDirectory"
  Call iaProbePath
  ${If} $iaProbeFound == "1"
    StrCpy $iaDeleteCandidate "$iaFinalDirectory"
    StrCpy $iaDeleteShapeCheck "0"
    Call iaPrepareDelete
    StrCmp $iaDeleteStatus "ok" 0 iaInterruptedFreshUnsafe
    ; No recursive delete is needed for the empty directory produced by
    ; the rename interruption. The stock section overlays any partial files.
    Goto iaInterruptedFreshSafeDone
iaInterruptedFreshUnsafe:
    DetailPrint "iaRecoverInterruptedInstall: refusing partial install ($iaDeleteStatus)"
    SetErrorLevel 2
    Quit
iaInterruptedFreshSafeDone:
  ${EndIf}
  StrCpy $iaRecoveryPending "0"
  SetOutPath $iaFinalDirectory
FunctionEnd

; customInit: move a previous installation aside before the section
; (and its uninstallOldVersion step) touches it.
!macro iaStageApplication
  StrCpy $iaFinalDirectory $INSTDIR
  StrCpy $iaBackupDirectory ""
  StrCpy $iaPlainPath "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
  Call iaProbePath
  ; #914: this directory is about to move, or to be written through, so nothing
  ; may be running from it. The gate runs before the app-executable branch rather
  ; than inside it: a damaged installation whose exe is gone still holds the
  ; service that answers clients, and that branch skips staging for it entirely —
  ; an in-branch call never classified it, so the stock section installed
  ; straight over a live service in place.
  ; It also runs before the backup pointer is read or armed and before the
  ; rename. Every refusal it raises therefore leaves the installation exactly as
  ; it was, and a non-empty IaBackupDir keeps meaning "the install section
  ; started" for .onGUIEnd and iaRollbackApplication.
  Call iaRequireNoRunningProduct
  ${If} $iaProbeFound == "1"
    ; #1066 test hook: hold the window between the gate above and the last-moment
    ; re-check below open, so a driver can start a process inside it. Compiled only
    ; when the probe defines IA_TEST_HOLD_STAGE_WINDOW; the shipped installer does
    ; not define it, so the product binary never carries this block.
    !ifdef IA_TEST_HOLD_STAGE_WINDOW
      WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaTestStageWindowOpen" "1"
      Sleep 5000
      DeleteRegValue HKCU "${INSTALL_REGISTRY_KEY}" "IaTestStageWindowOpen"
    !endif
    ; #904 item 2: the pointer is a single slot, so the value below overwrites
    ; whatever an earlier run left there. A value that still names a directory
    ; (the app was killed between the rename and promote) is recorded as a
    ; leftover first, so losing the slot never loses the directory from every
    ; record; a value that names nothing is only noted. A value without the
    ; backup shape is foreign state: refuse it and clear the slot before any
    ; success path can overwrite the refusal or replace that value.
    !insertmacro iaReadBackupDir
    ${If} $iaBackupDirectory != ""
      StrCpy $iaShapeCandidate "$iaBackupDirectory"
      StrCpy $iaShapeBase "$iaFinalDirectory"
      Call iaCheckBackupShape
      ${If} $iaShapeOk == "1"
        StrCpy $iaPlainPath "$iaBackupDirectory"
        Call iaProbePath
        ${If} $iaProbeFound == "1"
          WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory
          DetailPrint "iaStageApplication: earlier backup kept as a leftover: $iaBackupDirectory"
        ${Else}
          DetailPrint "iaStageApplication: earlier backup pointer names nothing, not recorded: $iaBackupDirectory"
        ${EndIf}
      ${Else}
        DetailPrint "iaStageApplication: earlier backup pointer refused (shape): $iaBackupDirectory"
        SetErrorLevel 2
        !insertmacro iaClearBackupDir
        Quit
      ${EndIf}
    ${EndIf}
    System::Call 'ole32::CoCreateGuid(g .r0) i .r1'
    ${If} $1 != 0
      SetErrorLevel 2
      Quit
    ${EndIf}
    StrCpy $iaBackupDirectory "$INSTDIR.old-$0"
    ; .onInit opened a handle on $INSTDIR via SetOutPath; release it first.
    SetOutPath $TEMP
    ; #1066: the gate above runs at the top of this macro, so everything between it
    ; and the rename is a window in which the bundled runtime or a client can start.
    ; Re-check here, after the working directory was released and before the backup
    ; pointer is armed: a refusal then leaves the installation exactly as it was and
    ; needs no rollback. A headless service does not block the rename (#914 section
    ; 1.3), so without this call the update lands and leaves the old process answering
    ; clients out of the "<dir>.old-{guid}" tree.
    Call iaRequireNoRunningProduct
    ClearErrors
    !insertmacro iaStashBackupDir
    ${If} ${Errors}
      DetailPrint "iaStageApplication: could not record backup directory"
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaRecoveryMetadataFailed)" /SD IDOK
      SetErrorLevel 2
      !insertmacro iaClearBackupDir
      Quit
    ${EndIf}
    ClearErrors
    Rename $INSTDIR $iaBackupDirectory
    ${If} ${Errors}
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaAppRunning)" /SD IDOK
      SetErrorLevel 2
      !insertmacro iaClearBackupDir
      Quit
    ${EndIf}
    !ifdef IA_TEST_HARD_KILL_AFTER_STAGE_RENAME
      Sleep 5000
    !endif
    SetOutPath $INSTDIR
  ${EndIf}
!macroend

; Restore the previous version after a failed update. The partial new
; $INSTDIR is removed; the backup is renamed back. If the restore rename
; fails (locked files), the complete backup is left in place and reported.
Function iaRollbackApplication
  StrCpy $iaRollbackStatus "failed"
  !insertmacro iaReadBackupDir
  ${If} $iaBackupDirectory == ""
    Return
  ${EndIf}
  ; #904 item 6: the pointer lives in HKCU, so it is user-writable; it must not
  ; steer a recursive delete or a rename before this check. A refused value is
  ; unusable and is cleared (keeping it would re-raise the anomaly on every
  ; later run), and the exit code says the restore did not happen.
  StrCpy $iaShapeCandidate "$iaBackupDirectory"
  StrCpy $iaShapeBase "$iaFinalDirectory"
  Call iaCheckBackupShape
  ${If} $iaShapeOk != "1"
    DetailPrint "iaRollbackApplication: refusing $iaBackupDirectory (shape)"
    !insertmacro iaClearBackupDir
    SetErrorLevel 2
    Return
  ${EndIf}
  SetOutPath $TEMP
  ; #1201 item 1: everything below removes $iaFinalDirectory recursively and
  ; renames the backup back over it, and a process running from that directory
  ; turns the pair into a partial delete plus a rename that fails on a
  ; non-empty directory — the recorded, non-zero case the restore below reports
  ; (that step deliberately does not read the delete's own error and lets the
  ; rename fail instead).
  ; The staging path refuses exactly this conflict before it moves anything
  ; (#914); the rollback path had no predicate at all, so the process that made
  ; the update fail could make the recovery fail too. The gate is reused, not
  ; re-derived: the bundled runtime is stopped and re-asked (bounded), anything
  ; else refuses. It sits after SetOutPath $TEMP for the reason #1066 measured —
  ; the PowerShell child it spawns must not inherit a current directory inside
  ; the tree this function is about to remove.
  ; A refusal here deliberately does NOT clear IaBackupDir. #1165's convention
  ; ("a terminal path always clears") covers a value that is unusable (the shape
  ; refusal above) or one that has reached a decision about the backup (the
  ; restore below, the refused delete); this refusal is not terminal — the user
  ; closes the product and runs the installer again, and the recovery page must
  ; come back with the pointer intact. Clearing it here would leave the backup on
  ; disk with no record naming it, which is the one outcome the recovery
  ; protocol exists to prevent.
  Call iaRequireNoRunningProduct
  DetailPrint "Rolling back to $iaBackupDirectory"
  ; #904 item 5: the partial install is removed through the shared prepare step,
  ; which refuses a tree containing a reparse point — RMDir /r deletes THROUGH
  ; a junction (measured), and this delete is older than the backup one.
  ; A hard kill can land after the backup pointer is written but before SetOutPath
  ; creates $iaFinalDirectory. Skip the scan only for the two Win32 not-found
  ; errors; access and other failures still go through the fail-closed scan.
  StrCpy $iaPlainPath "$iaFinalDirectory"
  Call iaBuildLongPath
  StrCpy $0 "$iaDeleteTarget"
  System::Call 'kernel32::GetFileAttributes(t r0)i.r1 ?e'
  Pop $2
  IntCmp $1 -1 iaRollbackCheckMissing iaRollbackPrepareDelete iaRollbackPrepareDelete
iaRollbackCheckMissing:
  IntCmp $2 2 iaRollbackRestoreBackup iaRollbackCheckPathMissing iaRollbackCheckPathMissing
iaRollbackCheckPathMissing:
  IntCmp $2 3 iaRollbackRestoreBackup iaRollbackPrepareDelete iaRollbackPrepareDelete
iaRollbackPrepareDelete:
  StrCpy $iaDeleteCandidate "$iaFinalDirectory"
  StrCpy $iaDeleteShapeCheck "0"
  Call iaPrepareDelete
  StrCmp $iaDeleteStatus "ok" 0 iaRollbackRefused
  ClearErrors
  RMDir /r "$iaDeleteTarget"
  ; The delete's own error is not read here: the restore is the operation that
  ; has to succeed, and a partial install that survived the delete makes the
  ; rename below fail on a non-empty directory — the recorded, non-zero case.
iaRollbackRestoreBackup:
  ClearErrors
  Rename $iaBackupDirectory $iaFinalDirectory
  ${If} ${Errors}
    ; #904 item 1: a restore that fails leaves a complete backup behind; record
    ; it exactly like a failed promote delete and signal failure in the exit
    ; code, because a silent install has no dialog to read.
    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory
    DetailPrint "iaRollbackApplication: could not restore $iaBackupDirectory"
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaRollbackFailed): $iaBackupDirectory" /SD IDOK
    SetErrorLevel 2
  ${Else}
    StrCpy $iaRollbackStatus "restored"
    ; The program is usable again. Note: DisplayVersion keeps the failed
    ; version's value (only InstallLocation is restored) — cosmetic, and the
    ; next successful update rewrites the whole entry.
    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "InstallLocation" $iaFinalDirectory
    !insertmacro iaReadLeftoverDir
    ${If} $iaLeftoverDirectory == $iaBackupDirectory
      DeleteRegValue HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir"
    ${EndIf}
  ${EndIf}
  !insertmacro iaClearBackupDir
  StrCpy $INSTDIR $iaFinalDirectory
  SetOutPath $INSTDIR
  Return
iaRollbackRefused:
  ; Nothing was deleted and nothing was renamed: the partial install stays as
  ; it is because a reparse point or an unreadable child sits under it. The
  ; backup is recorded so it is not lost, and the exit code says the restore
  ; did not happen.
  WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory
  DetailPrint "iaRollbackApplication: refusing to remove $iaFinalDirectory ($iaDeleteStatus)"
  MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaRollbackFailed): $iaBackupDirectory" /SD IDOK
  SetErrorLevel 2
  !insertmacro iaClearBackupDir
  StrCpy $INSTDIR $iaFinalDirectory
  SetOutPath $INSTDIR
FunctionEnd

; customInstall (end of the install section): the new files must be in
; place, otherwise roll back to the stashed previous version.
Function iaPromoteApplication
  !insertmacro iaReadBackupDir
  ${If} $iaBackupDirectory == ""
    Return
  ${EndIf}
  StrCpy $iaPlainPath "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
  Call iaProbePath
  ${If} $iaProbeFound == "1"
    !insertmacro iaReadLeftoverDir
    StrCmp $iaLeftoverDirectory $iaBackupDirectory iaPromoteBackupAlreadyLeftover
    ; #904 item 6/5: the pointer has to pass the backup-shape check before it
    ; steers a recursive delete (it lives in HKCU), and the delete itself only
    ; runs on a tree without a reparse point. iaPrepareDelete builds the
    ; long-path form for the delete either way: #901 (R1) measured that NSIS
    ; strings have no backslash escapes, so a hand-built "\\?$iaBackupDirectory"
    ; lost the separator backslash and made this delete fail on every update,
    ; silently (0.7 GB of unreferenced previous versions per update on one
    ; machine).
    StrCpy $iaDeleteCandidate "$iaBackupDirectory"
    StrCpy $iaDeleteBase "$iaFinalDirectory"
    StrCpy $iaDeleteShapeCheck "1"
    Call iaPrepareDelete
    StrCmp $iaDeleteStatus "ok" 0 iaPromoteDeleteSkipped
    ClearErrors
    RMDir /r "$iaDeleteTarget"
    ${If} ${Errors}
      StrCpy $iaDeleteStatus "failed"
    ${EndIf}
iaPromoteDeleteSkipped:
    ; Keep anything that was not removed discoverable instead of orphaning it:
    ; a silent install has no UI, so the registry is the record the user or
    ; tooling can read back. Both a refused delete (reparse point, unreadable
    ; child, tree too deep) and a failed one record the path; a shape refusal
    ; records nothing — that value is not this installer's backup, and a foreign
    ; path must not be published as "delete this folder". A kept backup — a
    ; refusal or a failed delete — reports the same reading: exit code 2 (#919
    ; Q6), like the uninstaller sweep (installer.nsh) and the rollback rename
    ; failure. The pointer below is
    ; still cleared on purpose — .onGUIEnd and iaRollbackApplication read a
    ; non-empty IaBackupDir as "the install section never completed" and would
    ; roll back a good update over a directory that is merely undeletable.
    ${If} $iaDeleteStatus == "refused-shape"
      DetailPrint "iaPromoteApplication: refusing $iaBackupDirectory ($iaDeleteStatus)"
      SetErrorLevel 2
    ${ElseIf} $iaDeleteStatus != "ok"
      WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir" $iaBackupDirectory
      DetailPrint "iaPromoteApplication: could not remove $iaBackupDirectory ($iaDeleteStatus)"
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaStaleBackup) $iaBackupDirectory" /SD IDOK
      SetErrorLevel 2
    ${EndIf}
    ; An earlier record names a directory that may be gone by now (removed by
    ; hand, or by a later delete that got through). Drop it only when no path
    ; form can see it, and keep it otherwise: clearing it blindly would hide an
    ; older leftover behind the update that just succeeded, and a single form on
    ; its own answers "false" for a directory that exists but that form cannot
    ; express — measured on NSIS 3.0.4.1, ${FileExists} says "false" for an
    ; unprefixed >MAX_PATH path, and the bare "\\?\" prefix cannot name a UNC
    ; path (that needs "\\?\UNC\"). iaProbePath tries the forms that fit the
    ; path, so "not found" now means "not there".
    !insertmacro iaReadLeftoverDir
    ${If} $iaLeftoverDirectory != ""
      StrCpy $iaPlainPath "$iaLeftoverDirectory"
      Call iaProbePath
      ${If} $iaProbeFound != "1"
        DeleteRegValue HKCU "${INSTALL_REGISTRY_KEY}" "IaLeftoverDir"
      ${EndIf}
    ${EndIf}
iaPromoteBackupAlreadyLeftover:
    !insertmacro iaClearBackupDir
  ${Else}
    Call iaRollbackApplication
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaUpdateFailed)" /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
FunctionEnd

!ifndef BUILD_UNINSTALLER
Function .onGUIEnd
  ; Cancel on the recovery page leaves the backup pointer armed for next run.
  ${If} $iaRecoveryPending == "1"
    Return
  ${EndIf}
  ; If the install section never reached iaPromoteApplication (extraction
  ; error, user cancel, crash of the section), restore the previous version.
  !insertmacro iaReadBackupDir
  ${If} $iaBackupDirectory != ""
    Call iaRollbackApplication
  ${EndIf}
FunctionEnd
!endif
