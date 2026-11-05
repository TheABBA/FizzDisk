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

from . import engine, ffutil, bdlib, movie, prefs, tvdb
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

# Sentinel return values for the movie/TV confirm popups: when the user clicks
# the "this is actually the other kind of disc" button, the popup returns one
# of these so the flow can switch to the opposite identifier instead of
# committing to the wrong one.
_SWITCH_TO_TV = "@@switch_to_tv@@"
_SWITCH_TO_MOVIE = "@@switch_to_movie@@"
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
        self._rip_active = False             # True while a rip worker is running
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
        # TV show identification (multi-episode disc)
        self.is_tv = False
        self.tv_info: Optional[dict] = None     # tvdb.identify_tv_disc() result
        self._tv_prompted = False
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
        self.text.tag_config("warn", foreground="#f59e0b")

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
            self.text.tag_config("warn", foreground=pal.get("warn", "#f59e0b"))
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
        # HARD re-entrancy guard. Disabling btn_scan is NOT sufficient: the
        # configure() call lives in a try/except and any other caller (a retry
        # path, a rebound key, an auto-scan on media change) can re-enter here
        # while a scan thread is still running. Two concurrent probes on ONE
        # optical device DEADLOCK libdvdnav/libdvdread -- verified: a single
        # `-f dvdvideo` probe returns in 3.5s, three concurrent probes on the
        # same drive all hang past 90s and leave unkillable children behind.
        if getattr(self, "_scan_in_flight", False):
            return
        # Also block scanning while a rip is in progress — the rip worker
        # uses the same optical drive and concurrent scan probes wedge it.
        if not self._stopping.is_set() and getattr(self, "_rip_active", False):
            self.log("Wait for the current rip to finish before scanning.")
            return
        # Clear any stale _stopping flag from a previous rip/stop so the
        # scan worker doesn't immediately bail out with "Scan cancelled."
        # (_stopping is set by on_stop and cleared by _rip_done, but if
        # _rip_done didn't run — e.g. the rip thread crashed — it can stick.)
        self._stopping.clear()
        src = self.var_source.get().strip()
        if not src:
            messagebox.showinfo("FizzDisk", "Choose a source first.")
            return
        self._scan_in_flight = True
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
        # Wait for Windows to finish mounting the disc before probing.
        # When a disc is inserted, Windows takes 2-5 seconds to mount the
        # filesystem (UDF/ISO9660).  Probing before the mount completes
        # causes I/O errors and drive retries that look like a read storm.
        # We poll for the BDMV/VIDEO_TS directory to appear, up to 10 seconds.
        m = re.match(r"^([A-Za-z]):[/\\]?$", src.strip())
        if m:
            drive = m.group(1).upper()
            root = f"{drive}:\\"
            waited = 0
            ready = False
            while waited < 10 and not self._stopping.is_set():
                try:
                    if os.path.isdir(root):
                        if (Path(root) / "BDMV").is_dir() or \
                           (Path(root) / "VIDEO_TS").is_dir() or \
                           (Path(root) / "AACS").is_dir():
                            ready = True
                            break
                        # For audio CDs there may be no video folders, but
                        # the drive is still accessible — check it's readable.
                        try:
                            os.listdir(root)
                            ready = True
                            break
                        except OSError:
                            pass
                except OSError:
                    pass
                time.sleep(0.5)
                waited += 0.5
            if not ready and not self._stopping.is_set():
                # Drive not ready after 10s — try anyway (the scan may
                # still succeed, or it'll produce a clear error).
                pass
        if self._stopping.is_set():
            self.after(0, lambda: self._scan_done_error("Scan cancelled."))
            return
        try:
            res = engine.scan_source(src)
        except Exception as e:  # noqa: BLE001
            self.after(0, lambda e=e: self._scan_done_error(str(e)))
            return
        self.after(0, lambda: self._scan_done(res.titles, res.info,
                                              res.source_kind, res.meta))

    def _scan_done(self, titles, info, source_kind="", meta=None):
        self._scan_in_flight = False
        self.titles = titles
        self.special_features = False
        self._sf_prompted = False
        # reset TV show state for each new scan
        self.is_tv = False
        self.tv_info = None
        self._tv_prompted = False
        # invalidate the cached disc-name so a new disc is re-read
        self._cached_disc_name = None
        if titles:
            self._scan_pending()
        else:
            self._scan_idle()
        self._set_status(f"Scan complete \u2014 {len(titles)} title(s)")
        self.log(info)
        # Warn about BD+ UP FRONT, right after the scan, rather than letting the
        # user queue a rip that is guaranteed to die partway through. The scan
        # itself succeeds on these discs (index/playlist files are not BD+
        # protected), so this is the first moment we can say anything useful.
        try:
            if (source_kind != "cd" and bdlib.disc_has_bdplus(self.source)
                    and not bdlib.bdplus_ready(self.source)):
                self._warn_bdplus(bdlib._BDPLUS_MESSAGE)
        except Exception:
            pass
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
            # Check if this disc looks like a TV show (multiple episode-length
            # titles) before falling back to the movie lookup.
            disc_dicts = [{"index": t.index, "name": t.name,
                           "duration": t.duration or 0} for t in titles]
            if tvdb.looks_like_tv(disc_dicts):
                try:
                    self._lookup_tv_async()
                except Exception:
                    traceback.print_exc()
                    # fall back to movie lookup
                    try:
                        self._lookup_movie_async()
                    except Exception:
                        traceback.print_exc()
            else:
                try:
                    self._lookup_movie_async()
                except Exception:
                    traceback.print_exc()

    def _scan_done_error(self, msg):
        self._scan_in_flight = False
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
        dscr = f"{track_count} tracks (will be saved as "
        if (meta.get("disc_count") or 1) > 1:
            dscr += f"{meta.get('disc_number') or 1} of {meta.get('disc_count') or 1}, "
        dscr += f"{artist} / {album} / files)"
        ctk.CTkLabel(frame, text=dscr,
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
        """Search for the album by artist/name, show cover thumbnails to pick,
        then fill in the track names. Falls back to manual entry if no results."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Search for album")
        dlg.transient(self)
        dlg.grab_set()
        dlg.attributes("-topmost", True)
        dlg.geometry("560x520")
        dlg.resizable(True, True)
        frame = ctk.CTkFrame(dlg, corner_radius=14)
        frame.pack(fill="both", expand=True, padx=12, pady=12)

        ctk.CTkLabel(frame, text="Search for your album",
                     font=("Segoe UI", 12, "bold"), anchor="w"
                     ).pack(anchor="w", padx=12, pady=(10, 4))

        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 4))
        ar_var = tk.StringVar(value=self._cd_artist or "")
        al_var = tk.StringVar(value=self._cd_album or "")
        ctk.CTkLabel(row, text="Artist:").pack(side="left", padx=(0, 4))
        ctk.CTkEntry(row, width=160, textvariable=ar_var,
                     placeholder_text="Artist").pack(side="left", padx=(0, 8))
        ctk.CTkLabel(row, text="Album:").pack(side="left", padx=(0, 4))
        ctk.CTkEntry(row, width=160, textvariable=al_var,
                     placeholder_text="Album").pack(side="left", padx=(0, 8))

        result = {"v": None}  # holds the chosen meta dict or "manual"

        # Scrollable results area
        results_frame = ctk.CTkScrollableFrame(frame, corner_radius=10)
        results_frame.pack(fill="both", expand=True, padx=12, pady=(4, 8))

        status_var = tk.StringVar(value="")
        ctk.CTkLabel(frame, textvariable=status_var, text_color="gray",
                     font=("Segoe UI", 10), anchor="w"
                     ).pack(anchor="w", padx=12)

        # ---- Worker: search MusicBrainz ----
        def do_search():
            a = ar_var.get().strip()
            b = al_var.get().strip()
            if not a and not b:
                status_var.set("Enter an artist or album name to search.")
                return
            status_var.set("Searching\u2026")
            # Clear previous results
            for w in results_frame.winfo_children():
                w.destroy()
            self._cd_search_photos = []

            def work():
                try:
                    from . import musicdisc as _md
                    cands = _md.search_albums(a, b, limit=8)
                except Exception:
                    cands = []
                self.after(0, lambda: _show_results(cands))

            threading.Thread(target=work, daemon=True).start()

        def _show_results(cands):
            if not cands:
                status_var.set("No results \u2014 click \"Enter manually\" to type the names yourself.")
                return
            status_var.set(f"{len(cands)} album(s) found \u2014 click one to select it.")

            for cand in cands:
                row_f = ctk.CTkFrame(results_frame, corner_radius=8, fg_color=("#f0f2f6", "#272a30"))
                row_f.pack(fill="x", padx=4, pady=2)

                # Cover thumbnail (if available and PIL is installed)
                cover_url = cand.get("cover_url")
                if cover_url and HAS_PIL:
                    def _load_thumb(url, rf=row_f, c=cand):
                        try:
                            req = urllib.request.Request(url, headers={"User-Agent": "FizzDisk/1.0"})
                            with urllib.request.urlopen(req, timeout=10) as resp:
                                data = resp.read()
                            import io as _io
                            img = Image.open(_io.BytesIO(data))
                            img.thumbnail((60, 60), Image.LANCZOS)
                            if img.mode not in ("RGB", "RGBA"):
                                img = img.convert("RGB")
                            cimg = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
                            lbl = ctk.CTkLabel(rf, image=cimg, text="")
                            lbl.image = cimg
                            lbl.pack(side="left", padx=6, pady=4)
                            self._cd_search_photos.append(cimg)
                        except Exception:
                            pass
                    threading.Thread(target=_load_thumb, args=(cover_url,), daemon=True).start()

                # Album info
                info_text = f"{cand.get('artist', '?')} \u2014 {cand.get('album', '?')}"
                if cand.get("year"):
                    info_text += f" ({cand['year']})"
                tracks = cand.get("tracks") or []
                if tracks:
                    info_text += f"  ({len(tracks)} tracks)"
                ctk.CTkLabel(row_f, text=info_text, anchor="w", justify="left",
                             wraplength=380, font=("Segoe UI", 11)
                             ).pack(side="left", fill="x", expand=True, padx=6, pady=4)

                def pick(c=cand):
                    result["v"] = c
                    dlg.destroy()

                ctk.CTkButton(row_f, text="Select", width=70, corner_radius=8,
                              fg_color=_BRAND_ORANGE, hover_color=_BRAND_ORANGE_HOVER,
                              text_color="#ffffff", command=pick
                              ).pack(side="right", padx=6, pady=4)

        # ---- Buttons ----
        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkButton(btns, text="Search", width=90, corner_radius=10,
                      fg_color=_BRAND_ORANGE, hover_color=_BRAND_ORANGE_HOVER,
                      text_color="#ffffff", command=do_search
                      ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text="Enter manually", corner_radius=10,
                      fg_color="transparent", border_width=1,
                      command=lambda: (result.__setitem__("v", "manual"), dlg.destroy())
                      ).pack(side="left")
        ctk.CTkButton(btns, text="Cancel", corner_radius=10,
                      fg_color="transparent", border_width=1,
                      command=dlg.destroy).pack(side="right")

        # Auto-search if we already have an artist or album
        if self._cd_artist or self._cd_album:
            self.after(300, do_search)

        dlg.bind("<Return>", lambda e: do_search())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self.wait_window(dlg)

        chosen = result["v"]
        if chosen == "manual":
            self._manual_cd_entry()
        elif chosen and isinstance(chosen, dict):
            # Fill in the artist, album, track names, cover, and MBIDs.
            self._cd_artist = chosen.get("artist") or "Unknown Artist"
            self._cd_album = chosen.get("album") or "Unknown Album"
            self._cd_release_mbid = chosen.get("release_mbid") or ""
            self._cd_release_group_mbid = chosen.get("release_group_mbid") or ""
            self.cd_meta = chosen
            # Rename the disc titles to the matched track names.
            mb_tracks = {t.get("position"): t.get("title", "")
                         for t in (chosen.get("tracks") or [])}
            for t in self.titles:
                num = (t.info or {}).get("cd_number")
                title = mb_tracks.get(num, "") if num else ""
                if title:
                    t.name = f"{num:02d} - {title}"
                    t.info["cd_title"] = title
            self.log(f"CD identified: {self._cd_artist} \u2014 {self._cd_album}")
            self._populate_tree()
            self._prefill_cd_folder()
            self._show_cd_album()

    def _manual_cd_entry(self):
        """Let the user type the artist, album, and each track name manually."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Enter album manually")
        dlg.transient(self)
        dlg.grab_set()
        dlg.attributes("-topmost", True)
        dlg.geometry("480x440")
        dlg.resizable(True, True)
        frame = ctk.CTkFrame(dlg, corner_radius=14)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        sc = ctk.CTkScrollableFrame(frame, corner_radius=10)
        sc.pack(fill="both", expand=True, padx=4, pady=4)

        ctk.CTkLabel(sc, text="Artist:", anchor="w").pack(anchor="w", padx=8, pady=(8, 0))
        ar = tk.StringVar(value=self._cd_artist or "")
        ctk.CTkEntry(sc, textvariable=ar, width=380,
                     placeholder_text="Artist name").pack(anchor="w", padx=8, pady=(0, 6))

        ctk.CTkLabel(sc, text="Album:", anchor="w").pack(anchor="w", padx=8)
        al = tk.StringVar(value=self._cd_album or "")
        ctk.CTkEntry(sc, textvariable=al, width=380,
                     placeholder_text="Album name").pack(anchor="w", padx=8, pady=(0, 10))

        ctk.CTkLabel(sc, text="Track names:", anchor="w",
                     font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=8, pady=(0, 4))

        track_vars = []
        for t in self.titles:
            num = (t.info or {}).get("cd_number") or (t.index + 1)
            row = ctk.CTkFrame(sc, fg_color="transparent")
            row.pack(fill="x", padx=8, pady=1)
            ctk.CTkLabel(row, text=f"#{int(num):02d}", width=30,
                         anchor="e").pack(side="left", padx=(0, 6))
            v = tk.StringVar(value=(t.info or {}).get("cd_title") or "")
            track_vars.append((num, v))
            ctk.CTkEntry(row, textvariable=v, width=320,
                         placeholder_text=f"Track {int(num)}").pack(side="left", fill="x", expand=True)

        res = {"ok": False}

        def ok():
            res["ok"] = True
            dlg.destroy()

        ctk.CTkButton(frame, text="Save", width=90, corner_radius=10,
                       fg_color=_BRAND_ORANGE, hover_color=_BRAND_ORANGE_HOVER,
                       text_color="#ffffff", command=ok).pack(side="right", padx=12, pady=(0, 10))
        ctk.CTkButton(frame, text="Cancel", corner_radius=10,
                       fg_color="transparent", border_width=1,
                       command=dlg.destroy).pack(side="right", padx=(0, 8))
        dlg.bind("<Return>", lambda e: ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self.wait_window(dlg)

        if res["ok"]:
            self._cd_artist = ar.get().strip() or "Unknown Artist"
            self._cd_album = al.get().strip() or "Unknown Album"
            self._cd_release_mbid = ""
            self._cd_release_group_mbid = ""
            for num, v in track_vars:
                title = v.get().strip()
                if not title:
                    title = f"Track {int(num)}"
                for t in self.titles:
                    if (t.info or {}).get("cd_number") == num:
                        t.name = f"{int(num):02d} - {title}"
                        t.info["cd_title"] = title
                        break
            self.log(f"CD saved as: {self._cd_artist} \u2014 {self._cd_album}")
            self._populate_tree()
            self._prefill_cd_folder()
            self._show_cd_album()

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
        # Cache the disc-metadata name from the first call so we never re-open
        # the raw device (``//./D:``) after the scan — a second raw-device read
        # while the drive is still settling from the scan probe can wedge it.
        if hasattr(self, "_cached_disc_name") and self._cached_disc_name is not None:
            cached = self._cached_disc_name
            if cached and movie.name_is_useful(cached):
                return cached
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
            self._cached_disc_name = name or label
            if movie.name_is_useful(name):
                return name
            if movie.name_is_useful(label):
                return label
            return name or label or src
        base = os.path.basename(src.rstrip("\\/"))
        name = movie.disc_metadata_name(src)
        self._cached_disc_name = name or base
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

    # ----------------------------------------------------------- TV show flow
    def _lookup_tv_async(self):
        """Identify a TV show disc: search TMDb for the show, then match
        disc titles to episodes by duration + order.  Runs in a background
        thread, marshals the result to ``_tv_done`` on the main thread."""
        term = self._source_term()
        self.log("Looking up TV show: " + (term or "(unknown)"))
        disc_dicts = [{"index": t.index, "name": t.name,
                       "duration": t.duration or 0} for t in self.titles]

        def work():
            try:
                result = tvdb.identify_tv_disc(term, disc_dicts)
                err = None
            except Exception as e:  # noqa: BLE001
                result, err = None, str(e)
            self.after(0, lambda: self._tv_done(result, term, err))

        threading.Thread(target=work, daemon=True).start()

    def _tv_done(self, result, term, err):
        if not result:
            self.log("TV show not found" + ((": " + err) if err else ""))
            # Fall back to the movie identification flow.
            self._lookup_movie_async()
            return
        self.log(f"TV show identified: {result['show']['name']} "
                 f"({len(result['matches'])} title(s) matched)")
        self.is_tv = True
        self.tv_info = result
        # Load the show poster (reuses the movie poster machinery).
        poster_url = result["show"].get("poster")
        if poster_url:
            self._load_poster(poster_url)
        # Ask the user to confirm this is the right show.
        self._ask_tv_show(result)

    def _ask_tv_show(self, result):
        """Confirm the identified TV show + season with the user.

        Shows the show name and a season dropdown (pre-filled with the
        matcher's best guess).  The user can:
          * Confirm the show + season -> apply episode matches
          * Change the season -> re-match against that season's episodes
          * Search for a different show -> text entry + TMDb search + picker
        """
        if self._tv_prompted:
            return
        self._tv_prompted = True
        show = result["show"]
        # Determine the matched season from the first matched episode.
        matched_season = None
        for m in result["matches"]:
            if m["episode"] is not None:
                matched_season = m["season"]
                break
        if matched_season is None:
            matched_season = 1

        # Get the list of available seasons from the show.
        seasons = [s["season_number"] for s in (show.get("seasons") or [])
                    if s["season_number"] != 0]  # skip specials
        if not seasons:
            seasons = [1]
        if matched_season not in seasons:
            seasons.insert(0, matched_season)

        # Show the confirmation dialog.
        chosen = self._confirm_tv_show_dialog(show["name"], seasons, matched_season)

        if chosen == _SWITCH_TO_MOVIE:
            # User says this disc is actually a movie.
            self._switch_to_movie()
            return

        if chosen is None:
            # User cancelled entirely — fall back to movie flow.
            self.is_tv = False
            self.tv_info = None
            self._lookup_movie_async()
            return

        if isinstance(chosen, dict) and chosen.get("action") == "search":
            # User wants to search for a different show.
            new_name = chosen.get("name", "")
            self._tv_search_different_show(new_name, result)
            return

        # chosen is a season number — apply it.
        season = chosen if isinstance(chosen, int) else matched_season
        self._apply_tv_match(result, show, season)

    def _confirm_tv_show_dialog(self, show_name, seasons, default_season):
        """Show the confirmation popup with a season dropdown.

        Returns:
          int (the chosen season number),
          dict {"action": "search", "name": str} (search for a different show),
          or None (cancelled).
        """
        res = {"v": None}
        dlg = ctk.CTkToplevel(self)
        dlg.title("Confirm TV show")
        dlg.transient(self)
        dlg.grab_set()
        dlg.attributes("-topmost", True)
        frame = ctk.CTkFrame(dlg, corner_radius=14)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        ctk.CTkLabel(
            frame,
            text=f"Is this the correct TV show?\n\n{show_name}",
            font=("Segoe UI", 12), wraplength=460, justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 8))

        # Season dropdown
        season_row = ctk.CTkFrame(frame, fg_color="transparent")
        season_row.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(season_row, text="Season:").pack(side="left", padx=(0, 6))
        season_vals = [f"Season {s:02d}" for s in seasons]
        default_val = f"Season {default_season:02d}"
        season_var = tk.StringVar(value=default_val)
        season_combo = ctk.CTkComboBox(season_row, values=season_vals,
                                       variable=season_var, width=160)
        season_combo.pack(side="left")

        ctk.CTkLabel(
            frame,
            text="Episodes will be named and organized into\n"
                 "  Show Name (Year) / Season XX / SxxExx - Episode Title.mkv",
            font=("Segoe UI", 10), text_color="#888", wraplength=460,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 12))

        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 12))

        def confirm():
            # Parse the season number from the dropdown value.
            val = season_var.get()
            import re as _re
            m = _re.search(r"Season\s+(\d+)", val)
            res["v"] = int(m.group(1)) if m else default_season
            dlg.destroy()

        def search_other():
            res["v"] = {"action": "search", "name": ""}
            dlg.destroy()

        def to_movie():
            res["v"] = _SWITCH_TO_MOVIE
            dlg.destroy()

        def cancel():
            res["v"] = None
            dlg.destroy()

        ctk.CTkButton(btns, text="Yes, use this season", fg_color=_BRAND_ORANGE,
                      hover_color=_BRAND_ORANGE_HOVER, text_color="#ffffff",
                      command=confirm).pack(side="left", expand=True, fill="x",
                                             padx=(0, 5))
        ctk.CTkButton(btns, text="Search for a different show",
                      fg_color="transparent", border_width=1,
                      command=search_other).pack(side="left", padx=5)
        ctk.CTkButton(btns, text="It's a movie", fg_color="transparent",
                      border_width=1, command=to_movie).pack(side="left",
                                                             padx=5)
        ctk.CTkButton(btns, text="Cancel", fg_color="transparent",
                      border_width=1, command=cancel).pack(side="left",
                                                            padx=(5, 0))
        try:
            dlg.wait_window()
        except Exception:
            res["v"] = None
        return res["v"]

    def _tv_search_different_show(self, initial_name, old_result):
        """Let the user search for a different show by name."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Search for TV show")
        dlg.transient(self)
        dlg.grab_set()
        dlg.attributes("-topmost", True)
        dlg.geometry("480x420")
        dlg.resizable(True, True)
        frame = ctk.CTkFrame(dlg, corner_radius=14)
        frame.pack(fill="both", expand=True, padx=12, pady=12)

        ctk.CTkLabel(frame, text="Search for your TV show",
                     font=("Segoe UI", 12, "bold"), anchor="w"
                     ).pack(anchor="w", padx=12, pady=(10, 4))

        name_var = tk.StringVar(value=initial_name or "")
        ctk.CTkEntry(frame, textvariable=name_var, width=380,
                     placeholder_text="TV show name").pack(anchor="w", padx=12, pady=(0, 8))

        results_frame = ctk.CTkScrollableFrame(frame, corner_radius=10)
        results_frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        status_var = tk.StringVar(value="")
        ctk.CTkLabel(frame, textvariable=status_var, text_color="gray",
                     font=("Segoe UI", 10), anchor="w"
                     ).pack(anchor="w", padx=12)

        chosen = {"v": None}

        def do_search():
            q = name_var.get().strip()
            if not q:
                status_var.set("Enter a show name to search.")
                return
            status_var.set("Searching\u2026")
            for w in results_frame.winfo_children():
                w.destroy()

            def work():
                try:
                    clean = tvdb.clean_tv_term(q)
                    cands = tvdb.search_tv(clean or q)
                except Exception:
                    cands = []
                self.after(0, lambda: show_results(cands))

            threading.Thread(target=work, daemon=True).start()

        def show_results(cands):
            if not cands:
                status_var.set("No results \u2014 try a different name.")
                return
            status_var.set(f"{len(cands)} show(s) found \u2014 click one to select.")
            for cand in cands:
                row_f = ctk.CTkFrame(results_frame, corner_radius=8,
                                     fg_color=("#f0f2f6", "#272a30"))
                row_f.pack(fill="x", padx=4, pady=2)
                info = f"{cand.get('name', '?')}"
                if cand.get("year"):
                    info += f"  ({cand['year']})"
                ctk.CTkLabel(row_f, text=info, anchor="w", justify="left",
                             wraplength=340, font=("Segoe UI", 11)
                             ).pack(side="left", fill="x", expand=True,
                                    padx=6, pady=4)

                def pick(c=cand):
                    chosen["v"] = c
                    dlg.destroy()

                ctk.CTkButton(row_f, text="Select", width=70, corner_radius=8,
                              fg_color=_BRAND_ORANGE, hover_color=_BRAND_ORANGE_HOVER,
                              text_color="#ffffff", command=pick
                              ).pack(side="right", padx=6, pady=4)

        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkButton(btns, text="Search", width=90, corner_radius=10,
                      fg_color=_BRAND_ORANGE, hover_color=_BRAND_ORANGE_HOVER,
                      text_color="#ffffff", command=do_search
                      ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text="It's a movie", width=100, corner_radius=10,
                      fg_color="transparent", border_width=1,
                      command=lambda: (chosen.__setitem__("v", _SWITCH_TO_MOVIE),
                                       dlg.destroy())).pack(side="right", padx=(0, 8))
        ctk.CTkButton(btns, text="Cancel", corner_radius=10,
                      fg_color="transparent", border_width=1,
                      command=dlg.destroy).pack(side="right")

        if initial_name:
            self.after(300, do_search)
        dlg.bind("<Return>", lambda e: do_search())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self.wait_window(dlg)

        c = chosen["v"]
        if c == _SWITCH_TO_MOVIE:
            self._switch_to_movie()
            return
        if c is None:
            # Cancelled — fall back to movie flow.
            self.is_tv = False
            self.tv_info = None
            self._lookup_movie_async()
            return

        # User picked a show — fetch it, get seasons, re-match.
        show_id = c.get("id")
        if not show_id:
            self.is_tv = False
            self.tv_info = None
            self._lookup_movie_async()
            return

        # Fetch the show + all episodes, then re-match against the disc titles.
        disc_dicts = [{"index": t.index, "name": t.name,
                       "duration": t.duration or 0} for t in self.titles]

        def work():
            try:
                show = tvdb.get_show(show_id)
                if not show:
                    self.after(0, lambda: self._tv_done(None, "", "show not found"))
                    return
                all_eps = tvdb.get_all_episodes(show_id)
                if not all_eps:
                    self.after(0, lambda: self._tv_done(None, "", "no episodes found"))
                    return
                matches = tvdb.match_episodes(disc_dicts, all_eps)
                # Determine the matched season.
                matched_season = None
                for m in matches:
                    if m["episode"] is not None:
                        matched_season = m["season"]
                        break
                if matched_season is None:
                    matched_season = 1
                # Build a new result dict.
                new_result = {
                    "show": show,
                    "matches": matches,
                    "folder": tvdb.build_tv_folder(show),
                    "seasons": {},
                    "special_features": [],
                }
                # Fill in seasons structure.
                for m in matches:
                    if m["episode"] is not None:
                        sn = m["season"]
                        if sn not in new_result["seasons"]:
                            new_result["seasons"][sn] = {
                                "folder": tvdb.build_season_folder(sn),
                                "episodes": [],
                            }
                        new_result["seasons"][sn]["episodes"].append(m)
                self.after(0, lambda: self._apply_tv_match(new_result, show, matched_season))
            except Exception as e:
                self.after(0, lambda e=e: self._tv_done(None, "", str(e)))

        self.log(f"Searching TMDb for: {c.get('name')} ...")
        threading.Thread(target=work, daemon=True).start()

    def _apply_tv_match(self, result, show, season):
        """Apply the matched TV show data to the GUI (rename titles, set folder, etc.)."""
        self.is_tv = True
        self.tv_info = result
        folder = result.get("folder") or tvdb.build_tv_folder(show)
        self._film_folder = _safe_name(folder)
        # Rename matched titles to their episode names + select them.
        matched_indices = set()
        first_match = None
        for m in result["matches"]:
            if m["episode"] is not None:
                di = m["disc_index"]
                matched_indices.add(di)
                if first_match is None:
                    first_match = m
                for t in self.titles:
                    if t.index == di:
                        sn = m["season"]
                        ep_num = m["episode_number"]
                        ep_name = m["name"] or f"Episode {ep_num}"
                        t.name = f"S{sn:02d}E{ep_num:02d} - {ep_name}"
                        break
        if matched_indices:
            self.selected = matched_indices
        if first_match:
            sn = first_match["season"]
            ep_num = first_match["episode_number"]
            ep_name = first_match["name"] or f"Episode {ep_num}"
            tmpl = tvdb.build_episode_filename(sn, ep_num, ep_name)
            self.var_tmpl.set(tmpl)
        self.log(f"TV show: {show['name']} (Season {season:02d})  ->  "
                 f"{self._film_folder}/Season {season:02d}/S{season:02d}Exx - Episode.mkv")
        try:
            self._populate_tree()
        except Exception:
            traceback.print_exc()

    def _movie_done(self, cands, term, err):
        self.movie_candidates = cands or []
        if self.movie_candidates:
            self.log(f"Movie lookup: {len(self.movie_candidates)} possible match(es)")
        else:
            self.log("Movie not found" + ((": " + err) if err else ""))
        self._ask_movie()

    def _switch_to_tv(self):
        """User decided an identification popup is actually a TV show disc.

        Resets the TV-prompt state so the TV flow can run fresh, then kicks
        off the TV lookup.  Used from the movie confirm/pick popups.
        """
        self.log("Switching to TV show identification")
        self.is_tv = False
        self.tv_info = None
        self._tv_prompted = False
        self._lookup_tv_async()

    def _switch_to_movie(self):
        """User decided an identification popup is actually a movie disc.

        Resets the TV state and kicks off the movie lookup.  Used from the
        TV confirm popup when the disc isn't a TV show.
        """
        self.log("Switching to movie identification")
        self.is_tv = False
        self.tv_info = None
        self._tv_prompted = False
        self._lookup_movie_async()

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
                if ok == _SWITCH_TO_TV:
                    self._switch_to_tv()
                    return
                if ok:
                    name = guess["name"]
                else:
                    self.movie_info = guess
            else:
                picked = self._pick_movie(cands)
                if picked == _SWITCH_TO_TV:
                    self._switch_to_tv()
                    return
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
            if name == _SWITCH_TO_TV:
                self._switch_to_tv()
                return
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

        def to_tv():
            res["v"] = _SWITCH_TO_TV
            dlg.destroy()

        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(btns, text="Use this one", corner_radius=10, command=use).pack(side="left")
        ctk.CTkButton(btns, text="Let me type it in", corner_radius=10,
                      fg_color="transparent", border_width=1, command=manual).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="It's a TV show", corner_radius=10,
                      fg_color="transparent", border_width=1, command=to_tv
                      ).pack(side="left", padx=(8, 0))
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
        """Settings popup: TheMovieDB API key (the only setting)."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Settings")
        dlg.transient(self)
        dlg.geometry("560x380")
        dlg.resizable(True, True)
        body = ctk.CTkFrame(dlg, corner_radius=14)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        sc = ctk.CTkScrollableFrame(body, corner_radius=12)
        sc.pack(fill="both", expand=True, padx=4, pady=4)

        # ---- TheMovieDB API key ----
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


        # ---- BD+ conversion tables ----
        cardbd = ctk.CTkFrame(sc, corner_radius=12, fg_color=("#f0f2f6", "#272a30"))
        cardbd.pack(fill="x", anchor="w", padx=4, pady=(2, 10))
        ctk.CTkLabel(cardbd, text="BD+ protected discs", font=("Segoe UI", 12, "bold"),
                     anchor="w").pack(anchor="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(cardbd, text=(
            "A few Blu-rays add BD+ on top of AACS. BD+ deliberately corrupts the\n"
            "picture, so these discs rip with perfect audio but broken video unless\n"
            "a 'conversion table' for that exact disc is installed. Insert the disc\n"
            "and scan it, then use the disc ID below to obtain its table and add it."),
            justify="left", anchor="w", wraplength=470).pack(anchor="w", padx=12, pady=(0, 6))

        bd_status = tk.StringVar(value="Insert a disc and press Scan to check.")
        ctk.CTkLabel(cardbd, textvariable=bd_status, anchor="w", justify="left",
                     wraplength=470, text_color="#3b82f6").pack(anchor="w", padx=12, pady=(0, 6))

        def refresh_bd():
            try:
                from . import bdplus as _bdp
                src = self.source or ""
                if not src:
                    bd_status.set("No disc selected.")
                    return
                st = _bdp.status(src)
                if not st["bdplus"]:
                    bd_status.set("This disc does not use BD+ \u2014 nothing needed.")
                elif st["ready"]:
                    bd_status.set(f"BD+ disc ID {st['media_key']}\n"
                                  f"Conversion table INSTALLED \u2014 this disc can be ripped.")
                elif st["media_key"]:
                    bd_status.set(f"BD+ disc ID {st['media_key']}\n"
                                  f"No conversion table installed for this disc yet.")
                else:
                    bd_status.set("BD+ disc, but its ID could not be read "
                                  "(is the disc readable?).")
            except Exception as e:  # noqa: BLE001
                bd_status.set(f"Could not check: {e}")

        def copy_id():
            try:
                from . import bdplus as _bdp
                mk = _bdp.media_key(self.source or "")
                if mk:
                    self.clipboard_clear()
                    self.clipboard_append(mk)
                    bd_status.set(f"Disc ID copied to the clipboard:\n{mk}")
                else:
                    bd_status.set("Could not read this disc's ID.")
            except Exception as e:  # noqa: BLE001
                bd_status.set(f"Could not read the disc ID: {e}")

        def add_table():
            try:
                from . import bdplus as _bdp
                mk = _bdp.media_key(self.source or "")
                if not mk:
                    bd_status.set("Insert the BD+ disc first \u2014 its ID is needed "
                                  "to name the table.")
                    return
                p = filedialog.askopenfilename(
                    title="Select the conversion table for this disc",
                    filetypes=[("Conversion table", "*.bin"), ("All files", "*.*")])
                if not p:
                    return
                dst = _bdp.install_table(p, mk)
                bd_status.set(f"Installed \u2014 {os.path.basename(dst)}\n"
                              f"Scan the disc again and it should rip correctly.")
                self.log("BD+ conversion table installed for this disc.")
            except Exception as e:  # noqa: BLE001
                bd_status.set(f"Could not install that file: {e}")

        def open_folder():
            try:
                from . import bdplus as _bdp
                _bdp.ensure_vm0()
                os.startfile(_bdp.convtab_dir())   # noqa: S606
            except Exception as e:  # noqa: BLE001
                bd_status.set(f"Could not open the folder: {e}")

        bdrow = ctk.CTkFrame(cardbd, fg_color="transparent")
        bdrow.pack(anchor="w", padx=12, pady=(0, 10))
        ctk.CTkButton(bdrow, text="Check this disc", width=120, corner_radius=10,
                      command=refresh_bd).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bdrow, text="Copy disc ID", width=110, corner_radius=10,
                      fg_color="transparent", border_width=1,
                      command=copy_id).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bdrow, text="Add table\u2026", width=100, corner_radius=10,
                      fg_color="transparent", border_width=1,
                      command=add_table).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bdrow, text="Open folder", width=100, corner_radius=10,
                      fg_color="transparent", border_width=1,
                      command=open_folder).pack(side="left")
        if self.source:
            refresh_bd()

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

        # ---- Card 4: Multi-disc CD layout ----
        card4 = ctk.CTkFrame(sc, corner_radius=12, fg_color=("#f0f2f6", "#272a30"))
        card4.pack(fill="x", anchor="w", padx=4, pady=(2, 10))
        ctk.CTkLabel(card4, text="Multi-disc CDs (albums on 2+ discs)",
                     font=("Segoe UI", 12, "bold"), anchor="w"
                     ).pack(anchor="w", padx=12, pady=(10, 2))
        ctk.CTkLabel(card4, text=(
            "When you rip a 2-disc album, choose how the tracks are organised:\n"
            "  \u2022 Single folder \u2014 all tracks in one album folder, numbered\n"
            "    continuously across the discs (Disc 1 = 1..12, Disc 2 = 13..24).\n"
            "  \u2022 Disc folders \u2014 a \\\"Disc 1\\\"/\\\"Disc 2\\\" subfolder per disc,\n"
            "    each numbered from 1 on that disc."),
            justify="left", anchor="w", wraplength=470).pack(anchor="w", padx=12, pady=(0, 6))
        # Match the aesthetic of the other settings checkboxes (eject_cb):
        # same 15x15 checkbox, same corner_radius, auto-sized to the text.
        # This is an exclusive single-choice, so drive each checkbox's state
        # directly (sharing one StringVar across CTkCheckBox does NOT clear
        # siblings on its own, which left the old radio answer staying checked).
        md_boxes: list = []

        def _sel_md(name):
            for n, cb in md_boxes:
                cb.set(n == name)
            prefs.set_("cd_multidisc_layout", name)

        md_single = ctk.CTkCheckBox(card4, text="Single folder, continuous track numbers",
                                    corner_radius=8, checkbox_width=15, checkbox_height=15,
                                    width=17, command=lambda: _sel_md("single"))
        md_single.pack(anchor="w", padx=12, pady=(2, 0))
        md_disc = ctk.CTkCheckBox(card4, text="Disc folders (Disc 1 / Disc 2 ...)",
                                  corner_radius=8, checkbox_width=15, checkbox_height=15,
                                  width=17, command=lambda: _sel_md("disc_folders"))
        md_disc.pack(anchor="w", padx=12, pady=(0, 2))
        md_boxes.append(("single", md_single))
        md_boxes.append(("disc_folders", md_disc))
        # Establish the initial selection from the stored preference (single by default).
        _sel_md(prefs.get("cd_multidisc_layout", "single") or "single")
        ctk.CTkLabel(card4, text=(
            "You can change this any time; it applies to future rips."),
            text_color="gray", anchor="w").pack(anchor="w", padx=12, pady=(0, 10))

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
        ctk.CTkButton(btns, text="It's a TV show", corner_radius=10,
                      fg_color="transparent", border_width=1,
                      command=lambda: (res.__setitem__("v", _SWITCH_TO_TV), dlg.destroy())
                      ).pack(side="left", padx=(8, 0))
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
                            # Mirror the auto-lookup flow: fetch this movie's poster
                            # so it is available when the rip writes poster.jpg. The
                            # chooser thumbnails live in _pick_photos (_start_thumb_load)
                            # and are NOT the same as self._poster_bytes, so without
                            # this the poster would show in the selector but never save.
                            if picked.get("poster"):
                                self._load_poster(movie.highres_poster(
                                    picked.get("title"), picked.get("year"),
                                    picked.get("poster")))
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

        def to_tv():
            res["v"] = _SWITCH_TO_TV
            cancel.set()
            state["open"] = False
            dlg.destroy()

        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.grid(row=4, column=0, columnspan=3, pady=(12, 4))     # centered across the form
        ctk.CTkButton(btns, text="Search Again", corner_radius=10, command=search_again,
                      fg_color="transparent", border_width=1).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="It's a TV show", corner_radius=10, command=to_tv,
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
                # CD and TV show discs use additive toggling (users select
                # multiple tracks/episodes).  Movie discs use exclusive
                # single-select (one feature at a time) unless Ctrl/Shift
                # is held.  'Select All'/'Select None' always work too.
                if self.cd_mode or self.is_tv or (e.state & (0x4 | 0x1)):
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
        self.stream_frame.grid_columnconfigure(0, weight=1)
        self.stream_frame.grid_columnconfigure(1, weight=0)
        r = 0
        if title.audio:
            ctk.CTkLabel(self.stream_frame, text="Audio:", font=("Segoe UI", 10, "bold"), anchor="w").grid(row=r, column=0, sticky="nw", padx=4, pady=2)
            r += 1
            for i, a in enumerate(title.audio):
                v = tk.BooleanVar(value=True)
                cb = ctk.CTkCheckBox(self.stream_frame, text=a.label, corner_radius=6,
                                     checkbox_width=13, checkbox_height=13, width=300,
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
                                     checkbox_width=13, checkbox_height=13, width=300,
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
        # Reserve column 0 for text (wraps), column 1 for cover art (fixed).
        self.stream_frame.grid_columnconfigure(0, weight=1)
        self.stream_frame.grid_columnconfigure(1, weight=0, minsize=190)
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
        if (meta.get("disc_count") or 1) > 1:
            disc_num = meta.get("disc_number") or 1
            disc_cnt = meta.get("disc_count") or 1
            rows.insert(3, ("Disc", f"{disc_num} of {disc_cnt}"))
        for k, v in rows:
            if v:
                ctk.CTkLabel(self.stream_frame, text=f"{k}: {v}", anchor="w",
                             wraplength=260, justify="left").grid(
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
            self._rip_active = True
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
        self._rip_active = True
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
                # In TV mode, _make_one_tv computes its own SF index; don't
                # pass the movie-style sf_index.
                if self.is_tv:
                    was_stopped = self._make_one(t, self._on_title_progress,
                                                 len(jobs))
                else:
                    was_stopped = self._make_one(t, self._on_title_progress,
                                                 len(jobs),
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

    def _make_one(self, title, progress_cb=None, num_jobs=1, sf_index=None,
                  _tv_delegated=False):
        # Obfuscated-disc guard: if we know the movie's runtime, prefer the title
        # whose duration matches it -- a decoy/truncated "main" playlist on
        # discs like Lionsgate otherwise loses scenes/credits from the end.
        title, reselected = self._best_title_for_movie(title)
        if reselected:
            rt = self._known_runtime()
            self.log(f"Disc title '{_safe_name(title.name)}' is closest to the "
                     f"known runtime ({rt} min) \u2014 using it.")
        out_dir = self.var_out.get().strip()

        # ---- TV show: build Show Name (Year)/Season XX/SxxExx - Title.mkv ----
        if self.is_tv and self.tv_info and not _tv_delegated:
            return self._make_one_tv(title, out_dir, progress_cb, num_jobs,
                                     sf_index)

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
                except bdlib.BDPlusError as e:
                    # BD+ is terminal: no bundled fallback can strip it either,
                    # so do NOT burn minutes retrying. Delete the part-file so
                    # no corrupt video is left looking like a usable rip, and
                    # explain the real cause.
                    try:
                        if os.path.exists(out_path):
                            os.remove(out_path)
                    except OSError:
                        pass
                    self.after(0, lambda m=str(e): self._warn_bdplus(m))
                    raise
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
            if d.get("event") == "verify_unavailable":
                # The rip finished but could not be MEASURED (ffprobe missing,
                # usually deleted by antivirus). The rip itself is kept and is
                # most likely fine -- say so plainly instead of implying the
                # disc read came up short.
                self.after(0, lambda d=d: self._notify_verify_unavailable(d))
                return
            if d.get("event") == "disk_copy_fallback":
                # The live-drive rip came up short; remux() is now copying the
                # whole disc to the hard drive and re-ripping. Warn the user so
                # the long quiet stretch isn't mistaken for a hang.
                self.after(0, lambda d=d: self._notify_fallback(title, d))
                return
            if "fraction" in d:
                # Recovery phase (disc copy + re-rip) reports an explicit overall
                # fraction so the bar moves and ETA counts down continuously.
                if progress_cb:
                    progress_cb(max(0.0, min(1.0, d["fraction"])))
                return
            if progress_cb:
                progress_cb(max(0.0, min(1.0, d.get("time", 0.0) / dur)))

        cb = _cb
        rc = ffutil.remux(title, out_path, video=True, audio_idx=audio_idx,
                          sub_idx=sub_idx, copy_codecs=not self.var_encode.get(),
                          progress_cb=cb, stop_event=self._stopping)
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

    def _make_one_tv(self, title, out_dir, progress_cb, num_jobs, sf_index):
        """Rip one title from a TV show disc into the Show/Season/Episode structure.

        Looks up this title's episode match in ``self.tv_info`` and builds:
            out_dir/Show Name (Year)/Season XX/SxxExx - Episode Title.mkv
        Unmatched titles (special features) go into:
            out_dir/Show Name (Year)/Special Features/Show Name - SF N.mkv
        """
        show_folder = self._film_folder or "TV Show"
        match = None
        for m in self.tv_info["matches"]:
            if m["disc_index"] == title.index:
                match = m
                break

        if match and match["episode"] is not None:
            # Matched episode: Show/Season/SxxExx - Title.mkv
            season = match["season"]
            ep_num = match["episode_number"]
            ep_name = match["name"] or f"Episode {ep_num}"
            season_dir = os.path.join(out_dir, show_folder,
                                     tvdb.build_season_folder(season))
            os.makedirs(season_dir, exist_ok=True)
            fname = tvdb.build_episode_filename(season, ep_num, ep_name)
            out_path = os.path.join(season_dir, fname)
        else:
            # Unmatched title (special feature / extra):
            # Show/Special Features/Show - Special Features N.mkv
            sf_dir = os.path.join(out_dir, show_folder, "Special Features")
            os.makedirs(sf_dir, exist_ok=True)
            # Compute the special-features index among unmatched titles.
            sf_idx = 1
            for m in self.tv_info["matches"]:
                if m["disc_index"] == title.index:
                    break
                if m["episode"] is None:
                    sf_idx += 1
            show_name = self.tv_info["show"].get("title") or "TV Show"
            fname = tvdb.build_special_features_filename(show_name, sf_idx)
            out_path = os.path.join(sf_dir, fname)

        # Save the show poster to the show folder (not the season folder).
        if self._poster_bytes:
            show_dir = os.path.join(out_dir, show_folder)
            try:
                with open(os.path.join(show_dir, "poster.jpg"), "wb") as pf:
                    pf.write(self._poster_bytes)
            except Exception:
                pass

        # Delegate the actual rip to the same engine as _make_one, but with
        # our computed out_path.  We temporarily set _film_folder to the
        # season subfolder and the template to the episode filename so the
        # existing _make_one logic produces the same out_path.
        if match and match["episode"] is not None:
            season = match["season"]
            rel_folder = os.path.join(show_folder,
                                     tvdb.build_season_folder(season))
        else:
            rel_folder = os.path.join(show_folder, "Special Features")

        saved_folder = self._film_folder
        saved_tmpl = self.var_tmpl.get()
        try:
            self._film_folder = rel_folder
            self.var_tmpl.set(os.path.basename(out_path))
            return self._make_one(title, progress_cb=progress_cb,
                                  num_jobs=num_jobs, sf_index=None,
                                  _tv_delegated=True)
        finally:
            self._film_folder = saved_folder
            self.var_tmpl.set(saved_tmpl)

    def _notify_verify_unavailable(self, d):
        """The rip completed but could not be verified because ffprobe is
        missing (antivirus deletes it from unsigned installs).

        This must NOT be worded as a short/failed rip: the output is kept and
        is usually complete. Previously a failed probe was indistinguishable
        from a truncated rip, which launched the disc-copy fallback and looped.
        """
        err = d.get("error") or "the verification tool could not run"
        self.log_warn("Rip finished but could not be verified \u2014 "
                      "ffprobe is missing (antivirus has likely removed it). "
                      "The rip was kept.")
        self._set_status("Rip complete (unverified)")
        try:
            from . import toolcheck
            msg = toolcheck.missing_tools_message() or str(err)
        except Exception:
            msg = str(err)
        try:
            messagebox.showwarning(
                "FizzDisk \u2014 could not verify the rip",
                "Your rip finished and has been kept.\n\n"
                "FizzDisk could not CHECK its length because a bundled tool "
                "is missing, so it cannot confirm the file is complete.\n\n"
                + msg)
        except Exception:
            pass

    def _notify_fallback(self, title, d):
        """Build a themed popup telling the user the rip entered the slow
        disc-copy recovery path, and why. Uses our customtkinter theme (not the
        Windows messagebox) and explains the usual USB cause."""
        from .model import _fmt_duration
        got, exp = d.get("got") or 0.0, d.get("expected") or 0.0
        body = ""
        self.log_warn("Disc read came up short \u2014 switching to disc-copy recovery "
                      "(this will take longer).")
        self._set_status("Recovering via disc copy\u2026")
        try:
            dlg = ctk.CTkToplevel(self)
            dlg.title("Rip needs a second pass")
            dlg.transient(self)
            dlg.grab_set()
            frame = ctk.CTkFrame(dlg, corner_radius=14)
            frame.pack(fill="both", expand=True, padx=12, pady=12)
            ctk.CTkLabel(frame, text="Rip needs a second pass",
                         font=("Segoe UI", 11, "bold"), text_color="#f59e0b",
                         anchor="w").pack(anchor="w", padx=10, pady=(10, 4))
            body = (
                f"The drive could only read {_fmt_duration(got)} of "
                f"{_fmt_duration(exp)} for {title.name}. FizzDisk is now copying "
                f"the whole disc to your hard drive and ripping from that copy "
                f"instead \u2014 this recovers the full movie, but it takes extra "
                f"time (roughly the length of a full disc copy). Please leave the "
                f"disc in the drive."
            )
            ctk.CTkLabel(frame, text=body, justify="left", anchor="w",
                         wraplength=420, font=("Segoe UI", 10)
                         ).pack(anchor="w", padx=10, pady=(2, 8))
            hint = (
                "This usually happens when the USB link between the drive and the "
                "computer can't keep up with the read. If you're going through a "
                "docking station, a hub, or any other adapter, try plugging the "
                "drive straight into a port on the computer \u2014 that often clears "
                "it up."
            )
            ctk.CTkLabel(frame, text=hint, justify="left", anchor="w",
                         text_color="gray", wraplength=420, font=("Segoe UI", 9)
                         ).pack(anchor="w", padx=10, pady=(0, 8))
            ok = ctk.CTkButton(frame, text="Got it", corner_radius=10,
                               fg_color="#f59e0b", hover_color="#d98e09",
                               text_color="#ffffff", command=dlg.destroy)
            ok.pack(anchor="e", padx=10, pady=(4, 10))
            dlg.bind("<Return>", lambda e: dlg.destroy())
            dlg.bind("<Escape>", lambda e: dlg.destroy())
            dlg.wait_window()
        except Exception:
            # Fall back to the native messagebox if the themed dialog can't build.
            try:
                from tkinter import messagebox
                messagebox.showinfo("Rip needs a second pass", body, parent=self)
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

    def _warn_bdplus(self, msg: str):
        """Explain a BD+ disc in plain language (themed popup, not a raw error).

        This is NOT a FizzDisk malfunction, so it must not read like a crash:
        the user needs to know the disc is unrippable by this tool and that
        nothing is wrong with their hardware.
        """
        self.log_warn("BD+ protected disc \u2014 FizzDisk cannot rip this title.")
        self._set_status("BD+ protected \u2014 cannot rip")
        try:
            dlg = ctk.CTkToplevel(self)
            dlg.title("BD+ protected disc")
            dlg.transient(self)
            dlg.grab_set()
            frame = ctk.CTkFrame(dlg, corner_radius=14)
            frame.pack(fill="both", expand=True, padx=12, pady=12)
            ctk.CTkLabel(frame, text="This disc is BD+ protected",
                         font=("Segoe UI", 11, "bold"), text_color="#f59e0b",
                         anchor="w").pack(anchor="w", padx=10, pady=(10, 4))
            ctk.CTkLabel(frame, text=msg, justify="left", anchor="w",
                         wraplength=430, font=("Segoe UI", 10)
                         ).pack(anchor="w", padx=10, pady=(2, 8))
            ok = ctk.CTkButton(frame, text="Got it", corner_radius=10,
                               fg_color="#f59e0b", hover_color="#d98e09",
                               text_color="#ffffff", command=dlg.destroy)
            ok.pack(anchor="e", padx=10, pady=(4, 10))
            dlg.bind("<Return>", lambda e: dlg.destroy())
            dlg.bind("<Escape>", lambda e: dlg.destroy())
        except Exception:
            try:
                messagebox.showwarning("BD+ protected disc", msg, parent=self)
            except Exception:
                pass

    def _rip_done(self, stopped=False):
        self._stop_idle()
        self._scan_idle()     # rip is over (done or stopped) — allow scanning again
        self._rip_active = False
        self._stopping.clear()  # clear the stop flag so the next scan can run
        # ---- Clean reset of all disc/scan/identification state ----
        # After a rip completes (or is stopped), the user will likely insert a
        # new disc and scan it.  Stale titles, movie metadata, TV info, and the
        # cached disc name from the *previous* disc must be cleared so the next
        # scan starts fresh — otherwise the old state can interfere with the
        # new disc's identification (e.g. the old movie runtime triggers
        # _rationalize_selection on the new disc, or the TV flag sends new
        # titles into the TV episode-matching path).
        self.titles = []
        self.selected.clear()
        self._clear_tree()
        self.movie_info = None
        self.movie_candidates = []
        self._film_folder = ""
        self.is_tv = False
        self.tv_info = None
        self._tv_prompted = False
        self.special_features = False
        self._sf_prompted = False
        self._cached_disc_name = None
        self.detail_title = None
        self._poster_bytes = None
        self._poster_photo = None
        self.cd_mode = False
        self.cd_meta = None
        self._cd_artist = ""
        self._cd_album = ""
        self._cd_release_mbid = ""
        self._cd_release_group_mbid = ""
        # Reset the output Folder field — a CD rip sets it to
        # ~/Music/<Artist>/<Album> and a movie/BD rip sets it to the film
        # folder; both must be cleared so the next disc starts at the default
        # output directory, not the previous disc's folder.
        self.var_out.set(self.output_dir)
        # After the clean reset there are no titles to rip — the Start button
        # goes back to the idle/disabled blue state (not amber/ready), since
        # there's nothing to start until a new disc is scanned.
        self._start_busy()
        if stopped:
            self.var_eta.set("Stopped")
            self._set_status("Stopped")
            self.log("Rip stopped.")
            # If the rip was stopped during a disc-copy fallback (scratched/
            # damaged disc), warn the user to check the disc before retrying.
            if any(getattr(t, "_remux_truncated", False) for t in self.titles):
                self.log_warn("The disc may be damaged or scratched \u2014 the rip "
                              "was recovering via a disc copy when you stopped it. "
                              "Clean the disc and try again, or try a different drive.")
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

    def log_warn(self, msg):
        """Append a log line rendered in brand amber (attention) — used only for
        the fallback/recovery notices so they stand out from normal log text."""
        def _append():
            try:
                self.text.config(state="normal")
                self.text.insert("end", msg + "\n", "warn")
                self.text.see("end")
                self.text.config(state="disabled")
            except Exception:
                pass
        self.after(0, _append)

    def _on_close(self):
        """Close the window IMMEDIATELY, then clean up in the background.

        Everything this used to do ran on the Tk UI thread: ``terminate()`` on
        the active ripper, then five synchronous ``taskkill`` calls. On an idle
        machine that is ~0.5s, but the case that matters is the one that goes
        wrong -- an ffmpeg/ffprobe wedged on a sick optical read sits in
        UNINTERRUPTIBLE kernel I/O, so ``terminate()`` and ``taskkill`` both
        stall on it for tens of seconds. While the UI thread is parked in that
        call Tk pumps no events at all, so the window stops redrawing and
        Windows paints the "not responding" ghost -- the freeze that forced
        the user to kill FizzDisk from Task Manager.

        So: hide the window first (the app LOOKS closed instantly), do the
        killing on a daemon thread, and guarantee the process actually dies
        with a watchdog, because a child that can never be reaped must not be
        able to keep FizzDisk alive.
        """
        if getattr(self, "_closing", False):
            return                      # ignore repeat X clicks
        self._closing = True

        # 1. Signal every background worker (rip + scan) to stop.
        self._stopping.set()

        # 2. Make the app disappear NOW, before any slow cleanup starts.
        try:
            self.withdraw()
        except Exception:
            pass

        # 3. Kill child processes off the UI thread so a wedged child can
        #    never block the shutdown.
        def _reap():
            try:
                ffutil.stop_active_proc()
            except Exception:
                pass
            # stop_active_proc only kills the *ripper*; the scan worker's
            # ffprobe children are not tracked, so sweep them by name too.
            import subprocess
            for name in ("ffprobe.exe", "ffmpeg.exe", "cd-paranoia.exe",
                         "makemkvcon64.exe", "fizzdisk-rip.exe"):
                try:
                    subprocess.run(["taskkill", "/F", "/IM", name],
                                   capture_output=True, timeout=5,
                                   creationflags=0x08000000)
                except Exception:
                    # A timeout here means the child is unreapable; the
                    # watchdog below still guarantees we exit.
                    pass

        threading.Thread(target=_reap, daemon=True).start()

        # 4. Watchdog: leave the grace period for a tidy Tk teardown, then
        #    force the process out. Daemon threads die with the process, and
        #    os._exit skips interpreter shutdown, which can itself block
        #    joining a thread stuck in kernel I/O.
        def _watchdog():
            time.sleep(5.0)
            os._exit(0)

        threading.Thread(target=_watchdog, daemon=True).start()

        # 5. Tear the UI down after a short beat so the kill sweep has a
        #    chance to land first (orphaned ffprobe must not outlive the GUI
        #    and keep reading the drive).
        self.after(300, self._finish_close)

    def _finish_close(self):
        """Destroy the Tk app and end the process, never hanging on exit."""
        try:
            self.quit()        # make mainloop() return
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


def _safe_name(s: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in s).strip()
    return out or "title"


def main():
    import sys
    app = FizzDiskApp()
    # Bundled-tool integrity: AV (Defender) false-positives the unsigned
    # binaries and can DELETE ffprobe.exe after install, leaving ffmpeg in
    # place. Rips then succeed but cannot be verified, which used to look
    # like an endless "copying the disc" fallback loop. Warn up front and
    # name the fix instead of failing mysteriously mid-rip.
    try:
        from . import toolcheck
        if not toolcheck.tools_ok():
            msg = toolcheck.missing_tools_message()
            app.log_warn("Missing bundled tool - see the warning dialog "
                         "(antivirus has most likely removed it).")
            app.after(400, lambda: messagebox.showwarning("FizzDisk", msg))
    except Exception:
        pass
    argv = sys.argv[1:] if len(sys.argv) > 1 else []
    # Headless CLI: `FizzDisk.exe scan|mux|rip ...` routes to the CLI harness
    # (used for testing the engine on machines with no Python installed).
    if argv and argv[0] in ("scan", "mux", "rip"):
        from . import cli as _cli
        sys.exit(_cli.main() or 0)
    if argv:
        src = " ".join(argv)
        app.var_source.set(src)
        app._source_from_combo()
        app.after(300, app.on_scan)
    app.mainloop()


if __name__ == "__main__":
    main()
