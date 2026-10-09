; #904: the launch forms of an NSIS 3.0.4.1 uninstaller and which of them can
; report the section's exit code. The leftover sweep's contract is "kept
; leftovers => non-zero exit code" (SetErrorLevel 2), and the only form that
; carries that code to the caller is an in-place launch with "_?=<install dir>"
; as the LAST, UNQUOTED argument -- the form the stock updater's own call uses
; (installUtil.nsh: ExecWait '"$uninstallerFileNameTemp" /S ... _?=$installationDir' $R0).
;
; Measured readings (2026-10-10, run twice; recorded in ../README.md):
;   plain             /S                       rc 0  section runs from %TEMP%\~nsu.tmp
;   self+inplace-raw  /S _?=<dir>              rc 2  section runs in place
;                                                     (marker EXEDIR = the dir)
;   self+inplace-q    /S _?="<dir>"            rc 2  section NEVER runs: no marker,
;                                                     nothing deleted (a quoted
;                                                     value is not a usable
;                                                     invocation -- a reading
;                                                     that must not be taken for
;                                                     the sweep's report)
;   copy+inplace-raw  %TEMP% copy, _?=<dir>    rc 2  section runs, whole dir removed
;
; Markers: Function un.onInit and the uninstall section append to files in
; ${PROBE_MARKERS}, so a run that leaves no marker provably never executed the
; script -- independent of what it deleted. Instructions (FileOpen) are illegal
; outside a section or function, so there is no top-level marker to add.
; Two builds from this file:
;   /DPROBE_SPACED  -> "${PROBE_BASE}\dir with space"
;   (default)       -> "${PROBE_BASE}\dir-no-space"
; Driven by run-launch-form-probe.ps1, which passes:
;   /DPROBE_BASE=<probe root>        parent of both install dirs
;   /DPROBE_MARKERS=<marker dir>     outside every install dir
Unicode true
RequestExecutionLevel user
Name "IA uninstaller launch-form probe"
SilentInstall silent
ShowInstDetails hide

!ifndef PROBE_BASE
  !error "PROBE_BASE is required"
!endif
!ifndef PROBE_MARKERS
  !error "PROBE_MARKERS is required"
!endif

!ifdef PROBE_SPACED
  InstallDir "${PROBE_BASE}\dir with space"
  OutFile "launch-form-probe-spaced.exe"
!else
  InstallDir "${PROBE_BASE}\dir-no-space"
  OutFile "launch-form-probe-nospace.exe"
!endif

; The installer's own init marker: the runner clears the marker files after the
; setup run, so only the measured uninstaller launches leave lines here.
Function .onInit
  FileOpen $8 "${PROBE_MARKERS}\init.txt" a
  FileWrite $8 "onInit: EXEDIR=[$EXEDIR]$\r$\n"
  FileClose $8
FunctionEnd

Function un.onInit
  FileOpen $8 "${PROBE_MARKERS}\init.txt" a
  FileWrite $8 "un.onInit: EXEDIR=[$EXEDIR]$\r$\n"
  FileClose $8
FunctionEnd

Section
  SetOutPath $INSTDIR
  FileOpen $0 "$INSTDIR\keep.txt" w
  FileWrite $0 "keep"
  FileClose $0
  WriteUninstaller "$INSTDIR\uninstall-probe.exe"
SectionEnd

Section "Uninstall"
  FileOpen $9 "${PROBE_MARKERS}\section.txt" a
  FileWrite $9 "section: INSTDIR=[$INSTDIR] EXEDIR=[$EXEDIR]$\r$\n"
  FileClose $9
  SetErrorLevel 2
  SetOutPath $TEMP
  RMDir /r "$INSTDIR"
SectionEnd
