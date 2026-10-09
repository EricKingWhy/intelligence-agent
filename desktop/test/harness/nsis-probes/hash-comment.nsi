; #905 probe 4: does '#' start a comment when glued to a string ("x"#glued) or inside a token (foo#bar)?
; If foo#bar were cut at '#', WriteRegStr would be missing its 4th parameter and fail to compile.
Name "probe"
OutFile "hash-comment.exe"
Section
  WriteRegStr HKCU "Software\p905" "v" foo#bar
  DetailPrint "x"#glued
SectionEnd
