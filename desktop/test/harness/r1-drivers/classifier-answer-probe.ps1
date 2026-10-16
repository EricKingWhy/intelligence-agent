# #1201 item 3 — measures what iaClassifyRunningProduct actually answers for each
# program the installer itself ships, using the classifier payload EXTRACTED
# VERBATIM from installer-directories.nsh (no re-implementation here: a
# re-typed copy could answer differently and would prove nothing about the
# shipped text).
#
# Answer vocabulary (installer-directories.nsh#iaClassifyRunningProduct):
#   0  = nothing running from the install root
#   11 = only processes under <install>\resources\python\   (the gate stops these)
#   12 = something else running from the install root       (the gate refuses)
#   13 = the probe itself could not answer
#
# Read-only with respect to the repo: everything happens under a scratch root in
# %TEMP% and is removed in finally unless -Keep is passed.
param([switch]$Keep)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$installerDir = (Resolve-Path (Join-Path $here '..\..\..\installer')).Path
$scratchRoot = Join-Path $env:TEMP 'ia-r1-drivers'
$runId = [Guid]::NewGuid().ToString('N')
$root = Join-Path $scratchRoot "classifier-answer-$runId"

$started = New-Object System.Collections.ArrayList
$installed = Join-Path $root 'App'
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Assert($condition, $message) {
  if (-not $condition) { throw "assertion failed: $message" }
}

function Get-ClassifierPayload {
  $source = [System.IO.File]::ReadAllText(
    (Join-Path $installerDir 'installer-directories.nsh'),
    [System.Text.Encoding]::UTF8)
  $matches = @($source -split "`n" | Where-Object { $_ -match '\$\$iaAll=@\(Get-CimInstance' })
  Assert ($matches.Count -eq 1) "expected exactly one classifier payload line, found $($matches.Count)"
  $line = $matches[0]
  # The line is an nsExec::Exec whose argument is a backtick-quoted shell command
  # of the form  `"$iaPowerShellPath" ... -Command "<payload>"  . Only the payload
  # inside -Command is the classifier; the launcher prefix is the caller's.
  $marker = '-Command "'
  $start = $line.IndexOf($marker)
  Assert ($start -ge 0) '-Command payload not found on the classifier line'
  $payload = $line.Substring($start + $marker.Length)
  $payload = $payload.Substring(0, $payload.LastIndexOf('"'))
  # NSIS escapes every "$" in a backtick-quoted string as "$$".
  return $payload.Replace('$$', '$')
}

# Invokes the extracted payload against $installed and returns its exit code,
# which is the classifier's answer. The payload exits the process itself
# (`exit 0` / `exit 11` / `exit 12`), so no output line is expected — the
# answer is the child's exit code.
function Get-Answer($payload) {
  $body = $payload.Replace('$iaFinalDirectory', $installed)
  $script = Join-Path $root ("ask-{0}.ps1" -f [Guid]::NewGuid().ToString('N'))
  [System.IO.File]::WriteAllText($script, $body + "`r`n", [System.Text.UTF8Encoding]::new($true))
  $null = & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $script 2>&1
  $answer = $LASTEXITCODE
  Assert ($answer -in @(0, 11, 12, 13)) "the classifier answered $answer, which is outside its vocabulary"
  return $answer
}

function Start-Probe($path, $argumentList) {
  $proc = Start-Process -FilePath $path -ArgumentList $argumentList -PassThru
  [void]$started.Add($proc)
  Start-Sleep -Milliseconds 800
  return $proc
}

function Test-Alive($proc) {
  return (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) -ne $null
}

try {
  New-Item -ItemType Directory -Force -Path $root | Out-Null

  # One live process per scene, each a copy of a real host executable placed at
  # the path the installer ships that program to.
  $scenes = [ordered]@{
    'python-runtime' = Join-Path $installed 'resources\python\python.exe'
    'mxc-wxc-exec'   = Join-Path $installed 'resources\mxc\wxc-exec.exe'
    'node-tui'       = Join-Path $installed 'resources\node\node.exe'
    'install-root'   = Join-Path $installed 'Intelligence Agent.exe'
  }
  $hostExe = (Get-Command node).Source
  foreach ($relative in @('resources\python', 'resources\mxc', 'resources\node')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $installed $relative) | Out-Null
  }
  foreach ($scene in $scenes.Keys) { Copy-Item -LiteralPath $hostExe -Destination $scenes[$scene] }

  $payload = Get-ClassifierPayload
  Write-Output 'classifier payload extracted verbatim from the shipped installer:'
  Write-Output ("  " + $payload)
  Write-Output ''

  $idle = Get-Answer $payload
  Write-Output ("{0,-16} answer={1}" -f 'idle', $idle)
  Assert ($idle -eq 0) "an idle install root must answer 0, got $idle"

  foreach ($scene in $scenes.Keys) {
    $proc = Start-Probe $scenes[$scene] @('-e', 'setTimeout(()=>{},90000)')
    Assert (Test-Alive $proc) "$scene did not start"
    $answer = Get-Answer $payload
    $alive = Test-Alive $proc
    Write-Output ("{0,-16} answer={1}  alive={2}  image={3}" -f $scene, $answer, $alive, $scenes[$scene])
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 400
  }

  Write-Output ''
  Write-Output 'reading: only resources\python\ is stoppable (answer 11); every other'
  Write-Output 'shipped program answers 12, which the gate treats as a refusal.'
}
finally {
  foreach ($proc in $started) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
  if ($Keep) {
    Write-Output "kept scratch: $root"
  }
  elseif (Test-Path -LiteralPath $root) {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
  }
}
