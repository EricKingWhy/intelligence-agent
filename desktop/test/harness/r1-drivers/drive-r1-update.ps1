<#
  R1 (#901) end-to-end verification driver.

  Drives the isolated W-16 installer test artifact -- built from the production
  NSIS config by desktop/scripts/test-windows-installer.mjs --compile-only --
  through a real update cycle:

      install #1  ->  plant payload  ->  install #2 (stage + promote)  ->  assert

  Case success : the planted payload is a >MAX_PATH deep tree, so promote can
                 only remove the previous version's tree with a working
                 long-path prefix.
  Case failure : the planted payload contains a DELETE-denied child directory,
                 so promote cannot remove the backup and must record the
                 leftover instead of orphaning it silently.

  Nothing outside the "IA Installer Test *" product directories is written; the
  driver asserts that the real product directories are untouched.

  Usage (build the test artifact first: `node scripts/test-windows-installer.mjs
  --compile-only`, then pass the printed installer path):

      powershell -File drive-r1-update.ps1 -Installer <installer-test.exe> `
        -Tag t1 -Case success -Expect fixed

  Evidence logs default to %TEMP%\ia-r1-drivers\evidence (-EvidenceDir changes it).

  ASCII only (Windows PowerShell 5.1 reads .ps1 as ANSI).
#>
param(
  [Parameter(Mandatory = $true)][string]$Installer,
  [Parameter(Mandatory = $true)][string]$Tag,
  [Parameter(Mandatory = $true)][ValidateSet('success', 'failure')][string]$Case,
  [Parameter(Mandatory = $true)][ValidateSet('baseline', 'fixed')][string]$Expect,
  [int]$DeepLevels = 12,
  [string]$EvidenceDir = ''
)

$ErrorActionPreference = 'Stop'

# Evidence lives outside the checkout; -EvidenceDir overrides the temp default.
if ([string]::IsNullOrEmpty($EvidenceDir)) { $EvidenceDir = Join-Path $env:TEMP 'ia-r1-drivers\evidence' }
$evidence = $EvidenceDir
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
$logPath = Join-Path $evidence ('{0}-{1}-{2}.log' -f $Tag, $Case, $Expect)
$script:failures = 0

function Log([string]$m) {
  $line = ('{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m)
  Write-Host $line
  Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Assert([string]$name, [bool]$ok, [string]$detail) {
  $verdict = if ($ok) { 'PASS' } else { 'FAIL' }
  if (-not $ok) { $script:failures = $script:failures + 1 }
  Log ('ASSERT {0} {1} {2}' -f $verdict, $name, $detail)
}

function ToLongPath([string]$p) {
  if ($p -like '\\?\*') { return $p }
  return ('\\?\' + $p)
}

# Windows PowerShell 5.1 turns a native command's stderr into a *terminating*
# error while $ErrorActionPreference is 'Stop', so native calls that may write
# to stderr (icacls on a path that is already gone) run with 'Continue'.
function Invoke-Native([string]$exe, [string[]]$arguments) {
  $saved = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { return @(& $exe @arguments 2>&1) } finally { $ErrorActionPreference = $saved }
}

function RegVal([string]$key, [string]$name) {
  $item = Get-ItemProperty -Path $key -Name $name -ErrorAction SilentlyContinue
  if ($null -eq $item) { return $null }
  return [string]$item.$name
}

function RunInstaller([string]$exe, [string]$logFile) {
  $arguments = @('/S')
  if (-not [string]::IsNullOrEmpty($logFile)) { $arguments = $arguments + ('/O{0}' -f $logFile) }
  $p = Start-Process -FilePath $exe -ArgumentList $arguments -Wait -PassThru
  return $p.ExitCode
}

function TestProductDirs([string]$root) {
  return @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'IA Installer Test*' } | ForEach-Object { $_.Name })
}

Log ('=== R1 e2e: case={0} expect={1} tag={2} ===' -f $Case, $Expect, $Tag)
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
Log ('installer = {0} size = {1} bytes' -f $installerPath, (Get-Item -LiteralPath $installerPath).Length)

$productsRoot = Join-Path $env:LOCALAPPDATA 'Programs'
Log ('products root = {0}' -f $productsRoot)
$realBefore = @(Get-ChildItem -LiteralPath $productsRoot -Directory |
  Where-Object { $_.Name -like 'Intelligence Agent*' } | ForEach-Object { $_.Name })
Log ('real product dirs before = [{0}]' -f ($realBefore -join ', '))
$before = TestProductDirs $productsRoot
Log ('test product dirs before = [{0}]' -f ($before -join ', '))

# ---------------------------------------------------------------- install #1
$rc1 = RunInstaller $installerPath $null
Log ('install #1 rc = {0}' -f $rc1)
Assert 'install1_rc0' ($rc1 -eq 0) ('rc={0}' -f $rc1)

$after1 = TestProductDirs $productsRoot
$new = @($after1 | Where-Object { $before -notcontains $_ })
Assert 'install1_created_one_dir' ($new.Count -eq 1) ('new=[{0}]' -f ($new -join ', '))
if ($new.Count -ne 1) {
  Log ('ABORT: cannot continue without exactly one new test product dir')
  Log ('DONE assertions_failed={0}' -f $script:failures)
  exit 1
}
$instDir = Join-Path $productsRoot $new[0]
Assert 'installdir_is_test_product' ($new[0] -like 'IA Installer Test*') $instDir

$exe = @(Get-ChildItem -LiteralPath $instDir -Filter '*.exe' | Where-Object { $_.Name -notlike 'Uninstall*' })[0]
Assert 'payload_exe_present' ($null -ne $exe) ('exe={0}' -f $(if ($exe) { $exe.Name } else { '<none>' }))
Log ('installed exe = {0}' -f $exe.FullName)

$key = Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' | Where-Object {
  $loc = (Get-ItemProperty -Path $_.PSPath -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
  $dn = (Get-ItemProperty -Path $_.PSPath -Name DisplayName -ErrorAction SilentlyContinue).DisplayName
  ($null -ne $loc) -and ($loc.TrimEnd('\') -ieq $instDir.TrimEnd('\')) -and ($dn -like 'IA Installer Test*')
} | Select-Object -First 1
Assert 'uninstall_key_found' ($null -ne $key) ('key={0}' -f $(if ($key) { $key.PSChildName } else { '<none>' }))
if ($null -eq $key) {
  Log ('ABORT: uninstall registry key not found')
  Log ('DONE assertions_failed={0}' -f $script:failures)
  exit 1
}
$keyPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\' + $key.PSChildName
Log ('uninstall registry key = {0}' -f $keyPath)

# electron-builder keeps the application's own values -- our backup pointer
# among them -- in HKCU\Software\<APP_GUID>, a different key from the uninstall
# entry: multiUser.nsh defines
#   INSTALL_REGISTRY_KEY   "Software\${APP_GUID}"
#   UNINSTALL_REGISTRY_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${UNINSTALL_APP_KEY}"
# IaBackupDir / IaLeftoverDir are written to the first one, so reading them
# from the uninstall key always returns an empty value. Locate it separately.
$appKey = Get-ChildItem 'HKCU:\Software' -ErrorAction SilentlyContinue | Where-Object {
  $loc = (Get-ItemProperty -Path $_.PSPath -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
  ($null -ne $loc) -and ($loc.TrimEnd('\') -ieq $instDir.TrimEnd('\'))
} | Select-Object -First 1
Assert 'app_registry_key_found' ($null -ne $appKey) ('key={0}' -f $(if ($appKey) { $appKey.PSChildName } else { '<none>' }))
if ($null -eq $appKey) {
  Log ('ABORT: application registry key HKCU\Software\<APP_GUID> not found')
  Log ('DONE assertions_failed={0}' -f $script:failures)
  exit 1
}
$appKeyPath = 'HKCU:\Software\' + $appKey.PSChildName
Log ('app registry key = {0}' -f $appKeyPath)
Log ('registry before update: IaBackupDir={0} IaLeftoverDir={1} InstallLocation={2}' -f `
    (RegVal $appKeyPath 'IaBackupDir'), (RegVal $appKeyPath 'IaLeftoverDir'), (RegVal $appKeyPath 'InstallLocation'))

# ------------------------------------------------------------- plant payload
$plant = Join-Path $instDir 'planted-deep'
$deep = $plant
for ($i = 1; $i -le $DeepLevels; $i++) {
  $deep = Join-Path $deep ('depth{0:d2}-abcdefghijklmnopqrstuvwx' -f $i)
}
[System.IO.Directory]::CreateDirectory((ToLongPath $deep)) | Out-Null
$leaf = Join-Path $deep 'leaf.txt'
[System.IO.File]::WriteAllText((ToLongPath $leaf), 'r1 deep leaf')
Log ('planted deep tree leaf = {0} length = {1}' -f $leaf, $leaf.Length)
Assert 'planted_deep_beyond_max_path' ($leaf.Length -gt 260) ('leaf_length={0}' -f $leaf.Length)
[System.IO.File]::WriteAllText((ToLongPath (Join-Path $plant 'top.txt')), 'r1 top')

$protected = $null
if ($Case -eq 'failure') {
  $protected = Join-Path $instDir 'protected-child'
  [System.IO.Directory]::CreateDirectory((ToLongPath $protected)) | Out-Null
  $ic = Invoke-Native 'icacls' @((ToLongPath $protected), '/deny', '*S-1-1-0:(D)')
  Log ('icacls deny DELETE on {0} -> {1}' -f $protected, ($ic -join ' | '))
  $denied = $false
  try { [System.IO.Directory]::Delete((ToLongPath $protected), $true) } catch { $denied = $true }
  if (-not $denied) {
    Log ('WARNING: the deny ACE did not hold; recreating the protected dir')
    [System.IO.Directory]::CreateDirectory((ToLongPath $protected)) | Out-Null
  }
  Assert 'protected_child_delete_denied' $denied ('delete_allowed={0}' -f (-not $denied))
}

# ------------------------------------------------------- install #2 (update)
$nsisLog = Join-Path $evidence ('{0}-{1}-{2}-install2-nsis.log' -f $Tag, $Case, $Expect)
$rc2 = RunInstaller $installerPath $nsisLog
Log ('install #2 (update) rc = {0}' -f $rc2)
Log ('nsis install log = {0}' -f $nsisLog)
Assert 'install2_rc0' ($rc2 -eq 0) ('rc={0}' -f $rc2)

# ------------------------------------------------------------------ observe
$pointer = RegVal $appKeyPath 'IaBackupDir'
$leftover = RegVal $appKeyPath 'IaLeftoverDir'
Log ('registry after update: IaBackupDir={0} IaLeftoverDir={1}' -f $pointer, $leftover)

$siblings = @(Get-ChildItem -LiteralPath $productsRoot -Directory | Where-Object { $_.Name -like ($new[0] + '.old-*') })
Log ('backup siblings after update = [{0}]' -f (($siblings | ForEach-Object { $_.Name }) -join ', '))
$backupGone = ($siblings.Count -eq 0)
$exeStillThere = Test-Path -LiteralPath $exe.FullName
$plantGoneFromNewInstall = -not (Test-Path -LiteralPath (Join-Path $instDir 'planted-deep'))

$expectBackupGone = (($Expect -eq 'fixed') -and ($Case -eq 'success'))
$expectLeftover = (($Expect -eq 'fixed') -and ($Case -eq 'failure'))

Assert 'backup_dir_removed' ($backupGone -eq $expectBackupGone) ('gone={0} expected={1}' -f $backupGone, $expectBackupGone)
Assert 'pointer_cleared' ([string]::IsNullOrEmpty($pointer)) ('IaBackupDir={0}' -f $pointer)
Assert 'leftover_recorded' ((-not [string]::IsNullOrEmpty($leftover)) -eq $expectLeftover) ('IaLeftoverDir={0} expected_present={1}' -f $leftover, $expectLeftover)
Assert 'update_payload_present' $exeStillThere ('exe_exists={0}' -f $exeStillThere)
Assert 'planted_moved_out_of_new_install' $plantGoneFromNewInstall ('planted_deep_in_new_install={0}' -f (-not $plantGoneFromNewInstall))

if ($expectLeftover -and $siblings.Count -gt 0) {
  Assert 'leftover_record_matches_dir' ($leftover -ieq $siblings[0].FullName) ('record={0} dir={1}' -f $leftover, $siblings[0].FullName)
}
if ($siblings.Count -gt 0) {
  $residual = $null
  try {
    $residual = [System.IO.Directory]::GetFileSystemEntries((ToLongPath $siblings[0].FullName), '*', [System.IO.SearchOption]::AllDirectories)
  } catch {
    Log ('residual enumeration stopped: {0}' -f $_.Exception.Message)
    $residual = @()
  }
  Log ('residual entry count = {0}' -f @($residual).Count)
  foreach ($r in @($residual | Select-Object -First 12)) {
    Log ('  residual: {0}' -f $r.Substring($siblings[0].FullName.Length))
  }
}

# ------------------------------------------- leftover-record lifetime (#901)
# The repaired promote keeps the record while the directory it names exists and
# drops it once that directory is gone. Both halves are driven here: unlock the
# leftover and update again (the record has to survive -- it may not be cleared
# by a delete that succeeds on some other directory), then remove the recorded
# directory and update once more (the record now points at nothing).
if ($Case -eq 'failure' -and $siblings.Count -gt 0) {
  $staleDir = $siblings[0].FullName
  $ace = Invoke-Native 'icacls' @((ToLongPath (Join-Path $staleDir 'protected-child')), '/remove:d', '*S-1-1-0', '/T', '/C')
  Log ('icacls remove deny under the leftover -> {0}' -f ($ace -join ' '))

  $rc3 = RunInstaller $installerPath $null
  Log ('install #3 rc = {0}' -f $rc3)
  Assert 'install3_rc0' ($rc3 -eq 0) ('rc={0}' -f $rc3)
  $leftover3 = RegVal $appKeyPath 'IaLeftoverDir'
  Assert 'record_kept_while_dir_exists' ((-not [string]::IsNullOrEmpty($leftover3)) -and ($leftover3 -ieq $staleDir)) ('IaLeftoverDir={0} dir={1}' -f $leftover3, $staleDir)
  Assert 'stale_dir_still_present' (Test-Path -LiteralPath $staleDir) ('exists={0}' -f (Test-Path -LiteralPath $staleDir))

  $removed = $false
  try {
    [System.IO.Directory]::Delete((ToLongPath $staleDir), $true)
    $removed = $true
  } catch {
    Log ('removing the recorded directory failed: {0}' -f $_.Exception.Message)
  }
  Assert 'stale_dir_removed_by_hand' $removed ('exists={0}' -f (Test-Path -LiteralPath $staleDir))

  $rc4 = RunInstaller $installerPath $null
  Log ('install #4 rc = {0}' -f $rc4)
  Assert 'install4_rc0' ($rc4 -eq 0) ('rc={0}' -f $rc4)
  $leftover4 = RegVal $appKeyPath 'IaLeftoverDir'
  Assert 'stale_record_cleared' ([string]::IsNullOrEmpty($leftover4)) ('IaLeftoverDir={0}' -f $leftover4)
}

# ------------------------------------------------------- registry dump (debug)
# Full value dump of both keys: the instrumented debug variant writes IaDbg*
# markers into the application key, and the stock old uninstaller may have
# deleted that key before customInstall ran -- the dump shows which happened.
foreach ($pair in @(@('app', $appKeyPath), @('uninstall', $keyPath))) {
  $dump = Get-ItemProperty -Path $pair[1] -ErrorAction SilentlyContinue
  if ($null -eq $dump) {
    Log ('regdump {0}: key absent' -f $pair[0])
    continue
  }
  foreach ($prop in $dump.PSObject.Properties) {
    if ($prop.Name -like 'PS*') { continue }
    Log ('regdump {0} {1} = {2}' -f $pair[0], $prop.Name, $prop.Value)
  }
}

# ------------------------------------------------------------------ cleanup
$deniedPaths = @()
if ($null -ne $protected) { $deniedPaths += $protected }
# A failed promote leaves the denied child inside the backup, so the ACE has to
# be lifted there as well before the leftover can be deleted.
foreach ($d in @($siblings)) { $deniedPaths += (Join-Path $d.FullName 'protected-child') }
foreach ($p in $deniedPaths) {
  if (-not (Test-Path -LiteralPath $p)) { continue }
  $ic2 = Invoke-Native 'icacls' @((ToLongPath $p), '/remove:d', '*S-1-1-0', '/T', '/C')
  Log ('icacls remove deny on {0} -> {1}' -f $p, ($ic2 -join ' | '))
}
foreach ($d in @($siblings)) {
  if (-not (Test-Path -LiteralPath $d.FullName)) { continue }
  try {
    [System.IO.Directory]::Delete((ToLongPath $d.FullName), $true)
    Log ('cleanup: removed leftover {0}' -f $d.FullName)
  } catch {
    Log ('cleanup FAILED for {0}: {1}' -f $d.FullName, $_.Exception.Message)
  }
}
$uninstaller = @(Get-ChildItem -LiteralPath $instDir -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue)[0]
if ($null -ne $uninstaller) {
  $rcu = RunInstaller $uninstaller.FullName $null
  Log ('uninstall rc = {0}' -f $rcu)
} else {
  Log ('WARNING: no uninstaller found in {0}' -f $instDir)
}
$dirGone = -not (Test-Path -LiteralPath $instDir)
Assert 'cleanup_installdir_gone' $dirGone ('installdir_exists={0}' -f (-not $dirGone))
$siblingsAfter = @(Get-ChildItem -LiteralPath $productsRoot -Directory | Where-Object { $_.Name -like ($new[0] + '.old-*') })
Assert 'cleanup_no_leftover' ($siblingsAfter.Count -eq 0) ('remaining=[{0}]' -f (($siblingsAfter | ForEach-Object { $_.Name }) -join ', '))

$realAfter = @(Get-ChildItem -LiteralPath $productsRoot -Directory |
  Where-Object { $_.Name -like 'Intelligence Agent*' } | ForEach-Object { $_.Name })
Assert 'real_product_dirs_untouched' (($realBefore -join ',') -eq ($realAfter -join ',')) ('before=[{0}] after=[{1}]' -f ($realBefore -join ', '), ($realAfter -join ', '))

Log ('DONE assertions_failed={0}' -f $script:failures)
if ($script:failures -gt 0) { exit 1 }
exit 0
