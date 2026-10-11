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

    cmd = [FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "info", *inputs]
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
        cmd += ["-c:v", "copy" if video and not video_codec else (video_codec or "copy"),
                "-c:a", "copy" if audio_codec is None else audio_codec]
    else:
        vc = video_codec or "libx264"
        cmd += ["-c:v", vc]
        if vc == "libx264":
            cmd += ["-preset", "fast", "-crf", "20"]
        cmd += ["-c:a", audio_codec or "aac", "-b:a", "192k"]

    cmd += ["-c:s", "copy"]
    cmd += ["-map_metadata", "0"]
    cmd += ["-default_mode", "infer", "-metadata", f"title={title.name}"]
    cmd += [_native(out_path)]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, creationflags=_NO_WINDOW)
    set_active_proc(proc)
    err_lines: List[str] = []
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
                        progress_cb({"time": t, "duration": title.duration})
            elif line and "Duplicate" not in line and "Press" not in line:
                if "Error" in line or "Invalid" in line or "failed" in line.lower():
                    err_lines.append(line)
        rc = proc.wait()
    finally:
        set_active_proc(None)
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
