# FizzDisk

A **clean-room, open-source ripper** for Windows. It scans an optical disc, ISO
image, or disc/image folder (DVD `VIDEO_TS`, Blu-ray `BDMV`), lists every title
and its audio/subtitle tracks, and remuxes the selected titles into a single
Matroska (`.mkv`) file — losslessly (stream copy) by default. It also detects
an **audio CD** and rips it to FLAC/MP3 with MusicBrainz metadata and cover art.

FizzDisk is **free software** — no trial period, no product key and no
activation. It is built entirely with free, open-source components and is
**fully self-contained**: it needs no other ripper, no Python, and no VC++
runtime on the target machine. A bundled console ripper is used *only* as a
last-resort decryption fallback for a disc the bundled decoder cannot open, and
is never required.

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
- **Click Start** — select one or more titles, choose the output folder and file
  name template, then remux them to `.mkv` with **stream copy (lossless)** by
  default, or re-encode if you prefer.
- **AACS decryption** — protected Blu-ray discs are decrypted in-house by the
  bundled open-source decoder (libbluray + libaacs + libbdplus + keydb.cfg).
  Only if that fails does FizzDisk fall back to the bundled console ripper.
- **Movie & TV metadata** — when you scan a movie or TV disc, FizzDisk
  identifies the title and year and fetches a poster. Identification uses
  **TheMovieDB** when an API key is configured (most accurate — title, year and
  an HD poster), and falls back to **Wikipedia** when no key is set (no account
  required). See [Getting movie metadata & a TMDb key](#getting-movie-metadata--a-tmdb-key)
  below.
- **Audio CD ripping** — a music CD is detected automatically (via the drive's
  audio tracks), identified by **MusicBrainz Disc-ID** (with a fuzzy TOC +
  duration fallback), and its cover art is fetched from the **Cover Art
  Archive** (saved as `cover.jpg`). Tracks are ripped with **paranoia error
  correction** using the bundled `cd-paranoia` (libcdio-paranoia), encoded with
  the bundled `ffmpeg.exe` to **FLAC** (default) or **MP3**, tagged
  (title/artist/album/track) with the **cover art embedded**, and saved as
  `<Output>/<Artist>/<Album>/NN - Track Title.<ext>`; an existing artist folder
  is reused and the album merged into it. No online account or API key required.

The whole thing is driven by a **GUI** (tkinter/CustomTkinter). A small **CLI**
is also provided for scripting.

---

## Getting movie metadata & a TMDb key

Movie and TV-show metadata (title, year, poster) is entirely **optional** —
FizzDisk works fine without it, and it gently nudges you to set it up on first
launch. The more accurate path uses TheMovieDB:

- **Set a key (optional, recommended).** Create a free account at
  [themoviedb.org](https://www.themoviedb.org) → **Settings → API** → request a
  *plugin / API key* (it's free). Copy the key, then in FizzDisk open
  **Settings** via the **gear icon** to the left of the light/dark toggle,
  paste the key into **TheMovieDB API key**, and click **Save**. FizzDisk stores
  the key per-user in `%LOCALAPPDATA%\FizzDisk\tmdb_api_key.txt` (no admin rights
  needed, even though the app installs under Program Files).
- **Without a key** FizzDisk falls back to **Wikipedia**, which needs no
  account.
- The key is never required — it only improves the *quality* of identification.
  With a key you get the correct title/year and an HD poster; without it you get
  a best-effort match. CD ripping never needs a key at all.

---

## Requirements

- **Windows 10/11**
- **Python 3.8+** (only needed to run from source; the packaged `.exe` does not
  require Python to be installed)
- **ffmpeg** — bundled in `bin\` (ffmpeg + ffprobe). The app finds its own
  bundled copy first, then the system PATH.
- *(optional)* more metadata accuracy — a key from TheMovieDB (see above).

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
If a bundled build exists in `dist\`, run **`dist\FizzDiskSetup-<version>.exe`** —
a single installer that puts FizzDisk into its own folder (Program Files),
creates Start Menu / Desktop shortcuts and an uninstaller, and asks for admin
(UAC) once during install. The installed app is a normal `onedir` layout with
ffmpeg, libbluray/libaacs and libcdio-paranoia bundled, so it starts instantly
(no per-launch temp extraction). Build it with `python build.py`.

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
│  ├─ movie.py      # movie/TV title+year+poster lookup (TMDb + Wikipedia fallback)
│  ├─ musicdisc.py  # MusicBrainz Disc-ID identification + cover art
│  ├─ cdda.py       # audio-CD scan + error-corrected rip (cd-paranoia)
│  ├─ cli.py        # scan / mux command-line interface
│  └─ __main__.py
├─ bin/             # bundled ffmpeg.exe, ffprobe.exe, cd-paranoia, the ripper, AACS libs
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
  (libbluray + libaacs + libbdplus + keydb.cfg). The bundled console ripper is a
  decryption **fallback only**, never required.
- **Lossless by default.** Tracks are stream-copied (no re-encode) so rips are
  fast and quality is preserved. Uncheck the box to re-encode.
- **Native paths.** All media paths passed to ffmpeg/ffprobe are Windows native.

## Website & development

The live site is deployed at **https://fizzdisk.philspad.com** (also `fizzdisk.com`) —
a static, fully serverless landing page (the source is in `website/`; the
deployed copy lives on the server at `/volume1/Server/srv/dir-local/fizzdisk`,
mounted as `/srv/dir/fizzdisk` in the Caddy container). It is pure static
HTML/CSS/JS — no backend or database.

Because FizzDisk is free software, the site has **no Buy button and no license
page** — instead it has a **"Support us"** section with a **donation** link
("Help me pay for my DGX Sparks!") pointing to **Buy Me a Coffee**. There is also
an **"Opening it for the first time?"** block explaining the SmartScreen
"unknown publisher" step, because FizzDisk isn't code-signed.

The only hosted service is a small stdlib HTTP container (`server/license_api.py`,
`fizzdisk-lic`) that **counts installer downloads per version** and serves a
private Basic-auth **/stats** page. The old `/activate`, `/deactivate` and
`/status` endpoints still run but are no longer called by the app, since
licensing was removed in v1.4.0 when FizzDisk became free software. Deployment
lives on server `.35`, served by Caddy's `file_server`; edits go through the
`website/` tree and an SSH/sudo upload script.

## License
Open source. Uses ffmpeg (LGPL/GPL); AACS decryption is handled by the bundled
libbluray/libaacs/libbdplus stack, with the bundled console ripper as a last
resort.
