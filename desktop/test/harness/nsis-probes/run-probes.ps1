# Compile and run the makensis probes in this directory, then clean up.
#
# The probe sources back the guard rules in scripts/build-windows-installer.mjs
# (#901 / #905): each one answers a question the guard's text scan depends on —
# how NSIS treats quote styles, `;`/`#` comments, `$\<quote>` escapes, variable
# case and `${Errors}` outside a condition. See ../README.md for the measured
# outcomes.
#
# The probes are compiled and run in a throwaway temp directory, so this
# checkout stays clean; the only machine state they touch is the probes' own
# registry key HKCU\Software\p905, which is deleted afterwards.
#
# Usage (from this directory), makensis discovered from the electron-builder
# cache or passed explicitly:
#   powershell -File run-probes.ps1 [-Makensis <path to makensis.exe>] [-KeepKey]
param(
  [string]$Makensis = '',
  [switch]$KeepKey
)
$ErrorActionPreference = 'Stop'
if ($Makensis -eq '') {
  $candidates = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'electron-builder\Cache') -Recurse -Filter makensis.exe -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending
  if ($candidates.Count -eq 0) {
    throw 'makensis.exe not found; pass -Makensis <path> (electron-builder downloads it into %LOCALAPPDATA%\electron-builder\Cache\nsis-*)'
  }
  $Makensis = $candidates[0].FullName
}
if (-not (Test-Path -LiteralPath $Makensis)) { throw "makensis not found at $Makensis" }
Write-Host "makensis: $Makensis"
# Native tools write to stderr for the expected failures too (errors-forms and
# unknown-var are supposed to fail to compile), and under PowerShell 5.1 a
# redirected stderr line becomes a terminating error when the preference is
# 'Stop'. Run native tools with 'Continue' and read the exit code instead.
function Invoke-Native {
  param([string]$FilePath, [string[]]$Arguments)
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $text = & $FilePath @Arguments 2>&1 | ForEach-Object { "$_" }
    $exit = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previous
  }
  [pscustomobject]@{ Text = @($text); ExitCode = $exit }
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("ia-nsis-probes-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
Copy-Item (Join-Path $here '*.nsi') $work
Push-Location $work
try {
  # Compile-only probes (they show a UI or a box when run) — the build log is
  # the measurement. errors-forms and unknown-var are expected NOT to compile:
  # that failure is what they measure.
  $compileOnly = @('var-case', 'unknown-var', 'quote-forms', 'errors-forms', 'hash-comment')
  foreach ($nsi in Get-ChildItem -Filter *.nsi) {
    $built = Invoke-Native -FilePath $Makensis -Arguments @('/V2', $nsi.Name)
    Write-Host "=== $($nsi.Name) (exit $($built.ExitCode))"
    $built.Text | Select-Object -Last 3 | ForEach-Object { Write-Host "    $_" }
    if ($built.ExitCode -ne 0) { continue }
    if ($compileOnly -contains $nsi.BaseName) { continue }
    Start-Process -FilePath (Join-Path $work ($nsi.BaseName + '.exe')) -Wait
  }
  Write-Host '=== HKCU\Software\p905'
  if (Test-Path 'HKCU:\Software\p905') {
    (Invoke-Native -FilePath 'reg' -Arguments @('query', 'HKCU\Software\p905', '/s')).Text |
      ForEach-Object { Write-Host "    $_" }
  } else {
    Write-Host '    (key absent)'
  }
} finally {
  if ((-not $KeepKey) -and (Test-Path 'HKCU:\Software\p905')) {
    $deleted = Invoke-Native -FilePath 'reg' -Arguments @('delete', 'HKCU\Software\p905', '/f')
    Write-Host "cleanup: reg delete exit $($deleted.ExitCode)"
  }
  Pop-Location
  Remove-Item -Recurse -Force $work
}
