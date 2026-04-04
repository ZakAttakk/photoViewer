# cleanup_registry.ps1
# Removes all old PhotoViewer registry entries so Windows stops trying to
# open images with missing/stale handlers.
# Run this BEFORE register_exe.ps1.

$ErrorActionPreference = "SilentlyContinue"

$extensions = @(".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif")

# Old ProgIDs written by previous scripts
$oldProgIds = @(
    "PhotoViewerPy.Image",   # setup_windows.ps1
    "PhotoViewerApp",        # register_default.ps1
    "PhotoViewerExe.Image"   # register_exe.ps1 (clean slate)
)

Write-Host "Cleaning old ProgID registrations..." -ForegroundColor Cyan
foreach ($id in $oldProgIds) {
    $path = "HKCU:\SOFTWARE\Classes\$id"
    if (Test-Path $path) {
        Remove-Item $path -Recurse -Force
        Write-Host "  Removed: $path" -ForegroundColor Yellow
    }
}

# Old Applications\PhotoViewer.exe entry (register_default.ps1)
$appEntry = "HKCU:\SOFTWARE\Classes\Applications\PhotoViewer.exe"
if (Test-Path $appEntry) {
    Remove-Item $appEntry -Recurse -Force
    Write-Host "  Removed: $appEntry" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Cleaning extension associations..." -ForegroundColor Cyan
foreach ($ext in $extensions) {
    # Remove HKCU\Classes extension key (our override)
    $extKey = "HKCU:\SOFTWARE\Classes\$ext"
    if (Test-Path $extKey) {
        Remove-Item $extKey -Recurse -Force
        Write-Host "  Removed: $extKey" -ForegroundColor Yellow
    }

    # Remove UserChoice (the hash-protected "always open with" setting)
    $uc = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$ext\UserChoice"
    if (Test-Path $uc) {
        Remove-Item $uc -Force
        Write-Host "  Cleared UserChoice for $ext" -ForegroundColor Yellow
    }

    # Remove stale entries from OpenWithProgids
    $owp = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$ext\OpenWithProgids"
    if (Test-Path $owp) {
        foreach ($id in $oldProgIds) {
            Remove-ItemProperty -Path $owp -Name $id -Force -ErrorAction SilentlyContinue
        }
    }
}

# Notify Windows shell to refresh
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class Shell32Cleanup {
    [DllImport("shell32.dll")]
    public static extern void SHChangeNotify(int wEventId, uint uFlags, IntPtr a, IntPtr b);
}
"@
[Shell32Cleanup]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)

Write-Host ""
Write-Host "Done. Now run register_exe.ps1 to set the new association." -ForegroundColor Green
Write-Host ""
Pause
