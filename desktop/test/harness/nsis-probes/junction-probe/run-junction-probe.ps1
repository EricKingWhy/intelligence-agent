# #904 item 5: build a junction inside a directory, run an `RMDir /r` over that
# directory through junction-probe.nsi, and report from PowerShell whether the
# junction TARGET's content survived. Content gone = the delete followed the
# reparse point. Scratch state lives under %TEMP%\ia-junction-probe-* and is
# removed at the end (-Keep retains it).
#
# Usage (from this directory); makensis is auto-discovered from the
# electron-builder cache like ../run-probes.ps1:
#   powershell -File run-junction-probe.ps1 [-Makensis <path>] [-Keep]
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
$root = Join-Path $env:TEMP ('ia-junction-probe-' + [guid]::NewGuid().ToString('N'))
$target = Join-Path $root 'target'
$victim = Join-Path $root 'victim'
$link = Join-Path $victim 'link'
$build = Join-Path $root 'build'
$result = Join-Path $root 'reading.txt'
New-Item -ItemType Directory -Force -Path $target, $victim, $build | Out-Null
Set-Content -LiteralPath (Join-Path $target 'keep.txt') -Value 'target payload' -Encoding ASCII
Set-Content -LiteralPath (Join-Path $victim 'plain.txt') -Value 'victim payload' -Encoding ASCII

$mklink = Invoke-Native -FilePath 'cmd' -Arguments @('/c', 'mklink', '/J', $link, $target)
$item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
if ($null -eq $item -or $item.LinkType -ne 'Junction') {
  throw "junction not created: $($mklink.Text -join ' ')"
}
Write-Host ("junction: {0} -> {1}" -f $link, $target)

Copy-Item -LiteralPath (Join-Path $here 'junction-probe.nsi') -Destination $build -Force
$built = Invoke-Native -FilePath $Makensis -Arguments @(
  '/V2', "/DPROBE_TARGET=\\?\$victim", "/DPROBE_DIR=$victim", "/DPROBE_RESULT=$result",
  (Join-Path $build 'junction-probe.nsi')
)
Write-Host "compile exit = $($built.ExitCode)"
if ($built.ExitCode -ne 0) {
  $built.Text | ForEach-Object { Write-Host "    $_" }
  throw 'probe did not compile'
}

Start-Process -FilePath (Join-Path $build 'junction-probe.exe') -Wait
$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-Path -LiteralPath $result)) {
  if ((Get-Date) -gt $deadline) { throw "no reading appeared at $result" }
  Start-Sleep -Milliseconds 250
}
Write-Host ("reading: {0}" -f ((Get-Content -LiteralPath $result) -join ' '))

$victimGone = -not (Test-Path -LiteralPath $victim)
$payloadSurvived = Test-Path -LiteralPath (Join-Path $target 'keep.txt')
Write-Host ("victim_gone={0} target_payload_survived={1}" -f $victimGone, $payloadSurvived)
if ($payloadSurvived) {
  Write-Host 'VERDICT: RMDir /r did NOT delete through the junction'
} else {
  Write-Host 'VERDICT: RMDir /r DID delete through the junction (target content gone)'
}

if (-not $Keep) {
  if (Test-Path -LiteralPath $link) { Invoke-Native -FilePath 'cmd' -Arguments @('/c', 'rmdir', $link) | Out-Null }
  Remove-Item -Recurse -Force $root
  Write-Host 'scratch tree removed (pass -Keep to retain it)'
}
