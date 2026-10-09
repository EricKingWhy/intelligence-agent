; #905 probe 1 negative control: an undeclared variable name must fail to compile
Name "probe"
OutFile "unknown-var.exe"
Section
  StrCpy $iDontExistAnywhere "abc"
SectionEnd
