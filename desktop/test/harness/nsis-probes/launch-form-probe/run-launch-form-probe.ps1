# #904: measure the launch forms of an NSIS uninstaller against
# launch-form-probe.nsi and print the readings the R1 driver's in-place step and
# the README table rest on. Scratch state lives under
# %TEMP%\ia-launch-form-probe-<guid> and is removed at the end (-Keep retains
# it).
#
# Every reading is also an expectation, and a violated one fails this script:
# the driver asserts the sweep's exit-code contract through the in-place form,
# so a silent change in what a form does must break the probe rather than turn
# those assertions into guesses. Two variants (a path with a space and one
# without) run the same matrix, because the value of "_?=" is "the rest of the
# command line" and a path with spaces is the case that quoting would ruin.
#
# Usage (from this directory); makensis is auto-discovered from the
# electron-builder cache like ../run-probes.ps1:
#   powershell -File run-launch-form-probe.ps1 [-Makensis <path>] [-Keep]
param(
  [string]$Makensis = '',
  [switch]$Keep
)
$ErrorActionPreference = 'Stop'

# Native tools write to stderr on failure, and under PowerShell 5.1 a redirected
# stderr line becomes a terminating error while the preference is 'Stop'; run
# native tools with 'Continue' and read the exit code instead.
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
$root = Join-Path $env:TEMP ('ia-launch-form-probe-' + [guid]::NewGuid().ToString('N'))
$markers = Join-Path $root 'markers'
$build = Join-Path $root 'build'
New-Item -ItemType Directory -Force -Path $markers, $build | Out-Null

$violations = New-Object System.Collections.Generic.List[string]
function Expect([string]$name, [bool]$ok, [string]$detail) {
  Write-Host ("  EXPECT {0}: {1} ({2})" -f $name, $(if ($ok) { 'ok' } else { 'VIOLATED' }), $detail)
  if (-not $ok) { $violations.Add($name) }
}
function Clear-Markers {
  foreach ($f in @('init.txt', 'section.txt')) {
    Remove-Item -LiteralPath (Join-Path $markers $f) -ErrorAction SilentlyContinue
  }
}
function Read-Marker([string]$name) {
  $p = Join-Path $markers $name
  if (Test-Path -LiteralPath $p) { return ((Get-Content -LiteralPath $p) -join ' | ') }
  return ''
}
# Remove-Item -Recurse is safe here: neither install dir holds a reparse point
# (the junction hazard is measured by ../junction-probe, not by this probe).
function Remove-Dir([string]$path) {
  if (Test-Path -LiteralPath $path) { Remove-Item -Recurse -Force -LiteralPath $path }
}

Copy-Item -LiteralPath (Join-Path $here 'launch-form-probe.nsi') -Destination $build -Force
Set-Location $build
$variants = @(
  @{ Name = 'nospace'; Spaced = $false; Setup = 'launch-form-probe-nospace.exe'; Dir = (Join-Path $root 'dir-no-space') },
  @{ Name = 'spaced'; Spaced = $true; Setup = 'launch-form-probe-spaced.exe'; Dir = (Join-Path $root 'dir with space') }
)
foreach ($v in $variants) {
  $arguments = @('/V2', "/DPROBE_BASE=$root", "/DPROBE_MARKERS=$markers")
  if ($v.Spaced) { $arguments = $arguments + '/DPROBE_SPACED' }
  $arguments = $arguments + (Join-Path $build 'launch-form-probe.nsi')
  $built = Invoke-Native -FilePath $Makensis -Arguments $arguments
  Write-Host ("compile {0} exit = {1}" -f $v.Name, $built.ExitCode)
  if ($built.ExitCode -ne 0) {
    $built.Text | ForEach-Object { Write-Host "    $_" }
    throw 'probe did not compile'
  }
}

function Reset-Probe($v) {
  Remove-Dir $v.Dir
  Clear-Markers
  Start-Process -FilePath (Join-Path $build $v.Setup) -ArgumentList '/S' -Wait | Out-Null
  # The setup run writes its own init line; drop it so only the measured
  # uninstaller launch leaves lines in the markers.
  Clear-Markers
}
function Show-Markers {
  Write-Host ("  marker init| {0}" -f (Read-Marker 'init.txt'))
  Write-Host ("  marker section| {0}" -f (Read-Marker 'section.txt'))
}

foreach ($v in $variants) {
  $dir = $v.Dir
  $un = Join-Path $dir 'uninstall-probe.exe'
  # Not "$keep": PowerShell variable names are case-insensitive and the script's
  # own [switch]$Keep parameter would be overwritten by the path.
  $keepFile = Join-Path $dir 'keep.txt'
  Write-Host ("=== variant {0}: dir=[{1}] ===" -f $v.Name, $dir)

  # --- plain: what a user or the Start menu does -------------------------
  Reset-Probe $v
  $p = Start-Process -FilePath $un -ArgumentList '/S' -Wait -PassThru
  Start-Sleep -Milliseconds 500
  Write-Host ("plain: rc={0} dir={1} keep={2} un={3}" -f $p.ExitCode, (Test-Path -LiteralPath $dir), (Test-Path -LiteralPath $keepFile), (Test-Path -LiteralPath $un))
  Show-Markers
  Expect 'plain_rc0' ($p.ExitCode -eq 0) ('rc={0}' -f $p.ExitCode)
  Expect 'plain_section_ran' ((Read-Marker 'section.txt') -ne '') 'section marker written'
  Expect 'plain_copied_to_temp' ((Read-Marker 'init.txt') -match 'EXEDIR=\[.*~nsu\.tmp\]') (Read-Marker 'init.txt')
  Expect 'plain_removed_dir' (-not (Test-Path -LiteralPath $dir)) 'install dir gone'

  # --- self+inplace-raw: the form the stock updater's call uses ----------
  Reset-Probe $v
  $p = Start-Process -FilePath $un -ArgumentList ('/S _?={0}' -f $dir) -Wait -PassThru
  Start-Sleep -Milliseconds 500
  Write-Host ("self+inplace-raw: rc={0} dir={1} keep={2} un={3}" -f $p.ExitCode, (Test-Path -LiteralPath $dir), (Test-Path -LiteralPath $keepFile), (Test-Path -LiteralPath $un))
  Show-Markers
  Expect 'self_raw_rc2' ($p.ExitCode -eq 2) ('rc={0}' -f $p.ExitCode)
  Expect 'self_raw_section_ran' ((Read-Marker 'section.txt') -ne '') 'section marker written'
  Expect 'self_raw_in_place' ((Read-Marker 'init.txt') -match [regex]::Escape("EXEDIR=[$dir]")) (Read-Marker 'init.txt')
  Expect 'self_raw_keep_removed' (-not (Test-Path -LiteralPath $keepFile)) 'the section removed keep.txt'
  Expect 'self_raw_uninstaller_kept' (Test-Path -LiteralPath $un) 'the running file cannot delete itself'
  Expect 'self_raw_dir_kept' (Test-Path -LiteralPath $dir) 'the directory holding the running file survives'

  # --- self+inplace-quoted: recorded because its rc looks like a report --
  Reset-Probe $v
  $p = Start-Process -FilePath $un -ArgumentList ('/S _?="{0}"' -f $dir) -Wait -PassThru
  Start-Sleep -Milliseconds 500
  Write-Host ("self+inplace-quoted: rc={0} dir={1} keep={2} un={3}" -f $p.ExitCode, (Test-Path -LiteralPath $dir), (Test-Path -LiteralPath $keepFile), (Test-Path -LiteralPath $un))
  Show-Markers
  Expect 'self_quoted_returns2' ($p.ExitCode -eq 2) ('rc={0}' -f $p.ExitCode)
  Expect 'self_quoted_script_absent' (((Read-Marker 'init.txt') -eq '') -and ((Read-Marker 'section.txt') -eq '')) 'no marker: the script never ran'
  Expect 'self_quoted_kept_everything' ((Test-Path -LiteralPath $keepFile) -and (Test-Path -LiteralPath $un)) 'nothing was deleted'

  # --- self+inplace-array: quoting differs by path shape -----------------
  # With the call operator an array element goes through PowerShell's native
  # argument quoting: a token with spaces comes out quoted, the leading quote
  # hides "_?=" from NSIS and the stub silently falls back to the copy form
  # (rc 0, dir removed). With no space the token is not quoted, the launch IS in
  # place (rc 2) -- which is why the driver builds its in-place line as one
  # string instead of an array.
  Reset-Probe $v
  $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { & $un '/S' ('_?={0}' -f $dir) 2>&1 | Out-Null; $rc = $LASTEXITCODE } finally { $ErrorActionPreference = $previous }
  Start-Sleep -Milliseconds 500
  Write-Host ("self+inplace-array: rc={0} dir={1} keep={2} un={3}" -f $rc, (Test-Path -LiteralPath $dir), (Test-Path -LiteralPath $keepFile), (Test-Path -LiteralPath $un))
  Show-Markers
  if ($v.Spaced) {
    Expect 'self_array_spaced_rc0' ($rc -eq 0) ('rc={0} -- the quoted token fell back to the stub form' -f $rc)
    Expect 'self_array_spaced_removed_dir' (-not (Test-Path -LiteralPath $dir)) 'the whole dir was removed'
  } else {
    Expect 'self_array_nospace_rc2' ($rc -eq 2) ('rc={0} -- no space, no quoting, so in place' -f $rc)
    Expect 'self_array_nospace_in_place' ((Read-Marker 'init.txt') -match [regex]::Escape("EXEDIR=[$dir]")) (Read-Marker 'init.txt')
  }

  # --- copy+inplace-raw: installUtil.nsh's primary shape -----------------
  Reset-Probe $v
  $copy = Join-Path $root 'old-uninstaller.exe'
  Copy-Item -LiteralPath $un -Destination $copy -Force
  $p = Start-Process -FilePath $copy -ArgumentList ('/S _?={0}' -f $dir) -Wait -PassThru
  Start-Sleep -Milliseconds 500
  Write-Host ("copy+inplace-raw: rc={0} dir={1} keep={2} un={3}" -f $p.ExitCode, (Test-Path -LiteralPath $dir), (Test-Path -LiteralPath $keepFile), (Test-Path -LiteralPath $un))
  Show-Markers
  Expect 'copy_raw_rc2' ($p.ExitCode -eq 2) ('rc={0}' -f $p.ExitCode)
  Expect 'copy_raw_section_ran' ((Read-Marker 'section.txt') -ne '') 'section marker written'
  Expect 'copy_raw_removed_dir' (-not (Test-Path -LiteralPath $dir)) 'a copy outside the dir lets the section remove it whole'
  Remove-Item -LiteralPath $copy -Force -ErrorAction SilentlyContinue
  Remove-Dir $dir
}

Write-Host ''
if ($violations.Count -gt 0) {
  Write-Host ("VERDICT: {0} expectation(s) violated: {1}" -f $violations.Count, ($violations -join ', '))
} else {
  Write-Host 'VERDICT: all expectations held'
}

# Step out of the scratch tree before removing it (the cwd is inside it).
Set-Location $here
if (-not $Keep) {
  Remove-Dir $root
  Write-Host 'scratch tree removed (pass -Keep to retain it)'
}
if ($violations.Count -gt 0) { exit 1 }
exit 0
