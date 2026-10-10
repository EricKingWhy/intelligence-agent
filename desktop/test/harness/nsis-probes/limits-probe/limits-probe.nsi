; #919 Q9: runtime limits and path-form behaviour the leftover sweep's
; fail-closed design rests on, measured on a real machine. Driven by
; run-limits-probe.ps1, which builds the fixtures and passes
;   /DPROBE_MODE=readings | mimic
; plus, for readings:
;   /DPROBE_LONG1..4=<dirs past the 327-char reading>  FindFirst through "\\?\"
;   /DPROBE_DRIVE=<drive letter of the scratch root>   UNC forms are built from
;   /DPROBE_UNC_TAIL_PLAIN=<tail of the plain target>  these two, because "$"
;   /DPROBE_UNC_TAIL_LONG=<tail of the long target>    cannot travel in a define
; and, for mimic:
;   /DPROBE_CALL_MAX=<recursion depth>                 the depth under test
;   /DPROBE_RESULT=<reading file>
;
; The two modes are separate runs on purpose. The mimic leg recurses until the
; stack dies at some depth, and a run that dies cannot write its line
; afterwards. A first version of this probe wrote progress from inside the
; recursion with FileOpen "a", and the append writes landed at the file's
; BEGINNING, overwriting the first reading (measured, #919 Q9); this version
; writes nothing during the recursion. The result file is opened before the
; recursion, so a crashed run leaves an empty file (measured: the 1400 rung
; reads back as no lines at all, not as absent), and the runner reads the
; outcome from the exit code instead of from mid-flight progress.
;
; Measurements (the readings the sweep's depth valve and path forms rest on):
;   1  FindFirst through "\\?\<dir>" past the 327-char boundary the leftover
;      probe measured: handle, first name and enumerated entries at ~400, ~800,
;      ~1600 and ~3000 characters
;   2  FindFirst and `RMDir /r` on the plain UNC path and on the
;      "\\?\UNC\<share>" form of real sibling trees, side by side: the plain
;      form is the writability control, and the pair says whether a failing
;      reading is about the form or about access
;   3  the scan's own load -- 4 pushes + one Call per level (the exact shape of
;      iaScanReparsePointsBody: handle, name, dir, depth, then Call ${FN_SELF})
;      -- recursed to PROBE_CALL_MAX levels, one depth per invocation

Unicode true
RequestExecutionLevel user
Name "IA limits probe"
OutFile "limits-probe.exe"
SilentInstall silent
ShowInstDetails hide

!include "LogicLib.nsh"

!ifndef PROBE_MODE
  !error "PROBE_MODE is required"
!endif
!if "${PROBE_MODE}" == "readings"
  !define PROBE_MODE_READINGS
!else
  !if "${PROBE_MODE}" == "mimic"
    !define PROBE_MODE_MIMIC
  !else
    !error "PROBE_MODE must be readings or mimic"
  !endif
!endif
!ifndef PROBE_RESULT
  !error "PROBE_RESULT is required"
!endif

!ifdef PROBE_MODE_READINGS
  !ifndef PROBE_LONG1
    !error "PROBE_LONG1 is required in readings mode"
  !endif
  !ifndef PROBE_LONG2
    !error "PROBE_LONG2 is required in readings mode"
  !endif
  !ifndef PROBE_LONG3
    !error "PROBE_LONG3 is required in readings mode"
  !endif
  !ifndef PROBE_LONG4
    !error "PROBE_LONG4 is required in readings mode"
  !endif
  !ifndef PROBE_DRIVE
    !error "PROBE_DRIVE is required in readings mode"
  !endif
  !ifndef PROBE_UNC_TAIL_PLAIN
    !error "PROBE_UNC_TAIL_PLAIN is required in readings mode"
  !endif
  !ifndef PROBE_UNC_TAIL_LONG
    !error "PROBE_UNC_TAIL_LONG is required in readings mode"
  !endif
!endif
!ifdef PROBE_MODE_MIMIC
  !ifndef PROBE_CALL_MAX
    !error "PROBE_CALL_MAX is required in mimic mode"
  !endif
!endif

!ifdef PROBE_MODE_MIMIC
Var iaProbeDepth

; iaScanReparsePointsBody's runtime load per level: four pushes, then a Call
; into the same body. The pushes are never popped until the level returns, so
; the stack grows four entries per level exactly like the real scan. Nothing
; is written from inside the recursion: a stack overflow must not be able to
; leave a half-written line behind, and the depth is known from the define.
Function iaProbeDescend
  Push $3
  Push $4
  Push $0
  Push $1
  IntOp $iaProbeDepth $iaProbeDepth + 1
  IntCmp $iaProbeDepth ${PROBE_CALL_MAX} iaProbeDeepest iaProbeDeeper iaProbeDeepest
iaProbeDeeper:
  Call iaProbeDescend
iaProbeDeepest:
  Pop $1
  Pop $0
  Pop $4
  Pop $3
  Return
FunctionEnd
!endif

!ifdef PROBE_MODE_READINGS
; $5 = "handle=[..] first=[..]", $6 = entries enumerated until the name ran
; empty, $7 = the path's length.
!macro iaProbeLongFind N PATH_VALUE
  FindFirst $3 $4 "\\?\${PATH_VALUE}\*.*"
  StrCpy $5 "handle=[$3] first=[$4]"
  StrCpy $6 0
  iaProbeLongFind${N}Loop:
    StrCmp $4 "" iaProbeLongFind${N}Done
    IntOp $6 $6 + 1
    FindNext $3 $4
    Goto iaProbeLongFind${N}Loop
  iaProbeLongFind${N}Done:
  FindClose $3
  StrLen $7 "${PATH_VALUE}"
  FileWrite $R9 "long_findfirst_${N}=len=$7 $5 entries=$6$\r$\n"
!macroend

; Reads the directory in $0 through FindFirst and leaves "handle=[..] first=[..]"
; in $7.
!macro iaProbeFind PATH_VALUE
  FindFirst $3 $4 "${PATH_VALUE}\*.*"
  StrCpy $7 "handle=[$3] first=[$4]"
  FindClose $3
!macroend

; $5/$6 in, $8 out: appends "dir_gone=.. inner_gone=.." for the form in $0.
!macro iaProbeGoneCheck PATH_VALUE
  ${If} ${FileExists} "${PATH_VALUE}"
    StrCpy $8 "dir_gone=no"
  ${Else}
    StrCpy $8 "dir_gone=yes"
  ${EndIf}
  ${If} ${FileExists} "${PATH_VALUE}\inner.txt"
    StrCpy $8 "$8 inner_gone=no"
  ${Else}
    StrCpy $8 "$8 inner_gone=yes"
  ${EndIf}
!macroend

; $5/$6 in, writes "unc_<label>_rmdir=flag=<set|clear> dir_gone=.. inner_gone=.."
; for the path in $0.
!macro iaProbeRmdirFlag LABEL
  ClearErrors
  RMDir /r "$0"
  IfErrors 0 iaProbe${LABEL}Clear
    StrCpy $7 "unc_${LABEL}_rmdir=flag=set"
    Goto iaProbe${LABEL}FlagDone
  iaProbe${LABEL}Clear:
    StrCpy $7 "unc_${LABEL}_rmdir=flag=clear"
  iaProbe${LABEL}FlagDone:
  !insertmacro iaProbeGoneCheck $0
  StrCpy $7 "$7 $8"
  FileWrite $R9 "$7$\r$\n"
!macroend
!endif

Section
  FileOpen $R9 "${PROBE_RESULT}" w
!ifdef PROBE_MODE_READINGS
  ; --- 1: FindFirst through "\\?\" past the 327-char reading --------------
  ; Each path is longer than the legacy boundary and exists on disk; the
  ; reading is the handle the enumeration returned plus the entries it walked.
  !insertmacro iaProbeLongFind 1 ${PROBE_LONG1}
  !insertmacro iaProbeLongFind 2 ${PROBE_LONG2}
  !insertmacro iaProbeLongFind 3 ${PROBE_LONG3}
  !insertmacro iaProbeLongFind 4 ${PROBE_LONG4}

  ; --- 2: the two UNC forms, read and deleted ----------------------------
  ; Both paths go through a variable, like iaProbePath's callers. The plain
  ; form is the control: it proves the share is reachable and writable here,
  ; so the long form's reading is about the form and not about access.
  StrCpy $0 "\\localhost\${PROBE_DRIVE}$$${PROBE_UNC_TAIL_PLAIN}"
  StrCpy $1 "\\?\UNC\localhost\${PROBE_DRIVE}$$${PROBE_UNC_TAIL_LONG}"
  !insertmacro iaProbeFind $0
  FileWrite $R9 "unc_plain_findfirst=$7$\r$\n"
  !insertmacro iaProbeFind $1
  FileWrite $R9 "unc_long_findfirst=$7$\r$\n"
  !insertmacro iaProbeRmdirFlag plain
  StrCpy $0 "$1"
  !insertmacro iaProbeRmdirFlag long
!endif
!ifdef PROBE_MODE_MIMIC
  ; --- 3: the scan's own load, one depth per invocation -----------------
  StrCpy $iaProbeDepth 0
  Call iaProbeDescend
  FileWrite $R9 "mimic_target=${PROBE_CALL_MAX} mimic_completed=$iaProbeDepth$\r$\n"
!endif
  FileClose $R9
SectionEnd
