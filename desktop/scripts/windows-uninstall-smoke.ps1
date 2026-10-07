# #361 [W-16] uninstall smoke test (runs on Windows x64 only, driven by
# scripts/test-windows-installer.mjs --uninstall-only).
#
# Verifies the ticket's core guarantee: uninstall removes the program but
# NEVER silently deletes user data (%APPDATA%\intelligence-agent).
param(
  [Parameter(Mandatory = $true)][string]$Installer,
  [Parameter(Mandatory = $true)][string]$ProductName,
  [Parameter(Mandatory = $true)][string]$RegistryKey,
  [Parameter(Mandatory = $true)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'

& $Installer /S
if ($LASTEXITCODE -ne 0) { throw "installer exited with $LASTEXITCODE" }

$regKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$RegistryKey"
$installLocation = (Get-ItemProperty -Path $regKey -ErrorAction Stop).InstallLocation

$dataDir = Join-Path $env:APPDATA 'intelligence-agent'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$marker = Join-Path $dataDir 'smoke-marker.txt'
'do-not-delete' | Out-File -FilePath $marker -Encoding utf8

$uninstaller = Join-Path $installLocation "Uninstall $ProductName.exe"
if (-not (Test-Path $uninstaller)) { throw "uninstaller missing: $uninstaller" }
& $uninstaller /S
if ($LASTEXITCODE -ne 0) { throw "uninstaller exited with $LASTEXITCODE" }

Start-Sleep -Seconds 2
if (Test-Path $installLocation) { throw "install dir still present after uninstall: $installLocation" }
if (Test-Path $regKey) { throw 'uninstall registry key still present after uninstall' }
# THE ticket assertion: user data must survive uninstall.
if (-not (Test-Path $marker)) { throw 'user data was deleted by uninstall — forbidden by #361' }

"UNINSTALL_OK userDataPreserved=true" | Out-File (Join-Path $OutputDirectory 'uninstall-result.txt')
Write-Output 'UNINSTALL_OK userDataPreserved=true'
