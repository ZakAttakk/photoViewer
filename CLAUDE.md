# PhotoViewer — Claude Project Notes

## What this is
A Python/Tkinter photo viewer built as a standalone Windows `.exe` via PyInstaller.
Single source file: `photo_viewer.py`. No framework, no config files.

## Environment
- **Python**: 3.14.3 (installed at `C:\Users\zach\AppData\Local\Python\pythoncore-3.14-64`)
- **Key packages**: Pillow 12.2.0, numpy 2.4.4, PyInstaller 6.19.0
- **Platform**: Windows 11

Install deps on a new machine:
```
python -m pip install pillow numpy pyinstaller
```

## Build & register
```powershell
# Build the exe (outputs to app\PhotoViewer.exe)
powershell -ExecutionPolicy Bypass -File build_exe.ps1

# Register as default opener for JPG/PNG/etc (run once per machine)
powershell -ExecutionPolicy Bypass -File register_exe.ps1

# If registry is messy from old registrations, clean first
powershell -ExecutionPolicy Bypass -File cleanup_registry.ps1
```

**Important**: close `app\PhotoViewer.exe` before building — PyInstaller cannot overwrite a running exe.

The build outputs to `dist\PhotoViewer\`, then copies to the stable `app\` folder.
Register `app\PhotoViewer.exe` once; future rebuilds overwrite the same path automatically.

## Architecture
- `PANEL_WIDTH = 384` — edit panel fixed width
- Edit panel sliders defined in `SLIDERS` list at top of file as `(key, label, min, max)` tuples
- `_apply_edits()` applies all slider adjustments in order via PIL LUTs and ImageEnhance
- Retouch (dodge/burn) mask stored as numpy float32 array at max 1500px (`_RETOUCH_MASK_MAX`)
- `_apply_retouch(img, high_quality=False)` — `high_quality=True` upscales mask to full image res (used for Save Copy and Bake); `False` downsamples image to mask size (fast, acceptable for live preview)

## Key design decisions
- **Retouch mask resolution**: capped at 1500px max dimension for performance. Brush strokes are painted into this small mask and upscaled at save/bake time.
- **Bake into Image**: permanently applies retouch at full quality into `self.pil_image`, clears mask. Use this before adjusting sliders after retouching — otherwise live preview softens the image at 100%/200% zoom.
- **Save Copy**: saves `filename_edited.jpg` at full quality (high_quality retouch path). Prompts to open the saved copy, which resets sliders to zero before displaying.
- **Tone algorithms**: Lightroom-style parametric zones — blacks/whites use quadratic weights, shadows/highlights use sqrt weights, all targeting their respective half of the tonal range. Brightness is gamma-based. Contrast is an S-curve.
- **Dodge/burn curves**: straight linear weighting (dodge strongest in shadows, burn strongest in highlights). Each stroke also applies ±25% saturation adjustment in the same numpy pass.
- **Slider +/- buttons**: step by 2. Per-slider ↺ reset button appears (gray) when value ≠ 0.
- **Tkinter DISABLED state**: only suppresses class bindings, not custom bindings — slider click/wheel handlers manually check `sl['state']`.

## Workflow notes
- Sliders are non-destructive and applied fresh on every render from `self.pil_image`
- Crop is destructive — it modifies `self.pil_image` in place and clears the retouch mask
- Retouch is semi-destructive at live preview (softens at 100%+ zoom); use Bake or Save Copy for full quality
- "Clear Strokes" and "Bake into Image" buttons are hidden until first stroke is painted
- Navigating to a new image clears the retouch mask and stops any active retouch mode

## File layout
```
photoViewer/
  photo_viewer.py       — entire application
  build_exe.ps1         — PyInstaller build script
  register_exe.ps1      — Windows registry file association
  cleanup_registry.ps1  — clears stale registry entries
  app/                  — stable built exe (gitignored)
  dist/                 — PyInstaller output (gitignored)
  build/                — PyInstaller cache (gitignored)
```
