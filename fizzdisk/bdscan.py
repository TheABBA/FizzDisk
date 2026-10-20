# -*- coding: utf-8 -*-
"""Blu-ray BDMV scanning (clean-room).

Enumerates titles by parsing the playlist (.mpls) files in BDMV/PLAYLIST.
The extraction source for a title is the ordered list of m2ts clips referenced
by that playlist. Stream/duration info comes from ffprobe (concatenated). If the
clip is AACS-encrypted (ffprobe can't demux it), the caller falls back to the
bundled open-source libbluray stack or the external ripper.

Also ships a self-test fixture generator (``build_test_bd``) so the scanner can
be verified offline.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import List

from .ffutil import scan_streams
from .model import Clip, Title

_CLIP_RE = re.compile(rb"(\d{5})(?:M2TS|M2TS\x00)", re.I)


def _parse_mpls(path: Path) -> List[dict]:
    """Return ordered list of {clip, in, out} for a playlist file."""
    b = path.read_bytes()
    items: List[dict] = []
    for m in _CLIP_RE.finditer(b):
        clip = m.group(1).decode()
        # Best-effort in/out: after "M2TS<0x00>" 4-byte InTime, OutTime (45kHz).
        # If they look bogus we ignore them (whole-clip extraction).
        pos = m.end()
        in_ = out_ = None
        if pos + 9 <= len(b):
            # bytes after M2TS: 00 01 00 <intime(4)> <outtime(4)>
            marker = b[pos: pos + 1]
            if marker == b"\x00" and pos + 11 <= len(b):
                in_val = struct.unpack_from(">I", b, pos + 3)[0]
                out_val = struct.unpack_from(">I", b, pos + 7)[0]
                if 0 <= in_val < out_val and out_val - in_val < (120 * 45000):
                    in_ = in_val / 45000.0
                    out_ = out_val / 45000.0
        items.append({"clip": clip, "in": in_, "out": out_})
    return items


def _resolve_base(base_str: str) -> Path:
    base = Path(base_str)
    if (base / "BDMV").is_dir():
        return base
    if (base / "PLAYLIST").is_dir():
        return base / ".."
    if (base / "BDMV" / "PLAYLIST").is_dir():
        return base
    raise ValueError(f"No BDMV/PLAYLIST found under {base_str}")


def _clips_for_playlist(base: Path, mpls: Path) -> List[dict]:
    items = _parse_mpls(mpls)
    clips = []
    for it in items:
        m2ts = base / "BDMV" / "STREAM" / f"{it['clip']}.m2ts"
        if m2ts.exists():
            clips.append({"file": str(m2ts), "in": it["in"], "out": it["out"]})
    return clips


def scan_bd_folder(base_str: str) -> List[Title]:
    base = _resolve_base(base_str)
    playlists = sorted((base / "BDMV" / "PLAYLIST").glob("*.mpls"))
    if not playlists:
        raise ValueError("No .mpls playlists found")

    # ---- Pass 1: enumerate playlists, estimate durations from file sizes ----
    # The .mpls in/out times are often bogus (near-32-bit-max values on many
    # discs, e.g. Black Mirror S01), so we use the m2ts file *size* as the
    # primary duration estimate (BD average ~28 Mbps = 3.5 MB/s, but we use a
    # conservative 4 MB/s to avoid over-estimating short extras).  This is only
    # for ranking / selecting the main title and for the TV heuristic — the real
    # duration comes from the ffprobe scan.
    raw_entries: List[dict] = []
    for mpls in playlists:
        clips = _clips_for_playlist(base, mpls)
        if not clips:
            continue
        total_bytes = 0
        for c in clips:
            try:
                total_bytes += Path(c["file"]).stat().st_size
            except OSError:
                pass
        # Estimate: ~4 MB/s average for BD (conservative; real varies 2-6 MB/s).
        est_dur = total_bytes / (4 * 1024 * 1024) if total_bytes > 0 else 0.0
        # Also compute .mpls in/out as a secondary estimate (may be valid on
        # some discs; use it only if it's in a sane range).
        mpls_dur = 0.0
        for c in clips:
            if c.get("in") is not None and c.get("out") is not None:
                d = float(c["out"]) - float(c["in"])
                # Sanity: 5 min to 4 hours per clip (300s to 14400s at 45kHz).
                if 300 * 45000 < d < 14400 * 45000:
                    mpls_dur += d / 45000.0
        # Prefer the .mpls duration if it's sane; else the file-size estimate.
        dur = mpls_dur if mpls_dur > 0 else est_dur
        raw_entries.append({
            "mpls": mpls, "clips": clips, "duration": dur,
            "file_list": [c["file"] for c in clips],
            "total_bytes": total_bytes,
        })

    if not raw_entries:
        raise ValueError("BD scan: no playlists with valid clips")

    # ---- Decide which titles to probe ----
    # On a normal BD (movie or TV), there are 3-10 playlists.  We probe ALL of
    # them — each probe is one ffprobe call on the m2ts, and 3-10 probes is
    # fast and safe.  The drive-panic issue was from Lionsgate-style obfuscation
    # discs with 100+ *identical* decoy playlists, each probing the same m2ts;
    # for those we cap at the first 15 unique-m2ts playlists.
    #
    # Deduplicate by m2ts file list: playlists that reference the same m2ts
    # set are duplicates (Lionsgate decoys).  Only probe one per unique set.
    seen_clip_sets: set = set()
    probe_set: set = set()
    for i, entry in enumerate(raw_entries):
        clip_key = tuple(sorted(entry["file_list"]))
        if clip_key in seen_clip_sets:
            continue
        seen_clip_sets.add(clip_key)
        probe_set.add(i)
        if len(probe_set) >= 15:  # cap: never more than 15 probes
            break

    main_idx = max(range(len(raw_entries)), key=lambda i: raw_entries[i]["duration"])
    # Make sure the main title is always in the probe set.
    probe_set.add(main_idx)

    titles: List[Title] = []
    for i, entry in enumerate(raw_entries):
        name = f"Title {entry['mpls'].stem}"
        file_list = entry["file_list"]
        if i in probe_set:
            try:
                info = scan_streams(file_list)
            except Exception:
                info = {"duration": entry["duration"], "streams": [],
                        "chapters": []}
        else:
            info = {"duration": entry["duration"], "streams": [],
                    "chapters": []}

        clip_objs = [Clip(files=[c["file"]]) for c in entry["clips"]]
        title = Title(index=len(titles), name=name, clips=clip_objs,
                      duration=info.get("duration", entry["duration"]),
                      source_kind="bd", is_main=(i == main_idx))
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
        raise ValueError("BD scan produced no titles")
    # sort by duration, keep index stable but mark longest as main
    if titles:
        main = max(range(len(titles)), key=lambda i: titles[i].duration)
        for i, t in enumerate(titles):
            t.is_main = (i == main)
    return titles


# ----------------------------------------------------------------------------
# Self-test fixture generator
# ----------------------------------------------------------------------------
def build_test_bd(m2ts_files: List[Path], out_dir: Path, title_seconds: float = 10.0,
                  playlist_number: int = 0) -> Path:
    """Build a minimal BDMV folder at out_dir referencing m2ts_files.

    m2ts files are copied into STREAM/ as 00000.m2ts, 00001.m2ts, ... and a
    matching playlist (00000.mpls) is written (which our scanner reads).
    Returns out_dir.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    bdmv = out_dir / "BDMV"
    (bdmv / "STREAM").mkdir(parents=True, exist_ok=True)
    (bdmv / "PLAYLIST").mkdir(parents=True, exist_ok=True)
    (bdmv / "CLIPINF").mkdir(parents=True, exist_ok=True)

    clips = []
    for i, src in enumerate(m2ts_files):
        name = f"{i:05d}"
        dst = bdmv / "STREAM" / f"{name}.m2ts"
        dst.write_bytes(src.read_bytes())
        clips.append((name, src.stat().st_size))

    # Build an mpls matching the structure our scanner extracts: it just needs
    # the clip names; produce a structurally-valid-enough playlist.
    mpls = _make_mpls([c[0] for c in clips], title_seconds)
    (bdmv / "PLAYLIST" / f"{playlist_number:05d}.mpls").write_bytes(mpls)

    # index.bdmv + MovieObject.bdmv (minimal, for other tools)
    (bdmv / "index.bdmv").write_bytes(_make_index(playlist_number))
    (bdmv / "MovieObject.bdmv").write_bytes(_make_movie_object(playlist_number))
    return out_dir


def _make_mpls(clip_names: List[str], title_seconds: float) -> bytes:
    # Per PlayItem: name bytes (5) + "M2TS" (4) + marker/in-out. We lay each item
    # out so it matches what the scanner's regex (\\d{5}M2TS) + in/out logic reads.
    item_bytes = []
    for cname in clip_names:
        it = bytearray(96)
        it[0] = 0x60                       # length (96 content bytes)
        it[1] = 0x00
        it[2:7] = cname.encode()           # 5-char clip name
        it[7:11] = b"M2TS"                 # codec identifier (as seen on real discs)
        it[11] = 0x00                      # marker after M2TS
        it[12] = 0x01                      # connection_condition
        it[13] = 0x00
        struct.pack_into(">I", it, 14, 0)                       # InTime (45kHz)
        struct.pack_into(">I", it, 18, int(title_seconds * 45000))  # OutTime
        item_bytes.append(bytes(it))

    playlist_start = 40
    n_i = len(item_bytes)
    body_sz = 4 + 2 + n_i * 96
    buf = bytearray(playlist_start + body_sz + 16)
    buf[0:4] = b"MPLS"
    buf[4:8] = b"0200"
    struct.pack_into(">I", buf, 0x0C, playlist_start)
    struct.pack_into(">I", buf, 0x10, playlist_start + body_sz)  # mark start
    p = playlist_start
    struct.pack_into(">I", buf, p, body_sz - 4)   # length of play list section
    struct.pack_into(">H", buf, p + 4, 0x0A)     # reserved
    struct.pack_into(">H", buf, p + 6, n_i)      # number of play items
    off = p + 8
    for it in item_bytes:
        buf[off:off + len(it)] = it
        off += len(it)
    return bytes(buf)


def _make_index(playlist_number: int) -> bytes:
    # Minimal but structurally sane index.bdmv (AppInfo + indexes).
    buf = bytearray(2048)
    buf[0:4] = b"INDX"
    buf[4:8] = b"0200"
    # number of playlists at offset computed from index table; for our scanner
    # it isn't needed, but write it anyway at 0x... We put a simple marker.
    # FirstPlayback / index entries omitted (scanner doesn't rely on it).
    struct.pack_into(">H", buf, 0x3C, 1)   # nr of playlists (best effort)
    return bytes(buf)


def _make_movie_object(playlist_number: int) -> bytes:
    buf = bytearray(1024)
    buf[0:4] = b"MOBJ"
    buf[4:8] = b"0200"
    return bytes(buf)
