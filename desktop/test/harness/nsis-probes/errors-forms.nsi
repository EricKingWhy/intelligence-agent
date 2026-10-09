; #905 probe 2: ${Errors} inside a string literal vs the canonical read form
Name "probe"
OutFile "errors-forms.exe"
!include LogicLib.nsh
Section
  ClearErrors
  DetailPrint "flag ${Errors} seen"
  ${If} ${Errors}
    DetailPrint "read branch"
  ${EndIf}
SectionEnd
