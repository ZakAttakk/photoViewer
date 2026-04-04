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
    Escape         Exit fullscreen / cancel crop
"""

import math
import os
import sys
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path

import numpy as np

try:
    from PIL import Image, ImageTk, ImageOps, ImageEnhance
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow"])
    from PIL import Image, ImageTk, ImageOps, ImageEnhance


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp"}

DARK_BG       = "#1e1e1e"
TOOLBAR_BG    = "#2d2d2d"
PANEL_BG      = "#252525"
BTN_BG        = "#3c3c3c"
BTN_FG        = "#e0e0e0"
BTN_ACTIVE_BG = "#0078d4"
BTN_ACTIVE_FG = "#ffffff"
LABEL_FG      = "#9e9e9e"
CANVAS_BG     = "#141414"
COPY_LABEL_FG = "#4fc3f7"
SECTION_FG    = "#6e9fd4"
PANEL_WIDTH   = 384

# (key, label, min, max)
EDIT_SLIDERS = [
    # Tone
    ("brightness",  "Brightness",   -100, 100),
    ("contrast",    "Contrast",     -100, 100),
    ("white_point", "White Point",  -100, 100),
    ("highlights",  "Highlights",   -100, 100),
    ("shadows",     "Shadows",      -100, 100),
    ("black_point", "Black Point",  -100, 100),
    # Color
    ("saturation",  "Saturation",   -100, 100),
    ("warmth",      "Warmth",       -100, 100),
    ("tint",        "Tint",         -100, 100),
    ("skin_tone",   "Skin Tone",    -100, 100),
    # Effects
    ("pop",         "Pop",            0,  100),
    ("vignette",    "Vignette",       0,  100),
]


class PhotoViewer:
    def __init__(self, root: tk.Tk, initial_file: str | None = None):
        self.root = root
        self.root.title("Photo Viewer")
        self.root.configure(bg=DARK_BG)
        self.root.minsize(400, 300)
        self.root.state("zoomed")

        # Image state
        self.folder_files: list[str] = []
        self.current_index: int = 0
        self.current_file: str | None = None
        self.pil_image: Image.Image | None = None
        self.tk_image:  ImageTk.PhotoImage | None = None
        self.zoom_mode: str = "fit"
        self.copy_to_folder: str | None = None
        self._resize_job = None

        # Cache
        self._cache: dict[str, Image.Image] = {}
        self._loading: set[str] = set()

        # Edit state
        self._edit_visible = False
        self._edit_vars: dict[str, tk.IntVar] = {}
        self._edit_debounce: int | None = None
        self._edit_controls: list = []  # Scale + ± buttons, disabled during crop

        # Retouch (dodge/burn) state
        self._retouch_mask = None       # np.ndarray float32 (H, W), values -1..1
        self._retouch_active = False
        self._retouch_mode = "dodge"
        self._retouch_size_var = None   # tk.IntVar set in _build_edit_panel
        self._retouch_last_pos = None   # (ix, iy) for stroke interpolation
        self._retouch_cursor_id = None  # canvas oval for brush preview

        # Crop state
        self._crop_active     = False
        self._crop_lock_ratio = False
        self._crop_rect_id: int | None = None
        self._crop_ix0 = self._crop_iy0 = 0.0
        self._crop_ix1 = self._crop_iy1 = 0.0
        # drag-handle state
        self._crop_drag_mode  = "new"   # "new"|"move"|"corner_TL/TR/BL/BR"|"edge_T/B/L/R"
        self._crop_drag_six   = 0.0     # image x at drag start
        self._crop_drag_siy   = 0.0     # image y at drag start
        self._crop_rect_start = (0.0, 0.0, 0.0, 0.0)

        # Last render metadata — image position in canvas coordinates
        self._rs_scale = 1.0
        self._rs_img_x = 0
        self._rs_img_y = 0

        self._build_ui()
        self._bind_keys()

        if initial_file and os.path.isfile(initial_file):
            self._load_folder_and_show(initial_file)

    # ──────────────────────────────────────────────────────────── UI build ──

    def _build_ui(self):
        self._build_toolbar()
        self._main_area = tk.Frame(self.root, bg=DARK_BG)
        self._main_area.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        # Build edit panel first (not packed yet) so canvas can expand into remaining space
        self._build_edit_panel()
        self._build_canvas_area()

    def _build_toolbar(self):
        self.toolbar = tk.Frame(self.root, bg=TOOLBAR_BG, pady=4)
        self.toolbar.pack(side=tk.TOP, fill=tk.X)
        tb = self.toolbar

        def btn(parent, text, cmd, **kw):
            return tk.Button(
                parent, text=text, command=cmd,
                bg=BTN_BG, fg=BTN_FG,
                activebackground=BTN_ACTIVE_BG, activeforeground=BTN_ACTIVE_FG,
                relief=tk.FLAT, bd=0, padx=12, pady=3,
                font=("Segoe UI", 12), cursor="hand2", **kw
            )

        def sep():
            tk.Frame(tb, bg="#555555", width=1).pack(side=tk.LEFT, padx=8, pady=3, fill=tk.Y)

        # RIGHT side first so left buttons don't crowd them out
        self.info_var = tk.StringVar(value="")
        tk.Label(tb, textvariable=self.info_var,
                 bg=TOOLBAR_BG, fg=LABEL_FG, font=("Segoe UI", 12)
                 ).pack(side=tk.RIGHT, padx=12)
        btn(tb, "Full Screen  [F11]", self._toggle_fullscreen).pack(side=tk.RIGHT, padx=(3, 6))
        btn(tb, "Info", self._show_exif).pack(side=tk.RIGHT, padx=3)
        self.btn_edit = btn(tb, "Edit", self._toggle_edit)
        self.btn_edit.pack(side=tk.RIGHT, padx=3)

        # LEFT: zoom
        self.btn_fit = btn(tb, "Fit  [F]",  lambda: self.set_zoom("fit"))
        self.btn_fit.pack(side=tk.LEFT, padx=(10, 3))
        self.btn_100 = btn(tb, "100%  [1]", lambda: self.set_zoom("100"))
        self.btn_100.pack(side=tk.LEFT, padx=3)
        self.btn_200 = btn(tb, "200%  [2]", lambda: self.set_zoom("200"))
        self.btn_200.pack(side=tk.LEFT, padx=3)
        sep()

        # LEFT: copy-to
        btn(tb, "Set Copy Folder", self._select_copy_folder).pack(side=tk.LEFT, padx=3)
        self.copy_folder_var = tk.StringVar(value="(none)")
        self.copy_folder_label = tk.Label(
            tb, textvariable=self.copy_folder_var,
            bg=TOOLBAR_BG, fg=LABEL_FG, font=("Segoe UI", 12), anchor="w"
        )
        self.copy_folder_label.pack(side=tk.LEFT, padx=(8, 3))
        self.btn_copy = btn(tb, "Copy Here  [C]", self.copy_current)
        self.btn_copy.pack(side=tk.LEFT, padx=3)
        sep()

        # LEFT: navigation
        btn(tb, "◀", lambda: self.navigate(-1)).pack(side=tk.LEFT, padx=3)
        btn(tb, "▶", lambda: self.navigate(+1)).pack(side=tk.LEFT, padx=3)

        self._highlight_zoom_btn()

    def _build_canvas_area(self):
        frame = tk.Frame(self._main_area, bg=CANVAS_BG)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._canvas_frame = frame

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

        self.canvas.bind("<Configure>",       self._on_canvas_resize)
        self.canvas.bind("<MouseWheel>",       self._on_mousewheel_y)
        self.canvas.bind("<Shift-MouseWheel>", self._on_mousewheel_x)
        self.canvas.bind("<ButtonPress-2>",    self._pan_start)
        self.canvas.bind("<B2-Motion>",         self._pan_move)
        self.canvas.bind("<ButtonPress-1>",    self._on_lpress)
        self.canvas.bind("<B1-Motion>",         self._on_ldrag)
        self.canvas.bind("<ButtonRelease-1>",  self._on_lrelease)
        self.canvas.bind("<Motion>",           self._on_canvas_motion)

    def _build_edit_panel(self):
        self._edit_frame = tk.Frame(self._main_area, bg=PANEL_BG, width=PANEL_WIDTH)
        self._edit_frame.pack_propagate(False)  # keep fixed width
        # Not packed yet — shown when the Edit button is toggled

        # Scrollable interior
        sc = tk.Canvas(self._edit_frame, bg=PANEL_BG, highlightthickness=0)
        vbar = tk.Scrollbar(self._edit_frame, orient=tk.VERTICAL, command=sc.yview)
        sc.configure(yscrollcommand=vbar.set)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        sc.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        content = tk.Frame(sc, bg=PANEL_BG)
        cwin = sc.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda e: sc.configure(scrollregion=sc.bbox("all")))
        sc.bind("<Configure>",      lambda e: sc.itemconfig(cwin, width=e.width))

        def panel_wheel(e):
            sc.yview_scroll(int(-1 * (e.delta / 120)), "units")
        sc.bind("<MouseWheel>", panel_wheel)
        content.bind("<MouseWheel>", panel_wheel)

        # ── helpers ──────────────────────────────────────────────────────────

        def section(text):
            tk.Label(content, text=text.upper(), bg=PANEL_BG, fg=SECTION_FG,
                     font=("Segoe UI", 9, "bold"), anchor="w"
                     ).pack(fill=tk.X, padx=14, pady=(14, 2))
            tk.Frame(content, bg="#3a3a3a", height=1).pack(fill=tk.X, padx=14, pady=(0, 6))

        def pbtn(parent, text, cmd, color=BTN_BG):
            return tk.Button(
                parent, text=text, command=cmd,
                bg=color, fg=BTN_FG,
                activebackground=BTN_ACTIVE_BG, activeforeground=BTN_ACTIVE_FG,
                relief=tk.FLAT, bd=0, padx=10, pady=3,
                font=("Segoe UI", 11), cursor="hand2"
            )

        # ── Crop ─────────────────────────────────────────────────────────────

        section("Crop")
        self.btn_crop_orig = pbtn(content, "Original Ratio", lambda: self._start_crop(lock=True))
        self.btn_crop_orig.pack(fill=tk.X, padx=14, pady=(0, 6))
        self.btn_crop_free = pbtn(content, "Freeform", lambda: self._start_crop(lock=False))
        self.btn_crop_free.pack(fill=tk.X, padx=14, pady=(0, 4))

        # Apply / Cancel — shown below crop buttons while a crop is active
        self._crop_action_frame = tk.Frame(content, bg=PANEL_BG)
        # Not packed yet; _start_crop will pack it
        pbtn(self._crop_action_frame, "Apply Crop",
             self._apply_crop, color="#2e7d32").pack(fill=tk.X, padx=14, pady=(4, 4))
        pbtn(self._crop_action_frame, "Cancel",
             self._cancel_crop).pack(fill=tk.X, padx=14, pady=(0, 4))

        # ── Tone sliders ──────────────────────────────────────────────────────

        section("Tone")
        for key, label, mn, mx in EDIT_SLIDERS[:6]:
            self._add_slider(content, panel_wheel, key, label, mn, mx)

        # ── Color sliders ─────────────────────────────────────────────────────

        section("Color")
        for key, label, mn, mx in EDIT_SLIDERS[6:10]:
            self._add_slider(content, panel_wheel, key, label, mn, mx)

        # ── Effects sliders ───────────────────────────────────────────────────

        section("Effects")
        for key, label, mn, mx in EDIT_SLIDERS[10:]:
            self._add_slider(content, panel_wheel, key, label, mn, mx)

        # ── Retouch ───────────────────────────────────────────────────────────

        section("Retouch")
        self.btn_dodge = pbtn(content, "Dodge", lambda: self._start_retouch("dodge"))
        self.btn_dodge.pack(fill=tk.X, padx=14, pady=(0, 6))
        self.btn_burn  = pbtn(content, "Burn",  lambda: self._start_retouch("burn"))
        self.btn_burn.pack(fill=tk.X, padx=14, pady=(0, 10))

        # Brush size row: label | value | − | +
        self._retouch_size_var = tk.IntVar(value=30)
        br_row = tk.Frame(content, bg=PANEL_BG)
        br_row.pack(fill=tk.X, padx=14, pady=(0, 8))
        tk.Label(br_row, text="Brush size", bg=PANEL_BG, fg=BTN_FG,
                 font=("Segoe UI", 11), anchor="w").pack(side=tk.LEFT)
        tk.Label(br_row, textvariable=self._retouch_size_var, bg=PANEL_BG, fg=LABEL_FG,
                 font=("Segoe UI", 11), width=4, anchor="e").pack(side=tk.LEFT, padx=(4, 0))

        def br_dec():
            self._retouch_size_var.set(max(5, self._retouch_size_var.get() - 5))
            self._update_retouch_cursor_size()
        def br_inc():
            self._retouch_size_var.set(min(300, self._retouch_size_var.get() + 5))
            self._update_retouch_cursor_size()

        br_inc_frame = tk.Frame(br_row, width=40, height=40, bg=BTN_BG)
        br_inc_frame.pack_propagate(False)
        br_inc_frame.pack(side=tk.RIGHT)
        tk.Button(br_inc_frame, text="+", command=br_inc,
                  bg=BTN_BG, fg=BTN_FG, activebackground=BTN_ACTIVE_BG,
                  activeforeground=BTN_ACTIVE_FG, relief=tk.FLAT, bd=0,
                  font=("Segoe UI", 10), cursor="hand2").pack(fill=tk.BOTH, expand=True)

        br_dec_frame = tk.Frame(br_row, width=40, height=40, bg=BTN_BG)
        br_dec_frame.pack_propagate(False)
        br_dec_frame.pack(side=tk.RIGHT, padx=(0, 4))
        tk.Button(br_dec_frame, text="−", command=br_dec,
                  bg=BTN_BG, fg=BTN_FG, activebackground=BTN_ACTIVE_BG,
                  activeforeground=BTN_ACTIVE_FG, relief=tk.FLAT, bd=0,
                  font=("Segoe UI", 10), cursor="hand2").pack(fill=tk.BOTH, expand=True)

        self._btn_clear_strokes = pbtn(content, "Clear Strokes", self._clear_retouch)
        self._btn_bake_retouch  = pbtn(content, "Bake into Image", self._bake_retouch)

        # ── Bottom buttons ────────────────────────────────────────────────────

        self._edit_bottom_sep = tk.Frame(content, bg="#3a3a3a", height=1)
        self._edit_bottom_sep.pack(fill=tk.X, padx=14, pady=(16, 8))
        self._btn_save_copy = pbtn(content, "Save Copy", self._save_edited_copy, color="#1565c0")
        self._btn_save_copy.pack(fill=tk.X, padx=14, pady=(0, 6))
        self._btn_reset = pbtn(content, "Reset All", self._reset_edits)
        self._btn_reset.pack(fill=tk.X, padx=14, pady=(0, 16))

        # Bind scroll to every non-Scale widget so 2-finger scroll always works
        def _bind_wheel_all(widget):
            if not isinstance(widget, tk.Scale):
                widget.bind("<MouseWheel>", panel_wheel)
            for child in widget.winfo_children():
                _bind_wheel_all(child)
        _bind_wheel_all(content)

    def _add_slider(self, parent, panel_wheel, key, label, mn, mx):
        var = tk.IntVar(value=0)
        self._edit_vars[key] = var

        # Label row
        row = tk.Frame(parent, bg=PANEL_BG)
        row.pack(fill=tk.X, padx=14, pady=(6, 0))
        tk.Label(row, text=label, bg=PANEL_BG, fg=BTN_FG,
                 font=("Segoe UI", 11), anchor="w", width=13).pack(side=tk.LEFT)
        tk.Label(row, textvariable=var, bg=PANEL_BG, fg=LABEL_FG,
                 font=("Segoe UI", 11), width=4, anchor="e").pack(side=tk.RIGHT)

        # Slider row with − and + buttons
        def dec(): var.set(max(mn, var.get() - 1)); self._schedule_edit_render()
        def inc(): var.set(min(mx, var.get() + 1)); self._schedule_edit_render()

        sl_row = tk.Frame(parent, bg=PANEL_BG)
        sl_row.pack(fill=tk.X, padx=14, pady=(2, 0))

        dec_frame = tk.Frame(sl_row, width=40, height=40, bg=BTN_BG)
        dec_frame.pack_propagate(False)
        dec_frame.pack(side=tk.LEFT, padx=(0, 4))
        dec_btn = tk.Button(dec_frame, text="−", command=dec,
                            bg=BTN_BG, fg=BTN_FG,
                            activebackground=BTN_ACTIVE_BG, activeforeground=BTN_ACTIVE_FG,
                            relief=tk.FLAT, bd=0, font=("Segoe UI", 10),
                            cursor="hand2")
        dec_btn.pack(fill=tk.BOTH, expand=True)

        sl = tk.Scale(sl_row, variable=var, from_=mn, to=mx,
                      orient=tk.HORIZONTAL, showvalue=False,
                      bg=PANEL_BG, troughcolor="#3c3c3c",
                      activebackground=BTN_ACTIVE_BG,
                      highlightthickness=0, bd=0,
                      sliderlength=22, width=40,
                      command=lambda _: self._schedule_edit_render())
        sl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        inc_frame = tk.Frame(sl_row, width=40, height=40, bg=BTN_BG)
        inc_frame.pack_propagate(False)
        inc_frame.pack(side=tk.LEFT, padx=(4, 0))
        inc_btn = tk.Button(inc_frame, text="+", command=inc,
                            bg=BTN_BG, fg=BTN_FG,
                            activebackground=BTN_ACTIVE_BG, activeforeground=BTN_ACTIVE_FG,
                            relief=tk.FLAT, bd=0, font=("Segoe UI", 10),
                            cursor="hand2")
        inc_btn.pack(fill=tk.BOTH, expand=True)

        self._edit_controls.extend([dec_btn, sl, inc_btn])

        # Zero-tick indicator for sliders with a true centre
        if mn < 0:
            tick = tk.Canvas(parent, bg=PANEL_BG, height=6, highlightthickness=0)
            tick.pack(fill=tk.X, padx=14, pady=(0, 2))
            def _draw_tick(e, c=tick):
                c.delete("all")
                w = c.winfo_width()
                if w > 4:
                    x = w // 2
                    c.create_line(x, 0, x, 6, fill="#666666", width=1)
            tick.bind("<Configure>", _draw_tick)
            tick.bind("<MouseWheel>", panel_wheel)
        else:
            tk.Frame(parent, bg=PANEL_BG, height=4).pack()

        def on_wheel(e):
            panel_wheel(e)
            return "break"

        def on_click(e):
            if sl['state'] == tk.DISABLED:
                return
            half_knob = 11  # sliderlength / 2
            trough_w = max(1, sl.winfo_width() - 2 * half_knob)
            rel = max(0.0, min(1.0, (e.x - half_knob) / trough_w))
            var.set(int(round(mn + rel * (mx - mn))))
            self._schedule_edit_render()

        sl.bind("<MouseWheel>", on_wheel)
        sl.bind("<Button-1>", on_click)
        sl_row.bind("<MouseWheel>", panel_wheel)
        row.bind("<MouseWheel>", panel_wheel)

    # ────────────────────────────────────────────────────────────── keys ──

    def _bind_keys(self):
        self.root.bind("<Left>",   lambda _: self.navigate(-1))
        self.root.bind("<Right>",  lambda _: self.navigate(+1))
        self.root.bind("f",        lambda _: self.set_zoom("fit"))
        self.root.bind("F",        lambda _: self.set_zoom("fit"))
        self.root.bind("1",        lambda _: self.set_zoom("100"))
        self.root.bind("2",        lambda _: self.set_zoom("200"))
        self.root.bind("c",        lambda _: self.copy_current())
        self.root.bind("C",        lambda _: self.copy_current())
        self.root.bind("<F11>",    lambda _: self._toggle_fullscreen())
        self.root.bind("<Escape>", lambda _: self._on_escape())

    def _on_escape(self):
        if self._crop_active:
            self._cancel_crop()
        elif self._retouch_active:
            self._stop_retouch()
        elif self.root.attributes("-fullscreen"):
            self._exit_fullscreen()

    # ─────────────────────────────────────────────────────────── mouse ──

    def _pan_start(self, event):
        self.canvas.scan_mark(event.x, event.y)

    def _pan_move(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _on_lpress(self, event):
        if self._crop_active:
            self._crop_press(event)
        elif self._retouch_active:
            self._retouch_last_pos = None
            ix, iy = self._canvas_to_img(event.x, event.y)
            self._paint_stroke_to(ix, iy)
            self._schedule_edit_render()
        else:
            self._pan_start(event)

    def _on_ldrag(self, event):
        if self._crop_active:
            self._crop_drag(event)
        else:
            self._pan_move(event)

    def _on_lrelease(self, event):
        if self._crop_active:
            self._crop_drag(event)
            # After a fresh draw, normalize so ix0<ix1, iy0<iy1
            # so handle hit-testing is well-defined
            if self._crop_drag_mode == "new":
                self._crop_ix0, self._crop_ix1 = (
                    min(self._crop_ix0, self._crop_ix1),
                    max(self._crop_ix0, self._crop_ix1))
                self._crop_iy0, self._crop_iy1 = (
                    min(self._crop_iy0, self._crop_iy1),
                    max(self._crop_iy0, self._crop_iy1))
        elif self._retouch_active:
            self._retouch_last_pos = None

    def _on_mousewheel_y(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_mousewheel_x(self, event):
        self.canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")

    # ──────────────────────────────────────────────────── file loading ──

    def _load_folder_and_show(self, filepath: str):
        filepath = os.path.abspath(filepath)
        folder = os.path.dirname(filepath)
        all_files = sorted(
            (os.path.join(folder, f) for f in os.listdir(folder)
             if Path(f).suffix.lower() in SUPPORTED_EXTENSIONS),
            key=lambda p: p.lower()
        )
        self.folder_files = all_files
        try:
            self.current_index = self.folder_files.index(filepath)
        except ValueError:
            self.current_index = 0
        self._show_current()

    # ───────────────────────────────────────────────────── image cache ──

    def _decode(self, filepath: str) -> Image.Image:
        img = Image.open(filepath)
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.load()
        return img

    def _preload(self, filepath: str):
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
        if filepath not in self._cache:
            self._cache[filepath] = self._decode(filepath)
        return self._cache[filepath]

    def _trim_cache(self):
        keep = {
            self.folder_files[(self.current_index + o) % len(self.folder_files)]
            for o in (-2, -1, 0, 1, 2)
        }
        for key in list(self._cache):
            if key not in keep:
                del self._cache[key]

    # ────────────────────────────────────────────────── show / render ──

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
        nf = len(self.folder_files)
        for offset in (1, -1, 2, -2):
            self._preload(self.folder_files[(self.current_index + offset) % nf])
        self._trim_cache()

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

        if nw > iw or nh > ih:
            # Zoom > 100%: apply edits at original resolution, then upscale
            display = self._apply_edits(self.pil_image) if self._edit_visible else self.pil_image.copy()
            display = display.resize((nw, nh), Image.BILINEAR)
        elif nw == iw and nh == ih:
            # Zoom 100%: skip the pointless same-size resize
            display = self.pil_image.copy()
            if self._edit_visible:
                display = self._apply_edits(display)
        else:
            # Zoom < 100%: downscale first so edits run on a small image
            display = self.pil_image.resize((nw, nh), Image.BILINEAR)
            if self._edit_visible:
                display = self._apply_edits(display)

        self.tk_image = ImageTk.PhotoImage(display)

        region_w = max(nw, cw)
        region_h = max(nh, ch)

        # Store image position for crop coordinate conversion
        self._rs_scale = scale
        self._rs_img_x = (region_w - nw) // 2
        self._rs_img_y = (region_h - nh) // 2

        self.canvas.delete("all")
        self._crop_rect_id     = None  # canvas.delete("all") removed it
        self._retouch_cursor_id = None

        self.canvas.create_image(region_w // 2, region_h // 2,
                                  anchor=tk.CENTER, image=self.tk_image)
        self.canvas.configure(scrollregion=(0, 0, region_w, region_h))

        if self.zoom_mode == "fit":
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)

        # Restore crop rect if the user is mid-draw
        if self._crop_active and (self._crop_ix1 != self._crop_ix0 or
                                   self._crop_iy1 != self._crop_iy0):
            cx0, cy0 = self._img_to_canvas(self._crop_ix0, self._crop_iy0)
            cx1, cy1 = self._img_to_canvas(self._crop_ix1, self._crop_iy1)
            self._crop_rect_id = self.canvas.create_rectangle(
                cx0, cy0, cx1, cy1, outline="#ffffff", width=2, dash=(6, 4))

        self._highlight_zoom_btn()

    def _on_canvas_resize(self, _event):
        if self.zoom_mode == "fit":
            if self._resize_job:
                self.root.after_cancel(self._resize_job)
            self._resize_job = self.root.after(40, self._render)

    # ──────────────────────────────────────────────────── navigation ──

    def navigate(self, direction: int):
        if not self.folder_files:
            return
        if self._crop_active:
            self._cancel_crop()
        if self._retouch_active:
            self._stop_retouch()
        self._set_retouch_mask(None)
        self.current_index = (self.current_index + direction) % len(self.folder_files)
        self._show_current()

    # ─────────────────────────────────────────────────────────── zoom ──

    def set_zoom(self, mode: str):
        self.zoom_mode = mode
        self._render()

    def _highlight_zoom_btn(self):
        for mode, b in (("fit", self.btn_fit), ("100", self.btn_100), ("200", self.btn_200)):
            b.config(bg=BTN_ACTIVE_BG if mode == self.zoom_mode else BTN_BG,
                     fg=BTN_ACTIVE_FG if mode == self.zoom_mode else BTN_FG)

    # ──────────────────────────────────────────────────── copy-to ──

    def _select_copy_folder(self):
        folder = filedialog.askdirectory(title="Select Copy Destination Folder")
        if folder:
            self.copy_to_folder = folder
            display = folder if len(folder) <= 55 else f"…{folder[-52:]}"
            self.copy_folder_var.set(display)
            self.copy_folder_label.config(fg=COPY_LABEL_FG)

    def copy_current(self):
        if not self.copy_to_folder:
            messagebox.showwarning("No copy folder",
                "Use 'Set Copy Folder' to choose a destination first.")
            return
        if not self.current_file:
            return
        filename = os.path.basename(self.current_file)
        dest = os.path.join(self.copy_to_folder, filename)
        if os.path.exists(dest):
            if not messagebox.askyesno("Overwrite?",
                    f"'{filename}' already exists in the destination.\nOverwrite it?"):
                return
        try:
            shutil.copy2(self.current_file, dest)
        except OSError as exc:
            messagebox.showerror("Copy failed", str(exc))
            return
        orig = self.btn_copy.cget("text")
        self.btn_copy.config(bg="#2e7d32", fg="#ffffff", text="Copied!")
        self.root.after(1200, lambda: self.btn_copy.config(bg=BTN_BG, fg=BTN_FG, text=orig))

    # ──────────────────────────────────────────────────── fullscreen ──

    def _toggle_fullscreen(self):
        going_full = not self.root.attributes("-fullscreen")
        self.root.attributes("-fullscreen", going_full)
        if going_full:
            self.toolbar.pack_forget()
            self.h_scroll.pack_forget()
            self.v_scroll.pack_forget()
            if self._edit_visible:
                self._edit_frame.pack_forget()
        else:
            self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self._main_area)
            self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
            self.v_scroll.pack(side=tk.RIGHT,  fill=tk.Y)
            if self._edit_visible:
                self._edit_frame.pack(side=tk.RIGHT, fill=tk.Y)

    def _exit_fullscreen(self):
        if self.root.attributes("-fullscreen"):
            self.root.attributes("-fullscreen", False)
            self.toolbar.pack(side=tk.TOP, fill=tk.X, before=self._main_area)
            self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
            self.v_scroll.pack(side=tk.RIGHT,  fill=tk.Y)
            if self._edit_visible:
                self._edit_frame.pack(side=tk.RIGHT, fill=tk.Y)

    # ──────────────────────────────────────────────────── edit panel ──

    def _toggle_edit(self):
        if self._edit_visible:
            self._edit_frame.pack_forget()
            self._edit_visible = False
            self.btn_edit.config(bg=BTN_BG, fg=BTN_FG)
            if self._crop_active:
                self._cancel_crop()
        else:
            self._edit_frame.pack(side=tk.RIGHT, fill=tk.Y)
            self._edit_visible = True
            self.btn_edit.config(bg=BTN_ACTIVE_BG, fg=BTN_ACTIVE_FG)
        self._render()

    def _schedule_edit_render(self):
        if self._edit_debounce:
            self.root.after_cancel(self._edit_debounce)
        self._edit_debounce = self.root.after(40, self._render)


    def _reset_edits(self):
        self._set_retouch_mask(None)
        for var in self._edit_vars.values():
            var.set(0)
        if self.current_file:
            # Reload from disk to undo any crops applied this session
            self.pil_image = self._decode(self.current_file)
            self._cache[self.current_file] = self.pil_image
            w, h = self.pil_image.size
            name = os.path.basename(self.current_file)
            n, total = self.current_index + 1, len(self.folder_files)
            self.info_var.set(f"{name}   {w} × {h}   {n} / {total}")
        self._render()

    def _save_edited_copy(self):
        if not self.current_file or not self.pil_image:
            return
        edited = self._apply_edits(self.pil_image, high_quality_retouch=True)   # full-res, no softening
        if edited.mode != "RGB":
            edited = edited.convert("RGB")
        p    = Path(self.current_file)
        dest = p.with_stem(p.stem + "_edited")
        try:
            if p.suffix.lower() in (".jpg", ".jpeg"):
                edited.save(str(dest), quality=95)
            else:
                edited.save(str(dest))
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return

        # Ask if they want to open the saved copy (sharp, no preview softening)
        open_it = messagebox.askyesno(
            "Saved",
            f"Saved as:\n{dest}\n\nOpen saved copy?")
        if open_it:
            dest_str = str(dest)
            folder   = str(dest.parent)
            self.folder_files = sorted(
                [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff", ".tif"))],
                key=lambda x: x.lower())
            if dest_str in self.folder_files:
                self.current_index = self.folder_files.index(dest_str)
            else:
                self.folder_files.insert(0, dest_str)
                self.current_index = 0
            if self._retouch_active:
                self._stop_retouch()
            self._set_retouch_mask(None)
            self._show_current()

    # ─────────────────────────────────────────── edit: image processing ──

    @staticmethod
    def _lut(fn):
        """Build a 256-entry integer lookup table from a function i → value."""
        return [max(0, min(255, int(fn(i)))) for i in range(256)]

    def _apply_edits(self, img: Image.Image, high_quality_retouch: bool = False) -> Image.Image:
        v = {k: var.get() for k, var in self._edit_vars.items()}

        if img.mode != "RGB":
            img = img.convert("RGB")
        else:
            img = img.copy()

        # ── Tone: black point / white point / shadows / highlights (single LUT pass) ──
        bp = v["black_point"]
        wp = v["white_point"]
        sh = v["shadows"]
        hl = v["highlights"]
        if any((bp, wp, sh, hl)):
            def tone(i):
                x = float(i)
                # black point: positive lifts blacks, negative crushes them
                if bp: x += bp * 1.28 * (1.0 - x / 255)
                # white point: positive boosts whites, negative pulls them down
                if wp: x += wp * 1.28 * (x / 255)
                # shadows: affects lower ¾ of tonal range
                if sh: x += sh * 0.8 * max(0.0, 1.0 - x / 192) ** 0.5
                # highlights: affects upper ¾ of tonal range
                if hl: x += hl * 0.8 * max(0.0, (x - 64) / 191) ** 0.5
                return x
            img = img.point(self._lut(tone) * 3)

        # ── Brightness ──────────────────────────────────────────────────────
        if v["brightness"]:
            img = ImageEnhance.Brightness(img).enhance(max(0.01, 1 + v["brightness"] / 100))

        # ── Contrast ────────────────────────────────────────────────────────
        if v["contrast"]:
            img = ImageEnhance.Contrast(img).enhance(max(0.01, 1 + v["contrast"] / 100))

        # ── Saturation ──────────────────────────────────────────────────────
        if v["saturation"]:
            img = ImageEnhance.Color(img).enhance(max(0.0, 1 + v["saturation"] / 100))

        # ── Warmth: boost red, reduce blue ──────────────────────────────────
        if v["warmth"]:
            w = v["warmth"]
            img = img.point(
                self._lut(lambda i: i + w * 0.30) +   # R
                list(range(256)) +                      # G unchanged
                self._lut(lambda i: i - w * 0.50)      # B
            )

        # ── Tint: green vs magenta axis ──────────────────────────────────────
        if v["tint"]:
            t = v["tint"]
            img = img.point(
                list(range(256)) +                      # R unchanged
                self._lut(lambda i: i + t * 0.40) +    # G
                list(range(256))                        # B unchanged
            )

        # ── Skin tone: bell-curve boost on R around mid-exposure (~140) ──────
        # Approximation — targets the tonal range where skin tones typically land
        if v["skin_tone"]:
            sk = v["skin_tone"]
            img = img.point(
                self._lut(lambda i: i + sk * 0.4 *
                          math.exp(-((i - 140) ** 2) / 9800)) +  # R
                list(range(256)) +                                  # G unchanged
                list(range(256))                                    # B unchanged
            )

        # ── Pop: midtone contrast S-curve + faint saturation lift ────────────
        if v["pop"]:
            p = v["pop"]
            def pop_curve(i):
                x = i / 255.0
                # S-curve: darkens lower midtones, brightens upper midtones
                return (x + (p / 100.0) * 0.5 * 4 * x * (1 - x) * (x - 0.5)) * 255
            img = img.point(self._lut(pop_curve) * 3)
            img = ImageEnhance.Color(img).enhance(1 + p * 0.002)

        # ── Vignette: radial darkening toward corners ─────────────────────────
        if v["vignette"]:
            img = self._apply_vignette(img, v["vignette"] / 100.0)

        # ── Retouch (dodge/burn) ──────────────────────────────────────────────
        if self._retouch_mask is not None:
            img = self._apply_retouch(img, high_quality=high_quality_retouch)

        return img

    # ──────────────────────────────────────────────── retouch (dodge/burn) ──

    _RETOUCH_STRENGTH = 0.10   # fixed 10% per brush pass

    def _start_retouch(self, mode: str):
        """Toggle dodge or burn mode; clicking the active mode turns it off."""
        if self._retouch_active and self._retouch_mode == mode:
            self._stop_retouch()
            return
        self._retouch_active = True
        self._retouch_mode   = mode
        self.canvas.config(cursor="none")
        self.btn_dodge.config(bg=BTN_ACTIVE_BG if mode == "dodge" else BTN_BG,
                               fg=BTN_ACTIVE_FG if mode == "dodge" else BTN_FG)
        self.btn_burn.config( bg=BTN_ACTIVE_BG if mode == "burn"  else BTN_BG,
                               fg=BTN_ACTIVE_FG if mode == "burn"  else BTN_FG)

    def _stop_retouch(self):
        self._retouch_active   = False
        self._retouch_last_pos = None
        if self._retouch_cursor_id:
            self.canvas.delete(self._retouch_cursor_id)
            self._retouch_cursor_id = None
        self.canvas.config(cursor="")
        self.btn_dodge.config(bg=BTN_BG, fg=BTN_FG)
        self.btn_burn.config( bg=BTN_BG, fg=BTN_FG)

    def _set_retouch_mask(self, mask):
        """Set _retouch_mask and show/hide the Clear/Bake buttons accordingly."""
        self._retouch_mask = mask
        if mask is None:
            self._btn_clear_strokes.pack_forget()
            self._btn_bake_retouch.pack_forget()
        else:
            self._btn_clear_strokes.pack(fill=tk.X, padx=14, pady=(0, 6))
            self._btn_bake_retouch.pack( fill=tk.X, padx=14, pady=(0, 10))

    def _clear_retouch(self):
        self._set_retouch_mask(None)
        self._render()

    def _bake_retouch(self):
        """Permanently apply the retouch mask into self.pil_image at full quality."""
        if self._retouch_mask is None or self.pil_image is None:
            return
        if self._retouch_active:
            self._stop_retouch()
        self.root.config(cursor="wait")
        self.root.update_idletasks()
        try:
            baked = self._apply_retouch(self.pil_image, high_quality=True)
            self.pil_image = baked
            self._cache[self.current_file] = baked
            self._set_retouch_mask(None)
        finally:
            self.root.config(cursor="")
        self._render()

    def _update_retouch_cursor(self, wx, wy):
        """Draw/move the circular brush preview on the canvas."""
        if self._rs_scale == 0:
            return
        cx = self.canvas.canvasx(wx)
        cy = self.canvas.canvasy(wy)
        r = self._retouch_size_var.get() * self._rs_scale
        x0, y0, x1, y1 = cx - r, cy - r, cx + r, cy + r
        if self._retouch_cursor_id:
            self.canvas.coords(self._retouch_cursor_id, x0, y0, x1, y1)
        else:
            color = "#ffe066" if self._retouch_mode == "dodge" else "#ff6666"
            self._retouch_cursor_id = self.canvas.create_oval(
                x0, y0, x1, y1, outline=color, width=1, dash=(4, 3))

    def _update_retouch_cursor_size(self):
        """Called when brush size changes — delete cursor so it's redrawn at new size."""
        if self._retouch_cursor_id:
            self.canvas.delete(self._retouch_cursor_id)
            self._retouch_cursor_id = None

    def _paint_stroke_to(self, ix, iy):
        """Paint from last position to (ix, iy), interpolating to avoid gaps."""
        if self._retouch_last_pos is not None:
            lx, ly = self._retouch_last_pos
            dist  = math.sqrt((ix - lx) ** 2 + (iy - ly) ** 2)
            steps = max(1, int(dist / max(1, self._retouch_size_var.get() * 0.25)))
            for i in range(1, steps + 1):
                t = i / steps
                self._paint_at(lx + (ix - lx) * t, ly + (iy - ly) * t)
        else:
            self._paint_at(ix, iy)
        self._retouch_last_pos = (ix, iy)

    # Mask is stored at this max dimension — soft brush strokes need no more detail
    _RETOUCH_MASK_MAX = 1500

    def _retouch_mask_scale(self) -> float:
        """Scale factor: original image px → mask px."""
        iw, ih = self.pil_image.size
        return min(1.0, self._RETOUCH_MASK_MAX / max(iw, ih))

    def _ensure_retouch_mask(self):
        if self._retouch_mask is None:
            iw, ih = self.pil_image.size
            s = self._retouch_mask_scale()
            self._set_retouch_mask(np.zeros(
                (max(1, int(ih * s)), max(1, int(iw * s))), dtype=np.float32))

    def _paint_at(self, ix, iy):
        """Stamp a soft Gaussian brush at image coordinate (ix, iy)."""
        if self.pil_image is None:
            return
        self._ensure_retouch_mask()
        s = self._retouch_mask_scale()
        mh, mw = self._retouch_mask.shape

        r = max(1, int(self._retouch_size_var.get() * s))
        yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
        dist2  = xx.astype(np.float32) ** 2 + yy.astype(np.float32) ** 2
        brush  = np.exp(-dist2 / (2 * (r / 2.0) ** 2)).astype(np.float32)

        sign = 1.0 if self._retouch_mode == "dodge" else -1.0
        cx, cy = int(round(ix * s)), int(round(iy * s))
        x0, y0 = cx - r, cy - r
        x1, y1 = cx + r + 1, cy + r + 1

        bx0, by0 = max(0, x0), max(0, y0)
        bx1, by1 = min(mw, x1), min(mh, y1)
        if bx1 <= bx0 or by1 <= by0:
            return

        bx_off, by_off = bx0 - x0, by0 - y0
        bw, bh = bx1 - bx0, by1 - by0
        delta = brush[by_off:by_off + bh, bx_off:bx_off + bw] * sign * self._RETOUCH_STRENGTH
        self._retouch_mask[by0:by1, bx0:bx1] = np.clip(
            self._retouch_mask[by0:by1, bx0:bx1] + delta, -1.0, 1.0)

    # If the image has more pixels than this, downsample it before numpy ops.
    # At fit mode a 6048×4048 image displays at ~1.7 M px — well under this limit,
    # so no roundtrip and no softening.  At 100 % zoom it's 24 M px — over the
    # limit, so we downsample, which is fast and the slight softness is acceptable
    # for a dodge/burn effect.
    _RETOUCH_NUMPY_MAX_PX = 2_500_000

    def _apply_retouch(self, img: Image.Image, high_quality: bool = False) -> Image.Image:
        """Apply the accumulated dodge/burn mask to img."""
        mh, mw = self._retouch_mask.shape
        iw, ih = img.size

        if high_quality or iw * ih <= self._RETOUCH_NUMPY_MAX_PX:
            # Upscale mask to image size — no image roundtrip, no softening
            if (iw, ih) != (mw, mh):
                mask = np.array(
                    Image.fromarray(self._retouch_mask, mode="F")
                    .resize((iw, ih), Image.BILINEAR))
            else:
                mask = self._retouch_mask
            arr = np.array(img, dtype=np.float32)
            upscale_to = None
        else:
            # Image is too large for live preview — downsample to mask size, apply, upscale result
            arr = np.array(img.resize((mw, mh), Image.BILINEAR), dtype=np.float32)
            mask = self._retouch_mask
            upscale_to = (iw, ih)

        mask = mask[:, :, np.newaxis]
        pos  = np.maximum(mask,  0)
        neg  = np.maximum(-mask, 0)
        dodge_weight = 1.0 - arr / 255.0
        burn_weight  = arr / 255.0
        arr  = arr + (255.0 - arr) * pos * dodge_weight - arr * neg * burn_weight
        result = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        if upscale_to:
            result = result.resize(upscale_to, Image.BILINEAR)
        return result

    @staticmethod
    def _apply_vignette(img: Image.Image, strength: float) -> Image.Image:
        w, h = img.size
        # Build mask at ¼ resolution for speed, then upscale
        sw, sh = max(4, w // 4), max(4, h // 4)
        data = []
        for y in range(sh):
            for x in range(sw):
                dx = (x / (sw - 1) - 0.5) * 2   # -1 … 1
                dy = (y / (sh - 1) - 0.5) * 2
                dist = min(1.0, math.sqrt(dx * dx + dy * dy) / math.sqrt(2))
                data.append(max(0, min(255, int(255 * (1 - strength * dist ** 1.5)))))
        mask = Image.new("L", (sw, sh))
        mask.putdata(data)
        mask = mask.resize((w, h), Image.LANCZOS)
        return Image.composite(img, Image.new("RGB", (w, h), 0), mask)

    # ──────────────────────────────────────────────────────────── crop ──

    def _start_crop(self, lock: bool):
        self._crop_active     = True
        self._crop_lock_ratio = lock
        self._crop_drag_mode  = "new"
        self.btn_crop_orig.config(bg=BTN_ACTIVE_BG if lock     else BTN_BG,
                                   fg=BTN_ACTIVE_FG if lock     else BTN_FG)
        self.btn_crop_free.config(bg=BTN_ACTIVE_BG if not lock else BTN_BG,
                                   fg=BTN_ACTIVE_FG if not lock else BTN_FG)
        self._crop_action_frame.pack(fill=tk.X, after=self.btn_crop_free)
        # Hide save/reset and disable sliders while crop is active
        self._edit_bottom_sep.pack_forget()
        self._btn_save_copy.pack_forget()
        self._btn_reset.pack_forget()
        for w in self._edit_controls:
            w.config(state=tk.DISABLED)
        self.canvas.config(cursor="crosshair")

    def _canvas_to_img(self, wx, wy):
        """Canvas widget coords → original image pixel coords."""
        cx = self.canvas.canvasx(wx)
        cy = self.canvas.canvasy(wy)
        return ((cx - self._rs_img_x) / self._rs_scale,
                (cy - self._rs_img_y) / self._rs_scale)

    def _img_to_canvas(self, ix, iy):
        """Original image pixel coords → canvas coords."""
        return (ix * self._rs_scale + self._rs_img_x,
                iy * self._rs_scale + self._rs_img_y)

    def _crop_hit_test(self, wx, wy):
        """Return which part of the crop rect the mouse is over (canvas widget coords)."""
        if self._crop_rect_id is None:
            return "new"
        cx = self.canvas.canvasx(wx)
        cy = self.canvas.canvasy(wy)
        x0, y0 = self._img_to_canvas(self._crop_ix0, self._crop_iy0)
        x1, y1 = self._img_to_canvas(self._crop_ix1, self._crop_iy1)
        C, E = 14, 9  # corner and edge grab radius in pixels
        # Corners first
        if abs(cx - x0) < C and abs(cy - y0) < C: return "corner_TL"
        if abs(cx - x1) < C and abs(cy - y0) < C: return "corner_TR"
        if abs(cx - x0) < C and abs(cy - y1) < C: return "corner_BL"
        if abs(cx - x1) < C and abs(cy - y1) < C: return "corner_BR"
        # Edges
        in_x = x0 - E < cx < x1 + E
        in_y = y0 - E < cy < y1 + E
        if abs(cy - y0) < E and in_x: return "edge_T"
        if abs(cy - y1) < E and in_x: return "edge_B"
        if abs(cx - x0) < E and in_y: return "edge_L"
        if abs(cx - x1) < E and in_y: return "edge_R"
        # Interior
        if x0 < cx < x1 and y0 < cy < y1: return "move"
        return "new"

    def _on_canvas_motion(self, event):
        if self._retouch_active:
            self._update_retouch_cursor(event.x, event.y)
        if not self._crop_active:
            return
        if self._crop_rect_id is None:
            self.canvas.config(cursor="crosshair")
            return
        cursors = {
            "new": "crosshair", "move": "fleur",
            "corner_TL": "size_nw_se", "corner_BR": "size_nw_se",
            "corner_TR": "size_ne_sw", "corner_BL": "size_ne_sw",
            "edge_T": "size_ns",       "edge_B": "size_ns",
            "edge_L": "size_ew",       "edge_R": "size_ew",
        }
        self.canvas.config(cursor=cursors.get(self._crop_hit_test(event.x, event.y), "crosshair"))

    def _crop_press(self, event):
        mode = self._crop_hit_test(event.x, event.y)
        self._crop_drag_mode = mode
        ix, iy = self._canvas_to_img(event.x, event.y)
        self._crop_drag_six = ix
        self._crop_drag_siy = iy
        self._crop_rect_start = (self._crop_ix0, self._crop_iy0,
                                  self._crop_ix1, self._crop_iy1)
        if mode == "new":
            # Start a fresh rectangle
            self._crop_ix0 = self._crop_ix1 = ix
            self._crop_iy0 = self._crop_iy1 = iy
            if self._crop_rect_id:
                self.canvas.delete(self._crop_rect_id)
                self._crop_rect_id = None

    def _crop_drag(self, event):
        if self.pil_image is None:
            return
        iw, ih = self.pil_image.size
        ix, iy = self._canvas_to_img(event.x, event.y)
        ix = max(0.0, min(float(iw), ix))
        iy = max(0.0, min(float(ih), iy))
        mode = self._crop_drag_mode
        sx0, sy0, sx1, sy1 = self._crop_rect_start
        dix = ix - self._crop_drag_six
        diy = iy - self._crop_drag_siy

        if mode == "new":
            if self._crop_lock_ratio:
                ratio = iw / max(1, ih)
                dx, dy = ix - self._crop_ix0, iy - self._crop_iy0
                sgx = 1 if dx >= 0 else -1
                sgy = 1 if dy >= 0 else -1
                adx, ady = abs(dx), abs(dy)
                if adx / max(1e-9, ady) > ratio:
                    ady = adx / ratio
                else:
                    adx = ady * ratio
                ix = self._crop_ix0 + sgx * adx
                iy = self._crop_iy0 + sgy * ady
            self._crop_ix1 = ix
            self._crop_iy1 = iy
        elif mode == "move":
            w, h = sx1 - sx0, sy1 - sy0
            self._crop_ix0 = max(0.0, min(float(iw) - w, sx0 + dix))
            self._crop_iy0 = max(0.0, min(float(ih) - h, sy0 + diy))
            self._crop_ix1 = self._crop_ix0 + w
            self._crop_iy1 = self._crop_iy0 + h
        elif mode == "corner_TL":
            if self._crop_lock_ratio and self.pil_image:
                ratio = iw / max(1, ih)
                new_x0 = max(0.0, min(sx1 - 4, sx0 + dix))
                new_y0 = max(0.0, min(sy1 - 4, sy0 + diy))
                new_w, new_h = sx1 - new_x0, sy1 - new_y0
                if new_w / max(1e-9, new_h) > ratio:
                    new_w = new_h * ratio; new_x0 = sx1 - new_w
                else:
                    new_h = new_w / ratio; new_y0 = sy1 - new_h
                self._crop_ix0 = max(0.0, new_x0)
                self._crop_iy0 = max(0.0, new_y0)
            else:
                self._crop_ix0 = max(0.0, min(sx1 - 4, sx0 + dix))
                self._crop_iy0 = max(0.0, min(sy1 - 4, sy0 + diy))
        elif mode == "corner_TR":
            if self._crop_lock_ratio and self.pil_image:
                ratio = iw / max(1, ih)
                new_x1 = min(float(iw), max(sx0 + 4, sx1 + dix))
                new_y0 = max(0.0, min(sy1 - 4, sy0 + diy))
                new_w, new_h = new_x1 - sx0, sy1 - new_y0
                if new_w / max(1e-9, new_h) > ratio:
                    new_w = new_h * ratio; new_x1 = sx0 + new_w
                else:
                    new_h = new_w / ratio; new_y0 = sy1 - new_h
                self._crop_ix1 = min(float(iw), new_x1)
                self._crop_iy0 = max(0.0, new_y0)
            else:
                self._crop_ix1 = min(float(iw), max(sx0 + 4, sx1 + dix))
                self._crop_iy0 = max(0.0, min(sy1 - 4, sy0 + diy))
        elif mode == "corner_BL":
            if self._crop_lock_ratio and self.pil_image:
                ratio = iw / max(1, ih)
                new_x0 = max(0.0, min(sx1 - 4, sx0 + dix))
                new_y1 = min(float(ih), max(sy0 + 4, sy1 + diy))
                new_w, new_h = sx1 - new_x0, new_y1 - sy0
                if new_w / max(1e-9, new_h) > ratio:
                    new_w = new_h * ratio; new_x0 = sx1 - new_w
                else:
                    new_h = new_w / ratio; new_y1 = sy0 + new_h
                self._crop_ix0 = max(0.0, new_x0)
                self._crop_iy1 = min(float(ih), new_y1)
            else:
                self._crop_ix0 = max(0.0, min(sx1 - 4, sx0 + dix))
                self._crop_iy1 = min(float(ih), max(sy0 + 4, sy1 + diy))
        elif mode == "corner_BR":
            if self._crop_lock_ratio and self.pil_image:
                ratio = iw / max(1, ih)
                new_x1 = min(float(iw), max(sx0 + 4, sx1 + dix))
                new_y1 = min(float(ih), max(sy0 + 4, sy1 + diy))
                new_w, new_h = new_x1 - sx0, new_y1 - sy0
                if new_w / max(1e-9, new_h) > ratio:
                    new_w = new_h * ratio; new_x1 = sx0 + new_w
                else:
                    new_h = new_w / ratio; new_y1 = sy0 + new_h
                self._crop_ix1 = min(float(iw), new_x1)
                self._crop_iy1 = min(float(ih), new_y1)
            else:
                self._crop_ix1 = min(float(iw), max(sx0 + 4, sx1 + dix))
                self._crop_iy1 = min(float(ih), max(sy0 + 4, sy1 + diy))
        elif mode == "edge_T":
            self._crop_iy0 = max(0.0, min(sy1 - 4, sy0 + diy))
        elif mode == "edge_B":
            self._crop_iy1 = min(float(ih), max(sy0 + 4, sy1 + diy))
        elif mode == "edge_L":
            self._crop_ix0 = max(0.0, min(sx1 - 4, sx0 + dix))
        elif mode == "edge_R":
            self._crop_ix1 = min(float(iw), max(sx0 + 4, sx1 + dix))

        # Draw the rectangle using normalized coords
        x0 = min(self._crop_ix0, self._crop_ix1)
        y0 = min(self._crop_iy0, self._crop_iy1)
        x1 = max(self._crop_ix0, self._crop_ix1)
        y1 = max(self._crop_iy0, self._crop_iy1)
        cx0, cy0 = self._img_to_canvas(x0, y0)
        cx1, cy1 = self._img_to_canvas(x1, y1)
        if self._crop_rect_id:
            self.canvas.coords(self._crop_rect_id, cx0, cy0, cx1, cy1)
        else:
            self._crop_rect_id = self.canvas.create_rectangle(
                cx0, cy0, cx1, cy1, outline="#ffffff", width=2, dash=(6, 4))

    def _apply_crop(self):
        if self.pil_image is None or self._crop_rect_id is None:
            self._cancel_crop(); return
        iw, ih = self.pil_image.size
        x0 = int(max(0,  min(self._crop_ix0, self._crop_ix1)))
        y0 = int(max(0,  min(self._crop_iy0, self._crop_iy1)))
        x1 = int(min(iw, max(self._crop_ix0, self._crop_ix1)))
        y1 = int(min(ih, max(self._crop_iy0, self._crop_iy1)))
        if x1 - x0 < 4 or y1 - y0 < 4:
            self._cancel_crop(); return

        cropped = self.pil_image.crop((x0, y0, x1, y1))
        self.pil_image = cropped
        self._cache[self.current_file] = cropped
        self._set_retouch_mask(None)  # image dimensions changed

        w, h = cropped.size
        name = os.path.basename(self.current_file)
        n, total = self.current_index + 1, len(self.folder_files)
        self.info_var.set(f"{name}   {w} × {h}   {n} / {total}")
        self._cancel_crop()
        self._render()

    def _cancel_crop(self):
        self._crop_active = False
        if self._crop_rect_id:
            self.canvas.delete(self._crop_rect_id)
            self._crop_rect_id = None
        self._crop_action_frame.pack_forget()
        self._edit_bottom_sep.pack(fill=tk.X, padx=14, pady=(16, 8))
        self._btn_save_copy.pack(fill=tk.X, padx=14, pady=(0, 6))
        self._btn_reset.pack(fill=tk.X, padx=14, pady=(0, 16))
        for w in self._edit_controls:
            w.config(state=tk.NORMAL)
        self.canvas.config(cursor="")
        self.btn_crop_orig.config(bg=BTN_BG, fg=BTN_FG)
        self.btn_crop_free.config(bg=BTN_BG, fg=BTN_FG)

    # ────────────────────────────────────────────────────── EXIF info ──

    def _show_exif(self):
        if not self.current_file:
            return
        try:
            img = Image.open(self.current_file)
            raw = img.getexif()
        except Exception as exc:
            messagebox.showerror("EXIF error", str(exc)); return

        TAGS = {
            271:   "Camera make",    272:   "Camera model",
            33434: "Shutter speed",  33437: "Aperture",
            34855: "ISO",            37386: "Focal length",
            41989: "Focal length (35mm)", 42036: "Lens model",
            42035: "Lens make",      36867: "Date taken",
            37378: "Aperture (APEX)", 41986: "Exposure program",
            41987: "White balance",  41988: "Digital zoom",
            37380: "Exposure bias",  41985: "Custom rendered",
        }

        def fmt_val(tid, val):
            if tid == 33434:
                try:
                    n, d = (val.numerator, val.denominator) \
                           if hasattr(val, "numerator") else val
                    if d == 0: return str(val)
                    s = n / d
                    return f"{s:.1f}s" if s >= 1 else f"1/{round(d/n)}s"
                except Exception: return str(val)
            if tid == 33437:
                try:   return f"f/{float(val):.1f}"
                except: return str(val)
            if tid in (37386, 41989):
                try:   return f"{float(val):.0f} mm"
                except: return str(val)
            if tid == 37380:
                try:   return f"{float(val):+.1f} EV"
                except: return str(val)
            return str(val)

        lines = [(lbl, fmt_val(tid, raw[tid]))
                 for tid, lbl in TAGS.items() if tid in raw]
        try:
            from PIL.ExifTags import IFD
            sub = raw.get_ifd(IFD.Exif)
            for tid, lbl in TAGS.items():
                if tid not in raw and tid in sub:
                    lines.append((lbl, fmt_val(tid, sub[tid])))
        except Exception:
            pass

        win = tk.Toplevel(self.root)
        win.title("Photo Info")
        win.configure(bg=DARK_BG)
        win.resizable(False, False)

        if not lines:
            tk.Label(win, text="No EXIF data found.", bg=DARK_BG, fg=LABEL_FG,
                     font=("Segoe UI", 12), padx=20, pady=20).pack()
        else:
            fr = tk.Frame(win, bg=DARK_BG, padx=20, pady=16)
            fr.pack()
            for i, (label, value) in enumerate(lines):
                bg = "#252525" if i % 2 == 0 else DARK_BG
                row = tk.Frame(fr, bg=bg)
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


# ──────────────────────────────────────────────────────────── entry ──

def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()

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
