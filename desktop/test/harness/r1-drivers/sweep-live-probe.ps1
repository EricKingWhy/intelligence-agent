# #1201 item 2 — the uninstaller sweep against a "<install dir>.old-<guid>" tree
# that still holds a live bundled runtime.
#
# WHY A MOCK OF THE SWEEP BODY: the sweep lives in customUnInstall, which only
# compiles inside the uninstaller pass (installer.nsh customUnInstall), and the
# uninstaller can only be produced by electron-builder's full packaging step. So
# this driver reproduces the sweep's decision sequence exactly as each revision
# of the shipped text performs it and reads what that sequence actually does to
# a tree holding a live process. The question is about NSIS + Win32 semantics,
# which are identical in both passes; the NsExec/RMDir primitives are the
# measured object here. The post-change sequence harvests the production live
# check (installer-cleanup.nsh's iaScanLiveProcessesBody) verbatim, so the
# reading describes the shipped predicate rather than a paraphrase.
#
# Red reading (pre-fix sequence): with a live runtime inside the tree, RMDir /r
# reports an error, the tree survives PARTIALLY deleted (every file the process
# does not hold open is gone) and the process stays alive — a mangled tree plus
# an orphan, reported as kept. Control: nothing running, the tree is deleted
# cleanly.
#
# Green reading (post-fix sequence): the live check answers 1 for the same
# scene, the delete never runs, the tree survives WHOLE and the process stays
# alive; with nothing running the check answers 0 and the tree is deleted
# exactly as before.
#
# Read-only with respect to the repo; everything is under a %TEMP% scratch root
# and removed in finally unless -Keep is passed.
param([switch]$Keep, [string]$CleanupSource)

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

# The production live-check body, taken verbatim so the green reading describes
# the shipped predicate (the same reason run-rollback-gate-probe harvests the
# rollback function). Overridable with -CleanupSource for a red-side variant.
function Get-ProductionBlock([string]$Source, [string]$StartToken, [string]$EndToken) {
  $i = $Source.IndexOf($StartToken, [System.StringComparison]::Ordinal)
  if ($i -lt 0) { throw "production text not found: $StartToken" }
  $j = $Source.IndexOf($EndToken, $i + $StartToken.Length, [System.StringComparison]::Ordinal)
  if ($j -lt 0) { throw "production text has no $EndToken : $StartToken" }
  return $Source.Substring($i, $j + $EndToken.Length - $i)
}

try {
  if (-not $makensis) { throw 'makensis.exe not found under the electron-builder cache' }
  $repoCleanup = Join-Path $PSScriptRoot '..\..\..\installer\installer-cleanup.nsh'
  $cleanupSource = if ($CleanupSource) {
    [System.IO.File]::ReadAllText((Resolve-Path $CleanupSource).Path, [System.Text.Encoding]::UTF8)
  } else {
    [System.IO.File]::ReadAllText($repoCleanup, [System.Text.Encoding]::UTF8)
  }
  $liveBody = Get-ProductionBlock $cleanupSource '!macro iaScanLiveProcessesBody' '!macroend'

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

  # The pre-fix sweep body: shape gate, then the delete, no process read.
  $before = @'
Unicode true
Name "sweep probe (pre-fix)"
OutFile "__BUILD__\probe-before.exe"
RequestExecutionLevel user
SilentInstall silent
!include "FileFunc.nsh"
Section
  SetOutPath $TEMP
  StrCpy $9 0
  StrCpy $2 ""
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

  # The post-fix sweep body: the production live check runs on the candidate
  # before the delete, and anything not proven clean is kept whole. The check is
  # inserted statement-for-statement where the shipped sweep now calls it. The
  # sweep only compiles as un. code, so this probe writes a real uninstaller out
  # of the plain .onInit-equivalent section (installer.nsi:63-66 does it from
  # .onInit — WriteUninstaller is not valid inside an un. function) and runs
  # THAT, which is also how the shipped sweep is reached. SilentUnInstall keeps
  # the run non-interactive, and the uninstaller's own OutFile form is the exe
  # the legs execute.
  $after = @'
Unicode true
Name "sweep probe (post-fix)"
OutFile "__BUILD__\probe-after-installer.exe"
RequestExecutionLevel user
SilentInstall silent
SilentUnInstall silent
!include "FileFunc.nsh"
Var iaLiveDir
Var iaLiveAnswer
@@LIVE_BODY@@
Function un.iaScanLiveProcesses
  !insertmacro iaScanLiveProcessesBody
FunctionEnd
; The sweep body only ever compiles as un. code, and NSIS refuses a Call to an
; un. name outside an uninstall section — so the delete pass runs in a real
; un. section, the name space the shipped customUnInstall expands in.
Section "install"
  ; WriteUninstaller resolves its argument against $INSTDIR, so the probe points
  ; $INSTDIR at its own scratch build directory first (a plain StrCpy on the
  ; built-in variable; SetOutPath alone does not move $INSTDIR).
  StrCpy $INSTDIR "__BUILD__"
  WriteUninstaller "probe-after.exe"
SectionEnd
Section "un.Sweep"
  SetOutPath $TEMP
  StrCpy $9 0
  StrCpy $2 ""
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
  Push "$7"
  Call un.iaScanLiveProcesses
  StrCmp $iaLiveAnswer "0" 0 keptLive
  ClearErrors
  RMDir /r "$7"
  IfErrors 0 sweepNext
  StrCpy $2 "$2 kept=$1(rmdir-failed)"
  Goto sweepNext
keptLive:
  StrCpy $2 "$2 kept=$1(live-answer-$iaLiveAnswer)"
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
'@.Replace('@@LIVE_BODY@@', $liveBody)

  foreach ($pair in @(@('before', $before), @('after', $after))) {
    $label = $pair[0]
    $nsiText = $pair[1].Replace('__BUILD__', $build).Replace('__INSTALLED__', $installed).Replace('__RESULT__', $result)
    $nsi = Join-Path $root ("probe-$label.nsi")
    [System.IO.File]::WriteAllText($nsi, $nsiText, [System.Text.UTF8Encoding]::new($true))
    & $makensis /V2 $nsi | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "makensis ($label) failed with $LASTEXITCODE" }
  }

  # --- red reading: the pre-fix sequence against a live runtime ---------------
  Write-Output '=== leg 1: PRE-FIX sweep with a LIVE bundled runtime inside the .old-* tree ==='
  $proc = Start-Process -FilePath $live -ArgumentList @('-e', 'setTimeout(()=>{},120000)') -PassThru
  [void]$started.Add($proc)
  Start-Sleep -Milliseconds 900
  Write-Output ("  live pid={0} alive={1}" -f $proc.Id, (Test-Alive $proc))

  $run = Start-Process -FilePath (Join-Path $build 'probe-before.exe') -Wait -PassThru
  Write-Output ("  sweep exit code = {0}" -f $run.ExitCode)
  $preReading = (Get-Content $result -Raw).Trim()
  Write-Output ("  reading: {0}" -f $preReading)
  $survived = Test-Path -LiteralPath $leftover
  Write-Output ("  leftover tree survives = {0}" -f $survived)
  if ($survived) {
    $remaining = (Get-ChildItem -LiteralPath $leftover -Recurse -Force |
      ForEach-Object { $_.FullName.Substring($leftover.Length) }) -join ' | '
    Write-Output ("  what remains: {0}" -f $remaining)
  }
  Write-Output ("  live process still alive = {0}" -f (Test-Alive $proc))
  if ($preReading -notmatch 'rmdir-failed') { throw 'the pre-fix sweep did not record a failed delete — the red reading is gone' }
  if ($survived -eq $false) { throw 'the pre-fix sweep deleted the tree — the red reading is gone' }

  # Rebuild the tree the pre-fix delete consumed, for the post-fix leg.
  New-Item -ItemType Directory -Force -Path (Join-Path $leftover 'resources\python') | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $leftover 'resources\mxc') | Out-Null
  Set-Content -Path (Join-Path $leftover 'marker.txt') -Value 'leftover-tree'
  Set-Content -Path (Join-Path $leftover 'resources\python\keep.dll') -Value 'dll'
  if (Test-Alive $proc) { Write-Output '  (live process kept across the rebuild)' } else { throw 'the live process died before the post-fix leg' }

  # The uninstaller exe only comes into being when the installer stub runs
  # (WriteUninstaller is an instruction, not a makensis output), so leg 2 runs
  # the stub first, then the uninstaller it wrote — the same two-binary shape
  # the shipped product has.
  $stubRun = Start-Process -FilePath (Join-Path $build 'probe-after-installer.exe') -Wait -PassThru
  if ($stubRun.ExitCode -ne 0) { throw "the post-fix installer stub failed with $($stubRun.ExitCode)" }
  $afterExe = Join-Path $build 'probe-after.exe'
  if (-not (Test-Path -LiteralPath $afterExe)) { throw 'the post-fix installer stub did not write the uninstaller exe' }

  Write-Output ''
  Write-Output '=== leg 2: POST-FIX sweep, same scene (live runtime inside the tree) ==='
  Remove-Item $result -Force -ErrorAction SilentlyContinue
  $run2 = Start-Process -FilePath $afterExe -Wait -PassThru
  Write-Output ("  sweep exit code = {0}" -f $run2.ExitCode)
  $postReading = (Get-Content $result -Raw).Trim()
  Write-Output ("  reading: {0}" -f $postReading)
  Write-Output ("  leftover tree survives = {0}" -f (Test-Path -LiteralPath $leftover))
  $stillWhole = (Test-Path -LiteralPath (Join-Path $leftover 'resources\mxc')) -and (Test-Path -LiteralPath (Join-Path $leftover 'marker.txt'))
  Write-Output ("  tree left WHOLE (mxc dir and marker still present) = {0}" -f $stillWhole)
  Write-Output ("  live process still alive = {0}" -f (Test-Alive $proc))
  if ($postReading -notmatch 'live-answer-1') { throw "the post-fix live check did not answer 1 for a live tree: $postReading" }
  if (-not $stillWhole) { throw 'the post-fix sweep deleted from the live tree — the keep-whole contract is broken' }

  Write-Output ''
  Write-Output '=== leg 3: POST-FIX sweep with nothing running (control) ==='
  Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
  Remove-Item $result -Force -ErrorAction SilentlyContinue
  $run3 = Start-Process -FilePath $afterExe -Wait -PassThru
  Write-Output ("  sweep exit code = {0}" -f $run3.ExitCode)
  $ctlReading = (Get-Content $result -Raw).Trim()
  Write-Output ("  reading: {0}" -f $ctlReading)
  Write-Output ("  leftover tree survives = {0}" -f (Test-Path -LiteralPath $leftover))
  if ($ctlReading -match 'live-answer-1') { throw "the live check still answers 1 with nothing running: $ctlReading" }
  if (Test-Path -LiteralPath $leftover) { throw 'the control tree was not deleted cleanly' }
  Write-Output ''
  Write-Output 'sweep readings OK: pre-fix partial delete + live orphan; post-fix keeps the live tree whole; control deletes cleanly'
}
finally {
  foreach ($proc in $started) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Milliseconds 300
  if ($Keep) { Write-Output "kept scratch: $root" }
  elseif (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
}
