# Build the offline installation bundle for a plant network without internet access.
# Run on a Windows build machine WITH internet access (Python 3.12 installed), from the project:
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\build-bundle.ps1 [-Out release]
#
# Result: release\iot-data-hub-<version>-windows.zip (+ .sha256) containing
#   * the program files (hub, backend, simulators, frontend, db, scripts, docs, ...)
#   * wheelhouse\  every dependency as a wheel, exactly the versions of requirements.lock,
#                  plus pip and setuptools (to update pip and install offline)
#   * config.dist\ the repository's sample configuration (copied to config\ on first install)
#   * SHA256SUMS.txt for every file
# Copy the zip (and Python 3.12's installer, and WinSW-x64.exe) to the server, check the .sha256,
# unpack it and run scripts\windows\install.ps1 (docs/fa/install.md).
param(
    [string]$Out = "release",
    [string]$Python = "py",
    [string]$PythonVersion = "-3.12"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ProgramItems = @("hub", "backend", "simulators", "frontend", "db", "scripts", "docs",
                  "pyproject.toml", "requirements.lock", "README.md", "CLAUDE.md", ".env.example")

Push-Location $root
try {
    $version = (Select-String -Path "pyproject.toml" -Pattern '^version = "([^"]+)"').Matches[0].Groups[1].Value
    if (-not $version) { throw "no version in pyproject.toml" }
    $name = "iot-data-hub-$version"
    $outDir = Join-Path $root $Out
    $stage = Join-Path $outDir $name
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    New-Item -ItemType Directory -Force $stage | Out-Null

    Write-Host "== program files"
    foreach ($item in $ProgramItems) {
        if (-not (Test-Path $item)) { throw "missing $item" }
        Copy-Item $item (Join-Path $stage $item) -Recurse -Force
    }
    Copy-Item "config" (Join-Path $stage "config.dist") -Recurse -Force
    # never ship caches, local service files or anything secret
    Get-ChildItem $stage -Recurse -Force -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
    foreach ($f in @("scripts\windows\hub-api-service.exe", "scripts\windows\hub-api-service.xml")) {
        $p = Join-Path $stage $f
        if (Test-Path $p) { Remove-Item $p -Force }
    }
    $secrets = Get-ChildItem $stage -Recurse -Force -File |
        Where-Object { $_.Name -eq ".env" -or $_.Extension -in @(".pem", ".key", ".pfx") }
    if ($secrets) { throw "refusing to bundle secret files: $($secrets.FullName -join ', ')" }

    Write-Host "== wheelhouse (requirements.lock, Windows x64, Python 3.12)"
    $wheels = Join-Path $stage "wheelhouse"
    $pip = @($PythonVersion, "-m", "pip", "download", "--disable-pip-version-check",
             "--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.12",
             "--implementation", "cp", "--dest", $wheels)
    & $Python @pip -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw "pip download failed" }
    & $Python @pip "pip" "setuptools>=69"
    if ($LASTEXITCODE -ne 0) { throw "pip download (pip, setuptools) failed" }

    Write-Host "== checksums"
    $sums = Get-ChildItem $stage -Recurse -File | Sort-Object FullName | ForEach-Object {
        $rel = $_.FullName.Substring($stage.Length + 1)
        "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $rel
    }
    $sums | Set-Content -Encoding ascii (Join-Path $stage "SHA256SUMS.txt")

    Write-Host "== zip"
    $zip = Join-Path $outDir "$name-windows.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $stage -DestinationPath $zip -CompressionLevel Optimal
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    "$hash  $name-windows.zip" | Set-Content -Encoding ascii "$zip.sha256"
    $mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "bundle: $zip ($mb MB)"
    Write-Host "sha256: $hash"
} finally {
    Pop-Location
}
