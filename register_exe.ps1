# register_exe.ps1
# Registers app\PhotoViewer.exe as the default opener for JPG/JPEG/PNG/BMP/WEBP/TIFF.
# Run once after building. Re-run any time Windows resets the association.
#
# Usage: Right-click -> "Run with PowerShell"
#   or:  powershell -ExecutionPolicy Bypass -File register_exe.ps1

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$exePath   = Join-Path $scriptDir "app\PhotoViewer.exe"

if (-not (Test-Path $exePath)) {
    Write-Error "app\PhotoViewer.exe not found. Run build_exe.ps1 first."
    Pause; exit 1
}

Write-Host "Registering: $exePath" -ForegroundColor Cyan

# ── 1. Register ProgID ────────────────────────────────────────────────────────
$progId  = "PhotoViewerExe.Image"
$openCmd = "`"$exePath`" `"%1`""

$progKey = "HKCU:\SOFTWARE\Classes\$progId"
New-Item -Path "$progKey\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path "$progKey"                    -Name "(Default)" -Value "Photo Viewer"
Set-ItemProperty -Path "$progKey\shell\open\command" -Name "(Default)" -Value $openCmd

# Also register the exe under App Paths so Windows can find it
$appPathKey = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\PhotoViewer.exe"
New-Item -Path $appPathKey -Force | Out-Null
Set-ItemProperty -Path $appPathKey -Name "(Default)" -Value $exePath

Write-Host "ProgID registered: $progId" -ForegroundColor Green

# ── 2. Map extensions to ProgID ───────────────────────────────────────────────
$extensions = @(".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif")

foreach ($ext in $extensions) {
    $extKey = "HKCU:\SOFTWARE\Classes\$ext"
    New-Item -Path $extKey -Force | Out-Null
    Set-ItemProperty -Path $extKey -Name "(Default)" -Value $progId

    # ── 3. Remove UserChoice so Windows falls back to HKCU\Classes ────────────
    # UserChoice has a tamper-proof hash on Win10/11; deleting it makes Windows
    # use our HKCU\Classes entry above instead.
    $ucPath = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$ext\UserChoice"
    if (Test-Path $ucPath) {
        Remove-Item -Path $ucPath -Force -ErrorAction SilentlyContinue
        Write-Host "  Cleared UserChoice for $ext" -ForegroundColor Yellow
    }

    # Also remove OpenWithProgids list entries that point to old handlers
    # (keeps our ProgID as the clean winner)
    $owpPath = "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$ext\OpenWithProgids"
    if (Test-Path $owpPath) {
        # Add our ProgID to the list
        Set-ItemProperty -Path $owpPath -Name $progId -Value ([byte[]]@()) -ErrorAction SilentlyContinue
    }

    Write-Host "  Registered $ext -> $progId" -ForegroundColor Green
}

# ── 4. Notify Windows shell ───────────────────────────────────────────────────
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class Shell32 {
    [DllImport("shell32.dll")]
    public static extern void SHChangeNotify(int wEventId, uint uFlags, IntPtr dwItem1, IntPtr dwItem2);
}
"@
[Shell32]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)

Write-Host ""
Write-Host "Done! JPG/PNG files should now open with PhotoViewer.exe." -ForegroundColor Green
Write-Host ""
Write-Host "If Windows shows an 'How do you want to open this?' prompt:" -ForegroundColor Gray
Write-Host "  Select 'Photo Viewer' from the list and check 'Always'." -ForegroundColor Gray
Write-Host "  Then run this script once more to lock it in." -ForegroundColor Gray
Write-Host ""
Pause
