# Compile and run the production #914 process gate against real Windows processes.
#
# The gate (iaRequireNoRunningProduct / iaClassifyRunningProduct in
# installer-directories.nsh) is the guard that decides whether an update may move
# the installation aside. Its two measured premises are that the stock
# CHECK_APP_RUNNING predicate cannot see a single matching process (`(...).Count`
# on one CimInstance is $null) and that the atomic rename does not stop a
# headless process — see docs/live_gate/w21/58-issue-914-service-in-use-gate.md
# §1. This driver pins the gate's own behaviour on the machine:
#
#   none    empty install tree                     -> answer 0, gate returns
#   runtime a headless process in resources\python -> answer 11, gate stops it, returns
#   client  a live process in the install root     -> answer 12, gate aborts, process alive
#   unknown an install path the query cannot parse -> not a proven answer, gate aborts
#
# The probe also records the stock predicate's reading on the same state, which
# is the contrast the ticket is about (stock reads the one-process case as "not
# running" — 1 — while the gate answers 11).
#
# Usage (from desktop/): powershell -File test/harness/r1-drivers/run-process-gate-probe.ps1 [-Keep]
param([switch]$Keep)
$ErrorActionPreference = 'Stop'

$harness = Split-Path -Parent $MyInvocation.MyCommand.Path
$installerDir = (Resolve-Path (Join-Path $harness '..\..\..\installer')).Path
$scratchRoot = [System.IO.Path]::GetFullPath((Join-Path $env:TEMP 'ia-r1-drivers'))
$runId = [Guid]::NewGuid().ToString('N')

$makensis = @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'electron-builder\Cache\nsis-3.0.4.1') -Recurse -Filter 'makensis.exe' -ErrorAction SilentlyContinue)[0].FullName
if ([string]::IsNullOrEmpty($makensis)) { throw 'makensis.exe not found in the electron-builder cache' }

$nodeExe = (Get-Command node).Source
if ([string]::IsNullOrEmpty($nodeExe)) { throw 'node.exe not found' }

# --- production text, taken from the shipped include -------------------------
$probeTemplatePath = Join-Path $harness 'process-gate-probe.nsi'
$probeTemplate = [System.IO.File]::ReadAllText($probeTemplatePath, [System.Text.Encoding]::UTF8)
$classifyMarker = '@@PRODUCTION_IA_CLASSIFY@@'
$requireMarker = '@@PRODUCTION_IA_REQUIRE@@'
foreach ($marker in @($classifyMarker, $requireMarker)) {
  if ([regex]::Matches($probeTemplate, [regex]::Escape($marker)).Count -ne 1) {
    throw "expected exactly one marker $marker in $probeTemplatePath"
  }
}
$directoriesPath = Join-Path $installerDir 'installer-directories.nsh'
$directoriesSource = [System.IO.File]::ReadAllText($directoriesPath, [System.Text.Encoding]::UTF8)
$extractFunction = {
  param([string]$source, [string]$name)
  $startToken = "Function $name"
  $endToken = 'FunctionEnd'
  $start = $source.IndexOf($startToken, [System.StringComparison]::Ordinal)
  if ($start -lt 0) { throw "production function not found: $name" }
  $end = $source.IndexOf($endToken, $start + $startToken.Length, [System.StringComparison]::Ordinal)
  if ($end -lt 0) { throw "production function has no FunctionEnd: $name" }
  return $source.Substring($start, $end + $endToken.Length - $start)
}
$classifySource = & $extractFunction $directoriesSource 'iaClassifyRunningProduct'
$requireSource = & $extractFunction $directoriesSource 'iaRequireNoRunningProduct'
$probeSource = $probeTemplate.Replace($classifyMarker, $classifySource).Replace($requireMarker, $requireSource)
if ($probeSource.Contains($classifyMarker) -or $probeSource.Contains($requireMarker)) {
  throw 'production function injection failed'
}
if (-not $probeSource.Contains('@(Get-CimInstance')) {
  throw 'the injected production classification lost its array cast — the probe would test an older text'
}

New-Item -ItemType Directory -Force -Path $scratchRoot | Out-Null

function New-FakeInstall {
  param([string]$leg)
  $root = Join-Path $scratchRoot ("gate-$leg-$runId")
  if (-not [System.IO.Path]::GetFullPath($root).StartsWith($scratchRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "scratch path escapes the test root: $root"
  }
  New-Item -ItemType Directory -Force -Path (Join-Path $root 'resources\python') | Out-Null
  return $root
}

function Start-ProbeProcess {
  param([string]$exePath, [string[]]$arguments)
  if ($arguments.Count -gt 0) {
    return Start-Process -FilePath $exePath -ArgumentList $arguments -PassThru
  }
  return Start-Process -FilePath $exePath -PassThru
}

function Get-ProcessImagePath {
  param([int]$processId)
  return (Get-Process -Id $processId -ErrorAction SilentlyContinue).Path
}

function Get-ProcessState {
  param([int]$processId)
  if ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { return 'alive' }
  return 'gone'
}

function Assert-Alive {
  param([int]$processId, [string]$leg)
  if ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
    throw "[$leg] the process $processId is gone but the gate had to leave it running"
  }
}

function Assert-Gone {
  param([int]$processId, [string]$leg)
  $deadline = (Get-Date).AddSeconds(10)
  while ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
    if ((Get-Date) -gt $deadline) { throw "[$leg] the process $processId survived the gate's stop" }
    Start-Sleep -Milliseconds 100
  }
}

function Invoke-GateLeg {
  param(
    [string]$leg,
    [string]$install,
    [string]$expected
  )
  $buildDir = Join-Path $scratchRoot ("gate-$leg-build-$runId")
  $result = Join-Path $scratchRoot ("gate-$leg-result-$runId.txt")
  New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
  $script = Join-Path $buildDir 'process-gate-probe.nsi'
  $exe = Join-Path $buildDir 'process-gate-probe.exe'
  # The injected production text carries non-ASCII comment characters (—, ×, …),
  # and makensis reads a BOM-less UTF-8 script as ANSI ("Bad text encoding"), so
  # this file is written with a BOM (the fileexists driver injects ASCII only and
  # does not need one).
  [System.IO.File]::WriteAllText($script, $probeSource, [System.Text.UTF8Encoding]::new($true))
  & $makensis /V2 "/DPROBE_INSTALL=$install" "/DPROBE_LEG=$leg" "/DPROBE_RESULT=$result" $script
  if ($LASTEXITCODE -ne 0) { throw "[$leg] makensis failed with exit code $LASTEXITCODE" }
  if (-not (Test-Path -LiteralPath $exe)) { throw "[$leg] probe exe missing: $exe" }

  Remove-Item -LiteralPath $result -ErrorAction SilentlyContinue
  $process = Start-Process -FilePath $exe -Wait -PassThru
  $exitCode = $process.ExitCode
  $reading = if (Test-Path -LiteralPath $result) { (Get-Content -LiteralPath $result -Raw).Trim() } else { '<no reading>' }
  Write-Host ("[{0}] exit={1} {2}" -f $leg, $exitCode, $reading)
  return [pscustomobject]@{ Leg = $leg; ExitCode = $exitCode; Reading = $reading; Expected = $expected; BuildDir = $buildDir; Result = $result }
}

$started = @()
$failures = @()
try {
  # --- none: nothing runs from the tree -------------------------------------
  $installNone = New-FakeInstall 'none'
  $leg = Invoke-GateLeg -leg 'none' -install $installNone -expected 'leg=none answer_before=0 stock=1 gate_returned=1 answer_after=0'
  if ($leg.ExitCode -ne 0) { $failures += "[none] expected exit 0, got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch 'answer_before=0 .*gate_returned=1 answer_after=0') { $failures += "[none] $($leg.Reading)" }

  # --- runtime: the headless service analogue --------------------------------
  $installRuntime = New-FakeInstall 'runtime'
  $runtimeExe = Join-Path $installRuntime 'resources\python\ia-probe-runtime.exe'
  Copy-Item -LiteralPath $nodeExe -Destination $runtimeExe
  $runtime = Start-ProbeProcess -exePath $runtimeExe -arguments @('-e', 'setTimeout(()=>{},120000)')
  $started += $runtime.Id
  $runtimePath = Get-ProcessImagePath -processId $runtime.Id
  if ($runtimePath -ne $runtimeExe) { throw "[runtime] probe process image path is '$runtimePath', expected '$runtimeExe'" }
  $leg = Invoke-GateLeg -leg 'runtime' -install $installRuntime -expected 'leg=runtime answer_before=11 stock=1 gate_returned=1 answer_after=0'
  if ($leg.ExitCode -ne 0) { $failures += "[runtime] expected exit 0, got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch 'answer_before=11 .*gate_returned=1 answer_after=0') {
    $failures += "[runtime] $($leg.Reading)"
  }
  # The stock predicate's own reading on that same state: the one-process case it
  # cannot see. This is the reproduced root cause, not a citation.
  if ($leg.Reading -notmatch 'stock=1') { $failures += "[runtime] the stock predicate did not reproduce its blind reading: $($leg.Reading)" }
  Assert-Gone -processId $runtime.Id -leg 'runtime'
  Write-Host ("[runtime] probe process {0} after the gate: {1}" -f $runtime.Id, (Get-ProcessState -processId $runtime.Id))

  # --- client: a live process in the install root, outside resources\python ----
  # The classifier keys on the path position, which is what the desktop client
  # occupies on a real install; the leg reuses the same long-lived image as the
  # runtime leg so the reading is deterministic (a notepad copy exits on its own
  # on this host — measured — and a GUI image is not what the gate inspects).
  $installClient = New-FakeInstall 'client'
  $clientExe = Join-Path $installClient 'ia-probe-client.exe'
  Copy-Item -LiteralPath $nodeExe -Destination $clientExe
  $client = Start-ProbeProcess -exePath $clientExe -arguments @('-e', 'setTimeout(()=>{},120000)')
  $started += $client.Id
  $clientPath = Get-ProcessImagePath -processId $client.Id
  if ($clientPath -ne $clientExe) { throw "[client] probe process image path is '$clientPath', expected '$clientExe'" }
  $leg = Invoke-GateLeg -leg 'client' -install $installClient -expected 'leg=client answer_before=12 gate_returned=0'
  if ($leg.ExitCode -ne 2) { $failures += "[client] expected exit 2 (the gate's refusal), got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch 'answer_before=12') { $failures += "[client] $($leg.Reading)" }
  if ($leg.Reading -notmatch 'gate_returned=0') { $failures += "[client] the gate returned instead of refusing: $($leg.Reading)" }
  Assert-Alive -processId $client.Id -leg 'client'
  Write-Host ("[client] probe process {0} after the refusal: {1}" -f $client.Id, (Get-ProcessState -processId $client.Id))
  if (-not (Test-Path -LiteralPath $clientExe)) { $failures += '[client] the install tree was modified by the refusal' }
  if (-not (Test-Path -LiteralPath $installClient)) { $failures += '[client] the install tree disappeared' }

  # --- unknown: an install path the query cannot parse -----------------------
  # The path carries an apostrophe, which closes the PowerShell string the gate
  # builds: the interpreter exits 1, and 1 is deliberately not one of the gate's
  # answers. The gate must refuse rather than treat it as clean (fail-closed),
  # where the stock predicate reads the same failure as "not running".
  $installUnknown = New-FakeInstall "unknown-Bob's App"
  $leg = Invoke-GateLeg -leg 'unknown' -install $installUnknown -expected 'leg=unknown answer_before=<not 0/11/12> gate_returned=0'
  if ($leg.ExitCode -ne 2) { $failures += "[unknown] expected exit 2, got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch 'gate_returned=0') { $failures += "[unknown] the gate returned on an answer it cannot prove clean: $($leg.Reading)" }
  if ($leg.Reading -match 'answer_before=(0|11|12)\b') {
    $failures += "[unknown] the unreadable answer was classified as $($Matches[1]) — the quoting premise did not hold: $($leg.Reading)"
  }
} finally {
  foreach ($processId in $started) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
  }
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
  throw "$($failures.Count) process-gate leg assertion(s) failed"
}
Write-Host 'process gate legs OK: none, runtime (stopped), client (refused, alive), unknown (refused)'
