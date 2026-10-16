# Compile and run the PRODUCTION iaRollbackApplication function against a real,
# damaged installation with a live process running from it (#1201 item 1).
#
# The rollback path is the one the user reaches after an update already failed.
# Before #1201 it called no process predicate at all: it removed $INSTDIR
# recursively and renamed the backup back over it while a process was running
# from the directory being removed — measured in #1165 as a partial delete plus
# a rename that fails on a non-empty directory, i.e. the recovery failed for the
# reason the update did. The fix reuses the #914 gate (iaRequireNoRunningProduct)
# at that call site rather than deriving a second rule, so this driver compiles
# the real function, taken verbatim from installer-directories.nsh (with the gate
# and the cleanup helpers it calls), and drives three legs:
#
#   idle-rollback     no process anywhere          -> the gate answers 0, the
#                     partial tree is deleted, the backup is renamed back over
#                     $INSTDIR. The marker file that only the backup carried is
#                     then found under $INSTDIR, which is what makes "restored"
#                     a reading rather than a return value: nothing else in the
#                     leg can put it there.
#   runtime-rollback  the headless bundled service (a real process whose image
#                     path is $INSTDIR\resources\python\...) -> the classifier
#                     answers 11, the gate stops it and re-asks (bounded), and
#                     the rollback then succeeds exactly like idle-rollback.
#                     This is the branch a reflexive "refuse when anything runs"
#                     would break: #914 adjudication settled that a runtime that
#                     stops is not a reason to keep the installation broken.
#   client-rollback   a live image in the install root ($INSTDIR\ia-probe-
#                     client.exe) -> the classifier answers 12 and the gate
#                     refuses, so the restore never runs. The reading is what a
#                     refusal has to leave behind: the client process still
#                     alive, the backup still on disk, and — the part that is
#                     easy to get wrong — IaBackupDir still present in the
#                     registry, because a refusal here is not a terminal
#                     decision about the backup (#1165's convention: a terminal
#                     path clears the pointer, this one must not, or the backup
#                     is left on disk with no record naming it).
#
# The client leg is where the pre-fix text goes red twice over. The old rollback
# had no gate, so it deleted the partial tree out from under the running client
# and then failed the rename on the now non-empty $INSTDIR — which is why
# -DirectoriesSource against the pre-change file reports a different rc, a
# different backup/final pair and a different pointer, and why the leg asserts on
# all three instead of only on "it refused".
#
# Every leg starts from the state a hard-killed update leaves behind, which is
# the state iaRecoverInterruptedInstall is written to recognise: $INSTDIR exists
# but holds no app executable, a real "$INSTDIR.old-{guid}" backup sits next to
# it, and IaBackupDir in the scratch HKCU key names that backup. The backup
# carries a marker file, so a restore that claims to have happened can be checked
# against the filesystem instead of against its own return value.
#
# The driver reads the probe's one-line result file twice over: `after=` says the
# function returned at all, and the fields after it say what it decided. A Quit
# from inside the rollback (the gate's refusal) truncates the line, so a missing
# `after=` is reported as a reading rather than treated as a harness failure —
# the client leg expects exactly that.
#
# Usage (from desktop/): powershell -File test/harness/r1-drivers/run-rollback-gate-probe.ps1 [-Keep]
#   -DirectoriesSource <path>  drive the legs against another revision of
#                              installer-directories.nsh. Used for the red proof:
#                              the same legs must FAIL on the pre-change function
#                              (the client leg above all), which is what shows
#                              they discriminate rather than merely pass.
param([switch]$Keep, [string]$DirectoriesSource)
$ErrorActionPreference = 'Stop'

$harness = Split-Path -Parent $MyInvocation.MyCommand.Path
$installerDir = (Resolve-Path (Join-Path $harness '..\..\..\installer')).Path
$scratchRoot = [System.IO.Path]::GetFullPath((Join-Path $env:TEMP 'ia-r1-drivers'))
$runId = [Guid]::NewGuid().ToString('N')
# The probe defines ${INSTALL_REGISTRY_KEY} as this scratch key, so the run never
# touches the product's own HKCU entry. Removed in the finally block.
$probeRegistryKey = 'HKCU:\Software\IA Probe 1201 Rollback'

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

$probeTemplatePath = Join-Path $harness 'rollback-gate-probe.nsi'
$probeTemplate = [System.IO.File]::ReadAllText($probeTemplatePath, [System.Text.Encoding]::UTF8)
# The four backup-pointer macros are harvested as a run rather than one by one:
# they are adjacent in the include, and the rollback function reaches all of
# them (read at the top, clear on every terminal path, and the leftover read the
# restore-success path uses to drop a stale record), so a partial harvest would
# be a makensis error rather than a narrower test.
$backupMacros = (Get-ProductionBlock $directoriesSource '!macro iaStashBackupDir' '!macroend' 'iaStashBackupDir') +
  "`r`n`r`n" +
  (Get-ProductionBlock $directoriesSource '!macro iaClearBackupDir' '!macroend' 'iaClearBackupDir') +
  "`r`n`r`n" +
  (Get-ProductionBlock $directoriesSource '!macro iaReadBackupDir' '!macroend' 'iaReadBackupDir') +
  "`r`n`r`n" +
  (Get-ProductionBlock $directoriesSource '!macro iaReadLeftoverDir' '!macroend' 'iaReadLeftoverDir')
$replacements = [ordered]@{
  '@@PRODUCTION_IA_CLEANUP@@'       = $cleanupSource
  '@@PRODUCTION_IA_PROBE_PATH@@'    = Get-ProductionBlock $directoriesSource 'Function iaProbePath' 'FunctionEnd' 'iaProbePath'
  '@@PRODUCTION_IA_BACKUP_MACROS@@' = $backupMacros
  '@@PRODUCTION_IA_CLASSIFY@@'      = Get-ProductionBlock $directoriesSource 'Function iaClassifyRunningProduct' 'FunctionEnd' 'iaClassifyRunningProduct'
  '@@PRODUCTION_IA_REQUIRE@@'       = Get-ProductionBlock $directoriesSource 'Function iaRequireNoRunningProduct' 'FunctionEnd' 'iaRequireNoRunningProduct'
  '@@PRODUCTION_IA_ROLLBACK@@'      = Get-ProductionBlock $directoriesSource 'Function iaRollbackApplication' 'FunctionEnd' 'iaRollbackApplication'
}
$probeSource = $probeTemplate
foreach ($marker in $replacements.Keys) {
  if ([regex]::Matches($probeSource, [regex]::Escape($marker)).Count -ne 1) {
    throw "expected exactly one marker $marker in $probeTemplatePath"
  }
  $probeSource = $probeSource.Replace($marker, $replacements[$marker])
}
if ($probeSource -match '@@PRODUCTION_') { throw 'production injection failed' }
if (-not $probeSource.Contains('!insertmacro iaReadBackupDir')) { throw 'the injected rollback function lost its backup-pointer read' }
if (-not $probeSource.Contains('@(Get-CimInstance')) { throw 'the injected classification lost its array cast' }
# Whether the rollback function calls the gate at all is NOT asserted here: the
# client leg below is what pins it (it goes red on a function without the call),
# and a pre-flight assertion would block -DirectoriesSource from producing that
# red proof.

New-Item -ItemType Directory -Force -Path $scratchRoot | Out-Null

function Assert-InScratch {
  param([string]$Path)
  if (-not [System.IO.Path]::GetFullPath($Path).StartsWith($scratchRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "scratch path escapes the test root: $Path"
  }
}

function New-DamagedInstall {
  param([string]$Leg)
  $root = Join-Path $scratchRoot ("rollback-$Leg-$runId")
  Assert-InScratch $root
  # The partial tree a failed update leaves: content, but no app executable, so
  # iaRecoverInterruptedInstall recognises it as damaged (that is the branch that
  # makes the rollback path reachable in a real run).
  New-Item -ItemType Directory -Force -Path (Join-Path $root 'resources\python') | Out-Null
  Set-Content -LiteralPath (Join-Path $root 'new-version.txt') -Value 'probe placeholder: partial new version' -Encoding ascii
  return $root
}

function New-Backup {
  param([string]$Install)
  # A real backup directory next to the install, named in the product's own
  # format: ".old-{8-4-4-4-12}". iaCheckBackupShape refuses anything else — the
  # braces come from System::Call's "g" GUID form — so a hand-made name without
  # them would exercise the shape refusal instead of the rollback. The marker
  # file is what makes the restore provable: it exists only in the backup, so
  # finding it under $INSTDIR after the call cannot be a leftover of the leg's
  # own setup.
  $backup = "${Install}.old-{$([Guid]::NewGuid().ToString().ToUpperInvariant())}"
  Assert-InScratch $backup
  New-Item -ItemType Directory -Force -Path (Join-Path $backup 'resources\python') | Out-Null
  Set-Content -LiteralPath (Join-Path $backup 'previous-version-marker.txt') -Value 'probe marker: restored from the backup' -Encoding ascii
  return $backup
}

function Set-BackupPointer {
  param([string]$Backup)
  # What iaRecoverInterruptedInstall + iaStashBackupDir leave in HKCU, written
  # here directly because the probe reads it with the production iaReadBackupDir.
  # Read back before the probe runs: the armed pointer IS the leg's precondition,
  # and a leg that starts without it would take iaRollbackApplication's empty-
  # pointer early Return instead — a settled reading that looks plausible
  # (rc=failed, backup kept, exit 0) while measuring the harness rather than the
  # function. Measured once in six runs on this host (red pass, run 6), which is
  # why it is a hard setup failure and not a retry.
  New-Item -Path $probeRegistryKey -Force | Out-Null
  New-ItemProperty -Path $probeRegistryKey -Name 'IaBackupDir' -PropertyType String -Value $Backup -Force | Out-Null
  $armed = (Get-ItemProperty -Path $probeRegistryKey -Name 'IaBackupDir' -ErrorAction SilentlyContinue).IaBackupDir
  if ($armed -ne $Backup) { throw "the armed backup pointer read back as '$armed', expected '$Backup'" }
}

function Get-BackupPointer {
  $value = (Get-ItemProperty -Path $probeRegistryKey -Name 'IaBackupDir' -ErrorAction SilentlyContinue).IaBackupDir
  if ($null -eq $value) { return '' }
  return [string]$value
}

function Reset-ProbeState {
  # Runs ONCE, before the first leg. The key is deleted and recreated here rather
  # than between legs because the probe is a separate process that reads this key
  # by name: a leg that starts while a delete-then-recreate of the same key is
  # still settling can read the previous run's armed pointer (measured twice:
  # once as a shape refusal — the stale pointer names another leg's directory, so
  # iaCheckBackupShape answers 0 — and once as the empty-pointer early Return).
  # Both readings look like settled production behaviour while actually measuring
  # the harness. Between legs there is no delete at all: Set-BackupPointer
  # overwrites the single value in place, which is also what the production
  # iaStashBackupDir does ("the pointer is a single slot").
  Remove-Item -LiteralPath $probeRegistryKey -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -Path $probeRegistryKey -Force | Out-Null
  $stale = (Get-ItemProperty -Path $probeRegistryKey -Name 'IaBackupDir' -ErrorAction SilentlyContinue).IaBackupDir
  if (-not [string]::IsNullOrEmpty($stale)) { throw "the probe registry key still holds a pointer after the reset: $stale" }
}

function Start-ProbeProcess {
  param([string]$ExePath, [string[]]$Arguments)
  return Start-Process -FilePath $ExePath -ArgumentList $Arguments -PassThru
}

function Get-ProcessImagePath {
  param([int]$ProcessId)
  return (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue).Path
}

function Get-ProcessState {
  param([int]$ProcessId)
  if ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return 'alive' }
  return 'gone'
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

function Test-MarkerRestored {
  param([string]$Install)
  return Test-Path -LiteralPath (Join-Path $Install 'previous-version-marker.txt')
}

function Invoke-RollbackLeg {
  param([string]$Leg, [string]$Install, [string]$Backup)
  $buildDir = Join-Path $scratchRoot ("rollback-$Leg-build-$runId")
  Assert-InScratch $buildDir
  $result = Join-Path $scratchRoot ("rollback-$Leg-result-$runId.txt")
  Assert-InScratch $result
  New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
  $script = Join-Path $buildDir 'rollback-gate-probe.nsi'
  $exe = Join-Path $buildDir 'rollback-gate-probe.exe'
  # The injected production text carries non-ASCII comment characters (—, ×, …),
  # and makensis reads a BOM-less UTF-8 script as ANSI ("Bad text encoding"), so
  # this file is written with a BOM.
  [System.IO.File]::WriteAllText($script, $probeSource, [System.Text.UTF8Encoding]::new($true))
  & $makensis /V2 "/DPROBE_INSTALL=$Install" "/DPROBE_LEG=$Leg" "/DPROBE_RESULT=$result" $script
  if ($LASTEXITCODE -ne 0) { throw "[$Leg] makensis failed with exit code $LASTEXITCODE" }
  if (-not (Test-Path -LiteralPath $exe)) { throw "[$Leg] probe exe missing: $exe" }

  Remove-Item -LiteralPath $result -ErrorAction SilentlyContinue
  Set-BackupPointer -Backup $Backup
  $process = Start-Process -FilePath $exe -Wait -PassThru
  $exitCode = $process.ExitCode
  # A Quit inside the rollback truncates the line after "before=", so the raw
  # text is kept as-is: "the function never returned" is a reading the legs
  # assert on, and reshaping it here would hide it. -match is line-based in
  # PowerShell, so the legs below anchor their patterns to the whole single-line
  # reading (^...$) instead of testing the fragment they happen to look for on
  # whichever line comes first.
  $reading = if (Test-Path -LiteralPath $result) { (Get-Content -LiteralPath $result -Raw).Trim() } else { '<no reading>' }
  Write-Host ("[{0}] exit={1} {2}" -f $Leg, $exitCode, $reading)
  return [pscustomobject]@{ Leg = $Leg; ExitCode = $exitCode; Reading = $reading; Install = $Install; Backup = $Backup }
}

$started = @()
$failures = @()
try {
  # One reset for the whole run, never between legs (see Reset-ProbeState).
  Reset-ProbeState
  # --- idle-rollback: nothing runs, the restore must simply happen -----------
  $installIdle = New-DamagedInstall 'idle'
  $backupIdle = New-Backup -Install $installIdle
  $leg = Invoke-RollbackLeg -Leg 'idle-rollback' -Install $installIdle -Backup $backupIdle
  if ($leg.ExitCode -ne 0) { $failures += "[idle-rollback] expected exit 0, got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch 'ptr=[^ ]+') { $failures += "[idle-rollback] the probe read no armed pointer (setup failure, not a gate reading): $($leg.Reading)" }
  if ($leg.Reading -notmatch 'before=0') { $failures += "[idle-rollback] the leg did not start from the damaged-tree state: $($leg.Reading)" }
  if ($leg.Reading -notmatch 'after=1') { $failures += "[idle-rollback] the rollback never returned (truncated reading): $($leg.Reading)" }
  if ($leg.Reading -notmatch '^leg=idle-rollback ptr=[^ ]+ before=0 after=1 running=0 stop= waits=0 shape=1 rc=restored backup=0 delete=ok final=1 pointer=gone errflag=(clear|set)$') {
    $failures += "[idle-rollback] $($leg.Reading)"
  }
  if (-not (Test-MarkerRestored -Install $installIdle)) {
    $failures += '[idle-rollback] the backup content is not under $INSTDIR: the restore did not happen'
  }
  if (Test-Path -LiteralPath $backupIdle) {
    $failures += "[idle-rollback] the backup directory survived the successful restore: $backupIdle"
  }

  # --- runtime-rollback: the bundled runtime is stopped, then the restore -----
  # Answer 11: only processes under $INSTDIR\resources\python\. The point of the
  # leg is that a runtime which stops is NOT a reason to leave the user on a
  # broken installation, so it asserts the same restore as idle-rollback plus the
  # gate's own trace. The trace is what separates "the gate stopped it" from "the
  # classifier never saw it, answered 0 and the restore happened to work": the
  # re-ask counter only leaves 0 when the FIRST answer was 11, and the final
  # answer is 0 because the loop exits on it. running=0 waits=1 is therefore the
  # stop-and-re-ask pair, and waits=0 would mean the process was never classified.
  $installRuntime = New-DamagedInstall 'runtime'
  $backupRuntime = New-Backup -Install $installRuntime
  $runtimeExe = Join-Path $installRuntime 'resources\python\ia-probe-runtime.exe'
  Copy-Item -LiteralPath $nodeExe -Destination $runtimeExe
  $runtime = Start-ProbeProcess -ExePath $runtimeExe -Arguments @('-e', 'setTimeout(()=>{},120000)')
  $started += $runtime.Id
  $runtimePath = Get-ProcessImagePath -ProcessId $runtime.Id
  if ($runtimePath -ne $runtimeExe) { throw "[runtime-rollback] probe process image path is '$runtimePath', expected '$runtimeExe'" }
  $leg = Invoke-RollbackLeg -Leg 'runtime-rollback' -Install $installRuntime -Backup $backupRuntime
  if ($leg.ExitCode -ne 0) { $failures += "[runtime-rollback] expected exit 0, got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch 'ptr=[^ ]+') { $failures += "[runtime-rollback] the probe read no armed pointer (setup failure, not a gate reading): $($leg.Reading)" }
  if ($leg.Reading -notmatch 'after=1') { $failures += "[runtime-rollback] the rollback never returned (truncated reading): $($leg.Reading)" }
  if ($leg.Reading -notmatch '^leg=runtime-rollback ptr=[^ ]+ before=0 after=1 running=0 stop=\d+ waits=[1-9][0-9]* shape=1 rc=restored backup=0 delete=ok final=1 pointer=gone errflag=(clear|set)$') {
    $failures += "[runtime-rollback] the gate did not stop the runtime and re-ask (waits stayed 0, so the first answer was never 11), or the restore did not follow: $($leg.Reading)"
  }
  if (-not (Wait-ProcessGone -ProcessId $runtime.Id)) {
    $failures += "[runtime-rollback] the process $($runtime.Id) survived the gate's stop"
  } else {
    Write-Host ("[runtime-rollback] probe process {0} after the gate: {1}" -f $runtime.Id, (Get-ProcessState -ProcessId $runtime.Id))
  }
  if (-not (Test-MarkerRestored -Install $installRuntime)) {
    $failures += '[runtime-rollback] the backup content is not under $INSTDIR: the restore did not happen'
  }
  if (Test-Path -LiteralPath $backupRuntime) {
    $failures += "[runtime-rollback] the backup directory survived the successful restore: $backupRuntime"
  }

  # --- client-rollback: a live client refuses the restore, and keeps the record
  # Answer 12 (anything that is not only the bundled runtime): the gate refuses
  # and the rollback Quits, so the line stops after "before=". What is asserted
  # is the state a refusal has to leave: a live client (it holds the user's
  # session and is never killed), the partial tree and the backup both exactly as
  # they were, and IaBackupDir still armed — a refusal that cleared the pointer
  # would leave the backup on disk with no record naming it.
  $installClient = New-DamagedInstall 'client'
  $backupClient = New-Backup -Install $installClient
  $clientExe = Join-Path $installClient 'ia-probe-client.exe'
  Copy-Item -LiteralPath $nodeExe -Destination $clientExe
  $client = Start-ProbeProcess -ExePath $clientExe -Arguments @('-e', 'setTimeout(()=>{},120000)')
  $started += $client.Id
  $clientPath = Get-ProcessImagePath -ProcessId $client.Id
  if ($clientPath -ne $clientExe) { throw "[client-rollback] probe process image path is '$clientPath', expected '$clientExe'" }
  $leg = Invoke-RollbackLeg -Leg 'client-rollback' -Install $installClient -Backup $backupClient
  if ($leg.ExitCode -ne 2) { $failures += "[client-rollback] expected exit 2 (the gate's refusal), got $($leg.ExitCode)" }
  if ($leg.Reading -notmatch '^leg=client-rollback ptr=[^ ]+ before=0$') { $failures += "[client-rollback] the leg did not start from the damaged-tree state (or the probe read no armed pointer): $($leg.Reading)" }
  if ($leg.Reading -match 'after=') {
    $failures += "[client-rollback] the rollback returned instead of refusing: $($leg.Reading)"
  }
  if (-not (Test-ProcessAlive -ProcessId $client.Id)) {
    $failures += "[client-rollback] the client process $($client.Id) is gone but a client in the install root is never stopped"
  } else {
    Write-Host ("[client-rollback] probe process {0} after the refusal: {1}" -f $client.Id, (Get-ProcessState -ProcessId $client.Id))
  }
  if (-not (Test-Path -LiteralPath $backupClient)) {
    $failures += '[client-rollback] the refusal removed the backup'
  }
  if (Test-MarkerRestored -Install $installClient) {
    $failures += '[client-rollback] the refusal restored the backup over the partial install'
  }
  if (-not (Test-Path -LiteralPath (Join-Path $installClient 'new-version.txt'))) {
    $failures += '[client-rollback] the refusal deleted part of the partial install'
  }
  $pointerAfter = Get-BackupPointer
  if ($pointerAfter -ne $backupClient) {
    $failures += "[client-rollback] the refusal left IaBackupDir='$pointerAfter', expected the armed '$backupClient' (#1165: a non-terminal refusal keeps the pointer)"
  } else {
    Write-Host '[client-rollback] IaBackupDir after the refusal: still armed'
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
  throw "$($failures.Count) rollback-gate leg assertion(s) failed"
}
Write-Host 'rollback gate legs OK: idle-rollback (restored), runtime-rollback (runtime stopped, restored), client-rollback (refused, client alive, backup armed)'
