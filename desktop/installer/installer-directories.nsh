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
;                          move the live $INSTDIR aside to $INSTDIR.old-{guid}
;                          (the braces are System::Call's "g" GUID form) and
;                          stash the backup path in the registry. A pointer
;                          an earlier run left behind is recorded as a leftover
;                          first (#904): the pointer is a single slot, and a
;                          live backup must not vanish from every record when
;                          the slot is overwritten.
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

Var iaFinalDirectory
Var iaBackupDirectory
Var iaLeftoverDirectory
Var iaProbeFound

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

; customInit: move a previous installation aside before the section
; (and its uninstallOldVersion step) touches it.
!macro iaStageApplication
  StrCpy $iaFinalDirectory $INSTDIR
  StrCpy $iaBackupDirectory ""
  ${If} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
    ; #904 item 2: the pointer is a single slot, so the value below overwrites
    ; whatever an earlier run left there. A value that still names a directory
    ; (the app was killed between the rename and promote) is recorded as a
    ; leftover first, so losing the slot never loses the directory from every
    ; record; a value that names nothing is only noted, and a value without the
    ; backup shape is refused and signalled in the exit code — it is not
    ; something this installer ever writes, and publishing a foreign path as a
    ; "leftover to delete" would be worse than dropping the hint.
    !insertmacro iaReadBackupDir
    ${If} $iaBackupDirectory != ""
      StrCpy $iaDeleteCandidate "$iaBackupDirectory"
      StrCpy $iaDeleteBase "$iaFinalDirectory"
      StrCpy $iaDeleteShapeCheck "1"
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
    ClearErrors
    Rename $INSTDIR $iaBackupDirectory
    ${If} ${Errors}
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaAppRunning)" /SD IDOK
      SetErrorLevel 2
      Quit
    ${EndIf}
    !insertmacro iaStashBackupDir
    SetOutPath $INSTDIR
  ${EndIf}
!macroend

; Restore the previous version after a failed update. The partial new
; $INSTDIR is removed; the backup is renamed back. If the restore rename
; fails (locked files), the complete backup is left in place and reported.
Function iaRollbackApplication
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
  DetailPrint "Rolling back to $iaBackupDirectory"
  ; #904 item 5: the partial install is removed through the shared prepare step,
  ; which refuses a tree containing a reparse point — RMDir /r deletes THROUGH
  ; a junction (measured), and this delete is older than the backup one.
  StrCpy $iaDeleteCandidate "$iaFinalDirectory"
  StrCpy $iaDeleteShapeCheck "0"
  Call iaPrepareDelete
  StrCmp $iaDeleteStatus "ok" 0 iaRollbackRefused
  ClearErrors
  RMDir /r "$iaDeleteTarget"
  ; The delete's own error is not read here: the restore is the operation that
  ; has to succeed, and a partial install that survived the delete makes the
  ; rename below fail on a non-empty directory — the recorded, non-zero case.
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
    ; The program is usable again. Note: DisplayVersion keeps the failed
    ; version's value (only InstallLocation is restored) — cosmetic, and the
    ; next successful update rewrites the whole entry.
    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "InstallLocation" $iaFinalDirectory
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
  ${If} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
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
  ; If the install section never reached iaPromoteApplication (extraction
  ; error, user cancel, crash of the section), restore the previous version.
  !insertmacro iaReadBackupDir
  ${If} $iaBackupDirectory != ""
    Call iaRollbackApplication
  ${EndIf}
FunctionEnd
!endif
