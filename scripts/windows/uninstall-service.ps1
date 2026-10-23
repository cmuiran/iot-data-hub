# Stop and remove the hub-api Windows service (elevated PowerShell, from the project folder):
#   powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall-service.ps1
$ErrorActionPreference = "Stop"
$winsw = Join-Path $PSScriptRoot "hub-api-service.exe"
$xml = Join-Path $PSScriptRoot "hub-api-service.xml"
if (-not (Test-Path $winsw) -or -not (Test-Path $xml)) {
    throw "hub-api-service.exe / .xml not found next to this script."
}
& $winsw stop $xml
& $winsw uninstall $xml
Write-Host "Service removed. Data in the database and logs\ are kept."
