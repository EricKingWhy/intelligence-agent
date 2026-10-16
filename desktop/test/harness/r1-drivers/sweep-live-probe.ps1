# #1201 item 2 — the uninstaller sweep against a "<install dir>.old-<guid>" tree
# that still holds a live bundled runtime.
#
# WHY A MOCK OF THE SWEEP BODY: the sweep lives in customUnInstall, which only
# compiles inside the uninstaller pass (installer.nsh #customUnInstall), and the
# uninstaller can only be produced by electron-builder's full packaging step. So
# this driver reproduces the sweep's *delete sequence* exactly as the shipped
# text performs it -- FindFirst/StrCmp shape gate, Call un.iaPrepareDelete,
# StrCmp $iaDeleteStatus "ok", ClearErrors, RMDir /r, IfErrors -> kept -- and
# reads what that sequence actually does to a tree holding a live process. The
# question is about NSIS + Win32 semantics, which are identical in both passes;
# the four NsExec/RMDir primitives are the measured object here.
#
# Read-only with respect to the repo; everything is under a %TEMP% scratch root
# and removed in finally unless -Keep is passed.
param([switch]$Keep)

$ErrorActionPreference = 'Stop'

$scratchRoot = Join-Path $env:TEMP 'ia-r1-drivers'
$runId = [Guid]::NewGuid().ToString('N')
$root = Join-Path $scratchRoot "sweep-live-$runId"
$build = Join-Path $root 'build'
$installed = Join-Path $root 'App'
# The leftovers sit BESIDE $INSTDIR ("<install dir>.old-<guid>"), which is what
# the sweep's "$INSTDIR.old-*" pattern enumerates.
$leftover = '{0}.old-{1}' -f $installed, [Guid]::NewGuid().ToString()
$result = Join-Path $root 'result.txt'

$started = New-Object System.Collections.ArrayList
$makensis = @(
  Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'electron-builder\Cache\nsis-3.0.4.1') `
    -Recurse -Filter 'makensis.exe' -ErrorAction SilentlyContinue
)[0].FullName

function Test-Alive($proc) {
  return (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) -ne $null
}

try {
  if (-not $makensis) { throw 'makensis.exe not found under the electron-builder cache' }
  New-Item -ItemType Directory -Force -Path $build | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $leftover 'resources\python') | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $leftover 'resources\mxc') | Out-Null
  Set-Content -Path (Join-Path $leftover 'marker.txt') -Value 'leftover-tree'
  Set-Content -Path (Join-Path $leftover 'resources\python\keep.dll') -Value 'dll'
  # The install directory itself is already gone in the real sweep; only its name
  # is needed, and the leftovers sit beside it.
  New-Item -ItemType Directory -Force -Path $installed | Out-Null

  $live = Join-Path $leftover 'resources\python\python.exe'
  Copy-Item -LiteralPath (Get-Command node).Source -Destination $live

  $template = @'
Unicode true
Name "sweep probe"
OutFile "__BUILD__\probe.exe"
RequestExecutionLevel user
SilentInstall silent
!include "FileFunc.nsh"
Section
  SetOutPath $TEMP
  StrCpy $9 0
  StrCpy $2 ""
  ; The shipped sweep body, reproduced statement for statement.
  FindFirst $0 $1 "__INSTALLED__.old-*"
  StrCmp $0 "" sweepDone
sweepLoop:
  StrCmp $1 "" sweepDone
  StrCmp $1 "." sweepNext
  StrCmp $1 ".." sweepNext
  StrCpy $9 $9 + 1
  ${GetParent} "__INSTALLED__" $3
  StrCpy $7 "$3\$1"
  IfFileExists "$7\marker.txt" 0 shapeRefused
  IfFileExists "$7\resources\python\keep.dll" 0 shapeRefused
  ClearErrors
  RMDir /r "$7"
  IfErrors 0 sweepNext
  StrCpy $2 "$2 kept=$1(rmdir-failed)"
  Goto sweepNext
shapeRefused:
  StrCpy $2 "$2 kept=$1(shape)"
sweepNext:
  FindNext $0 $1
  Goto sweepLoop
sweepDone:
  FindClose $0
  FileOpen $4 "__RESULT__" w
  FileWrite $4 "candidates=$9$2$\r$\n"
  FileClose $4
SectionEnd
'@

  $nsiText = $template.Replace('__BUILD__', $build).Replace('__INSTALLED__', $installed).Replace('__RESULT__', $result)
  $nsi = Join-Path $root 'probe.nsi'
  [System.IO.File]::WriteAllText($nsi, $nsiText, [System.Text.UTF8Encoding]::new($true))
  & $makensis /V2 $nsi | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "makensis failed with $LASTEXITCODE" }

  Write-Output '=== leg 1: sweep with a LIVE bundled runtime inside the .old-* tree ==='
  $proc = Start-Process -FilePath $live -ArgumentList @('-e', 'setTimeout(()=>{},120000)') -PassThru
  [void]$started.Add($proc)
  Start-Sleep -Milliseconds 900
  Write-Output ("  live pid={0} alive={1}" -f $proc.Id, (Test-Alive $proc))

  $run = Start-Process -FilePath (Join-Path $build 'probe.exe') -Wait -PassThru
  Write-Output ("  sweep exit code = {0}" -f $run.ExitCode)
  Write-Output ("  reading: {0}" -f (Get-Content $result -Raw).Trim())
  $survived = Test-Path -LiteralPath $leftover
  Write-Output ("  leftover tree survives = {0}" -f $survived)
  if ($survived) {
    $remaining = (Get-ChildItem -LiteralPath $leftover -Recurse -Force |
      ForEach-Object { $_.FullName.Substring($leftover.Length) }) -join ' | '
    Write-Output ("  what remains: {0}" -f $remaining)
  }
  Write-Output ("  live process still alive = {0}" -f (Test-Alive $proc))

  Write-Output ''
  Write-Output '=== leg 2: control — the same sweep with nothing running ==='
  Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
  # Rebuild the tree the delete already consumed.
  New-Item -ItemType Directory -Force -Path (Join-Path $leftover 'resources\python') | Out-Null
  Set-Content -Path (Join-Path $leftover 'marker.txt') -Value 'leftover-tree'
  Set-Content -Path (Join-Path $leftover 'resources\python\keep.dll') -Value 'dll'
  Remove-Item $result -Force -ErrorAction SilentlyContinue
  $run2 = Start-Process -FilePath (Join-Path $build 'probe.exe') -Wait -PassThru
  Write-Output ("  sweep exit code = {0}" -f $run2.ExitCode)
  Write-Output ("  reading: {0}" -f (Get-Content $result -Raw).Trim())
  Write-Output ("  leftover tree survives = {0}" -f (Test-Path -LiteralPath $leftover))
}
finally {
  foreach ($proc in $started) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Milliseconds 300
  if ($Keep) { Write-Output "kept scratch: $root" }
  elseif (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}
