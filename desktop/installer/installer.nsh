; #361 [W-16] Stock electron-builder NSIS custom include.
;
; Hooks used (see app-builder-lib/templates/nsis):
;   customHeader    - compile-time: strings, per-user manifest, directory-swap
;   customInit      - installer startup: refuse per-machine installs, then
;                     move any previous version aside (iaStageApplication)
;   customInstall   - end of the install section: verify the new files and
;                     either delete the backup or roll back (iaPromoteApplication)
;   customUnInstall - uninstall section: user data is NEVER deleted here;
;                     explicit cleanup is scripts/clean-user-data.mjs
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

; #885 — capture THIS file's directory at PARSE time into IA_INSTALLER_DIR.
; electron-builder `!include`s this file by absolute path, so ${__FILEDIR__}
; here is .../desktop/installer. It must be captured now (not inside the
; macro): the macro body is expanded later, while makensis reads installer.nsi
; (fed on stdin, cwd = the NSIS template dir, where ${__FILEDIR__} is "."), so
; a bare ${__FILEDIR__} inside the macro points at the template, not us.
!define IA_INSTALLER_DIR "${__FILEDIR__}"

!macro customHeader
  ManifestDPIAware true

  ; Keep the literal `\`: on Windows ${__FILEDIR__} has NO trailing separator
  ; (NSIS strips it via PathRemoveFileSpec), so it must be added here; on POSIX
  ; it keeps one and the doubled separator is tolerated. Omitting it produced
  ; `...\desktop\installerinstaller-directories.nsh` on the Windows runner.
  !include "${IA_INSTALLER_DIR}\installer-directories.nsh"

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
  !endif
  !ifdef LANG_SIMPCHINESE
  LangString iaPerUserOnly ${LANG_SIMPCHINESE} "此安装程序仅支持按用户安装。检测到 Intelligence Agent 的按计算机安装，请先卸载它，再重新运行此安装程序。"
  LangString iaAppRunning ${LANG_SIMPCHINESE} "Intelligence Agent（或其后台进程）仍在运行。请关闭后重新运行安装程序——旧版本未被改动。"
  LangString iaUpdateFailed ${LANG_SIMPCHINESE} "更新失败：新文件不完整。已恢复到旧版本。"
  LangString iaRollbackFailed ${LANG_SIMPCHINESE} "无法自动恢复旧版本。完整备份保留在："
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
!macroend
