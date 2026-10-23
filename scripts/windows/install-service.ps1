# Install hub-api as a Windows service (run in an elevated PowerShell, from the project folder):
#
#   1. Download WinSW-x64.exe from https://github.com/winsw/winsw/releases (v2.12 or later),
#      save it as scripts\windows\hub-api-service.exe
#   2. Fill in .env (HUB_DATABASE_URL, HUB_JWT_SECRET, ...), create the first admin with
#      .venv\Scripts\hub-admin.exe create-admin --username <name>
#   3. powershell -ExecutionPolicy Bypass -File scripts\windows\install-service.ps1
#
# Optional: -Account "DOMAIN\svc-hub" to run under a service account instead of LocalSystem
# (WinSW asks for its password).
param([string]$Account = "")

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$winsw = Join-Path $PSScriptRoot "hub-api-service.exe"
$xml = Join-Path $PSScriptRoot "hub-api-service.xml"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script in an elevated (Administrator) PowerShell."
}
if (-not (Test-Path $winsw)) {
    throw "Missing $winsw - download WinSW-x64.exe from https://github.com/winsw/winsw/releases and rename it."
}
if (-not (Test-Path (Join-Path $root ".env"))) {
    throw "Missing .env in $root - copy .env.example and fill it in first."
}

Push-Location $root
try {
    & ".venv\Scripts\python.exe" -m hub.validate_config
    if ($LASTEXITCODE -ne 0) { throw "config/ has errors; fix them before installing the service." }
    $args = @("service-config", "--out", $xml)
    if ($Account) { $args += @("--account", $Account) }
    & ".venv\Scripts\hub-admin.exe" @args
    if ($LASTEXITCODE -ne 0) { throw "could not write the service definition" }
    & $winsw install $xml
    & $winsw start $xml
    & $winsw status $xml
} finally {
    Pop-Location
}
Write-Host "Service installed. Logs: $root\logs. Health: http://127.0.0.1:8000/api/health"
