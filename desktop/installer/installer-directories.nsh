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
;                          move the live $INSTDIR aside to $INSTDIR.old-<guid>
;                          and stash the backup path in the registry.
;                          A running app locks its directory, so a failed
;                          rename also blocks updating over a live process.
;   iaPromoteApplication (customInstall, end of the install section):
;                          if the new files are in place, delete the backup;
;                          otherwise roll back.
;   iaRollbackApplication: remove the partial install and rename the backup
;                          back. A backup that cannot be restored is left in
;                          place, never deleted (same rule as upstream).
;   Only directories this installer created or renamed are ever removed.
;
; Bootstrapping note: the backup is created by the NEW installer, so
; rollback-on-update-failure works for updates FROM this release onward.

!include "LogicLib.nsh"

Var iaFinalDirectory
Var iaBackupDirectory

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

; customInit: move a previous installation aside before the section
; (and its uninstallOldVersion step) touches it.
!macro iaStageApplication
  StrCpy $iaFinalDirectory $INSTDIR
  StrCpy $iaBackupDirectory ""
  ${If} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
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
  SetOutPath $TEMP
  DetailPrint "Rolling back to $iaBackupDirectory"
  RMDir /r "$iaFinalDirectory"
  ClearErrors
  Rename $iaBackupDirectory $iaFinalDirectory
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaRollbackFailed): $iaBackupDirectory" /SD IDOK
  ${Else}
    ; The program is usable again. Note: DisplayVersion keeps the failed
    ; version's value (only InstallLocation is restored) — cosmetic, and the
    ; next successful update rewrites the whole entry.
    WriteRegStr HKCU "${INSTALL_REGISTRY_KEY}" "InstallLocation" $iaFinalDirectory
  ${EndIf}
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
    ; Long-path prefix so deep runtime trees (node_modules-style) are removed.
    RMDir /r "\\?$iaBackupDirectory"
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
