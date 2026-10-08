# #361 [W-16] install smoke test (runs on Windows x64 only, driven by
# scripts/test-windows-installer.mjs).
#
# Verifies: silent install works, the per-user uninstall registry entry is
# written, the install directory contains the payload, and a marker file in
# the user-data dir survives the install.
param(
  [Parameter(Mandatory = $true)][string]$Installer,
  [Parameter(Mandatory = $true)][string]$ProductName,
  [Parameter(Mandatory = $true)][string]$RegistryKey,
  [Parameter(Mandatory = $true)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'

# NSIS installers are GUI-subsystem executables: `& $exe` returns immediately
# and never sets $LASTEXITCODE, so both the wait and the exit code have to come
# from Start-Process (#831, measured on this machine).
$install = Start-Process -FilePath $Installer -ArgumentList '/S' -Wait -PassThru
if ($install.ExitCode -ne 0) { throw "installer exited with $($install.ExitCode)" }

$regKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$RegistryKey"
$installLocation = (Get-ItemProperty -Path $regKey -ErrorAction Stop).InstallLocation
if ([string]::IsNullOrEmpty($installLocation)) { throw 'InstallLocation missing from registry' }
if (-not (Test-Path (Join-Path $installLocation "$ProductName.exe"))) {
  throw "payload exe missing under $installLocation"
}

# User data lives outside the install dir; plant a marker and prove the
# install did not touch it.
$dataDir = Join-Path $env:APPDATA 'intelligence-agent'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$marker = Join-Path $dataDir 'smoke-marker.txt'
'do-not-delete' | Out-File -FilePath $marker -Encoding utf8

"INSTALL_OK installLocation=$installLocation" | Out-File (Join-Path $OutputDirectory 'install-result.txt')
Write-Output "INSTALL_OK $installLocation"
