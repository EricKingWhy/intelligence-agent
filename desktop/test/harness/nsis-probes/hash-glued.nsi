; #905 probe 8 (runtime): is '#' glued after a single-quoted / backtick string also a comment?
; If not a comment, StrCpy would receive 2+ params ("x" and #glued) -> compile error. Expect compile OK.
Name "probe"
OutFile "hash-glued.exe"
RequestExecutionLevel user
SilentInstall silent
Section
  StrCpy $0 'x'#glued1
  WriteRegStr HKCU "Software\p905" "sq_glued" $0
  StrCpy $1 `y`#glued2
  WriteRegStr HKCU "Software\p905" "bt_glued" $1
SectionEnd
