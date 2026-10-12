# Compile and run the production iaProbePath against existing, missing and short files.
param([switch]$Keep)
$ErrorActionPreference = 'Stop'

$harness = Split-Path -Parent $MyInvocation.MyCommand.Path
$installerDir = (Resolve-Path (Join-Path $harness '..\..\..\installer')).Path
$scratchRoot = [System.IO.Path]::GetFullPath((Join-Path $env:TEMP 'ia-r1-drivers'))
$runId = [Guid]::NewGuid().ToString('N')
$probeRoot = Join-Path $scratchRoot ('fileexists-' + $runId)
$buildDir = Join-Path $scratchRoot ('fileexists-build-' + $runId)
$shortDir = Join-Path $scratchRoot ('fileexists-short-' + $runId)
$result = Join-Path $scratchRoot ('fileexists-result-' + $runId + '.txt')
$fileName = 'Intelligence Agent.exe'

foreach ($path in @($probeRoot, $buildDir, $shortDir, $result)) {
  if (-not [System.IO.Path]::GetFullPath($path).StartsWith($scratchRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "scratch path escapes the test root: $path"
  }
}

$makensis = @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'electron-builder\Cache\nsis-3.0.4.1') -Recurse -Filter 'makensis.exe' -ErrorAction SilentlyContinue)[0].FullName
if ([string]::IsNullOrEmpty($makensis)) { throw 'makensis.exe not found in the electron-builder cache' }

# Keep the product path longer than MAX_PATH, including the executable filename.
$deep = $probeRoot
$index = 0
$longDirectory = Join-Path $deep 'longdir'
$existingLong = Join-Path $longDirectory $fileName
while ($existingLong.Length -le 320) {
  $index = $index + 1
  $deep = Join-Path $deep ('segment-{0:D2}-0123456789-0123456789-0123456789' -f $index)
  $longDirectory = Join-Path $deep 'longdir'
  $existingLong = Join-Path $longDirectory $fileName
}
$missingLong = Join-Path $longDirectory 'Intelligence Agent.missing.exe'
$shortExisting = Join-Path $shortDir $fileName
if ($existingLong.Length -le 260 -or $missingLong.Length -le 260) {
  throw "long probe paths must exceed MAX_PATH: existing=$($existingLong.Length) missing=$($missingLong.Length)"
}

New-Item -ItemType Directory -Force -Path $scratchRoot | Out-Null
[System.IO.Directory]::CreateDirectory('\\?\' + $longDirectory) | Out-Null
[System.IO.File]::WriteAllText('\\?\' + $existingLong, 'present', [System.Text.Encoding]::ASCII)
[System.IO.Directory]::CreateDirectory($shortDir) | Out-Null
[System.IO.File]::WriteAllText($shortExisting, 'present', [System.Text.Encoding]::ASCII)
if (-not [System.IO.File]::Exists('\\?\' + $existingLong)) { throw "long existing file was not created: $existingLong" }
if ([System.IO.File]::Exists('\\?\' + $missingLong)) { throw "missing control unexpectedly exists: $missingLong" }
if (-not [System.IO.File]::Exists($shortExisting)) { throw "short existing control was not created: $shortExisting" }

$probeTemplatePath = Join-Path $harness 'fileexists-probe.nsi'
$probeTemplate = [System.IO.File]::ReadAllText($probeTemplatePath, [System.Text.Encoding]::UTF8)
$buildMarker = '@@PRODUCTION_IA_BUILD_LONG_PATH_BODY@@'
$probeMarker = '@@PRODUCTION_IA_PROBE_PATH@@'
if ([regex]::Matches($probeTemplate, [regex]::Escape($buildMarker)).Count -ne 1) {
  throw "expected exactly one production build-helper marker in $probeTemplatePath"
}
if ([regex]::Matches($probeTemplate, [regex]::Escape($probeMarker)).Count -ne 1) {
  throw "expected exactly one production function marker in $probeTemplatePath"
}
$cleanupPath = Join-Path $installerDir 'installer-cleanup.nsh'
$cleanupSource = [System.IO.File]::ReadAllText($cleanupPath, [System.Text.Encoding]::UTF8)
$macroStartToken = '!macro iaBuildLongPathBody'
$macroEndToken = '!macroend'
$macroStart = $cleanupSource.IndexOf($macroStartToken, [System.StringComparison]::Ordinal)
$macroEnd = if ($macroStart -ge 0) {
  $cleanupSource.IndexOf($macroEndToken, $macroStart + $macroStartToken.Length, [System.StringComparison]::Ordinal)
} else {
  -1
}
if ($macroStart -lt 0 -or $macroEnd -lt 0) { throw "production iaBuildLongPathBody macro not found in $cleanupPath" }
$buildBody = $cleanupSource.Substring($macroStart + $macroStartToken.Length, $macroEnd - $macroStart - $macroStartToken.Length)
$directoriesPath = Join-Path $installerDir 'installer-directories.nsh'
$directoriesSource = [System.IO.File]::ReadAllText($directoriesPath, [System.Text.Encoding]::UTF8)
$functionStartToken = 'Function iaProbePath'
$functionEndToken = 'FunctionEnd'
$functionStart = $directoriesSource.IndexOf($functionStartToken, [System.StringComparison]::Ordinal)
$functionEnd = if ($functionStart -ge 0) {
  $directoriesSource.IndexOf($functionEndToken, $functionStart + $functionStartToken.Length, [System.StringComparison]::Ordinal)
} else {
  -1
}
if ($functionStart -lt 0 -or $functionEnd -lt 0) { throw "production iaProbePath function not found in $directoriesPath" }
$functionSource = $directoriesSource.Substring($functionStart, $functionEnd + $functionEndToken.Length - $functionStart)
$probeSource = $probeTemplate.Replace($buildMarker, $buildBody).Replace($probeMarker, $functionSource)
if ($probeSource.Contains($buildMarker) -or $probeSource.Contains($probeMarker)) { throw 'production helper injection failed' }

New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
$probeScript = Join-Path $buildDir 'fileexists-probe.nsi'
$probeExe = Join-Path $buildDir 'fileexists-probe.exe'
[System.IO.File]::WriteAllText($probeScript, $probeSource, [System.Text.UTF8Encoding]::new($false))

& $makensis /V2 "/DPROBE_EXISTING=$existingLong" "/DPROBE_MISSING=$missingLong" "/DPROBE_SHORT=$shortExisting" "/DPROBE_RESULT=$result" $probeScript
if ($LASTEXITCODE -ne 0) { throw "makensis failed with exit code $LASTEXITCODE" }
if (-not (Test-Path -LiteralPath $probeExe)) { throw "probe exe missing: $probeExe" }
Write-Host ("probe sha256 = {0}" -f (Get-FileHash -Algorithm SHA256 -LiteralPath $probeExe).Hash)

Remove-Item -LiteralPath $result -ErrorAction SilentlyContinue
& $probeExe
if ($LASTEXITCODE -ne 0) { throw "NSIS probe failed with exit code $LASTEXITCODE" }
$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-Path -LiteralPath $result)) {
  if ((Get-Date) -gt $deadline) { throw "no reading appeared at $result" }
  Start-Sleep -Milliseconds 250
}
$reading = Get-Content -LiteralPath $result -Raw
Write-Host ("existing path length = {0}" -f $existingLong.Length)
Write-Host ("probe result = {0}" -f $reading.Trim())
if ($reading -notmatch 'existing_long=1 missing_long=0 existing_short=1') {
  throw "production iaProbePath returned unexpected results: $reading"
}

if (-not $Keep) {
  [System.IO.Directory]::Delete('\\?\' + $probeRoot, $true)
  Remove-Item -Recurse -Force -LiteralPath $buildDir
  Remove-Item -LiteralPath $result -Force
  Write-Host 'scratch tree removed (pass -Keep to retain it)'
}
