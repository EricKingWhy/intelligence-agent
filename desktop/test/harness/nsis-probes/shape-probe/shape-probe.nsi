; #904 backup-shape probe (self-driving).
;
; Compiles the SHIPPED shape predicate -- installer-cleanup.nsh is included from
; its real path, passed in as PROBE_CLEANUP -- and runs it against names built
; the way the installer builds them, so the reading is about the product and
; not about a copy of it. Every case carries its expected verdict, one line per
; case goes to PROBE_RESULT, and the exit code is the number of failed cases:
; a predicate that stops matching the product turns this red instead of green.
;
; Why this probe exists: the first version of the predicate expected
; "$base.old-<8-4-4-4-12 hex>" and therefore refused the installer's OWN
; backup name, which carries the GUID in the braces of System::Call's "g" type.
; The unit fixtures did not notice -- they were written to the same assumption.
; The R1 driver's junction case did, on a real update: rc 2, no leftover
; record, and the backup "...\IA Installer Test 5c8433ad.old-{3D3D3D70-...}"
; left in place (measured 2026-10-10). The predicate now accepts exactly the
; braced form and nothing else; these cases pin that, one character at a time.
;
; Driven by run-shape-probe.ps1, which compiles this file into a scratch
; directory and passes:
;   /DPROBE_CLEANUP=<path to desktop/installer/installer-cleanup.nsh>
;   /DPROBE_RESULT=<reading file>
Unicode true
RequestExecutionLevel user
Name "IA backup shape probe"
OutFile "shape-probe.exe"
SilentInstall silent
ShowInstDetails hide

!include "LogicLib.nsh"

!ifndef PROBE_CLEANUP
  !error "PROBE_CLEANUP is required"
!endif
!ifndef PROBE_RESULT
  !error "PROBE_RESULT is required"
!endif

!include "${PROBE_CLEANUP}"

Var probeFailures
Var probeLabel

; $8 is the expected verdict, $9 the result file handle. Cases whose Ok and
; expectation differ are counted, so the exit code carries the verdict.
Function ReportCase
  StrCmp $iaShapeOk $8 0 probeCaseBad
  FileWrite $9 "PASS $probeLabel ok=$iaShapeOk expected=$8$\r$\n"
  Return
probeCaseBad:
  IntOp $probeFailures $probeFailures + 1
  FileWrite $9 "FAIL $probeLabel ok=$iaShapeOk expected=$8$\r$\n"
FunctionEnd

Section
  FileOpen $9 "${PROBE_RESULT}" w
  StrCpy $probeFailures 0

  StrCpy $iaShapeBase "C:\Users\probe\AppData\Local\Programs\Intelligence Agent"

  ; 1. The name iaStageApplication actually writes: CoCreateGuid, then
  ;    StrCpy $iaBackupDirectory "$INSTDIR.old-$0". Derived here rather than
  ;    typed in, so the case tracks the product's own atom of format.
  System::Call 'ole32::CoCreateGuid(g .r0) i .r1'
  StrCpy $iaShapeCandidate "$iaShapeBase.old-$0"
  StrCpy $probeLabel "cocreated"
  StrCpy $8 "1"
  Call iaCheckBackupShape
  Call ReportCase

  ; 2. The name a real update left on disk (R1 driver, 2026-10-10).
  StrCpy $iaShapeCandidate "$iaShapeBase.old-{3D3D3D70-B680-4B81-9BB5-5FEF39727EA0}"
  StrCpy $probeLabel "literal-uppercase"
  StrCpy $8 "1"
  Call iaCheckBackupShape
  Call ReportCase

  ; 3. The hex set accepts either case.
  StrCpy $iaShapeCandidate "$iaShapeBase.old-{3d3d3d70-b680-4b81-9bb5-5fef39727ea0}"
  StrCpy $probeLabel "literal-lowercase"
  StrCpy $8 "1"
  Call iaCheckBackupShape
  Call ReportCase

  ; 4. No braces -- not a name this installer writes, so refused.
  StrCpy $iaShapeCandidate "$iaShapeBase.old-3D3D3D70-B680-4B81-9BB5-5FEF39727EA0"
  StrCpy $probeLabel "unbraced"
  StrCpy $8 "0"
  Call iaCheckBackupShape
  Call ReportCase

  ; 5. Missing the closing brace (one character short).
  StrCpy $iaShapeCandidate "$iaShapeBase.old-{3D3D3D70-B680-4B81-9BB5-5FEF39727EA0"
  StrCpy $probeLabel "no-closing-brace"
  StrCpy $8 "0"
  Call iaCheckBackupShape
  Call ReportCase

  ; 6. The last character is not the brace: length and window are right.
  StrCpy $iaShapeCandidate "$iaShapeBase.old-{3D3D3D70-B680-4B81-9BB5-5FEF39727EA00"
  StrCpy $probeLabel "last-char-not-brace"
  StrCpy $8 "0"
  Call iaCheckBackupShape
  Call ReportCase

  ; 7. One character too many before the brace.
  StrCpy $iaShapeCandidate "$iaShapeBase.old-{3D3D3D70-B680-4B81-9BB5-5FEF39727EA0A}"
  StrCpy $probeLabel "one-char-too-long"
  StrCpy $8 "0"
  Call iaCheckBackupShape
  Call ReportCase

  ; 8. A non-hex character inside the braces.
  StrCpy $iaShapeCandidate "$iaShapeBase.old-{zD3D3D70-B680-4B81-9BB5-5FEF39727EA0}"
  StrCpy $probeLabel "non-hex-inside"
  StrCpy $8 "0"
  Call iaCheckBackupShape
  Call ReportCase

  ; 9. The first dash one position late (same length: the hex set would accept
  ;    the character that moved, so only the dash position can refuse it).
  StrCpy $iaShapeCandidate "$iaShapeBase.old-{3D3D3D70B-680-4B81-9BB5-5FEF39727EA0}"
  StrCpy $probeLabel "dash-moved"
  StrCpy $8 "0"
  Call iaCheckBackupShape
  Call ReportCase

  ; 10. A different base with the very same tail.
  StrCpy $iaShapeCandidate "$iaShapeBase-something.old-{3D3D3D70-B680-4B81-9BB5-5FEF39727EA0}"
  StrCpy $probeLabel "wrong-base"
  StrCpy $8 "0"
  Call iaCheckBackupShape
  Call ReportCase

  ; 11. A UNC install directory: the shape is string work, nothing here touches
  ;     the filesystem -- the long-path form is iaBuildLongPath's business.
  StrCpy $iaShapeBase "\\server\share\Programs\Intelligence Agent"
  StrCpy $iaShapeCandidate "$iaShapeBase.old-{3D3D3D70-B680-4B81-9BB5-5FEF39727EA0}"
  StrCpy $probeLabel "unc-base"
  StrCpy $8 "1"
  Call iaCheckBackupShape
  Call ReportCase

  FileWrite $9 "failures=$probeFailures$\r$\n"
  FileClose $9
  SetErrorLevel $probeFailures
SectionEnd
