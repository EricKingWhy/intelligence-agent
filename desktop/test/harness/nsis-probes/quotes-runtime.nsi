; #905 probe 5 (runtime): do single-quote / backtick delete arguments really expand $vars and delete?
; Run silently, then read HKCU\Software\p905: single_deleted / backtick_deleted / sq_expand / bt_expand.
Name "probe"
OutFile "quotes-runtime.exe"
RequestExecutionLevel user
SilentInstall silent
!include LogicLib.nsh
Var /GLOBAL iaBackupDirectory
Var /GLOBAL dir
Section
  StrCpy $iaBackupDirectory "$TEMP\p905-del"
  ; --- single-quoted long-path delete ---
  CreateDirectory "$iaBackupDirectory"
  FileOpen $9 "$iaBackupDirectory\m.txt" w
  FileWrite $9 "x"
  FileClose $9
  RMDir /r '\\?\$iaBackupDirectory'
  ${If} ${FileExists} "$iaBackupDirectory\*.*"
    WriteRegStr HKCU "Software\p905" "single_deleted" "no"
  ${Else}
    WriteRegStr HKCU "Software\p905" "single_deleted" "yes"
  ${EndIf}
  ; --- backtick long-path delete ---
  CreateDirectory "$iaBackupDirectory"
  FileOpen $9 "$iaBackupDirectory\m.txt" w
  FileWrite $9 "x"
  FileClose $9
  RMDir /r `\\?\$iaBackupDirectory`
  ${If} ${FileExists} "$iaBackupDirectory\*.*"
    WriteRegStr HKCU "Software\p905" "backtick_deleted" "no"
  ${Else}
    WriteRegStr HKCU "Software\p905" "backtick_deleted" "yes"
  ${EndIf}
  ; --- variable expansion inside single quotes / backticks ---
  StrCpy $dir "VALUE1"
  StrCpy $0 'pre$dirpost'
  WriteRegStr HKCU "Software\p905" "sq_expand" $0
  StrCpy $1 `pre$dirpost`
  WriteRegStr HKCU "Software\p905" "bt_expand" $1
SectionEnd
