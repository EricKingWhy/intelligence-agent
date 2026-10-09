; #904 shared cleanup helpers: the backup/leftover name shape, the reparse-point
; refusal, the long-path form for drive and UNC paths, and the prepare-delete
; wrapper the installer (stage / rollback / promote) and the uninstaller
; (leftover sweep) all go through. Each function here is called from BOTH
; builds: installer-directories.nsh -- which holds the installer-only existence
; probe iaProbePath -- is under the BUILD_UNINSTALLER guard, and an unreferenced
; function is a fatal warning (6010) in the uninstaller pass.
;
; Build-time guarded by scripts/build-windows-installer.mjs. The primitives are
; measured on NSIS 3.0.4.1 (desktop/test/harness/nsis-probes/, see
; desktop/test/harness/README.md):
;   - registers ($R*) and user variables ($0-$9) are NOT preserved across Call,
;     so every helper keeps state in its own Vars and the recursive scan saves
;     the find handle, the name and the directory on the NSIS stack around the
;     recursive call;
;   - FindFirst returns the leaf name, not a path, and returns an empty handle
;     for a directory it cannot enumerate — an existing empty directory still
;     enumerates "." and ".." with a real handle — so an empty handle is the
;     fail-closed signal;
;   - kernel32::GetFileAttributes is 16 for a directory, 1040 for a junction
;     (DIRECTORY|REPARSE_POINT) and -1 for a path that cannot be read; "-1 AND
;     0x400" is non-zero, so the 0x400 mask reports "reparse present" for
;     unreadable paths too, which is the fail-closed direction. Measured on a
;     real junction: RMDir /r DID delete through it (target content gone, error
;     flag false), which is why nothing recursive runs before this scan.

; The uninstaller build compiles customUnInstall into an "un." section, where
; NSIS refuses to Call anything else -- 'Call must be used with function names
; starting with "un." in the uninstall section' -- and the rule holds inside
; functions of that name space too (both measured: the sweep first shipped with
; plain calls and the uninstaller pass aborted). Every helper therefore has to
; exist twice, once as "iaXxx" for the installer and once as "un.iaXxx" for the
; uninstaller. Each body is written once as a macro below and instantiated in
; the name space of the build, with the calls between the helpers passed in as
; macro arguments -- a prefix define does not work here, because the NSIS
; preprocessor replaces `${name}` only, never a bare token (measured), and the
; call lines are plain instructions. Only one name space is instantiated per
; build, so the labels and the recursive calls stay unique.

Var iaShapeCandidate
Var iaShapeBase
Var iaShapeOk
Var iaPlainPath
Var iaDeleteCandidate
Var iaDeleteBase
Var iaDeleteShapeCheck
Var iaDeleteTarget
Var iaDeleteStatus

Var iaScanDir
Var iaScanDepth
Var iaScanReparse

; $iaShapeCandidate must be exactly "$iaShapeBase.old-$0", where $0 is the GUID
; iaStageApplication gets from CoCreateGuid through System::Call's "g" type --
; and that type formats the GUID WITH BRACES ("{8-4-4-4-12}"). The braces are
; the product's own format, unchanged since the first installer that wrote a
; backup, so a name without them is not this installer's; the length is
; base + 43 (".old-", the opening brace, 36 characters, the closing brace) and
; the guid sits in the 36-character window after ".old-{". The first version of
; this check expected "$base.old-<8-4-4-4-12 hex>" and therefore refused the
; installer's own backup name -- measured on a real update (R1 driver, case
; junction: rc 2 and a kept backup named "…\IA Installer Test 5c8433ad.old-
; {3D3D3D70-B680-4B81-9BB5-5FEF39727EA0}"); the readings that pin the format
; live in desktop/test/harness/nsis-probes/shape-probe/.
; Sets $iaShapeOk to "1" or "0". Plain instructions only: an early Return out of
; a LogicLib block leaves the caller's LogicLib state unbalanced, and this runs
; from branches of the installer protocol.
!macro iaCheckBackupShapeBody
  StrCpy $iaShapeOk "0"
  StrLen $R1 "$iaShapeBase"
  StrCpy $R2 "$iaShapeCandidate" $R1
  StrCmp $R2 "$iaShapeBase" 0 iaShapeDone
  StrLen $R4 "$iaShapeCandidate"
  IntOp $R3 $R1 + 43
  StrCmp $R4 $R3 0 iaShapeDone
  StrCpy $R2 "$iaShapeCandidate" 6 $R1
  StrCmp $R2 ".old-{" 0 iaShapeDone
  IntOp $R3 $R1 + 42
  StrCpy $R2 "$iaShapeCandidate" 1 $R3
  StrCmp $R2 "}" 0 iaShapeDone
  IntOp $R3 $R1 + 6
  StrCpy $R2 "$iaShapeCandidate" 36 $R3
  StrCpy $R4 0
iaShapeChar:
  StrCpy $R5 "$R2" 1 $R4
  StrCmp $R4 8 iaShapeDash
  StrCmp $R4 13 iaShapeDash
  StrCmp $R4 18 iaShapeDash
  StrCmp $R4 23 iaShapeDash
  StrCpy $R6 0
iaShapeHex:
  StrCpy $R7 "0123456789abcdefABCDEF" 1 $R6
  StrCmp $R5 $R7 iaShapeNext
  IntOp $R6 $R6 + 1
  IntCmp $R6 22 iaShapeDone iaShapeHex iaShapeDone
iaShapeDash:
  StrCmp $R5 "-" 0 iaShapeDone
iaShapeNext:
  IntOp $R4 $R4 + 1
  IntCmp $R4 36 iaShapePass iaShapeChar iaShapePass
iaShapePass:
  StrCpy $iaShapeOk "1"
iaShapeDone:
  Return
!macroend

; $iaPlainPath -> $iaDeleteTarget: the long-path form used to delete and to
; scan. The bare "\\?\" prefix cannot express a UNC path (Win32 needs the
; "\\?\UNC\" spelling), so UNC gets its own form and drive paths keep theirs.
; Clobbers $0.
!macro iaBuildLongPathBody
  StrCpy $0 "$iaPlainPath" 2
  StrCmp $0 "\\" iaBuildLongPathUnc
  StrCpy $iaDeleteTarget "\\?\$iaPlainPath"
  Return
iaBuildLongPathUnc:
  StrCpy $0 "$iaPlainPath" "" 2
  StrCpy $iaDeleteTarget "\\?\UNC\$0"
!macroend

; Recursive scan of $iaScanDir (already in long-path form). Sets
; $iaScanReparse to "reparse", "unreadable" or "too-deep" when the directory
; itself or anything under it is a reparse point, or when the tree cannot be
; read — fail-closed either way. "0" means clean.
; The depth cap is a stack valve, not a cycle guard: a cycle needs a reparse
; point, which is refused before any descent. It is set high so that a deep,
; legitimate tree (an npm-style node_modules) is not refused.
; FN_SELF is the name of the function this body is instantiated as, so the
; recursive call lands in the same name space.
!macro iaScanReparsePointsBody FN_SELF
  IntOp $iaScanDepth $iaScanDepth + 1
  IntCmp $iaScanDepth 512 iaScanTooDeep iaScanEnter iaScanTooDeep
iaScanEnter:
  StrCpy $0 "$iaScanDir"
  System::Call 'kernel32::GetFileAttributes(t r0)i .r1'
  IntCmp $1 -1 iaScanUnreadable iaScanAttrs iaScanAttrs
iaScanAttrs:
  IntOp $2 $1 & 0x400
  IntCmp $2 0 iaScanFind iaScanReparsePoint iaScanReparsePoint
iaScanFind:
  FindFirst $3 $4 "$iaScanDir\*.*"
  StrCmp $3 "" iaScanUnreadable
iaScanLoop:
  StrCmp $4 "" iaScanClose
  StrCmp $4 "." iaScanNext
  StrCmp $4 ".." iaScanNext
  StrCpy $0 "$iaScanDir\$4"
  System::Call 'kernel32::GetFileAttributes(t r0)i .r1'
  IntCmp $1 -1 iaScanUnreadableClose iaScanMask iaScanMask
iaScanMask:
  IntOp $2 $1 & 0x400
  IntCmp $2 0 iaScanMaybeDir iaScanReparseClose iaScanReparseClose
iaScanMaybeDir:
  IntOp $2 $1 & 0x10
  IntCmp $2 0 iaScanNext iaScanDescend iaScanNext
iaScanDescend:
  Push $3
  Push $4
  Push $iaScanDir
  Push $iaScanDepth
  StrCpy $iaScanDir "$iaScanDir\$4"
  Call ${FN_SELF}
  Pop $iaScanDepth
  Pop $iaScanDir
  Pop $4
  Pop $3
  StrCmp $iaScanReparse "0" iaScanNext iaScanHitClose
iaScanNext:
  FindNext $3 $4
  Goto iaScanLoop
iaScanClose:
  FindClose $3
  Return
iaScanHitClose:
  FindClose $3
  Return
iaScanTooDeep:
  StrCpy $iaScanReparse "too-deep"
  Return
iaScanUnreadable:
  StrCpy $iaScanReparse "unreadable"
  Return
iaScanUnreadableClose:
  StrCpy $iaScanReparse "unreadable"
  Goto iaScanHitClose
iaScanReparsePoint:
  StrCpy $iaScanReparse "reparse"
  Return
iaScanReparseClose:
  StrCpy $iaScanReparse "reparse"
  Goto iaScanHitClose
!macroend

; Prepares the recursive delete of $iaDeleteCandidate. With $iaDeleteShapeCheck
; "1" the name must have the backup shape first; then the long-path form is
; built and the tree is scanned. Sets $iaDeleteStatus to
;   ok | refused-shape | refused-reparse | refused-unreadable | refused-too-deep
; Nothing here deletes, and a refusal clears $iaDeleteTarget so a caller that
; ignores the status cannot delete anything. Saves the user variables the
; helpers use, so callers may keep their own state in $0-$4.
; FN_CHECK / FN_LONG / FN_SCAN are the names of the three helpers in the name
; space this body is instantiated in.
!macro iaPrepareDeleteBody FN_CHECK FN_LONG FN_SCAN
  Push $0
  Push $1
  Push $2
  Push $3
  Push $4
  StrCpy $iaDeleteStatus "ok"
  StrCpy $iaDeleteTarget ""
  StrCmp $iaDeleteShapeCheck "1" 0 iaPrepareSkipShape
  StrCpy $iaShapeCandidate "$iaDeleteCandidate"
  StrCpy $iaShapeBase "$iaDeleteBase"
  Call ${FN_CHECK}
  StrCmp $iaShapeOk "1" 0 iaPrepareRefuseShape
iaPrepareSkipShape:
  StrCpy $iaPlainPath "$iaDeleteCandidate"
  Call ${FN_LONG}
  StrCpy $iaScanDir "$iaDeleteTarget"
  StrCpy $iaScanDepth 0
  StrCpy $iaScanReparse "0"
  Call ${FN_SCAN}
  StrCmp $iaScanReparse "0" 0 iaPrepareRefuseScan
  Goto iaPrepareDone
iaPrepareRefuseShape:
  StrCpy $iaDeleteStatus "refused-shape"
  StrCpy $iaDeleteTarget ""
  Goto iaPrepareDone
iaPrepareRefuseScan:
  StrCpy $iaDeleteStatus "refused-$iaScanReparse"
  StrCpy $iaDeleteTarget ""
iaPrepareDone:
  Pop $4
  Pop $3
  Pop $2
  Pop $1
  Pop $0
!macroend

; One instantiation per name space. The plain names are the ones the installer
; protocol calls; the un. names are the ones the uninstaller's sweep can call.
!ifdef BUILD_UNINSTALLER
  Function un.iaCheckBackupShape
    !insertmacro iaCheckBackupShapeBody
  FunctionEnd
  Function un.iaBuildLongPath
    !insertmacro iaBuildLongPathBody
  FunctionEnd
  Function un.iaScanReparsePoints
    !insertmacro iaScanReparsePointsBody un.iaScanReparsePoints
  FunctionEnd
  Function un.iaPrepareDelete
    !insertmacro iaPrepareDeleteBody un.iaCheckBackupShape un.iaBuildLongPath un.iaScanReparsePoints
  FunctionEnd
!else
  Function iaCheckBackupShape
    !insertmacro iaCheckBackupShapeBody
  FunctionEnd
  Function iaBuildLongPath
    !insertmacro iaBuildLongPathBody
  FunctionEnd
  Function iaScanReparsePoints
    !insertmacro iaScanReparsePointsBody iaScanReparsePoints
  FunctionEnd
  Function iaPrepareDelete
    !insertmacro iaPrepareDeleteBody iaCheckBackupShape iaBuildLongPath iaScanReparsePoints
  FunctionEnd
!endif
