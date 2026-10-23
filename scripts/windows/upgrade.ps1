# Upgrade the hub to a new version with automatic rollback (elevated PowerShell, project folder):
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\upgrade.ps1 -Source D:\hub-0.5.0
#   ... -Wheelhouse D:\hub-0.5.0-wheels     # offline plant network: packages from this folder
#
# -Source is the new version (unpacked release or a git checkout). Steps:
#   1. hub-admin upgrade-check (current version): config valid, schema not newer, tools present
#   2. backup of the database to backups\pre-upgrade (kept apart from the daily backups)
#   3. stop the service; move the program files and .venv to rollback\<timestamp>
#   4. copy the new program files, create .venv, install (from -Wheelhouse when given)
#   5. start the service (it migrates the database) and wait for /api/health and the new version
#   6. any failure in 4-5: automatic rollback (rollback.ps1): old files and .venv back, and the
#      database restored from the pre-upgrade backup when the new version migrated it
# Never touched: .env, config\, pki\, logs\, backups\ - they belong to the site, not the version.
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [string]$Wheelhouse = "",
    [string]$HealthUrl = "http://127.0.0.1:8000/api/health",
    [int]$TimeoutSeconds = 180,
    [switch]$SkipCheck
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$winsw = Join-Path $PSScriptRoot "hub-api-service.exe"
$xml = Join-Path $PSScriptRoot "hub-api-service.xml"
# program files of a version (everything else in $root is site data)
$ProgramItems = @("hub", "backend", "simulators", "frontend", "db", "scripts", "docs",
                  "pyproject.toml", "requirements.lock", "README.md", "CLAUDE.md", ".env.example")

function Assert-Admin {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script in an elevated (Administrator) PowerShell."
    }
}

function Get-HubVersion([string]$folder) {
    $text = Get-Content (Join-Path $folder "pyproject.toml") -Raw
    if ($text -match 'version\s*=\s*"([^"]+)"') { return $Matches[1] }
    throw "no version in $folder\pyproject.toml"
}

function Get-SchemaPending {
    $out = & ".venv\Scripts\hub-admin.exe" migrate --dry-run 2>&1 | Out-String
    return $out.Trim()
}

function Wait-Healthy([string]$version) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $versionsUrl = $HealthUrl -replace "/api/health$", "/api/versions"
    while ((Get-Date) -lt $deadline) {
        try {
            $h = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
            $v = Invoke-RestMethod -Uri $versionsUrl -TimeoutSec 5
            if ($h.status -eq "ok" -and $v.hub -eq $version) { return $true }
        } catch { }
        Start-Sleep -Seconds 3
    }
    return $false
}

Assert-Admin
$Source = (Resolve-Path $Source).Path
if ($Source -eq $root) { throw "-Source must be the folder of the NEW version, not $root" }
foreach ($item in @("pyproject.toml", "hub", "backend", "frontend", "db")) {
    if (-not (Test-Path (Join-Path $Source $item))) { throw "$Source is not a hub release (missing $item)" }
}
if (-not (Test-Path $winsw)) { throw "Missing $winsw (see install-service.ps1)" }
$old = Get-HubVersion $root
$new = Get-HubVersion $Source
Write-Host "upgrade $old -> $new"

Push-Location $root
try {
    if (-not $SkipCheck) {
        & ".venv\Scripts\hub-admin.exe" upgrade-check
        if ($LASTEXITCODE -ne 0) { throw "upgrade-check failed: fix the items above (or -SkipCheck)" }
    }
    $backupDir = Join-Path $root "backups\pre-upgrade"
    $line = & ".venv\Scripts\hub-admin.exe" backup --out $backupDir --keep 5 | Out-String
    if ($LASTEXITCODE -ne 0 -or $line -notmatch "backup written: (.+?) \(\d+ bytes\)") { throw "pre-upgrade backup failed: $line" }
    $backupFile = $Matches[1]
    Write-Host "pre-upgrade backup: $backupFile"
    $schemaBefore = Get-SchemaPending

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $rollbackDir = Join-Path $root "rollback\$stamp-$old"
    New-Item -ItemType Directory -Force $rollbackDir | Out-Null
    @{ from = $old; to = $new; backup = $backupFile; schema = $schemaBefore; time = $stamp } |
        ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $rollbackDir "upgrade.json")

    & $winsw stop $xml
    try {
        foreach ($item in $ProgramItems + @(".venv")) {
            $path = Join-Path $root $item
            if (Test-Path $path) { Move-Item $path (Join-Path $rollbackDir $item) }
        }
        foreach ($item in $ProgramItems) {   # targets were moved away: no nested folders
            $src = Join-Path $Source $item
            if (Test-Path $src) { Copy-Item $src (Join-Path $root $item) -Recurse -Force }
        }
        # the service definition and WinSW (scripts\windows) belong to this site: keep them
        New-Item -ItemType Directory -Force (Join-Path $root "scripts\windows") | Out-Null
        foreach ($keep in @("hub-api-service.exe", "hub-api-service.xml")) {
            $saved = Join-Path $rollbackDir "scripts\windows\$keep"
            if (Test-Path $saved) { Copy-Item $saved (Join-Path $root "scripts\windows\$keep") -Force }
        }
        & py -3.12 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "could not create .venv (Python 3.12 installed?)" }
        # locked dependency versions, then the program itself as an editable install (db\ and
        # frontend\ are found next to the code)
        $src = @("--disable-pip-version-check")
        if ($Wheelhouse) { $src += @("--no-index", "--find-links", $Wheelhouse) }
        if (Test-Path (Join-Path $root "requirements.lock")) {
            & ".venv\Scripts\python.exe" -m pip install @src -r requirements.lock
            if ($LASTEXITCODE -ne 0) { throw "installing the dependencies of $new failed" }
            & ".venv\Scripts\python.exe" -m pip install @src --no-deps -e .
        } else {
            & ".venv\Scripts\python.exe" -m pip install @src -e .
        }
        if ($LASTEXITCODE -ne 0) { throw "installing $new failed" }
        & ".venv\Scripts\python.exe" -m hub.validate_config
        if ($LASTEXITCODE -ne 0) { throw "the site configuration is not valid for $new" }
        & $winsw start $xml
        if (-not (Wait-Healthy $new)) { throw "$new did not become healthy within $TimeoutSeconds s (see logs\)" }
    } catch {
        Write-Warning "upgrade failed: $_"
        Write-Warning "rolling back to $old"
        & (Join-Path $rollbackDir "scripts\windows\rollback.ps1") -From $rollbackDir -HealthUrl $HealthUrl
        throw "upgrade to $new failed and was rolled back to ${old}: $_"
    }
    Write-Host "upgraded to $new. Rollback copy: $rollbackDir (remove it after a few days of normal operation)"
} finally {
    Pop-Location
}
