# -*- coding: utf-8 -*-
"""Music disc metadata lookup (artist, album, track titles, cover art).

Uses the MusicBrainz web service (free, no API key) and the Cover Art Archive.
Disc identification is done by MusicBrainz Disc ID first; if the disc's TOC
does not exactly match a known pressing (e.g. the TOC was read approximately),
we fall back to a fuzzy Table-of-Contents search and match candidate releases
by track duration.

No third-party library is required -- only urllib. A proper User-Agent is
mandatory for MusicBrainz (rate limits), so one is always sent.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from . import cdda

_UA = {"User-Agent": "FizzDisk/1.0 (CD audio disc lookup)"}
_MB = "https://musicbrainz.org/ws/2"
_CAA = "https://coverartarchive.org"


def _get_json(url: str, retries: int = 3) -> dict:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:  # noqa: PERF203
            # MusicBrainz throttles; 429/503 are transient.
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(1.2 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise
    raise last  # type: ignore[misc]


def _artist_credit(ac: list) -> str:
    parts = []
    for item in ac or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            parts.append(name)
        jp = " ".join((x.get("joinphrase", "") for x in ([item] if isinstance(item, dict) else [])
                       if isinstance(x, dict)))
    return "".join(parts) if parts else "Unknown Artist"


def _release_to_meta(release: dict) -> dict:
    """Build a meta dict {artist, album, year, release_mbid, tracks, date}."""
    ac = release.get("artist-credit") or []
    artist = ""
    for item in ac:
        name = item.get("name") if isinstance(item, dict) else item
        artist += name or ""
        artist += (item.get("joinphrase", "") if isinstance(item, dict) else "")
    artist = artist.strip() or "Unknown Artist"
    album = release.get("title") or ""
    year = None
    date = (release.get("date") or "").strip()
    import re as _re
    m = _re.search(r"(\d{4})", date)
    if m:
        year = int(m.group(1))
    tracks = []
    for med in release.get("media") or []:
        for tr in med.get("tracks") or []:
            length = tr.get("length") or 0
            tracks.append({
                "position": tr.get("position"),
                "title": tr.get("title") or "",
                "length_ms": length,
                "length_sec": round((length / 1000.0), 1) if length else 0.0,
            })
    return {
        "artist": artist,
        "album": album,
        "year": year,
        "release_mbid": release.get("id"),
        "release_group_mbid": (release.get("release-group") or {}).get("id"),
        "date": date,
        "tracks": tracks,
    }


def _track_seconds(tracks: List[dict]) -> List[float]:
    return [t["length_sec"] for t in tracks]


def _duration_match(release_sec: List[float], my_sec: List[float]) -> int:
    """Score how well a release's track durations match the disc's."""
    if not release_sec or len(release_sec) != len(my_sec):
        return -1
    score = 0
    for a, b in zip(release_sec, my_sec):
        if abs(a - b) <= 3.0:
            score += 2
        elif abs(a - b) <= 6.0:
            score += 1
    return score


def _candidates_for_toc(toc: str) -> List[dict]:
    """Fuzzy TOC search; returns candidate release dicts (minimal)."""
    url = _MB + "/discid/-?toc=%s&inc=artist-credits+recordings&fmt=json" % (
        urllib.parse.quote(toc))
    d = _get_json(url)
    return d.get("releases") or []


def _best_candidate(releases: List[dict], my_sec: List[float]) -> Optional[dict]:
    """Pick the release from candidates whose track durations match the disc."""
    best, best_score = None, -2
    for rel in releases:
        meta = _release_to_meta(rel)
        score = _duration_match(_track_seconds(meta["tracks"]), my_sec)
        if score > best_score:
            best, best_score = meta, score
    return best


def my_track_seconds(tracks: List[dict]) -> List[float]:
    """Convert cdda TOC track sectors to seconds for duration matching."""
    out = []
    for t in tracks:
        if t.get("track") and t.get("is_audio"):
            out.append(round((t.get("sectors") or 0) / 75.0, 1))
    return out


def identify_disc(tracks: List[dict]) -> dict:
    """Return metadata for the CD from its TOC, or {} if not found.

    Tries the exact MusicBrainz Disc ID first (best when the TOC came from a
    real device), then falls back to a fuzzy TOC search + duration match.
    """
    doc = {}
    try:
        my_sec = my_track_seconds(tracks)
    except Exception:  # noqa: BLE001
        my_sec = []

    # 1. Exact Disc ID lookup
    try:
        did = cdda.toc_to_discid(tracks)
        if did:
            d = _get_json(_MB + "/discid/%s?inc=artist-credits+recordings&fmt=json"
                          % urllib.parse.quote(did))
            rels = d.get("releases") or []
            if rels:
                return _release_to_meta(rels[0])
    except urllib.error.HTTPError as e:
        if e.code != 404:  # 404 = unknown disk; fall through
            doc["discid_error"] = str(e.code)
    except Exception as e:  # noqa: BLE001
        doc["discid_error"] = str(e)

    # 2. Fuzzy TOC search + duration match
    try:
        toc = cdda.toc_string(tracks)
        if toc:
            rels = _candidates_for_toc(toc)
            if rels:
                best = _best_candidate(rels, my_sec)
                if best:
                    return best
                # no duration match; take the first candidate
                return _release_to_meta(rels[0])
    except Exception as e:  # noqa: BLE001
        doc["fuzzy_error"] = str(e)
    return doc


def cover_art_url(release_mbid: str, size: Optional[str] = None) -> str:
    """Return a Cover Art Archive front-image URL for a release."""
    path = "/release/%s/front" % release_mbid
    if size:
        path += "-" + size
    return _CAA + path


def download_cover(release_mbid: str, dest: str,
                   size: Optional[str] = None) -> Optional[str]:
    """Download the front cover to `dest`. Returns dest or None."""
    url = cover_art_url(release_mbid, size)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read()
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    try:
        with open(dest, "wb") as f:
            f.write(data)
        return dest
    except OSError:
        return None
