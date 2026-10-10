; #361 [W-16] Stock electron-builder NSIS custom include.
;
; Hooks used (see app-builder-lib/templates/nsis):
;   customHeader    - compile-time: strings, per-user manifest, directory-swap
;   customInit      - installer startup: refuse per-machine installs, then
;                     move any previous version aside (iaStageApplication)
;   customInstall   - end of the install section: verify the new files and
;                     either delete the backup or roll back (iaPromoteApplication)
;   customUnInstall - uninstall section: sweep the "<install dir>.old-{guid}"
;                     backup directories earlier updates left behind (#904);
;                     user data is NEVER deleted here, explicit cleanup is
;                     scripts/clean-user-data.mjs
;
; Design notes (from DeepSeek Harness installer.nsh, MIT, commit
; 5badb15009ae1756c3afe0ae0cef1faafc290ccc — per-user refusal pattern):
;   - per-user install only (RequestExecutionLevel user comes from the
;     electron-builder `perMachine: false` config); a per-machine install of
;     the same app id refuses to continue instead of fighting it.
;   - the running-app guard is the atomic rename itself: Windows cannot
;     rename a directory containing a running executable, so iaStageApplication
;     fails closed with a message instead of installing over a live process.
;     (The stock CHECK_APP_RUNNING PowerShell probe still runs in the section.)
;   - rollback needs no cooperation from the previous version: the backup is
;     created by the NEW installer, so updating from a pre-#361 build still
;     rolls back correctly.
; Full MIT text and provenance ledger: desktop/THIRD_PARTY_NOTICES.md.

!include "LogicLib.nsh"
; The uninstaller sweep needs ${GetParent}; FileFunc.nsh is the NSIS standard
; library that provides it (its own include guard makes the double include with
; the template's multiUser.nsh harmless).
!include "FileFunc.nsh"

; #816 (W-21 D1): `__FILEDIR__` is only usable here, at the top level of this
; file. Inside a macro body it is substituted when the macro is inserted — in
; the *inserting* file's context, i.e. the stock template directory — which is
; why the previous `!include "${__FILEDIR__}\installer-directories.nsh"` inside
; `customHeader` aborted the build with "could not find:
; …\app-builder-lib\templates\nsis\installer-directories.nsh" (measured; see
; #816). `!define` substitutes eagerly, so the two-level form below is enough
; (same shape as DeepSeek Harness installer.nsh:3-4).
;
; The include itself stays inside `customHeader` on purpose, and is
; installer-only:
;   - `multiUser.nsh` — which defines `${INSTALL_REGISTRY_KEY}`, used by the
;     stash macros — is included *after* this file's top level (app-builder-lib
;     injects the custom include into the generated script header), so a
;     top-level include would hit `warning 6000: unknown variable/constant`;
;   - the uninstaller build inserts `customHeader` too and references none of
;     these functions, so including them there fails the build with
;     `warning 6010: install function "iaPromoteApplication" not referenced`
;     (both warnings are errors under electron-builder's makensis settings).
!define IA_INSTALLER_DIR "${__FILEDIR__}"

!macro customHeader
  ManifestDPIAware true

  ; #904: the shape check, the reparse-point refusal and the long-path form are
  ; shared: the installer protocol uses them, and the uninstaller's leftover
  ; sweep does too, so this include is NOT under the BUILD_UNINSTALLER guard.
  ; Every function in it stays reachable from both builds — an unreferenced
  ; function is fatal there (warning 6010).
  !include "${IA_INSTALLER_DIR}\installer-cleanup.nsh"

  !ifndef BUILD_UNINSTALLER
    !include "${IA_INSTALLER_DIR}\installer-directories.nsh"
  !endif

  ; Bilingual UI strings. The template loads only the configured
  ; installerLanguages, so guard each language: a ${LANG_<NAME>} that is not
  ; loaded (e.g. a single-language build) makes makensis emit warning 7025,
  ; fatal under electron-builder's warnings-as-errors. #831 — full rationale
  ; and the matching build-time assertion live in
  ; scripts/build-windows-installer.mjs.
  !ifdef LANG_ENGLISH
  LangString iaPerUserOnly ${LANG_ENGLISH} "This installer is per-user only. A per-machine installation of Intelligence Agent was found; uninstall it first, then run this installer again."
  LangString iaAppRunning ${LANG_ENGLISH} "Intelligence Agent (or one of its background processes) is still running. Close it and run the installer again — the previous version was left untouched."
  LangString iaUpdateFailed ${LANG_ENGLISH} "The update failed: the new files are incomplete. The previous version has been restored."
  LangString iaRollbackFailed ${LANG_ENGLISH} "Could not restore the previous version automatically. The complete backup was kept at:"
  LangString iaStaleBackup ${LANG_ENGLISH} "The update is complete, but the previous version could not be removed completely. Delete this folder to reclaim the space:"
  LangString iaLeftoverSweep ${LANG_ENGLISH} "Earlier updates left backup folders of the previous version next to the installation directory, and they can take up a lot of disk space. Delete them now?"
  LangString iaLeftoverKept ${LANG_ENGLISH} "Some backup folders could not be removed: they are in use, contain links, were not created by this installer, or the delete itself failed — which is what happens when the installation is on a network (UNC) path, where the uninstaller cannot delete. They were left in place — delete them by hand to reclaim the space."
  !endif
  !ifdef LANG_SIMPCHINESE
  LangString iaPerUserOnly ${LANG_SIMPCHINESE} "此安装程序仅支持按用户安装。检测到 Intelligence Agent 的按计算机安装，请先卸载它，再重新运行此安装程序。"
  LangString iaAppRunning ${LANG_SIMPCHINESE} "Intelligence Agent（或其后台进程）仍在运行。请关闭后重新运行安装程序——旧版本未被改动。"
  LangString iaUpdateFailed ${LANG_SIMPCHINESE} "更新失败：新文件不完整。已恢复到旧版本。"
  LangString iaRollbackFailed ${LANG_SIMPCHINESE} "无法自动恢复旧版本。完整备份保留在："
  LangString iaStaleBackup ${LANG_SIMPCHINESE} "更新已完成，但旧版本未能完全删除。可手动删除以下文件夹以回收磁盘空间："
  LangString iaLeftoverSweep ${LANG_SIMPCHINESE} "早前的更新在安装目录旁留下了旧版本的备份文件夹，可能占用大量磁盘空间。现在删除它们吗？"
  LangString iaLeftoverKept ${LANG_SIMPCHINESE} "部分备份文件夹未能删除：它们正在使用、包含链接、不是本安装程序创建的，或者删除操作本身失败了——安装目录位于网络（UNC）路径时即如此，卸载程序无法在其上删除。已保留原样，可手动删除以回收磁盘空间。"
  !endif
!macroend

!macro customInit
  ; Refuse when a per-machine installation of the same app id exists.
  ReadRegStr $0 HKLM "${UNINSTALL_REGISTRY_KEY}" "UninstallString"
  ${If} $0 != ""
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaPerUserOnly)" /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
  ; Move any previous version aside; aborts (leaving it untouched) when the
  ; rename fails, e.g. because the app is still running.
  !insertmacro iaStageApplication
!macroend

!macro customInstall
  Call iaPromoteApplication
  ; Standard uninstall-entry metadata (mirrors DSH's customInstall).
  WriteRegStr SHELL_CONTEXT "${UNINSTALL_REGISTRY_KEY}" "InstallLocation" "$INSTDIR"
!macroend

!macro customUnInstall
  ; #361: uninstall NEVER deletes user data. SessionEvent, artifacts,
  ; workspace files, the model config and Windows credentials live under
  ; %APPDATA%\intelligence-agent (outside the install directory) and are
  ; left intact. Explicit cleanup is a separate, previewed action:
  ;   node scripts/clean-user-data.mjs --preview
  ; The stock template only removes $APPDATA\${APP_FILENAME} when the
  ; --delete-app-data flag is passed; this installer never passes it.
  ;
  ; #904 item 4: the same update protocol leaves "<install dir>.old-{guid}"
  ; backup directories NEXT TO the install directory, and the stock uninstaller
  ; does not know them — one machine carried 3.50 GiB in five of them. This
  ; sweep is the authoritative discovery path for them: it enumerates by NAME
  ; SHAPE, so it does not depend on the IaBackupDir / IaLeftoverDir single
  ; slots (only one leftover can be recorded at a time, and the uninstall
  ; section is free to remove the registry key). Fail-closed throughout: only a
  ; name passing the shape check for "$INSTDIR.old-{8-4-4-4-12}"
  ; (iaStageApplication's own format, braces included — measured) is touched.
  ; StrCmp is case-insensitive by default and NTFS is too, so ".OLD-{…}" names
  ; the same directory: the check pins the name, not a particular case.
  ; Every candidate goes through the shared prepare step (which refuses a tree
  ; containing a reparse point — RMDir /r deletes THROUGH a junction, measured),
  ; and anything left behind — a refusal, a failed delete, or the user
  ; answering No to the prompt — is reported and turns the uninstall's exit code
  ; non-zero (the section's SetErrorLevel: it reaches a caller through an
  ; in-place launch -- the form the stock updater's own call uses -- while a
  ; plain launch only reports the stub's 0 either way; readings in
  ; test/harness/nsis-probes/launch-form-probe). $9 counts in the first pass,
  ; then whatever was kept. The prompt is interactive-only: its /SD IDOK default
  ; makes a silent uninstall take the delete path, so the declined branch below
  ; is never a silent run's outcome.
  ; $INSTDIR itself may already be gone here; only its name is needed, and the
  ; leftovers live beside it.
  FindFirst $0 $1 "$INSTDIR.old-*"
  StrCmp $0 "" iaSweepDone
  StrCpy $9 0
iaSweepCount:
  StrCmp $1 "" iaSweepAsk
  StrCmp $1 "." iaSweepCountNext
  StrCmp $1 ".." iaSweepCountNext
  IntOp $9 $9 + 1
iaSweepCountNext:
  FindNext $0 $1
  Goto iaSweepCount
iaSweepAsk:
  FindClose $0
  StrCmp $9 0 iaSweepDone
  MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "($9) $(iaLeftoverSweep)" /SD IDOK IDOK iaSweepDelete IDCANCEL iaSweepDeclined
iaSweepDeclined:
  ; #904 item 4: declining is a "kept" outcome, not a success — the leftovers
  ; are still on disk, so the exit code says so. No second dialog: the user
  ; just answered this one, and the DetailPrint line is the log's record.
  DetailPrint "customUnInstall: leftover sweep declined: $9 kept"
  SetErrorLevel 2
  Goto iaSweepDone
iaSweepDelete:
  ; The leftovers sit beside $INSTDIR and FindFirst hands back leaf names, so
  ; the parent directory is cut off $INSTDIR once (FileFunc.nsh's ${GetParent},
  ; the NSIS standard library helper -- "GetParent" is not an instruction).
  ; FileFunc's order is "[path]" $result: swapped, the macro's last Pop lands
  ; on $INSTDIR, the delete pass then enumerates ".old-*" relative and finds
  ; nothing (measured on the real uninstaller), so the guard pins this form.
  ${GetParent} "$INSTDIR" $2
  FindFirst $0 $1 "$INSTDIR.old-*"
  StrCmp $0 "" iaSweepDone
  StrCpy $9 0
iaSweepLoop:
  StrCmp $1 "" iaSweepLoopEnd
  StrCmp $1 "." iaSweepNext
  StrCmp $1 ".." iaSweepNext
  StrCpy $iaDeleteCandidate "$2\$1"
  StrCpy $iaDeleteBase "$INSTDIR"
  StrCpy $iaDeleteShapeCheck "1"
  ; This body only ever compiles inside the uninstaller build (the installer
  ; build never inserts customUnInstall, and NSIS compiles a macro body only
  ; where it is inserted -- measured: an invalid instruction in this body was
  ; reported by the BUILD_UNINSTALLER pass alone). There, this is un. code and
  ; NSIS only lets it Call un.-prefixed functions, so the call names the
  ; uninstaller instantiation of the helper directly. The installer's own
  ; delete sites use the plain name.
  Call un.iaPrepareDelete
  StrCmp $iaDeleteStatus "ok" 0 iaSweepKept
  ClearErrors
  RMDir /r "$iaDeleteTarget"
  IfErrors 0 iaSweepNext
  StrCpy $iaDeleteStatus "failed"
iaSweepKept:
  DetailPrint "customUnInstall: leftover kept: $iaDeleteCandidate ($iaDeleteStatus)"
  IntOp $9 $9 + 1
iaSweepNext:
  FindNext $0 $1
  Goto iaSweepLoop
iaSweepLoopEnd:
  FindClose $0
  StrCmp $9 0 iaSweepDone
  MessageBox MB_OK|MB_ICONEXCLAMATION "$(iaLeftoverKept)" /SD IDOK
  SetErrorLevel 2
iaSweepDone:
!macroend
