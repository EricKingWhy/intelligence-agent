; #905 probe 1: variable-name case sensitivity (does $IABackupDirectory resolve to $iaBackupDirectory?)
Name "probe"
OutFile "var-case.exe"
Section
  Var /GLOBAL iaBackupDirectory
  StrCpy $iaBackupDirectory "abc"
  StrCmp $IABackupDirectory "abc" 0 +2
    MessageBox MB_OK "casings differ at runtime"
SectionEnd
