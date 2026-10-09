# Build a >MAX_PATH directory, compile the leftover-probe against it, run the
# probe from that directory and report the reading. The target is passed at
# compile time because NSIS reports $EXEDIR in 8.3 short form. ASCII only
# (Windows PowerShell 5.1 reads .ps1 as ANSI), so the makensis path comes from
# LOCALAPPDATA instead of a literal user name.
#
# All scratch state lives under %TEMP%\ia-r1-drivers and is removed at the end
# (-Keep retains it); the probe .nsi is copied to the build dir first because
# makensis resolves OutFile relative to the .nsi's directory.
param([switch]$Keep)
$ErrorActionPreference = 'Stop'

$harness = Split-Path -Parent $MyInvocation.MyCommand.Path
$probeRoot = Join-Path $env:TEMP 'ia-r1-drivers\longcheck'
$makensis = @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'electron-builder\Cache\nsis-3.0.4.1') -Recurse -Filter 'makensis.exe' -ErrorAction SilentlyContinue)[0].FullName
if ([string]::IsNullOrEmpty($makensis)) { throw 'makensis.exe not found in the electron-builder cache' }

# Nest segments until <deep>\longdir is well past MAX_PATH.
$deep = $probeRoot
$index = 0
while (($deep.Length + '\longdir'.Length) -le 320) {
  $index = $index + 1
  $deep = Join-Path $deep ('segment-{0:D2}-0123456789-0123456789-0123456789' -f $index)
}
$target = Join-Path $deep 'longdir'

foreach ($p in @($deep, $target)) {
  [System.IO.Directory]::CreateDirectory('\\?\' + $p) | Out-Null
}
Write-Host ("target = {0}" -f $target)
Write-Host ("target length = {0} exists = {1}" -f $target.Length, (Test-Path -LiteralPath ('\\?\' + $target)))

$buildDir = Join-Path $env:TEMP 'ia-r1-drivers\longcheck-build'
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
# makensis resolves OutFile relative to the script directory, not the cwd.
$probeExe = Join-Path $buildDir 'leftover-probe.exe'
Remove-Item -LiteralPath $probeExe -ErrorAction SilentlyContinue
Copy-Item -LiteralPath (Join-Path $harness 'leftover-probe.nsi') -Destination $buildDir -Force
& $makensis /V2 "/DPROBE_TARGET=$target" "/DPROBE_CONTROL=$probeRoot" (Join-Path $buildDir 'leftover-probe.nsi')
if (-not (Test-Path -LiteralPath $probeExe)) { throw "probe exe missing: $probeExe" }
Write-Host ("probe sha256 = {0}" -f (Get-FileHash -Algorithm SHA256 -LiteralPath $probeExe).Hash)

# Run the probe FROM the deep directory: a probe launched from a short path
# would resolve a relative target differently.
Copy-Item -LiteralPath $probeExe -Destination (Join-Path $deep 'leftover-probe.exe') -Force
$result = Join-Path $env:TEMP 'leftover-probe-result.txt'
Remove-Item -LiteralPath $result -ErrorAction SilentlyContinue
& (Join-Path $deep 'leftover-probe.exe')
# NSIS copies itself to %TEMP% and relaunches, so the parent returns before the
# copy has written anything: wait for the reading to appear.
$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-Path -LiteralPath $result)) {
  if ((Get-Date) -gt $deadline) { throw "no reading appeared at $result" }
  Start-Sleep -Milliseconds 250
}
Get-Content -LiteralPath $result

if (-not $Keep) {
  [System.IO.Directory]::Delete('\\?\' + $probeRoot, $true)
  Remove-Item -Recurse -Force $buildDir
  Write-Host 'scratch tree removed (pass -Keep to retain it)'
}
