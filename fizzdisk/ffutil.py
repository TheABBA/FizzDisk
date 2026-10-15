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
    global _active_proc
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


def _native(p: str) -> str:
    """Normalise a path for a native Windows binary."""
    return p.replace("\\", "/")


def run(cmd: Sequence[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True,
                          timeout=timeout, creationflags=_NO_WINDOW)


# ---- ffprobe ---------------------------------------------------------------
def ffprobe_json(input_args: List[str]) -> dict:
    """Run ffprobe returning parsed JSON dict. input_args excludes the -i."""
    cmd = [FFPROBE, "-v", "error", "-show_format", "-show_streams", "-show_chapters",
           "-of", "json", *input_args]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "ffprobe failed")
    return json.loads(r.stdout)


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


def _copy_dvd_to_disk(drive_dir: str) -> Optional[str]:
    """Copy a physical DVD's VIDEO_TS folder to a fresh temp dir.

    libdvdnav navigating the LIVE optical drive can abort on a disc whose cell
    navigation state is fragile (it demuxes everything before the bad NAV packet
    then exits 0 -- a silent truncation). Copying the disc's file tree to disk
    yields a stable source: a raw file copy does not need NAV packets, and the
    bundled libdvdcss still descrambles the VOBs.

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
    base = tempfile.mkdtemp(prefix="fizzdisk_dvfolder_")
    dst = os.path.join(base, "VIDEO_TS")

    def _run_copy(retries: str) -> bool:
        # Bounded retries (NOT robocopy's default /R:1000000 /W:30, which hangs
        # forever on a bad sector and gets killed by the timeout half-way,
        # leaving a partial tree). /Z enables restartable copy for the rare
        # re-try pass. We do NOT trust the exit code alone -- we verify the tree.
        try:
            subprocess.run(
                ["robocopy", src, dst, "/E", "/R:" + retries, "/W:2", "/Z",
                 "/NFL", "/NDL", "/NJH", "/NJS"],
                capture_output=True, text=True, timeout=1800, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            # Copy may still have produced a complete tree before the cap; verify
            # rather than assuming failure (robocopy is a child of a killed proc).
            pass
        except Exception:
            return False
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
def copy_audio_flags(title: "Title", audio_idx: Optional[List[int]]) -> List[str]:
    """FFmpeg args to copy the selected audio streams, transcoding LPCM (the
    pcm_bluray codec on Blu-rays) losslessly to FLAC.

    Matroska cannot carry a ``pcm_bluray`` track directly (``No wav codec tag
    found for codec pcm_bluray``) and refuses to write the MKV header, so any
    LPCM stream must be re-encoded. FLAC is lossless, natively supported by
    Matroska, and preserves the audio bit-exactly. All other audio is copied.
    """
    aud = list(audio_idx or [])
    if not aud:
        return ["-c:a", "copy"]
    flags: List[str] = []
    for j, ai in enumerate(aud):
        cn = ""
        try:
            cn = (title.audio[ai].codec_name or "").lower()
        except (IndexError, AttributeError):
            pass
        if cn in ("lpcm", "pcm_bluray"):
            flags += [f"-c:a:{j}", "flac"]
        else:
            flags += [f"-c:a:{j}", "copy"]
    return flags


def remux(title: Title, out_path: str, video: bool = True,
          audio_idx: Optional[List[int]] = None, sub_idx: Optional[List[int]] = None,
          copy_codecs: bool = True, video_codec: Optional[str] = None,
          audio_codec: Optional[str] = None, progress_cb: Optional[Callable[[dict], None]] = None,
          extra_map: Optional[List[int]] = None) -> int:
    """Remux a title to an MKV. Returns ffmpeg returncode (0 == success).

    audio_idx / sub_idx are ORDINALS within the title's audio / subtitle lists
    (0-based) that the user selected. video selects the video track.
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

    def _run(inputs: List[str], out: str, dur: float) -> int:
        """Build cmd from `inputs`, run ffmpeg, stream progress, return rc."""
        cmd = [FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "info", *inputs]
        print("[remux] ffmpeg:", " ".join(cmd[:40]), flush=True)
        # maps
        cmd += ["-map", "0:v:0"] if video else []
        for i in (audio_idx or []):
            cmd += ["-map", f"0:a:{i}"]
        for i in (sub_idx or []):
            cmd += ["-map", f"0:s:{i}"]
        for i in (extra_map or []):
            cmd += ["-map", f"0:{i}"]

        if copy_codecs and video_codec is None and video and audio_idx is None:
            cmd += ["-c", "copy"]
        elif copy_codecs:
            cmd += ["-c:v", "copy" if video and not video_codec else (video_codec or "copy")]
            if audio_codec is None:
                cmd += copy_audio_flags(title, audio_idx)
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
                                text=True, creationflags=_NO_WINDOW)
        set_active_proc(proc)
        last_time = 0.0
        try:
            for line in proc.stderr:
                line = line.strip()
                if line.startswith("frame=") or "time=" in line:
                    m = re.search(r"time=(\d+):(\d+):(\d+)\.(\d+)", line)
                    if m:
                        t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + float(m.group(4)) / 10
                        if progress_cb and t != last_time:
                            last_time = t
                            progress_cb({"time": t, "duration": dur})
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
        truncated = (rc != 0) or (not info.get("ok")) or \
                    (out_dur > 0 and out_dur < expected_dur * 0.90)
        if truncated:
            print(f"[remux] drive rip failed/truncated (rc={rc}, got {out_dur:.1f}s "
                  f"vs expected {expected_dur:.1f}s) -> re-ripping from a disc copy",
                  flush=True)
            dvd_base = _copy_dvd_to_disk(dvd_dir) if dvd_dir else None
            if dvd_base:
                try:
                    fb_inputs = ["-analyzeduration", "20000000", "-probesize", "20000000",
                                 "-f", "dvdvideo", "-title", str(dvd_title_no),
                                 "-i", _native(dvd_base)]
                    rc2 = _run(fb_inputs, out_path, expected_dur)
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
    """Probe an output MKV; return {duration, streams:[Stream], ok}."""
    try:
        data = ffprobe_json([_native(path)])
    except Exception as e:  # noqa: BLE001
        return {"duration": 0.0, "streams": [], "ok": False, "error": str(e)}
    duration = _parse_time((data.get("format") or {}).get("duration"))
    streams = _streams_from_probe(data)
    return {"duration": duration or 0.0, "streams": streams, "ok": True}


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
                                text=True, creationflags=_NO_WINDOW)
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
