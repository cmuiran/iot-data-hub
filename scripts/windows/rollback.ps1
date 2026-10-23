# Go back to the version saved by upgrade.ps1 (elevated PowerShell):
#
#   powershell -ExecutionPolicy Bypass -File rollback\<stamp>-<version>\scripts\windows\rollback.ps1 `
#       -From rollback\<stamp>-<version>
#
# Run the copy INSIDE the rollback folder (it belongs to the old version). Steps: stop the
# service, put the old program files and .venv back (the new ones go to rollback\<stamp>-failed),
# restore the database from the pre-upgrade backup when the new version migrated it (the old
# version would refuse a newer schema), start the service and wait for /api/health.
# Data written between the upgrade and the rollback is lost only when the database is restored;
# the script says so before doing it.
param(
    [Parameter(Mandatory = $true)][string]$From,
    [string]$HealthUrl = "http://127.0.0.1:8000/api/health",
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$From = (Resolve-Path $From).Path
$root = (Resolve-Path (Join-Path $From "..\..")).Path
$info = Get-Content (Join-Path $From "upgrade.json") -Raw | ConvertFrom-Json
$winsw = Join-Path $root "scripts\windows\hub-api-service.exe"
$xml = Join-Path $root "scripts\windows\hub-api-service.xml"
$ProgramItems = @("hub", "backend", "simulators", "frontend", "db", "scripts", "docs",
                  "pyproject.toml", "requirements.lock", "README.md", "CLAUDE.md", ".env.example",
                  ".venv")

$p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script in an elevated (Administrator) PowerShell."
}
Write-Host "rollback $($info.to) -> $($info.from)"

Push-Location $root
try {
    if (Test-Path $winsw) { & $winsw stop $xml }
    $failed = Join-Path $root ("rollback\" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-failed-$($info.to)")
    New-Item -ItemType Directory -Force $failed | Out-Null
    foreach ($item in $ProgramItems) {
        $path = Join-Path $root $item
        $saved = Join-Path $From $item
        if ((Test-Path $saved) -and (Test-Path $path)) { Move-Item $path (Join-Path $failed $item) }
        if (Test-Path $saved) { Copy-Item $saved $path -Recurse -Force }
    }
    # schema: compare what the old version knows with the database
    $state = & ".venv\Scripts\hub-admin.exe" migrate --dry-run 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -and $state -match "NEWER") {
        Write-Warning "the database was migrated by $($info.to): restoring the pre-upgrade backup $($info.backup)"
        Write-Warning "values and events recorded since the upgrade are lost."
        & ".venv\Scripts\hub-admin.exe" restore-backup $info.backup --yes
        if ($LASTEXITCODE -ne 0) { throw "database restore failed; the service stays stopped" }
    }
    if (Test-Path $winsw) { & $winsw start $xml }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            if ((Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5).status -eq "ok") {
                Write-Host "rolled back to $($info.from); the failed version is kept in $failed"
                return
            }
        } catch { }
        Start-Sleep -Seconds 3
    }
    throw "the old version did not become healthy within $TimeoutSeconds s (see logs\)"
} finally {
    Pop-Location
}
