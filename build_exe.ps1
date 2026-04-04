# build_exe.ps1
# Builds PhotoViewer.exe and copies it to a stable 'app' folder.
# Register 'app\PhotoViewer.exe' with "Open with" once — every future
# rebuild overwrites that same path, so no re-registering is needed.

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$appDir    = Join-Path $scriptDir "app"
$exePath   = Join-Path $appDir "PhotoViewer.exe"

Write-Host "Installing/updating PyInstaller..." -ForegroundColor Cyan
python -m pip install --upgrade pyinstaller | Out-Null

Write-Host "Building PhotoViewer.exe..." -ForegroundColor Cyan
Set-Location $scriptDir

python -m PyInstaller `
    --onedir `
    --noconsole `
    --name PhotoViewer `
    --distpath "$scriptDir\dist" `
    -y `
    photo_viewer.py

$builtExe = Join-Path $scriptDir "dist\PhotoViewer\PhotoViewer.exe"
if (-not (Test-Path $builtExe)) {
    Write-Error "Build failed - dist\PhotoViewer\PhotoViewer.exe not found."
    Pause; exit 1
}

# Copy built app into the stable 'app' folder, replacing previous version.
Write-Host "Copying to stable app folder..." -ForegroundColor Cyan
if (Test-Path $appDir) { Remove-Item $appDir -Recurse -Force }
Copy-Item (Join-Path $scriptDir "dist\PhotoViewer") $appDir -Recurse

Write-Host ""
Write-Host "Build successful!" -ForegroundColor Green
Write-Host ""
Write-Host "Stable exe path (register this once, never again):" -ForegroundColor Cyan
Write-Host "  $exePath" -ForegroundColor White
Write-Host ""
Write-Host "To register as default JPG/PNG opener:" -ForegroundColor Cyan
Write-Host "  Right-click any JPG -> Open with -> Choose another app"
Write-Host "  -> More apps -> Look for another app on this PC"
Write-Host "  -> Browse to the path above -> check 'Always use this app'"
Write-Host ""
Write-Host "Future rebuilds update that same file automatically." -ForegroundColor Gray

Pause
