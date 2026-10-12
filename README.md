# FizzDisk

A **clean-room, open-source ripper** for Windows. It scans an optical disc, ISO
image, or disc/image folder (DVD `VIDEO_TS`, Blu-ray `BDMV`), lists every title
and its audio/subtitle tracks, and remuxes the selected titles into a single
Matroska (`.mkv`) file — losslessly (stream copy) by default. It also detects
an **audio CD** and rips it to FLAC/MP3 with MusicBrainz metadata and cover art.

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
- **Audio CD ripping** — a music CD is detected automatically (via the drive's
  audio tracks), identified by **MusicBrainz Disc-ID** (with a fuzzy TOC +
  duration fallback), and its cover art is fetched from the **Cover Art
  Archive** (saved as `cover.jpg`). Tracks are ripped with **paranoia error
  correction** using the bundled `cd-paranoia` (libcdio-paranoia), encoded with
  the bundled `ffmpeg.exe` to **FLAC** (default) or **MP3** (radio-button
  choice), tagged (title/artist/album/track) with the **cover art embedded**,
  and saved as `<Output>/<Artist>/<Album>/NN - Track Title.<ext>`; an existing
  artist folder is reused and the album merged into it. No online account or
  API key required.
- **Trial & licensing** — the app offers **50 trial backups**; once they're
  used a **product key** is required for unlimited backups and lifetime
  updates. The key is self-validating (offline, HMAC-signed), so no server is
  needed. Open **Settings** via the **gear icon** (left of the light/dark
  toggle) to see trial status, activate a key, and set a TheMovieDB API key.
  Generate a key with `python -m fizzdisk.license gen`.

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

## Website & payments

The live site is deployed at **https://fizzdisk.philspad.com** (also `fizzdisk.com`) —
a static, fully serverless site based on the existing themed landing page. Source
lives in `website/`; the deployed copy is on the server at
`/volume1/Server/srv/dir-local/fizzdisk` (mounted as `/srv/dir/fizzdisk` in the
Caddy container). It is pure static HTML/CSS/JS — no backend or database.

Files (mirror the deployed layout):
- `website/index.html` — the themed landing page: free 50-backup trial, the
  original feature / how-it-works sections, a new "Buy a license" section, and a
  "Download for Windows" button pointing at the real installer. Brand mark and
  favicon use the app's two-tone disc (blue ring / amber center).
- `website/success.html` — shown after checkout. It **mints the buyer's product
  key client-side** (no server) with a Copy button + activation instructions;
  refreshing keeps the same key (URL hash) and a `mailto:` link lets them email
  it to themselves.
- `website/assets/app.css`, `website/assets/app.js` — the original theme and
  section behavior; unchanged.
- `website/assets/config.js` — **edit before go-live:** `BUY_URL` (paste your
  2Checkout checkout link), `PRICE`, `VERSION`, `DOWNLOAD_URL`, `SUCCESS_URL`.
- `website/assets/keygen.js` — cross-context key generator (WebCrypto with a
  pure-JS SHA-256/HMAC fallback), mirroring `fizzdisk/license.py` so every key
  it issues validates in the app. Verify: `node website/assets/keygen.js`.
- `website/assets/fizzdisk_icon.png`, `fizzdisk.ico` — brand icon / favicon.
- `website/download/FizzDiskSetup.exe` — the installer to upload each release
  (git-ignored; it's a large build artifact).

> **Keep the secret in sync.** The HMAC key in `website/assets/keygen.js`
> (`FIZZ_SECRET`) MUST equal `_SECRET` in `fizzdisk/license.py`. Change both or
> every key the site issues will be rejected by the app.

### Deployment (server .35)
The site is served by Caddy's `file_server` — no reload is needed after pushing
files. The `fizzdisk` directory is root-owned, so writes need sudo (the scratch
helpers `ssh35.py` / `upload35.py` in the Hermes cache handle this via
`sudo -A tee` with an askpass helper). To update: edit `website/`, then run the
upload script. To access the box: SSH as `Philspad@192.168.0.35`.

### Accepting payment

No processor cares that this rips video/audio — that's not a restricted
category. Stripe, PayPal, 2Checkout, Paddle and Lemon Squeezy will all approve
you. Pick based on geography, fees and who handles tax:

- **2Checkout (chosen)** — broadest international reach (200+ countries /
  territories, localized checkout, 100+ currencies), and the sign-up flow has
  an explicit *digital products / software* category. Important nuance:
  **2Sell** (3.5% + $0.35) means **you** handle sales tax & compliance;
  **2Monetize** is their merchant-of-record tier where 2Checkout handles global
  VAT, invoicing and refunds for you (higher fee, but zero tax bookkeeping). If
  you sell internationally, 2Monetize is usually the better value despite the
  fee.
- **Stripe** — cheaper on domestic sales (2.9% + 30¢) and the most
  developer-friendly, but you handle VAT yourself (unless Stripe Tax) and need
  an account in a supported country with a bank/ACH payout. Use a *Payment
  Link* and set its success URL to your `success.html`.
- **Paddle / Lemon Squeezy** — merchant of record (like 2Monetize) but with
  simpler onboarding.

Whichever you choose, the flow is the same: create a product → copy its hosted
checkout/buy URL → paste into `BUY_URL` in `config.js` → set the
**post-payment (success / Approved) URL** to your `success.html`. The key is
minted on that page in the buyer's browser; nothing else is required on your
side.

## License
Open source. Uses ffmpeg (LGPL/GPL) and, optionally, a user-installed MakeMKV
for AACS decryption as a last resort.
