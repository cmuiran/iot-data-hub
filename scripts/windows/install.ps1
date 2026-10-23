# First installation on a server (elevated PowerShell), from the unpacked bundle or a checkout:
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 `
#       [-DatabaseUrl "postgresql://hub_app:...@localhost:5432/hub"] [-AdminUser boss] [-NoService]
#
# Steps (each is skipped when already done, so the script can be run again):
#   1. checks: Python 3.12, Administrator rights (not needed with -NoService)
#   2. .venv with the locked dependency versions - offline from wheelhouse\ when it exists
#   3. config\ from config.dist\ (the samples) when config\ does not exist yet
#   4. .env from .env.example with new random HUB_JWT_SECRET and HUB_DATA_KEY (never printed),
#      HUB_DATABASE_URL from -DatabaseUrl; access to .env limited to Administrators and SYSTEM
#   5. python -m hub.validate_config, database migrations, first administrator (-AdminUser;
#      hub-admin asks for the password)
#   6. the Windows service (install-service.ps1; needs scripts\windows\hub-api-service.exe)
# Remote control stays off (HUB_CONTROL_ENABLED=false) until commissioning (docs/fa/commissioning.md).
param(
    [string]$DatabaseUrl = "",
    [string]$AdminUser = "",
    [string]$Account = "",
    [switch]$NoService
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$py = Join-Path $root ".venv\Scripts\python.exe"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

function New-Secret {
    $bytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Set-EnvValue([string]$file, [string]$key, [string]$value) {
    $lines = @(Get-Content $file -Encoding utf8)
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^$key=") { $lines[$i] = "$key=$value"; $found = $true }
    }
    if (-not $found) { $lines += "$key=$value" }
    [IO.File]::WriteAllLines($file, $lines, (New-Object Text.UTF8Encoding($false)))
}

function Get-EnvValue([string]$file, [string]$key) {
    $line = Get-Content $file -Encoding utf8 | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
    if ($line) { return $line.Substring($key.Length + 1).Trim() }
    return ""
}

Push-Location $root
try {
    Step "checks"
    $isAdmin = (New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $NoService -and -not $isAdmin) {
        throw "Run this script in an elevated (Administrator) PowerShell, or use -NoService."
    }
    $pyVersion = & py -3.12 -c "import sys; print(sys.version.split()[0])" 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 (64-bit) is not installed (py -3.12 failed)." }
    Write-Host "Python $pyVersion"

    Step "program and dependencies (.venv)"
    if (-not (Test-Path $py)) {
        & py -3.12 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "could not create .venv" }
    }
    $src = @("--disable-pip-version-check")
    $wheelhouse = Join-Path $root "wheelhouse"
    if (Test-Path $wheelhouse) {
        Write-Host "offline install from $wheelhouse"
        $src += @("--no-index", "--find-links", $wheelhouse)
    }
    & $py -m pip install @src --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "updating pip failed" }
    & $py -m pip install @src -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw "installing the dependencies failed" }
    & $py -m pip install @src --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw "installing the hub failed" }

    Step "configuration"
    if (-not (Test-Path "config") -and (Test-Path "config.dist")) {
        Copy-Item "config.dist" "config" -Recurse
        Write-Host "config\ created from the samples: register the plant's devices (docs/fa/commissioning.md)"
    } else {
        Write-Host "config\ exists: kept"
    }

    Step ".env (settings and secrets)"
    $envFile = Join-Path $root ".env"
    if (-not (Test-Path $envFile)) {
        Copy-Item ".env.example" $envFile
        Write-Host ".env created from .env.example"
    }
    foreach ($key in @("HUB_JWT_SECRET", "HUB_DATA_KEY")) {
        $current = Get-EnvValue $envFile $key
        if (-not $current -or $current.Length -lt 32 -or $current -match "change|example|placeholder") {
            Set-EnvValue $envFile $key (New-Secret)
            $note = if ($key -eq "HUB_DATA_KEY") { " - keep a copy in the company's password safe" } else { "" }
            Write-Host "$key generated$note"
        }
    }
    if ($DatabaseUrl) { Set-EnvValue $envFile "HUB_DATABASE_URL" $DatabaseUrl }
    Set-EnvValue $envFile "HUB_CONTROL_ENABLED" "false"
    if ($isAdmin) {
        & icacls $envFile /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
        if ($Account) { & icacls $envFile /grant:r "${Account}:R" | Out-Null }
        Write-Host ".env readable only by Administrators and SYSTEM$(if ($Account) { " and $Account" })"
    }

    Step "check and database"
    & $py -m hub.validate_config
    if ($LASTEXITCODE -ne 0) { throw "config\ has errors (see above)" }
    $db = Get-EnvValue $envFile "HUB_DATABASE_URL"
    if (-not $db -or $db -match "<|password@|change") {
        Write-Warning "HUB_DATABASE_URL is not set in .env: set it, then run this script again."
        return
    }
    & ".venv\Scripts\hub-admin.exe" migrate
    if ($LASTEXITCODE -ne 0) { throw "database migration failed (is PostgreSQL running, is the URL right?)" }
    if ($AdminUser) {
        & ".venv\Scripts\hub-admin.exe" create-admin --username $AdminUser
        if ($LASTEXITCODE -ne 0) { throw "creating the administrator failed" }
    }

    if ($NoService) {
        Write-Host "`nInstalled. Start by hand: .venv\Scripts\hub-api.exe"
        return
    }
    Step "Windows service"
    if (-not (Test-Path (Join-Path $PSScriptRoot "hub-api-service.exe"))) {
        Write-Warning ("WinSW is missing: save WinSW-x64.exe as scripts\windows\hub-api-service.exe, " +
                       "then run scripts\windows\install-service.ps1")
        return
    }
    $svcArgs = @{}
    if ($Account) { $svcArgs["Account"] = $Account }
    & (Join-Path $PSScriptRoot "install-service.ps1") @svcArgs
    Write-Host "`nDone. Next: HTTPS (docs/fa/install.md section 6), then commissioning."
} finally {
    Pop-Location
}
