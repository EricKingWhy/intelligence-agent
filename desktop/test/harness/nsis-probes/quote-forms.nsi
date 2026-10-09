; #905 probe 3: can the delete argument be written with single quotes / backticks?
Name "probe"
OutFile "quote-forms.exe"
Section
  Var /GLOBAL iaBackupDirectory
  StrCpy $iaBackupDirectory "$TEMP\p905"
  RMDir /r '\\?\$iaBackupDirectory'
  RMDir /r `\\?\$iaBackupDirectory`
SectionEnd
