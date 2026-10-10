# #919 Q9: run the runtime-limit and path-form legs the leftover sweep's
# fail-closed design rests on, and print the readings. limits-probe.nsi does
# the measuring; this script builds the fixtures, compiles it once per
# mode/depth and reports.
#
#   readings (one run, fresh file)
#     1  FindFirst through "\\?\<dir>" past the 327-char boundary the leftover
#        probe measured (~400 / ~800 / ~1600 / ~3000 characters)
#     2  FindFirst and `RMDir /r` on the plain UNC and the "\\?\UNC\<share>"
#        form of two real sibling trees, side by side
#   mimic (one run per depth in the ladder, one file each)
#     3  the scan's own load (4 pushes + one Call per level) recursed to the
#        depth; each run is classified by its exit code, because a run whose
#        stack dies cannot write its line afterwards (the result file is
#        already open, so the crashed rung reads back empty). 512 is the valve
#        in iaScanReparsePointsBody; the higher rungs bracket the stack's own
#        limit, so the ladder says how much headroom the valve has.
#
# Scratch state lives under %TEMP%\ia-limits-probe-* and is removed at the end
# (-Keep retains it). The long trees are deleted through their "\\?\" form.
#
# Usage (from this directory); makensis is auto-discovered from the
# electron-builder cache like ../run-probes.ps1:
#   powershell -File run-limits-probe.ps1 [-Makensis <path>] [-Keep]
param(
  [string]$Makensis = '',
  [switch]$Keep
)
$ErrorActionPreference = 'Stop'

# Native tools write to stderr on failure, and under PowerShell 5.1 a
# redirected stderr line becomes a terminating error while the preference is
# 'Stop'; run native tools with 'Continue' and read the exit code instead.
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

if ($Makensis -eq '') {
  $candidates = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'electron-builder\Cache') -Recurse -Filter makensis.exe -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending
  if ($candidates.Count -eq 0) { throw 'makensis.exe not found; pass -Makensis <path>' }
  $Makensis = $candidates[0].FullName
}
if (-not (Test-Path -LiteralPath $Makensis)) { throw "makensis not found at $Makensis" }
Write-Host "makensis: $Makensis"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Join-Path $env:TEMP ('ia-limits-probe-' + [guid]::NewGuid().ToString('N'))
$build = Join-Path $root 'build'
New-Item -ItemType Directory -Force -Path $build | Out-Null
Copy-Item -LiteralPath (Join-Path $here 'limits-probe.nsi') -Destination $build -Force
$probeExe = Join-Path $build 'limits-probe.exe'

# --- leg 1 fixtures: trees past the 327-char reading ------------------------
function New-LongTree([string]$base, [int]$targetLength) {
  $segment = 'seg-' + ('0123456789abcdef' * 2)
  $path = $base
  while ($path.Length -lt $targetLength) {
    $path = $path + '\' + $segment
    [System.IO.Directory]::CreateDirectory('\\?\' + $path) | Out-Null
  }
  [System.IO.File]::WriteAllText('\\?\' + (Join-Path $path 'leaf.txt'), 'leaf')
  return $path
}
$long1 = New-LongTree (Join-Path $root 'long1') 400
$long2 = New-LongTree (Join-Path $root 'long2') 800
$long3 = New-LongTree (Join-Path $root 'long3') 1600
$long4 = New-LongTree (Join-Path $root 'long4') 3000
Write-Host ("long trees: {0} {1} {2} {3}" -f $long1.Length, $long2.Length, $long3.Length, $long4.Length)

# --- leg 2 fixtures: two sibling targets reached through the admin share ----
$uncPlain = Join-Path $root 'unc-plain'
$uncLong = Join-Path $root 'unc-long'
foreach ($target in $uncPlain, $uncLong) {
  New-Item -ItemType Directory -Force -Path (Join-Path $target 'sub') | Out-Null
  Set-Content -LiteralPath (Join-Path $target 'inner.txt') -Value 'inner payload' -Encoding ASCII
  Set-Content -LiteralPath (Join-Path $target 'sub\deep.txt') -Value 'deep payload' -Encoding ASCII
}
$drive = $root.Substring(0, 1)
$tailPlain = $root.Substring(2) + '\unc-plain'
$tailLong = $root.Substring(2) + '\unc-long'
Write-Host ("unc plain form: \\localhost\{0}`${1}" -f $drive, $tailPlain)
Write-Host ("unc long form : \\?\UNC\localhost\{0}`${1}" -f $drive, $tailLong)

$commonDefines = @(
  "/DPROBE_DRIVE=$drive",
  "/DPROBE_UNC_TAIL_PLAIN=$tailPlain",
  "/DPROBE_UNC_TAIL_LONG=$tailLong"
)

function Invoke-Compile([string[]]$Defines) {
  $built = Invoke-Native -FilePath $Makensis -Arguments (@('/V2') + $Defines + @((Join-Path $build 'limits-probe.nsi')))
  if ($built.ExitCode -ne 0) {
    $built.Text | ForEach-Object { Write-Host "    $_" }
    throw "probe did not compile: $($Defines -join ' ')"
  }
}

# A crashed probe exits on its own; the bound is only a guard against a hung
# dialog leaving a process behind.
function Invoke-ProbeRun {
  $process = Start-Process -FilePath $probeExe -PassThru
  if (-not $process.WaitForExit(180000)) {
    $process.Kill()
    return [pscustomobject]@{ ExitCode = $null; TimedOut = $true }
  }
  return [pscustomobject]@{ ExitCode = $process.ExitCode; TimedOut = $false }
}

# --- readings run -----------------------------------------------------------
$reading = Join-Path $root 'reading.txt'
Invoke-Compile (@(
  '/DPROBE_MODE=readings',
  "/DPROBE_RESULT=$reading",
  "/DPROBE_LONG1=$long1",
  "/DPROBE_LONG2=$long2",
  "/DPROBE_LONG3=$long3",
  "/DPROBE_LONG4=$long4"
) + $commonDefines)
Write-Host 'compile (readings) ok'
$readingsRun = Invoke-ProbeRun
Write-Host ("readings run exit = {0} timeout = {1}" -f $readingsRun.ExitCode, $readingsRun.TimedOut)
if (Test-Path -LiteralPath $reading) {
  Write-Host '--- readings ---'
  Get-Content -LiteralPath $reading | ForEach-Object { Write-Host "  $_" }
} else {
  Write-Host 'NOTHING WRITTEN: the readings run left no file'
}
Write-Host ("ps_check unc_plain_dir_exists={0}" -f (Test-Path -LiteralPath $uncPlain))
Write-Host ("ps_check unc_long_dir_exists={0}" -f (Test-Path -LiteralPath $uncLong))
Write-Host ("ps_check long4_exists={0} len={1}" -f (Test-Path -LiteralPath ('\\?\' + $long4)), $long4.Length)

# --- mimic ladder: one run per depth ---------------------------------------
$ladder = @(
  @{ Depth = 512;  Expect = 'completed' },
  @{ Depth = 1024; Expect = 'completed' },
  @{ Depth = 1300; Expect = 'completed' },
  @{ Depth = 1400; Expect = 'stack-overflow' }
)
$mismatch = $false
Write-Host '--- mimic ladder ---'
foreach ($step in $ladder) {
  $depthReading = Join-Path $root ('mimic-{0}.txt' -f $step.Depth)
  Invoke-Compile (@(
    '/DPROBE_MODE=mimic',
    "/DPROBE_RESULT=$depthReading",
    "/DPROBE_CALL_MAX=$($step.Depth)"
  ) + $commonDefines)
  $run = Invoke-ProbeRun
  if ($run.TimedOut) {
    $actual = 'timeout'
  } elseif ($run.ExitCode -eq 0) {
    $actual = 'completed'
  } elseif ($run.ExitCode -eq -1073741571) {
    $actual = 'stack-overflow'
  } else {
    $actual = "exit-$($run.ExitCode)"
  }
  $text = '(no file)'
  if (Test-Path -LiteralPath $depthReading) {
    $text = (Get-Content -LiteralPath $depthReading) -join ' '
  }
  $verdict = 'FAIL'
  if ($actual -ne $step.Expect) {
    $mismatch = $true
  } elseif ($step.Expect -eq 'completed') {
    # #919 review (F4): exit 0 alone would also pass a recursion that returned
    # early, and the rung only measures the real per-level load (4 pushes + one
    # Call) when the ladder ran to the depth. The probe prints the depth it
    # actually reached, so a completed rung has to show it.
    if ($text -match ("mimic_completed=" + $step.Depth + "(?!\d)")) {
      $verdict = 'PASS'
    } else {
      $mismatch = $true
    }
  } else {
    $verdict = 'PASS'
  }
  Write-Host ("mimic_check depth={0} exit={1} expected={2} actual={3} {4} reading=[{5}]" -f $step.Depth, $run.ExitCode, $step.Expect, $actual, $verdict, $text)
}

if (-not $Keep) {
  foreach ($path in $long4, $long3, $long2, $long1, $uncPlain, $uncLong) {
    if (Test-Path -LiteralPath ('\\?\' + $path)) {
      try { [System.IO.Directory]::Delete('\\?\' + $path, $true) } catch { Write-Host ("cleanup FAILED for {0}: {1}" -f $path, $_.Exception.Message) }
    }
  }
  Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
  Write-Host 'scratch tree removed (pass -Keep to retain it)'
}

if ($mismatch) {
  Write-Host 'VERDICT: mimic ladder did not match expectations -- stop and attribute before using these readings'
  exit 1
}
Write-Host 'VERDICT: mimic ladder matched expectations'
