<#
  R1 (#901) end-to-end verification driver.

  Drives the isolated W-16 installer test artifact -- built from the production
  NSIS config by desktop/scripts/test-windows-installer.mjs --compile-only --
  through a real update cycle:

      install #1  ->  plant payload  ->  install #2 (stage + promote)  ->  assert

  Case success  : the planted payload is a >MAX_PATH deep tree, so promote can
                  only remove the previous version's tree with a working
                  long-path prefix.
  Case failure  : the planted payload contains a DELETE-denied child directory,
                  so promote cannot remove the backup: it must record the
                  leftover instead of orphaning it silently AND report rc 2
                  (#919 Q6 -- a kept backup is not a success; the pre-Q6 build
                  returned 0 here, the measured defect).
  Case junction : (#904 item 5) the planted payload contains a junction to a
                  directory outside the install directory. RMDir /r deletes
                  THROUGH a junction (measured, see ../nsis-probes/junction-probe),
                  so promote must refuse the backup, record it, return rc 2 and
                  leave BOTH the junction and its target intact.
  Case sweep    : (#904 item 4) drives the uninstaller's leftover sweep instead
                  of an update, with four hand-built siblings: one deletable
                  (the braced name iaStageApplication writes), one holding a
                  junction to a target outside the product directory, one whose
                  name has no braces (the shape an earlier fixture assumed --
                  the first real run corrected it), and one whose name fails
                  the shape outright. Two launches, because they do not report
                  alike: a plain launch (the stub copies itself, exit code 0 for
                  every artifact) must delete only the first sibling and keep
                  the other three, target intact; an in-place launch
                  ("/S _?=<dir>", value last and unquoted -- the form the stock
                  updater's own call uses; readings and the wrong invocations
                  in ../nsis-probes/launch-form-probe) must do the same and
                  return rc 2 on the fixed artifact, rc 0 on the pre-#904 one,
                  which has no sweep at all.

  The junction-bearing cases plant their target in a scratch directory under
  %TEMP% ("ia-r1-junction-<tag>") and remove it at the end; nothing else outside
  the "IA Installer Test *" product directories is written, and the driver
  asserts that the real product directories are untouched.

  -Expect names the artifact under test and therefore the expected behavior:
  'fixed' is the #904 build from the current tree; 'baseline' is the artifact
  built from the code before the change the case verifies -- pre-#901 for
  success/failure (its delete used a broken long-path prefix and never removed
  the backup) and pre-#904 for junction/sweep (its delete works, follows the
  junction into the target, and its uninstaller has no sweep at all). The
  baseline values are the measured defect, not a tolerance.

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
  [Parameter(Mandatory = $true)][ValidateSet('success', 'failure', 'junction', 'sweep')][string]$Case,
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

# No /O log switch here: NSIS only honours it on a stub built with
# NSIS_CONFIG_LOG, and the makensis this repo builds with has it off
# ("Error: LogSet specified, NSIS_CONFIG_LOG not defined", measured #919 Q10).
# electron-builder's template calls ${LogSet} only behind
# ENABLE_LOGGING_ELECTRON_BUILDER, which its build does not define either, so a
# log file can never appear and an /O path recorded in the evidence would be a
# dead pointer. The run is a plain /S launch; nothing reads a log.
function RunInstaller([string]$exe) {
  $p = Start-Process -FilePath $exe -ArgumentList '/S' -Wait -PassThru
  return $p.ExitCode
}

# Remove a junction entry by itself. cmd's rmdir removes the link and never the
# target; a recursive delete left to guess is exactly the hazard this driver
# measures, so the link is taken off the tree first and the caller refuses to
# recurse while it is still there. Returns $true when the entry is gone.
function Remove-LinkEntry([string]$p) {
  if (-not (Test-Path -LiteralPath $p)) { return $true }
  Invoke-Native 'cmd' @('/c', 'rmdir', $p) | Out-Null
  if (Test-Path -LiteralPath $p) {
    try { [System.IO.Directory]::Delete($p) } catch { Log ('link removal failed: {0}: {1}' -f $p, $_.Exception.Message) }
  }
  return -not (Test-Path -LiteralPath $p)
}

# Recursive delete in long-path form; logs and returns $false on failure.
function Remove-Tree([string]$p) {
  if (-not (Test-Path -LiteralPath $p)) { return $true }
  try {
    [System.IO.Directory]::Delete((ToLongPath $p), $true)
    return $true
  } catch {
    Log ('cleanup FAILED for {0}: {1}' -f $p, $_.Exception.Message)
    return $false
  }
}

function TestProductDirs([string]$root) {
  return @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'IA Installer Test*' } | ForEach-Object { $_.Name })
}

# The in-place form of an uninstaller launch. An NSIS uninstaller stub copies
# itself to %TEMP% (~nsu.tmp) and runs the copy; the copy's error level never
# reaches the launcher, so a plain launch reports the stub's 0 no matter what
# the section sets. With "/S _?=<dir>" the stub runs the file in place and the
# section's SetErrorLevel IS the process exit code -- the form the stock
# updater's own call uses (installUtil.nsh:
# ExecWait '"$uninstallerFileNameTemp" /S ... _?=$installationDir' $R0) and the
# only form whose exit code carries the sweep's report.
# The value has to reach the raw command line whole, last and UNQUOTED: NSIS
# takes the rest of the line, so a path with spaces needs no quotes. Two
# measured traps (../nsis-probes/launch-form-probe): a QUOTED value returns 2
# while the script never runs (no un.onInit marker, nothing deleted), and an
# array element goes through PowerShell's native argument quoting, which for a
# path with spaces wraps the token, hides "_?=" from NSIS and silently falls
# back to the stub form (rc 0, install dir removed). The line is therefore built
# as one string, and the caller still confirms the run really was in place.
function RunInstallerInPlace([string]$exe, [string]$dir) {
  $p = Start-Process -FilePath $exe -ArgumentList ('/S _?={0}' -f $dir) -Wait -PassThru
  return $p.ExitCode
}

# The four shapes the sweep has to tell apart, beside the install dir:
#   deletable - the braced name iaStageApplication writes, with a plain file:
#               the only one the sweep may remove;
#   junction  - braced, holding a junction to $Target outside the product
#               directory: the reparse scan must refuse it and both the link and
#               the target payload must survive;
#   unbraced  - the same prefix and a bare GUID (the shape an earlier fixture
#               assumed before the first real run corrected it): not a name this
#               installer writes, so it is kept;
#   decoy     - shape-invalid: enumerated and never touched.
function New-SweepFixture([string]$installDir, [string]$target) {
  $fixture = [pscustomobject]@{
    Deletable = "$installDir.old-{$([guid]::NewGuid().ToString())}"
    Junction  = "$installDir.old-{$([guid]::NewGuid().ToString())}"
    Unbraced  = "$installDir.old-$([guid]::NewGuid().ToString())"
    Decoy     = "$installDir.old-not-a-guid"
    Link      = ''
    Target    = $target
  }
  $fixture.Link = Join-Path $fixture.Junction 'sweep-junction'
  [System.IO.Directory]::CreateDirectory($fixture.Deletable) | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $fixture.Deletable 'plain.txt'), 'plain leftover')
  [System.IO.Directory]::CreateDirectory($fixture.Junction) | Out-Null
  New-Item -ItemType Junction -Path $fixture.Link -Target $target | Out-Null
  [System.IO.Directory]::CreateDirectory($fixture.Unbraced) | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $fixture.Unbraced 'plain.txt'), 'unbraced leftover')
  [System.IO.Directory]::CreateDirectory($fixture.Decoy) | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $fixture.Decoy 'plain.txt'), 'decoy leftover')
  return $fixture
}

# Take a fixture apart: the link comes off first (rmdir removes the link, never
# the target), then the trees — a recursive delete left to guess would destroy
# exactly what the assertions just checked.
function Remove-SweepFixture($fixture) {
  Remove-LinkEntry $fixture.Link | Out-Null
  Remove-Tree $fixture.Junction | Out-Null
  Remove-Tree $fixture.Deletable | Out-Null
  Remove-Tree $fixture.Unbraced | Out-Null
  Remove-Tree $fixture.Decoy | Out-Null
}

function Assert-SweepOutcome([string]$step, $fixture, [bool]$sweepFixed, [int]$rc, [int]$expectRc) {
  Assert ("{0}_uninstall_rc" -f $step) ($rc -eq $expectRc) ('rc={0} expected={1}' -f $rc, $expectRc)
  Assert ("{0}_deletable_removed" -f $step) ((-not (Test-Path -LiteralPath $fixture.Deletable)) -eq $sweepFixed) ('gone={0} expected={1}' -f (-not (Test-Path -LiteralPath $fixture.Deletable)), $sweepFixed)
  Assert ("{0}_junction_kept" -f $step) (Test-Path -LiteralPath $fixture.Junction) ('exists={0}' -f (Test-Path -LiteralPath $fixture.Junction))
  Assert ("{0}_junction_link_intact" -f $step) (Test-Path -LiteralPath $fixture.Link) ('link={0}' -f $fixture.Link)
  Assert ("{0}_decoy_kept" -f $step) (Test-Path -LiteralPath $fixture.Decoy) ('exists={0}' -f (Test-Path -LiteralPath $fixture.Decoy))
  # The unbraced shape is the one an earlier fixture assumed: no real backup
  # ever carried it, so the sweep must leave it alone (fail-closed both ways).
  Assert ("{0}_unbraced_kept" -f $step) (Test-Path -LiteralPath $fixture.Unbraced) ('exists={0}' -f (Test-Path -LiteralPath $fixture.Unbraced))
  # True for both artifacts, for different reasons (see the block comment).
  Assert ("{0}_junction_target_intact" -f $step) (Test-Path -LiteralPath (Join-Path $fixture.Target 'sweep-target-payload.txt')) ('target={0}' -f $fixture.Target)
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
$rc1 = RunInstaller $installerPath
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

# ------------------------------------------------------ case sweep (#904 item 4)
# The uninstaller's name-shape sweep is the authoritative discovery path for
# "<install dir>.old-{guid}" backup directories beside the install directory.
# This case builds four shapes by hand (see New-SweepFixture) and drives the real
# uninstaller twice, because the two launch forms do not report the same way:
#
#   step plain   - what a user or the Start menu does: the stub copies itself to
#                  %TEMP% and runs the copy, so the exit code is the stub's 0 for
#                  every artifact (measured: nsis-probes/semantics-probe case 10,
#                  and the artifact's own run). The sweep's work is what this
#                  step asserts: the deletable sibling gone, the other three
#                  kept, the junction target intact, $INSTDIR removed;
#   step inplace - "/S _?=<dir>" with the value last and unquoted, the form the
#                  stock updater's own call uses, where the section's
#                  SetErrorLevel reaches the caller: rc 2 when the sweep kept
#                  something (fixed artifact) and 0 when there is no sweep at
#                  all (pre-#904). Launched in place the uninstaller file and
#                  its directory survive (it runs from inside them and cannot
#                  delete itself), which is also how the run proves it really
#                  was in place. The form's readings, and the two wrong
#                  invocations that would hide in it, are measured in
#                  ../nsis-probes/launch-form-probe.
#
# The pre-#904 artifact has no sweep: its deletable sibling survives both steps.
# Its target payload survives too -- but only because nothing ran, not because
# anything refused; the discriminating assertions are the deletable sibling's
# removal and the in-place exit code.
if ($Case -eq 'sweep') {
  $sweepFixed = ($Expect -eq 'fixed')
  $scratch = Join-Path $env:TEMP ('ia-r1-junction-' + $Tag)
  if (Test-Path -LiteralPath $scratch) { Remove-Tree $scratch | Out-Null }
  [System.IO.Directory]::CreateDirectory($scratch) | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $scratch 'sweep-target-payload.txt'), 'r1 sweep target')

  $uninstaller = @(Get-ChildItem -LiteralPath $instDir -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue)[0]
  Assert 'sweep_uninstaller_found' ($null -ne $uninstaller) ('exe={0}' -f $(if ($uninstaller) { $uninstaller.Name } else { '<none>' }))
  if ($null -eq $uninstaller) {
    Log ('ABORT: no uninstaller in {0}' -f $instDir)
    Remove-Tree $scratch | Out-Null
    Log ('DONE assertions_failed={0}' -f $script:failures)
    exit 1
  }

  # ------------------------------------------------------------- step: plain
  $plainFixture = New-SweepFixture $instDir $scratch
  Log ('plain planted: deletable={0}' -f $plainFixture.Deletable)
  Log ('plain planted: junction={0} -> {1}' -f $plainFixture.Junction, $scratch)
  Log ('plain planted: unbraced={0}' -f $plainFixture.Unbraced)
  Log ('plain planted: decoy={0}' -f $plainFixture.Decoy)
  $rcu = RunInstaller $uninstaller.FullName
  Log ('plain uninstall rc = {0} (the stub reports 0 for every artifact)' -f $rcu)
  Assert-SweepOutcome 'sweep_plain' $plainFixture $sweepFixed $rcu 0
  Assert 'sweep_plain_installdir_gone' (-not (Test-Path -LiteralPath $instDir)) ('installdir_exists={0}' -f (Test-Path -LiteralPath $instDir))
  Remove-SweepFixture $plainFixture
  Assert 'sweep_plain_cleanup_all_gone' (-not (Test-Path -LiteralPath $plainFixture.Junction)) ('sibling_exists={0}' -f (Test-Path -LiteralPath $plainFixture.Junction))

  # ----------------------------------------------------------- step: in place
  # A fresh install for the in-place run: the plain run removed the product and
  # its uninstaller with it. The app id is fixed, so the directory name has to
  # come back identical -- if it does not, the fixture paths below would be
  # built beside a different directory than the one the sweep enumerates.
  $rc2 = RunInstaller $installerPath
  Log ('reinstall rc = {0}' -f $rc2)
  Assert 'sweep_reinstall_rc0' ($rc2 -eq 0) ('rc={0}' -f $rc2)
  $reinstalled = @(TestProductDirs $productsRoot)
  Assert 'sweep_reinstall_same_dir' (($reinstalled.Count -eq 1) -and ($reinstalled[0] -eq $new[0])) ('dirs=[{0}] expected={1}' -f ($reinstalled -join ', '), $new[0])
  $inplaceUninstaller = @(Get-ChildItem -LiteralPath $instDir -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue)[0]
  Assert 'sweep_inplace_uninstaller_found' ($null -ne $inplaceUninstaller) ('exe={0}' -f $(if ($inplaceUninstaller) { $inplaceUninstaller.Name } else { '<none>' }))
  if ($null -eq $inplaceUninstaller) {
    Log ('ABORT: the reinstall left no uninstaller in {0}' -f $instDir)
    Remove-Tree $scratch | Out-Null
    Log ('DONE assertions_failed={0}' -f $script:failures)
    exit 1
  }
  $inplaceFixture = New-SweepFixture $instDir $scratch
  Log ('inplace planted: deletable={0}' -f $inplaceFixture.Deletable)
  Log ('inplace planted: junction={0} -> {1}' -f $inplaceFixture.Junction, $scratch)
  $rcu = RunInstallerInPlace $inplaceUninstaller.FullName $instDir
  Log ('in-place uninstall rc = {0} expected = {1}' -f $rcu, $(if ($sweepFixed) { 2 } else { 0 }))
  # Fail loud on a degraded invocation first: if "_?=" did not take effect the
  # stub ran the copy, the uninstaller file is gone with the directory, and the
  # rc below would be read as a sweep reading it never was.
  $inPlaceConfirmed = Test-Path -LiteralPath $inplaceUninstaller.FullName
  Assert 'sweep_inplace_confirmed' $inPlaceConfirmed ('uninstaller_exists={0}' -f $inPlaceConfirmed)
  Assert-SweepOutcome 'sweep_inplace' $inplaceFixture $sweepFixed $rcu $(if ($sweepFixed) { 2 } else { 0 })
  if ($inPlaceConfirmed) {
    Assert 'sweep_inplace_installdir_kept' (Test-Path -LiteralPath $instDir) ('installdir_exists={0}' -f (Test-Path -LiteralPath $instDir))
    $payloadLeft = @(Get-ChildItem -LiteralPath $instDir -Filter '*.exe' | Where-Object { $_.Name -notlike 'Uninstall*' })
    Assert 'sweep_inplace_payload_removed' ($payloadLeft.Count -eq 0) ('left=[{0}]' -f ($payloadLeft.Name -join ', '))
  }
  Remove-SweepFixture $inplaceFixture
  Remove-Tree $scratch | Out-Null
  Assert 'sweep_cleanup_all_gone' (-not (Test-Path -LiteralPath $inplaceFixture.Junction)) ('sibling_exists={0}' -f (Test-Path -LiteralPath $inplaceFixture.Junction))
  # The in-place run leaves the directory and its uninstaller behind by design;
  # take them off so the products root is clean for the next case.
  Remove-Tree $instDir | Out-Null
  Assert 'sweep_cleanup_installdir_removed' (-not (Test-Path -LiteralPath $instDir)) ('installdir_exists={0}' -f (Test-Path -LiteralPath $instDir))

  $realAfter = @(Get-ChildItem -LiteralPath $productsRoot -Directory |
    Where-Object { $_.Name -like 'Intelligence Agent*' } | ForEach-Object { $_.Name })
  Assert 'real_product_dirs_untouched' (($realBefore -join ',') -eq ($realAfter -join ',')) ('before=[{0}] after=[{1}]' -f ($realBefore -join ', '), ($realAfter -join ', '))

  Log ('DONE assertions_failed={0}' -f $script:failures)
  if ($script:failures -gt 0) { exit 1 }
  exit 0
}

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

$junctionTarget = $null
$junctionTargetPayload = $null
if ($Case -eq 'junction') {
  # The link points OUTSIDE the install directory: that is what makes a delete
  # through it destructive instead of merely incomplete. The scratch target
  # lives in %TEMP% so the payload is plainly outside anything the installer
  # owns, and the driver removes it in cleanup.
  $junctionTarget = Join-Path $env:TEMP ('ia-r1-junction-' + $Tag)
  if (Test-Path -LiteralPath $junctionTarget) { Remove-Tree $junctionTarget | Out-Null }
  [System.IO.Directory]::CreateDirectory($junctionTarget) | Out-Null
  $junctionTargetPayload = Join-Path $junctionTarget 'target-payload.txt'
  [System.IO.File]::WriteAllText($junctionTargetPayload, 'r1 junction target')
  $junctionLink = Join-Path $instDir 'planted-junction'
  New-Item -ItemType Junction -Path $junctionLink -Target $junctionTarget | Out-Null
  $attrs = [System.IO.File]::GetAttributes($junctionLink)
  Log ('planted junction {0} -> {1} attrs={2}' -f $junctionLink, $junctionTarget, $attrs)
  Assert 'junction_planted_reparse' (($attrs -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) ('attrs={0}' -f $attrs)
  Assert 'junction_target_payload_planted' (Test-Path -LiteralPath $junctionTargetPayload) ('payload={0}' -f $junctionTargetPayload)
}

# ------------------------------------------------- per-case expectations (#904)
# What the artifact under test must do in this case. The 'baseline' values are
# the measured defect of the pre-change artifact (see the header): the driver is
# run against both artifacts and the pair is the before/after evidence.
$isFixed = ($Expect -eq 'fixed')
$expectRc2 = 0
$expectBackupGone = $false
$expectLeftover = $false
$expectTargetIntact = $false
switch ($Case) {
  'success' {
    # The pre-#901 baseline delete used a broken long-path prefix, so it never
    # removed a >MAX_PATH backup; the fixed build does, records nothing and
    # returns 0.
    $expectBackupGone = $isFixed
  }
  'failure' {
    # The denied child makes the delete itself fail: both builds keep the
    # backup, only the fixed one records it. A kept backup is not a success:
    # the fixed build reports rc 2 (#919 Q6 -- the same reading the refusal
    # cases and the uninstaller sweep use), while the baseline artifact has no
    # such exit code and keeps its measured rc 0.
    $expectLeftover = $isFixed
    if ($isFixed) { $expectRc2 = 2 }
  }
  'junction' {
    # Scan refusal: the backup is kept and recorded, the exit code is 2, and
    # nothing was deleted through the link. The pre-#904 build deleted THROUGH
    # the junction -- the target payload went with it -- and returned 0.
    $expectBackupGone = -not $isFixed
    $expectLeftover = $isFixed
    $expectTargetIntact = $isFixed
    if ($isFixed) { $expectRc2 = 2 }
  }
}

# ------------------------------------------------------- install #2 (update)
$rc2 = RunInstaller $installerPath
Log ('install #2 (update) rc = {0}' -f $rc2)
Assert 'install2_rc_expected' ($rc2 -eq $expectRc2) ('rc={0} expected={1}' -f $rc2, $expectRc2)

# ------------------------------------------------------------------ observe
$pointer = RegVal $appKeyPath 'IaBackupDir'
$leftover = RegVal $appKeyPath 'IaLeftoverDir'
Log ('registry after update: IaBackupDir={0} IaLeftoverDir={1}' -f $pointer, $leftover)

$siblings = @(Get-ChildItem -LiteralPath $productsRoot -Directory | Where-Object { $_.Name -like ($new[0] + '.old-*') })
Log ('backup siblings after update = [{0}]' -f (($siblings | ForEach-Object { $_.Name }) -join ', '))
$backupGone = ($siblings.Count -eq 0)
$exeStillThere = Test-Path -LiteralPath $exe.FullName
$plantGoneFromNewInstall = -not (Test-Path -LiteralPath (Join-Path $instDir 'planted-deep'))

Assert 'backup_dir_removed' ($backupGone -eq $expectBackupGone) ('gone={0} expected={1}' -f $backupGone, $expectBackupGone)
Assert 'pointer_cleared' ([string]::IsNullOrEmpty($pointer)) ('IaBackupDir={0}' -f $pointer)
Assert 'leftover_recorded' ((-not [string]::IsNullOrEmpty($leftover)) -eq $expectLeftover) ('IaLeftoverDir={0} expected_present={1}' -f $leftover, $expectLeftover)
Assert 'update_payload_present' $exeStillThere ('exe_exists={0}' -f $exeStillThere)
Assert 'planted_moved_out_of_new_install' $plantGoneFromNewInstall ('planted_deep_in_new_install={0}' -f (-not $plantGoneFromNewInstall))

if ($Case -eq 'junction') {
  # The link and its target are the point of this case: the fixed build must
  # still hold the link in the backup, and the pre-#904 build is the one that
  # emptied the target (that half is the defect this pair of runs measures).
  $linkPath = $null
  if ($siblings.Count -gt 0) { $linkPath = Join-Path $siblings[0].FullName 'planted-junction' }
  $linkKept = ($null -ne $linkPath) -and (Test-Path -LiteralPath $linkPath)
  Assert 'junction_kept_in_backup' ($linkKept -eq $isFixed) ('link={0} exists={1} expected={2}' -f $linkPath, $linkKept, $isFixed)
  $targetIntact = Test-Path -LiteralPath $junctionTargetPayload
  Assert 'junction_target_payload_intact' ($targetIntact -eq $expectTargetIntact) ('exists={0} expected={1}' -f $targetIntact, $expectTargetIntact)
  $linkInNewInstall = Test-Path -LiteralPath (Join-Path $instDir 'planted-junction')
  Assert 'junction_absent_from_new_install' (-not $linkInNewInstall) ('exists={0}' -f $linkInNewInstall)
}

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

  $rc3 = RunInstaller $installerPath
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

  $rc4 = RunInstaller $installerPath
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
  # A reparse point inside the backup is taken off the tree on its own first:
  # the junction case leaves one there on purpose, and a recursive delete that
  # followed it would destroy the target the run just asserted is intact.
  if ($Case -eq 'junction') {
    $link = Join-Path $d.FullName 'planted-junction'
    if (-not (Remove-LinkEntry $link)) {
      Log ('cleanup: leaving {0}; its link could not be removed' -f $d.FullName)
      continue
    }
  }
  if (Remove-Tree $d.FullName) { Log ('cleanup: removed leftover {0}' -f $d.FullName) }
}
# The junction case plants its target in a %TEMP% scratch directory; it is
# removed here, after the assertions above have read it.
if ($null -ne $junctionTarget) {
  Remove-Tree $junctionTarget | Out-Null
  Assert 'cleanup_junction_target_gone' (-not (Test-Path -LiteralPath $junctionTarget)) ('target={0}' -f $junctionTarget)
}
$uninstaller = @(Get-ChildItem -LiteralPath $instDir -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue)[0]
if ($null -ne $uninstaller) {
  $rcu = RunInstaller $uninstaller.FullName
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
