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
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BDPlusError(RuntimeError):
    """The disc is BD+ protected and cannot be decoded.

    A distinct type (not a bare RuntimeError) because the GUI must NOT treat
    it as "this decoder failed, try the fallback ripper": no bundled fallback
    can strip BD+ either, so retrying only burns minutes of drive time before
    failing the same way. It is a terminal, explainable condition.
    """

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
    lib.bd_seek_time.restype = ctypes.c_uint64
    lib.bd_seek_time.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint32]
    lib.bd_tell_time.restype = ctypes.c_uint64
    lib.bd_tell_time.argtypes = [ctypes.c_void_p]


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


# ---------------------------------------------------------------------------
# AACS key database (KEYDB.cfg) refresh
# ---------------------------------------------------------------------------
_KEYDB_URL = "https://fvonline-db.bplaced.net/fv_download.php?lang=eng"

def _cfg_dir() -> str:
    """Writable dir for a user-updated KEYDB.cfg (never Program Files)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "FizzDisk")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.dirname(os.path.abspath(__file__))
    return d


def _user_keydb_path() -> Optional[str]:
    p = os.path.join(_cfg_dir(), "keydb.cfg")
    return p if os.path.exists(p) else None


def update_keydb(force: bool = False) -> Optional[str]:
    """Best-effort, silent refresh of the AACS KEYDB.cfg from FindVUK.

    The bundled keydb is a snapshot; newer discs' keys won't be in it. We keep
    a user copy under LOCALAPPDATA (writable, never Program Files) and refresh
    it at most once per 24h. libaacs uses VUK entries from here, so an updated
    db keeps new discs decryptable WITHOUT any online lookup at rip time (so
    ISP/DNS blocks of online key services don't matter to us).

    Returns the path to the keydb to use, or None. Never raises.
    """
    p = os.path.join(_cfg_dir(), "keydb.cfg")
    if not force and os.path.exists(p):
        try:
            if time.time() - os.path.getmtime(p) < 24 * 3600:
                return p
        except Exception:
            pass
    try:
        import io
        import urllib.request
        import zipfile
        req = urllib.request.Request(_KEYDB_URL, headers={"User-Agent":
                                                          "FizzDisk/1.0"})
        data = urllib.request.urlopen(req, timeout=30).read()
        zf = zipfile.ZipFile(io.BytesIO(data))
        name = next((n for n in zf.namelist()
                     if n.lower().endswith("keydb.cfg")), None)
        if name:
            cfg = zf.read(name)
            # sanity: a real keydb has many disc keys with VUK/unit entries
            if b" | " in cfg and cfg.count(b"0x") > 100000:
                tmp = p + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(cfg)
                os.replace(tmp, p)
                return p
    except Exception:
        pass
    return p if os.path.exists(p) else None


def open_disc(path: str) -> int:
    """Open a Blu-ray disc/ISO/folder. Returns the BLURAY* handle (int)."""
    ensure_loaded()
    # BD+ discs: libbdplus aborts with "VM configuration not found" unless a
    # vm0 config directory exists, and never reaches its cached
    # conversion-table loader. Create the (key-free) scaffold first so a disc
    # whose table the user has installed can actually be repaired.
    try:
        from . import bdplus as _bdp
        if disc_has_bdplus(path):
            _bdp.ensure_vm0()
    except Exception:
        pass
    # Automatic BD+ repair: obtain the cached conversion table for this
    # disc's media key now, so the rip below succeeds without user steps.
    bdplus_ensure(path)
    bdir = _bin_dir()
    # Prefer an auto-refreshed, user-owned keydb (newer discs) over the bundled
    # snapshot; fall back to the bundled one if no refresh was done.
    up = _user_keydb_path()
    keyfile = (up or os.path.join(bdir, "keydb.cfg")).encode() if (up or bdir) \
        else None
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
_PROBE_HEAD_BYTES = 24 * 1024 * 1024


def _probe_ts_head(head: bytes) -> dict:
    """ffprobe a buffered head of the decrypted TS to learn its REAL layout.

    libbluray's playlist stream table (``bd_get_title_info`` -> clip[0]) is a
    *declaration*, not a guarantee: the TS actually delivered by ``bd_read``
    routinely exposes fewer streams (secondary//dependent audio that never
    starts, PGS tracks declared but absent) and in a different order than the
    table lists.  Building ``-map 0:a:N`` / ``-c:a:N`` from the declaration then
    either points at a stream that does not exist -- ffmpeg aborts at option
    parse time with ``Stream map '' matches no streams`` / EINVAL (-22, shown
    unsigned as 4294967274) before a single frame is written -- or applies
    ``copy`` to a track that is really LPCM, which Matroska cannot carry.

    Returns ``{"audio": [codec_name, ...], "subtitle": [...], "video": n}``
    describing what is genuinely demuxable, or ``{}`` if the probe fails (in
    which case the caller must fall back to permissive optional maps).
    """
    tmp = ""
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".ts", prefix="fizzdisk_probe_")
        with os.fdopen(fd, "wb") as f:
            f.write(head)
        data = ffutil.ffprobe_json([
            "-f", "mpegts",
            "-probesize", "50M", "-analyzeduration", "50M",
            "-i", _native(tmp),
        ])
    except Exception:
        return {}
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    out = {"audio": [], "subtitle": [], "video": 0}
    for s in (data.get("streams") or []):
        kind = s.get("codec_type")
        if kind == "video":
            out["video"] += 1
        elif kind == "audio":
            out["audio"].append((s.get("codec_name") or "").lower())
        elif kind == "subtitle":
            out["subtitle"].append((s.get("codec_name") or "").lower())
    return out


# Audio codecs Matroska can carry as-is lives in ffutil (single source of
# truth, shared with the file/ISO/DVD remux path). Anything NOT on that list
# (LPCM / pcm_bluray above all) is transcoded to FLAC, which is lossless and
# Matroska-native. Whitelist, never blacklist: an unknown codec that MKV
# cannot mux would otherwise kill the whole rip at header time.
_MKV_SAFE_AUDIO = ffutil.MKV_SAFE_AUDIO


def _bd_stream_flags(real: dict, title: Title,
                     audio_idx: Optional[List[int]],
                     sub_idx: Optional[List[int]]) -> tuple:
    """Build (map_args, audio_codec_args) from the PROBED stream layout.

    Requested indices are clamped to what the TS really exposes, so no map can
    reference a missing stream, and each output audio stream gets its codec
    decided by the codec ffprobe actually reported for it.

    Every map is emitted with a trailing ``?`` (optional). Clamping and the
    optional marker are belt-and-braces on purpose: the probe only saw the
    HEAD of the title, so a stream it missed must never abort the rip.
    """
    real_audio = real.get("audio") or []
    real_subs = real.get("subtitle") or []
    # Requested selection, defaulting to "everything the TS really has".
    want_a = list(audio_idx) if audio_idx else list(range(len(real_audio)))
    want_s = list(sub_idx) if sub_idx else []
    # Clamp: drop any index the real TS does not have.
    keep_a = [i for i in want_a if 0 <= i < len(real_audio)]
    keep_s = [i for i in want_s if 0 <= i < len(real_subs)]
    maps: List[str] = []
    codecs: List[str] = []
    for j, i in enumerate(keep_a):
        maps += ["-map", f"0:a:{i}?"]
        cn = real_audio[i]
        codecs += [f"-c:a:{j}", "copy" if cn in _MKV_SAFE_AUDIO else "flac"]
    for i in keep_s:
        maps += ["-map", f"0:s:{i}?"]
    return maps, codecs


def _probe_is_trustworthy(real: dict, title: Title) -> bool:
    """Is the head-probe's stream list safe to build the rip command from?

    A probe that found NO audio while the disc's playlist table declares audio
    has not understood the stream (e.g. an LPCM/HDMV track the demuxer only
    recognises once it sees the HDMV registration descriptor, or audio that
    simply has not started within the buffered head). Building maps from it
    would silently produce a VIDEO-ONLY MKV and report success -- far worse
    than the crash we are fixing, because the user only finds out on playback.
    In that case the caller must use the permissive fallback instead.
    """
    if not real:
        return False
    if not real.get("video"):
        return False
    declared_audio = len(title.audio or [])
    found_audio = len(real.get("audio") or [])
    if declared_audio and not found_audio:
        return False
    return True


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

        read_buf = (ctypes.c_ubyte * (256 * 1024))()

        # --- Buffer the head of the decrypted TS and probe it -----------------
        # This costs a few seconds of disc read and is the only reliable way to
        # know which streams exist and what they really are. The buffer is NOT
        # discarded: it is pushed into ffmpeg first, so nothing is re-read.
        head = bytearray()
        while len(head) < _PROBE_HEAD_BYTES:
            if stop_event is not None and stop_event.is_set():
                break
            r = _BLURAY.bd_read(ctypes.c_void_p(bd), read_buf, len(read_buf))
            if r <= 0:
                break
            head += bytes(read_buf[:r])
        real = _probe_ts_head(bytes(head)) if head else {}

        cmd = [ffutil.FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "info",
               "-f", "mpegts",
               # The pipe is unseekable, so ffmpeg only ever sees what it has
               # read; the defaults are too small to find every BD stream.
               "-probesize", "50M", "-analyzeduration", "50M",
               "-i", "pipe:0"]
        cmd += ["-map", "0:v:0"] if video else []
        if _probe_is_trustworthy(real, title):
            maps, acodecs = _bd_stream_flags(real, title, audio_idx, sub_idx)
            print(f"[fizz] probed TS: {len(real.get('audio') or [])} audio, "
                  f"{len(real.get('subtitle') or [])} subs "
                  f"(playlist declared {len(title.audio or [])}/"
                  f"{len(title.subtitles or [])})",
                  file=sys.stderr, flush=True)
        else:
            # The probe could not be trusted (it saw no audio although the
            # playlist declares some, or it failed outright). Fall back to the
            # declared table, but make every map OPTIONAL ("?") so a
            # declared-but-absent stream is skipped instead of aborting the
            # rip, and transcode ALL audio to FLAC: we cannot tell which track
            # is LPCM, and FLAC is lossless so nothing is lost by being safe.
            ai = list(audio_idx) if audio_idx else list(range(len(title.audio)))
            maps = []
            for i in ai:
                maps += ["-map", f"0:a:{i}?"]
            for i in (sub_idx or []):
                maps += ["-map", f"0:s:{i}?"]
            declared = [(a.codec_name or "").lower() for a in (title.audio or [])]
            all_safe = bool(declared) and all(c in _MKV_SAFE_AUDIO for c in declared)
            acodecs = ["-c:a", "copy" if all_safe else "flac"]
            print(f"[fizz] head-probe inconclusive; using declared streams with "
                  f"optional maps and -c:a {acodecs[1]}",
                  file=sys.stderr, flush=True)
        cmd += maps
        if copy_codecs and video_codec is None:
            cmd += ["-c:v", "copy", "-c:s", "copy"] + acodecs
        else:
            cmd += ["-c:v", video_codec or ("libx264" if not copy_codecs else "copy")]
            if video_codec == "libx264" or (not copy_codecs and video_codec is None):
                cmd += ["-preset", "fast", "-crf", "20"]
            if audio_codec:
                cmd += ["-c:a", audio_codec]
            elif copy_codecs:
                cmd += acodecs
            else:
                cmd += ["-c:a", "aac"]
            cmd += ["-c:s", "copy"]
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
                    if line and ("Error" in line or "Invalid" in line
                                 or "matches no streams" in line
                                 or "Could not write header" in line
                                 or "failed" in line.lower()):
                        err_buf.append(line)
            except Exception:
                pass

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()

        written = 0
        last_frac = -1.0
        recover_rounds = 0
        try:
            # Push the probe buffer in first — it is real title data.
            if head:
                try:
                    proc.stdin.write(bytes(head))
                    written += len(head)
                except (BrokenPipeError, OSError):
                    pass
            while True:
                if stop_event is not None and stop_event.is_set():
                    stopped[0] = True
                    ffutil.stop_active_proc()
                    break
                r = _BLURAY.bd_read(ctypes.c_void_p(bd), read_buf, len(read_buf))
                if r <= 0:
                    # Some BD-50 / obfuscated discs (e.g. Lionsgate "boneyard"
                    # segments at a layer boundary) present an undecryptable
                    # unit that makes libbluray STOP bd_read() before the
                    # title's real end, truncating the rip (loses the tail).
                    # The movie content after the break IS on the disc, so if we
                    # stopped well short of the declared duration, seek past the
                    # broken unit and keep reading the remainder into ffmpeg.
                    try:
                        cur = _BLURAY.bd_tell_time(ctypes.c_void_p(bd)) / 90000.0
                    except Exception:
                        cur = 0.0
                    dur = float(title.duration or 0)
                    if recover_rounds < 4 and dur > 60 and (cur <= 0 or cur < dur - 10):
                        recover_rounds += 1
                        resume_s = (cur + 3.0) if cur > 0 else dur * 0.75
                        print(f"[fizz] boneyard: stopped at {cur:.1f}s, seeking to "
                              f"{resume_s:.1f}s to recover ({recover_rounds})",
                              file=sys.stderr, flush=True)
                        _BLURAY.bd_seek_time(ctypes.c_void_p(bd),
                                             int(resume_s * 90000), 0)
                        if progress_cb:
                            progress_cb(min(1.0, written / total))
                        continue
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
        if written == 0 and not stopped[0]:
            raise RuntimeError(
                "No decrypted video was read from the disc (AACS2 / UHD). "
                "Your optical drive must be UHD-capable (e.g. a BP60NB10 / "
                "UHD cross-flash) and the disc's key must be in the bundled "
                "keydb.cfg. FizzDisk will fall back to its bundled ripper if "
                "this decoder can't handle it.")
        if rc != 0 and not stopped[0]:
            # A BD+ disc decrypts its AACS layer fine and then feeds us video
            # that is still scrambled: ffmpeg muxes a little, hits the first
            # packet whose timestamp the mangled stream destroyed, and the
            # Matroska muxer aborts with EINVAL (-22). Blaming ffmpeg here
            # sends the user chasing a codec bug that does not exist, so name
            # the real cause when the disc carries BD+.
            if disc_has_bdplus(path) and not bdplus_ready(path):
                raise BDPlusError(_BDPLUS_MESSAGE)
            raise RuntimeError(
                f"ffmpeg exited {rc} for {title.name}: " + " | ".join(err_buf[-3:]))
    finally:
        close_disc(bd)
    return rc


def disc_has_bdplus(path: str) -> bool:
    """True if the disc carries BD+ protection (a BDSVM folder of .svm files).

    BD+ is a SECOND layer on top of AACS: even with the AACS keys correct and
    ``bd_read`` returning cleanly, the video elementary stream stays mangled
    until a BD+ virtual machine un-munges it. libbdplus needs a VM
    configuration (device keys / conversion tables) that we do not ship, so on
    these discs it logs ``bdplus_init() failed`` + ``VM configuration not
    found`` and libbluray hands back video that merely LOOKS like valid H.264.

    The tell-tale symptom is lopsided: AUDIO decodes perfectly (BD+ does not
    touch it) while VIDEO throws continuous ``error while decoding MB`` /
    ``non-existing PPS`` and eventually yields a packet with no timestamp that
    the Matroska muxer rejects with EINVAL (-22). Detecting the folder lets us
    say so plainly instead of blaming ffmpeg or, far worse, writing a
    corrupt-but-playable file and calling it a success.
    """
    try:
        m = re.match(r"^([A-Za-z]):[/\\]?$", path)
        root = (m.group(1) + ":\\") if m else path
        svm = os.path.join(root, "BDSVM")
        if not os.path.isdir(svm):
            return False
        return any(f.lower().endswith(".svm") for f in os.listdir(svm))
    except Exception:
        return False


_BDPLUS_MESSAGE = (
    "This disc uses BD+ copy protection and no conversion table for it is "
    "installed yet.\n\n"
    "The disc's AACS encryption was unlocked correctly, but BD+ is a second "
    "layer that deliberately corrupts the picture; a 'conversion table' for "
    "this exact disc is needed to repair it. That is why the audio is perfect "
    "while the video breaks up and the rip fails partway through.\n\n"
    "FizzDisk can use a conversion table once you install one. Open "
    "Settings \u2192 BD+ to see this disc's ID and add its table. Without it, "
    "this title cannot be ripped correctly.\n\n"
    "Nothing is wrong with your disc, your drive, or your computer."
)


def bdplus_ready(path: str) -> bool:
    """True if this BD+ disc has an installed conversion table.

    When True the rip can proceed normally: libbdplus loads the cached table
    and repairs the video, even though its VM cannot run this disc's BD+
    generation (the cached-table path is independent of the VM).
    """
    try:
        from . import bdplus as _bdp
        return bool(_bdp.status(path).get("ready"))
    except Exception:
        return False


def bdplus_ensure(path: str, progress_cb=None) -> bool:
    """Proactively make a BD+ disc rip-able by obtaining its conversion table.

    If the disc is not BD+ (or already has a table), returns True immediately.
    Otherwise it downloads the cached table for the disc's media key from the
    Doom9 archive and installs it, so the rip is automatic. If nothing can be
    obtained it returns False and the caller surfaces the BD+ error.
    """
    try:
        from . import bdplus as _bdp
        return bool(_bdp.ensure_table(path, progress_cb=progress_cb))
    except Exception:
        return False


def _native(p: str) -> str:
    return p.replace("\\", "/")
