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

from . import engine, ffutil, bdlib, movie
from .model import Title

# subtle per-window theming for the few native tk widgets kept (log text box)
_DARK = {"text_bg": "#0d1013", "text_fg": "#dfe3ea", "err": "#ff6b6b"}
_LIGHT = {"text_bg": "#ffffff", "text_fg": "#20242a", "err": "#b00020"}


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

        if ctk is not None:
            ctk.set_appearance_mode("Dark")     # dark is the default
            ctk.set_default_color_theme("blue")

        self._build_ui()
        self.refresh_drives()
        self._apply_theme()
        self._prompted_tmdb = False
        self.after(250, self._maybe_prompt_tmdb_key)
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
        self.btn_scan = ctk.CTkButton(top, text="Scan", width=80, corner_radius=10, command=self.on_scan)
        self.btn_scan.pack(side="left", padx=(0, 6))
        ctk.CTkButton(top, text="Refresh Drives", width=110, corner_radius=10, command=self.refresh_drives).pack(side="left")
        # theme toggle on the right
        self.btn_theme = ctk.CTkButton(top, text="\U0001F319", width=42, corner_radius=10,
                                       fg_color="transparent", border_width=1,
                                       command=self._toggle_theme)
        self.btn_theme.pack(side="right", padx=8)

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
        ctk.CTkLabel(left, text="Titles (click a row to select & view)",
                     anchor="w", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="ew", padx=8, pady=(3, 1))
        self.tree = ctk.CTkScrollableFrame(left, corner_radius=8, fg_color="transparent", height=150)
        self.tree._scrollbar.configure(height=60)
        self.tree.grid(row=1, column=0, sticky="nsew", padx=4, pady=2)
        self._row_frames: list = []
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
        ctk.CTkLabel(detail, text="Streams (selected title)", anchor="w", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="ew", padx=8, pady=(3, 1))
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
        ctk.CTkLabel(out_box, text="File name:", anchor="w").grid(row=2, column=0, sticky="w", padx=(8, 2), pady=1)
        self.var_tmpl = tk.StringVar(value="{name}.mkv")
        ctk.CTkEntry(out_box, textvariable=self.var_tmpl, corner_radius=8).grid(row=2, column=1, sticky="ew", padx=2, pady=1)

        self.var_backup = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(out_box, text="Raw Backup Only (Copy AACS/BDMV)", corner_radius=8,
                        checkbox_width=13, checkbox_height=13, width=17,
                        variable=self.var_backup).grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 0))
        self.var_encode = tk.BooleanVar(value=False)   # checked = re-encode; default = lossless stream copy
        ctk.CTkCheckBox(out_box, text="Compress MKV (H.264/x264/AAC)", corner_radius=8,
                        checkbox_width=13, checkbox_height=13, width=17,
                        variable=self.var_encode).grid(row=4, column=0, columnspan=3, sticky="w", padx=8, pady=(1, 2))

        btnrow = ctk.CTkFrame(out_box, corner_radius=8, fg_color="transparent")
        btnrow.grid(row=5, column=0, columnspan=3, sticky="ew", padx=8, pady=2)
        btnrow.grid_columnconfigure((0, 1), weight=1)
        self.btn_make = ctk.CTkButton(btnrow, text="Start", corner_radius=10, command=self.on_make, state="disabled")
        self.btn_make.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.btn_stop = ctk.CTkButton(btnrow, text="Stop", corner_radius=10, command=self.on_stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=(3, 0))
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
        if hasattr(self, "btn_theme"):
            self.btn_theme.configure(text="\u2600\ufe0f" if dark else "\U0001F319")

    def _toggle_theme(self):
        if ctk is None:
            return
        ctk.set_appearance_mode("Light" if ctk.get_appearance_mode() == "Dark" else "Dark")
        self._apply_theme()

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
        self.btn_scan.configure(state="disabled")
        threading.Thread(target=self._scan_worker, args=(src,), daemon=True).start()

    def _scan_worker(self, src):
        try:
            res = engine.scan_source(src)
        except Exception as e:  # noqa: BLE001
            self.after(0, lambda e=e: self._scan_done_error(str(e)))
            return
        self.after(0, lambda: self._scan_done(res.titles, res.info))

    def _scan_done(self, titles, info):
        self.titles = titles
        self.btn_scan.configure(state="normal")
        self._set_status(f"Scan complete \u2014 {len(titles)} title(s)")
        self.log(info)
        if titles:
            main = next((t for t in titles if t.is_main), None)
            if main is None:
                main = max(titles, key=lambda t: t.duration or 0)
            self.selected.add(main.index)
        self.btn_make.configure(state="normal" if titles else "disabled")
        self._populate_tree()
        self.var_eta.set("Ready \u2014 select titles, then click Start")
        main = next((t for t in titles if t.is_main and t.duration > 0), titles[0] if titles else None)
        if main:
            try:
                self._show_detail(main)
            except Exception:
                traceback.print_exc()
        try:
            self._lookup_movie_async()
        except Exception:
            traceback.print_exc()

    def _scan_done_error(self, msg):
        self.btn_scan.configure(state="normal")
        self._set_status("Scan failed")
        self.var_eta.set("Scan failed")
        self.log("ERROR: " + msg, err=True)
        messagebox.showerror("FizzDisk", "Could not scan source.\n" + msg)

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
            if movie.name_is_useful(label):
                return label
            # The label is blank/generic (e.g. 'LOGICAL_VOLUME_ID'); the media still
            # names itself in its raw UDF/ISO descriptor or BDMV/META. Dig it out.
            name = movie.disc_metadata_name(src)
            if name:
                return name
            return label or src
        base = os.path.basename(src.rstrip("\\/"))
        if movie.name_is_useful(base):
            return base
        # A folder/ISO source: try the disc metadata as a last resort.
        name = movie.disc_metadata_name(src)
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

        def _select(name):
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
            rb = ctk.CTkRadioButton(row, text=c["name"], variable=sel, value=c["name"],
                                    corner_radius=8, fg_color="#3b82f6",
                                    radiobutton_width=13, radiobutton_height=13, width=16,
                                    command=lambda n=c["name"]: _select(n))
            rb.pack(side="left", fill="x", expand=True, padx=(0, 4), pady=2)
            self._start_thumb_load(c, thumb, _tw, _th, 60, 86)

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
        ctk.CTkButton(btns, text="None of these \u2014 type it", corner_radius=10,
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
        if self._prompted_tmdb:
            return
        self._prompted_tmdb = True
        if movie.has_user_key():
            return
        dlg = ctk.CTkToplevel(self)
        dlg.title("High-quality posters")
        dlg.transient(self)
        dlg.geometry("460x320")
        dlg.resizable(False, False)
        body = ctk.CTkFrame(dlg, corner_radius=14)
        body.pack(fill="both", expand=True, padx=12, pady=12)

        ctk.CTkLabel(body, text="TheMovieDB API key (recommended)", font=("Segoe UI", 12, "bold"),
                     anchor="w").pack(anchor="w", padx=6, pady=(6, 2))
        ctk.CTkLabel(body, text=(
            "FizzDisk uses TheMovieDB to identify the movie title & year when a\n"
            "disc is scanned, and to download a sharp HD poster. It needs a free\n"
            "API key. We don't share one master key, so please grab your own\n"
            "(~1 minute) \u2014 each user having their own avoids usage limits.\n"
            "Without a key, FizzDisk falls back to Wikipedia: titles and years\n"
            "still work, but posters are smaller."),
            justify="left", anchor="w", wraplength=400).pack(anchor="w", padx=6, pady=(0, 4))

        def open_key():
            webbrowser.open("https://www.themoviedb.org/settings/api")

        ctk.CTkButton(body, text="Get a free API key \u2192", command=open_key,
                      width=180).pack(anchor="w", padx=6, pady=(0, 6))

        ctk.CTkLabel(body, text="Paste your API key:", anchor="w").pack(anchor="w", padx=6)
        key = tk.StringVar(value=movie.tmdb_key())
        entry = ctk.CTkEntry(body, width=380, textvariable=key,
                             placeholder_text="e.g. 1a2b3c...").pack(anchor="w", padx=6, pady=(2, 8))

        status = tk.StringVar(value="")
        def save():
            k = key.get().strip()
            if not k:
                self.log("No API key \u2014 using Wikipedia lookup.")
                status.set("Using Wikipedia (no key).")
            else:
                p = movie.set_tmdb_key(k)
                if p:
                    status.set("Saved \u2014 TMDb search & HD posters enabled.")
                    self.log("TMDb key saved \u2014 will identify movies & get HD posters.")
                else:
                    status.set("Couldn't write the key file.")
                    self.log("TMDb key: write failed.")
            self.after(800, dlg.destroy)

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(anchor="w", padx=6)
        ctk.CTkButton(row, text="Save", width=90, command=save).pack(side="left", padx=(0, 6))
        ctk.CTkButton(row, text="Skip (use Wikipedia)", fg_color="transparent", border_width=1,
                      command=dlg.destroy).pack(side="left")
        ctk.CTkLabel(body, textvariable=status, text_color="#3b82f6", anchor="w").pack(
            anchor="w", padx=6, pady=(6, 0))

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
        ctk.CTkLabel(frame, text="Enter the movie title — it searches as you type:",
                     font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(4, 8))
        ctk.CTkLabel(frame, text="Title:").grid(row=1, column=0, sticky="e", pady=2)
        var_title = tk.StringVar(value=t0)
        t = ctk.CTkEntry(frame, width=320, corner_radius=8, textvariable=var_title)
        t.grid(row=1, column=1, sticky="w", padx=6, pady=2)
        ctk.CTkLabel(frame, text="Year:").grid(row=2, column=0, sticky="e", pady=2)
        y = ctk.CTkEntry(frame, width=80, corner_radius=8)
        y.grid(row=2, column=1, sticky="w", padx=6, pady=2)
        ctk.CTkLabel(frame, text="(Optional)", font=("Segoe UI", 9), text_color="gray"
                     ).grid(row=2, column=2, sticky="w", padx=(2, 0))
        status = ctk.CTkLabel(frame, text="", font=("Segoe UI", 9), text_color="#3b82f6", anchor="w")
        status.grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 0))
        res = {"v": None}

        state = {"open": True, "last_q": t0}
        cancel = threading.Event()
        deb = {"id": None}

        def _norm(s):
            return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

        def safe(fn):
            """Run fn on the main thread; drop it if the dialog has been closed."""
            def run():
                if state["open"]:
                    try:
                        fn()
                    except Exception:
                        pass
            self.after(0, run)

        def do_search(q):
            if not (q or "").strip():
                safe(lambda: status.configure(text=""))
                return
            try:
                cands = movie.search_movies(q)
            except Exception:  # noqa: BLE001
                safe(lambda: status.configure(text="Search failed", text_color="#ef4444"))
                return
            if cancel.is_set() or not state["open"]:
                return
            best = cands[0] if cands else None
            if not best:
                safe(lambda: status.configure(text="No match yet — keep typing or Save as-is",
                                              text_color="#f59e0b"))
                return
            best_title = (best.get("title") or "").strip()
            best_year = best.get("year")

            def apply():
                if not state["open"]:
                    return
                # Normalize the title only when the typed text is recognisably the
                # same movie (avoids clobbering input). The year stays as typed.
                if _norm(q) == _norm(best_title) and best_title:
                    t.delete(0, "end")
                    t.insert(0, best_title)
                status.configure(text="Found: %s (%s) — type the year or leave blank" % (best_title, best_year),
                                 text_color="#16a34a")
            safe(apply)

        def search_again():
            if not state["open"]:
                return
            q = t.get().strip()
            status.configure(text="Searching…", text_color="#3b82f6")
            threading.Thread(target=do_search, args=(q,), daemon=True).start()

        def on_key(*_a):
            q = var_title.get().strip()
            if q == state.get("last_q"):
                return
            state["last_q"] = q
            if deb["id"] is not None:
                try:
                    self.after_cancel(deb["id"])
                except Exception:
                    pass
            deb["id"] = self.after(500, lambda: (deb.__setitem__("id", None), search_again()))

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

        t.bind("<KeyRelease>", on_key)
        var_title.trace_add("write", on_key)
        btns = ctk.CTkFrame(frame, fg_color="transparent")
        btns.grid(row=4, column=1, columnspan=2, sticky="e", pady=(10, 4))
        ctk.CTkButton(btns, text="Search Again", corner_radius=10, command=search_again,
                      fg_color="transparent", border_width=1).pack(side="left")
        ctk.CTkButton(btns, text="Save", corner_radius=10, command=ok).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Cancel", corner_radius=10,
                      fg_color="transparent", border_width=1, command=close).pack(side="left")
        dlg.bind("<Return>", lambda e: ok())
        dlg.bind("<Escape>", lambda e: close())
        t.focus_set()
        t.select_range(0, "end")
        if t0:                                                # prefill search, if any
            self.after(200, search_again)
        self.wait_window(dlg)
        return res["v"]

    def _clear_tree(self):
        for f in getattr(self, "_row_frames", []):
            try:
                f.destroy()
            except Exception:
                pass
        self._row_frames = []
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
                self._toggle_rip(idx)
                self._show_detail(self.titles[idx])
                try:
                    lbl.configure(text=self._row_text(self.titles[idx], s))
                except Exception:
                    pass
            label.bind("<Button-1>", _click)
            row.bind("<Button-1>", _click)
            self._row_frames.append(row)

    def _toggle_rip(self, idx):
        if idx in self.selected:
            self.selected.discard(idx)
        else:
            self.selected.add(idx)

    def _set_all(self, on):
        self.selected = {t.index for t in self.titles} if on else set()
        self._populate_tree()

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

    # -------------------------------------------------------------- ripping
    def on_make(self):
        if not self.var_out.get().strip():
            messagebox.showinfo("FizzDisk", "Choose an output folder.")
            return
        os.makedirs(self.var_out.get().strip(), exist_ok=True)
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
        self.btn_make.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.set(0.0)
        self.var_eta.set("Preparing\u2026")
        self.log(f"Making MKV for {len(jobs)} title(s) into {self.var_out.get()} ...")
        threading.Thread(target=self._rip_worker, args=(jobs,), daemon=True).start()

    def on_stop(self):
        self._stopping.set()
        self.btn_stop.configure(state="disabled")
        self._set_status("Stopping\u2026")
        ffutil.stop_active_proc()

    def _rip_worker(self, jobs):
        total = float(sum(t.duration for t in jobs) or 1)
        self._prog_total = total
        self._prog_base = 0.0
        self._rip_started = time.time()
        stopped = False
        for i, t in enumerate(jobs):
            if self._stopping.is_set():
                stopped = True
                break
            self._prog_curdur = t.duration or 0.0
            self.after(0, lambda t=t: self._set_status(f"Ripping {t.name} ..."))
            try:
                was_stopped = self._make_one(t, self._on_title_progress, len(jobs))
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

    def _make_one(self, title, progress_cb=None, num_jobs=1):
        out_dir = self.var_out.get().strip()
        tmpl = self.var_tmpl.get().strip() or "{name}.mkv"
        if "{name}" not in tmpl and num_jobs > 1:
            stem, ext = os.path.splitext(tmpl)
            tmpl = f"{stem} ({{name}}){ext or '.mkv'}"
        if not os.path.splitext(tmpl)[1]:
            tmpl += ".mkv"
        name = tmpl.replace("{name}", _safe_name(title.name))
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
                rc = bdlib.rip_bd(self.source, title, out_path, video=True,
                                  audio_idx=audio_idx, sub_idx=sub_idx,
                                  copy_codecs=not self.var_encode.get(),
                                  progress_cb=progress_cb,
                                  stop_event=self._stopping,
                                  total_bytes=title.size_bytes)
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
        cb = (lambda d: progress_cb(max(0.0, min(1.0, d.get("time", 0.0) / dur)))) if progress_cb else None
        rc = ffutil.remux(title, out_path, video=True, audio_idx=audio_idx,
                          sub_idx=sub_idx, copy_codecs=not self.var_encode.get(),
                          progress_cb=cb)
        if rc != 0:
            if self._stopping.is_set():
                return True
            raise RuntimeError(f"ffmpeg exited {rc} for {title.name}")
        self.after(0, lambda p=out_path: self.log("Made " + p))
        return self._stopping.is_set()

    def _rip_done(self, stopped=False):
        self.btn_make.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if stopped:
            self.var_eta.set("Stopped")
            self._set_status("Stopped")
            self.log("Rip stopped.")
        else:
            self.progress.set(1.0)
            self.var_eta.set("Done")
            self._set_status("Done")
            self.log("Rip finished.")

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
