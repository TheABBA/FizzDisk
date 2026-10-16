# -*- coding: utf-8 -*-
"""DVD VIDEO_TS scanning (clean-room).

Strategy:
  * Read VIDEO_TS.IFO (VMG) TT_SRPT to enumerate titles (TTN -> VTS).
  * Each title maps to a VTS title set; extraction source is the VTS_xx_N.VOB
    files of that VTS (concatenated in order).
  * VTS_01_0.IFO is parsed for a duration heuristic (VTS_PGCIT playback time);
    the whole-VTS clip is correct for single-title-single-VTS discs (the common
    main-movie case).

Also ships a self-test fixture generator (``build_test_dvd``) that writes a
minimal single-title DVD matching this parser, so the scanner can be verified
fully offline.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import List

from .ffutil import _native, ProbeTimeoutError, _streams_from_probe, ffprobe_json
from .model import Clip, Stream, Title

DVD_SECTOR = 2048


def _u16(b: bytes, off: int) -> int:
    return struct.unpack_from(">H", b, off)[0]


def _u32(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def _bcd(byte: int) -> int:
    """Decode a packed-BCD byte (DVD dvd_time_t fields are BCD)."""
    return (byte >> 4) * 10 + (byte & 0x0F)


def _bcd_encode(v: int) -> int:
    """Encode 0..99 as a packed-BCD byte."""
    return ((v // 10) << 4) | (v % 10)


def _bcd_time_parts(dur_sec: int):
    """Return (HH, MM, SS, FF) as BCD-encoded bytes for a duration in seconds."""
    hh = _bcd_encode((dur_sec // 3600) % 100)
    mm = _bcd_encode(((dur_sec % 3600) // 60) % 60)
    ss = _bcd_encode(dur_sec % 60)
    return (hh, mm, ss, 0)


def _time_to_sec(b: bytes, off: int) -> float:
    hh = _bcd(b[off])
    mm = _bcd(b[off + 1])
    ss = _bcd(b[off + 2] & 0x3F)
    return hh * 3600 + mm * 60 + ss


def _vts_vob_files(base: Path, vts_num: int) -> List[Path]:
    vts = base / "VIDEO_TS"
    out: List[Path] = []
    # Include every VTS_xx_N.VOB (video/audio/subtitle live in _1.._N, but a
    # 223MB VTS_xx_0.VOB can be the first title segment, not a menu). ffmpeg's
    # concat demuxer needs the video PID from _0 to list the title's streams.
    for p in sorted(vts.glob(f"VTS_{vts_num:02d}_*.VOB")):
        if p.suffix.upper() == ".VOB":
            out.append(p)
    return out


def _read_vmg_titles(base: Path) -> List[dict]:
    vmgf = base / "VIDEO_TS" / "VIDEO_TS.IFO"
    b = vmgf.read_bytes()
    if b[0:12] != b"DVDVIDEO-VMG":
        raise ValueError("Not a DVD VMG")
    tt_srpt = _u32(b, 0xC4)
    ts = tt_srpt * DVD_SECTOR
    n = _u16(b, ts)
    out = []
    for i in range(n):
        e = ts + 8 + i * 12
        out.append({"vts": b[e + 6], "ttn": b[e + 7], "start_sector": _u32(b, e + 8)})
    return out


def _pgc_duration_ifo(base: Path, vts_num: int, ttn: int = 1) -> float:
    ifo = base / "VIDEO_TS" / f"VTS_{vts_num:02d}_0.IFO"
    if not ifo.exists():
        return 0.0
    b = ifo.read_bytes()
    if b[0:12] != b"DVDVIDEO-VTS":
        return 0.0
    pgcit = _u32(b, 0xCC) * DVD_SECTOR
    if pgcit + 8 > len(b):
        return 0.0
    nr = _u16(b, pgcit)
    if nr < 1:
        return 0.0
    srp0 = pgcit + 8  # PGCIT header is 8 bytes; PGCI search pointers follow
    # Each PGCI SRP is 8 bytes: entry_id(+0), block bits(+1), ptl_id_mask(+2),
    # pgc_start_byte(+4, u32, byte offset from the start of VTS_PGCIT).
    chosen = None
    for k in range(nr):
        es = srp0 + k * 8
        if es + 8 > len(b):
            break
        entry = b[es]
        # Title program chains carry entry_id 0x80|ttn (0x81..0x8F).
        if (entry & 0x80) and (entry & 0x0F) == (ttn & 0x0F):
            chosen = es
            break
    if chosen is None:
        idx = min(max(ttn - 1, 0), nr - 1)
        chosen = srp0 + idx * 8
    start_byte = _u32(b, chosen + 4)
    pg = pgcit + start_byte
    if pg + 8 > len(b):
        return 0.0
    return _time_to_sec(b, pg + 4)


def _resolve_base(base_str: str) -> Path:
    """Return the parent directory that CONTAINS a VIDEO_TS folder."""
    base = Path(base_str)
    # A bare drive letter ("D:") is drive-RELATIVE in pathlib: Path("D:")/"VIDEO_TS"
    # becomes "D:VIDEO_TS" and resolves against the process CWD on D: instead of
    # "D:\VIDEO_TS". Normalize any bare "X:" to "X:\" (like detect_source /
    # _bd_base / _raw_device all do) so a manually-typed drive letter scans too.
    m = re.match(r"^([A-Za-z]):$", base_str.strip())
    if m:
        base = Path(f"{m.group(1)}:\\")
    if (base / "VIDEO_TS").is_dir():
        return base
    if (base / "VIDEO_TS.IFO").exists():
        return base.parent
    raise ValueError(f"No VIDEO_TS.IFO found under {base_str}")


def _dvd_demux_path(base: Path) -> str:
    """Return the path to hand the ``-f dvdvideo`` demuxer (libdvdnav).

    libdvdcss (linked statically into the bundled ffmpeg) must open the RAW
    DEVICE -- the bare drive letter, e.g. ``D:`` -- to perform the CSS key
    exchange and descramble protected VOBs. If we instead pass the
    ``D:/VIDEO_TS`` directory, libdvdcss tries to open that *directory* as a
    device file and fails with ``Permission denied`` (it uses the libc API),
    so CSS authentication never happens and every title decodes to 0 frames
    (an empty/valid-but-header-only MKV). Passing the device root lets
    libdvdcss open the drive and decrypt, exactly like the external ripper.

    For a VIDEO_TS folder on disk (typically not scrambled) the folder path is
    accepted by libdvdread's fallback and we keep it unchanged.
    """
    s = str(base)
    m = re.match(r"^([A-Za-z]:)[\\/]*$", s)
    if m:
        return m.group(1)              # e.g. 'D:' (raw device)
    return str(base / "VIDEO_TS")      # folder source


def _dvdvideo_streams(base: Path, title_no: int) -> List[Stream]:
    """Return the title's stream list via libdvdread's DVD demuxer.

    The built-in ffmpeg ``mpeg``/concat demuxers cannot read CSS-protected DVD
    video (or only one audio program), so for the per-title stream list we probe
    with ``-f dvdvideo -title N`` (libdvdread), which decrypts and navigates the
    disc just like the external ripper. This yields the exact video/audio/subpicture set for
    that title, so the GUI selection and the rip stay consistent.
    """
    global _DVDVIDEO_STUCK
    if _DVDVIDEO_STUCK:
        return []          # a previous title already timed out; don't re-wait
    demux = _dvd_demux_path(base)
    try:
        # -trim 0: don't trim padding cells, so titles whose PGC is only
        # padding/empty ("Title N, PGC 1 looks empty") still expose their
        # stream list. With the default trim, ffprobe errors "Invalid data
        # found when processing input" and would abort the whole scan.
        data = ffprobe_json(["-f", "dvdvideo", "-title", str(title_no),
                             "-trim", "0", _native(demux)])
        return _streams_from_probe(data)
    except ProbeTimeoutError:
        # The drive is stuck/hanging; probe every subsequent title too and
        # we'd burn PROBE_TIMEOUT each. Latch so only the first title waits.
        _DVDVIDEO_STUCK = True
        return []
    except Exception:
        # A title that libdvdread cannot demux (pillarbox/padding-only PGC,
        # malformed IFO…) must not abort the scan. Return no streams; the GUI
        # will show the title with no video/audio and the rip will use the VOBs.
        return []


_VOB_STREAM_CACHE: dict = {}

# Latch set when the dvdvideo (libdvdread) probe times out on a title, meaning
# the drive is stuck/hanging. Once set, no further dvdvideo probe is attempted
# in the same scan (each would otherwise wait PROBE_TIMEOUT seconds), so the
# scan finishes quickly with IFO-derived durations and the movie prompt still
# appears. Reset at the start of every scan_dvd_folder.
_DVDVIDEO_STUCK: bool = False


def _vts_stream_union(base: Path, vts_num: int) -> List[Stream]:
    """Return the union of audio/subtitle streams from probing the VTS's VOBs
    (cached per VTS).

    The bundled ffmpeg ``-f dvdvideo`` demuxer only exposes the default audio
    program and hides subpicture (VobSub) streams. The VOB files' (unencrypted)
    program-stream headers list every PID, so probing them recovers the full
    audio and subtitle set. Deduplicated by stream PID.
    """
    key = (str(base), vts_num)
    if key in _VOB_STREAM_CACHE:
        return _VOB_STREAM_CACHE[key]
    out: List[Stream] = []
    seen = set()
    for vob in _vts_vob_files(base, vts_num):
        try:
            data = ffprobe_json(["-f", "mpeg", _native(vob)])
        except ProbeTimeoutError:
            # The drive is stuck/timing out; probing further VOBs would just
            # burn PROBE_TIMEOUT each. Stop here rather than stall the scan.
            break
        except Exception:
            continue
        for s in _streams_from_probe(data):
            if s.codec_type not in ("audio", "subtitle"):
                continue
            k = (s.codec_type, s.id)
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
    _VOB_STREAM_CACHE[key] = out
    return out


def _title_streams(base: Path, title_no: int, vts_num: int,
                   probe_dvdvideo: bool = True) -> List[Stream]:
    """Return the title's stream list: the dvdvideo probe merged with any
    audio/subtitle streams recovered from the VOB program stream headers.

    ``probe_dvdvideo`` is False for every non-feature title: the ``-f dvdvideo``
    (libdvdread) probe is the ONLY source of the VIDEO stream, but it can HANG
    on padding/menu/extras PGCs that libdvdnav cannot navigate (some retail discs
    have a handful of such PGCs, each stalling the scan for the full probe
    timeout). The feature title is the only one needing the video stream, so we
    probe only it and get the rest (audio/subtitle) from the cheap VOB union.
    """
    streams = _dvdvideo_streams(base, title_no) if probe_dvdvideo else []
    present = {(s.codec_type, s.id) for s in streams}
    for s in _vts_stream_union(base, vts_num):
        if (s.codec_type, s.id) not in present:
            streams.append(s)
            present.add((s.codec_type, s.id))
    return streams


def scan_dvd_folder(base_str: str) -> List[Title]:
    global _DVDVIDEO_STUCK
    _DVDVIDEO_STUCK = False
    base = _resolve_base(base_str)
    titles_raw = _read_vmg_titles(base)
    if not titles_raw:
        raise ValueError("DVD scan: no titles in TT_SRPT")

    titles: List[Title] = []
    vts_dir = _dvd_demux_path(base)
    # Pre-read IFO durations (fast local reads) to identify the feature title
    # BEFORE probing, so the slow libdvdread (-f dvdvideo) probe runs on ONLY
    # the feature. Most retail discs have padding/menu/extras PGCs whose
    # dvdvideo probe HANGS (libdvdnav cannot navigate them); probing every
    # title would stall the scan for the full probe timeout per hung title.
    durs: List[float] = []
    for t in titles_raw:
        try:
            durs.append(_pgc_duration_ifo(base, t["vts"], t["ttn"]))
        except Exception:
            durs.append(0.0)
    main_idx = max(range(len(durs)), key=lambda i: durs[i] or 0) if durs else 0
    # Seamless-branching / multi-angle discs expose SEVERAL VMG titles that are
    # alternate navigation entries into the SAME physical cells (e.g. a disc
    # with VTS1=6250s and VTS3=6249s, plus branch-entry titles 7-9). libdvdnav
    # cannot navigate those entries on a live optical device and the
    # `-f dvdvideo` probe HANGS on each one for the full timeout -- verified
    # sequentially, with nothing else touching the drive, on a disc that other
    # rippers read fine (they parse the IFOs instead of navigating).
    #
    # Only ever issue the navigating probe for the feature title. Every other
    # title's audio/subtitle set comes from the cheap VOB union, which reads
    # the program-stream headers directly and never navigates.
    probe_ok = {main_idx}
    for i, t in enumerate(titles_raw):
        vts_num = t["vts"]
        ttn = t["ttn"]
        title_no = i + 1          # VMG title number (libdvdread / dvdvideo -title)
        vob_files = _vts_vob_files(base, vts_num)
        if not vob_files:
            continue
        # The DVD's own PGC playback time (BCD) is the authoritative per-title
        # length and matches the external ripper. Never trust the ffprobe concat duration for
        # a VTS: every title in a VTS would then get the same (wrong) number.
        dur = durs[i]
        info = {"duration": dur, "chapters": [], "streams": _title_streams(
                    base, title_no, vts_num, probe_dvdvideo=(i in probe_ok)),
                "raw": None, "dvd_title_no": title_no, "dvd_dir": vts_dir}
        name = f"Title {ttn}"
        clips = [Clip(files=[str(f) for f in vob_files])]
        title = Title(index=len(titles), name=name, clips=clips,
                      duration=info.get("duration", 0.0),
                      source_kind="dvd", is_main=False)
        for s in info.get("streams", []):
            if s.codec_type == "video" and title.video is None:
                title.video = s
            elif s.codec_type == "audio":
                title.audio.append(s)
            elif s.codec_type == "subtitle":
                title.subtitles.append(s)
        title.info = info
        titles.append(title)

    if not titles:
        raise ValueError("DVD scan produced no titles")
    # Second line of defense: a DVD has no authoritative "main title" navigation
    # (unlike a BD), so mark the LONGEST title as main rather than blindly title
    # #1 -- title #1 is often a menu/marker/extras clip (e.g. a 1-second entry).
    # The runtime match in the GUI later refines this against the movie's real
    # length; this just keeps the raw auto-pick from being obviously wrong.
    if titles:
        main = max(range(len(titles)), key=lambda i: titles[i].duration or 0)
        for i, t in enumerate(titles):
            t.is_main = (i == main)
    return titles


# ----------------------------------------------------------------------------
# Self-test fixture generator
# ----------------------------------------------------------------------------
def _video_attr(mpeg_version=1, video_format=0, aspect=1, permitted_df=0,
                bit_rate=1, letterboxed=0, film_mode=0, picture_size=0):
    b0 = ((mpeg_version & 3) << 6) | ((video_format & 3) << 4) | ((aspect & 3) << 2) | (permitted_df & 3)
    b1 = ((bit_rate & 1) << 4) | ((picture_size & 3) << 2) | ((letterboxed & 1) << 1) | (film_mode & 1)
    return bytes([b0, b1])


def _audio_attr(audio_format=0, lang_type=0, application_mode=0, quant=0,
                sample_freq=0, channels=1, lang_code=0x656E):
    b0 = ((audio_format & 7) << 5) | ((lang_type & 3) << 2) | (application_mode & 3)
    b1 = ((quant & 3) << 6) | ((sample_freq & 3) << 4) | (channels & 7)
    return struct.pack(">BBHBBBB", b0, b1, lang_code, 0, 0, 0, 0)


def _vts_attributes(nr_audio=1) -> bytes:
    b = bytearray(542)
    struct.pack_into(">I", b, 0, 541)
    b[8:10] = _video_attr(mpeg_version=2)
    b[93] = 0
    b[264:266] = _video_attr(mpeg_version=2)
    b[267] = nr_audio
    aa = _audio_attr(audio_format=0, channels=1, lang_code=0x656E)
    for i in range(nr_audio):
        b[268 + 8 * i: 276 + 8 * i] = aa
    return bytes(b)


def _write_vmg_menu(buf: bytes, u: int) -> None:
    """Write a minimal VMG menu PGCI_UT at byte offset ``u``.

    libdvdnav refuses to open a DVD whose VMG menu PGCI_UT is NULL (it
    segfaults), and it also rejects an empty (0-language-unit) PGCI_UT. We
    therefore write a single-language-unit PGCI_UT whose menu PGC has 1 program
    and 1 cell, mirroring the exact on-disk layout of the VTS PGCIT that
    libdvdread already accepts. This lets the offline fixture open under
    ``-f dvdvideo`` (libdvdnav) just like a real retail disc.
    """
    # --- pgci_ut_t (Menu PGCI Unit Table) ---------------
    struct.pack_into(">H", buf, u + 0x00, 1)       # nr_of_lus = 1
    struct.pack_into(">H", buf, u + 0x02, 0)       # zero_1
    struct.pack_into(">I", buf, u + 0x04, 0x0F)    # last_byte (8 + 8 - 1)
    # --- pgci_lu_t (Menu PGCI Language Unit) -----------
    struct.pack_into(">H", buf, u + 0x08, 0x656E)  # lang_code = "en"
    buf[u + 0x0A] = 0                              # lang_extension
    buf[u + 0x0B] = 1                              # exists
    struct.pack_into(">I", buf, u + 0x0C, 0x10)    # lang_start_byte -> PGCIT at u+0x10
    # --- pgcit_t at u+0x10 ------------------------------
    p = u + 0x10
    struct.pack_into(">H", buf, p + 0x00, 1)       # nr_of_pgci_srp = 1
    struct.pack_into(">H", buf, p + 0x02, 0)       # zero_1
    struct.pack_into(">I", buf, p + 0x04, 0x10 + 236 - 1)  # pgcit last_byte (header + PGC)
    # --- pgci_srp_t at p+0x08 ----------------------------
    buf[p + 0x08] = 0x00                           # entry_id
    buf[p + 0x09] = 0x00                           # block_bits
    struct.pack_into(">H", buf, p + 0x0A, 0)       # ptl_id_mask
    struct.pack_into(">I", buf, p + 0x0C, 0x10)    # pgc_start_byte = 16
    # --- pgc_t at g = p + 0x10 (same layout as the VTS PGC) ---
    g = p + 0x10
    struct.pack_into(">H", buf, g + 0x00, 0)       # zero_1
    buf[g + 0x02] = 1                              # nr_of_programs
    buf[g + 0x03] = 1                              # nr_of_cells
    buf[g + 0x04] = 0                              # playback_time (BCD) = 0
    buf[g + 0x05] = 0
    buf[g + 0x06] = 0
    buf[g + 0x07] = 0
    pm_off = 236
    cp_off = 236 + 1
    cpos_off = cp_off + 24
    struct.pack_into(">H", buf, g + 0xE6, pm_off)   # cell_playback_offset
    struct.pack_into(">H", buf, g + 0xE8, cp_off)   # cell_position_offset
    struct.pack_into(">H", buf, g + 0xEA, cpos_off)
    buf[g + pm_off] = 1                            # program_map nr_of_programs
    cb = g + cp_off
    buf[cb] = 0; buf[cb + 1] = 0
    buf[cb + 2] = 0; buf[cb + 3] = 0
    buf[cb + 4] = 0; buf[cb + 5] = 0; buf[cb + 6] = 0; buf[cb + 7] = 0  # playback time 0
    struct.pack_into(">I", buf, cb + 8, 0)
    struct.pack_into(">I", buf, cb + 12, 0)
    struct.pack_into(">I", buf, cb + 16, 0)        # last sector
    struct.pack_into(">I", buf, cb + 20, 0)        # last sector
    cp = g + cpos_off
    struct.pack_into(">H", buf, cp, 1)             # vob_id_nr = 1
    buf[cp + 2] = 0                                # zero_1
    buf[cp + 3] = 1                                # cell_nr


def _write_vmg(dur_sec: int) -> bytes:
    n = 8
    buf = bytearray(n * DVD_SECTOR)
    buf[0:12] = b"DVDVIDEO-VMG"
    struct.pack_into(">I", buf, 0x0C, n - 1)   # vmg_last_sector (total VMG file)
    struct.pack_into(">I", buf, 0x1C, 3)       # vmgi_last_sector: VMGI tables span sectors 0..3
                                              #   (libdvdread requires vmgi_last_sector*2 <= vmg_last_sector,
                                              #    and tt_srpt/vts_atrt <= vmgi_last_sector)
    buf[0x21] = 0x28
    buf[0x2A] = 1                              # disc_side (must be 1 or 2)
    struct.pack_into(">H", buf, 0x26, 1)
    struct.pack_into(">H", buf, 0x28, 1)
    struct.pack_into(">H", buf, 0x3E, 1)
    struct.pack_into(">I", buf, 0x80, 0x200)
    struct.pack_into(">I", buf, 0x84, 0)
    struct.pack_into(">I", buf, 0xC0, 0)
    struct.pack_into(">I", buf, 0xC4, 2)
    struct.pack_into(">I", buf, 0xD0, 3)
    buf[0x100:0x102] = _video_attr(mpeg_version=2)
    p = 2 * DVD_SECTOR
    struct.pack_into(">H", buf, p, 1)
    struct.pack_into(">H", buf, p + 2, 0)
    entry = p + 8
    struct.pack_into(">I", buf, p + 4, entry + 12 - 1)
    buf[entry] = 0x00
    buf[entry + 1] = 0x01
    struct.pack_into(">H", buf, entry + 2, 1)
    struct.pack_into(">H", buf, entry + 4, 0)
    buf[entry + 6] = 1
    buf[entry + 7] = 1
    struct.pack_into(">I", buf, entry + 8, 8)
    a = 3 * DVD_SECTOR
    struct.pack_into(">H", buf, a, 1)
    struct.pack_into(">H", buf, a + 2, 0)
    attrs_off = 12
    struct.pack_into(">I", buf, a + 4, attrs_off + 542 - 1)
    struct.pack_into(">I", buf, a + 8, attrs_off)
    buf[a + attrs_off: a + attrs_off + 542] = _vts_attributes(1)
    # VMG menu PGCI_UT (1 language unit + menu PGC) at sector 4 so libdvdnav
    # can open the disc instead of failing on a NULL pgci_ut.
    u = 4 * DVD_SECTOR
    struct.pack_into(">I", buf, 0xC8, 4)     # vmgm_pgci_ut = sector 4
    _write_vmg_menu(buf, u)
    return bytes(buf)


def _write_vts(vob_start: int, vob_sectors: int, dur_sec: int) -> bytes:
    ifo_sectors = 6
    buf = bytearray(ifo_sectors * DVD_SECTOR)
    buf[0:12] = b"DVDVIDEO-VTS"
    struct.pack_into(">I", buf, 0x0C, vob_start + vob_sectors - 1)
    struct.pack_into(">I", buf, 0x1C, ifo_sectors - 1)
    buf[0x21] = 0x28
    struct.pack_into(">I", buf, 0x80, 0x400)
    struct.pack_into(">I", buf, 0xC0, 0)
    struct.pack_into(">I", buf, 0xC4, vob_start)
    struct.pack_into(">I", buf, 0xC8, 1)
    struct.pack_into(">I", buf, 0xCC, 2)
    struct.pack_into(">I", buf, 0xE0, 3)
    struct.pack_into(">I", buf, 0xE4, 4)
    buf[0x100:0x102] = _video_attr(mpeg_version=2)
    buf[0x103] = 0
    buf[0x200:0x202] = _video_attr(mpeg_version=2)
    buf[0x203] = 1
    a1 = _audio_attr(audio_format=0, channels=1, lang_code=0x656E)
    buf[0x204:0x20C] = a1

    p = 1 * DVD_SECTOR
    struct.pack_into(">H", buf, p, 1)
    struct.pack_into(">H", buf, p + 2, 0)
    ttu_off = 12
    struct.pack_into(">I", buf, p + 4, ttu_off + 4 - 1)
    struct.pack_into(">I", buf, p + 8, ttu_off)
    struct.pack_into(">H", buf, p + ttu_off, 1)
    struct.pack_into(">H", buf, p + ttu_off + 2, 1)

    q = 2 * DVD_SECTOR
    struct.pack_into(">H", buf, q, 1)
    struct.pack_into(">H", buf, q + 2, 0)
    pgc = 16
    struct.pack_into(">I", buf, q + 4, pgc + 236 - 1)
    # PGC search pointer (8 bytes): entry_id=0x81 (title #1), block bits=0,
    # ptl_id_mask=0, pgc_start_byte=16 (byte offset from PGCIT start).
    buf[q + 8] = 0x81
    buf[q + 9] = 0x00
    struct.pack_into(">H", buf, q + 10, 0)
    struct.pack_into(">I", buf, q + 12, pgc)
    g = q + pgc
    struct.pack_into(">H", buf, g, 0)        # pgc.zero_1 (reserved) = 0
    buf[g + 2] = 1                           # pgc.nr_of_programs (u8) = 1
    buf[g + 3] = 1                           # pgc.nr_of_cells (u8) = 1
    hh_b, mm_b, ss_b, ff_b = _bcd_time_parts(dur_sec)
    buf[g + 4] = hh_b
    buf[g + 5] = mm_b
    buf[g + 6] = ss_b
    buf[g + 7] = ff_b
    pm_off = 236
    cp_off = 236 + 1
    cpos_off = cp_off + 24
    struct.pack_into(">H", buf, g + 0xE6, pm_off)
    struct.pack_into(">H", buf, g + 0xE8, cp_off)
    struct.pack_into(">H", buf, g + 0xEA, cpos_off)
    buf[g + pm_off] = 1
    cb = g + cp_off
    buf[cb] = 0; buf[cb + 1] = 0
    buf[cb + 2] = 0; buf[cb + 3] = 0
    buf[cb + 4] = hh_b; buf[cb + 5] = mm_b; buf[cb + 6] = ss_b; buf[cb + 7] = ff_b
    struct.pack_into(">I", buf, cb + 8, 0)
    struct.pack_into(">I", buf, cb + 12, 0)
    struct.pack_into(">I", buf, cb + 16, vob_sectors - 1)
    struct.pack_into(">I", buf, cb + 20, vob_sectors - 1)
    cp = g + cpos_off
    struct.pack_into(">H", buf, cp, 1)   # cell_position.vob_id_nr = 1
    buf[cp + 2] = 0                      # cell_position.zero_1 (reserved) = 0
    buf[cp + 3] = 1                      # cell_position.cell_nr = 1

    r = 3 * DVD_SECTOR
    struct.pack_into(">H", buf, r, 1)
    struct.pack_into(">H", buf, r + 2, 0)
    struct.pack_into(">I", buf, r + 4, 8 + 12 - 1)
    struct.pack_into(">H", buf, r + 8, 1)
    buf[r + 10] = 1
    buf[r + 11] = 0
    struct.pack_into(">I", buf, r + 12, 0)
    struct.pack_into(">I", buf, r + 16, vob_sectors - 1)

    s = 4 * DVD_SECTOR
    struct.pack_into(">I", buf, s, 4 + 4 - 1)
    struct.pack_into(">I", buf, s + 4, 1)
    return bytes(buf)


def build_test_dvd(vob_content: Path, out_dir: Path) -> Path:
    """Generate a minimal single-title VIDEO_TS at out_dir from vob_content
    (must be a valid MPEG-2 program stream; becomes VTS_01_1.VOB)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    vts = out_dir / "VIDEO_TS"
    vts.mkdir(exist_ok=True)
    data = vob_content.read_bytes()
    vob_sectors = -(-len(data) // DVD_SECTOR)
    vob_start = 8 + 6 + 6
    vts_ifo = _write_vts(vob_start, vob_sectors, 12)
    vmg = _write_vmg(12)
    (vts / "VIDEO_TS.IFO").write_bytes(vmg)
    (vts / "VIDEO_TS.BUP").write_bytes(vmg)
    (vts / "VTS_01_0.IFO").write_bytes(vts_ifo)
    (vts / "VTS_01_0.BUP").write_bytes(vts_ifo)
    (vts / "VTS_01_1.VOB").write_bytes(data)
    (vts / "VIDEO_TS.VOB").write_bytes(b"")
    return out_dir
