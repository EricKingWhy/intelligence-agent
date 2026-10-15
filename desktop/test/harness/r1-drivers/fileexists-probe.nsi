; Compile the production iaProbePath function into this focused long-path probe.
; The runner replaces the marker from installer-directories.nsh at runtime.
Unicode true
RequestExecutionLevel user
Name "IA long-path file exists probe"
OutFile "fileexists-probe.exe"
SilentInstall silent
ShowInstDetails hide

!ifndef PROBE_EXISTING
  !error "PROBE_EXISTING is required"
!endif
!ifndef PROBE_MISSING
  !error "PROBE_MISSING is required"
!endif
!ifndef PROBE_SHORT
  !error "PROBE_SHORT is required"
!endif
!ifndef PROBE_RESULT
  !error "PROBE_RESULT is required"
!endif

Var iaPlainPath
Var iaDeleteTarget
Var iaProbeFound
Function iaBuildLongPath
@@PRODUCTION_IA_BUILD_LONG_PATH_BODY@@
FunctionEnd
@@PRODUCTION_IA_PROBE_PATH@@

Section
  StrCpy $iaPlainPath "${PROBE_EXISTING}"
  Call iaProbePath
  StrCpy $2 "$iaProbeFound"

  StrCpy $iaPlainPath "${PROBE_MISSING}"
  Call iaProbePath
  StrCpy $3 "$iaProbeFound"

  StrCpy $iaPlainPath "${PROBE_SHORT}"
  Call iaProbePath
  StrCpy $4 "$iaProbeFound"

  FileOpen $9 "${PROBE_RESULT}" w
  FileWrite $9 "existing_long=$2 missing_long=$3 existing_short=$4$\r$\n"
  FileClose $9
SectionEnd
