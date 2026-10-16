# -*- coding: utf-8 -*-
"""FizzDisk GUI (CustomTkinter, frosted-glass light/dark theme).

A self-contained disc ripper: scan a disc / ISO / folder / file, select titles
and tracks, and make MKV files. Uses the open-source ffmpeg engine; for
AACS-protected Blu-ray discs it falls back to the bundled open-source
libbluray/libaacs decoder, and only uses the installed optional backend as a
last resort.
"""
from __future__ import annotations

import os
import re
import io
import threading
import time
import traceback
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path
from typing import List, Optional

try:
    import customtkinter as ctk
    import pywinstyles
except ImportError:  # very old tkinter-only fallback
    ctk = None
    pywinstyles = None

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:  # pillow optional: poster preview just won't show
    HAS_PIL = False

from . import engine, ffutil, bdlib, movie, prefs
from . import license as lic
from .version import APP_NAME as _APP_NAME, __version__ as _APP_VERSION
from .model import Title

# subtle per-window theming for the few native tk widgets kept (log text box)
_DARK = {"text_bg": "#0d1013", "text_fg": "#dfe3ea", "err": "#ff6b6b"}
_LIGHT = {"text_bg": "#ffffff", "text_fg": "#20242a", "err": "#b00020"}

# brand palette (mirrors make_icon.py): amber accent = the icons' orange,
# blue = the primary accent used for progress/busy states.
_BRAND_ORANGE = "#f59e0b"
_BRAND_ORANGE_HOVER = "#d98e09"
_BRAND_BLUE = "#3b82f6"
_BRAND_BLUE_HOVER = "#2563eb"

# ---------------------------------------------------------------------------
# App icon (hard-wired). Every window -- the root and ALL CTkToplevel dialogs
# -- gets the FizzDisk brand icon, never the Tk feather default. We apply both
# iconbitmap (.ico, for title bar / taskbar) and iconphoto(default=True, .png),
# which sets the app-wide default for every (incl. future) toplevel, so even a
# dialog that never sets it explicitly cannot fall back to the Tk icon.
# ---------------------------------------------------------------------------
_APP_ICON_PHOTO = None
_APP_ICON_PATHS = None


def _find_icon_paths():
    """Return (ico_path, png_path) for the brand icon, or (None, None)."""
    global _APP_ICON_PATHS
    if _APP_ICON_PATHS is not None:
        return _APP_ICON_PATHS
    import sys
    bases = []
    try:
        bases += [Path(__file__).resolve().parent.parent,
                  Path(__file__).resolve().parent.parent.parent]
    except Exception:
        pass
    try:
        exe_dir = Path(sys.executable).resolve().parent
        bases += [exe_dir, exe_dir / "_internal"]
    except Exception:
        pass
    try:
        mp = getattr(sys, "_MEIPASS", None)
        if mp:
            bases.append(Path(mp))
    except Exception:
        pass
    try:
        bdir = bdlib._bin_dir()
        if bdir:
            bases.append(Path(bdir).parent)
    except Exception:
        pass
    ico = png = None
    for b in bases:
        if ico is None:
            for cand in (b / "assets" / "fizzdisk.ico", b / "fizzdisk.ico"):
                if cand.exists():
                    ico = str(cand)
                    break
        if png is None:
            for cand in (b / "assets" / "fizzdisk_icon.png",
                         b / "assets" / "fizzdisk_icon_512.png",
                         b / "fizzdisk_icon.png"):
                if cand.exists():
                    png = str(cand)
                    break
    _APP_ICON_PATHS = (ico, png)
    return _APP_ICON_PATHS


def set_window_icon(win) -> None:
    """Apply the FizzDisk brand icon to a window (root or a Toplevel dialog)."""
    ico, png = _find_icon_paths()
    try:
        if ico:
            win.iconbitmap(ico)
    except Exception:
        pass
    try:
        if png and HAS_PIL:
            global _APP_ICON_PHOTO
            if _APP_ICON_PHOTO is None:
                img = Image.open(png)
                img = img.resize((64, 64), Image.LANCZOS)
                _APP_ICON_PHOTO = ImageTk.PhotoImage(img)
            # default=True makes this the app-wide default icon for every window.
            win.iconphoto(True, _APP_ICON_PHOTO)
    except Exception:
        pass


# Hard-wire: every CTkToplevel created anywhere in the app gets our icon, so a
# dialog can never silently show the Tk default icon.
if ctk is not None and hasattr(ctk, "CTkToplevel"):
    _orig_ctk_toplevel = ctk.CTkToplevel

    def _iconed_toplevel(*a, **k):
        w = _orig_ctk_toplevel(*a, **k)
        try:
            set_window_icon(w)
        except Exception:
            pass
        return w

    ctk.CTkToplevel = _iconed_toplevel


class FizzDiskApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("FizzDisk")
        self.geometry("980x620")
        self.minsize(860, 500)

        self.source = ""
        self.source_kind = ""
        self.titles: List[Title] = []
        self.selected = set()          # title indices checked for ripping
        self.audio_sel: set = set()    # selected audio ordinals (for detail title)
        self.sub_sel: set = set()
        self.detail_title: Optional[Title] = None
        self.output_dir = str(Path.home() / "Videos")
        self._audio_vars: dict = {}
        self._stopping = threading.Event()   # set by Stop button / window close
        # movie metadata lookup (title/year/poster) from the disc
        self.movie_info: Optional[dict] = None
        self.movie_candidates: List[dict] = []
        self._poster_bytes: Optional[bytes] = None
        self._poster_photo = None              # keep a ref to the PhotoImage
        self._film_folder: str = ""
        self._dark = False
        # special-features disc detection (no title matches the movie runtime)
        self.special_features = False
        self._sf_prompted = False
        # audio-CD state
        self.cd_mode = False
        self.cd_meta: Optional[dict] = None
        self._cd_artist: str = ""
        self._cd_album: str = ""
        self._cd_release_mbid: str = ""
        self._cd_release_group_mbid: str = ""
        self._cd_fmtrow = None

        if ctk is not None:
            ctk.set_appearance_mode("Dark")     # dark is the default
            ctk.set_default_color_theme("blue")

        self._build_ui()
        self._set_cd_mode(False)
        self.refresh_drives()
        self._apply_theme()
        self._set_window_icon()
        self._prompted_tmdb = False
        self.after(250, self._maybe_prompt_tmdb_key)
        # Best-effort: keep the AACS key database fresh so newer discs stay
        # decryptable (silent, backgrounded, never blocks the UI).
        try:
            threading.Thread(target=lambda: bdlib.update_keydb(),
                             daemon=True).start()
        except Exception:
            pass
        self.log("FizzDisk ready. Choose a source and click Scan.")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # ---- Source bar ----
        top = ctk.CTkFrame(self, corner_radius=12)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(top, text="Source:").pack(side="left", padx=(10, 4))
        self.var_source = tk.StringVar()
        self.combo_source = ctk.CTkComboBox(top, values=[], width=300, variable=self.var_source)
        self.combo_source.pack(side="left", padx=4)
        self.combo_source.bind("<<ComboboxSelected>>", lambda e: self._source_from_combo())
        ctk.CTkButton(top, text="Browse\u2026", width=90, corner_radius=10, command=self.browse).pack(side="left", padx=(0, 6))
        ctk.CTkButton(top, text="Refresh Drives", width=110, corner_radius=10, command=self.refresh_drives).pack(side="left", padx=(0, 6))
        self.btn_scan = ctk.CTkButton(top, text="Scan", width=80, corner_radius=10, command=self.on_scan,
                                      fg_color="#f59e0b", hover_color="#d98e09", text_color="#ffffff")
        self.btn_scan.pack(side="left", padx=(0, 6))
        # theme toggle on the right
        self.btn_theme = ctk.CTkButton(top, text="\U0001F319", width=42, corner_radius=10,
                                       fg_color="transparent", border_width=1,
                                       command=self._toggle_theme)
        self.btn_theme.pack(side="right", padx=8)
        # gear (settings) sits to the LEFT of the light/dark toggle; opens
        # the trial/licensing + TMDb Settings popup
        self.btn_gear = ctk.CTkButton(top, text="\u2699", width=42, corner_radius=10,
                                      fg_color="transparent", border_width=1,
                                      command=self._open_settings)
        self.btn_gear.pack(side="right", padx=(0, 8))

        # ---- Main split ----
        body = ctk.CTkFrame(self, corner_radius=12)
        body.pack(fill="both", expand=False, padx=10, pady=4)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        # Left: titles
        left = ctk.CTkFrame(body, corner_radius=10)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=6)
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)
        self._titles_label = ctk.CTkLabel(left, text="Titles (click a row to select & view)",
                                          anchor="w", font=("Segoe UI", 9, "bold"))
        self._titles_label.grid(row=0, column=0, sticky="ew", padx=8, pady=(3, 1))
        self.tree = ctk.CTkScrollableFrame(left, corner_radius=8, fg_color="transparent", height=150)
        self.tree._scrollbar.configure(height=60)
        self.tree.grid(row=1, column=0, sticky="nsew", padx=4, pady=2)
        self._row_frames: list = []
        self._row_labels: list = []
        row = ctk.CTkFrame(left, corner_radius=8)
        row.grid(row=2, column=0, sticky="ew", padx=6, pady=(4, 8))
        ctk.CTkButton(row, text="Select All", width=110, corner_radius=10, command=lambda: self._set_all(True)).pack(side="left", padx=6, pady=4)
        ctk.CTkButton(row, text="Select None", width=110, corner_radius=10, command=lambda: self._set_all(False)).pack(side="left", padx=6, pady=4)

        # Right: detail + output
        right = ctk.CTkFrame(body, corner_radius=10)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=6)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        # stream detail (scrollable so many tracks never force the window tall)
        detail = ctk.CTkFrame(right, corner_radius=10)
        detail.grid(row=0, column=0, sticky="nsew", padx=6, pady=(6, 4))
        detail.grid_rowconfigure(1, weight=1)
        detail.grid_columnconfigure(0, weight=1)
        self._streams_label = ctk.CTkLabel(detail, text="Streams (selected title)", anchor="w", font=("Segoe UI", 9, "bold"))
        self._streams_label.grid(row=0, column=0, sticky="ew", padx=8, pady=(3, 1))
        self.stream_frame = ctk.CTkScrollableFrame(detail, corner_radius=8, fg_color="transparent", height=150)
        self.stream_frame._scrollbar.configure(height=60)
        self.stream_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))

        # output + actions
        out_box = ctk.CTkFrame(right, corner_radius=10)
        out_box.grid(row=1, column=0, sticky="ew", padx=6, pady=(4, 6))
        ctk.CTkLabel(out_box, text="Output", anchor="w", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(3, 1))
        out_box.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(out_box, text="Folder:", anchor="w").grid(row=1, column=0, sticky="w", padx=(8, 2), pady=1)
        self.var_out = tk.StringVar(value=self.output_dir)
        ctk.CTkEntry(out_box, textvariable=self.var_out, corner_radius=8).grid(row=1, column=1, sticky="ew", padx=2, pady=1)
        ctk.CTkButton(out_box, text="\u2026", width=38, corner_radius=8, command=self.browse_out).grid(row=1, column=2, padx=(4, 8), pady=1)
        self._file_label = ctk.CTkLabel(out_box, text="File name:", anchor="w")
        self._file_label.grid(row=2, column=0, sticky="w", padx=(8, 2), pady=1)
        self.var_tmpl = tk.StringVar(value="{name}.mkv")
        self._file_entry = ctk.CTkEntry(out_box, textvariable=self.var_tmpl, corner_radius=8)
        self._file_entry.grid(row=2, column=1, sticky="ew", padx=2, pady=1)

        # CD output compression toggles (independent -- may pick both) -- CD only
        self._cd_fmtrow = ctk.CTkFrame(out_box, fg_color="transparent")
        self._cd_fmtrow.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 1))
        self.var_cd_flac = tk.BooleanVar(value=True)   # FLAC on by default
        self.var_cd_mp3 = tk.BooleanVar(value=False)
        self.cb_flac = ctk.CTkCheckBox(self._cd_fmtrow, text="FLAC Compression",
                                       corner_radius=8, checkbox_width=13,
                                       checkbox_height=13, width=17,
                                       variable=self.var_cd_flac,
                                       command=self._on_cd_formats)
        self.cb_flac.pack(side="top", anchor="w")
        self.cb_mp3 = ctk.CTkCheckBox(self._cd_fmtrow, text="MP3 Compression",
                                      corner_radius=8, checkbox_width=13,
                                      checkbox_height=13, width=17,
                                      variable=self.var_cd_mp3,
                                      command=self._on_cd_formats)
        self.cb_mp3.pack(side="top", anchor="w")

        self.var_backup = tk.BooleanVar(value=False)
        self.cb_backup = ctk.CTkCheckBox(out_box, text="Raw Backup Only (Copy AACS/BDMV)",
                                         corner_radius=8,
                                         checkbox_width=13, checkbox_height=13, width=17,
                                         variable=self.var_backup)
        self.cb_backup.grid(row=4, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 0))
        self.var_encode = tk.BooleanVar(value=False)   # checked = re-encode; default = lossless stream copy
        self.cb_encode = ctk.CTkCheckBox(out_box, text="Compress MKV (H.264/x264/AAC)",
                                         corner_radius=8,
                                         checkbox_width=13, checkbox_height=13, width=17,
                                         variable=self.var_encode)
        self.cb_encode.grid(row=5, column=0, columnspan=3, sticky="w", padx=8, pady=(1, 2))

        btnrow = ctk.CTkFrame(out_box, corner_radius=8, fg_color="transparent")
        btnrow.grid(row=6, column=0, columnspan=3, sticky="ew", padx=8, pady=2)
        btnrow.grid_columnconfigure((0, 1), weight=1)
        self.btn_make = ctk.CTkButton(btnrow, text="Start", corner_radius=10, command=self.on_make, state="disabled")
        # capture the default palette so we can restore it after the orange flash
        self._start_fg = self.btn_make.cget("fg_color")
        self._start_hover = self.btn_make.cget("hover_color")
        self._start_text = self.btn_make.cget("text_color")
        self.btn_make.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.btn_stop = ctk.CTkButton(btnrow, text="Stop", corner_radius=10, command=self.on_stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=(3, 0))
        # capture the default palette so we can restore it after the orange flash
        self._stop_fg = self.btn_stop.cget("fg_color")
        self._stop_hover = self.btn_stop.cget("hover_color")
        self._stop_text = self.btn_stop.cget("text_color")
        # ---- progress + status (single row: bar left, status right) ----
        bar_row = ctk.CTkFrame(self, fg_color="transparent")
        bar_row.pack(fill="x", padx=10, pady=(2, 0))
        bar_row.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(bar_row, height=14, corner_radius=6,
                                           progress_color="#3b82f6")
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.progress.set(0.0)
        self.var_eta = tk.StringVar(value="Ready")
        ctk.CTkLabel(bar_row, textvariable=self.var_eta, anchor="e",
                     font=("Segoe UI", 10)).grid(row=0, column=1, sticky="e")

        # ---- Log ----
        logf = ctk.CTkFrame(self, corner_radius=10)
        logf.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        ctk.CTkLabel(logf, text="Log", anchor="w", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(3, 0))
        self.text = tk.Text(logf, height=7, width=100, state="disabled", wrap="word",
                            relief="flat", highlightthickness=0)
        self.text.pack(fill="both", expand=True, padx=6, pady=4)
        self.text.tag_config("err", foreground="#b00020")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------- themes
    def _apply_theme(self):
        """Recolor any native tk widgets when the appearance switches."""
        if ctk is None:
            return
        dark = ctk.get_appearance_mode() == "Dark"
        self._dark = dark
        pal = _DARK if dark else _LIGHT
        try:
            self.text.configure(bg=pal["text_bg"], fg=pal["text_fg"],
                                insertbackground=pal["text_fg"])
            self.text.tag_config("err", foreground=pal["err"])
        except Exception:
            pass
        try:
            if pywinstyles is not None:
                pywinstyles.apply_style(self, "mica")
        except Exception:
            pass
        icon_fg = "#dfe3ea" if dark else "#23272e"
        theme_fg = "#f59e0b"        # yellow/orange for the sun & moon toggle
        if hasattr(self, "btn_theme"):
            self.btn_theme.configure(text="\u2600\ufe0f" if dark else "\U0001F319",
                                     text_color=theme_fg)
        if hasattr(self, "btn_gear"):
            self.btn_gear.configure(text_color=icon_fg)

    def _toggle_theme(self):
        if ctk is None:
            return
        ctk.set_appearance_mode("Light" if ctk.get_appearance_mode() == "Dark" else "Dark")
        self._apply_theme()

    def _set_window_icon(self):
        """Set the title-bar icon to the FizzDisk brand mark (hard-wired)."""
        set_window_icon(self)

    # ------------------------------------------------------------- drives
    def refresh_drives(self):
        drives = engine.list_optical_drives()
        vals = list(drives)
        for letter in "CDEFGHIJK":
            root = f"{letter}:\\"
            if os.path.isdir(root):
                if (Path(root) / "BDMV").is_dir() or (Path(root) / "VIDEO_TS").is_dir():
                    if root not in vals:
                        vals.append(root)
        try:
            self.combo_source.configure(values=vals if vals else [])
        except Exception:
            pass
        if vals and not self.var_source.get():
            self.var_source.set(vals[0])
            self._source_from_combo()

    def _source_from_combo(self):
        self.source = self.var_source.get()
        self.source_kind = engine.detect_source(self.source)

    def browse(self):
        path = filedialog.askopenfilename(
            title="Open source",
            filetypes=[("All supported", "*.iso *.m2ts *.vob *.mkv *.mp4 *.ts *.m4v *.mov *.avi"),
                       ("ISO image", "*.iso"), ("All files", "*.*")])
        if path:
            self.var_source.set(path)
            self._source_from_combo()

    def browse_out(self):
        d = filedialog.askdirectory(title="Output folder", initialdir=self.var_out.get() or self.output_dir)
        if d:
            self.var_out.set(d)

    # ------------------------------------------------------------- scanning
    def on_scan(self):
        src = self.var_source.get().strip()
        if not src:
            messagebox.showinfo("FizzDisk", "Choose a source first.")
            return
        self.source = src
        self.titles = []
        self.selected.clear()
        self._clear_tree()
        self.progress.set(0.0)
        self.var_eta.set("Scanning\u2026")
        self._set_status("Scanning\u2026")
        self._scan_running()
        threading.Thread(target=self._scan_worker, args=(src,), daemon=True).start()

    def _scan_worker(self, src):
        try:
            res = engine.scan_source(src)
        except Exception as e:  # noqa: BLE001
            self.after(0, lambda e=e: self._scan_done_error(str(e)))
            return
        self.after(0, lambda: self._scan_done(res.titles, res.info,
                                              res.source_kind, res.meta))

    def _scan_done(self, titles, info, source_kind="", meta=None):
        self.titles = titles
        self.special_features = False
        self._sf_prompted = False
        if titles:
            self._scan_pending()
        else:
            self._scan_idle()
        self._set_status(f"Scan complete \u2014 {len(titles)} title(s)")
        self.log(info)
        self.cd_mode = (source_kind == "cd")
        self.cd_meta = meta or None
        self._set_cd_mode(self.cd_mode)
        if not self.cd_mode:
            # a video/other disc: drop any stale CD artwork/title and movie
            # candidates from a previous scan so identification starts fresh
            self._cd_artist = ""
            self._cd_album = ""
            self._cd_release_mbid = ""
            self._cd_release_group_mbid = ""
            self.movie_candidates = []
            self.movie_info = None
        if self.cd_mode:
            # a music CD: default-select every track
            self.selected = {t.index for t in titles}
            self._cd_artist = (meta or {}).get("artist") or ""
            self._cd_album = (meta or {}).get("album") or ""
            self._cd_release_mbid = (meta or {}).get("release_mbid") or ""
            self._cd_release_group_mbid = (meta or {}).get("release_group_mbid") or ""
        elif titles:
            main = next((t for t in titles if t.is_main), None)
            if main is None:
                main = max(titles, key=lambda t: t.duration or 0)
            self.selected.add(main.index)
        if titles:
            self._start_ready()
        else:
            self._start_busy()
        self._populate_tree()
        # Bring the auto-selected (main feature) title to the top of the list.
        try:
            if titles and not self.cd_mode:
                sel = next((t for t in titles if t.is_main), None) or max(titles, key=lambda t: t.duration or 0)
                pos = self.titles.index(sel)
                self.after(30, lambda p=pos: self._scroll_title_to_top(p))
        except Exception:
            pass
        self.var_eta.set("Ready \u2014 select titles, then click Start")
        main = next((t for t in titles if t.is_main and t.duration > 0), titles[0] if titles else None)
        if main and not self.cd_mode:
            try:
                self._show_detail(main)
            except Exception:
                traceback.print_exc()
        if self.cd_mode:
            try:
                self._confirm_cd_album()
            except Exception:
                traceback.print_exc()
        else:
            try:
                self._lookup_movie_async()
            except Exception:
                traceback.print_exc()

    def _scan_done_error(self, msg):
        self._scan_idle()
        self._start_busy()
        self._set_status("Scan failed")
        self.var_eta.set("Scan failed")
        self.log("ERROR: " + msg, err=True)
        messagebox.showerror("FizzDisk", "Could not scan source.\n" + msg)

    # --------------------------------------------------------------- CD mode
    def _set_cd_mode(self, on):
        self.cd_mode = bool(on)
        try:
            if self._cd_fmtrow is not None:
                if on:
                    self._cd_fmtrow.grid()
                else:
                    self._cd_fmtrow.grid_remove()
            # the File-name template is irrelevant for CD tracks (auto-named)
            if getattr(self, "_file_entry", None) is not None:
                if on:
                    self._file_label.grid_remove()
                    self._file_entry.grid_remove()
                else:
                    self._file_label.grid()
                    self._file_entry.grid()
            # video-only options don't apply to a CD
            if getattr(self, "cb_backup", None) is not None:
                if on:
                    self.cb_backup.grid_remove()
                    self.cb_encode.grid_remove()
                else:
                    self.cb_backup.grid()
                    self.cb_encode.grid()
            # retitle the panes for a CD
            if getattr(self, "_titles_label", None) is not None:
                self._titles_label.configure(
                    text="Tracks (click a row to select & view)" if on
                    else "Titles (click a row to select & view)")
            if getattr(self, "_streams_label", None) is not None:
                self._streams_label.configure(text="Album info" if on
                                              else "Streams (selected title)")
        except Exception:
            pass

    def _on_cd_formats(self):
        try:
            fmts = self._selected_cd_formats()
            label = " + ".join(f.upper() for f in fmts) or "FLAC"
            self.var_eta.set(f"Ripping to {label} \u2014 select tracks, then Start")
        except Exception:
            pass

    def _selected_cd_formats(self) -> list:
        """Return the chosen compression format(s); at least ['flac']."""
        fmts = []
        if self.var_cd_flac.get():
            fmts.append("flac")
        if self.var_cd_mp3.get():
            fmts.append("mp3")
        return fmts or ["flac"]

    def _confirm_cd_album(self):
        """Confirm (or edit) the Artist/Album identified for the CD, via a popup."""
        meta = self.cd_meta or {}
        artist = (meta.get("artist") or "Unknown Artist").strip()
        album = (meta.get("album") or "Unknown Album").strip()
        track_count = len(meta.get("tracks") or []) or len(self.titles)
        dlg = ctk.CTkToplevel(self)
        dlg.title("Identify CD")
        dlg.transient(self)
        dlg.grab_set()
        frame = ctk.CTkFrame(dlg, corner_radius=14)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(frame, text="Is this the CD?", font=("Segoe UI", 10, "bold"),
                     anchor="w").pack(anchor="w", padx=8, pady=(10, 2))
        ctk.CTkLabel(frame, text=f"{artist} \u2014 {album}", text_color="#3b82f6",
                     font=("Segoe UI", 12)).pack(anchor="w", padx=8)
        ctk.CTkLabel(frame, text=f"{track_count} tracks (will be saved as "
                                 f"{artist} / {album} / files)",
                     text_color="gray", font=("Segoe UI", 10)).pack(anchor="w", padx=8, pady=(2, 8))
        res = {"v": None}

        def use():
            self._cd_artist, self._cd_album = artist, album
            res["v"] = (artist, album)
            dlg.destroy()

        def change():
            res["v"] = "change"
            dlg.destroy()

        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.pack(anchor="e", padx=8, pady=12)
        ctk.CTkButton(btns, text="Use this album", corner_radius=10,
                      command=use).pack(side="left")
        ctk.CTkButton(btns, text="Change it \u2026", corner_radius=10,
                      fg_color="transparent", border_width=1, command=change).pack(side="left", padx=8)
        dlg.bind("<Return>", lambda e: use())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self.wait_window(dlg)
        if res["v"] == "change":
            self._edit_cd_album()
        self._prefill_cd_folder()
        self._show_cd_album()
        if res["v"]:
            self.log(f"CD identified: {self._cd_artist} \u2014 {self._cd_album}")

    def _prefill_cd_folder(self):
        """Pre-fill the output Folder to ~/Music/<Artist>/<Album>."""
        try:
            base = Path.home() / "Music"
            folder = os.path.join(str(base),
                                  ffutil.safe_name(self._cd_artist or "Unknown Artist"),
                                  ffutil.safe_name(self._cd_album or "Unknown Album"))
            self.var_out.set(folder)
        except Exception:
            pass

    def _edit_cd_album(self):
        dlg = ctk.CTkToplevel(self)
        dlg.title("Artist & album")
        dlg.transient(self)
        dlg.grab_set()
        frame = ctk.CTkFrame(dlg, corner_radius=14)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(frame, text="Enter the artist and album:", anchor="w",
                     font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2,
                                                         sticky="w", padx=8, pady=(6, 8))
        ctk.CTkLabel(frame, text="Artist:").grid(row=1, column=0, sticky="e", pady=2)
        ar = ctk.CTkEntry(frame, width=320, corner_radius=8)
        ar.insert(0, self._cd_artist or "")
        ar.grid(row=1, column=1, sticky="w", padx=6, pady=2)
        ctk.CTkLabel(frame, text="Album:").grid(row=2, column=0, sticky="e", pady=2)
        al = ctk.CTkEntry(frame, width=320, corner_radius=8)
        al.insert(0, self._cd_album or "")
        al.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        res = {"v": None}

        def ok():
            self._cd_artist = ar.get().strip() or "Unknown Artist"
            self._cd_album = al.get().strip() or "Unknown Album"
            res["v"] = (self._cd_artist, self._cd_album)
            dlg.destroy()

        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.grid(row=3, column=1, sticky="e", padx=6, pady=10)
        ctk.CTkButton(btns, text="OK", corner_radius=10, command=ok).pack(side="left")
        ctk.CTkButton(btns, text="Cancel", corner_radius=10,
                      fg_color="transparent", border_width=1, command=dlg.destroy).pack(side="left", padx=8)
        dlg.bind("<Return>", lambda e: ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self.wait_window(dlg)
        if res["v"]:
            self.log(f"CD saved as: {self._cd_artist} \u2014 {self._cd_album}")

    # ------------------------------------------------- movie metadata lookup
    @staticmethod
    def _volume_label(drive_letter: str) -> str:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        try:
            ctypes.windll.kernel32.GetVolumeInformationW(
                ("%s:\\" % drive_letter), buf, 261, None, None, None, fs, 261)
            return (buf.value or "").strip()
        except Exception:
            return ""

    def _source_term(self) -> str:
        src = (self.source or "").strip()
        m = re.match(r"^([A-Za-z]):", src)
        if m:
            label = self._volume_label(m.group(1).upper())
            # The on-disc identity (BDMV/META or the raw UDF/ISO volume
            # descriptor) is the authoritative movie title and must win over the
            # generic Windows volume label, which is often a studio token like
            # 'BLURAY'/'LOGICAL_VOLUME_ID' that searches fail on even though the
            # real title is present on the media.
            name = movie.disc_metadata_name(src)
            if movie.name_is_useful(name):
                return name
            if movie.name_is_useful(label):
                return label
            return name or label or src
        base = os.path.basename(src.rstrip("\\/"))
        name = movie.disc_metadata_name(src)
        if movie.name_is_useful(name):
            return name
        if movie.name_is_useful(base):
            return base
        return name or base or src

    def _fallback_name(self, term: str) -> str:
        t = movie.clean_term(term) if term else ""
        if t and len(t) >= 3:
            return t
        if self.titles:
            return _safe_name(self.titles[0].name)
        return "Movie"

    def _lookup_movie_async(self):
        term = self._source_term()
        self.log("Looking up movie: " + (term or "(unknown)"))

        def work():
            try:
                cands = movie.search_movies(term)
                err = None
            except Exception as e:  # noqa: BLE001
                cands, err = [], str(e)
            self.after(0, lambda: self._movie_done(cands, term, err))

        threading.Thread(target=work, daemon=True).start()

    def _movie_done(self, cands, term, err):
        self.movie_candidates = cands or []
        if self.movie_candidates:
            self.log(f"Movie lookup: {len(self.movie_candidates)} possible match(es)")
        else:
            self.log("Movie not found" + ((": " + err) if err else ""))
        self._ask_movie()

    def _ask_movie(self):
        cands = list(getattr(self, "movie_candidates", None) or [])
        name = None
        self.movie_info = cands[0] if cands else None
        if cands:
            if len(cands) == 1:
                guess = cands[0]
                self.movie_info = guess
                if guess.get("poster"):
                    self._load_poster(movie.highres_poster(guess.get("title"), guess.get("year"), guess.get("poster")))
                try:
                    ok = self._confirm_movie(guess["name"])
                except Exception:
                    traceback.print_exc()
                    ok = True
                if ok:
                    name = guess["name"]
                else:
                    self.movie_info = guess
            else:
                picked = self._pick_movie(cands)
                if isinstance(picked, dict):
                    self.movie_info = picked
                    if picked.get("poster"):
                        self._load_poster(movie.highres_poster(picked.get("title"), picked.get("year"), picked.get("poster")))
                    name = picked["name"]
                else:
                    self.movie_info = cands[0]
        if name is None:
            initial = (self.movie_info["name"] if self.movie_info
                       else self._fallback_name(self._source_term()))
            try:
                name = self._get_title_year(initial)
            except Exception:
                traceback.print_exc()
                name = None
            if not name:
                name = self.movie_info["name"] if self.movie_info else self._fallback_name(self._source_term())

        name = _safe_name(name) or "movie"
        self.var_tmpl.set(name + ".mkv")
        self._film_folder = name
        self.log("Movie title set: " + name)
        try:
            self._rationalize_selection()
        except Exception:
            traceback.print_exc()

    def _rationalize_selection(self):
        """Correct the auto-selected title now that the movie runtime is known.

        The scan only picks the disc's ``is_main`` flag (title #1 on a DVD) or
        the longest title, so an obscure menu/marker title can end up selected.
        Once the movie is identified we know the real runtime, so:
          * if some title is feature-length (matches the runtime), auto-select the
            closest match if the current pick is wrong; leaves a deliberate choice
            alone;
          * if no title is feature-length, this is a special-features/extra disc
            \u2014 ask whether to rip all files or let the user choose, naming each
            output 'Movie - Special Features N.mkv'."""
        if self.source_kind not in ("dvd", "bd"):
            return
        if self.cd_mode or not self._film_folder or self._sf_prompted:
            return
        rt = self._known_runtime()
        if not rt:
            return
        target = float(rt) * 60.0
        tol = max(180.0, target * 0.06)  # 6% or ±3 min
        near = [t for t in self.titles
                if (t.duration or 0) > 0 and abs(float(t.duration) - target) <= tol]
        if near:
            # The movie IS on this disc. Only re-select if the current pick is a
            # bad/short title that doesn't match the runtime (don't hijack a
            # deliberate choice that already matches).
            sel_ok = any(t.index in self.selected and
                         abs(float(t.duration or 0) - target) <= tol
                         for t in self.titles)
            if not sel_ok:
                best = min(near, key=lambda t: abs(float(t.duration) - target))
                self.selected = {best.index}
                self.log(f"Re-selected '{_safe_name(best.name)}' "
                         f"({int((best.duration or 0) / 60)} min) \u2014 closest to the "
                         f"known runtime ({rt} min).")
                try:
                    self._populate_tree()
                    self._show_detail(best)
                except Exception:
                    traceback.print_exc()
            return
        # No feature-length title \u2014 likely a special-features disc.
        self._sf_prompted = True
        self._ask_sf_confirm(target)

    def _ask_sf_confirm(self, target):
        res = {"v": None}
        dlg = ctk.CTkToplevel(self)
        dlg.title("Special features disc?")
        dlg.transient(self)
        dlg.grab_set()
        dlg.attributes("-topmost", True)
        frame = ctk.CTkFrame(dlg, corner_radius=14)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        ctk.CTkLabel(
            frame,
            text=f"No title matches the movie runtime ({int(target / 60)} min). "
                 "This looks like a special-features (extras) disc.\n\n"
                 "Is this a special features disc? If so, would you like me to rip "
                 "all the files, or would you like to choose them?",
            font=("Segoe UI", 10), wraplength=460, justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 8))

        def pick(v):
            res["v"] = v
            dlg.destroy()

        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(btns, text="Rip all files", fg_color="#f59e0b",
                      command=lambda: pick("all")).pack(side="left", expand=True, fill="x", padx=(0, 5))
        ctk.CTkButton(btns, text="Let me choose", command=lambda: pick("choose")).pack(
            side="left", expand=True, fill="x", padx=5)
        ctk.CTkButton(btns, text="Eject disc", command=lambda: pick("eject")).pack(
            side="left", expand=True, fill="x", padx=(5, 0))
        try:
            dlg.wait_window()
        except Exception:
            res["v"] = None

        ch = res["v"]
        if ch == "all":
            self.special_features = True
            self.selected = {t.index for t in self.titles if (t.duration or 0) > 0}
            self.log(f"Special-features disc: ripping all {len(self.selected)} title(s) as "
                     f"'{self._film_folder} - Special Features N.mkv'.")
            self._populate_tree()
        elif ch == "choose":
            self.special_features = True
            self.log("Special-features disc: pick the titles to rip.")
        elif ch == "eject":
            # Pop the disc out so the user can look at it before deciding.
            try:
                letter = engine._optical_drive_letter(self.source)
                if letter and engine.eject_drive(letter):
                    self.log(f"Ejected drive {letter} \u2014 pop the disc out to have a look, "
                             "then close the tray and rescan.")
                else:
                    self.log("Ejecting the disc\u2026")
            except Exception:
                traceback.print_exc()
        else:
            self.log("Left title selection as-is.")

    def _pick_movie(self, cands) -> Optional[dict]:
        dlg = ctk.CTkToplevel(self)
        dlg.title("Which movie?")
        dlg.transient(self)
        dlg.grab_set()
        frame = ctk.CTkFrame(dlg, corner_radius=14)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        ctk.CTkLabel(frame, text="Found several matches. Pick the right one:",
                     font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 6))
        sel = tk.StringVar(value=cands[0]["name"])
        sc = ctk.CTkScrollableFrame(frame, width=500, height=min(400, 96 * len(cands)), corner_radius=8)
        sc.pack(fill="both", expand=True, padx=10)

        # Keep a strong reference to every thumbnail PhotoImage (else they get GC'd).
        self._pick_photos: list = []
        try:
            _scal = ctk.ScalingTracker.get_widget_scaling(self)
        except Exception:  # noqa: BLE001
            _scal = 1.0
        _tw, _th = max(1, int(60 * _scal)), max(1, int(86 * _scal))

        # Exclusive single-choice. Sharing one StringVar across CTkCheckBox
        # does NOT clear siblings on its own (the variable callback only fires
        # when the value equals a checkbox's own onvalue/offvalue), which made
        # the wrong candidate stay checked until a second click. Instead we
        # drive each checkbox's state directly so clicking one clears the rest.
        checkboxes: list = []

        def _select(name):
            for n, cb in checkboxes:
                cb.set(n == name)
            sel.set(name)

        for c in cands:
            row = ctk.CTkFrame(sc, corner_radius=8, fg_color="transparent")
            row.pack(fill="x", anchor="w", padx=4, pady=3)
            row.bind("<Button-1>", lambda e, n=c["name"]: _select(n))
            thumb = ctk.CTkLabel(row, text="\u2026", width=60, height=86,
                                 corner_radius=6, fg_color="#2f343b",
                                 text_color="#8b949e", font=("Segoe UI", 9))
            thumb.pack(side="left", padx=(2, 8), pady=2)
            thumb.bind("<Button-1>", lambda e, n=c["name"]: _select(n))
            cb = ctk.CTkCheckBox(row, text=c["name"],
                                 corner_radius=8, checkbox_width=13,
                                 checkbox_height=13, width=16,
                                 command=lambda n=c["name"]: _select(n))
            cb.pack(side="left", fill="x", expand=True, padx=(0, 4), pady=2)
            checkboxes.append((c["name"], cb))
            self._start_thumb_load(c, thumb, _tw, _th, 60, 86)

        # Establish the initial selection (first candidate is the pre-picked one).
        _select(cands[0]["name"])

        res = {"v": None}

        def use():
            res["v"] = next((c for c in cands if c["name"] == sel.get()), cands[0])
            dlg.destroy()

        def manual():
            res["v"] = "manual"
            dlg.destroy()
        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(btns, text="Use this one", corner_radius=10, command=use).pack(side="left")
        ctk.CTkButton(btns, text="Let me type it in", corner_radius=10,
                      fg_color="transparent", border_width=1, command=manual).pack(side="left", padx=8)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self.wait_window(dlg)
        return res["v"]

    def _chooser_poster_url(self, c: dict) -> Optional[str]:
        url = (c or {}).get("poster")
        if not url:
            return None
        # Wikipedia thumbs are already small; a TMDb 'original' URL is huge, so
        # downscale it to a lightweight chooser-sized image.
        m = re.match(r"(https?://image\.tmdb\.org/t/p/)[^/]+(/.*)$", url)
        if m:
            return f"{m.group(1)}w185{m.group(2)}"
        return url

    def _start_thumb_load(self, c: dict, label, px: int, py: int, w: int, h: int):
        """Download a small poster thumbnail for a chooser row and show it.

        The image is wrapped in a CTkImage so it scales correctly on HighDPI
        displays; the worker thread only does the network fetch + downscale, and
        the UI update is marshalled to the main loop via self.after.
        """
        if not HAS_PIL:
            return
        url = self._chooser_poster_url(c)
        if not url:
            self._set_thumb(label, None)
            return

        def work():
            img = None
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "FizzDisk/1.0"})
                data = urllib.request.urlopen(req, timeout=15).read()
                pil = Image.open(io.BytesIO(data))
                pil.thumbnail((px, py), Image.LANCZOS)
                if pil.mode not in ("RGB", "RGBA"):
                    pil = pil.convert("RGB")
                img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(w, h))
            except Exception:  # noqa: BLE001
                img = None
            self.after(0, lambda im=img, lb=label: self._set_thumb(lb, im))

        threading.Thread(target=work, daemon=True).start()

    def _set_thumb(self, label, img):
        try:
            if img is not None:
                label.configure(image=img, text="")
                self._pick_photos.append(img)   # keep a strong reference
            else:
                label.configure(text="\u2013")
        except Exception:  # noqa: BLE001
            pass

    def _load_poster(self, url: str):
        def work():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "FizzDisk/1.0"})
                data = urllib.request.urlopen(req, timeout=25).read()
                if HAS_PIL:
                    Image.open(io.BytesIO(data)).load()
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda e=e: self.log("poster: " + str(e)))
                return
            self.after(0, lambda: self._poster_ready(data))

        threading.Thread(target=work, daemon=True).start()

    def _poster_ready(self, data: bytes):
        self._poster_bytes = data
        if not HAS_PIL:
            return
        try:
            img = Image.open(io.BytesIO(data))
            img.thumbnail((160, 240))
            self._poster_photo = ImageTk.PhotoImage(img)
        except Exception as e:  # noqa: BLE001
            self.log("poster preview: " + str(e))

    def _maybe_prompt_tmdb_key(self):
        # First-run auto-prompt (only until a user key is set, once per session).
        # The gear button can always open Settings directly instead.
        if self._prompted_tmdb:
            return
        self._prompted_tmdb = True
        if movie.has_user_key():
            return
        self._open_settings()

    def _open_settings(self):
        """Settings popup: trial/licensing + TheMovieDB API key, in two cards."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Settings")
        dlg.transient(self)
        dlg.geometry("560x560")
        dlg.resizable(True, True)
        body = ctk.CTkFrame(dlg, corner_radius=14)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        sc = ctk.CTkScrollableFrame(body, corner_radius=12)
        sc.pack(fill="both", expand=True, padx=4, pady=4)

        # ---- Card 1: trial / product key ----
        card1 = ctk.CTkFrame(sc, corner_radius=12, fg_color=("#f0f2f6", "#272a30"))
        card1.pack(fill="x", anchor="w", padx=4, pady=(2, 10))
        ctk.CTkLabel(card1, text="Trial & Registration", font=("Segoe UI", 12, "bold"),
                     anchor="w").pack(anchor="w", padx=12, pady=(10, 2))
        lic_status = tk.StringVar(value=lic.status_text())
        ctk.CTkLabel(card1, textvariable=lic_status, wraplength=480, justify="left",
                     anchor="w").pack(anchor="w", padx=12, pady=(0, 4))
        prow = ctk.CTkFrame(card1, fg_color="transparent")
        prow.pack(anchor="w", padx=12, pady=(4, 2))
        ctk.CTkLabel(prow, text="Product key:", anchor="w").pack(side="left", padx=(0, 6))
        pk = tk.StringVar(value=lic.format_key(lic._load().get("license_key", "")))
        ctk.CTkEntry(prow, width=260, textvariable=pk,
                     placeholder_text="FZFK-XXXX-XXXX-XXXX-XXXX").pack(side="left")
        pk_status = tk.StringVar(value="")

        def do_activate():
            ok, msg = lic.activate(pk.get())
            lic_status.set(lic.status_text())
            pk_status.set(msg)
            self.log("License: " + msg)

        ctk.CTkButton(prow, text="Activate", width=90, corner_radius=10,
                      command=do_activate).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(card1, textvariable=pk_status, text_color="#3b82f6",
                     anchor="w").pack(anchor="w", padx=12, pady=(2, 10))

        def do_deactivate():
            ok, msg = lic.deactivate()
            lic_status.set(lic.status_text())
            pk_status.set(msg)
            self.log("License: " + msg)

        ctk.CTkButton(card1, text="Move to another machine? Click here", fg_color="transparent",
                      border_width=1, corner_radius=10, command=do_deactivate,
                      width=260).pack(anchor="w", padx=12, pady=(0, 10))


        # ---- Card 2: TheMovieDB API key ----
        card2 = ctk.CTkFrame(sc, corner_radius=12, fg_color=("#f0f2f6", "#272a30"))
        card2.pack(fill="x", anchor="w", padx=4, pady=(2, 10))
        ctk.CTkLabel(card2, text="TheMovieDB API key (recommended)", font=("Segoe UI", 12, "bold"),
                     anchor="w").pack(anchor="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(card2, text=(
            "FizzDisk uses TheMovieDB to identify the movie title & year when a\n"
            "disc is scanned, to download a sharp HD poster, and to check a disc's\n"
            "length against the real runtime so it can back up an obfuscated disc\n"
            "(e.g. Lionsgate) in full. It needs a free API key (~1 minute) \u2014 do\n"
            "grab one: we don't share a master key, so each user having their own\n"
            "avoids usage limits. Without a key FizzDisk still works (Wikipedia\n"
            "titles/years and a best-effort runtime), but HD posters are\n"
            "unavailable and the length check is less reliable."),
            justify="left", anchor="w", wraplength=470).pack(anchor="w", padx=12, pady=(0, 4))

        def open_key():
            webbrowser.open("https://www.themoviedb.org/settings/api")

        ctk.CTkButton(card2, text="Get a free API key \u2192", command=open_key,
                      width=180).pack(anchor="w", padx=12, pady=(0, 6))
        ctk.CTkLabel(card2, text="Paste your API key:", anchor="w").pack(anchor="w", padx=12)
        key = tk.StringVar(value=movie.tmdb_key())
        ctk.CTkEntry(card2, width=380, textvariable=key,
                     placeholder_text="e.g. 1a2b3c...").pack(anchor="w", padx=12, pady=(2, 8))
        status = tk.StringVar(value="")

        def save():
            k = key.get().strip()
            if not k:
                self.log("No API key \u2014 using Wikipedia (titles/years/runtime).")
                status.set("Using Wikipedia (no key) \u2014 titles/years/runtime still work.")
            else:
                p = movie.set_tmdb_key(k)
                if p:
                    status.set("Saved \u2014 TMDb search & HD posters enabled.")
                    self.log("TMDb key saved \u2014 will identify movies & get HD posters.")
                else:
                    status.set("Couldn't write the key file.")
                    self.log("TMDb key: write failed.")
            self.after(800, dlg.destroy)

        row = ctk.CTkFrame(card2, fg_color="transparent")
        row.pack(anchor="w", padx=12)
        ctk.CTkButton(row, text="Save", width=90, corner_radius=10, command=save).pack(side="left", padx=(0, 6))
        ctk.CTkButton(row, text="Skip (use Wikipedia)", fg_color="transparent", border_width=1,
                      command=dlg.destroy).pack(side="left")
        ctk.CTkLabel(card2, textvariable=status, text_color="#3b82f6", anchor="w").pack(
            anchor="w", padx=12, pady=(6, 10))


        # ---- Card 3: Ripping behaviour ----
        card3 = ctk.CTkFrame(sc, corner_radius=12, fg_color=("#f0f2f6", "#272a30"))
        card3.pack(fill="x", anchor="w", padx=4, pady=(2, 10))
        ctk.CTkLabel(card3, text="Ripping", font=("Segoe UI", 12, "bold"),
                     anchor="w").pack(anchor="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(card3, text=(
            "When a rip finishes, FizzDisk automatically opens the disc tray so\n"
            "you can swap the disc. Turn this off if you'd rather leave the tray\n"
            "closed."),
            justify="left", anchor="w", wraplength=470).pack(anchor="w", padx=12, pady=(0, 4))
        eject_cb = ctk.CTkCheckBox(card3, text="Auto-eject disc after rip",
                                   corner_radius=8, checkbox_width=15, checkbox_height=15,
                                   width=17)
        if prefs.get("auto_eject_after_rip", True):
            eject_cb.select()
        else:
            eject_cb.deselect()

        def on_eject_toggle():
            prefs.set_("auto_eject_after_rip", bool(eject_cb.get()))

        eject_cb.configure(command=on_eject_toggle)
        eject_cb.pack(anchor="w", padx=12, pady=(2, 10))

        # app version footer
        ctk.CTkLabel(sc, text=f"{_APP_NAME} v{_APP_VERSION}",
                     text_color="gray", anchor="w").pack(anchor="w", padx=8, pady=(2, 4))

    # ------------------------------------------------------------ movie popups
    def _confirm_movie(self, name: str) -> bool:
        dlg = ctk.CTkToplevel(self)
        dlg.title("Confirm movie")
        dlg.transient(self)
        dlg.grab_set()
        row = ctk.CTkFrame(dlg, corner_radius=14)
        row.pack(fill="both", expand=True, padx=10, pady=10)
        col = ctk.CTkFrame(row, fg_color="transparent")
        col.pack(side="left", fill="both", expand=True)
        ctk.CTkLabel(col, text="Is this the correct movie?",
                     font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 0))
        ctk.CTkLabel(col, text=name, text_color="#3b82f6",
                     font=("Segoe UI", 12)).pack(anchor="w", pady=(4, 0))
        ctk.CTkLabel(col, text="If not, you'll be asked to enter the title and year.",
                     text_color="gray", font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 8))
        res = {"v": False}
        btns = ctk.CTkFrame(col, fg_color="transparent")
        btns.pack(anchor="e", pady=(4, 12))
        ctk.CTkButton(btns, text="Yes, correct", corner_radius=10,
                      command=lambda: (res.__setitem__("v", True), dlg.destroy())).pack(side="left")
        ctk.CTkButton(btns, text="No, it's wrong", corner_radius=10,
                      fg_color="transparent", border_width=1, command=dlg.destroy).pack(side="left", padx=8)
        dlg.bind("<Return>", lambda e: (res.__setitem__("v", True), dlg.destroy()))
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        # Measure the text column's height so the poster scales to the popup height
        dlg.update_idletasks()
        col_h = col.winfo_reqheight()
        measured = [col_h]

        def add_poster():
            # Insert a small poster on the left, scaled to the popup height.
            # This widens the popup (does not raise it). The poster appears once
            # the async download finishes, so the dialog stays the same height.
            if not (self._poster_bytes and HAS_PIL):
                return
            try:
                img = Image.open(io.BytesIO(self._poster_bytes))
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                h = max(80, measured[0] - 20)          # poster height = content height minus padding
                w = max(1, int(h * img.width / img.height))
                img = img.resize((w, h), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                lbl = tk.Label(row, image=photo, borderwidth=0)
                lbl.image = photo                       # keep a reference
                col.pack_forget()
                lbl.pack(side="left", padx=(12, 10), pady=10)
                col.pack(side="left", fill="both", expand=True)
            except Exception as e:  # noqa: BLE001
                self.log("poster preview: " + str(e))

        if self._poster_bytes and HAS_PIL:
            add_poster()
        elif HAS_PIL:
            deadline = [time.time() + 6]                # wait up to 6s for the download
            def poll():
                if self._poster_bytes:
                    add_poster()
                elif time.time() < deadline[0]:
                    self.after(120, poll)
            self.after(120, poll)

        self.wait_window(dlg)
        return bool(res["v"])

    def _get_title_year(self, initial: str = "") -> Optional[str]:
        dlg = ctk.CTkToplevel(self)
        dlg.title("Movie title & year")
        dlg.transient(self)
        dlg.grab_set()
        mt = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", initial)
        t0 = mt.group(1).strip() if mt else initial
        frame = ctk.CTkFrame(dlg, corner_radius=14)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        ctk.CTkLabel(frame, text="Sorry we couldn't match your disk.\n"
                                "Enter the Title and see if we can find it.",
                     font=("Segoe UI", 10, "bold"), justify="left"
                     ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(4, 8))
        ctk.CTkLabel(frame, text="Title:").grid(row=1, column=0, sticky="e", pady=2)
        t = ctk.CTkEntry(frame, width=300, corner_radius=8)
        t.grid(row=1, column=1, sticky="w", padx=6, pady=2)
        t.insert(0, t0)
        ctk.CTkLabel(frame, text="Year:").grid(row=2, column=0, sticky="e", pady=2)
        y = ctk.CTkEntry(frame, width=80, corner_radius=8)
        y.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        ctk.CTkLabel(frame, text="(Optional)", font=("Segoe UI", 9), text_color="gray"
                     ).grid(row=2, column=2, sticky="w", padx=(2, 0))
        status = ctk.CTkLabel(frame, text="", font=("Segoe UI", 9), text_color="#3b82f6", anchor="w")
        status.grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 0))
        res = {"v": None}

        state = {"open": True}
        cancel = threading.Event()

        def search_again():
            if not state["open"]:
                return
            q = t.get().strip()
            status.configure(text="Searching…", text_color="#3b82f6")

            def work():
                try:
                    cands = movie.search_movies(q)
                except Exception:  # noqa: BLE001
                    cands = []

                def show():
                    if not state["open"]:
                        return
                    if cands:
                        # Reopen the "Which movie?" chooser with the new results so
                        # the user can pick the right one; choosing applies it to the
                        # main app (File Name field) and closes this dialog.
                        picked = self._pick_movie(cands)
                        if isinstance(picked, dict):
                            res["v"] = picked.get("name") or (
                                "%s (%s)" % (picked.get("title"), picked.get("year"))
                                if picked.get("title") else None)
                            self.movie_info = picked     # so the poster can resolve later
                            cancel.set()
                            state["open"] = False
                            dlg.destroy()
                        else:
                            status.configure(text="Type the title and year, then Save (year is optional).",
                                             text_color="#f59e0b")
                    else:
                        status.configure(text="No match found — enter the title and Save (year is optional).",
                                         text_color="#f59e0b")
                self.after(0, show)
            threading.Thread(target=work, daemon=True).start()

        def ok():
            val = t.get().strip() or (self.movie_info["title"] if self.movie_info else "")
            yr = y.get().strip()
            m2 = re.search(r"\((\d{4})\)\s*$", val)          # avoid "(2020) (2020)"
            if m2:
                if not yr:
                    yr = m2.group(1)
                val = val[:m2.start()].strip()
            if val:
                res["v"] = "%s (%s)" % (val, yr) if yr else val
            cancel.set()
            state["open"] = False
            dlg.destroy()

        def close():
            cancel.set()
            state["open"] = False
            dlg.destroy()

        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.grid(row=4, column=0, columnspan=3, pady=(12, 4))     # centered across the form
        ctk.CTkButton(btns, text="Search Again", corner_radius=10, command=search_again,
                      fg_color="transparent", border_width=1).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Save", corner_radius=10, command=ok).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cancel", corner_radius=10,
                      fg_color="transparent", border_width=1, command=close).pack(side="left", padx=4)
        dlg.bind("<Return>", lambda e: ok())
        dlg.bind("<Escape>", lambda e: close())
        t.focus_set()
        t.select_range(0, "end")
        self.wait_window(dlg)
        return res["v"]

    def _clear_tree(self):
        for f in getattr(self, "_row_frames", []):
            try:
                f.destroy()
            except Exception:
                pass
        self._row_frames = []
        self._row_labels = []
        self._clear_streams()

    def _set_status(self, s):
        self.var_eta.set(s)

    # ---------------------------------------------------------------- list
    def _fmt_size(self, n):
        if not n:
            return ""
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    def _row_text(self, t, src=""):
        mark = "\u2611" if t.index in self.selected else "\u2610"
        if self.cd_mode:
            num = t.info.get("cd_number")
            title = t.info.get("cd_title") or t.name
            num = f"#{int(num):02d}" if num else "#--"
            return f"{mark}  {num}    {title}    {t.nice_duration}"
        base = f"{mark}  {t.name}    {t.nice_duration}    {self._fmt_size(t.size_bytes)}"
        return f"{base}    {src}" if src else base

    def _populate_tree(self):
        self._clear_tree()
        for t in self.titles:
            row = ctk.CTkFrame(self.tree, corner_radius=8)
            row.pack(fill="x", padx=3, pady=1)
            src = ""
            if t.clips and t.clips[0].files:
                src = Path(t.clips[0].files[0]).name
            label = ctk.CTkLabel(row, anchor="w", text=self._row_text(t, src))
            label.pack(side="left", fill="x", expand=True, padx=6, pady=2)

            def _click(e, idx=t.index, lbl=label, s=src):
                # Exclusive single-select for video: a plain click makes this
                # the ONLY selected title (deselecting the auto-picked one).
                # Ctrl/Shift click toggles additively so the user can still
                # select several extras; 'Select All'/'Select None' remain too.
                # Music CDs stay on plain toggling (users select multiple tracks).
                if self.cd_mode or (e.state & (0x4 | 0x1)):   # CD, or Ctrl/Shift
                    self._toggle_rip(idx)
                else:
                    self.selected = {idx}
                self._refresh_row_marks()
                self._show_detail(self.titles[idx])
            label.bind("<Button-1>", _click)
            row.bind("<Button-1>", _click)
            self._row_frames.append(row)
            self._row_labels.append((label, t, src))

    def _refresh_row_marks(self):
        """Re-render the checkbox glyph on every row after a selection change."""
        for label, t, src in getattr(self, "_row_labels", []):
            try:
                label.configure(text=self._row_text(t, src))
            except Exception:
                pass

    def _toggle_rip(self, idx):
        if idx in self.selected:
            self.selected.discard(idx)
        else:
            self.selected.add(idx)

    def _set_all(self, on):
        self.selected = {t.index for t in self.titles} if on else set()
        self._populate_tree()

    def _scroll_title_to_top(self, pos):
        """Scroll the titles list so the row at list-position *pos* is at the top."""
        try:
            if 0 <= pos < len(self._row_frames):
                row = self._row_frames[pos]
                canvas = getattr(self.tree, "_parent_canvas", None)
                if canvas is None:
                    return
                y = row.winfo_y()
                total = canvas.winfo_reqheight()
                if total > 0:
                    canvas.yview_moveto(y / total)
        except Exception:
            pass

    # ------------------------------------------------------------- details
    def _clear_streams(self):
        for w in list(self.stream_frame.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass

    def _show_detail(self, title):
        self.detail_title = title
        self.audio_sel = set(range(len(title.audio)))
        self.sub_sel = set(range(len(title.subtitles)))
        self._clear_streams()
        r = 0
        if title.audio:
            ctk.CTkLabel(self.stream_frame, text="Audio:", font=("Segoe UI", 10, "bold"), anchor="w").grid(row=r, column=0, sticky="nw", padx=4, pady=2)
            r += 1
            for i, a in enumerate(title.audio):
                v = tk.BooleanVar(value=True)
                cb = ctk.CTkCheckBox(self.stream_frame, text=a.label, corner_radius=6,
                                     checkbox_width=13, checkbox_height=13, width=16,
                                     variable=v, command=lambda i=i, v=v: self._toggle("a", i, v.get()))
                cb.grid(row=r, column=0, columnspan=2, sticky="w", padx=10)
                self._audio_vars[i] = v
                r += 1
        if title.subtitles:
            ctk.CTkLabel(self.stream_frame, text="Subtitles:", font=("Segoe UI", 10, "bold"), anchor="w").grid(row=r, column=0, sticky="nw", padx=4, pady=2)
            r += 1
            for i, s in enumerate(title.subtitles):
                v = tk.BooleanVar(value=True)
                cb = ctk.CTkCheckBox(self.stream_frame, text=s.label, corner_radius=6,
                                     checkbox_width=13, checkbox_height=13, width=16,
                                     variable=v, command=lambda i=i, v=v: self._toggle("s", i, v.get()))
                cb.grid(row=r, column=0, columnspan=2, sticky="w", padx=10)
                r += 1
        if not title.audio and not title.subtitles:
            ctk.CTkLabel(self.stream_frame, text="(no audio or subtitle tracks)",
                         text_color="gray").grid(row=r, column=0, sticky="w", padx=4)

    def _toggle(self, kind, i, val):
        if kind == "a":
            if val:
                self.audio_sel.add(i)
            else:
                self.audio_sel.discard(i)
        else:
            if val:
                self.sub_sel.add(i)
            else:
                self.sub_sel.discard(i)

    def _show_cd_album(self):
        """Streams pane becomes album info + a small cover-art thumbnail."""
        self._clear_streams()
        meta = self.cd_meta or {}
        r = 0
        ctk.CTkLabel(self.stream_frame, text="Album", font=("Segoe UI", 10, "bold"),
                     anchor="w").grid(row=r, column=0, sticky="nw", padx=4, pady=(2, 2))
        r += 1
        rows = [
            ("Artist", self._cd_artist or "Unknown Artist"),
            ("Title", self._cd_album or "Unknown Album"),
            ("Year", meta.get("year") or ""),
            ("Tracks", str(len(self.titles))),
        ]
        for k, v in rows:
            if v:
                ctk.CTkLabel(self.stream_frame, text=f"{k}: {v}", anchor="w").grid(
                    row=r, column=0, sticky="w", padx=10)
                r += 1
        try:
            from . import musicdisc as _md
            out_dir = self.var_out.get().strip() or str(Path.home() / "Music")
            os.makedirs(out_dir, exist_ok=True)
            cov = os.path.join(out_dir, "cover.jpg")
            if self._cd_release_mbid and not os.path.exists(cov):
                try:
                    _md.download_cover(
                        self._cd_release_mbid, cov, size="500",
                        artist=self._cd_artist or "Unknown Artist",
                        album=self._cd_album or "Unknown Album",
                        release_group_mbid=self._cd_release_group_mbid or None)
                except Exception:
                    pass
            if HAS_PIL and os.path.exists(cov):
                try:
                    img = Image.open(cov)
                    img.thumbnail((180, 180), Image.LANCZOS)
                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGB")
                    cimg = ctk.CTkImage(light_image=img, dark_image=img,
                                        size=img.size)
                    lbl = ctk.CTkLabel(self.stream_frame, image=cimg, text="")
                    lbl.image = cimg
                    # place cover art to the RIGHT of the text (column 1)
                    lbl.grid(row=0, column=1, rowspan=max(1, r),
                             sticky="ne", padx=(14, 8), pady=6)
                except Exception:
                    pass
            elif not os.path.exists(cov):
                ctk.CTkLabel(self.stream_frame, text="(cover art unavailable)",
                             text_color="gray").grid(row=0, column=1,
                                                     sticky="ne", padx=14)
        except Exception:
            pass

    # -------------------------------------------------------------- ripping
    def on_make(self):
        if not self.var_out.get().strip():
            messagebox.showinfo("FizzDisk", "Choose an output folder.")
            return
        os.makedirs(self.var_out.get().strip(), exist_ok=True)
        # 50-backup trial: block ripping once the limit is reached (no license)
        if not lic.can_rip():
            self.log("Trial limit reached \u2014 a product key unlocks unlimited backups.")
            messagebox.showinfo(
                "FizzDisk",
                "You've used all 50 trial backups.\n\n"
                "Enter a product key in Settings (the gear icon) to unlock "
                "unlimited backups and lifetime updates.")
            self._open_settings()
            return
        if self.cd_mode:
            jobs = [t for t in self.titles if t.index in self.selected]
            if not jobs:
                messagebox.showinfo("FizzDisk", "Select at least one track.")
                return
            if not self._cd_artist or not self._cd_album:
                self._confirm_cd_album()
                self._prefill_cd_folder()
            fmts = self._selected_cd_formats()
            label = " + ".join(f.upper() for f in fmts)
            self._stopping.clear()
            self._start_busy()
            self._stop_active()
            self.progress.set(0.0)
            self.var_eta.set("Preparing\u2026")
            self.log(f"Ripping CD to {label} into {self.var_out.get()} ...")
            threading.Thread(target=self._rip_cd_worker, args=(jobs, fmts),
                             daemon=True).start()
            return
        if not self._film_folder:
            self._ask_movie()
        if self.var_backup.get():
            self._run_backup()
            return
        jobs = [t for t in self.titles if t.index in self.selected]
        if not jobs:
            messagebox.showinfo("FizzDisk", "Select at least one title.")
            return
        self._stopping.clear()
        self._start_busy()
        self._stop_active()
        self.progress.set(0.0)
        self.var_eta.set("Preparing\u2026")
        self.log(f"Making MKV for {len(jobs)} title(s) into {self.var_out.get()} ...")
        threading.Thread(target=self._rip_worker, args=(jobs,), daemon=True).start()

    def on_stop(self):
        self._stopping.set()
        self._stop_idle()
        self._set_status("Stopping\u2026")
        ffutil.stop_active_proc()

    def _rip_cd_worker(self, jobs, fmts=None):
        out_root = self.var_out.get().strip() or str(Path.home() / "Music")
        os.makedirs(out_root, exist_ok=True)
        letter = engine._cd_letter(self.source)
        fmts = list(fmts) if fmts else ["flac"]
        # reuse the movie progress/ETA machinery by treating the album as ONE job
        self._prog_total = 1.0
        self._prog_base = 0.0
        self._prog_curdur = 1.0
        self._rip_started = time.time()
        stopped = False
        try:
            produced = engine.rip_cd(
                letter, self.titles, out_root, fmt=fmts[0],
                formats=fmts,
                artist=self._cd_artist or "Unknown Artist",
                album=self._cd_album or "Unknown Album",
                release_mbid=self._cd_release_mbid or None,
                release_group_mbid=self._cd_release_group_mbid or None,
                selected={t.index for t in jobs},
                progress_cb=self._on_title_progress,
                stop_event=self._stopping,
            )
            if self._stopping.is_set():
                stopped = True
            elif produced:
                lic.record_backup()          # one backup per disc ripped
                label = " + ".join(f.upper() for f in fmts)
                self.after(0, lambda p=produced, lb=label: self.log(
                    f"Ripped {len(p)} file(s) [{lb}] \u2014 {p[0]} ..."))
        except Exception as e:  # noqa: BLE001
            if not self._stopping.is_set():
                self.after(0, lambda e=e: self.log(f"ERROR: {e}", err=True))
                self.after(0, lambda: self._rip_done(False))
                return
            stopped = True
        self.after(0, lambda: self._rip_done(stopped))

    def _rip_worker(self, jobs):
        total = float(sum(t.duration for t in jobs) or 1)
        self._prog_total = total
        self._prog_base = 0.0
        self._rip_started = time.time()
        stopped = False
        made = 0
        sf = self.special_features
        for i, t in enumerate(jobs):
            if self._stopping.is_set():
                stopped = True
                break
            self._prog_curdur = t.duration or 0.0
            self.after(0, lambda t=t: self._set_status(f"Ripping {t.name} ..."))
            try:
                was_stopped = self._make_one(t, self._on_title_progress, len(jobs),
                                             sf_index=(i + 1) if sf else None)
            except Exception as e:  # noqa: BLE001
                if not self._stopping.is_set():
                    self.after(0, lambda e=e: self.log(f"ERROR: {e}", err=True))
                was_stopped = self._stopping.is_set()
            self._prog_base += (t.duration or 0.0)
            frac = self._prog_base / total
            self.after(0, lambda frac=frac: self.progress.set(min(1.0, frac)))
            if was_stopped:
                stopped = True
                break
            made += 1
        if made and not stopped:
            lic.record_backup()              # one backup per disc rip (Made MKV)
        self.after(0, lambda: self._rip_done(stopped))

    def _on_title_progress(self, frac):
        self.after(0, lambda: self._update_progress(frac))

    def _update_progress(self, frac):
        frac = max(0.0, min(1.0, frac))
        cur = getattr(self, "_prog_curdur", 0.0) or 0.0
        base = getattr(self, "_prog_base", 0.0) or 0.0
        total = getattr(self, "_prog_total", 1.0) or 1.0
        overall = (base + frac * cur) / total
        overall = max(0.0, min(1.0, overall))
        self.progress.set(overall)
        started = getattr(self, "_rip_started", None)
        if not started:
            return
        if overall > 0.02:
            elapsed = time.time() - started
            remain = max(0.0, (elapsed / overall) - elapsed)
            self.var_eta.set(f"{overall * 100:.0f}%  \u00b7  ~{self._fmt_eta(remain)}")
        else:
            self.var_eta.set(f"{overall * 100:.0f}%  \u00b7  preparing\u2026")

    @staticmethod
    def _fmt_eta(seconds: float) -> str:
        sec = int(max(0, round(seconds)))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m {s:02d}s"
        if m:
            return f"{m}m {s:02d}s"
        return f"{s}s"

    def _known_runtime(self):
        """Return the movie's runtime in minutes (via TMDb), cached on movie_info."""
        mi = self.movie_info or {}
        rt = mi.get("runtime")
        if rt:
            return rt
        title = mi.get("title")
        if title:
            try:
                rt = movie.movie_runtime(title, mi.get("year"))
            except Exception:
                rt = None
            if rt:
                mi["runtime"] = rt
        return rt

    def _best_title_for_movie(self, title):
        """Re-select the main title to the one whose duration best matches the
        known movie runtime.

        Obfuscated discs (e.g. Lionsgate) can present a *truncated* playlist as
        the disc's "main" title, so an auto-rip loses scenes/credits from the end
        (bd_get_main_title / "longest" alone is not reliable). When the movie's
        real runtime is known (via TMDb) we prefer the title closest to that
        length. Returns (chosen_title, changed).
        """
        rt = self._known_runtime()
        if not rt or not title or getattr(title, "source_kind", "") != "bd":
            return title, False
        # Only guide the *main feature* title. A secondary title (featurette,
        # trailer) the user deliberately selected must not be hijacked to the
        # feature-length title.
        if not getattr(title, "is_main", False):
            return title, False
        target = float(rt) * 60.0
        cur = float(title.duration or 0.0)
        if cur <= 0:
            return title, False
        all_bd = [t for t in self.titles
                  if getattr(t, "source_kind", "") == "bd" and (t.duration or 0) > 0]
        if len(all_bd) <= 1:
            return title, False
        best = min(all_bd, key=lambda t: abs(float(t.duration or 0) - target))
        # Only override if the current pick is meaningfully off the real runtime
        # and a clearly closer match exists (don't hijack a deliberate choice).
        tol = max(90.0, target * 0.02)
        if best is not title and abs(cur - target) > tol and \
                abs(float(best.duration or 0) - target) < abs(cur - target) - tol:
            return best, True
        return title, False

    def _make_one(self, title, progress_cb=None, num_jobs=1, sf_index=None):
        # Obfuscated-disc guard: if we know the movie's runtime, prefer the title
        # whose duration matches it -- a decoy/truncated "main" playlist on
        # discs like Lionsgate otherwise loses scenes/credits from the end.
        title, reselected = self._best_title_for_movie(title)
        if reselected:
            rt = self._known_runtime()
            self.log(f"Disc title '{_safe_name(title.name)}' is closest to the "
                     f"known runtime ({rt} min) \u2014 using it.")
        out_dir = self.var_out.get().strip()
        tmpl = self.var_tmpl.get().strip() or "{name}.mkv"
        if "{name}" not in tmpl and num_jobs > 1:
            stem, ext = os.path.splitext(tmpl)
            tmpl = f"{stem} ({{name}}){ext or '.mkv'}"
        if not os.path.splitext(tmpl)[1]:
            tmpl += ".mkv"
        name = tmpl.replace("{name}", _safe_name(title.name))
        if sf_index is not None:
            # special-features rip: name each output with the movie title
            film = _safe_name(self._film_folder or "movie")
            name = f"{film} - Special Features {sf_index}.mkv"
        film_dir = os.path.join(out_dir, self._film_folder or "")
        os.makedirs(film_dir, exist_ok=True)
        out_path = os.path.join(film_dir, name)
        if self._poster_bytes:
            try:
                with open(os.path.join(film_dir, "poster.jpg"), "wb") as pf:
                    pf.write(self._poster_bytes)
            except Exception:
                pass

        if not title.clips:
            bd_playlist = (title.info or {}).get("bd_playlist")
            if bd_playlist is not None:
                audio_idx = (sorted(self.audio_sel) if self.detail_title is title
                             else list(range(len(title.audio))))
                sub_idx = (sorted(self.sub_sel) if self.detail_title is title
                           else list(range(len(title.subtitles))))
                try:
                    rc = bdlib.rip_bd(self.source, title, out_path, video=True,
                                      audio_idx=audio_idx, sub_idx=sub_idx,
                                      copy_codecs=not self.var_encode.get(),
                                      progress_cb=progress_cb,
                                      stop_event=self._stopping,
                                      total_bytes=title.size_bytes)
                except RuntimeError:
                    if self._stopping.is_set():
                        return True
                    # The bundled decoder couldn't decrypt this disc (typical
                    # for AACS2/UHD 4K). Fall back to the bundled ripper.
                    if not engine.RIPPER:
                        raise
                    self.log("That decoder didn't work \u2014 let's try another\u2026")
                    try:
                        mk_titles = engine.scan_titles(self.source) or []
                    except Exception:
                        mk_titles = []
                    if not mk_titles:
                        raise
                    if title.duration:
                        tgt = min(mk_titles, key=lambda t: abs((t.duration or 0) - title.duration))
                    else:
                        tgt = max(mk_titles, key=lambda t: t.duration or 0)
                    fb_total = title.size_bytes or max(1, int((title.duration or 1) * 33_000_000 / 8))
                    produced = engine.disc_rip(self.source, tgt.index, out_dir,
                                               progress_cb=progress_cb,
                                               output_path=out_path,
                                               total_bytes=fb_total)
                    if produced:
                        if os.path.abspath(produced) != os.path.abspath(out_path):
                            try:
                                os.replace(produced, out_path)
                            except OSError:
                                pass
                        self.after(0, lambda p=out_path: self.log("Made " + p))
                        return self._stopping.is_set()
                    raise
                if rc != 0:
                    if self._stopping.is_set():
                        return True
                    raise RuntimeError(f"Blu-ray rip failed (rc={rc}) for {title.name}")
                self.after(0, lambda p=out_path: self.log("Made " + p))
                return self._stopping.is_set()
            total_b = getattr(title, "size_bytes", 0) or 0
            if total_b <= 0:
                rate = 33_000_000 if getattr(title, "source_kind", "") == "bd" else 8_000_000
                total_b = max(1, int((title.duration or 1) * rate / 8))
            expected_out = os.path.join(out_dir, _safe_name(title.name))
            produced = engine.disc_rip(self.source, title.index, out_dir,
                                          progress_cb=progress_cb,
                                          output_path=expected_out,
                                          total_bytes=total_b)
            if produced:
                if os.path.abspath(produced) != os.path.abspath(out_path):
                    try:
                        os.replace(produced, out_path)
                    except OSError:
                        pass
                self.after(0, lambda p=out_path: self.log("Made " + p))
                return self._stopping.is_set()
            if self._stopping.is_set():
                return True
            raise RuntimeError(f"Rip failed for {title.name}")

        audio_idx = sorted(self.audio_sel) if self.detail_title is title else list(range(len(title.audio)))
        sub_idx = sorted(self.sub_sel) if self.detail_title is title else list(range(len(title.subtitles)))
        dur = title.duration or 1

        def _cb(d):
            if d.get("event") == "disk_copy_fallback":
                # The live-drive rip came up short; remux() is now copying the
                # whole disc to the hard drive and re-ripping. Warn the user so
                # the long quiet stretch isn't mistaken for a hang.
                self.after(0, lambda d=d: self._notify_fallback(title, d))
                return
            if progress_cb:
                progress_cb(max(0.0, min(1.0, d.get("time", 0.0) / dur)))

        cb = _cb
        rc = ffutil.remux(title, out_path, video=True, audio_idx=audio_idx,
                          sub_idx=sub_idx, copy_codecs=not self.var_encode.get(),
                          progress_cb=cb)
        if rc != 0:
            if self._stopping.is_set():
                return True
            raise RuntimeError(f"ffmpeg exited {rc} for {title.name}")
        if getattr(title, "_remux_truncated", False):
            # The rip "succeeded" (ffmpeg rc 0) but the output is known-short
            # and the disc-copy recovery couldn't fix it. Tell the user instead
            # of silently reporting success on a truncated file.
            self.after(0, lambda p=out_path, t=title: self._warn_truncated(t, p))
        self.after(0, lambda p=out_path: self.log("Made " + p))
        return self._stopping.is_set()

    def _notify_fallback(self, title, d):
        """Tell the user the rip entered the slow disc-copy recovery path."""
        from .model import _fmt_duration
        got, exp = d.get("got") or 0.0, d.get("expected") or 0.0
        msg = (f"The drive could only read {_fmt_duration(got)} of "
               f"{_fmt_duration(exp)} for {title.name}. FizzDisk is now copying "
               f"the whole disc to your hard drive and ripping from that copy "
               f"instead \u2014 this recovers the full movie but takes extra time "
               f"(roughly the length of a full disc copy). Please leave the "
               f"disc in the drive.")
        self.log("Disc read came up short \u2014 switching to disc-copy recovery "
                 "(this will take longer).")
        self._set_status("Recovering via disc copy\u2026")
        try:
            from tkinter import messagebox
            messagebox.showinfo("Rip needs a second pass", msg, parent=self)
        except Exception:
            pass

    def _warn_truncated(self, title, out_path):
        """Surface a visible warning for a rip that completed short."""
        try:
            got = ffutil.validate_mkv(out_path).get("duration") or 0.0
        except Exception:
            got = 0.0
        exp = title.duration or 0.0
        from .model import _fmt_duration
        msg = (f"WARNING: {os.path.basename(out_path)} is INCOMPLETE \u2014 "
               f"got {_fmt_duration(got) if got else 'unknown length'}"
               f" of {_fmt_duration(exp)} expected. The disc could not "
               f"be fully read even after a disc-copy retry; the file is "
               f"missing its ending. Try cleaning the disc or another drive.")
        self.log(msg)
        try:
            from tkinter import messagebox
            messagebox.showwarning("Rip incomplete", msg, parent=self)
        except Exception:
            pass

    def _rip_done(self, stopped=False):
        self._start_ready()    # scan is complete + title chosen — Start is actionable again
        self._stop_idle()
        self._scan_idle()     # rip is over (done or stopped) — allow scanning again
        if stopped:
            self.var_eta.set("Stopped")
            self._set_status("Stopped")
            self.log("Rip stopped.")
        else:
            self.progress.set(1.0)
            self.var_eta.set("Done")
            self._set_status("Done")
            self.log("Rip finished.")
            self._eject_source()

    def _eject_source(self):
        """Eject the physical optical drive after a completed rip (BD/DVD/CD).

        Honours the Settings > 'Auto-eject disc after rip' toggle (default on).
        """
        try:
            if not prefs.get("auto_eject_after_rip", True):
                return
            letter = engine._optical_drive_letter(self.source)
            if letter and engine.eject_drive(letter):
                self.log(f"Ejected drive {letter}.")
        except Exception:
            pass

    def _scan_idle(self):
        """Scan button: idle/ready — brand orange, enabled, 'Scan'."""
        try:
            self.btn_scan.configure(fg_color=_BRAND_ORANGE, hover_color=_BRAND_ORANGE_HOVER,
                                    text_color="#ffffff", text="Scan", state="normal")
        except Exception:
            pass

    def _scan_running(self):
        """Scan button: busy scanning — default button palette (all other buttons),
        disabled with greyed text, label 'Scanning...'."""
        try:
            self.btn_scan.configure(fg_color=getattr(self, "_start_fg", ("#3B8ED0", "#1F6AA5")),
                                    hover_color=getattr(self, "_start_hover", ("#36719F", "#144870")),
                                    text_color=getattr(self, "_start_text", ("#DCE4EE", "#DCE4EE")),
                                    text="Scanning\u2026", state="disabled")
        except Exception:
            pass

    def _scan_pending(self):
        """Scan button: greyed out (disabled) after a scan until the rip finishes."""
        try:
            self.btn_scan.configure(fg_color=getattr(self, "_start_fg", ("#3B8ED0", "#1F6AA5")),
                                    hover_color=getattr(self, "_start_hover", ("#36719F", "#144870")),
                                    text_color=getattr(self, "_start_text", ("#DCE4EE", "#DCE4EE")),
                                    text="Scan", state="disabled")
        except Exception:
            pass

    def _start_ready(self):
        """Start button: actionable (scan complete + title chosen) — brand orange,
        enabled, to draw attention that a rip can start."""
        try:
            self.btn_make.configure(state="normal", fg_color=_BRAND_ORANGE,
                                    hover_color=_BRAND_ORANGE_HOVER, text_color="#ffffff")
        except Exception:
            pass

    def _start_busy(self):
        """Start button: clicked / rip running — app-default blue, disabled
        (customtkinter greys the text automatically in the disabled state)."""
        try:
            self.btn_make.configure(state="disabled", fg_color=self._start_fg,
                                    hover_color=self._start_hover,
                                    text_color=self._start_text)
        except Exception:
            pass

    def _stop_active(self):
        """Stop button: rip in progress — brand orange, enabled, to draw
        attention that it can be stopped."""
        try:
            self.btn_stop.configure(state="normal", fg_color=_BRAND_ORANGE,
                                    hover_color=_BRAND_ORANGE_HOVER, text_color="#ffffff")
        except Exception:
            pass

    def _stop_idle(self):
        """Stop button: no rip running / stopped — app-default blue, disabled
        (customtkinter greys the text automatically in the disabled state)."""
        try:
            self.btn_stop.configure(state="disabled", fg_color=self._stop_fg,
                                    hover_color=self._stop_hover,
                                    text_color=self._stop_text)
        except Exception:
            pass

    def _run_backup(self):
        import shutil
        src = self.source
        out_root = self.var_out.get().strip()
        os.makedirs(out_root, exist_ok=True)
        p = Path(src)
        disk = p.name if p.suffix else (p if p.is_dir() else Path(src))
        name = disk if isinstance(disk, str) else disk.name
        folder = self._film_folder or (name or "disc")
        dst = os.path.join(out_root, folder)
        os.makedirs(dst, exist_ok=True)
        self.log(f"Backing up {src} -> {dst}")
        self.progress.set(0.30)
        base = p if p.is_dir() else Path(src)
        for sub in ("AACS", "BDMV", "CERTIFICATE", "VIDEO_TS"):
            srcd = base / sub
            if srcd.is_dir():
                self.log(f"  copy {srcd}")
                try:
                    shutil.copytree(srcd, os.path.join(dst, sub), dirs_exist_ok=True)
                except Exception:
                    pass
        if Path(src).suffix.lower() == ".iso":
            try:
                shutil.copy2(src, os.path.join(dst, Path(src).name))
            except Exception:
                pass
        self.progress.set(1.0)
        lic.record_backup()      # a raw-disk backup also counts against the trial
        self.log("Backup complete.")

    # ---------------------------------------------------------------- misc
    def log(self, msg, err=False):
        def _append():
            try:
                self.text.config(state="normal")
                self.text.insert("end", msg + "\n", "err" if err else None)
                self.text.see("end")
                self.text.config(state="disabled")
            except Exception:
                pass
        self.after(0, _append)

    def _on_close(self):
        self._stopping.set()
        ffutil.stop_active_proc()
        self.destroy()


def _safe_name(s: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in s).strip()
    return out or "title"


def main():
    import sys
    app = FizzDiskApp()
    argv = sys.argv[1:] if len(sys.argv) > 1 else []
    if argv:
        src = " ".join(argv)
        app.var_source.set(src)
        app._source_from_combo()
        app.after(300, app.on_scan)
    app.mainloop()


if __name__ == "__main__":
    main()
