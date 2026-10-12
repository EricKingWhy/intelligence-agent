# Compile and run the PRODUCTION iaStageApplication macro against a damaged
# installation (#914 review Axis A — the user-ruled must-fix).
#
# The Axis A defect was a placement defect, not a logic defect: the gate call sat
# INSIDE the `${If} $iaProbeFound == "1"` branch, and that branch skips staging
# entirely when the app executable is gone. A damaged installation (exe
# quarantined or deleted by hand) still holds the live bundled service, and the
# stock section then installed straight over it — #914's symptom on an edge path.
#
# run-process-gate-probe.ps1 drives the gate FUNCTION, so it cannot see where the
# call sits. This driver compiles the real macro (taken verbatim from
# installer-directories.nsh, with iaProbePath and the two cleanup helpers it
# calls) and drives four legs:
#
#   intact-clean     app.exe present, nothing running   -> gate returns, staging
#                    proceeds: the tree is renamed to "<dir>.old-{guid}"
#   damaged-runtime  no app.exe, headless image under resources\python running
#                    -> the gate runs anyway, stops it, and returns (no staging)
#   damaged-rootproc no app.exe, live image in the install root -> the gate runs
#                    anyway and aborts (exit 2), the process stays alive
#   damaged-unknown  no app.exe, install path the query cannot parse -> the gate
#                    runs anyway and aborts (exit 2) instead of reading the
#                    failure as clean
#
# The last three are the discriminating legs: before the fix the macro never
# called the gate on any of them (exit 0, service untouched).
#
# Usage (from desktop/): powershell -File test/harness/r1-drivers/run-stage-damaged-probe.ps1 [-Keep]
#   -DirectoriesSource <path>  drive the legs against another revision of
#                              installer-directories.nsh. Used for the red proof:
#                              the same legs must FAIL on the pre-fix macro (the
#                              three damaged legs), which is what shows they
#                              discriminate rather than merely pass.
param([switch]$Keep, [string]$DirectoriesSource)
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
$backupMacros = (Get-ProductionBlock $directoriesSource '!macro iaReadBackupDir' '!macroend' 'iaReadBackupDir') +
  "`r`n`r`n" +
  (Get-ProductionBlock $directoriesSource '!macro iaStashBackupDir' '!macroend' 'iaStashBackupDir')
$replacements = [ordered]@{
  '@@PRODUCTION_IA_CLEANUP@@'       = $cleanupSource
  '@@PRODUCTION_IA_PROBE_PATH@@'    = Get-ProductionBlock $directoriesSource 'Function iaProbePath' 'FunctionEnd' 'iaProbePath'
  '@@PRODUCTION_IA_BACKUP_MACROS@@' = $backupMacros
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
  param([string]$Name, [switch]$WithAppExe)
  $root = Join-Path $scratchRoot ("stage-$Name-$runId")
  if (-not [System.IO.Path]::GetFullPath($root).StartsWith($scratchRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "scratch path escapes the test root: $root"
  }
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

function Invoke-StageLeg {
  param([string]$Leg, [string]$Install)
  $buildDir = Join-Path $scratchRoot ("stage-$Leg-build-$runId")
  $result = Join-Path $scratchRoot ("stage-$Leg-result-$runId.txt")
  New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
  $script = Join-Path $buildDir 'stage-damaged-probe.nsi'
  $exe = Join-Path $buildDir 'stage-damaged-probe.exe'
  # The injected production text carries non-ASCII comment characters (—, ×, …),
  # and makensis reads a BOM-less UTF-8 script as ANSI ("Bad text encoding"), so
  # this file is written with a BOM.
  [System.IO.File]::WriteAllText($script, $probeSource, [System.Text.UTF8Encoding]::new($true))
  & $makensis /V2 "/DPROBE_INSTALL=$Install" "/DPROBE_LEG=$Leg" "/DPROBE_RESULT=$result" $script
  if ($LASTEXITCODE -ne 0) { throw "[$Leg] makensis failed with exit code $LASTEXITCODE" }
  if (-not (Test-Path -LiteralPath $exe)) { throw "[$Leg] probe exe missing: $exe" }

  Remove-Item -LiteralPath $result -ErrorAction SilentlyContinue
  $process = Start-Process -FilePath $exe -Wait -PassThru
  $exitCode = $process.ExitCode
  $reading = if (Test-Path -LiteralPath $result) { (Get-Content -LiteralPath $result -Raw).Trim() } else { '<no reading>' }
  Write-Host ("[{0}] exit={1} {2}" -f $Leg, $exitCode, $reading)
  return [pscustomobject]@{ Leg = $Leg; ExitCode = $exitCode; Reading = $reading; Install = $Install }
}

$started = @()
$failures = @()
try {
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
Write-Host 'stage-damaged legs OK: intact-clean (staged), damaged-runtime (stopped), damaged-rootproc (refused, alive), damaged-unknown (refused)'
