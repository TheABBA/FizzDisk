# -*- coding: utf-8 -*-
"""Self-contained AACS Blu-ray support via libbluray + libaacs + libbdplus.

This is the open-source decoder that replaces the external ripper.
All four pieces are bundled under ``bin/``:
  * ``bluray.dll``     - libbluray  (BD navigation + demux + decryption hook)
  * ``libaacs.dll``    - AACS decryption
  * ``libbdplus.dll``  - BD+ decryption
  * ``keydb.cfg``      - AACS volume-key database (FindVuk KEYDB.cfg)

libbluray dlopens libaacs/libbdplus at runtime, so they must be reachable via
the DLL search path (we pre-load them and add the binary dir).  Titles are
enumerated with ``bd_get_titles``/``bd_get_title_info`` (durations + streams);
ripping reads *decrypted* MPEG-TS packets with ``bd_read`` and pipes them into
ffmpeg, which remuxes to MKV.  No external ripper required.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from . import ffutil
from .model import Stream, Title

# ---------------------------------------------------------------------------
# AACS / BD+ stream-type byte values (from libbluray bd_stream_type_e)
# ---------------------------------------------------------------------------
_STREAM_TYPES = {
    0x01: ("video", "mpeg1video"),
    0x02: ("video", "mpeg2video"),
    0x03: ("audio", "mp1"),          # MPEG-1 audio
    0x04: ("audio", "mp2"),          # MPEG-2 audio
    0x1B: ("video", "h264"),
    0x24: ("video", "hevc"),
    0x80: ("audio", "lpcm"),
    0x81: ("audio", "ac3"),
    0x82: ("audio", "dts"),
    0x83: ("audio", "truehd"),
    0x84: ("audio", "eac3"),
    0x85: ("audio", "dts"),
    0x86: ("audio", "dts"),          # dts-hd master
    0x90: ("subtitle", "hdmv_pgs_subtitle"),
    0x91: ("subtitle", "hdmv_pgs_subtitle"),
    0x92: ("subtitle", "subrip"),
    0xEA: ("video", "vc1"),
    0xA1: ("audio", "eac3"),         # secondary
    0xA2: ("audio", "dts"),          # secondary
}
# Values 0xA1/0xA2 secondary audio should not be treated as main audio by
# default; we still expose them but the GUI filters via codec_type==audio.

# ---------------------------------------------------------------------------
# DLL loading (once)
# ---------------------------------------------------------------------------
_BLURAY = None


def _bin_dir() -> Optional[str]:
    """Find the directory holding bluray.dll / libaacs.dll / keydb.cfg."""
    for base in (Path(__file__).resolve().parent.parent,
                 Path(__file__).resolve().parent.parent.parent):
        cand = os.path.join(str(base), "bin")
        if (Path(cand) / "bluray.dll").exists() and (Path(cand) / "keydb.cfg").exists():
            return cand
    return os.path.dirname(os.path.abspath(ffutil.FFMPEG)) if ffutil.FFMPEG else None


def ensure_loaded() -> None:
    """Load bluray.dll (and make libaacs/libbdplus reachable). Idempotent."""
    global _BLURAY
    if _BLURAY is not None:
        return
    bdir = _bin_dir()
    if bdir:
        try:
            os.add_dll_directory(bdir)          # Python 3.8+
        except Exception:
            pass
        if bdir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bdir + os.pathsep + os.environ.get("PATH", "")
        # Pre-load so libbluray's LoadLibrary("libaacs.dll") resolves.
        try:
            ctypes.WinDLL(os.path.join(bdir, "libaacs.dll"))
        except OSError:
            pass
        try:
            ctypes.WinDLL(os.path.join(bdir, "libbdplus.dll"))
        except OSError:
            pass
    dll = os.path.join(bdir, "bluray.dll") if bdir else "bluray.dll"
    if not os.path.isfile(dll):
        raise RuntimeError(f"bundled bluray.dll not found (looked near {bdir})")
    _BLURAY = ctypes.WinDLL(dll)
    _init_prototypes(_BLURAY)


# ---------------------------------------------------------------------------
# ctypes structs / prototypes
# ---------------------------------------------------------------------------
class _Stream(ctypes.Structure):
    _fields_ = [("coding_type", ctypes.c_uint8), ("format", ctypes.c_uint8),
                ("rate", ctypes.c_uint8), ("char_code", ctypes.c_uint8),
                ("lang", ctypes.c_uint8 * 4), ("pid", ctypes.c_uint16),
                ("aspect", ctypes.c_uint8), ("subpath_id", ctypes.c_uint8)]


class _Clip(ctypes.Structure):
    _fields_ = [("pkt_count", ctypes.c_uint32), ("still_mode", ctypes.c_uint8),
                ("still_time", ctypes.c_uint16), ("video_stream_count", ctypes.c_uint8),
                ("audio_stream_count", ctypes.c_uint8), ("pg_stream_count", ctypes.c_uint8),
                ("ig_stream_count", ctypes.c_uint8), ("sec_audio_stream_count", ctypes.c_uint8),
                ("sec_video_stream_count", ctypes.c_uint8),
                ("video_streams", ctypes.POINTER(_Stream)), ("audio_streams", ctypes.POINTER(_Stream)),
                ("pg_streams", ctypes.POINTER(_Stream)), ("ig_streams", ctypes.POINTER(_Stream)),
                ("sec_audio_streams", ctypes.POINTER(_Stream)), ("sec_video_streams", ctypes.POINTER(_Stream)),
                ("start_time", ctypes.c_uint64), ("in_time", ctypes.c_uint64),
                ("out_time", ctypes.c_uint64)]


class _TitleInfo(ctypes.Structure):
    _fields_ = [("idx", ctypes.c_uint32), ("playlist", ctypes.c_uint32),
                ("duration", ctypes.c_uint64), ("clip_count", ctypes.c_uint32),
                ("angle_count", ctypes.c_uint8), ("chapter_count", ctypes.c_uint32),
                ("mark_count", ctypes.c_uint32), ("clips", ctypes.POINTER(_Clip)),
                ("chapters", ctypes.c_void_p), ("marks", ctypes.c_void_p),
                ("mvc", ctypes.c_uint8)]


def _init_prototypes(lib) -> None:
    lib.bd_open.restype = ctypes.c_void_p
    lib.bd_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.bd_open_disc.restype = ctypes.c_int
    lib.bd_open_disc.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.bd_close.argtypes = [ctypes.c_void_p]
    lib.bd_get_titles.restype = ctypes.c_uint32
    lib.bd_get_titles.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint32]
    lib.bd_get_main_title.restype = ctypes.c_int
    lib.bd_get_main_title.argtypes = [ctypes.c_void_p]
    lib.bd_get_title_info.restype = ctypes.POINTER(_TitleInfo)
    lib.bd_get_title_info.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint]
    lib.bd_free_title_info.argtypes = [ctypes.POINTER(_TitleInfo)]
    lib.bd_select_playlist.restype = ctypes.c_int
    lib.bd_select_playlist.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.bd_read.restype = ctypes.c_int
    lib.bd_read.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
    lib.bd_get_version.restype = ctypes.c_char_p
    lib.bd_get_version.argtypes = []


def _raw_device(path: str) -> str:
    """Return the raw device name for a physical drive, so libbluray/libaacs
    can do AACS bus-encryption (required for UHD/AACS2). A bare drive path
    like 'D:' or 'D:\\' must be a raw device (\\\\.\\D:); a folder/ISO path is
    left unchanged. libbluray in *folder* mode cannot init bus encryption, so
    bd_read() returns 0 and ffmpeg fails with 'Error opening input file pipe:0'.
    """
    m = re.match(r"^([A-Za-z]):[/\\]?$", path)
    if m:
        return "\\\\.\\%s:" % m.group(1)
    return path


def open_disc(path: str) -> int:
    """Open a Blu-ray disc/ISO/folder. Returns the BLURAY* handle (int)."""
    ensure_loaded()
    bdir = _bin_dir()
    keyfile = os.path.join(bdir, "keydb.cfg").encode() if bdir else None
    dev = _raw_device(path)
    bd = _BLURAY.bd_open(dev.encode("utf-8", "replace") if isinstance(dev, str) else dev,
                         keyfile)
    if not bd:
        raise RuntimeError(f"libbluray could not open {path!r} (AACS keys missing?)")
    return ctypes.c_void_p(bd).value


def close_disc(bd) -> None:
    if bd:
        _BLURAY.bd_close(ctypes.c_void_p(bd))


def _stream_name(ctype: int) -> str:
    codec_type, name = _STREAM_TYPES.get(ctype, ("data", "unknown"))
    return codec_type, name


def _lang(bytes4: bytes) -> Optional[str]:
    s = bytes4.rstrip(b"\x00").decode("latin-1")
    return s or None


def _build_streams(ti: _TitleInfo) -> tuple:
    """Return (video, audio_list, sub_list) from a title info (first clip)."""
    video, audio, subs = None, [], []
    if ti.clip_count and ti.clips:
        c = ti.clips[0]
        for i in range(c.video_stream_count):
            s = c.video_streams[i]
            ct, name = _stream_name(s.coding_type)
            st = Stream(index=i, codec_type=ct, codec_name=name, id=f"0x{s.pid:04x}")
            if video is None:
                video = st
        for i in range(c.audio_stream_count):
            s = c.audio_streams[i]
            ct, name = _stream_name(s.coding_type)
            audio.append(Stream(index=i, codec_type="audio", codec_name=name,
                                language=_lang(bytes(s.lang)), id=f"0x{s.pid:04x}"))
        for i in range(c.pg_stream_count):
            s = c.pg_streams[i]
            ct, name = _stream_name(s.coding_type)
            subs.append(Stream(index=len(subs), codec_type="subtitle", codec_name=name,
                               language=_lang(bytes(s.lang)), id=f"0x{s.pid:04x}"))
    return video, audio, subs


def scan_disc(path: str, min_seconds: float = 0.0) -> List[Title]:
    """Enumerate Blu-ray titles with durations and stream info.

    This mirrors the reference external ripper's title enumeration/durations
    using the open-source libbluray+libaacs stack (no external ripper required).
    """
    ensure_loaded()
    bd = open_disc(path)
    titles: List[Title] = []
    try:
        # TITLES_RELEVANT = 0x03 removes duplicate-title/clip entries.
        n = _BLURAY.bd_get_titles(ctypes.c_void_p(bd), 0x03, 0)
        infos = []
        for i in range(n):
            ti = _BLURAY.bd_get_title_info(ctypes.c_void_p(bd), i, 0)
            if not ti:
                continue
            info = ti.contents
            # Skip the disc's first-play / top-menu titles (huge clip counts,
            # no audio, e.g. playlists 1000/1001).
            is_menu = (info.clip_count > 50 and info.clip_count > 0
                       and _has_no_audio(info))
            dur_s = info.duration / 90000.0
            if min_seconds and dur_s and dur_s < min_seconds:
                _BLURAY.bd_free_title_info(ti)
                continue
            # size estimate from mpeg-ts packet counts (192 bytes / packet);
            # guard against absurd multi-clip sums by falling back to a
            # duration-based estimate.
            pkt_est = sum(info.clips[j].pkt_count * 192 for j in range(info.clip_count))
            dur_est = int((dur_s or 0) * 40_000_000 / 8)
            size_b = dur_est
            if pkt_est and (dur_est <= 0 or pkt_est <= dur_est * 4):
                size_b = pkt_est
            video, audio, subs = _build_streams(info)
            infos.append({
                "idx": i, "playlist": info.playlist, "duration": dur_s,
                "clip_count": info.clip_count, "size": size_b,
                "video": video, "audio": audio, "subs": subs, "menu": is_menu,
            })
            _BLURAY.bd_free_title_info(ti)
        # The disc's own navigation (BD-J / Hdmv) is authoritative for the main
        # title. This is essential for obfuscated discs (e.g. Lionsgate), where
        # decoy playlists can match the feature's duration exactly — "longest"
        # alone would often pick a fake. Fall back to the longest non-menu title
        # if libbluray reports no main.
        main_idx = _BLURAY.bd_get_main_title(ctypes.c_void_p(bd))
        main_ent = next((x for x in infos if x.get("idx") == main_idx), None)
        if main_ent is None:
            non_menu = [x for x in infos if not x.get("menu")]
            main_ent = max(non_menu, key=lambda x: x["duration"], default=None) if non_menu else None
        for k, ent in enumerate(infos):
            if ent.get("menu"):
                continue
            t = Title(index=ent["idx"], name=f"Title {ent['idx'] + 1}", clips=[],
                      duration=ent["duration"], size_bytes=ent["size"],
                      source_kind="bd", is_main=(main_ent is not None and ent is main_ent),
                      video=ent["video"], audio=ent["audio"], subtitles=ent["subs"],
                      info={"bd_playlist": ent["playlist"], "bd_size_bytes": ent["size"],
                            "bd_clip_count": ent["clip_count"]})
            titles.append(t)
    finally:
        close_disc(bd)
    return titles


def _has_no_audio(ti: _TitleInfo) -> bool:
    if ti.clip_count and ti.clips:
        c = ti.clips[0]
        return (c.audio_stream_count == 0 and c.video_stream_count > 0)
    return False


# ---------------------------------------------------------------------------
# ripping: stream decrypted TS from libbluray into ffmpeg -> MKV
# ---------------------------------------------------------------------------
def rip_bd(path: str, title: Title, out_path: str,
           video: bool = True, audio_idx: Optional[List[int]] = None,
           sub_idx: Optional[List[int]] = None, copy_codecs: bool = True,
           video_codec: Optional[str] = None, audio_codec: Optional[str] = None,
           progress_cb: Optional[Callable[[float], None]] = None,
           stop_event: Optional[threading.Event] = None,
           total_bytes: float = 0.0) -> int:
    """Rip title (by its stored bd_playlist) to MKV, decrypting via libaacs.

    Reads *decrypted* MPEG-TS from libbluray over stdin and feeds ffmpeg, which
    remuxes to MKV. Returns ffmpeg return code (0 == success). Registered with
    ffutil so the Stop button / window-close can terminate the running ffmpeg.
    """
    playlist = int((title.info or {}).get("bd_playlist", 0))
    ensure_loaded()
    bd = open_disc(path)
    rc = 0
    try:
        # bd_select_playlist requires the title/playlist table to be populated
        # first; call bd_get_titles (discard count) to initialize it.
        _BLURAY.bd_get_titles(ctypes.c_void_p(bd), 0x03, 0)
        sel = _BLURAY.bd_select_playlist(ctypes.c_void_p(bd), playlist)
        if sel != 1:
            raise RuntimeError(f"libbluray: select playlist {playlist} failed")

        cmd = [ffutil.FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "info",
               "-f", "mpegts", "-i", "pipe:0"]
        cmd += ["-map", "0:v:0"] if video else []
        for i in (audio_idx or []):
            cmd += ["-map", f"0:a:{i}"]
        for i in (sub_idx or []):
            cmd += ["-map", f"0:s:{i}"]
        if copy_codecs and video_codec is None:
            cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]
        else:
            cmd += ["-c:v", video_codec or ("libx264" if not copy_codecs else "copy")]
            if video_codec == "libx264" or (not copy_codecs and video_codec is None):
                cmd += ["-preset", "fast", "-crf", "20"]
            cmd += ["-c:a", audio_codec or ("aac" if not copy_codecs else "copy"),
                    "-c:s", "copy"]
        cmd += ["-map_metadata", "0", "-default_mode", "infer",
                "-metadata", f"title={title.name}", _native(out_path)]

        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=_NO_WINDOW)
        ffutil.set_active_proc(proc)
        total = float(total_bytes or title.size_bytes or 0)
        if total <= 0:
            total = max(1.0, float(title.duration or 0) * 33_000_000 / 8)

        err_buf: List[str] = []
        stopped = [False]

        def _drain_stderr():
            try:
                for raw in proc.stderr:
                    line = raw.decode("utf-8", "replace").strip()
                    if line and ("Error" in line or "Invalid" in line or "failed" in line.lower()):
                        err_buf.append(line)
            except Exception:
                pass

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()

        read_buf = (ctypes.c_ubyte * (256 * 1024))()
        written = 0
        last_frac = -1.0
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    stopped[0] = True
                    ffutil.stop_active_proc()
                    break
                r = _BLURAY.bd_read(ctypes.c_void_p(bd), read_buf, len(read_buf))
                if r <= 0:
                    break
                chunk = bytes(read_buf[:r])
                try:
                    proc.stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    break
                written += r
                if progress_cb and total > 0:
                    frac = min(1.0, written / total)
                    if frac - last_frac >= 0.005:
                        last_frac = frac
                        progress_cb(frac)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            if stopped[0]:
                if proc.poll() is None:
                    proc.terminate()
            rc = proc.wait()
            t.join(timeout=3)
            ffutil.set_active_proc(None)
        if rc != 0 and not stopped[0]:
            raise RuntimeError(
                f"ffmpeg exited {rc} for {title.name}: " + " | ".join(err_buf[-3:]))
    finally:
        close_disc(bd)
    return rc


def _native(p: str) -> str:
    return p.replace("\\", "/")
