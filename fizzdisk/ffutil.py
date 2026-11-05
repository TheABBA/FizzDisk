# -*- coding: utf-8 -*-
"""Thin wrappers around ffprobe / ffmpeg.

All paths passed to ffmpeg/ffprobe must be native Windows paths (forward slashes
are fine; use C:/... not /c/...). The engine builds minimal temp concat list
files in the scratch dir to assemble multi-clip titles.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence

# Suppress a flashing console window when console executables (ffmpeg/ffprobe)
# are launched from a windowed (pythonw / PyInstaller) GUI app.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from .model import Clip, Stream, Title


# ---- active ripper process (for Stop / close) ------------------------------
# The currently running ffmpeg / external-ripper Popen is registered here so the GUI
# Stop button or the window-close handler can terminate it. Only one title is
# ripped at a time, so a single slot is sufficient.
_active_proc = None


def set_active_proc(proc):
    global _active_proc
    _active_proc = proc


def stop_active_proc():
    """Terminate the currently running ripper subprocess, if any."""
    if _active_proc:
        try:
            _active_proc.terminate()
        except Exception:
            pass
        return True
    return False


# ---- locate binaries -------------------------------------------------------
def find_tool(name: str) -> Optional[str]:
    """Return an absolute path to `name` (ffmpeg/ffprobe) incl. tool dir.

    Priority: the bundled `bin/<name>.exe` next to this package (self-contained
    app), then the `bin` dir of the source checkout, then the system PATH.
    """
    for base in (Path(__file__).resolve().parent.parent, Path(__file__).resolve().parent.parent.parent):
        candidate = os.path.join(base, "bin", name + ".exe")
        if Path(candidate).exists():
            return candidate
    exe = shutil.which(name)
    if exe:
        return exe
    return None


FFMPEG = find_tool("ffmpeg") or "ffmpeg"
FFPROBE = find_tool("ffprobe") or "ffprobe"

# ffmpeg may need its DLLs on PATH (libaacs.dll etc.). Ensure we can see them.
_FFDIR = os.path.dirname(os.path.abspath(FFMPEG)) if os.path.isfile(FFMPEG) else ""
if _FFDIR and _FFDIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _FFDIR + os.pathsep + os.environ.get("PATH", "")


def _native(p) -> str:
    """Normalise a path for a native Windows binary.

    Accepts str OR pathlib.Path (callers pass both). str(p) is required: a
    Path uses ``Path.replace()`` (a 2-arg file-rename), NOT ``str.replace``,
    so calling ``p.replace('\\\\', '/')`` on a Path raised TypeError, which
    was silently swallowed and made _vts_stream_union return no streams.
    """
    return str(p).replace("\\", "/")


# How long to let a single ffprobe/ffmpeg invocation run before we give up.
# On a slow, flaky or scratched optical drive (or a disc still spinning up)
# libdvdread/libdvdcss can block for a very long time; without a cap the scan
# worker hangs forever and the "Which movie?" prompt never appears. We kill
# the child on expiry so no orphaned ffprobe processes pile up and hammer the
# drive, and the per-title probe is skipped (the caller falls back to probing
# the raw VOBs / shows the title without streams) instead of stalling the scan.
PROBE_TIMEOUT = 45


class ProbeTimeoutError(RuntimeError):
    """Raised when ffprobe did not finish within PROBE_TIMEOUT seconds.

    Distinct from a normal ffprobe failure so the DVD scan can bail out of
    probing further VOBs (a stuck drive will just keep timing out) rather
    than spend PROBE_TIMEOUT seconds on every single VOB."""


# Serialises EVERY ffprobe/ffmpeg invocation that touches an optical device.
#
# Two concurrent probes on ONE optical drive DEADLOCK libdvdnav/libdvdread:
# measured on a healthy drive with a healthy disc, a single `-f dvdvideo`
# probe returns in 3.5s, while three concurrent probes on the same drive ALL
# hang past 90s and leave children the OS cannot reap (taskkill reports
# "no running instance" while the process is still listed, because the thread
# is stuck in an uninterruptible kernel I/O wait on the device). The GUI's
# scan guard covers the scan path; this lock is the backstop for every other
# caller (rip, metadata, retry) so the invariant holds process-wide.
_DEVICE_LOCK = threading.Lock()


_RAW_DEV_RE    = re.compile(r"^\\\\[.?]\\[A-Za-z]:")
_BARE_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]*$")
_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/]")


def _drive_is_cdrom(letter: str) -> bool:
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 5  # DRIVE_CDROM
    except Exception:
        return False


def _is_optical(cmd: Sequence[str]) -> bool:
    """True if this command reads an optical device (drive letter / raw device).

    A VIDEO_TS folder copied to a hard disk is NOT optical and must not be
    serialised -- that would needlessly halve disk-copy fallback throughput.
    """
    for a in cmd:
        s = str(a)
        if _RAW_DEV_RE.match(s):          # \\.\D:
            return True
        if _BARE_DRIVE_RE.match(s):       # "D:"  or  "D:\"
            return True
        m = _DRIVE_PATH_RE.match(s)       # D:\VIDEO_TS\...
        if m and _drive_is_cdrom(m.group(1)):
            return True
    return False


def run(cmd: Sequence[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    if _is_optical(cmd):
        with _DEVICE_LOCK:
            return _run_inner(cmd, timeout)
    return _run_inner(cmd, timeout)


def _run_inner(cmd: Sequence[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    p = subprocess.Popen(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace",
                         creationflags=_NO_WINDOW)
    try:
        out, err = p.communicate(timeout=float(timeout) if timeout is not None else None)
    except subprocess.TimeoutExpired:
        # Wrapper timeouts do NOT reap the child -> it keeps running and
        # contends for the optical drive (we saw multiple orphaned ffprobe).
        p.kill()
        # A child wedged in UNINTERRUPTIBLE kernel I/O on a sick optical device
        # cannot be reaped at all: Windows reports HasExited=True while the
        # thread stays in wait=Executive and taskkill answers "no running
        # instance". communicate() would then block FOREVER draining pipes the
        # corpse still holds open -- that is what hung the scan worker (2
        # threads in Executive wait) and left the GUI with no movie prompt.
        # Drain on a daemon thread so an unreapable child can never pin us.
        box = {}

        def _drain():
            try:
                box["r"] = p.communicate()
            except Exception:
                box["r"] = ("", "")

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        t.join(10)
        out, err = box.get("r", ("", ""))
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


# ---- ffprobe ---------------------------------------------------------------
def ffprobe_json(input_args: List[str]) -> dict:
    """Run ffprobe returning parsed JSON dict. input_args excludes the -i."""
    cmd = [FFPROBE, "-v", "error", "-show_format", "-show_streams", "-show_chapters",
           "-of", "json", *input_args]
    try:
        r = run(cmd, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # A probe that never returns (flaky/slow drive, disc still spinning up,
        # bad sector) must fail OPEN rather than hang the scan forever.
        raise ProbeTimeoutError(
            f"ffprobe timed out after {PROBE_TIMEOUT}s (hanging on a slow, "
            f"flaky or bad optical read?)")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "ffprobe failed")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        # Empty/partial output (e.g. probe killed early) -> treat as no result.
        raise RuntimeError("ffprobe returned no usable output")


def probe_media_file(path: str) -> dict:
    if path.lower().endswith(".vob"):
        # DVD VOBs are MPEG-PS; ffmpeg cannot auto-detect them (and a concat
        # probe only survives the first program), so hint the demuxer.
        return ffprobe_json(["-f", "mpeg", _native(path)])
    return ffprobe_json([_native(path)])


def _write_concat_list(files: List[str], in_points: Optional[List[float]] = None,
                       out_points: Optional[List[float]] = None) -> str:
    """Build a concat demuxer list file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="fizzdisk_concat_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for i, file_ in enumerate(files):
            f.write(f"file '{_native(file_).replace(chr(39), chr(39)*2)}'\n")
            if in_points and i < len(in_points) and in_points[i] is not None:
                f.write(f"inpoint {in_points[i]}\n")
            if out_points and i < len(out_points) and out_points[i] is not None:
                f.write(f"outpoint {out_points[i]}\n")
    return path


def probe_concat(files: List[str], in_points=None, out_points=None) -> dict:
    """ffprobe over a concat list; returns JSON dict."""
    list_path = _write_concat_list(files, in_points, out_points)
    try:
        return ffprobe_json(["-f", "concat", "-safe", "0", "-i", _native(list_path)])
    finally:
        _safe_unlink(list_path)


def _safe_unlink(p: str) -> None:
    try:
        if p and os.path.exists(p):
            os.unlink(p)
    except OSError:
        pass


def _dir_size(p: str) -> int:
    """Sum the byte size of every file under `p` (used for copy progress)."""
    n = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    n += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return n


def _dvd_copy_complete(dst: str, expected: dict) -> bool:
    """True only if `dst/VIDEO_TS` holds EVERY expected file at the right size.

    A copy that stops part-way can still contain VIDEO_TS.IFO (which is copied
    first), so checking just the IFO is not enough -- we require the whole tree
    to be present and byte-for-byte sized. `expected` maps filename -> size.
    """
    if not os.path.isfile(os.path.join(dst, "VIDEO_TS.IFO")):
        return False
    if not expected:
        return True
    try:
        for name, size in expected.items():
            p = os.path.join(dst, name)
            if not os.path.isfile(p) or os.path.getsize(p) != size:
                return False
    except OSError:
        return False
    return True


def _copy_dvd_to_disk(drive_dir: str,
                      on_progress: Optional[Callable[[float], None]] = None,
                      stop_event: Optional[threading.Event] = None) -> Optional[str]:
    """Copy a physical DVD's VIDEO_TS folder to a fresh temp dir.

    libdvdnav navigating the LIVE optical drive can abort on a disc whose cell
    navigation state is fragile (it demuxes everything before the bad NAV packet
    then exits 0 -- a silent truncation). Copying the disc's file tree to disk
    yields a stable source: a raw file copy does not need NAV packets, and the
    bundled libdvdcss still descrambles the VOBs.

    `on_progress` (if given) is called with a 0..1 fraction of bytes copied so
    the caller can show motion + time remaining during the copy.

    Returns the temp PARENT dir (the one containing a VIDEO_TS subfolder, safe to
    pass to ``-f dvdvideo -i``), or None if the copy failed. The caller is
    responsible for removing the returned dir when done.
    """
    if not drive_dir:
        return None
    src = os.path.join(drive_dir, "VIDEO_TS")
    if not os.path.isdir(src):
        return None
    # Snapshot the exact file set + sizes we must end up with, so we can tell a
    # complete copy from a partial one (a partial tree still contains the IFO).
    expected: dict = {}
    try:
        for f in os.scandir(src):
            if f.is_file():
                expected[f.name] = f.stat().st_size
    except OSError:
        expected = {}
    total = _dir_size(src) or 1
    base = tempfile.mkdtemp(prefix="fizzdisk_dvfolder_")
    dst = os.path.join(base, "VIDEO_TS")

    def _report():
        if on_progress and total:
            try:
                on_progress(min(1.0, _dir_size(dst) / total))
            except Exception:
                pass

    def _run_copy(retries: str) -> bool:
        # Bounded retries (NOT robocopy's default /R:1000000 /W:30, which hangs
        # forever on a bad sector and gets killed by the timeout half-way,
        # leaving a partial tree). /Z enables restartable copy for the rare
        # re-try pass. We do NOT trust the exit code alone -- we verify the tree.
        try:
            proc = subprocess.Popen(
                ["robocopy", src, dst, "/E", "/R:" + retries, "/W:2", "/Z",
                 "/NFL", "/NDL", "/NJH", "/NJS"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW)
            start = time.time()
            while True:
                try:
                    proc.wait(timeout=0.6)
                    break
                except subprocess.TimeoutExpired:
                    if stop_event and stop_event.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
                        break
                    if time.time() - start > 1800:
                        proc.terminate()
                        proc.wait()
                        break
                    _report()
        except Exception:
            return False
        _report()   # report final state after the copy finishes
        return _dvd_copy_complete(dst, expected)

    # Try the copy, then a second pass if the first came up short.
    ok = _run_copy("8")
    if not ok:
        shutil.rmtree(dst, ignore_errors=True)   # clear partials before retry
        ok = _run_copy("16")
    if not ok:
        # Last resort: plain shutil copy (honors no retries, but a clean copy).
        try:
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
            ok = _dvd_copy_complete(dst, expected)
        except Exception:
            ok = False
    if not ok:
        shutil.rmtree(base, ignore_errors=True)
        return None
    return base



def _parse_time(pts: Optional[str]) -> Optional[float]:
    if pts is None:
        return None
    try:
        return float(pts)
    except (TypeError, ValueError):
        return None


def _lang_from_tags(tags: dict) -> Optional[str]:
    if not tags:
        return None
    for k in ("language", "LANG"):
        if k in tags and tags[k]:
            return tags[k]
    return None


def _streams_from_probe(data: dict) -> List[Stream]:
    """Convert ffprobe stream dicts into our Stream model (indices as-is)."""
    out: List[Stream] = []
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and st.get("disposition", {}).get("attached_pic"):
            continue
        codec_type = st.get("codec_type", "data")
        mapping = {"video": "video", "audio": "audio", "subtitle": "subtitle"}
        codec_type = mapping.get(codec_type, "data")
        disp = st.get("disposition", {}) or {}
        lang = _lang_from_tags(st.get("tags") or None)
        titre = (st.get("tags") or {}).get("title")
        out.append(Stream(
            index=int(st.get("index", 0)),
            codec_type=codec_type,
            codec_name=st.get("codec_name", ""),
            channels=int(st.get("channels") or 0) or None,
            language=lang,
            title=titre,
            default=bool(disp.get("default")),
            forced=bool(disp.get("forced")),
            disposition=disp,
            id=st.get("id"),
        ))
    return out


def scan_streams(files: List[str]) -> dict:
    """Return {'duration','streams':[Stream],'chapters':[...]} for a clip set.

    Probes each file individually (robust for VOB/m2ts where a concat probe does
    not expose a format duration), sums durations, and takes the stream layout
    from the first file that probes successfully. Falls back to a single concat
    probe if individual probing fails (e.g. timing-sensitive containers).
    """
    if len(files) == 1:
        try:
            data = probe_media_file(files[0])
        except Exception:
            data = probe_concat(files)
    else:
        streams: List[Stream] = []
        raw = None
        duration = 0.0
        for f in files:
            try:
                if f.lower().endswith(".vob"):
                    d = ffprobe_json(["-f", "mpeg", _native(f)])
                else:
                    d = ffprobe_json([_native(f)])
            except Exception:
                continue
            dur = _parse_time((d.get("format") or {}).get("duration"))
            if dur:
                duration += dur
            if raw is None:
                raw = d
        if raw is not None:
            streams = _streams_from_probe(raw)
            return {"duration": duration, "streams": streams, "chapters": [], "raw": raw}
        # fall back to concat
        data = probe_concat(files)
    duration = _parse_time((data.get("format") or {}).get("duration"))
    streams = _streams_from_probe(data)
    chapters = []
    for ch in data.get("chapters", []):
        tags = ch.get("tags") or {}
        chapters.append({
            "start": _parse_time(ch.get("start_time")),
            "end": _parse_time(ch.get("end_time")),
            "title": tags.get("title", ""),
        })
    return {"duration": duration or 0.0, "streams": streams, "chapters": chapters,
            "raw": data}


def build_title_from_streams(idx: int, name: str, clips: List[Clip], info: dict,
                             source_kind: str, is_main: bool = False) -> Title:
    """Assemble a Title from a scan result."""
    title = Title(index=idx, name=name, clips=clips, duration=info.get("duration", 0.0),
                  source_kind=source_kind, is_main=is_main,
                  chapters=info.get("chapters", []),
                  raw_streams=info.get("raw", {}).get("streams", []))
    for s in info.get("streams", []):
        if s.codec_type == "video" and title.video is None:
            title.video = s
        elif s.codec_type == "audio":
            title.audio.append(s)
        elif s.codec_type == "subtitle":
            title.subtitles.append(s)
        elif s.codec_type == "data":
            title.data.append(s)
    title.info = info
    return title


# ---- ffmpeg remux ----------------------------------------------------------
MKV_SAFE_AUDIO = {
    "ac3", "eac3", "dts", "truehd", "mlp", "aac", "mp3", "mp2", "flac",
    "opus", "vorbis", "alac", "pcm_s16le", "pcm_s24le",
}


def copy_audio_flags(title: "Title", audio_idx: Optional[List[int]],
                     real_codecs: Optional[List[str]] = None) -> List[str]:
    """FFmpeg args to copy the selected audio streams, transcoding anything
    Matroska cannot carry (LPCM / ``pcm_bluray`` above all) losslessly to FLAC.

    Matroska cannot carry a ``pcm_bluray`` track directly (``No wav codec tag
    found for codec pcm_bluray``) and refuses to write the MKV header, so any
    such stream must be re-encoded. FLAC is lossless, natively supported by
    Matroska, and preserves the audio bit-exactly.

    ``real_codecs`` is the codec list ffprobe reported for the RIP INPUT, in
    demuxer order. It is authoritative and must be preferred over the title's
    scan-time stream list: the two routinely disagree (a DVD's VOB
    program-stream union vs the selected PGC's real tracks, a Blu-ray
    playlist's declared table vs the TS libbluray actually delivers), and
    trusting the scan list applies ``copy`` to a track that is really LPCM,
    which kills the rip at header time with EINVAL (-22 / 4294967274).

    Decisions are made by WHITELIST: a codec is only stream-copied when it is
    known to be Matroska-safe, so an unfamiliar codec degrades to a lossless
    FLAC transcode instead of failing the whole rip.
    """
    real = [c.lower() for c in (real_codecs or [])]
    aud = list(audio_idx or [])
    if not aud:
        # No explicit selection — ffmpeg will map all audio itself, so the
        # codec decision has to cover every track the input exposes.
        codecs = real or [(a.codec_name or "").lower() for a in title.audio]
        if codecs and all(c in MKV_SAFE_AUDIO for c in codecs):
            return ["-c:a", "copy"]
        if not codecs:
            return ["-c:a", "copy"]
        return ["-c:a", "flac"]
    flags: List[str] = []
    for j, ai in enumerate(aud):
        cn = ""
        if 0 <= ai < len(real):
            cn = real[ai]
        else:
            try:
                cn = (title.audio[ai].codec_name or "").lower()
            except (IndexError, AttributeError):
                pass
        # Unknown/unreadable codec -> transcode; copying is the risky choice.
        flags += [f"-c:a:{j}", "copy" if cn in MKV_SAFE_AUDIO else "flac"]
    return flags


_INPUT_COUNT_CACHE: dict = {}


def _input_stream_counts(inputs: Sequence[str]) -> dict:
    """Count the video/audio/subtitle streams the RIP input actually exposes.

    Probes with the SAME demuxer flags the rip will use, because the counts
    differ per demuxer: for a DVD title the `-f dvdvideo` demuxer exposes only
    the selected PGC's streams while the scan's list is the VOB program-stream
    union of the whole VTS. Returns {} if it cannot tell (never guess -- an
    empty result means "do not clamp", so a probe failure can't silently drop
    tracks the user asked for).

    Cached per input: remux() may call this for the primary rip and again for
    the disc-copy re-rip, and the probe costs a few seconds of drive time.
    """
    key = tuple(str(x) for x in inputs)
    if key in _INPUT_COUNT_CACHE:
        return _INPUT_COUNT_CACHE[key]
    out: dict = {}
    try:
        data = ffprobe_json(list(inputs))
        counts: dict = {"video": 0, "audio": 0, "subtitle": 0}
        # Record the REAL codec of each audio stream, in demuxer order. The
        # title's scan-time stream list can disagree with the rip demuxer (a
        # DVD's VOB union vs the PGC's actual tracks), so choosing copy-vs-
        # transcode from the scan list can apply `copy` to a track that is in
        # fact LPCM -- which Matroska refuses at header time (EINVAL/-22).
        acodecs: List[str] = []
        for s in (data.get("streams") or []):
            t = s.get("codec_type")
            if t in counts:
                counts[t] += 1
            if t == "audio":
                acodecs.append((s.get("codec_name") or "").lower())
        counts["audio_codecs"] = acodecs
        out = counts
    except Exception as e:  # noqa: BLE001
        print(f"[remux] could not enumerate input streams ({e}); not clamping maps",
              flush=True)
        out = {}
    _INPUT_COUNT_CACHE[key] = out
    return out


def remux(title: Title, out_path: str, video: bool = True,
          audio_idx: Optional[List[int]] = None, sub_idx: Optional[List[int]] = None,
          copy_codecs: bool = True, video_codec: Optional[str] = None,
          audio_codec: Optional[str] = None, progress_cb: Optional[Callable[[dict], None]] = None,
          extra_map: Optional[List[int]] = None,
          stop_event: Optional[threading.Event] = None) -> int:
    """Remux a title to an MKV. Returns ffmpeg returncode (0 == success).

    audio_idx / sub_idx are ORDINALS within the title's audio / subtitle lists
    (0-based) that the user selected. video selects the video track.

    ``stop_event`` (if given) is checked during the rip and the disc-copy
    fallback; if set, ffmpeg/robocopy are killed and the rip aborts.
    """
    inputs: List[str] = []
    list_path = None
    dvd_title_no = (title.info or {}).get("dvd_title_no")
    dvd_dir = (title.info or {}).get("dvd_dir")
    is_drive_dvd = (title.source_kind == "dvd" and dvd_title_no and dvd_dir
                    and re.match(r"^[A-Za-z]:$", dvd_dir))
    if is_drive_dvd:
        # Physical DVD drive: use libdvdread's dvdvideo demuxer, which decrypts
        # CSS and navigates per-title, exactly like the external ripper. libdvdcss must be
        # handed the RAW DEVICE (the drive letter) so it can do the CSS key
        # exchange; the 'D:/VIDEO_TS' directory path makes it 0 frames.
        inputs = ["-f", "dvdvideo", "-title", str(dvd_title_no), "-i", _native(dvd_dir)]
    elif len(title.clips) == 1 and len(title.clips[0].files) == 1:
        src = title.clips[0].files[0]
        if title.clips[0].start is not None:
            inputs += ["-ss", f"{title.clips[0].start}"]
        if title.clips[0].end is not None:
            inputs += ["-t", f"{title.clips[0].end - (title.clips[0].start or 0)}"]
        if src.lower().endswith(".vob"):
            inputs += ["-f", "mpeg"]   # VOB = MPEG program stream; auto-detect fails
        inputs += ["-i", _native(src)]
    else:
        # Multi-clip: concat demuxer. Trimming per clip via inpoint/outpoint.
        all_files: List[str] = []
        in_pts: List[float] = []
        out_pts: List[float] = []
        for c in title.clips:
            for f in c.files:
                all_files.append(f)
                in_pts.append(c.start)
                out_pts.append(c.end)
        list_path = _write_concat_list(all_files, in_pts, out_pts)
        inputs += ["-f", "concat", "-safe", "0", "-i", _native(list_path)]

    def _run(inputs: List[str], out: str, dur: float,
             prog: Optional[Callable[[dict], None]] = None) -> int:
        """Build cmd from `inputs`, run ffmpeg, stream progress, return rc.

        `prog` overrides the default progress_cb (used to translate the re-rip's
        time-progress into a continuous overall fraction during recovery)."""
        cb = prog if prog is not None else progress_cb
        cmd = [FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "info", *inputs]
        # The scan's stream list and what the RIP demuxer exposes can disagree.
        # For a DVD the audio/subtitle lists come from the VOB program-stream
        # union (every PID in the VTS), but `-f dvdvideo` presents only the
        # streams of the selected title's PGC -- e.g. union says 4 audio, the
        # demuxer offers 3. A `-map 0:a:3` that matches nothing makes ffmpeg
        # abort INSTANTLY with "Stream map '' matches no streams" / rc=-22 and
        # zero bytes. remux() then read rc!=0 as a failed rip and launched the
        # disc-copy fallback, which rebuilt the same impossible map and failed
        # the same way -- an endless "copying the disc" loop on a disc that
        # rips perfectly. Clamp every requested ordinal to what this input
        # actually has, and never emit a map we know cannot resolve.
        a_sel, s_sel = list(audio_idx or []), list(sub_idx or [])
        avail = _input_stream_counts(inputs)
        real_acodecs: List[str] = []
        if avail:
            n_a, n_s = avail.get("audio", 0), avail.get("subtitle", 0)
            real_acodecs = list(avail.get("audio_codecs") or [])
            a_keep = [i for i in a_sel if i < n_a]
            s_keep = [i for i in s_sel if i < n_s]
            if len(a_keep) != len(a_sel) or len(s_keep) != len(s_sel):
                print(f"[remux] dropping unavailable maps: audio {a_sel}->{a_keep} "
                      f"(have {n_a}), subs {s_sel}->{s_keep} (have {n_s})", flush=True)
            a_sel, s_sel = a_keep, s_keep
        print("[remux] ffmpeg:", " ".join(cmd[:40]), flush=True)
        # maps. Every map carries a trailing '?' so that a stream which the
        # probe saw but this run does not expose (flaky optical read, a
        # fallback input built from a disc copy) is skipped instead of
        # aborting the entire rip at option-parse time with rc=-22.
        cmd += ["-map", "0:v:0?"] if video else []
        for i in a_sel:
            cmd += ["-map", f"0:a:{i}?"]
        for i in s_sel:
            cmd += ["-map", f"0:s:{i}?"]
        for i in (extra_map or []):
            cmd += ["-map", f"0:{i}?"]

        if copy_codecs and video_codec is None and video and audio_idx is None:
            # Blanket '-c copy' is only safe when every audio track the input
            # exposes is a codec Matroska can carry; an LPCM track makes the
            # muxer reject the header before any frame is written.
            if real_acodecs and not all(c in MKV_SAFE_AUDIO for c in real_acodecs):
                cmd += ["-c:v", "copy", "-c:s", "copy"]
                for j, c in enumerate(real_acodecs):
                    cmd += [f"-c:a:{j}", "copy" if c in MKV_SAFE_AUDIO else "flac"]
            else:
                cmd += ["-c", "copy"]
        elif copy_codecs:
            cmd += ["-c:v", "copy" if video and not video_codec else (video_codec or "copy")]
            if audio_codec is None:
                cmd += copy_audio_flags(title, audio_idx, real_acodecs)
            else:
                cmd += ["-c:a", audio_codec]
        else:
            vc = video_codec or "libx264"
            cmd += ["-c:v", vc]
            if vc == "libx264":
                cmd += ["-preset", "fast", "-crf", "20"]
            cmd += ["-c:a", audio_codec or "aac", "-b:a", "192k"]

        cmd += ["-c:s", "copy"]
        cmd += ["-map_metadata", "0"]
        cmd += ["-default_mode", "infer", "-metadata", f"title={title.name}"]
        cmd += [_native(out)]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=_NO_WINDOW)
        set_active_proc(proc)
        last_time = 0.0
        try:
            for line in proc.stderr:
                if stop_event and stop_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                    break
                line = line.strip()
                if line.startswith("frame=") or "time=" in line:
                    m = re.search(r"time=(\d+):(\d+):(\d+)\.(\d+)", line)
                    if m:
                        t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + float(m.group(4)) / 10
                        if cb and t != last_time:
                            last_time = t
                            cb({"time": t, "duration": dur})
                elif line and "Duplicate" not in line and "Press" not in line:
                    if "Error" in line or "Invalid" in line or "failed" in line.lower():
                        print("[remux] ERR>", line, flush=True)
            rc = proc.wait()
        finally:
            set_active_proc(None)
        return rc

    expected_dur = title.duration or 0.0
    rc = _run(inputs, out_path, expected_dur)
    setattr(title, "_remux_used_disk_copy", False)
    setattr(title, "_remux_truncated", False)

    # A physical drive DVD can fail while reading the LIVE optical drive:
    #   * libdvdnav navigating the device can hit a cell with no NAV packet and
    #     abort the demux yet ffmpeg exits 0 (a silent truncation), or
    #   * the device itself can fail to open (libdvdcss needing a real raw device).
    # The same bytes, copied to a temp VIDEO_TS folder, open and navigate cleanly
    # (a raw file copy doesn't need NAV packets, and bundled libdvdcss still
    # descrambles). So for drive DVDs, if the primary rip failed OR produced a
    # much-too-short file, re-rip once from a disk copy.
    if is_drive_dvd and expected_dur > 0:
        info = validate_mkv(out_path)
        out_dur = info.get("duration") or 0.0
        if info.get("probe_failed"):
            # We could NOT measure the output (ffprobe missing -- usually AV
            # deleted it -- or the probe could not run). This says nothing
            # about the rip, so do NOT trigger the disc-copy fallback: that
            # re-rips, fails the same verification, and loops forever while
            # reporting a perfectly good rip as truncated. Surface the real
            # problem instead and keep the rip we have.
            setattr(title, "_remux_verify_failed", True)
            setattr(title, "_remux_verify_error", info.get("error") or "probe failed")
            print(f"[remux] cannot verify output ({info.get('error')}) -- "
                  f"keeping rip, NOT running disc-copy fallback", flush=True)
            if progress_cb:
                try:
                    progress_cb({"event": "verify_unavailable",
                                 "error": info.get("error") or "probe failed"})
                except Exception:
                    pass
            _safe_unlink(list_path)
            return rc
        truncated = (rc != 0) or (not info.get("ok")) or \
                    (out_dur > 0 and out_dur < expected_dur * 0.90)
        if truncated:
            print(f"[remux] drive rip failed/truncated (rc={rc}, got {out_dur:.1f}s "
                  f"vs expected {expected_dur:.1f}s) -> re-ripping from a disc copy",
                  flush=True)
            # Tell the UI the rip is entering the slow recovery path (disc copy
            # + second rip pass) so the user knows why it will take longer.
            if progress_cb:
                try:
                    progress_cb({"event": "disk_copy_fallback",
                                 "got": out_dur, "expected": expected_dur})
                except Exception:
                    pass
            # The recovery = disc copy + re-rip. Show the copy as the first part
            # of the progress (COPY_WEIGHT of the bar) and continue the re-rip
            # monotonically from there, so the bar moves and the ETA counts down
            # throughout instead of stalling during the copy.
            COPY_WEIGHT = 0.30

            def _copy_prog(f: float) -> None:
                # f is 0..1 bytes copied; map onto the first COPY_WEIGHT portion.
                if progress_cb:
                    try:
                        progress_cb({"fraction": COPY_WEIGHT * min(1.0, f)})
                    except Exception:
                        pass

            def _fb_prog(d: dict) -> None:
                # d has time/duration from ffmpeg; map onto the remaining portion
                # so the overall progress is continuous with the copy phase.
                frac = min(1.0, (d.get("time") or 0.0) / max(1.0, d.get("duration") or 1.0))
                if progress_cb:
                    try:
                        progress_cb({"fraction": COPY_WEIGHT + (1.0 - COPY_WEIGHT) * frac})
                    except Exception:
                        pass

            dvd_base = _copy_dvd_to_disk(dvd_dir, on_progress=_copy_prog,
                                        stop_event=stop_event) if dvd_dir else None
            if dvd_base:
                if stop_event and stop_event.is_set():
                    shutil.rmtree(dvd_base, ignore_errors=True)
                    print("[remux] stopped during disc-copy fallback — aborting", flush=True)
                    _safe_unlink(list_path)
                    return rc
                try:
                    fb_inputs = ["-analyzeduration", "20000000", "-probesize", "20000000",
                                 "-f", "dvdvideo", "-title", str(dvd_title_no),
                                 "-i", _native(dvd_base)]
                    rc2 = _run(fb_inputs, out_path, expected_dur, prog=_fb_prog)
                    if rc2 == 0:
                        # Verify the re-rip is actually the full length; a bad or
                        # partial disk copy can still "succeed" (ffmpeg exits 0)
                        # with a short output, which must NOT be reported as done.
                        info2 = validate_mkv(out_path)
                        dur2 = info2.get("duration") or 0.0
                        if dur2 > 0 and dur2 < expected_dur * 0.90:
                            print(f"[remux] disk-copy re-rip still short "
                                  f"({dur2:.1f}s vs {expected_dur:.1f}s) \u2014 keeping "
                                  f"problematic rip", flush=True)
                            setattr(title, "_remux_truncated", True)
                        else:
                            rc = rc2
                            setattr(title, "_remux_used_disk_copy", True)
                            print("[remux] disk-copy re-rip complete", flush=True)
                finally:
                    shutil.rmtree(dvd_base, ignore_errors=True)
            else:
                print("[remux] could not copy disc to disk; keeping problematic rip", flush=True)
                setattr(title, "_remux_truncated", True)

    _safe_unlink(list_path)
    return rc


def validate_mkv(path: str) -> dict:
    """Probe an output MKV; return {duration, streams:[Stream], ok, probe_failed}.

    ``ok=False`` means *this file is bad*. ``probe_failed=True`` means we could
    not MEASURE the file at all (ffprobe missing/killed/unrunnable) and the
    result says NOTHING about the rip. Callers MUST NOT treat a failed probe as
    a short rip: when AV deletes ffprobe.exe the rip itself is perfect, but
    conflating the two sends the user into an endless disc-copy fallback loop
    and reports a complete rip as truncated.
    """
    if not os.path.isfile(FFPROBE):
        return {"duration": 0.0, "streams": [], "ok": False, "probe_failed": True,
                "error": f"ffprobe is missing: {FFPROBE}"}
    try:
        data = ffprobe_json([_native(path)])
    except ProbeTimeoutError as e:
        # The probe could not COMPLETE (hung drive) -> unmeasurable, not short.
        return {"duration": 0.0, "streams": [], "ok": False, "probe_failed": True,
                "error": str(e)}
    except FileNotFoundError as e:
        # ffprobe.exe vanished between the check above and the call (AV).
        return {"duration": 0.0, "streams": [], "ok": False, "probe_failed": True,
                "error": str(e)}
    except Exception as e:  # noqa: BLE001
        # ffprobe RAN and rejected the file: that is a genuinely bad/corrupt
        # output and the disc-copy fallback SHOULD run. Do not mask it as
        # "unmeasurable" -- only a tool that cannot run sets probe_failed.
        return {"duration": 0.0, "streams": [], "ok": False, "probe_failed": False,
                "error": str(e)}
    duration = _parse_time((data.get("format") or {}).get("duration"))
    streams = _streams_from_probe(data)
    return {"duration": duration or 0.0, "streams": streams, "ok": True,
            "probe_failed": False}


def encode_audio(input_wav: str, output_path: str, fmt: str = "flac",
                 progress_cb: Optional[Callable[[float], None]] = None,
                 duration: float = 0.0, bitrate: str = "320k",
                 meta: Optional[dict] = None, cover: Optional[str] = None) -> int:
    """Encode a WAV to FLAC or MP3 with the bundled ffmpeg. Returns rc.

    Optionally writes metadata tags (`meta`: title/artist/album/track/date)
    and embeds cover art (`cover`: path to an image) as an attached picture.
    If cover embedding fails (unsupported build) it retries without it.
    """
    fmt = (fmt or "flac").lower()
    if fmt == "flac":
        args = ["-c:a", "flac", "-compression_level", "8"]
    elif fmt == "mp3":
        args = ["-c:a", "libmp3lame", "-b:a", bitrate or "320k"]
    else:
        raise ValueError("Unsupported encode format: %s" % fmt)

    def _run(with_cover: bool) -> int:
        cmd = [FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "info",
               "-i", _native(input_wav)]
        if with_cover and cover:
            cmd += ["-i", _native(cover)]
        cmd += args
        if with_cover and cover:
            cmd += ["-map", "0:a", "-map", "1:v",
                    "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
        for k, v in (meta or {}).items():
            if v:
                cmd += ["-metadata", f"{k}={v}"]
        cmd += [_native(output_path)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=_NO_WINDOW)
        set_active_proc(proc)
        last_time = 0.0
        try:
            for line in proc.stderr:
                line = line.strip()
                if "time=" in line:
                    m = re.search(r"time=(\d+):(\d+):(\d+)\.(\d+)", line)
                    if m:
                        t = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                             + int(m.group(3)) + float(m.group(4)) / 10)
                        if progress_cb and t != last_time:
                            last_time = t
                            if duration and duration > 0:
                                progress_cb(max(0.0, min(1.0, t / duration)))
            rc = proc.wait()
        finally:
            set_active_proc(None)
        return rc

    rc = _run(with_cover=bool(cover))
    if rc != 0 and cover:
        # cover embedding unsupported -> retry with tags only
        rc = _run(with_cover=False)
    return rc


def safe_name(s: str) -> str:
    """Sanitize a string for use as a file/folder name on Windows."""
    import re as _re
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in (s or "")).strip()
    out = _re.sub(r"[ \t]+", " ", out).strip(" ._")
    return out or "unknown"
