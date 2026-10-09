; #905 probe 6 (runtime): is '#' mid-token a comment? StrCpy $0 foo#bar then store the value.
Name "probe"
OutFile "hash-runtime.exe"
RequestExecutionLevel user
SilentInstall silent
Section
  StrCpy $0 foo#bar
  WriteRegStr HKCU "Software\p905" "hash_val" $0
  StrCpy $1 "x"#glued comment
  WriteRegStr HKCU "Software\p905" "glued_val" $1
SectionEnd
