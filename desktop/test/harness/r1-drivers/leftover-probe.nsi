; Does ${FileExists} see a >MAX_PATH directory without the long-path prefix?
; The #901 leftover-record block reads a recorded path back out of the registry
; and probes it with ${FileExists}, so this mirrors that exactly: write the
; target into HKCU, read it back, then probe both forms on the same directory.
; $EXEDIR is NOT usable here: measured on NSIS 3.0.4.1 it comes back in 8.3
; short form (92 chars for a 340-char directory), which is short enough to
; make the unprefixed probe succeed and hide the very difference under test.
; ASCII only; the target arrives as /DPROBE_TARGET=<dir> at compile time.

Unicode true
RequestExecutionLevel user
Name "IA leftover exists probe"
OutFile "leftover-probe.exe"
SilentInstall silent
ShowInstDetails hide

!include "LogicLib.nsh"

!ifndef PROBE_TARGET
  !error "PROBE_TARGET is required"
!endif
!ifndef PROBE_CONTROL
  !error "PROBE_CONTROL is required"
!endif

Section
  WriteRegStr HKCU "Software\IA901Probe" "Target" "${PROBE_TARGET}"
  ReadRegStr $0 HKCU "Software\IA901Probe" "Target"
  StrLen $4 "$0"
  ${If} ${FileExists} "$0"
    StrCpy $2 "true"
  ${Else}
    StrCpy $2 "false"
  ${EndIf}
  ${If} ${FileExists} "\\?\$0"
    StrCpy $3 "true"
  ${Else}
    StrCpy $3 "false"
  ${EndIf}
  ; Control: the same two forms on a short directory, so a "false" above cannot
  ; be blamed on the probe itself being broken.
  ${If} ${FileExists} "${PROBE_CONTROL}"
    StrCpy $5 "true"
  ${Else}
    StrCpy $5 "false"
  ${EndIf}
  ; The deep directory also proves the writing side: an unprefixed >MAX_PATH
  ; path fails there too, so the reading goes to $TEMP instead.
  ClearErrors
  FileOpen $8 "$0\leftover-probe-write.txt" w
  ${If} ${Errors}
    StrCpy $6 "failed"
  ${Else}
    StrCpy $6 "ok"
    FileClose $8
  ${EndIf}
  FileOpen $9 "$TEMP\leftover-probe-result.txt" w
  FileWrite $9 "target_len=$4 unprefixed=[$2] prefixed=[$3] control_short=[$5] deepwrite=[$6]$\r$\n"
  FileWrite $9 "target=[$0]$\r$\n"
  FileClose $9
  DeleteRegKey HKCU "Software\IA901Probe"
SectionEnd
