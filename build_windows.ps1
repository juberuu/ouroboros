# Build script for Ouroboros on Windows
# Run from repo root: powershell -ExecutionPolicy Bypass -File build_windows.ps1

$ErrorActionPreference = "Stop"

$Version = (Get-Content VERSION).Trim()
$ArchiveName = "Ouroboros-${Version}-windows-x64.zip"
$ManagedSourceBranch = if ($env:OUROBOROS_MANAGED_SOURCE_BRANCH) { $env:OUROBOROS_MANAGED_SOURCE_BRANCH } else { "ouroboros" }
$ReleaseTag = "v$Version"

Write-Host "=== Building Ouroboros for Windows (v${Version}) ==="

if (-not (Test-Path "python-standalone\python.exe")) {
    Write-Host "ERROR: python-standalone\ not found."
    Write-Host "Run first: powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1"
    exit 1
}

Write-Host "--- Installing launcher dependencies ---"
python -m pip install -q -r requirements-launcher.txt

Write-Host "--- Installing agent dependencies into python-standalone ---"
& "python-standalone\python.exe" -m pip install -q -r requirements.txt

if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }

$env:PYINSTALLER_CONFIG_DIR = Join-Path (Get-Location) ".pyinstaller-cache"
New-Item -ItemType Directory -Force -Path $env:PYINSTALLER_CONFIG_DIR | Out-Null

Write-Host "--- Installing Chromium for browser tools (bundled into python-standalone) ---"
$env:PLAYWRIGHT_BROWSERS_PATH = "0"
& "python-standalone\python.exe" -m playwright install chromium

Write-Host "--- Building embedded managed repo bundle ---"
git rev-parse -q --verify "refs/tags/$ReleaseTag" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Packaging requires git tag $ReleaseTag to exist."
}
$tagType = (git cat-file -t "refs/tags/$ReleaseTag" 2>$null).Trim()
if ($tagType -ne "tag") {
    throw "Packaging requires annotated git tag $ReleaseTag (got '$tagType'). Recreate with: git tag -a $ReleaseTag -m 'Release $ReleaseTag'"
}
$headTags = git tag --points-at HEAD
if (-not ($headTags | Where-Object { $_ -eq $ReleaseTag })) {
    throw "Packaging requires HEAD to be tagged with $ReleaseTag."
}
python scripts/build_repo_bundle.py --source-branch $ManagedSourceBranch

Write-Host "--- Running PyInstaller ---"
python -m PyInstaller Ouroboros.spec --clean --noconfirm

Write-Host ""
Write-Host "=== Creating archive ==="
Compress-Archive -Path "dist\Ouroboros" -DestinationPath "dist\$ArchiveName" -Force

Write-Host ""
Write-Host "=== Done ==="
Write-Host "Archive: dist\$ArchiveName"
Write-Host ""
Write-Host "To run: extract and execute Ouroboros\Ouroboros.exe"
