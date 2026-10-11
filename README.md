# FizzDisk

A **clean-room, open-source ripper** for Windows. It scans an optical disc, ISO
image, or disc/image folder (DVD `VIDEO_TS`, Blu-ray `BDMV`), lists every title
and its audio/subtitle tracks, and remuxes the selected titles into a single
Matroska (`.mkv`) file — losslessly (stream copy) by default.

FizzDisk is built entirely with free, open-source components and is **fully
self-contained**: it does **not** require MakeMKV, Python, or a VC++ runtime on
the target machine. A user-installed MakeMKV (`makemkvcon64.exe`) may be used
*optionally* as a decryption fallback for a disc the bundled decoder cannot
open, but it is never required.

---

## What it does

- **Scan sources** — a real Blu-ray / DVD disc in an optical drive, an `.iso`
  image, a `VIDEO_TS` or `BDMV` folder, or a single media file.
- **List titles** — every title on the source with its length, size and number
  of tracks, using its **own** parsers:
  - `dvdscan.py` — a clean-room DVD **IFO** (`TT_SRPT`/`VTS_PGCIT`) parser.
  - `bdscan.py` — a clean-room Blu-ray **MPLS** playlist parser.
- **Inspect tracks** — video, audio and subtitle streams per title with
  codec/language info.
- **Copy to MKV** — select one or more titles, choose the output folder and file
  name template, then remux them to `.mkv` with **stream copy (lossless)** by
  default, or re-encode if you prefer.
- **AACS decryption** — protected Blu-ray discs are decrypted in-house by the
  bundled open-source decoder (libbluray + libaacs + libbdplus + keydb.cfg).
  Only if that fails does FizzDisk fall back to an external ripper.

The whole thing is driven by a **GUI** (tkinter/CustomTkinter). A small **CLI**
is also provided for scripting.

---

## Requirements

- **Windows 10/11**
- **Python 3.8+** (only needed to run from source; the packaged `.exe` does not
  require Python to be installed)
- **ffmpeg** — bundled in `bin\` (ffmpeg + ffprobe). The app finds its own
  bundled copy first, then the system PATH.
- *(optional)* an external ripper — the app ships its own
  `bin\fizzdisk-rip.exe`, so none is required. A previously-installed
  `makemkvcon64.exe` is still detected and used as a last resort for discs the
  bundled decoder cannot open.

---

## Run it

### GUI (recommended)
Double-click **`FizzDisk.pyw`** (no console window), or run **`FizzDisk.bat`**.
Alternatively:
```
python -m fizzdisk
```

To auto-scan a source on launch:
```
python -m fizzdisk "D:\"
```

### Packaged app
If a bundled build exists in `dist\`, run **`dist\FizzDisk\FizzDisk.exe`** —
a self-contained, double-clickable application with ffmpeg included.

### CLI (scripting)
```
python -m fizzdisk.cli scan "D:\"
python -m fizzdisk.cli mux "D:\" --title 5 --out "C:\Users\Abba\Videos"
```
`scan` prints the detected titles; `mux` remuxes the selected title to MKV.

---

## How to use the GUI

1. Choose a **Source** (drive letter, ISO, folder, or file) and click **Scan**.
2. Review the detected **Titles** in the left pane (click the checkbox to
   select; the main title is pre-selected).
3. Click a title row to see its **Streams** in the right pane; toggle tracks
   if you want subset.
4. Set the **Output folder** and file-name template (default `{name}.mkv`).
5. Click **Start**. Watch progress in the log and status bar.

---

## Project layout

```
FizzDisk/
├─ fizzdisk/
│  ├─ gui.py        # tkinter/CustomTkinter GUI (the FizzDisk interface)
│  ├─ engine.py     # source detection, scanning, external-ripper backend, ripping
│  ├─ dvdscan.py    # clean-room DVD IFO parser (TT_SRPT / VTS_PGCIT)
│  ├─ bdscan.py     # clean-room Blu-ray MPLS parser
│  ├─ bdlib.py      # self-contained AACS decoder (libbluray + libaacs + libbdplus)
│  ├─ ffutil.py     # ffprobe/ffmpeg wrappers, remux, validation, path helpers
│  ├─ model.py      # data model (Stream, Clip, Title, ScanResult)
│  ├─ movie.py      # movie-title/year/poster metadata lookup
│  ├─ cli.py        # scan / mux command-line interface
│  └─ __main__.py
├─ bin/             # bundled ffmpeg.exe, ffprobe.exe, the ripper, AACS libs
├─ FizzDisk.pyw    # double-click GUI launcher (no console)
└─ FizzDisk.bat    # console-free launch helper
```

---

## Design notes

- **Clean-room parsers.** The DVD IFO and Blu-ray MPLS formats are parsed from
  scratch (`dvdscan.py`, `bdscan.py`) — the product does not depend on
  libdvdread / libbluray as *metadata* parsers and does not reuse any other
  project's parser code.
- **Self-contained engine.** The default engine is ffmpeg/ffprobe; AACS-protected
  Blu-rays are decrypted in-house by the bundled open-source stack
  (libbluray + libaacs + libbdplus + keydb.cfg). The external ripper is a
  decryption **fallback only**, never required.
- **Lossless by default.** Tracks are stream-copied (no re-encode) so rips are
  fast and quality is preserved. Uncheck the box to re-encode.
- **Native paths.** All media paths passed to ffmpeg/ffprobe are Windows native.

## License
Open source. Uses ffmpeg (LGPL/GPL) and, optionally, a user-installed MakeMKV
for AACS decryption as a last resort.
