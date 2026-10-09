; #905 probe 7 (runtime): do ';' inside single-quoted/backtick strings stay literal? Can ' be escaped there?
Name "probe"
OutFile "quotes-semi.exe"
RequestExecutionLevel user
SilentInstall silent
Section
  StrCpy $0 'a;b'
  WriteRegStr HKCU "Software\p905" "sq_semi" $0
  StrCpy $1 `c;d`
  WriteRegStr HKCU "Software\p905" "bt_semi" $1
  StrCpy $2 'x$\'y'
  WriteRegStr HKCU "Software\p905" "sq_escaped_quote" $2
SectionEnd
