; #904 item 5: does NSIS 3.0.4.1's `RMDir /r` follow a junction?
;
; run-junction-probe.ps1 builds <root>\target (with content) and
; <root>\victim\link -- a junction to target -- compiles this probe with
; /DPROBE_TARGET=<the exact string to delete> /DPROBE_DIR=<dir to re-check>
; /DPROBE_RESULT=<reading file>, then inspects from PowerShell whether the
; junction TARGET's content survived. Target content gone = the recursive
; delete walked through the reparse point.
Unicode true
RequestExecutionLevel user
Name "IA junction probe"
OutFile "junction-probe.exe"
SilentInstall silent
ShowInstDetails hide

!include "LogicLib.nsh"

!ifndef PROBE_TARGET
  !error "PROBE_TARGET is required"
!endif
!ifndef PROBE_DIR
  !error "PROBE_DIR is required"
!endif
!ifndef PROBE_RESULT
  !error "PROBE_RESULT is required"
!endif

Section
  ClearErrors
  RMDir /r "${PROBE_TARGET}"
  ${If} ${Errors}
    StrCpy $2 "true"
  ${Else}
    StrCpy $2 "false"
  ${EndIf}
  ${If} ${FileExists} "${PROBE_DIR}\*.*"
    StrCpy $3 "true"
  ${Else}
    StrCpy $3 "false"
  ${EndIf}
  FileOpen $9 "${PROBE_RESULT}" w
  FileWrite $9 "errors_after=[$2] dir_exists=[$3]$\r$\n"
  FileClose $9
SectionEnd
