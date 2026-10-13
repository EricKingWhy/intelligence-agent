; Compile the production #914 process gate into this focused probe.
;
; The runner (run-process-gate-probe.ps1) replaces the two markers with the
; iaClassifyRunningProduct / iaRequireNoRunningProduct functions taken verbatim
; from installer-directories.nsh, prepares a fake install tree, leaves a real
; Windows process running under it (or none), and then reads what the gate did.
; The probe runs silent, so the gate's MessageBox (whose /SD IDOK the shipped
; installer relies on) answers itself instead of hanging.
;
; Two readings per leg:
;   answer_before  the production classification's answer (0 / 11 / 12 / other)
;   stock          the stock FIND_PROCESS answer (0 = "running", 1 = "not
;                  running") for the state the leg prepared. The contrast is the
;                  measured root cause of #914: a single CimInstance has no
;                  synthetic Count, so the shipped predicate reads the
;                  one-process case as "not running" (1), and the shipped guard
;                  then updates over a live service.
; The gate call follows: it returns on a proven-clean answer, and aborts with
; exit code 2 for everything else — in which case no reading is appended here
; and the runner reads the process state itself.
Unicode true
RequestExecutionLevel user
Name "IA process gate probe"
OutFile "process-gate-probe.exe"
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

; Stand-ins for the production LangStrings (installer.nsh). Silent install: they
; are never shown, they only keep the injected production text compiling.
LangString iaAppRunning ${LANG_ENGLISH} "probe stand-in: still running"
LangString iaProcessCheckFailed ${LANG_ENGLISH} "probe stand-in: could not tell"

; The variables the injected production functions write (installer-directories.nsh).
Var iaFinalDirectory
Var iaRunningProduct
Var iaRuntimeStopStatus
Var iaRuntimeExitWaits
Var iaPowerShellPath
; Probe-only registers.
Var iaProbeStock

; The two production functions, injected verbatim (each marker carries its own
; Function/FunctionEnd, exactly as the shipped include writes them).
@@PRODUCTION_IA_CLASSIFY@@

@@PRODUCTION_IA_REQUIRE@@

Section
  StrCpy $INSTDIR "${PROBE_INSTALL}"
  StrCpy $iaFinalDirectory $INSTDIR

  ; Production resolves the interpreter inside the gate, before its first
  ; classification; the probe calls the classifier directly first, so it resolves
  ; the same interpreter here (the stock reading below needs it too).
  StrCpy $iaPowerShellPath "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"

  Call iaClassifyRunningProduct
  StrCpy $1 "$iaRunningProduct"

  ; The stock FIND_PROCESS predicate, verbatim from app-builder-lib 26.15.3
  ; (templates/nsis/include/allowOnlyOneInstallerInstance.nsh:66): the contrast
  ; this ticket is about.
  nsExec::Exec `"$iaPowerShellPath" -C "if ((Get-CimInstance -ClassName Win32_Process | ? {$$_.Path -and $$_.Path.StartsWith('$INSTDIR', 'CurrentCultureIgnoreCase')}).Count -gt 0) { exit 0 } else { exit 1 }"`
  Pop $iaProbeStock


  FileOpen $9 "${PROBE_RESULT}" w
  FileWrite $9 "leg=${PROBE_LEG} answer_before=$1 stock=$iaProbeStock gate_returned=0$\r$\n"
  FileClose $9

  Call iaRequireNoRunningProduct

  FileOpen $9 "${PROBE_RESULT}" w
  FileWrite $9 "leg=${PROBE_LEG} answer_before=$1 stock=$iaProbeStock gate_returned=1 answer_after=$iaRunningProduct$\r$\n"
  FileClose $9
SectionEnd
