# #1201 — compiles the shipped installer scripts in BOTH passes, for real.
#
# WHY: the guard suites in desktop/test/ are static text analysis. They prove the
# rules still see the text they look for; they never run makensis, so a stray
# macro, a variable that is declared but unreferenced (fatal warning 6001) or an
# install-only function reachable from the uninstaller pass (fatal 6010) passes
# every suite and still breaks the real build, where electron-builder's
# warnings-as-errors turns each one into a failure.
#
# The wrapper below mirrors app-builder-lib/templates/nsis/installer.nsi: same
# BUILD_UNINSTALLER split, same WriteUninstaller/Uninstall-section shape, same
# includes package.nsh reaches through customHeader. Only the electron-builder
# bookkeeping (multiUser, UAC, MUI pages) is replaced by the defines the shipped
# scripts actually read.
#
# Read-only with respect to the repo; scratch lives under %TEMP%.
param([switch]$Keep)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$installerDir = (Resolve-Path (Join-Path $here '..\..\..\installer')).Path
$scratchRoot = Join-Path $env:TEMP 'ia-r1-drivers'
$runId = [Guid]::NewGuid().ToString('N')
$root = Join-Path $scratchRoot "compile-both-$runId"

$makensis = @(
  Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'electron-builder\Cache\nsis-3.0.4.1') `
    -Recurse -Filter 'makensis.exe' -ErrorAction SilentlyContinue
)[0].FullName

function Invoke-Pass($label, $defineBlock) {
  $nsi = Join-Path $root ("{0}.nsi" -f $label)
  $out = Join-Path $root ("{0}.exe" -f $label)
  $body = @"
Unicode true
!define APP_EXECUTABLE_FILENAME "Intelligence Agent.exe"
!define INSTALL_REGISTRY_KEY "Software\IA Compile Probe"
!define UNINSTALL_REGISTRY_KEY "Software\IA Compile Probe"
!define APP_GUID "00000000-0000-0000-0000-0000000000ff"
!define PRODUCT_NAME "Intelligence Agent"
!define APP_FILENAME "intelligence-agent"
!define VERSION "0.0.0"
!define UNINSTALLER_OUT_FILE "$root\uninst-__LABEL__.exe"
!define UNINSTALL_SECTION_NAME "Uninstall"
!define LANG_ENGLISH 1033
!define LANG_SIMPCHINESE 2052
; common.nsh is where the real template gets quitSuccess / LogSet from; the
; shipped scripts call it, so the wrapper has to provide it under the same name.
!define LogSet "!insertmacro LogSetMacro"
!macro LogSetMacro SETTING
!macroend
!macro quitSuccess
  SetErrorLevel 0
  Quit
!macroend
!macro check64BitAndSetRegView
!macroend
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
RequestExecutionLevel user
Name "compile probe"
OutFile "$out"
SilentInstall silent
$defineBlock

; Mirrors installer.nsi: customHeader pulls in the shipped scripts (and is where
; the BUILD_UNINSTALLER guard around installer-directories.nsh lives).
!ifmacrodef customHeader
  !insertmacro customHeader
!endif

; Mirrors assistedInstaller.nsh, which inserts customWelcomePage (the recovery
; page) ahead of the directory page and then the standard page set. MUI compiles
; a page's GUIInit function only when that page is actually inserted, so the
; pages have to be declared here or makensis reports them as unreferenced (6010).
!ifdef BUILD_UNINSTALLER
  !insertmacro MUI_UNPAGE_INSTFILES
!else
  !ifmacrodef customWelcomePage
    !insertmacro customWelcomePage
  !endif
  !insertmacro MUI_PAGE_DIRECTORY
  !insertmacro MUI_PAGE_INSTFILES
!endif
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "SimpChinese"

; installer.nsi declares the plain "install" section in BOTH passes (only its
; body is guarded), and the uninstaller pass adds uninstaller.nsh's un. section.
; makensis rejects a script with no plain section ("no sections specified"), so
; both are declared here exactly as the template does.
Section "install"
  !ifndef BUILD_UNINSTALLER
    !ifmacrodef customInstall
      !insertmacro customInstall
    !endif
  !endif
SectionEnd

!ifdef BUILD_UNINSTALLER
  Section "un.@@UNINSTALL_SECTION_NAME@@"
    !ifmacrodef customUnInstall
      !insertmacro customUnInstall
    !endif
  SectionEnd
  ; The real template writes the uninstaller out of the plain .onInit while
  ; BUILD_UNINSTALLER is defined (installer.nsi:63-66). WriteUninstaller is not
  ; valid inside an un. function.
  Function .onInit
    WriteUninstaller "@@UNINSTALLER_OUT_FILE@@"
  FunctionEnd
!else
  Function .onInit
    !ifmacrodef customInit
      !insertmacro customInit
    !endif
  FunctionEnd
!endif
"@
  # The here-string is expanded by PowerShell, so NSIS's own ${...} / $VAR tokens
  # have to be protected from it: @@...@@ placeholders survive expansion and are
  # substituted after, exactly like the r1 drivers do with @@PRODUCTION_...@@.
  $body = $body.Replace('__LABEL__', $label)
  $body = $body.Replace('@@UNINSTALLER_OUT_FILE@@', ('{0}\uninst-{1}.exe' -f $root, $label))
  $body = $body.Replace('@@UNINSTALL_SECTION_NAME@@', 'Uninstall')
  [System.IO.File]::WriteAllText($nsi, $body, [System.Text.UTF8Encoding]::new($true))

  $log = Join-Path $root ("{0}.log" -f $label)
  # makensis reports through stderr; a warnings-as-errors failure is a legitimate
  # outcome here, so do not let the native-error abort swallow the log.
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  & $makensis /V4 /WX $nsi *> $log
  $code = $LASTEXITCODE
  $ErrorActionPreference = $previous

  $text = Get-Content $log -Raw
  # Write-Host, not Write-Output: this function returns its exit code, and
  # Write-Output would join the pipeline and turn the result into an array.
  Write-Host ("=== {0} pass: makensis exit {1} ===" -f $label, $code)
  $diagnostics = @($text -split "`r?`n" | Where-Object { $_ -match 'warning \d+|error:|Error:' })
  if ($diagnostics.Count -gt 0) { $diagnostics | ForEach-Object { Write-Host ("  " + $_.Trim()) } }
  else {
    Write-Host '  (no matching diagnostics; raw tail follows)'
    @($text -split "`r?`n" | Where-Object { $_.Trim() -ne '' } | Select-Object -Last 6) |
      ForEach-Object { Write-Host ("  " + $_.Trim()) }
  }
  return $code
}

try {
  if (-not $makensis) { throw 'makensis.exe not found under the electron-builder cache' }
  New-Item -ItemType Directory -Force -Path $root | Out-Null
  # The shipped scripts are reached through ${__FILEDIR__} (IA_INSTALLER_DIR), so
  # they resolve relative to themselves; the wrapper only needs to include
  # installer.nsh by absolute path.
  $installerNsh = Join-Path $installerDir 'installer.nsh'

  $installerExit = Invoke-Pass 'installer' ('!include "{0}"' -f $installerNsh)
  $uninstallExit = Invoke-Pass 'uninstall' ('!define BUILD_UNINSTALLER' + "`r`n" + ('!include "{0}"' -f $installerNsh))

  if ($installerExit -ne 0 -or $uninstallExit -ne 0) {
    throw "makensis rejected a pass (installer=$installerExit uninstall=$uninstallExit)"
  }
  Write-Output ''
  Write-Output 'both passes compile with warnings-as-errors'
}
finally {
  if ($Keep) { Write-Output "kept scratch: $root" }
  elseif (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}
