<#
  #915 real Windows hard-interruption recovery driver.

  Builds no product code. It uses the isolated test installer built from the
  production NSIS configuration, force-kills an update after the directory
  swap, then drives the recovery choice through Windows UI Automation.

  Usage:
    powershell -File drive-r3-recovery.ps1 -Installer <installer-test.exe>
    powershell -File drive-r3-recovery.ps1 -Installer <installer-test.exe> -AbsentInstallDirectory
    powershell -File drive-r3-recovery.ps1 -Installer <installer-test.exe> -BlockRollback
    powershell -File drive-r3-recovery.ps1 -Installer <installer-test.exe> -Choice continue
    powershell -File drive-r3-recovery.ps1 -Installer <installer-test.exe> -Choice continue -AfterContinue cancel
    powershell -File drive-r3-recovery.ps1 -Installer <installer-test.exe> -Choice continue -AfterContinue hard-kill-restore
    powershell -File drive-r3-recovery.ps1 -Installer <installer-test.exe> -Choice continue -FailLeftoverRecord
    powershell -File drive-r3-recovery.ps1 -Installer <installer-test.exe> -Choice continue -FailLeftoverRecord -LeaveFailurePromptOpen
    powershell -File drive-r3-recovery.ps1 -Installer <installer-test.exe> -Choice silent

  The test app uses a unique app id. It only installs under
  %LOCALAPPDATA%\Programs\IA Installer Test* and checks that the real product
  directories and the isolated test registry key are clean afterward.

  ASCII only (Windows PowerShell 5.1 reads .ps1 as ANSI).
#>
param(
  [Parameter(Mandatory = $true)][string]$Installer,
  [ValidateSet('restore', 'continue', 'silent')][string]$Choice = 'restore',
  [ValidateSet('complete', 'cancel', 'hard-kill-restore')][string]$AfterContinue = 'complete',
  [string]$EvidenceDir = '',
  [switch]$AbsentInstallDirectory,
  [switch]$BlockRollback,
  [switch]$FailLeftoverRecord,
  [switch]$LeaveFailurePromptOpen
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

if ([string]::IsNullOrEmpty($EvidenceDir)) { $EvidenceDir = Join-Path $env:TEMP 'ia-r3-recovery\evidence' }
New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
$rollbackBlockPath = ''
$rollbackTarget = ''
$rollbackMarker = ''
if (($AbsentInstallDirectory -or $BlockRollback) -and $Choice -ne 'restore') {
  throw 'restore fixtures require -Choice restore'
}
if ($AfterContinue -ne 'complete' -and $Choice -ne 'continue') {
  throw '-AfterContinue requires -Choice continue'
}
if ($AbsentInstallDirectory -and $BlockRollback) {
  throw 'choose only one restore fixture'
}
if ($FailLeftoverRecord -and $Choice -ne 'continue') {
  throw '-FailLeftoverRecord requires -Choice continue'
}
if ($LeaveFailurePromptOpen -and -not $FailLeftoverRecord) {
  throw '-LeaveFailurePromptOpen requires -FailLeftoverRecord'
}
$logPath = Join-Path $EvidenceDir ('recovery-{0}-{1}.log' -f $Choice, (Get-Date -Format 'yyyyMMdd-HHmmss'))
$script:failures = 0

function Log([string]$message) {
  $line = ('{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message)
  Write-Host $line
  Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Assert([string]$name, [bool]$ok, [string]$detail) {
  $verdict = if ($ok) { 'PASS' } else { 'FAIL' }
  if (-not $ok) { $script:failures = $script:failures + 1 }
  Log ('ASSERT {0} {1} {2}' -f $verdict, $name, $detail)
}

function Invoke-Installer([string]$path, [string]$arguments) {
  return Start-Process -FilePath $path -ArgumentList $arguments -Wait -PassThru
}

function Get-ProcessWindows([int]$processId) {
  $root = [System.Windows.Automation.AutomationElement]::RootElement
  $windows = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
  )
  foreach ($window in $windows) {
    try {
      if ($window.Current.ProcessId -ne $processId) { continue }
      $elements = $window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
      )
      $names = @($window.Current.Name)
      $buttons = @()
      foreach ($element in $elements) {
        $name = $element.Current.Name
        if (-not [string]::IsNullOrWhiteSpace($name)) { $names += $name }
        if ($element.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button) {
          $buttons += $element
        }
      }
      [pscustomobject]@{ Window = $window; Text = ($names -join "`n"); Buttons = $buttons }
    } catch {
      # A window can disappear between UIA enumeration and property access.
    }
  }
}

function Invoke-Button($windowRecord, [string]$namePattern) {
  foreach ($button in $windowRecord.Buttons) {
    try {
      $buttonName = $button.Current.Name
      $enabled = $button.Current.IsEnabled
      $matchesChoice = $buttonName -match $namePattern
      if ($matchesChoice) { Log ('button candidate name={0} enabled={1}' -f $buttonName, $enabled) }
      if ($enabled -and $matchesChoice) {
        try {
          $pattern = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
          $pattern.Invoke()
        } catch {
          Log ('InvokePattern unavailable for {0}: {1}' -f $buttonName, $_.Exception.Message)
          $windowRecord.Window.SetFocus()
          $button.SetFocus()
          [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
        }
        return $buttonName
      }
    } catch {
      Log ('button inspection failed: {0}' -f $_.Exception.Message)
      # Keep looking when a button goes away as the wizard changes pages.
    }
  }
  return $null
}

function Find-RecoveryPrompt([int]$processId) {
  foreach ($window in Get-ProcessWindows $processId) {
    if ($window.Text -match '(?is)(interrupted.*restore.*previous version.*fresh installation|\u4e0a\u6b21\u66f4\u65b0\u5df2\u4e2d\u65ad.*\u6062\u590d\u65e7\u7248\u672c.*\u7ee7\u7eed\u5168\u65b0\u5b89\u88c5)') {
      return $window
    }
  }
  return $null
}

function Find-RecoveryFailurePrompt([int]$processId) {
  foreach ($window in Get-ProcessWindows $processId) {
    if ($window.Text -match '(?is)(Could not restore the previous version automatically|\u65e0\u6cd5\u81ea\u52a8\u6062\u590d\u65e7\u7248\u672c|The installer could not record recovery information|\u5b89\u88c5\u7a0b\u5e8f\u65e0\u6cd5\u8bb0\u5f55\u6062\u590d\u4fe1\u606f)') {
      return $window
    }
  }
  return $null
}

function Wait-StockWelcomePage([System.Diagnostics.Process]$process, [datetime]$deadline) {
  while ((Get-Date) -lt $deadline -and -not $process.HasExited) {
    foreach ($window in Get-ProcessWindows $process.Id) {
      if ($window.Text -match '(?is)(welcome|\u6b22\u8fce)' -and
          $window.Text -notmatch '(?is)(interrupted.*restore.*previous version.*fresh installation|\u4e0a\u6b21\u66f4\u65b0\u5df2\u4e2d\u65ad.*\u6062\u590d\u65e7\u7248\u672c.*\u7ee7\u7eed\u5168\u65b0\u5b89\u88c5)') {
        return $window
      }
    }
    Start-Sleep -Milliseconds 100
  }
  return $null
}

function Find-CancelConfirmationPrompt([int]$processId) {
  foreach ($window in Get-ProcessWindows $processId) {
    if ($window.Text -match '(?is)(are you sure.*cancel|cancel.*installation|\u786e\u5b9a.*\u53d6\u6d88|\u53d6\u6d88.*\u5b89\u88c5)') {
      return $window
    }
  }
  return $null
}

function Wait-RecoveryFailurePrompt([System.Diagnostics.Process]$process, [datetime]$deadline) {
  while ((Get-Date) -lt $deadline -and -not $process.HasExited) {
    $prompt = Find-RecoveryFailurePrompt $process.Id
    if ($null -ne $prompt) { return $prompt }
    Start-Sleep -Milliseconds 100
  }
  return $null
}

function Acknowledge-RecoveryFailurePrompt($prompt, [string]$assertionName) {
  $acknowledged = Invoke-Button $prompt '^(OK|\u786e\u5b9a)(\(.*\))?$'
  Assert $assertionName ($null -ne $acknowledged) ('button={0}' -f $acknowledged)
}

function Get-RegistryValue([string]$path, [string]$name) {
  $item = Get-ItemProperty -Path $path -Name $name -ErrorAction SilentlyContinue
  if ($null -eq $item) { return '' }
  return [string]$item.$name
}

$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$artifactRoot = Split-Path (Split-Path $installerPath -Parent) -Parent
$payloadScript = Get-Content -LiteralPath (Join-Path $artifactRoot 'payload.nsi') -Raw
if ($payloadScript -notmatch '(?m)^Name "([^"]+)"') { throw 'test product name missing from payload.nsi' }
$productName = $Matches[1]
if ($productName -notlike 'IA Installer Test *') { throw "refusing non-test product: $productName" }
$payloadExe = Get-ChildItem -LiteralPath (Join-Path $artifactRoot 'payload') -Filter '*.exe' | Select-Object -First 1
if ($null -eq $payloadExe) { throw 'isolated app executable missing' }

$productsRoot = Join-Path $env:LOCALAPPDATA 'Programs'
$installDir = Join-Path $productsRoot $productName
$appPath = Join-Path $installDir $payloadExe.Name
$shortcutRoots = @(
  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'),
  [Environment]::GetFolderPath('Desktop')
)
$testDirsBefore = @(Get-ChildItem -LiteralPath $productsRoot -Directory | Where-Object Name -like 'IA Installer Test*' | ForEach-Object FullName)
$realDirsBefore = @(Get-ChildItem -LiteralPath $productsRoot -Directory | Where-Object Name -like 'Intelligence Agent*' | ForEach-Object FullName)
if ($testDirsBefore -contains $installDir) { throw "test fixture already exists: $installDir" }

function Assert-NoTestShortcuts([string]$phase) {
  $shortcuts = @(
    foreach ($root in $shortcutRoots) {
      if (Test-Path -LiteralPath $root) {
        Get-ChildItem -LiteralPath $root -Filter ($productName + '.lnk') -File -Recurse -Force -ErrorAction SilentlyContinue
      }
    }
  )
  Assert ('no_test_shortcuts_{0}' -f $phase) ($shortcuts.Count -eq 0) ('found=[{0}]' -f (($shortcuts | ForEach-Object FullName) -join ', '))
}

$appKeyPath = ''
$backupDir = ''
$stageProcess = $null
$recoveryProcess = $null
$finalChoice = $Choice
Log ('=== R3 hard-interruption recovery; choice={0} after_continue={1} ===' -f $Choice, $AfterContinue)
Log ('installer={0} product={1} app={2}' -f $installerPath, $productName, $payloadExe.Name)

try {
  $first = Invoke-Installer $installerPath '/S'
  Assert 'initial_install_rc0' ($first.ExitCode -eq 0) ('rc={0}' -f $first.ExitCode)
  Assert 'initial_app_present' (Test-Path -LiteralPath $appPath) ('exists={0}' -f (Test-Path -LiteralPath $appPath))
  Assert-NoTestShortcuts 'after_initial_install'

  $appKey = Get-ChildItem 'HKCU:\Software' -ErrorAction SilentlyContinue | Where-Object {
    $location = (Get-ItemProperty -Path $_.PSPath -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
    ($null -ne $location) -and ($location.TrimEnd('\') -ieq $installDir.TrimEnd('\'))
  } | Select-Object -First 1
  if ($null -eq $appKey) { throw 'isolated app registry key not found' }
  $appKeyPath = 'HKCU:\Software\' + $appKey.PSChildName

  # The interactive installer pauses at its first page after customInit stages
  # the existing directory. Force termination only after observing that state.
  $stageProcess = Start-Process -FilePath $installerPath -PassThru
  $stageDeadline = (Get-Date).AddSeconds(20)
  while ((Get-Date) -lt $stageDeadline -and -not $stageProcess.HasExited) {
    $backups = @(Get-ChildItem -LiteralPath $productsRoot -Directory -Filter ($productName + '.old-*') -ErrorAction SilentlyContinue)
    if ($backups.Count -gt 0 -and -not (Test-Path -LiteralPath $appPath)) {
      $backupDir = $backups[0].FullName
      Log ('hard-kill state backup={0}' -f $backupDir)
      Stop-Process -Id $stageProcess.Id -Force
      $stageProcess.WaitForExit()
      break
    }
    Start-Sleep -Milliseconds 5
  }
  $backupDir = if ($backupDir) { $backupDir } else {
    $remaining = @(Get-ChildItem -LiteralPath $productsRoot -Directory -Filter ($productName + '.old-*') -ErrorAction SilentlyContinue)
    if ($remaining.Count -gt 0) { $remaining[0].FullName } else { '' }
  }
  $pointerAfterKill = Get-RegistryValue $appKeyPath 'IaBackupDir'
  $backupApp = ($backupDir -ne '') -and (Test-Path -LiteralPath (Join-Path $backupDir $payloadExe.Name))
  Assert 'hard_kill_left_backup_and_pointer' (($backupApp) -and ($pointerAfterKill -ieq $backupDir)) `
      ('backup_app={0} pointer_matches={1}' -f $backupApp, ($pointerAfterKill -ieq $backupDir))
  Assert 'hard_kill_removed_current_app' (-not (Test-Path -LiteralPath $appPath)) ('app_exists={0}' -f (Test-Path -LiteralPath $appPath))
  if ($AbsentInstallDirectory) {
    if (Test-Path -LiteralPath $installDir) {
      $partialEntries = @(Get-ChildItem -LiteralPath $installDir -Force)
      Assert 'pre_restore_install_dir_empty' ($partialEntries.Count -eq 0) ('entry_count={0}' -f $partialEntries.Count)
      if ($partialEntries.Count -eq 0) { [System.IO.Directory]::Delete($installDir, $false) }
    }
    Assert 'hard_kill_state_install_dir_absent' (-not (Test-Path -LiteralPath $installDir)) ('exists={0}' -f (Test-Path -LiteralPath $installDir))
  }
  if ($BlockRollback) {
    if (-not (Test-Path -LiteralPath $installDir)) { [System.IO.Directory]::CreateDirectory($installDir) | Out-Null }
    $rollbackBlockPath = Join-Path $installDir 'rollback-block'
    $rollbackTarget = Join-Path $EvidenceDir ('rollback-target-{0}' -f [Guid]::NewGuid().ToString('N'))
    [System.IO.Directory]::CreateDirectory($rollbackTarget) | Out-Null
    $rollbackMarker = Join-Path $rollbackTarget 'must-survive.txt'
    [System.IO.File]::WriteAllText($rollbackMarker, 'untouched')
    New-Item -ItemType Junction -Path $rollbackBlockPath -Target $rollbackTarget | Out-Null
    $isReparsePoint = ((Get-Item -LiteralPath $rollbackBlockPath).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    Assert 'rollback_block_fixture_created' $isReparsePoint ('path={0}' -f $rollbackBlockPath)
  }
  if ($AbsentInstallDirectory) {
    New-ItemProperty -Path $appKeyPath -Name 'IaTestRemoveInstallDirectory' -PropertyType String -Value '1' -Force | Out-Null
  }
  if ($FailLeftoverRecord) {
    New-ItemProperty -Path $appKeyPath -Name 'IaTestFailLeftoverRecord' -PropertyType String -Value '1' -Force | Out-Null
  }

  $prompt = $null
  if ($Choice -eq 'silent') {
    $recoveryProcess = Invoke-Installer $installerPath '/S'
    Assert 'silent_retry_exit_2' ($recoveryProcess.ExitCode -eq 2) ('rc={0}' -f $recoveryProcess.ExitCode)
  } else {
    $recoveryProcess = Start-Process -FilePath $installerPath -PassThru
    $promptDeadline = (Get-Date).AddSeconds(12)
    while ((Get-Date) -lt $promptDeadline -and -not $recoveryProcess.HasExited) {
      $prompt = Find-RecoveryPrompt $recoveryProcess.Id
      if ($null -ne $prompt) { break }
      Start-Sleep -Milliseconds 150
    }
    Assert 'recovery_choice_shown' ($null -ne $prompt) ('prompt_found={0}' -f ($null -ne $prompt))

    if ($null -ne $prompt) {
      if ($AbsentInstallDirectory) {
        Assert 'recovery_entry_install_dir_absent' (-not (Test-Path -LiteralPath $installDir)) ('exists={0}' -f (Test-Path -LiteralPath $installDir))
      }
      Log ('prompt buttons=[{0}]' -f (($prompt.Buttons | ForEach-Object { $_.Current.Name }) -join ', '))
      $choicePattern = if ($Choice -eq 'restore') { '^(Restore previous version|\u6062\u590d\u65e7\u7248\u672c)$' } else { '^(Continue with a fresh installation|\u7ee7\u7eed\u5168\u65b0\u5b89\u88c5)$' }
      $clicked = Invoke-Button $prompt $choicePattern
      Assert 'recovery_choice_accepted' ($null -ne $clicked) ('choice={0} button={1}' -f $Choice, $clicked)

      if ($BlockRollback -and $Choice -eq 'restore' -and $null -ne $clicked) {
        $failureDeadline = (Get-Date).AddSeconds(8)
        $failurePrompt = Wait-RecoveryFailurePrompt $recoveryProcess $failureDeadline
        Assert 'rollback_failure_prompt_shown' ($null -ne $failurePrompt) ('prompt_found={0}' -f ($null -ne $failurePrompt))
        if ($null -ne $failurePrompt) {
          Acknowledge-RecoveryFailurePrompt $failurePrompt 'rollback_failure_prompt_acknowledged'
        }
      }

      if ($Choice -eq 'continue' -and $AfterContinue -ne 'complete' -and $null -ne $clicked) {
        $continuedPointer = Get-RegistryValue $appKeyPath 'IaBackupDir'
        Assert 'continue_choice_keeps_recovery_pointer_armed' ($continuedPointer -ieq $backupDir) ('IaBackupDir={0}' -f $continuedPointer)
        $welcomePage = Wait-StockWelcomePage $recoveryProcess (Get-Date).AddSeconds(12)
        Assert 'continue_choice_reaches_stock_welcome_page' ($null -ne $welcomePage) ('page_found={0}' -f ($null -ne $welcomePage))

        if ($AfterContinue -eq 'cancel') {
          if ($null -ne $welcomePage) {
            $cancelled = Invoke-Button $welcomePage '^(Cancel|&Cancel|\u53d6\u6d88.*)$'
            Assert 'continue_cancel_button_accepted' ($null -ne $cancelled) ('button={0}' -f $cancelled)
          } else {
            $cancelled = $null
          }
          if ($null -ne $cancelled) {
            $cancelPromptDeadline = (Get-Date).AddSeconds(5)
            $cancelPrompt = $null
            while ((Get-Date) -lt $cancelPromptDeadline -and -not $recoveryProcess.HasExited) {
              $cancelPrompt = Find-CancelConfirmationPrompt $recoveryProcess.Id
              if ($null -ne $cancelPrompt) { break }
              Start-Sleep -Milliseconds 100
            }
            if ($null -ne $cancelPrompt) {
              $confirmed = Invoke-Button $cancelPrompt '^(Yes|\u662f.*)$'
              Assert 'continue_cancel_confirmed' ($null -ne $confirmed) ('button={0}' -f $confirmed)
            }
            $recoveryProcess.WaitForExit(15000)
          }
          Assert 'continue_cancel_exits_installer' $recoveryProcess.HasExited ('process_exited={0}' -f $recoveryProcess.HasExited)
          $finalChoice = 'restore'
        } else {
          Stop-Process -Id $recoveryProcess.Id -Force
          $recoveryProcess.WaitForExit()
          $pointerAfterContinueKill = Get-RegistryValue $appKeyPath 'IaBackupDir'
          Assert 'continue_hard_kill_retains_backup_pointer' ($pointerAfterContinueKill -ieq $backupDir) ('IaBackupDir={0}' -f $pointerAfterContinueKill)
          Assert 'continue_hard_kill_retains_backup_app' (Test-Path -LiteralPath (Join-Path $backupDir $payloadExe.Name)) ('backup={0}' -f $backupDir)
          Assert 'continue_hard_kill_leaves_app_absent' (-not (Test-Path -LiteralPath $appPath)) ('app_exists={0}' -f (Test-Path -LiteralPath $appPath))

          $recoveryProcess = Start-Process -FilePath $installerPath -PassThru
          $retryPrompt = $null
          $retryDeadline = (Get-Date).AddSeconds(12)
          while ((Get-Date) -lt $retryDeadline -and -not $recoveryProcess.HasExited) {
            $retryPrompt = Find-RecoveryPrompt $recoveryProcess.Id
            if ($null -ne $retryPrompt) { break }
            Start-Sleep -Milliseconds 150
          }
          Assert 'continue_hard_kill_recovery_prompt_shown' ($null -ne $retryPrompt) ('prompt_found={0}' -f ($null -ne $retryPrompt))
          if ($null -ne $retryPrompt) {
            $restoreClicked = Invoke-Button $retryPrompt '^(Restore previous version|\u6062\u590d\u65e7\u7248\u672c)$'
            Assert 'continue_hard_kill_restore_choice_accepted' ($null -ne $restoreClicked) ('button={0}' -f $restoreClicked)
            if ($null -ne $restoreClicked) {
              $recoveryProcess.WaitForExit(15000)
              Assert 'continue_hard_kill_restore_exits' $recoveryProcess.HasExited ('process_exited={0}' -f $recoveryProcess.HasExited)
              if ($recoveryProcess.HasExited) {
                Assert 'continue_hard_kill_restore_exit_0' ($recoveryProcess.ExitCode -eq 0) ('rc={0}' -f $recoveryProcess.ExitCode)
              }
            }
          }
          $finalChoice = 'restore'
        }
      } elseif ($Choice -eq 'continue' -and $FailLeftoverRecord -and $null -ne $clicked) {
        $failureDeadline = (Get-Date).AddSeconds(8)
        $failurePrompt = Wait-RecoveryFailurePrompt $recoveryProcess $failureDeadline
        Assert 'leftover_record_failure_prompt_shown' ($null -ne $failurePrompt) ('prompt_found={0}' -f ($null -ne $failurePrompt))
        if ($null -ne $failurePrompt) {
          if ($LeaveFailurePromptOpen) {
            Log 'leaving recovery failure prompt open to exercise the exit timeout'
          } else {
            Acknowledge-RecoveryFailurePrompt $failurePrompt 'leftover_record_failure_prompt_acknowledged'
          }
        }
        $recoveryProcess.WaitForExit(15000)
        $recoveryExited = $recoveryProcess.HasExited
        $recoveryExitCode = if ($recoveryExited) { $recoveryProcess.ExitCode } else { 'still running' }
        Assert 'leftover_record_failure_exit_2' ($recoveryExited -and $recoveryExitCode -eq 2) ('exited={0} rc={1}' -f $recoveryExited, $recoveryExitCode)
      } elseif ($Choice -eq 'continue' -and $null -ne $clicked) {
        # The stock assisted installer uses Next, Install, and Finish buttons.
        # Drive those public wizard controls; wait through the progress page.
        $installDeadline = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $installDeadline -and -not $recoveryProcess.HasExited) {
          $clickedWizard = $null
          foreach ($window in Get-ProcessWindows $recoveryProcess.Id) {
            $clickedWizard = Invoke-Button $window '^(Next\s*>|Next|Install|Finish|&Next.*|&Install.*|&Finish.*|\u4e0b\u4e00\u6b65.*|\u5b89\u88c5.*|\u5b8c\u6210.*)$'
            if ($null -ne $clickedWizard) { break }
          }
          if ($null -eq $clickedWizard) { Start-Sleep -Milliseconds 250 }
          else { Log ('wizard button={0}' -f $clickedWizard); Start-Sleep -Milliseconds 400 }
        }
        Assert 'fresh_install_completed' $recoveryProcess.HasExited ('process_exited={0}' -f $recoveryProcess.HasExited)
        if ($recoveryProcess.HasExited) {
          Assert 'fresh_install_exit_0' ($recoveryProcess.ExitCode -eq 0) ('rc={0}' -f $recoveryProcess.ExitCode)
        }
      } else {
        $recoveryProcess.WaitForExit(15000)
        Assert 'restore_exits_installer' $recoveryProcess.HasExited ('process_exited={0}' -f $recoveryProcess.HasExited)
        if ($recoveryProcess.HasExited) {
          if ($BlockRollback) {
            Assert 'restore_refusal_exit_2' ($recoveryProcess.ExitCode -eq 2) ('rc={0}' -f $recoveryProcess.ExitCode)
          } else {
            Assert 'restore_installer_exit_0' ($recoveryProcess.ExitCode -eq 0) ('rc={0}' -f $recoveryProcess.ExitCode)
          }
        }
      }
    } else {
      if (-not $recoveryProcess.HasExited) { Stop-Process -Id $recoveryProcess.Id -Force }
    }
  }

  $backupStillExists = ($backupDir -ne '') -and (Test-Path -LiteralPath $backupDir)
  $backupPointer = Get-RegistryValue $appKeyPath 'IaBackupDir'
  $leftoverPointer = Get-RegistryValue $appKeyPath 'IaLeftoverDir'
  $appPresent = Test-Path -LiteralPath $appPath
  if ($finalChoice -eq 'restore') {
    if ($BlockRollback) {
      Assert 'restore_refusal_app_absent' (-not $appPresent) ('exists={0}' -f $appPresent)
      Assert 'restore_refusal_backup_retained' $backupStillExists ('backup_exists={0}' -f $backupStillExists)
      Assert 'restore_refusal_backup_recorded' ($leftoverPointer -ieq $backupDir) ('IaLeftoverDir={0}' -f $leftoverPointer)
      Assert 'restore_refusal_backup_pointer_cleared' ([string]::IsNullOrEmpty($backupPointer)) ('IaBackupDir={0}' -f $backupPointer)
      Assert 'rollback_block_target_untouched' (Test-Path -LiteralPath $rollbackMarker) ('marker_exists={0}' -f (Test-Path -LiteralPath $rollbackMarker))
    } else {
      Assert 'restore_app_present' $appPresent ('exists={0}' -f $appPresent)
      Assert 'restore_backup_consumed' (-not $backupStillExists) ('backup_exists={0}' -f $backupStillExists)
      Assert 'restore_backup_pointer_cleared' ([string]::IsNullOrEmpty($backupPointer)) ('IaBackupDir={0}' -f $backupPointer)
      Assert 'restore_consumed_leftover_pointer_cleared' ([string]::IsNullOrEmpty($leftoverPointer)) ('IaLeftoverDir={0}' -f $leftoverPointer)
    }
  } elseif ($Choice -eq 'continue' -and $FailLeftoverRecord) {
    Assert 'leftover_failure_app_remains_absent' (-not $appPresent) ('exists={0}' -f $appPresent)
    Assert 'leftover_failure_backup_retained' $backupStillExists ('backup_exists={0}' -f $backupStillExists)
    Assert 'leftover_failure_backup_pointer_preserved' ($backupPointer -ieq $backupDir) ('IaBackupDir={0}' -f $backupPointer)
    Assert 'leftover_failure_leftover_not_written' ([string]::IsNullOrEmpty($leftoverPointer)) ('IaLeftoverDir={0}' -f $leftoverPointer)
  } elseif ($Choice -eq 'continue') {
    Assert 'fresh_app_present' $appPresent ('exists={0}' -f $appPresent)
    Assert 'fresh_backup_retained' $backupStillExists ('backup_exists={0}' -f $backupStillExists)
    Assert 'fresh_backup_recorded' ($leftoverPointer -ieq $backupDir) ('IaLeftoverDir={0}' -f $leftoverPointer)
    Assert 'fresh_backup_pointer_cleared' ([string]::IsNullOrEmpty($backupPointer)) ('IaBackupDir={0}' -f $backupPointer)
  } else {
    Assert 'silent_app_remains_absent' (-not $appPresent) ('exists={0}' -f $appPresent)
    Assert 'silent_backup_retained' $backupStillExists ('backup_exists={0}' -f $backupStillExists)
    Assert 'silent_backup_pointer_preserved' ($backupPointer -ieq $backupDir) ('IaBackupDir={0}' -f $backupPointer)
    Assert 'silent_leftover_pointer_unchanged' ([string]::IsNullOrEmpty($leftoverPointer)) ('IaLeftoverDir={0}' -f $leftoverPointer)
  }
  Assert-NoTestShortcuts 'after_recovery'
  if ($appPresent) {
    $appRun = Start-Process -FilePath $appPath -Wait -PassThru
    $launchMarker = Test-Path -LiteralPath (Join-Path $installDir 'launched.txt')
    Assert 'recovered_app_runs' (($appRun.ExitCode -eq 0) -and $launchMarker) ('rc={0} marker={1}' -f $appRun.ExitCode, $launchMarker)
  }
} finally {
  if ($null -ne $stageProcess -and -not $stageProcess.HasExited) { Stop-Process -Id $stageProcess.Id -Force }
  if ($null -ne $recoveryProcess -and -not $recoveryProcess.HasExited) { Stop-Process -Id $recoveryProcess.Id -Force }
  if ($appKeyPath -and (Test-Path -LiteralPath $appKeyPath)) {
    Remove-ItemProperty -Path $appKeyPath -Name 'IaTestRemoveInstallDirectory' -ErrorAction SilentlyContinue
    Remove-ItemProperty -Path $appKeyPath -Name 'IaTestFailLeftoverRecord' -ErrorAction SilentlyContinue
  }

  if ($rollbackBlockPath -and (Test-Path -LiteralPath $rollbackBlockPath)) {
    [System.IO.Directory]::Delete($rollbackBlockPath, $false)
  }

  # For a baseline artifact that never offered recovery, put the isolated old
  # install back before invoking its own uninstaller.
  if ($backupDir -and (Test-Path -LiteralPath $backupDir) -and -not (Test-Path -LiteralPath $appPath)) {
    if (Test-Path -LiteralPath $installDir) { Remove-Item -LiteralPath $installDir -Recurse -Force }
    Move-Item -LiteralPath $backupDir -Destination $installDir
    if ($appKeyPath -and (Test-Path -LiteralPath $appKeyPath)) { Remove-ItemProperty -Path $appKeyPath -Name 'IaBackupDir' -ErrorAction SilentlyContinue }
  }
  if (Test-Path -LiteralPath $installDir) {
    $uninstaller = Get-ChildItem -LiteralPath $installDir -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($uninstaller) { $cleanup = Invoke-Installer $uninstaller.FullName '/S'; Log ('cleanup rc={0}' -f $cleanup.ExitCode) }
  }
  if ($rollbackMarker -and (Test-Path -LiteralPath $rollbackMarker)) { [System.IO.File]::Delete($rollbackMarker) }
  if ($rollbackTarget -and (Test-Path -LiteralPath $rollbackTarget)) { [System.IO.Directory]::Delete($rollbackTarget, $false) }
}

$testDirsAfter = @(Get-ChildItem -LiteralPath $productsRoot -Directory | Where-Object Name -like 'IA Installer Test*' | ForEach-Object FullName)
$realDirsAfter = @(Get-ChildItem -LiteralPath $productsRoot -Directory | Where-Object Name -like 'Intelligence Agent*' | ForEach-Object FullName)
Assert 'test_install_cleaned' ($testDirsAfter.Count -eq 0) ('test_dirs=[{0}]' -f ($testDirsAfter -join ', '))
Assert 'real_product_dirs_untouched' (($realDirsBefore -join ',') -eq ($realDirsAfter -join ',')) ('before=[{0}] after=[{1}]' -f ($realDirsBefore -join ', '), ($realDirsAfter -join ', '))
$testRegistryKeyExistsAfter = $appKeyPath -and (Test-Path -LiteralPath $appKeyPath)
Assert 'test_registry_key_cleaned' (-not $testRegistryKeyExistsAfter) ('app_key={0} exists={1}' -f $appKeyPath, $testRegistryKeyExistsAfter)
Assert-NoTestShortcuts 'after_cleanup'
Log ('DONE assertions_failed={0}' -f $script:failures)
if ($script:failures -gt 0) { exit 1 }
exit 0
