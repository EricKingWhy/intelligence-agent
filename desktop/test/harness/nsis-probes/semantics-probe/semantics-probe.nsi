; #904: NSIS 3.0.4.1 semantics the reparse-point refusal and the leftover sweep
; depend on. Driven by run-semantics-probe.ps1, which builds the directories and
; passes:
;   /DPROBE_EMPTY=<empty dir>            FindFirst/FindNext on nothing
;   /DPROBE_CONTENT=<dir with a file>    FindFirst/FindNext with content
;   /DPROBE_JUNCTION=<junction to empty> reparse detection
;   /DPROBE_MISSING=<path that does not exist>
;   /DPROBE_RESULT=<reading file>
;
; Readings are written to the result file as they are taken, so a later
; measurement cannot clobber an earlier one (the first version of this probe
; overwrote the register it was about to report).
;
; Measurements (the readings the guard rules rest on):
;   1  are $0/$1 and $R0/$R1 preserved across a Call?   -> recursion safety
;      (also met in real code: section 7's first version kept the path pieces
;      in $1/$2 across the helper call and measured a garbage path)
;   2  FindFirst on an empty dir / on a missing path: handle/name
;   3  FindNext past the end: name empty?
;   4  kernel32::GetFileAttributesW raw values (plain dir / junction / missing)
;   5  the FILE_ATTRIBUTE_REPARSE_POINT (0x400) mask as the detector
;   6  FindFirst through a "\\?\"-prefixed path
;   7  the three spellings of the same real directory -- plain UNC, "\\?\UNC\",
;      and "\\?\" in front of a UNC path -- read through a variable exactly like
;      iaProbePath passes them, plus "\\?\<drive>:" as the prefix control: the
;      form iaBuildLongPath builds for a UNC target has to resolve, the bare
;      "\\?\" form must not
;   8  a recursive delete of an EMPTY path: the refusal paths clear
;      $iaDeleteTarget on purpose, and a caller that ignores the status must
;      then delete nothing
;   9  a recursive delete of a path that is not there leaves the error flag
;      CLEAR (plain and prefixed) -- so only a real delete failure reaches the
;      promote site's "failed" branch, and a missing backup directory has to be
;      refused by the prepare check instead
Unicode true
RequestExecutionLevel user
Name "IA nsis semantics probe"
OutFile "semantics-probe.exe"
SilentInstall silent
ShowInstDetails hide

!include "LogicLib.nsh"

!ifndef PROBE_EMPTY
  !error "PROBE_EMPTY is required"
!endif
!ifndef PROBE_CONTENT
  !error "PROBE_CONTENT is required"
!endif
!ifndef PROBE_JUNCTION
  !error "PROBE_JUNCTION is required"
!endif
!ifndef PROBE_MISSING
  !error "PROBE_MISSING is required"
!endif
!ifndef PROBE_RESULT
  !error "PROBE_RESULT is required"
!endif

; Clobbers $0/$1 on purpose; the caller reports what it sees afterwards.
Function iaProbeInnerVars
  StrCpy $0 "inner"
  StrCpy $1 "inner"
FunctionEnd

; Clobbers $R0/$R1 on purpose.
Function iaProbeInnerRegs
  StrCpy $R0 "innerR"
  StrCpy $R1 "innerR"
FunctionEnd

; $0 = path (in); $R0 = raw value, $R1 = "yes"/"no" for the 0x400 mask.
Function iaProbeAttributes
  System::Call 'kernel32::GetFileAttributes(t r0)i .r1'
  IntOp $R0 $1 + 0
  IntOp $2 $1 & 0x400
  StrCpy $R1 "no"
  ${If} $2 != 0
    StrCpy $R1 "yes"
  ${EndIf}
FunctionEnd

Section
  FileOpen $R9 "${PROBE_RESULT}" w

  ; --- 1: register preservation across Call -------------------------------
  StrCpy $0 "outer0"
  StrCpy $1 "outer1"
  Call iaProbeInnerVars
  StrCpy $R5 "clobbered"
  ${If} $0 == "outer0"
  ${AndIf} $1 == "outer1"
    StrCpy $R5 "preserved"
  ${EndIf}
  FileWrite $R9 "vars_across_call=[$R5]$\r$\n"

  StrCpy $R0 "outerR0"
  StrCpy $R1 "outerR1"
  Call iaProbeInnerRegs
  StrCpy $R5 "clobbered"
  ${If} $R0 == "outerR0"
  ${AndIf} $R1 == "outerR1"
    StrCpy $R5 "preserved"
  ${EndIf}
  FileWrite $R9 "regs_across_call=[$R5]$\r$\n"

  ; --- 2: FindFirst on an empty directory and on a missing path -----------
  FindFirst $3 $4 "${PROBE_EMPTY}\*.*"
  StrCpy $5 "handle=[$3] name=[$4]"
  FindNext $3 $4
  StrCpy $5 "$5 after_next=[$4]"
  FindClose $3
  FileWrite $R9 "empty_findfirst=$5$\r$\n"

  FindFirst $3 $4 "${PROBE_MISSING}\*.*"
  StrCpy $5 "handle=[$3] name=[$4]"
  FindClose $3
  FileWrite $R9 "missing_findfirst=$5$\r$\n"

  ; --- 3: FindNext in a directory with one file ---------------------------
  FindFirst $3 $4 "${PROBE_CONTENT}\*.*"
  StrCpy $6 "first=[$4]"
  ${DoWhile} $4 != ""
    FindNext $3 $4
  ${Loop}
  FindClose $3
  FileWrite $R9 "content_find_loop=$6$\r$\n"

  ; --- 4/5: raw attribute values and the reparse mask ---------------------
  StrCpy $0 "${PROBE_EMPTY}"
  Call iaProbeAttributes
  StrCpy $7 "raw=$R0 reparse=$R1"
  StrCpy $0 "${PROBE_JUNCTION}"
  Call iaProbeAttributes
  StrCpy $8 "raw=$R0 reparse=$R1"
  StrCpy $0 "${PROBE_MISSING}"
  Call iaProbeAttributes
  StrCpy $9 "raw=$R0 reparse=$R1"
  FileWrite $R9 "attrs_plain=[$7]$\r$\n"
  FileWrite $R9 "attrs_junction=[$8]$\r$\n"
  FileWrite $R9 "attrs_missing=[$9]$\r$\n"

  ; --- 6: FindFirst through a prefixed path ------------------------------
  FindFirst $3 $4 "\\?\${PROBE_CONTENT}\*.*"
  StrCpy $R2 "handle=[$3] first=[$4]"
  FindClose $3
  FileWrite $R9 "prefixed_findfirst=$R2$\r$\n"

  ; --- 7: the UNC forms of the long-path prefix --------------------------
  ; The same REAL directory (read through its admin-share form) through all
  ; three spellings, plus the drive-letter form as the control that the prefix
  ; itself works, so the reading is an attribute value rather than an error
  ; class: the "\\?\UNC\" form has to resolve like the plain UNC path, while
  ; the bare "\\?\" form in front of a UNC path must not. The paths go through
  ; a variable, exactly like iaProbePath passes them to GetFileAttributes.
  ; Where the user may not open the admin share, the plain and UNC forms read
  ; -1 as well and the reading is access-limited rather than a verdict on the
  ; form (the probe table records which way it went).
  StrCpy $1 $WINDIR 1
  StrCpy $2 $WINDIR "" 2
  StrCpy $0 "\\localhost\$1$$$2"
  StrCpy $R7 "unc_plain_path=[$0]"
  Call iaProbeAttributes
  StrCpy $R3 "unc_plain_attrs=[$R0]"
  ; iaProbeAttributes holds the returned value and its mask in $1/$2, so the
  ; path pieces are rebuilt for every form -- measurement 1, met in real code:
  ; the first run of this probe asked about "\\?\UNC\localhost\16$0" (the
  ; residue of the previous call) and read -1 for a form Win32 accepts.
  StrCpy $1 $WINDIR 1
  StrCpy $2 $WINDIR "" 2
  StrCpy $0 "\\?\UNC\localhost\$1$$$2"
  StrCpy $R8 "unc_long_path=[$0]"
  Call iaProbeAttributes
  StrCpy $R4 "unc_long_attrs=[$R0]"
  StrCpy $1 $WINDIR 1
  StrCpy $2 $WINDIR "" 2
  StrCpy $0 "\\?\localhost\$1$$$2"
  Call iaProbeAttributes
  StrCpy $R5 "unc_bare_attrs=[$R0]"
  StrCpy $0 "\\?\$WINDIR"
  Call iaProbeAttributes
  StrCpy $R6 "drive_long_attrs=[$R0]"
  FileWrite $R9 "$R7$\r$\n"
  FileWrite $R9 "$R8$\r$\n"
  FileWrite $R9 "$R3$\r$\n"
  FileWrite $R9 "$R4$\r$\n"
  FileWrite $R9 "$R5$\r$\n"
  FileWrite $R9 "$R6$\r$\n"

  ; --- 8: a recursive delete of an empty path ----------------------------
  ; The probe's own content file survives: nothing may be removed.
  StrCpy $0 ""
  ClearErrors
  RMDir /r "$0"
  IfErrors 0 iaEmptyRmdirNoFlag
    StrCpy $R5 "empty_rmdir_flag=set"
    Goto iaEmptyRmdirFlagDone
  iaEmptyRmdirNoFlag:
    StrCpy $R5 "empty_rmdir_flag=clear"
  iaEmptyRmdirFlagDone:
  ${If} ${FileExists} "${PROBE_CONTENT}\one.txt"
    StrCpy $R5 "$R5 keep_after=yes"
  ${Else}
    StrCpy $R5 "$R5 keep_after=no"
  ${EndIf}
  FileWrite $R9 "$R5$\r$\n"

  ; --- 9: a recursive delete of a path that is not there -----------------
  StrCpy $0 "${PROBE_MISSING}"
  ClearErrors
  RMDir /r "$0"
  IfErrors 0 iaMissingPlainNoFlag
    StrCpy $R6 "missing_rmdir_flag=set"
    Goto iaMissingPlainDone
  iaMissingPlainNoFlag:
    StrCpy $R6 "missing_rmdir_flag=clear"
  iaMissingPlainDone:
  StrCpy $0 "\\?\${PROBE_MISSING}"
  ClearErrors
  RMDir /r "$0"
  IfErrors 0 iaMissingLongNoFlag
    StrCpy $R7 "missing_long_rmdir_flag=set"
    Goto iaMissingLongDone
  iaMissingLongNoFlag:
    StrCpy $R7 "missing_long_rmdir_flag=clear"
  iaMissingLongDone:
  FileWrite $R9 "$R6$\r$\n"
  FileWrite $R9 "$R7$\r$\n"

  FileClose $R9
SectionEnd
