# -*- coding: utf-8 -*-
"""Engine: detect sources, scan, and build MKVs.

Backends:
  * ffmpeg/ffprobe (open-source) - every file, unencrypted DVD/BD, remux.
  * the bundled libbluray + libaacs + libbdplus stack - AACS-protected Blu-ray.
  * an optional external ripper (fizzdisk-rip, or a user-installed console ripper)
    - only as a last resort for discs the pipeline cannot demux; auto-detected
    and never required.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

# When launched from a windowed (pythonw / PyInstaller) GUI, console executables
# such as the external ripper / powershell would flash a console window. Suppress it.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
from typing import Callable, List, Optional

from . import bdlib, bdscan, cdda, dvdscan, ffutil, musicdisc
from .model import ScanResult, Title, Stream

_RIP_ENV = ("FIZZDISK_RIP", "FIZZDISK_BACKEND", "MAKEMKVCON")  # last two are legacy aliases


def _find_backend() -> Optional[str]:
    r"""Locate the external console ripper, or return None.

    The app is fully self-contained (bundled ffmpeg + libbluray/libaacs +
    libdvdcss + the bundled console ripper), so the ripper is never required.
    Priority:
      1. the bundled copy shipped with the app (``binizzdisk-rip.exe``);
      2. FIZZDISK_RIP / FIZZDISK_BACKEND / MAKEMKVCON env var (explicit path);
      3. ``fizzdisk-rip`` on PATH;
      4. a previously-installed ``makemkvcon64`` (legacy) on PATH;
      5. the legacy install location (Program Files\MakeMKV).
    Returns None ("no external backend") when none is found.
    """
    bundled = ffutil.find_tool("fizzdisk-rip")
    if bundled:
        return bundled
    for var in _RIP_ENV:
        loc = os.environ.get(var)
        if loc and os.path.exists(loc):
            return loc
    w = shutil.which("fizzdisk-rip") or shutil.which("makemkvcon64")
    if w:
        return w
    for var in ("ProgramFiles(x86)", "ProgramFiles", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        root = os.environ.get(var)
        if root:
            p = os.path.join(root, "MakeMKV", "makemkvcon64.exe")
            if os.path.exists(p):
                return p
    return None


RIPPER = _find_backend()


# ---------------------------------------------------------------------------
# drives / source detection
# ---------------------------------------------------------------------------
def list_optical_drives() -> List[str]:
    """Return drive letters (e.g. 'D:\\') that are CD/DVD/BD drives."""
    drives: List[str] = []
    drive_bits = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if drive_bits & (1 << i):
            letter = chr(65 + i)
            root = f"{letter}:\\"
            # GetDriveType: 2=removable, 5=CDROM
            dtype = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
            if dtype in (2, 5):
                drives.append(root)
    return drives


def _has(root: str, *sub) -> bool:
    return any((Path(root) / s).exists() for s in sub)


def detect_source(src: str) -> str:
    """Return 'file'|'dvd'|'bd'|'disc'|'iso'|'cd' for a source path."""
    if not src:
        return ""
    # drive letter first (D:, D:\ ...) -- a bare drive letter is a directory,
    # so it must be classified before the generic is_dir() branch below
    m = re.match(r"^([A-Za-z]):[\\/]?$", src.strip())
    if m:
        letter = m.group(1).upper()
        root = f"{letter}:\\"
        # a video disc always has a VIDEO_TS/BDMV filesystem -- check those
        # first so a DVD/BD is never mistaken for an audio CD (an MCI TOC read
        # on a DVD can report a phantom "audio" track with 0 sectors).
        if _has(root, "BDMV") or _has(root, "AACS"):
            return "bd"
        if _has(root, "VIDEO_TS"):
            return "dvd"
        # a pure audio CD has no video folders
        try:
            if cdda.detect_audio_cd(letter):
                return "cd"
        except Exception:
            pass
        return "disc"
    path = Path(src)
    # directory scan
    if path.is_dir():
        if (path / "VIDEO_TS" / "VIDEO_TS.IFO").exists() or (path / "VIDEO_TS.IFO").exists():
            return "dvd"
        if (path / "BDMV" / "PLAYLIST").exists() or (path / "PLAYLIST").exists():
            return "bd"
        return "dvd" if (path / "BDMV").exists() else "file"
    # file
    low = src.lower()
    if low.endswith(".iso"):
        return "iso"
    return "file"


# ---------------------------------------------------------------------------
# ISO mount
# ---------------------------------------------------------------------------
def mount_iso(iso_path: str) -> Optional[str]:
    """Mount an ISO via PowerShell and return its drive letter (or None)."""
    cmd = ["powershell", "-NoProfile", "-Command",
           f"(Mount-DiskImage -ImagePath '{iso_path.replace(chr(39), chr(39)*2)}' -PassThru).DevicePath"]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
    dev = r.stdout.strip()
    if not dev:
        return None
    # DevicePath like "\\?\Volume{...}\" -> map to drive letter
    cmd2 = ["powershell", "-NoProfile", "-Command",
            "(Get-Volume -DiskImage (Get-DiskImage -ImagePath '%s')).DriveLetter" % iso_path.replace(chr(39), chr(39) * 2)]
    r2 = subprocess.run(cmd2, capture_output=True, text=True, creationflags=_NO_WINDOW)
    letter = r2.stdout.strip()
    return f"{letter}:\\" if letter else None


def dismount_iso(iso_path: str) -> None:
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"(Get-DiskImage -ImagePath '{iso_path.replace(chr(39), chr(39)*2)}' | Dismount-DiskImage)"],
                   capture_output=True, text=True, creationflags=_NO_WINDOW)


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------
def scan_source(src: str, mounted_iso: bool = False) -> ScanResult:
    """Detect source kind and return a ScanResult of titles."""
    kind = detect_source(src)
    if kind == "dvd":
        titles = dvdscan.scan_dvd_folder(src)
        info = f"DVD: {len(titles)} title(s)"
        meta = None
    elif kind in ("bd", "disc"):
        titles = _scan_disc(src)
        info = f"Disc: {len(titles)} title(s)"
        meta = None
    elif kind == "cd":
        titles, meta = scan_cd(src)
        info = f"Audio CD: {len(titles)} track(s)"
    elif kind == "iso":
        titles, _mountpt = _scan_iso(src)
        info = f"ISO: {len(titles)} title(s)"
        meta = None
    else:
        titles = _scan_file(src)
        info = f"File: {len(titles)} title(s)"
        meta = None
    res = ScanResult(titles=titles, source=src, source_kind=kind, info=info)
    res.meta = meta
    return res


def _cd_letter(src: str) -> str:
    m = re.match(r"^([A-Za-z]):", src.strip())
    if m:
        return m.group(1).upper()
    s = (src or "").strip().rstrip(":\\/")
    return s[:1].upper() if s else "D"


def scan_cd(src: str):
    """Scan an audio CD. Returns (titles, meta); one Title per audio track."""
    letter = _cd_letter(src)
    tracks = cdda.scan_toc(letter)
    meta = musicdisc.identify_disc(tracks) or {}
    title_by_pos = {t.get("position"): t.get("title")
                    for t in (meta.get("tracks") or [])}
    titles: List[Title] = []
    for t in tracks:
        if not t.get("track") or not t.get("is_audio"):
            continue
        num = int(t["track"])
        raw_title = title_by_pos.get(num) or f"Track {num:02d}"
        name = f"{num:02d} - {raw_title}"
        title = Title(index=len(titles), name=name, clips=[],
                      duration=round((t.get("sectors") or 0) / 75.0, 3),
                      source_kind="cd",
                      size_bytes=(t.get("sectors") or 0) * cdda.CDDA_SECTOR)
        title.is_main = (len(titles) == 0)
        title.info = {"cd_track": t, "cd_number": num, "cd_title": raw_title}
        titles.append(title)
    return titles, meta


def rip_cd(letter: str, cd_titles: List[Title], out_root: str, fmt: str = "flac",
           artist: str = "Unknown Artist", album: str = "Unknown Album",
           release_mbid: Optional[str] = None, selected: Optional[set] = None,
           progress_cb: Optional[Callable[[float], None]] = None,
           stop_event: Optional[threading.Event] = None,
           formats: Optional[list] = None) -> List[str]:
    """Rip selected CD audio tracks to FLAC/MP3 directly into `out_root`.

    `out_root` is the exact album folder (e.g. <Music>/<Artist>/<Album>) -- the
    caller embeds Artist/Album. Writes cover.jpg for the album and returns the
    list of produced file paths. `formats` may be a list to produce both FLAC
    and MP3 (one of each requested format per selected track).
    """
    import tempfile
    album_dir = out_root
    os.makedirs(album_dir, exist_ok=True)
    fmts = [f.lower() for f in (list(formats) if formats else [fmt or "flac"])]
    if not fmts:
        fmts = ["flac"]
    cover_path = os.path.join(album_dir, "cover.jpg")
    if release_mbid:
        try:
            musicdisc.download_cover(release_mbid, cover_path, size="500")
        except Exception:  # noqa: BLE001
            pass
    if not Path(cover_path).exists():
        cover_path = None
    produced: List[str] = []
    jobs = [t for t in cd_titles if selected is None or t.index in selected]
    total = max(1, len(jobs))

    def _emit(i: int, f: float):
        if progress_cb:
            progress_cb(max(0.0, min(1.0, (i + max(0.0, min(1.0, f))) / total)))

    for i, title in enumerate(jobs):
        if stop_event and stop_event.is_set():
            break
        track = title.info.get("cd_track")
        if not track:
            continue
        num = int(title.info.get("cd_number") or track.get("track") or 0)
        clean = title.info.get("cd_title") or ffutil.safe_name(title.name)
        meta = {"title": clean, "artist": artist, "album": album,
                "track": f"{num:02d}" if num else num}
        wav = tempfile.mktemp(suffix=".wav", prefix="fizzdisk_")
        try:
            _emit(i, 0.0)
            cdda.rip_track_to_wav(letter, track, wav, progress_cb=lambda f, i=i: _emit(i, f))
            for j, f in enumerate(fmts):
                ext = ".flac" if f == "flac" else ".mp3"
                out_path = os.path.join(album_dir, ffutil.safe_name(title.name) + ext)
                # scale per-format encode progress into this track's share
                def cb(frac, i=i, j=j, n=len(fmts)):
                    _emit(i, (j + max(0.0, min(1.0, frac))) / n)
                rc = ffutil.encode_audio(wav, out_path, fmt=f,
                                         progress_cb=cb,
                                         duration=title.duration or 0.0,
                                         meta=meta, cover=cover_path)
                if rc != 0:
                    raise RuntimeError(f"Encoding failed (rc={rc}) for {title.name} [{f}]")
                produced.append(out_path)
        finally:
            try:
                os.remove(wav)
            except OSError:
                pass
    return produced


def _scan_iso(src: str):
    """Mount an ISO, scan it, and dismount. Returns (titles, mount_point)."""
    mount = mount_iso(src)
    if not mount:
        raise ValueError(f"Could not mount ISO {src}")
    kind = detect_source(mount)
    if kind == "dvd":
        titles = dvdscan.scan_dvd_folder(mount)
    elif kind in ("bd", "disc"):
        titles = _scan_disc(mount)
    else:
        titles = _scan_file(mount)
    return titles, mount


_MEDIA_EXT = {".m2ts", ".mts", ".vob", ".mkv", ".mp4", ".m4v", ".mov", ".avi",
              ".ts", ".mpg", ".mpeg", ".wmv", ".flv", ".rmvb", ".webm"}


def _media_files(base) -> list:
    try:
        return [str(f) for f in sorted(Path(base).iterdir())
                if f.is_file() and f.suffix.lower() in _MEDIA_EXT]
    except Exception:  # noqa: BLE001
        return []


def _scan_file(path: str) -> List[Title]:
    """Scan a media file (or a directory of media files)."""
    p = Path(path)
    media = _media_files(p) if p.is_dir() else None
    if media is not None and not media:
        # a directory with no playable media (e.g. an empty drive root)
        return []
    if media is not None:
        files = media
        name = p.name or "disc"
    else:
        files = [path]
        name = Path(path).stem
    info = ffutil.scan_streams(files)
    title = Title(index=0, name=name, clips=[ffutil.Clip(files=files)],
                  duration=info.get("duration", 0.0), source_kind="file", is_main=True)
    for s in info.get("streams", []):
        if s.codec_type == "video" and title.video is None:
            title.video = s
        elif s.codec_type == "audio":
            title.audio.append(s)
        elif s.codec_type == "subtitle":
            title.subtitles.append(s)
    title.info = info
    return [title]


def _scan_disc(src: str) -> List[Title]:
    """Best-effort disc scan.

    Fast path: for a BD source, probe a single m2ts clip. If it is readable
    (unencrypted) use our own ffmpeg engine; otherwise (AACS-protected) fall
    back to the bundled open-source decoder. DVD sources use our IFO parser.
    """
    try:
        if _looks_like_bd(src):
            base = _bd_base(src)
            # AACS-protected Blu-ray: decrypt with the bundled, self-contained
            # open-source stack (libbluray + libaacs + libbdplus + keydb.cfg).
            # This reproduces the title list and durations without needing the
            # optional external ripper.
            if _is_aacs_protected(base):
                try:
                    return bdlib.scan_disc(src)
                except Exception:
                    pass   # libbluray failed -> fall back to the external demuxer
            first = _first_m2ts(base)
            if first and _probe_has_video(first):
                return bdscan.scan_bd_folder(src)
            # else encrypted -> external demuxer below
        if _looks_like_dvd(src):
            return dvdscan.scan_dvd_folder(src)
    except Exception:
        pass
    if RIPPER and Path(RIPPER).exists():
        return scan_titles(src)
    raise ValueError(f"Could not scan {src}")


def _is_aacs_protected(base: Path) -> bool:
    """A disc is AACS-protected if it carries an AACS key folder (with files)."""
    aacs = base / "AACS"
    if not aacs.is_dir():
        return False
    try:
        return any(aacs.iterdir())
    except OSError:
        return False


def _bd_base(src: str) -> Path:
    p = Path(src)
    if (p / "BDMV").is_dir():
        return p
    if (p / "STREAM").is_dir():
        return p.parent
    m = re.match(r"^([A-Za-z]):[\\/]?$", src)
    if m:
        return Path(f"{m.group(1)}:\\")
    return p


def _first_m2ts(base: Path) -> Optional[Path]:
    streams = sorted((base / "BDMV" / "STREAM").glob("*.m2ts"))
    return streams[0] if streams else None


def _probe_has_video(m2ts: Path) -> bool:
    try:
        data = ffutil.ffprobe_json([m2ts.as_posix()])
    except Exception:
        return False
    return any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
               for s in data.get("streams", []))


def _looks_like_bd(src: str) -> bool:
    p = Path(src)
    try:
        if (p / "BDMV" / "PLAYLIST").is_dir() or (p / "PLAYLIST").is_dir():
            return True
        if p.suffix.lower() == ".iso":
            return False
    except Exception:
        pass
    m = re.match(r"^([A-Za-z]):[\\/]?$", src)
    if m and (Path(f"{m.group(1)}:\\") / "BDMV").is_dir():
        return True
    return False


def _looks_like_dvd(src: str) -> bool:
    p = Path(src)
    try:
        if (p / "VIDEO_TS" / "VIDEO_TS.IFO").exists() or (p / "VIDEO_TS.IFO").exists():
            return True
    except Exception:
        pass
    m = re.match(r"^([A-Za-z]):[\\/]?$", src)
    if m and (Path(f"{m.group(1)}:\\") / "VIDEO_TS").is_dir():
        return True
    return False


# ---------------------------------------------------------------------------
# External demuxer backend (for protected / non-ffmpeg-readable discs)
# ---------------------------------------------------------------------------
def _run_backend(args: List[str], timeout: Optional[int] = None) -> str:
    r = subprocess.run([RIPPER, "-r", *args], capture_output=True,
                       text=True, timeout=timeout or 7200, creationflags=_NO_WINDOW)
    return r.stdout + r.stderr


def scan_titles(src: str, min_seconds: float = 120.0) -> List[Title]:
    """Use the external console ripper (-r info) to enumerate titles on a disc/ISO."""
    arg = f"disc:0" if detect_source(src) in ("disc", "bd") and _drive_arg(src) else _source_arg(src)
    out = _run_backend(["info", arg])
    titles: List[Title] = []
    title_ids: List[int] = []
    # Titles are announced by MSG lines? Actually the title list is in the
    # TINFO/`TCOUNT` block from the info output. We parse per-title blocks that
    # look like: "TINFO:<i>,<titleid>,<field>,<value>". Simpler: the default
    # `info` prints TSV of titles when not -r; but -r gives TNAME/TINFO.
    # We also tolerate the human-readable list for robustness.
    lines = out.splitlines()
    # Title headers appear as lines starting with TCOUNT then per title TINFO.
    # Each title entry: a TINFO line with field 1 = name? We use field 1 as
    # the description and field 3 as duration.
    # -r information: title lines look like:
    #   TINFO:0,<titleid>,0,<duration> ... etc.
    # We group by titleid (index 1 in TINFO under -r) — but the exact schema:
    #   TCOUNT:N  then per title: TINFO:<dummy>,<title>,<key>,<value>.
    # We parse the human-readable `info` (non -r) fallback below.
    if not titles:
        titles = _parse_backend_info(out, min_seconds)
    return titles


def _source_arg(src: str) -> str:
    low = src.lower()
    if low.endswith(".iso"):
        return f"iso:{src}"
    return f"file:{src}"


def _drive_arg(src: str) -> Optional[str]:
    m = re.match(r"^([A-Za-z]):", src)
    if m:
        return m.group(1).upper()
    return None


def _parse_backend_info(out: str, min_seconds: float) -> List[Title]:
    """Parse the external backend's ``-r info`` output into Title objects.

    TINFO lines are CSV:  TINFO:<title_id>,<field_id>,<unused>,"value"
    Field 9 = duration (HH:MM:SS), field 2 = name, field 27 = output file name.
    SINFO lines carry the per-title stream list:
      SINFO:<title_id>,<stream_index>,<field_id>,<unused>,"value"
      field 1 = type (6201=video, 6202=audio, 6203=subtitles), field 4 = language
      name, field 5 = codec id, field 7 = long codec name, field 14 = channels.
    """
    import csv
    import io
    titles: List[Title] = []
    by_id: dict = {}
    streams: dict = {}   # title_id -> {stream_index: {field_id: value}}
    for line in out.splitlines():
        if line.startswith("SINFO:"):
            try:
                row = next(csv.reader(io.StringIO(line[len("SINFO:"):]), skipinitialspace=True))
            except Exception:
                continue
            if len(row) < 5:
                continue
            tid, sidx, field = row[0], row[1], row[2]
            val = row[4]
            streams.setdefault(tid, {}).setdefault(sidx, {})[field] = val
            continue
        if not line.startswith("TINFO:"):
            continue
        try:
            row = next(csv.reader(io.StringIO(line[len("TINFO:"):]), skipinitialspace=True))
        except Exception:
            continue
        if len(row) < 3:
            continue
        tid = row[0]
        fid = row[1]
        val = row[3] if len(row) > 3 else ""
        by_id.setdefault(tid, {})
        by_id[tid][fid] = val
    for tid in sorted(by_id, key=lambda s: int(s) if s.isdigit() else 999999):
        rec = by_id[tid]
        name = rec.get("27") or rec.get("2") or f"Title {int(tid) + 1}"
        dur = 0.0
        m = re.match(r"(\d+):(\d+):(\d+)", rec.get("9", ""))
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        if dur and dur < min_seconds:
            continue
        # TINFO carries the title's source size in bytes as the largest integer.
        size_b = 0
        for v in rec.values():
            vs = v.strip()
            if vs.isdigit() and len(vs) >= 7:  # > 1,000,000 => bytes
                size_b = max(size_b, int(vs))
        title = Title(index=int(tid), name=name.strip(), clips=[],
                      duration=dur, source_kind="bd", size_bytes=size_b)
        # Attach the streams the backend reports (so the Streams pane lists the
        # actual audio/subtitle tracks instead of the fallback note).
        stblk = streams.get(tid)
        if stblk:
            for sidx in sorted(stblk, key=lambda s: int(s) if s.isdigit() else 999999):
                fs = stblk[sidx]
                ftype = fs.get("1", "")
                codec = fs.get("5", "")
                if ftype == "6201" or codec.startswith("V_"):
                    continue                       # video track (not selectable)
                if ftype == "6202" or codec.startswith("A_"):
                    title.audio.append(_stream_from_backend("audio", fs, len(title.audio)))
                elif ftype == "6203" or codec.startswith("S_"):
                    title.subtitles.append(_stream_from_backend("subtitle", fs, len(title.subtitles)))
        titles.append(title)
    return titles


def _stream_from_backend(codec_type: str, fs: dict, index: int) -> Stream:
    """Build a Stream from a backend SINFO field dict (for display/selection)."""
    long_name = (fs.get("7") or fs.get("6") or fs.get("5") or "").strip()
    channels = None
    try:
        if fs.get("14") and fs["14"].strip().isdigit():
            channels = int(fs["14"].strip())
    except Exception:
        channels = None
    lang = (fs.get("4") or fs.get("3") or "").strip() or None
    return Stream(index=index, codec_type=codec_type, codec_name=long_name,
                  channels=channels, language=lang)


def disc_rip(src: str, title_index: int, out_dir: str,
                progress_cb: Optional[Callable[[float], None]] = None,
                output_path: Optional[str] = None,
                total_bytes: float = 0.0) -> Optional[str]:
    """Rip title_index (0-based, as reported by the backend) to MKV in out_dir.

    progress_cb(fraction) is called with 0.0..1.0 as data is copied. Because
    the external ripper does not emit a reliable progress stream on this build,
    progress is measured from the growth of the output file (size / total_bytes). If
    total_bytes is unknown (0), only a "preparing" state is reported until the
    caller can compute a percentage; the transfer is never blocked.
    """
    if not RIPPER:
        return None
    arg = f"disc:0" if detect_source(src) in ("disc", "bd") and _drive_arg(src) else _source_arg(src)
    cmd = [RIPPER, "-r", "mkv", arg, str(title_index), out_dir]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=_NO_WINDOW)
    ffutil.set_active_proc(proc)

    def _drain():
        # Drain stdout/stderr; parse PRGV progress if a build emits it.
        try:
            for line in proc.stdout:
                if progress_cb and line.startswith("PRGV:"):
                    parts = line[len("PRGV:"):].split(",")
                    if len(parts) >= 3:
                        try:
                            tot = float(parts[1])
                            mx = float(parts[2])
                        except (ValueError, IndexError):
                            continue
                        if mx:
                            progress_cb(max(0.0, min(1.0, tot / mx)))
        except Exception:
            pass
        try:
            if proc.stderr:
                for _ in proc.stderr:
                    pass
        except Exception:
            pass

    t = threading.Thread(target=_drain, daemon=True)
    t.start()

    # Monitor growth of the output file for real progress. Prefer the exact
    # path passed in; if it doesn't exist (naming may differ), fall back to the
    # newest .mkv being written into out_dir.
    target = output_path
    last_size = -1.0
    while proc.poll() is None:
        if target is None or not os.path.exists(target):
            if Path(out_dir).exists():
                candidates = sorted(Path(out_dir).glob("*.mkv"), key=lambda p: p.stat().st_mtime)
                if candidates:
                    target = str(candidates[-1])
        if target and os.path.exists(target):
            try:
                size = float(os.path.getsize(target))
            except OSError:
                size = last_size
            if size != last_size:
                last_size = size
                if progress_cb and total_bytes and total_bytes > 0:
                    progress_cb(max(0.0, min(1.0, size / total_bytes)))
        time.sleep(0.5)
    proc.wait()
    t.join(timeout=3)
    ffutil.set_active_proc(None)

    dirs = [p for p in Path(out_dir).glob("*.mkv")] if Path(out_dir).exists() else []
    if dirs:
        return str(max(dirs, key=lambda p: p.stat().st_mtime))
    return None
