# #904: build the directories semantics-probe.nsi measures, compile and run it,
# print the reading, then clean up (-Keep retains the scratch tree). Scratch
# state lives under %TEMP%\ia-semantics-probe-*.
#
# Usage (from this directory); makensis is auto-discovered from the
# electron-builder cache like ../../run-probes.ps1:
#   powershell -File run-semantics-probe.ps1 [-Makensis <path>] [-Keep]
param(
  [string]$Makensis = '',
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
$root = Join-Path $env:TEMP ('ia-semantics-probe-' + [guid]::NewGuid().ToString('N'))
$empty = Join-Path $root 'plain'
$content = Join-Path $root 'content'
$junction = Join-Path $root 'junc'
$missing = Join-Path $root 'gone'
$build = Join-Path $root 'build'
$result = Join-Path $root 'reading.txt'
New-Item -ItemType Directory -Force -Path $empty, $content, $build | Out-Null
Set-Content -LiteralPath (Join-Path $content 'one.txt') -Value 'x' -Encoding ASCII
$mklink = Invoke-Native -FilePath 'cmd' -Arguments @('/c', 'mklink', '/J', $junction, $empty)
$item = Get-Item -LiteralPath $junction -Force -ErrorAction SilentlyContinue
if ($null -eq $item -or $item.LinkType -ne 'Junction') {
  throw "junction not created: $($mklink.Text -join ' ')"
}

Copy-Item -LiteralPath (Join-Path $here 'semantics-probe.nsi') -Destination $build -Force
$built = Invoke-Native -FilePath $Makensis -Arguments @(
  '/V2', "/DPROBE_EMPTY=$empty", "/DPROBE_CONTENT=$content", "/DPROBE_JUNCTION=$junction",
  "/DPROBE_MISSING=$missing", "/DPROBE_RESULT=$result", (Join-Path $build 'semantics-probe.nsi')
)
Write-Host "compile exit = $($built.ExitCode)"
if ($built.ExitCode -ne 0) {
  $built.Text | ForEach-Object { Write-Host "    $_" }
  throw 'probe did not compile'
}

Start-Process -FilePath (Join-Path $build 'semantics-probe.exe') -Wait
$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-Path -LiteralPath $result)) {
  if ((Get-Date) -gt $deadline) { throw "no reading appeared at $result" }
  Start-Sleep -Milliseconds 250
}
Get-Content -LiteralPath $result | ForEach-Object { Write-Host $_ }

if (-not $Keep) {
  if (Test-Path -LiteralPath $junction) { Invoke-Native -FilePath 'cmd' -Arguments @('/c', 'rmdir', $junction) | Out-Null }
  Remove-Item -Recurse -Force $root
  Write-Host 'scratch tree removed (pass -Keep to retain it)'
}
