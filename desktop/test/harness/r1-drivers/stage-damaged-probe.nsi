; Compile the PRODUCTION iaStageApplication macro into this focused probe (#914
; review Axis A, user-ruled must-fix).
;
; Why this probe exists next to process-gate-probe.nsi: that one drives the gate
; FUNCTION directly, so it cannot see where the call sits inside the macro. The
; Axis A defect was exactly a placement defect — the call sat inside the
; `${If} $iaProbeFound == "1"` branch, and that branch skips staging entirely
; when the app executable is gone, so a damaged installation (exe quarantined or
; deleted by hand, bundled service still running) was never classified and the
; stock section installed straight over the live service.
;
; The runner replaces the markers with production text taken verbatim from
; installer-directories.nsh and installer-cleanup.nsh, prepares a fake install
; tree with or without the app executable (or no tree at all, for the
; first-install leg of #1074), leaves a real Windows process running under it
; (or none), inserts the production macro, and reads what happened.
; The probe runs silent, so the gate's MessageBox (/SD IDOK) answers itself.
;
; Two readings: the exit code (2 = the gate refused, and then no result file is
; written at all, because the refusal `Quit`s) and the result file the macro
; writes when it returns.
;
; Only ${INSTALL_REGISTRY_KEY} and ${APP_EXECUTABLE_FILENAME} differ from the
; shipped build: the probe points the registry at a scratch key so the run never
; touches the product's own HKCU entry. The macro, the two gate functions, the
; existence probe and the two cleanup helpers are the shipped text, unchanged.
Unicode true
RequestExecutionLevel user
Name "IA stage damaged probe"
OutFile "stage-damaged-probe.exe"
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

!define INSTALL_REGISTRY_KEY "Software\IA Probe 914 Stage"
!define APP_EXECUTABLE_FILENAME "app.exe"

; Stand-ins for the production LangStrings (installer.nsh); a silent run never
; shows them, they only keep the injected production text compiling.
LangString iaAppRunning ${LANG_ENGLISH} "probe stand-in: still running"
LangString iaProcessCheckFailed ${LANG_ENGLISH} "probe stand-in: could not tell"
LangString iaRecoveryMetadataFailed ${LANG_ENGLISH} "probe stand-in: metadata"
LangString iaStaleBackup ${LANG_ENGLISH} "probe stand-in: stale backup"
LangString iaRollbackFailed ${LANG_ENGLISH} "probe stand-in: rollback failed"
LangString iaUpdateFailed ${LANG_ENGLISH} "probe stand-in: update failed"

; The variables the injected production text writes. The cleanup helpers bring
; their own (installer-cleanup.nsh declares the shape/scan/delete state).
Var iaFinalDirectory
Var iaBackupDirectory
Var iaProbeFound
Var iaRunningProduct
Var iaRuntimeStopStatus
Var iaRuntimeExitWaits
Var iaPowerShellPath

; installer-cleanup.nsh: iaCheckBackupShape / iaBuildLongPath (plus the reparse
; scan and the prepare wrapper, which the injected macro does not reach).
@@PRODUCTION_IA_CLEANUP@@

; installer-directories.nsh: the existence probe, the two gate functions, the
; backup-pointer macros, and the staging macro under test.
@@PRODUCTION_IA_PROBE_PATH@@

@@PRODUCTION_IA_BACKUP_MACROS@@

@@PRODUCTION_IA_CLASSIFY@@

@@PRODUCTION_IA_REQUIRE@@

@@PRODUCTION_IA_STAGE@@

Section
  StrCpy $INSTDIR "${PROBE_INSTALL}"

  ; The production customInit calls this macro. It either returns (the gate
  ; proved the tree clean, or stopped the bundled runtime) or `Quit`s with exit
  ; code 2 — in which case this Section never reaches the write below.
  !insertmacro iaStageApplication

  FileOpen $9 "${PROBE_RESULT}" w
  FileWrite $9 "leg=${PROBE_LEG} stage_returned=1 answer=$iaRunningProduct probe=$iaProbeFound$\r$\n"
  FileClose $9
SectionEnd
