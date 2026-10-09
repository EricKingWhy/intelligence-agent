# #904: compile and run the backup-shape probe against the SHIPPED predicate.
#
# The probe includes desktop/installer/installer-cleanup.nsh from its real path
# and runs iaCheckBackupShape over names built the way the installer builds them
# (one case derives its candidate from CoCreateGuid, like iaStageApplication).
# One line per case lands in the reading; the probe's exit code is the number of
# failed cases, so a predicate that stops matching the product fails here.
#
# Scratch state lives under %TEMP%\ia-shape-probe-* (-Keep retains it).
#
# Usage (from this directory); makensis is auto-discovered from the
# electron-builder cache like ../../run-probes.ps1:
#   powershell -File run-shape-probe.ps1 [-Makensis <path>] [-Predicate <nsh>] [-Keep]
#
# -Predicate points the probe at any copy of the predicate instead of the
# shipped one: the red proof for these cases runs the earlier, unbraced
# predicate and must report failures.
param(
  [string]$Makensis = '',
  [string]$Predicate = '',
  [switch]$Keep
)
$ErrorActionPreference = 'Stop'

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
if ($Predicate -eq '') {
  $cleanup = (Resolve-Path -LiteralPath (Join-Path $here '..\..\..\..\installer\installer-cleanup.nsh')).Path
} else {
  $cleanup = (Resolve-Path -LiteralPath $Predicate).Path
}
Write-Host ("predicate: {0}" -f $cleanup)
Write-Host ("predicate sha256: {0}" -f (Get-FileHash -LiteralPath $cleanup -Algorithm SHA256).Hash.ToLowerInvariant())

$root = Join-Path $env:TEMP ('ia-shape-probe-' + [guid]::NewGuid().ToString('N'))
$build = Join-Path $root 'build'
$result = Join-Path $root 'reading.txt'
New-Item -ItemType Directory -Force -Path $build | Out-Null

Copy-Item -LiteralPath (Join-Path $here 'shape-probe.nsi') -Destination $build -Force
$built = Invoke-Native -FilePath $Makensis -Arguments @(
  '/V2', "/DPROBE_CLEANUP=$cleanup", "/DPROBE_RESULT=$result", (Join-Path $build 'shape-probe.nsi')
)
Write-Host "compile exit = $($built.ExitCode)"
if ($built.ExitCode -ne 0) {
  $built.Text | ForEach-Object { Write-Host "    $_" }
  throw 'probe did not compile'
}

$process = Start-Process -FilePath (Join-Path $build 'shape-probe.exe') -Wait -PassThru
$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-Path -LiteralPath $result)) {
  if ((Get-Date) -gt $deadline) { throw "no reading appeared at $result" }
  Start-Sleep -Milliseconds 250
}
$lines = @(Get-Content -LiteralPath $result)
$lines | ForEach-Object { Write-Host $_ }
Write-Host "probe exit code = $($process.ExitCode)"

if (-not $Keep) {
  Remove-Item -Recurse -Force $root
  Write-Host 'scratch tree removed (pass -Keep to retain it)'
}

$failed = @($lines | Where-Object { $_ -like 'FAIL *' }).Count
if ($failed -gt 0) {
  Write-Host "shape cases failed = $failed"
  exit 1
}
if ($process.ExitCode -ne 0) {
  Write-Host 'probe exit code is not 0'
  exit 1
}
Write-Host ("shape cases passed = {0}" -f @($lines | Where-Object { $_ -like 'PASS *' }).Count)
exit 0
