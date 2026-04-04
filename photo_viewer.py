"""
photo_viewer.py — A fast, keyboard-friendly image viewer for Windows.

Usage:
    python photo_viewer.py [image_path]

Keyboard shortcuts:
    Left / Right   Navigate photos in the same folder
    F              Fit to window
    1              100% (actual size)
    2              200% zoom
    C              Copy current photo to the selected copy-to folder
    F11            Toggle fullscreen
    Escape         Exit fullscreen
"""

import os
import sys
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path

try:
    from PIL import Image, ImageTk, ImageOps
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow"])
    from PIL import Image, ImageTk, ImageOps


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp"}

DARK_BG        = "#1e1e1e"
TOOLBAR_BG     = "#2d2d2d"
BTN_BG         = "#3c3c3c"
BTN_FG         = "#e0e0e0"
BTN_ACTIVE_BG  = "#0078d4"
BTN_ACTIVE_FG  = "#ffffff"
LABEL_FG       = "#9e9e9e"
CANVAS_BG      = "#141414"
COPY_LABEL_FG  = "#4fc3f7"




class PhotoViewer:
    def __init__(self, root: tk.Tk, initial_file: str | None = None):
        self.root = root
        self.root.title("Photo Viewer")
        self.root.configure(bg=DARK_BG)
        self.root.state("zoomed")
        self.root.minsize(400, 300)

        # State
        self.folder_files: list[str] = []
        self.current_index: int = 0
        self.current_file: str | None = None
        self.pil_image: Image.Image | None = None
        self.tk_image: ImageTk.PhotoImage | None = None
        self.zoom_mode: str = "fit"          # "fit" | "100" | "200"
        self.copy_to_folder: str | None = None
        self._resize_job = None
        # Pre-load cache: filepath -> PIL Image (decoded, EXIF-rotated)
        self._cache: dict[str, Image.Image] = {}
        self._loading: set[str] = set()

        self._build_ui()
        self._bind_keys()

        if initial_file and os.path.isfile(initial_file):
            self._load_folder_and_show(initial_file)

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        self._build_toolbar()
        self._build_canvas_area()

    def _build_toolbar(self):
        self.toolbar = tk.Frame(self.root, bg=TOOLBAR_BG, pady=4)
        self.toolbar.pack(side=tk.TOP, fill=tk.X)
        tb = self.toolbar

        def btn(parent, text, cmd, **kw):
            b = tk.Button(
                parent, text=text, command=cmd,
                bg=BTN_BG, fg=BTN_FG,
                activebackground=BTN_ACTIVE_BG, activeforeground=BTN_ACTIVE_FG,
                relief=tk.FLAT, bd=0, padx=12, pady=3,
                font=("Segoe UI", 12), cursor="hand2", **kw
            )
            return b

        def sep():
            tk.Frame(tb, bg="#555555", width=1).pack(side=tk.LEFT, padx=8, pady=3, fill=tk.Y)

        # RIGHT-side items must be packed first so left items don't crowd them out.
        self.info_var = tk.StringVar(value="")
        tk.Label(
            tb, textvariable=self.info_var,
            bg=TOOLBAR_BG, fg=LABEL_FG, font=("Segoe UI", 12)
        ).pack(side=tk.RIGHT, padx=12)

        btn(tb, "Full Screen  [F11]", self._toggle_fullscreen).pack(side=tk.RIGHT, padx=(3, 6))
        btn(tb, "Info", self._show_exif).pack(side=tk.RIGHT, padx=3)

        # --- Left cluster: zoom ---
        self.btn_fit = btn(tb, "Fit  [F]",  lambda: self.set_zoom("fit"))
        self.btn_fit.pack(side=tk.LEFT, padx=(10, 3))

        self.btn_100 = btn(tb, "100%  [1]", lambda: self.set_zoom("100"))
        self.btn_100.pack(side=tk.LEFT, padx=3)

        self.btn_200 = btn(tb, "200%  [2]", lambda: self.set_zoom("200"))
        self.btn_200.pack(side=tk.LEFT, padx=3)

        sep()

        # --- Copy-to cluster ---
        btn(tb, "Set Copy Folder", self._select_copy_folder).pack(side=tk.LEFT, padx=3)

        self.copy_folder_var = tk.StringVar(value="(none)")
        self.copy_folder_label = tk.Label(
            tb, textvariable=self.copy_folder_var,
            bg=TOOLBAR_BG, fg=LABEL_FG,
            font=("Segoe UI", 12), anchor="w"
        )
        self.copy_folder_label.pack(side=tk.LEFT, padx=(8, 3))

        self.btn_copy = btn(tb, "Copy Here  [C]", self.copy_current)
        self.btn_copy.pack(side=tk.LEFT, padx=3)

        sep()

        # --- Nav buttons ---
        btn(tb, "◀", lambda: self.navigate(-1)).pack(side=tk.LEFT, padx=3)
        btn(tb, "▶", lambda: self.navigate(+1)).pack(side=tk.LEFT, padx=3)

        self._highlight_zoom_btn()

    def _build_canvas_area(self):
        frame = tk.Frame(self.root, bg=CANVAS_BG)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.v_scroll = tk.Scrollbar(frame, orient=tk.VERTICAL)
        self.h_scroll = tk.Scrollbar(frame, orient=tk.HORIZONTAL)

        self.canvas = tk.Canvas(
            frame, bg=CANVAS_BG, highlightthickness=0,
            yscrollcommand=self.v_scroll.set,
            xscrollcommand=self.h_scroll.set
        )

        self.v_scroll.config(command=self.canvas.yview)
        self.h_scroll.config(command=self.canvas.xview)

        self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.v_scroll.pack(side=tk.RIGHT,  fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<MouseWheel>",   self._on_mousewheel_y)
        self.canvas.bind("<Shift-MouseWheel>", self._on_mousewheel_x)
        # Middle-drag to pan
        self.canvas.bind("<ButtonPress-2>",   self._pan_start)
        self.canvas.bind("<B2-Motion>",        self._pan_move)
        # Left-drag to pan as well (handy when zoomed in)
        self.canvas.bind("<ButtonPress-1>",   self._pan_start)
        self.canvas.bind("<B1-Motion>",        self._pan_move)

    # --------------------------------------------------------------- keys --

    def _bind_keys(self):
        self.root.bind("<Left>",       lambda _: self.navigate(-1))
        self.root.bind("<Right>",      lambda _: self.navigate(+1))
        self.root.bind("f",            lambda _: self.set_zoom("fit"))
        self.root.bind("F",            lambda _: self.set_zoom("fit"))
        self.root.bind("1",            lambda _: self.set_zoom("100"))
        self.root.bind("2",            lambda _: self.set_zoom("200"))
        self.root.bind("c",            lambda _: self.copy_current())
        self.root.bind("C",            lambda _: self.copy_current())
        self.root.bind("<F11>",        lambda _: self._toggle_fullscreen())
        self.root.bind("<Escape>",     lambda _: self._exit_fullscreen())

    # ----------------------------------------------------------- pan support --

    def _pan_start(self, event):
        self.canvas.scan_mark(event.x, event.y)

    def _pan_move(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _on_mousewheel_y(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_mousewheel_x(self, event):
        self.canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")

    # --------------------------------------------------------- file loading --

    def _load_folder_and_show(self, filepath: str):
        filepath = os.path.abspath(filepath)
        folder = os.path.dirname(filepath)

        all_files = sorted(
            (
                os.path.join(folder, f)
                for f in os.listdir(folder)
                if Path(f).suffix.lower() in SUPPORTED_EXTENSIONS
            ),
            key=lambda p: p.lower()
        )

        self.folder_files = all_files
        try:
            self.current_index = self.folder_files.index(filepath)
        except ValueError:
            self.current_index = 0

        self._show_current()

    # ---------------------------------------------------------- image cache --

    def _decode(self, filepath: str) -> Image.Image:
        """Open, EXIF-rotate, and fully decode an image file."""
        img = Image.open(filepath)
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.load()   # force full decode now rather than on first pixel access
        return img

    def _preload(self, filepath: str):
        """Decode filepath on a background thread and store in cache."""
        if filepath in self._cache or filepath in self._loading:
            return
        self._loading.add(filepath)
        def _work():
            try:
                self._cache[filepath] = self._decode(filepath)
            except Exception:
                pass
            finally:
                self._loading.discard(filepath)
        threading.Thread(target=_work, daemon=True).start()

    def _get_cached(self, filepath: str) -> Image.Image:
        """Return image from cache, decoding synchronously if not ready yet."""
        if filepath not in self._cache:
            self._cache[filepath] = self._decode(filepath)
        return self._cache[filepath]

    def _trim_cache(self):
        """Keep only the 5 images nearest the current index."""
        keep = {
            self.folder_files[(self.current_index + o) % len(self.folder_files)]
            for o in (-2, -1, 0, 1, 2)
        }
        for key in list(self._cache.keys()):
            if key not in keep:
                del self._cache[key]

    # --------------------------------------------------------- file loading --

    def _show_current(self):
        if not self.folder_files:
            return

        filepath = self.folder_files[self.current_index]
        self.current_file = filepath

        try:
            self.pil_image = self._get_cached(filepath)
        except Exception as exc:
            messagebox.showerror("Cannot open image", str(exc))
            return

        name = os.path.basename(filepath)
        w, h = self.pil_image.size
        n, total = self.current_index + 1, len(self.folder_files)
        self.info_var.set(f"{name}   {w} × {h}   {n} / {total}")
        self.root.title(f"{name} — Photo Viewer")

        self._render()

        # Kick off background pre-loading for neighbours, evict distant cache
        n = len(self.folder_files)
        for offset in (1, -1, 2, -2):
            self._preload(self.folder_files[(self.current_index + offset) % n])
        self._trim_cache()

    # ------------------------------------------------------------ rendering --

    def _render(self):
        if self.pil_image is None:
            return

        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 2 or ch < 2:
            self.root.after(50, self._render)
            return

        iw, ih = self.pil_image.size

        if self.zoom_mode == "fit":
            scale = min(cw / iw, ch / ih)
        elif self.zoom_mode == "100":
            scale = 1.0
        elif self.zoom_mode == "200":
            scale = 2.0
        else:
            scale = 1.0

        nw = max(1, int(iw * scale))
        nh = max(1, int(ih * scale))

        # BILINEAR is ~4x faster than LANCZOS for the fit downscale; use LANCZOS only when upscaling
        resample = Image.BILINEAR if (nw < iw or nh < ih) else Image.LANCZOS
        resized = self.pil_image.resize((nw, nh), resample)
        self.tk_image = ImageTk.PhotoImage(resized)

        # Center on canvas; allow scroll region to expand when image > canvas
        region_w = max(nw, cw)
        region_h = max(nh, ch)

        self.canvas.delete("all")
        self.canvas.create_image(region_w // 2, region_h // 2,
                                  anchor=tk.CENTER, image=self.tk_image)
        self.canvas.configure(scrollregion=(0, 0, region_w, region_h))

        # Reset scroll to top-left when switching images in fit mode
        if self.zoom_mode == "fit":
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)

        self._highlight_zoom_btn()

    def _on_canvas_resize(self, _event):
        if self.zoom_mode == "fit":
            # Debounce rapid resize events
            if self._resize_job:
                self.root.after_cancel(self._resize_job)
            self._resize_job = self.root.after(40, self._render)

    # ----------------------------------------------------------- navigation --

    def navigate(self, direction: int):
        if not self.folder_files:
            return
        self.current_index = (self.current_index + direction) % len(self.folder_files)
        self._show_current()

    # --------------------------------------------------------------- zoom --

    def set_zoom(self, mode: str):
        self.zoom_mode = mode
        self._render()

    def _highlight_zoom_btn(self):
        for mode, btn in (("fit", self.btn_fit), ("100", self.btn_100), ("200", self.btn_200)):
            if mode == self.zoom_mode:
                btn.config(bg=BTN_ACTIVE_BG, fg=BTN_ACTIVE_FG)
            else:
                btn.config(bg=BTN_BG, fg=BTN_FG)

    # --------------------------------------------------------- copy-to logic --

    def _select_copy_folder(self):
        folder = filedialog.askdirectory(title="Select Copy Destination Folder")
        if folder:
            self.copy_to_folder = folder
            display = folder if len(folder) <= 55 else f"…{folder[-52:]}"
            self.copy_folder_var.set(display)
            self.copy_folder_label.config(fg=COPY_LABEL_FG)

    def copy_current(self):
        if not self.copy_to_folder:
            messagebox.showwarning(
                "No copy folder",
                "Use 'Set Copy Folder' to choose a destination first."
            )
            return
        if not self.current_file:
            return

        filename = os.path.basename(self.current_file)
        dest = os.path.join(self.copy_to_folder, filename)

        if os.path.exists(dest):
            if not messagebox.askyesno(
                "Overwrite?",
                f"'{filename}' already exists in the destination.\nOverwrite it?"
            ):
                return

        try:
            shutil.copy2(self.current_file, dest)
        except OSError as exc:
            messagebox.showerror("Copy failed", str(exc))
            return

        # Brief visual confirmation
        orig_text = self.btn_copy.cget("text")
        self.btn_copy.config(bg="#2e7d32", fg="#ffffff", text="Copied!")
        self.root.after(1200, lambda: self.btn_copy.config(
            bg=BTN_BG, fg=BTN_FG, text=orig_text))

    # --------------------------------------------------------- fullscreen --

    def _toggle_fullscreen(self):
        going_full = not self.root.attributes("-fullscreen")
        self.root.attributes("-fullscreen", going_full)
        if going_full:
            self.toolbar.pack_forget()
            self.h_scroll.pack_forget()
            self.v_scroll.pack_forget()
        else:
            self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self.canvas.master)
            self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
            self.v_scroll.pack(side=tk.RIGHT,  fill=tk.Y)

    def _exit_fullscreen(self):
        if self.root.attributes("-fullscreen"):
            self.root.attributes("-fullscreen", False)
            self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self.canvas.master)
            self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
            self.v_scroll.pack(side=tk.RIGHT,  fill=tk.Y)

    # --------------------------------------------------------- EXIF info --

    def _show_exif(self):
        if not self.current_file:
            return

        try:
            img = Image.open(self.current_file)
            raw = img.getexif()
        except Exception as exc:
            messagebox.showerror("EXIF error", str(exc))
            return

        # Tag IDs we care about
        TAGS = {
            271:  "Camera make",
            272:  "Camera model",
            33434: "Shutter speed",
            33437: "Aperture",
            34855: "ISO",
            37386: "Focal length",
            41989: "Focal length (35mm)",
            42036: "Lens model",
            42035: "Lens make",
            36867: "Date taken",
            37378: "Aperture (APEX)",
            41986: "Exposure program",
            41987: "White balance",
            41988: "Digital zoom",
            37380: "Exposure bias",
            41985: "Custom rendered",
        }

        def fmt_val(tag_id, val):
            if tag_id == 33434:  # ExposureTime
                if isinstance(val, tuple) and len(val) == 2:
                    n, d = val
                else:
                    try:
                        n, d = val.numerator, val.denominator
                    except Exception:
                        return str(val)
                if d == 0:
                    return str(val)
                secs = n / d
                if secs >= 1:
                    return f"{secs:.1f}s"
                return f"1/{round(d/n)}s"
            if tag_id == 33437:  # FNumber
                try:
                    return f"f/{float(val):.1f}"
                except Exception:
                    return str(val)
            if tag_id in (37386, 41989):  # FocalLength
                try:
                    return f"{float(val):.0f} mm"
                except Exception:
                    return str(val)
            if tag_id == 37380:  # ExposureBias
                try:
                    v = float(val)
                    return f"{v:+.1f} EV"
                except Exception:
                    return str(val)
            return str(val)

        lines = []
        for tag_id, label in TAGS.items():
            val = raw.get(tag_id)
            if val is None:
                continue
            lines.append((label, fmt_val(tag_id, val)))

        # Also check IFD 0x8769 (Exif sub-IFD) for tags not in root
        try:
            from PIL.ExifTags import IFD
            sub = raw.get_ifd(IFD.Exif)
            for tag_id, label in TAGS.items():
                if tag_id not in raw and tag_id in sub:
                    lines.append((label, fmt_val(tag_id, sub[tag_id])))
        except Exception:
            pass

        # Build popup
        win = tk.Toplevel(self.root)
        win.title("Photo Info")
        win.configure(bg=DARK_BG)
        win.resizable(False, False)

        if not lines:
            tk.Label(win, text="No EXIF data found.", bg=DARK_BG, fg=LABEL_FG,
                     font=("Segoe UI", 12), padx=20, pady=20).pack()
        else:
            frame = tk.Frame(win, bg=DARK_BG, padx=20, pady=16)
            frame.pack()
            for i, (label, value) in enumerate(lines):
                bg = "#252525" if i % 2 == 0 else DARK_BG
                row = tk.Frame(frame, bg=bg)
                row.pack(fill=tk.X)
                tk.Label(row, text=label, bg=bg, fg=LABEL_FG,
                         font=("Segoe UI", 11), width=22, anchor="w",
                         padx=8, pady=4).pack(side=tk.LEFT)
                tk.Label(row, text=value, bg=bg, fg=BTN_FG,
                         font=("Segoe UI", 11), anchor="w",
                         padx=8, pady=4).pack(side=tk.LEFT)

        tk.Button(win, text="Close", command=win.destroy,
                  bg=BTN_BG, fg=BTN_FG, relief=tk.FLAT, padx=16, pady=4,
                  font=("Segoe UI", 11), cursor="hand2").pack(pady=(0, 14))
        win.bind("<Escape>", lambda _: win.destroy())


# ------------------------------------------------------------------ entry --

def main():
    # Tell Windows this process handles its own DPI scaling.
    # Without this, Windows silently doubles all sizes at 200% scaling.
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()

    # Tell Tk the real DPI so its point-to-pixel conversion is correct.
    try:
        from ctypes import windll
        dpi = windll.user32.GetDpiForSystem()
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass

    initial = sys.argv[1] if len(sys.argv) > 1 else None
    PhotoViewer(root, initial)
    root.mainloop()


if __name__ == "__main__":
    main()
