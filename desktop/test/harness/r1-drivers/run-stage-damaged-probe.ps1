# Compile and run the PRODUCTION iaStageApplication macro against a damaged
# installation (#914 review Axis A — the user-ruled must-fix) and the #1165
# rejection paths.
#
# The Axis A defect was a placement defect, not a logic defect: the gate call sat
# INSIDE the `${If} $iaProbeFound == "1"` branch, and that branch skips staging
# entirely when the app executable is gone. A damaged installation (exe
# quarantined or deleted by hand) still holds the live bundled service, and the
# stock section then installed straight over it — #914's symptom on an edge path.
#
# run-process-gate-probe.ps1 drives the gate FUNCTION, so it cannot see where the
# call sits. This driver compiles the real macro (taken verbatim from
# installer-directories.nsh, with iaRecoverInterruptedInstall,
# iaStageApplication, iaProbePath and the cleanup helpers they call) and drives
# the legs below:
#
#   intact-clean     app.exe present, nothing running   -> gate returns, staging
#                    proceeds: the tree is renamed to "<dir>.old-{guid}"
#   damaged-runtime  no app.exe, headless image under resources\python running
#                    -> the gate runs anyway, stops it, and returns (no staging)
#   damaged-mixed    no app.exe, the headless image AND a client image in the
#                    install root both running -> answer 12: the gate aborts
#                    (exit 2) and stops NEITHER, so the client's live session
#                    survives (#1074; the #914 adjudication D1/D4)
#   damaged-rootproc no app.exe, live image in the install root -> the gate runs
#                    anyway and aborts (exit 2), the process stays alive
#   damaged-unknown  no app.exe, install path the query cannot parse -> the gate
#                    runs anyway and aborts (exit 2) instead of reading the
#                    failure as clean
#   first-install    no install tree at all (the path does not exist yet) -> the
#                    gate still classifies it, proves the empty directory clean
#                    and returns, and staging is skipped because there is no app
#                    executable to find: a fresh install proceeds (#1074)
#   missing-backup   damaged install plus a correctly-shaped but missing backup
#                    path -> first run refuses with exit 2 and clears IaBackupDir;
#                    second run reaches the existing no-pointer path and the
#                    process gate before staging
#   foreign-shape    intact install plus an IaBackupDir outside the installer's
#                    backup shape -> exit 2, pointer cleared, tree not renamed,
#                    and the success exit-code reset is unreachable (#1165)
#
#   window-runtime   app.exe present and the gate already returned, then the
#                    bundled service starts inside the staging window -> the
#                    last-moment re-check stops it and the update proceeds
#   window-client    the same window, but a client image starts in the install
#                    root -> the re-check refuses (exit 2), the client keeps
#                    running and the installation is left exactly as it was
#                    (#1066: a client in the install root is never killed, #914
#                    adjudication D4)
#
# The damaged legs are the discriminating ones: before the #914 fix the macro
# never called the gate on any of them (exit 0, service untouched). The
# first-install leg is coverage rather than discrimination — it pins that the
# gate the #914 fix round widened to first installs does not block one (#1074).
#
# damaged-mixed and damaged-runtime are a pair on purpose. The classifier answers
# 11 ("only the bundled runtime") or 12 ("anything else, or the runtime plus
# anything else"), and the gate stops processes only on 11. A weakening that
# collapses 12 into 11 — e.g. `$iaRuntimeProcs.Count -eq $iaAll.Count` rewritten
# as `-ge 1`, or as `-ge 0` — therefore still ends in a refusal on every
# single-process leg (the same exit code, so the leg cannot tell), but on this
# pair it turns the refusal into one that first kills the bundled runtime. Only
# the mixed leg has a non-runtime process in the root, so only it can see the
# difference (#1074 review Axis A).
#
# window-runtime and window-client pin the second call site (#1066). The gate at
# the top of the macro runs before the staging branch, so everything up to the
# rename is a window in which the user can start the service. A headless service
# does not block the rename (measured, #914 section 1.3), so before the fix the
# update landed and left the old service answering clients out of the
# "<dir>.old-{guid}" tree. -DirectoriesSource with the second call deleted is
# the red proof: window-runtime then reports the service still alive.
#
# Every run clears the probe registry key first. The #1165 legs seed a pointer
# only after that reset; they use this dedicated key rather than the product's
# registry location.
#
# Usage (from desktop/): powershell -File test/harness/r1-drivers/run-stage-damaged-probe.ps1 [-Keep]
#   -DirectoriesSource <path>  drive the legs against another revision of
#                              installer-directories.nsh. Used for the red proof:
#                              the #914/#1074 legs distinguish process-gate
#                              regressions and the #1165 legs fail on the old
#                              rejection paths. #1074 also uses this switch to
#                              show that a body-level early return inside the
#                              gate — which every build-time rule accepts —
#                              turns the process legs red.
#   -Expect1165Regression      assert both #1165 symptoms against the supplied
#                              pre-fix source, then exit successfully.
param([switch]$Keep, [string]$DirectoriesSource, [switch]$Expect1165Regression)
$ErrorActionPreference = 'Stop'

$harness = Split-Path -Parent $MyInvocation.MyCommand.Path
$installerDir = (Resolve-Path (Join-Path $harness '..\..\..\installer')).Path
$scratchRoot = [System.IO.Path]::GetFullPath((Join-Path $env:TEMP 'ia-r1-drivers'))
$runId = [Guid]::NewGuid().ToString('N')
# The probe defines ${INSTALL_REGISTRY_KEY} as this scratch key, so the run never
# touches the product's own HKCU entry. Removed in the finally block.
$probeRegistryKey = 'HKCU:\Software\IA Probe 914 Stage'

$makensis = @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'electron-builder\Cache\nsis-3.0.4.1') -Recurse -Filter 'makensis.exe' -ErrorAction SilentlyContinue)[0].FullName
if ([string]::IsNullOrEmpty($makensis)) { throw 'makensis.exe not found in the electron-builder cache' }
$nodeExe = (Get-Command node).Source
if ([string]::IsNullOrEmpty($nodeExe)) { throw 'node.exe not found' }

# --- production text, taken verbatim from the shipped includes ---------------
$directoriesPath = if ([string]::IsNullOrEmpty($DirectoriesSource)) {
  Join-Path $installerDir 'installer-directories.nsh'
} else {
  (Resolve-Path $DirectoriesSource).Path
}
Write-Host "installer-directories.nsh: $directoriesPath"
$directoriesSource = [System.IO.File]::ReadAllText($directoriesPath, [System.Text.Encoding]::UTF8)
$cleanupSource = [System.IO.File]::ReadAllText((Join-Path $installerDir 'installer-cleanup.nsh'), [System.Text.Encoding]::UTF8)

function Get-ProductionBlock {
  param([string]$Source, [string]$StartToken, [string]$EndToken, [string]$Label)
  $start = $Source.IndexOf($StartToken, [System.StringComparison]::Ordinal)
  if ($start -lt 0) { throw "production text not found: $Label" }
  $end = $Source.IndexOf($EndToken, $start + $StartToken.Length, [System.StringComparison]::Ordinal)
  if ($end -lt 0) { throw "production text has no $EndToken : $Label" }
  return $Source.Substring($start, $end + $EndToken.Length - $start)
}

$probeTemplatePath = Join-Path $harness 'stage-damaged-probe.nsi'
$probeTemplate = [System.IO.File]::ReadAllText($probeTemplatePath, [System.Text.Encoding]::UTF8)
$stageMacro = Get-ProductionBlock $directoriesSource '!macro iaStageApplication' '!macroend' 'iaStageApplication'
$recoveryMacro = Get-ProductionBlock $directoriesSource '!macro iaRecoverInterruptedInstall' '!macroend' 'iaRecoverInterruptedInstall'
$backupMacros = (Get-ProductionBlock $directoriesSource '!macro iaClearBackupDir' '!macroend' 'iaClearBackupDir') +
  "`r`n`r`n" +
  (Get-ProductionBlock $directoriesSource '!macro iaReadBackupDir' '!macroend' 'iaReadBackupDir') +
  "`r`n`r`n" +
  (Get-ProductionBlock $directoriesSource '!macro iaStashBackupDir' '!macroend' 'iaStashBackupDir')
$replacements = [ordered]@{
  '@@PRODUCTION_IA_CLEANUP@@'       = $cleanupSource
  '@@PRODUCTION_IA_PROBE_PATH@@'    = Get-ProductionBlock $directoriesSource 'Function iaProbePath' 'FunctionEnd' 'iaProbePath'
  '@@PRODUCTION_IA_BACKUP_MACROS@@' = $backupMacros
  '@@PRODUCTION_IA_RECOVERY@@'      = $recoveryMacro
  '@@PRODUCTION_IA_CLASSIFY@@'      = Get-ProductionBlock $directoriesSource 'Function iaClassifyRunningProduct' 'FunctionEnd' 'iaClassifyRunningProduct'
  '@@PRODUCTION_IA_REQUIRE@@'       = Get-ProductionBlock $directoriesSource 'Function iaRequireNoRunningProduct' 'FunctionEnd' 'iaRequireNoRunningProduct'
  '@@PRODUCTION_IA_STAGE@@'         = $stageMacro
}
$probeSource = $probeTemplate
foreach ($marker in $replacements.Keys) {
  if ([regex]::Matches($probeSource, [regex]::Escape($marker)).Count -ne 1) {
    throw "expected exactly one marker $marker in $probeTemplatePath"
  }
  $probeSource = $probeSource.Replace($marker, $replacements[$marker])
}
if ($probeSource -match '@@PRODUCTION_IA_') { throw 'production injection failed' }
if (-not $probeSource.Contains('!insertmacro iaStageApplication')) { throw 'the injected staging macro is not called' }
if (-not $probeSource.Contains('@(Get-CimInstance')) { throw 'the injected classification lost its array cast' }
# Where the gate call sits is NOT asserted here: the three damaged legs below are
# what pin it (they fail on a macro that calls the gate inside the app-executable
# branch), and a pre-flight assertion would block -DirectoriesSource from
# producing that red proof.

New-Item -ItemType Directory -Force -Path $scratchRoot | Out-Null

function New-FakeInstall {
  param([string]$Name, [switch]$WithAppExe, [switch]$NoTree)
  $root = Join-Path $scratchRoot ("stage-$Name-$runId")
  if (-not [System.IO.Path]::GetFullPath($root).StartsWith($scratchRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "scratch path escapes the test root: $root"
  }
  # A first install has no tree yet: the path must stay absent, so nothing is
  # created for it (#1074).
  if ($NoTree) { return $root }
  New-Item -ItemType Directory -Force -Path (Join-Path $root 'resources\python') | Out-Null
  if ($WithAppExe) {
    # Existence is all iaProbePath reads; a small placeholder keeps the leg fast.
    Set-Content -LiteralPath (Join-Path $root 'app.exe') -Value 'probe placeholder' -Encoding ascii
  }
  return $root
}

function Get-OldSiblings {
  param([string]$Root)
  $parent = Split-Path -Parent $Root
  $leaf = Split-Path -Leaf $Root
  return @(Get-ChildItem -LiteralPath $parent -ErrorAction SilentlyContinue |
    Where-Object { $_.Name.StartsWith($leaf + '.old-', [System.StringComparison]::OrdinalIgnoreCase) })
}

function Wait-ProcessGone {
  param([int]$ProcessId, [int]$Seconds = 10)
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
    if ((Get-Date) -gt $deadline) { return $false }
    Start-Sleep -Milliseconds 100
  }
  return $true
}

function Test-ProcessAlive {
  param([int]$ProcessId)
  return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Reset-ProbeState {
  # Every run starts with an empty scratch registry key. Individual rejection
  # legs seed the exact IaBackupDir value they need after this reset.
  Remove-Item -LiteralPath $probeRegistryKey -Recurse -Force -ErrorAction SilentlyContinue
}

function Wait-StageWindow {
  param([string]$Leg, [int]$Seconds = 30)
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    $marker = (Get-ItemProperty -Path $probeRegistryKey -Name 'IaTestStageWindowOpen' -ErrorAction SilentlyContinue).IaTestStageWindowOpen
    if ($marker -eq '1') { return }
    Start-Sleep -Milliseconds 100
  }
  throw "[$Leg] the installer never opened the stage window (IaTestStageWindowOpen under $probeRegistryKey)"
}

function Invoke-StageLeg {
  param([string]$Leg, [string]$Install, [switch]$HoldWindow, [string]$WindowExe, [string]$BackupPointer)
  $buildDir = Join-Path $scratchRoot ("stage-$Leg-build-$runId")
  $result = Join-Path $scratchRoot ("stage-$Leg-result-$runId.txt")
  New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
  $script = Join-Path $buildDir 'stage-damaged-probe.nsi'
  $exe = Join-Path $buildDir 'stage-damaged-probe.exe'
  # The injected production text carries non-ASCII comment characters (—, ×, …),
  # and makensis reads a BOM-less UTF-8 script as ANSI ("Bad text encoding"), so
  # this file is written with a BOM.
  [System.IO.File]::WriteAllText($script, $probeSource, [System.Text.UTF8Encoding]::new($true))
  $defines = if ($HoldWindow) { @('/DIA_TEST_HOLD_STAGE_WINDOW=1') } else { @() }
  $arguments = @('/V2', "/DPROBE_INSTALL=$Install", "/DPROBE_LEG=$Leg", "/DPROBE_RESULT=$result") + $defines + @($script)
  & $makensis @arguments
  if ($LASTEXITCODE -ne 0) { throw "[$Leg] makensis failed with exit code $LASTEXITCODE" }
  if (-not (Test-Path -LiteralPath $exe)) { throw "[$Leg] probe exe missing: $exe" }

  Remove-Item -LiteralPath $result -ErrorAction SilentlyContinue
  Reset-ProbeState
  if (-not [string]::IsNullOrEmpty($BackupPointer)) {
    New-Item -ItemType Directory -Force -Path $probeRegistryKey | Out-Null
    New-ItemProperty -LiteralPath $probeRegistryKey -Name 'IaBackupDir' -Value $BackupPointer -PropertyType String -Force | Out-Null
  }
  $windowProcess = $null
  if (-not $HoldWindow) {
    $process = Start-Process -FilePath $exe -Wait -PassThru
  } else {
    # #1066: the installer holds a window open between the gate at the top of the
    # staging macro and the last-moment re-check, so the driver has to act while
    # the installer is still running.
    $process = Start-Process -FilePath $exe -PassThru
    $script:started += $process.Id
    Wait-StageWindow -Leg $Leg
    if (-not [string]::IsNullOrEmpty($WindowExe)) {
      $windowProcess = Start-Process -FilePath $WindowExe -ArgumentList @('-e', 'setTimeout(()=>{},120000)') -PassThru
      $script:started += $windowProcess.Id
      $probePath = (Get-Process -Id $windowProcess.Id -ErrorAction SilentlyContinue).Path
      if ($probePath -ne $WindowExe) { throw "[$Leg] the process started inside the window reports image path '$probePath', expected '$WindowExe'" }
      Write-Host ("[{0}] started {1} inside the window" -f $Leg, $windowProcess.Id)
    }
    $process.WaitForExit()
  }
  $exitCode = $process.ExitCode
  $reading = if (Test-Path -LiteralPath $result) { (Get-Content -LiteralPath $result -Raw).Trim() } else { '<no reading>' }
  $backupPointerAfter = (Get-ItemProperty -LiteralPath $probeRegistryKey -Name 'IaBackupDir' -ErrorAction SilentlyContinue).IaBackupDir
  $pointerReading = if ($null -eq $backupPointerAfter) { '<empty>' } else { $backupPointerAfter }
  Write-Host ("[{0}] exit={1} pointer={2} {3}" -f $Leg, $exitCode, $pointerReading, $reading)
  return [pscustomobject]@{ Leg = $Leg; ExitCode = $exitCode; Reading = $reading; Install = $Install; BackupPointer = $backupPointerAfter; WindowProcess = $windowProcess }
}

$started = @()
$failures = @()
try {
  if ($Expect1165Regression) {
    if ([string]::IsNullOrEmpty($DirectoriesSource)) {
      throw '-Expect1165Regression requires -DirectoriesSource with the pre-fix installer source'
    }
    $regressionFailures = @()

    $installMissingBackup = New-FakeInstall 'missing-backup-pre-fix'
    $missingBackupPointer = "$installMissingBackup.old-{00000000-0000-0000-0000-000000000000}"
    $firstMissingBackup = Invoke-StageLeg -leg 'missing-backup-pre-fix' -install $installMissingBackup -BackupPointer $missingBackupPointer
    if ($firstMissingBackup.ExitCode -ne 2 -or $firstMissingBackup.Reading -ne '<no reading>') {
      $regressionFailures += '[missing-backup] pre-fix source did not refuse before returning into customInit'
    }
    if ($firstMissingBackup.BackupPointer -ne $missingBackupPointer) {
      $regressionFailures += '[missing-backup] pre-fix source did not leave the missing pointer armed'
    }
    $secondMissingBackup = Invoke-StageLeg -leg 'missing-backup-repeat-pre-fix' -install $installMissingBackup -BackupPointer $firstMissingBackup.BackupPointer
    if ($secondMissingBackup.ExitCode -ne 2 -or $secondMissingBackup.Reading -ne '<no reading>') {
      $regressionFailures += '[missing-backup] pre-fix source did not repeat the refusal on rerun'
    }
    if ($secondMissingBackup.BackupPointer -ne $missingBackupPointer) {
      $regressionFailures += '[missing-backup] the repeated pre-fix refusal did not retain the pointer'
    }

    $installForeignShape = New-FakeInstall 'foreign-stage-shape-pre-fix' -WithAppExe
    $foreignShape = Invoke-StageLeg -leg 'foreign-stage-shape-pre-fix' -install $installForeignShape -BackupPointer 'C:\foreign-backup'
    if ($foreignShape.ExitCode -ne 0 -or $foreignShape.Reading -notmatch 'stage_returned=1 answer=0 probe=1') {
      $regressionFailures += '[foreign-shape] pre-fix source did not fall through into the success path with exit 0'
    }
    if ([string]::IsNullOrEmpty($foreignShape.BackupPointer) -or $foreignShape.BackupPointer -eq 'C:\foreign-backup') {
      $regressionFailures += '[foreign-shape] pre-fix success did not replace the foreign pointer'
    }
    if ((Get-OldSiblings -Root $installForeignShape).Count -ne 1 -or (Test-Path -LiteralPath (Join-Path $installForeignShape 'app.exe'))) {
      $regressionFailures += '[foreign-shape] pre-fix success did not rename the install tree aside'
    }

    if ($regressionFailures.Count -gt 0) {
      throw "pre-fix #1165 red proof was not reproduced: $($regressionFailures -join '; ')"
    }
    Write-Host 'pre-fix #1165 red proofs reproduced: missing-backup refusal repeats; foreign-shape exit 2 is reset to 0 after staging'
    return
  }

  # --- intact-clean: the ordinary update path still stages -------------------
  $installIntact = New-FakeInstall 'intact-clean' -WithAppExe
  $leg = Invoke-StageLeg -leg 'intact-clean' -install $installIntact
  if ($leg.ExitCode -ne 0) { $failures += "[intact-clean] expected exit 0, got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch 'stage_returned=1 answer=0 probe=1') { $failures += "[intact-clean] $($leg.Reading)" }
  $siblings = Get-OldSiblings -Root $installIntact
  if ($siblings.Count -ne 1) {
    $failures += "[intact-clean] expected the install tree to be renamed aside, found $($siblings.Count) .old- sibling(s)"
  } else {
    Write-Host ("[intact-clean] renamed aside: {0}" -f $siblings[0].Name)
    if (-not (Test-Path -LiteralPath (Join-Path $siblings[0].FullName 'resources\python'))) {
      $failures += '[intact-clean] the renamed tree lost its contents'
    }
  }

  # --- first-install: $INSTDIR does not exist yet ---------------------------
  # #1074: the #914 fix round widened the gate to first installs (the call sits
  # outside the app-executable branch, which a fresh install also fails to
  # enter). Nothing runs from a directory that is not there, so this leg pins
  # that a fresh install still proceeds: the gate classifies the absent
  # directory, proves it clean, and the macro skips staging for want of an app
  # executable — it must not refuse, stage, or create anything.
  $installFresh = New-FakeInstall 'first-install' -NoTree
  if (Test-Path -LiteralPath $installFresh) { throw "[first-install] the scratch path must not exist before the leg" }
  $leg = Invoke-StageLeg -leg 'first-install' -install $installFresh
  if ($leg.ExitCode -ne 0) { $failures += "[first-install] expected exit 0, got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch 'stage_returned=1 answer=0 probe=0') { $failures += "[first-install] $($leg.Reading)" }
  if ((Get-OldSiblings -Root $installFresh).Count -ne 0) {
    $failures += '[first-install] a fresh install must not be staged aside'
  }
  if (Test-Path -LiteralPath $installFresh) {
    $failures += '[first-install] the run created the install directory; a fresh install must be left untouched'
  }

  # --- #1165: a missing backup is refused once, then the slot stays clear ----
  # Carry the registry value observed after the first isolated process into the
  # second one. This reproduces what a real rerun sees without sharing any other
  # per-process state.
  $installMissingBackup = New-FakeInstall 'missing-backup-recovery'
  $missingBackupPointer = "$installMissingBackup.old-{00000000-0000-0000-0000-000000000000}"
  $firstMissingBackup = Invoke-StageLeg -leg 'missing-backup-first' -install $installMissingBackup -BackupPointer $missingBackupPointer
  if ($firstMissingBackup.ExitCode -ne 2) { $failures += "[missing-backup-first] expected exit 2, got $($firstMissingBackup.ExitCode)" }
  if ($firstMissingBackup.Reading -ne '<no reading>') { $failures += "[missing-backup-first] refusal continued into customInit: $($firstMissingBackup.Reading)" }
  if (-not [string]::IsNullOrEmpty($firstMissingBackup.BackupPointer)) {
    $failures += '[missing-backup-first] the refused missing path stayed in IaBackupDir'
  }
  $secondMissingBackup = Invoke-StageLeg -leg 'missing-backup-rerun' -install $installMissingBackup -BackupPointer $firstMissingBackup.BackupPointer
  if ($secondMissingBackup.ExitCode -ne 0) { $failures += "[missing-backup-rerun] expected the no-pointer path to proceed, got exit $($secondMissingBackup.ExitCode)" }
  if ($secondMissingBackup.Reading -notmatch 'recovery_pending= stage_returned=1 answer=0 probe=0') {
    $failures += "[missing-backup-rerun] expected recovery selection to stay closed and the process gate to run before staging: $($secondMissingBackup.Reading)"
  }
  if (-not [string]::IsNullOrEmpty($secondMissingBackup.BackupPointer)) {
    $failures += '[missing-backup-rerun] the no-pointer damaged-install path armed a backup pointer'
  }

  # --- #1165: a foreign staging shape must not fall through to success -------
  $installForeignShape = New-FakeInstall 'foreign-stage-shape' -WithAppExe
  $leg = Invoke-StageLeg -leg 'foreign-stage-shape' -install $installForeignShape -BackupPointer 'C:\foreign-backup'
  if ($leg.ExitCode -ne 2) { $failures += "[foreign-stage-shape] expected exit 2, got $($leg.ExitCode)" }
  if ($leg.Reading -ne '<no reading>') { $failures += "[foreign-stage-shape] the refusal reached the success path: $($leg.Reading)" }
  if (-not [string]::IsNullOrEmpty($leg.BackupPointer)) {
    $failures += '[foreign-stage-shape] the foreign pointer stayed in IaBackupDir'
  }
  if ((Get-OldSiblings -Root $installForeignShape).Count -ne 0) {
    $failures += '[foreign-stage-shape] the refusal renamed the live install aside'
  }
  if (-not (Test-Path -LiteralPath (Join-Path $installForeignShape 'app.exe'))) {
    $failures += '[foreign-stage-shape] the refusal changed the live install tree'
  }

  # --- damaged-runtime: exe gone, the bundled service is what is running -----
  $installRuntime = New-FakeInstall 'damaged-runtime'
  $runtimeExe = Join-Path $installRuntime 'resources\python\ia-probe-runtime.exe'
  Copy-Item -LiteralPath $nodeExe -Destination $runtimeExe
  $runtime = Start-Process -FilePath $runtimeExe -ArgumentList @('-e', 'setTimeout(()=>{},120000)') -PassThru
  $started += $runtime.Id
  $runtimePath = (Get-Process -Id $runtime.Id -ErrorAction SilentlyContinue).Path
  if ($runtimePath -ne $runtimeExe) { throw "[damaged-runtime] probe process image path is '$runtimePath', expected '$runtimeExe'" }
  $leg = Invoke-StageLeg -leg 'damaged-runtime' -install $installRuntime
  if ($leg.ExitCode -ne 0) { $failures += "[damaged-runtime] expected exit 0, got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch 'stage_returned=1 answer=0 probe=0') { $failures += "[damaged-runtime] $($leg.Reading)" }
  if (-not (Wait-ProcessGone -ProcessId $runtime.Id)) {
    $failures += "[damaged-runtime] the process $($runtime.Id) survived the gate's stop"
  } else {
    Write-Host ("[damaged-runtime] probe process {0} after the gate: gone" -f $runtime.Id)
  }
  if ((Get-OldSiblings -Root $installRuntime).Count -ne 0) {
    $failures += '[damaged-runtime] the tree was renamed aside although the app executable is absent'
  }
  if (-not (Test-Path -LiteralPath (Join-Path $installRuntime 'resources\python'))) {
    $failures += '[damaged-runtime] the install tree was modified by the gate'
  }

  # --- damaged-mixed: the bundled runtime AND a client image both running ----
  # Answer 12 ("the runtime plus anything else"): the gate must abort and stop
  # nothing. This is the only leg that can tell 12 from 11 — a classifier that
  # collapses the two still refuses here, but it stops the bundled runtime on the
  # way, which is the one thing a refusal must not do (#1074 review Axis A).
  $installMixed = New-FakeInstall 'damaged-mixed'
  $mixedRuntimeExe = Join-Path $installMixed 'resources\python\ia-probe-runtime.exe'
  Copy-Item -LiteralPath $nodeExe -Destination $mixedRuntimeExe
  $mixedClientExe = Join-Path $installMixed 'ia-probe-client.exe'
  Copy-Item -LiteralPath $nodeExe -Destination $mixedClientExe
  $mixedRuntime = Start-Process -FilePath $mixedRuntimeExe -ArgumentList @('-e', 'setTimeout(()=>{},120000)') -PassThru
  $started += $mixedRuntime.Id
  $mixedClient = Start-Process -FilePath $mixedClientExe -ArgumentList @('-e', 'setTimeout(()=>{},120000)') -PassThru
  $started += $mixedClient.Id
  foreach ($case in @(@($mixedRuntime, $mixedRuntimeExe), @($mixedClient, $mixedClientExe))) {
    $probePath = (Get-Process -Id $case[0].Id -ErrorAction SilentlyContinue).Path
    if ($probePath -ne $case[1]) { throw "[damaged-mixed] probe process image path is '$probePath', expected '$($case[1])'" }
  }
  $leg = Invoke-StageLeg -leg 'damaged-mixed' -install $installMixed
  if ($leg.ExitCode -ne 2) { $failures += "[damaged-mixed] expected exit 2 (the gate's refusal), got $($leg.ExitCode)" }
  if ($leg.Reading -ne '<no reading>') { $failures += "[damaged-mixed] the gate returned instead of refusing: $($leg.Reading)" }
  if (-not (Test-ProcessAlive -ProcessId $mixedClient.Id)) {
    $failures += "[damaged-mixed] the client process $($mixedClient.Id) is gone but the gate had to leave it running"
  }
  if (-not (Test-ProcessAlive -ProcessId $mixedRuntime.Id)) {
    $failures += "[damaged-mixed] the bundled runtime $($mixedRuntime.Id) was stopped; with a client up the answer is 12 and a refusal stops nothing"
  } else {
    Write-Host ("[damaged-mixed] both processes after the refusal: alive (client {0}, runtime {1})" -f $mixedClient.Id, $mixedRuntime.Id)
  }
  if ((Get-OldSiblings -Root $installMixed).Count -ne 0) {
    $failures += '[damaged-mixed] the refusal moved the install tree aside'
  }
  if (-not (Test-Path -LiteralPath $mixedRuntimeExe)) { $failures += '[damaged-mixed] the refusal modified the install tree' }

  # --- damaged-rootproc: exe gone, a live image in the install root ---------
  $installRoot = New-FakeInstall 'damaged-rootproc'
  $serviceExe = Join-Path $installRoot 'ia-probe-service.exe'
  Copy-Item -LiteralPath $nodeExe -Destination $serviceExe
  $service = Start-Process -FilePath $serviceExe -ArgumentList @('-e', 'setTimeout(()=>{},120000)') -PassThru
  $started += $service.Id
  $servicePath = (Get-Process -Id $service.Id -ErrorAction SilentlyContinue).Path
  if ($servicePath -ne $serviceExe) { throw "[damaged-rootproc] probe process image path is '$servicePath', expected '$serviceExe'" }
  $leg = Invoke-StageLeg -leg 'damaged-rootproc' -install $installRoot
  if ($leg.ExitCode -ne 2) { $failures += "[damaged-rootproc] expected exit 2 (the gate's refusal), got $($leg.ExitCode)" }
  if ($leg.Reading -ne '<no reading>') { $failures += "[damaged-rootproc] the gate returned instead of refusing: $($leg.Reading)" }
  if (-not (Test-ProcessAlive -ProcessId $service.Id)) {
    $failures += "[damaged-rootproc] the process $($service.Id) is gone but the gate had to leave it running"
  } else {
    Write-Host ("[damaged-rootproc] probe process {0} after the refusal: alive" -f $service.Id)
  }
  if ((Get-OldSiblings -Root $installRoot).Count -ne 0) {
    $failures += '[damaged-rootproc] the refusal moved the install tree aside'
  }
  if (-not (Test-Path -LiteralPath $serviceExe)) { $failures += '[damaged-rootproc] the refusal modified the install tree' }

  # --- damaged-unknown: exe gone, an install path the query cannot parse ----
  # The apostrophe closes the PowerShell string the gate builds, so the
  # interpreter exits 1 and 1 is deliberately not one of the gate's answers. The
  # gate must refuse (fail-closed); before the fix it was never called at all on
  # a tree without the app executable, so this leg returned 0.
  $installUnknown = New-FakeInstall "damaged-unknown-Bob's App"
  $leg = Invoke-StageLeg -leg 'damaged-unknown' -install $installUnknown
  if ($leg.ExitCode -ne 2) { $failures += "[damaged-unknown] expected exit 2, got $($leg.ExitCode)" }
  if ($leg.Reading -ne '<no reading>') { $failures += "[damaged-unknown] the gate returned on an answer it cannot prove clean: $($leg.Reading)" }
  if ((Get-OldSiblings -Root $installUnknown).Count -ne 0) {
    $failures += '[damaged-unknown] the refusal moved the install tree aside'
  }
  # --- window-runtime: the bundled service starts after the gate returned -----
  $installWindowRuntime = New-FakeInstall 'window-runtime' -WithAppExe
  $windowRuntimeExe = Join-Path $installWindowRuntime 'resources\python\ia-probe-runtime.exe'
  Copy-Item -LiteralPath $nodeExe -Destination $windowRuntimeExe
  $leg = Invoke-StageLeg -leg 'window-runtime' -install $installWindowRuntime -HoldWindow -WindowExe $windowRuntimeExe
  if ($leg.ExitCode -ne 0) { $failures += "[window-runtime] expected exit 0 (the re-check stops the bundled runtime and the update proceeds), got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch 'stage_returned=1 answer=0 probe=1') { $failures += "[window-runtime] the update did not complete: $($leg.Reading)" }
  if ($null -eq $leg.WindowProcess) {
    $failures += '[window-runtime] the leg never started its process inside the window'
  } elseif (-not (Wait-ProcessGone -ProcessId $leg.WindowProcess.Id)) {
    $failures += "[window-runtime] the service $($leg.WindowProcess.Id) started inside the window survived the update"
  } else {
    Write-Host ("[window-runtime] service {0} started inside the window: stopped by the re-check" -f $leg.WindowProcess.Id)
  }
  if ((Get-OldSiblings -Root $installWindowRuntime).Count -ne 1) {
    $failures += '[window-runtime] the update did not stage the install tree aside'
  }

  # --- window-client: a client image starts after the gate returned -----------
  $installWindowClient = New-FakeInstall 'window-client' -WithAppExe
  $windowClientExe = Join-Path $installWindowClient 'ia-probe-client.exe'
  Copy-Item -LiteralPath $nodeExe -Destination $windowClientExe
  $leg = Invoke-StageLeg -leg 'window-client' -install $installWindowClient -HoldWindow -WindowExe $windowClientExe
  if ($leg.ExitCode -ne 2) { $failures += "[window-client] expected exit 2 (the re-check's refusal), got $($leg.ExitCode)" }
  if ($leg.Reading -ne '<no reading>') { $failures += "[window-client] the re-check returned instead of refusing: $($leg.Reading)" }
  if ($null -eq $leg.WindowProcess) {
    $failures += '[window-client] the leg never started its process inside the window'
  } elseif (-not (Test-ProcessAlive -ProcessId $leg.WindowProcess.Id)) {
    $failures += "[window-client] the client $($leg.WindowProcess.Id) was killed; a client in the install root is never stopped"
  } else {
    Write-Host ("[window-client] client {0} after the refusal: alive" -f $leg.WindowProcess.Id)
  }
  if ((Get-OldSiblings -Root $installWindowClient).Count -ne 0) {
    $failures += '[window-client] the refusal moved the install tree aside'
  }
  if (-not (Test-Path -LiteralPath (Join-Path $installWindowClient 'app.exe'))) {
    $failures += '[window-client] the refusal modified the install tree'
  }
} finally {
  foreach ($processId in $started) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $probeRegistryKey -Recurse -Force -ErrorAction SilentlyContinue
  if (-not $Keep) {
    Get-ChildItem -LiteralPath $scratchRoot -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -like "*$runId*" } |
      Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host 'scratch tree removed (pass -Keep to retain it)'
  } else {
    Write-Host "scratch retained at $scratchRoot (runId $runId)"
  }
}

if ($failures.Count -gt 0) {
  foreach ($failure in $failures) { Write-Host "FAIL $failure" }
  throw "$($failures.Count) stage-damaged leg assertion(s) failed"
}
Write-Host 'stage-damaged legs OK: intact-clean (staged), first-install (clean, nothing staged), missing-backup (refused once, then no-pointer path), foreign-shape (refused before success), damaged-runtime (stopped), damaged-mixed (refused, both alive), damaged-rootproc (refused, alive), damaged-unknown (refused), window-runtime (stopped by the re-check), window-client (refused, alive)'
