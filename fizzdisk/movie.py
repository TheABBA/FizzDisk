# -*- coding: utf-8 -*-
"""Movie metadata lookup (title, year, poster) via Wikipedia — no API key.

Wikipedia exposes (a) a per-page summary (title + short description + the
theatrical poster thumbnail) and (b) a text search. iTunes' movie catalog was
verified to return empty results from some networks, so Wikipedia is used
instead; it needs no key and reliably returns the release year and poster.
"""

from __future__ import annotations

import glob
import json
import os
import re
import struct
import urllib.parse
import urllib.request
from typing import List, Optional

from ._words import _WORDS

_UA = {"User-Agent": "FizzDisk/1.0 (movie metadata lookup; contact: local)"}

# Optional TheMovieDB API key: read from the TMDB_API_KEY env var, or a sidecar
# file 'tmdb_api_key.txt' next to the app (or in the user's home dir). Each
# customer gets their own free key (prompted on first run) so no single key is
# ever shared/abused. Empty = no shipped key; Wikipedia is the no-key fallback.
_BUILTIN_TMDB_KEY = os.environ.get("TMDB_API_KEY", "").strip()

# common tokens appended to disc volume labels that aren't part of a title
_SUFFIXES = ("UHD", "ULTRA HD", "ULTRAHD", "4K", "BLURAY", "BD", "HD", "DVD")


def _get_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers=_UA)
    # Wikipedia rate-limits; retry transient 429/5xx with backoff.
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            if getattr(e, "code", None) in (429,) or getattr(e, "code", 0) >= 500:
                import time
                time.sleep(0.4 * (attempt + 1))
                req = urllib.request.Request(url, headers=_UA)
                continue
            raise
    raise last


def _year(text: Optional[str]) -> Optional[int]:
    """Return the first plausible release year (1900-2100) in a string."""
    if not text:
        return None
    for m in re.finditer(r"(19|20)\d{2}", text):
        y = int(m.group(0))
        if 1900 <= y <= 2100:
            return y
    return None


def clean_term(raw: Optional[str]) -> str:
    """Turn a disc volume label like 'KUNG_FU_PANDA_2' into a searchable title."""
    s = (raw or "").strip()
    for ch in ("_", "-", ".", ":", "x24"):
        s = s.replace(ch, " ")
    s = re.sub(r"\s+", " ", s).strip()
    # drop trailing media-format tokens (case-insensitive, after a space)
    for suf in sorted(_SUFFIXES, key=len, reverse=True):
        s = re.sub(r"\s+%s$" % re.escape(suf), "", s, flags=re.IGNORECASE)
    s = s.title()
    # don't title-cast words that the source already kept all-caps for no reason
    s = re.sub(r"\b([A-Z]{2,})\b", lambda m: m.group(1).title(), s)
    return s.strip()


# -------------------------------------------------------------- disc metadata
# Windows' volume-label APIs only surface the generic UDF label ("LOGICAL_VOLUME_ID"),
# hiding the publisher-set name. The media still names itself in the raw UDF/ISO
# volume descriptor (and, for Blu-ray, in BDMV/META/DL), so we read those when the
# label is blank or generic.

_GENERIC_LABELS = frozenset((
    "", "LOGICAL_VOLUME_ID", "VOLUME", "CD-ROM", "CDROM", "DVD-RAM", "DVD_VIDEO",
    "DVD", "VIDEO_TS", "BDMV", "UNNAMED", "NONE",
))


def name_is_useful(label: Optional[str]) -> bool:
    """True for a label worth searching: non-empty and not a generic one."""
    s = (label or "").strip()
    return bool(s) and s.upper() not in _GENERIC_LABELS


def _decode_dstring(raw: bytes) -> str:
    """Decode a UDF OSTA compressed-unicode dstring (8- or 16-bit)."""
    if not raw:
        return ""
    comp = raw[0]
    if comp == 8:
        out = []
        for b in raw[1:]:
            if not b or b < 0x20:
                break
            out.append(chr(b))
        return "".join(out).rstrip()
    if comp == 16:
        out = []
        for i in range(1, len(raw) - 1, 2):
            v = (raw[i] << 8) | raw[i + 1]
            if not v:
                break
            out.append(chr(v))
        return "".join(out).rstrip()
    return ""


def _raw_volume_name(paths) -> Optional[str]:
    """Read the publisher-set volume identifier from a raw device or .iso file.

    ``paths`` is an ordered list of openable paths ('//./D:' then '\\\\.\\D:',
    or the path of an .iso). ISO-9660 discs expose it in the PVD, UDF discs
    (Blu-ray/DVD) in the UDF Volume Descriptor.
    """
    handle = None
    for p in paths:
        try:
            handle = open(p, "rb")
            break
        except OSError:
            continue
    if handle is None:
        return None
    try:
        def sec(lba, n=2048):
            handle.seek(lba * n)
            return handle.read(n)

        start = sec(16)
        if start[1:6] == b"CD001" and start[6] == 1:
            v = start[40:72].decode("ascii", "replace").rstrip(" \x00")
            return v or None
        if b"BEA01" in start[:8] or b"NSR02" in start or b"NSR03" in start:
            avdp = sec(256)
            if len(avdp) >= 0x18:
                loc = struct.unpack("<I", avdp[0x14:0x18])[0]
                blen = struct.unpack("<I", avdp[0x10:0x14])[0]
                for i in range(max(1, blen // 2048)):
                    b = sec(loc + i)
                    if len(b) < 0x18:
                        break
                    tag = struct.unpack("<H", b[0:2])[0]
                    if tag == 0x0001:  # Volume Descriptor
                        v = _decode_dstring(b[0x18:0x18 + 32]) or _decode_dstring(b[0x3A:0x3A + 128])
                        return v or None
                    if tag == 0x0008:  # Terminal Descriptor
                        break
    finally:
        handle.close()
    return None


def _bd_meta_name(dirbase: str) -> Optional[str]:
    """Blu-ray disc-provided title from BDMV/META/DL/bdmt_*.xml (<di:name>)."""
    if not os.path.isdir(dirbase):
        return None
    for pat in ("*bdmt*.xml", "dl_*.xml"):
        for fp in glob.glob(os.path.join(dirbase, "BDMV", "META", "**", pat), recursive=True):
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    m = re.search(r"<di:name>([^<]+)</di:name>", fh.read())
                if m and m.group(1).strip():
                    return m.group(1).strip()
            except OSError:
                continue
    return None


def _clean_disc_name(raw: str) -> str:
    s = (raw or "").strip()
    # A UDF volume identifier embeds a numeric volume-set-number prefix.
    s = re.sub(r"^\d{1,9}[-_.]", "", s).strip()
    return clean_term(s)


def disc_metadata_name(source: str) -> Optional[str]:
    """Best-guess on-disc name for a disc source, straight off the media.

    Returns a cleaned searchable title when the media identifies itself (Blu-ray
    META/DL metadata, else the raw UDF/ISO volume descriptor), or None. Used only
    as a fallback when the Windows volume label is missing or generic.
    """
    src = (source or "").strip()
    m = re.match(r"^([A-Za-z]):", src)
    if m:
        drive = m.group(1).upper()
        meta = _bd_meta_name(drive + ":" + chr(92))
        if meta:
            return clean_term(meta)
        raw = _raw_volume_name(["//./%s:" % drive,
                                chr(92) * 2 + "." + chr(92) + drive + ":"])
        return _clean_disc_name(raw) if raw else None
    if src.lower().endswith(".iso") and os.path.exists(src):
        raw = _raw_volume_name([src])
        return _clean_disc_name(raw) if raw else None
    if os.path.isdir(src):
        meta = _bd_meta_name(src)
        if meta:
            return clean_term(meta)
    return None


_2LETTER = frozenset("on of in to at by it is as up be we he me my no so do go an or us if am fu ok".split())


def _seg_ok(sub: str) -> bool:
    """A valid split segment: a real word (>=3 chars) or a common 2-letter word."""
    return (len(sub) >= 3 and sub in _WORDS) or (len(sub) == 2 and sub in _2LETTER)


def _segment_word(word: str) -> str:
    """Split a concatenated word into known words, e.g. 'stuckonyou' -> 'Stuck on You'.

    Uses a dynamic-programming pass over an embedded English word list so that
    disc volume labels with words run together can still be searched. Returns the
    word unchanged when it is already a known word or cannot be split cleanly
    (so we never produce garbage fragments like 'K Un G Fu Pan Da').
    """
    w = word.lower()
    if len(w) < 4 or not w.isalpha() or w in _WORDS:
        return word
    n = len(w)
    maxw = 14
    dp: List[Optional[List[str]]] = [None] * (n + 1)
    dp[0] = []
    for i in range(1, n + 1):
        best = None
        for j in range(1, min(i, maxw) + 1):
            sub = w[i - j:i]
            if _seg_ok(sub) and dp[i - j] is not None:
                cand = dp[i - j] + [sub]
                if best is None or len(cand) < len(best):
                    best = cand
        dp[i] = best
    if dp[n] is not None and len(dp[n]) >= 2:
        return " ".join(dp[n]).title()
    return word


def _segment_phrase(term: str) -> str:
    """Split any concatenated words in a phrase; leave normal words untouched."""
    return " ".join(_segment_word(p) if p.isalpha() else p for p in term.split())


def _drop_trailing_numbers(term: str) -> str:
    """Drop trailing pure-numeric tokens (e.g. 'Stuck on You 43' -> 'Stuck on You')."""
    parts = term.split()
    while parts and re.fullmatch(r"\d{1,4}", parts[-1]):
        parts.pop()
    return " ".join(parts)


def _expand_terms(term: str) -> List[str]:
    """Yield candidate search terms for a cleaned source label, best guess first.

    The original label is tried first (it is usually the most accurate query, e.g.
    'Wargames' correctly finds 'WarGames (1983)'). If that yields nothing we fall
    back to a word-split version ('Stuckonyou 43' -> 'Stuck on You') so that labels
    with words run together still resolve, and finally to a number-stripped form.
    """
    results: List[str] = []

    def add(s: str):
        s = s.strip()
        if s and s not in results:
            results.append(s)

    add(term)
    seg = _segment_phrase(term)
    if seg and seg != term:
        add(_drop_trailing_numbers(seg))
        add(seg)
    add(_drop_trailing_numbers(term))
    return results


def _is_film(desc: str, extract: str) -> bool:
    return bool(re.search(r"\bfilm\b", desc, re.I) or re.search(r"\bfilm\b", extract, re.I))


def _summary(page: str) -> dict:
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(page)
    return _get_json(url)


def _search(term: str) -> List[str]:
    url = ("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
           "&srlimit=8&srsearch=" + urllib.parse.quote(term + " film"))
    d = _get_json(url)
    return [x.get("title", "") for x in d.get("query", {}).get("search", []) if x.get("title")]


def _poster(result: dict) -> Optional[str]:
    thumb = (result.get("thumbnail") or {}).get("source")
    if not thumb:
        return None
    # strip the "?utm_source=..." sizing params to get the original, full-res file
    return thumb.split("?")[0]


def _parse(result: dict):
    desc = result.get("description", "") or ""
    extract = result.get("extract", "") or ""
    title = (result.get("title", "") or "").strip()
    year = _year(desc) or _year(extract)
    film = _is_film(desc, extract)
    page = ((result.get("content_urls") or {}).get("desktop") or {}).get("page")
    return {
        "title": title,
        "year": year,
        "film": film,
        "poster": _poster(result),
        "url": page,
    }


def _make_result(c: dict) -> dict:
    return {
        "title": c["title"],
        "year": c["year"],
        "poster": c.get("poster"),
        "url": c.get("url"),
        "name": f"{c['title']} ({c['year']})",
    }


def _trim_terms(term: str) -> List[str]:
    """Yield progressively shorter versions of the search term.

    Disc volume labels often carry trailing junk (e.g. 'STAR_TREK_NEMESIS_AC'),
    so we start from the full cleaned term and, on failure, drop trailing words
    one at a time until only two remain. The first variant that yields any
    plausible film page wins.
    """
    parts = term.split()
    if len(parts) <= 2:
        return [term]
    return [" ".join(parts[:n]) for n in range(len(parts), 1, -1)]


def _gather_candidates(term: str) -> List[dict]:
    """Collect plausible film pages (film + year + overlap) for a term, ranked."""
    candidates: List[dict] = []
    underscore = term.replace(" ", "_")
    try:  # direct page hit
        candidates.append(_parse(_summary(underscore)))
    except Exception:
        pass
    try:  # search fallback
        for title in _search(term):
            try:
                candidates.append(_parse(_summary(title.replace(" ", "_"))))
            except Exception:
                continue
    except Exception:
        pass

    def score(c):
        s = 0
        if c.get("film"):
            s += 4
        if c.get("year"):
            s += 3
        if c.get("poster"):
            s += 1
        if _overlap(term, c.get("title", "")):
            s += 5
        s += len(_tokens(term) & _tokens(c.get("title", ""))) * 2  # closer title ranks higher
        return s

    candidates.sort(key=score, reverse=True)
    out: List[dict] = []
    seen = set()
    for c in candidates:
        if not (c.get("year") and c.get("title") and _overlap(term, c["title"])):
            continue
        if c["title"] in seen:
            continue
        seen.add(c["title"])
        out.append(_make_result(c))
        if len(out) >= 5:  # keep the chooser list short and relevant
            break
    return out


def _gather_tmdb(term: str, limit: int = 6) -> List[dict]:
    """Search TheMovieDB for the movie title/year (+ HD poster) via the API key.

    Returns a ranked list of plausible movies (best guess first). Empty when no
    key is configured or nothing matches.
    """
    key = tmdb_key()
    if not key:
        return []
    try:
        url = ("https://api.themoviedb.org/3/search/movie?api_key=%s&query=%s&language=en-US"
               % (key, urllib.parse.quote(term)))
        d = _get_json(url)
        res = d.get("results") or []
    except Exception:  # noqa: BLE001
        return []
    out: List[dict] = []
    for r in res[:limit]:
        title = (r.get("title") or "").strip()
        year = _year(r.get("release_date") or "")
        if not (title and year):
            continue
        poster = None
        if r.get("poster_path"):
            poster = "https://image.tmdb.org/t/p/original" + r["poster_path"]
        out.append({
            "title": title,
            "year": year,
            "film": True,
            "poster": poster,
            "url": "https://www.themoviedb.org/movie/%s" % r.get("id"),
            "name": f"{title} ({year})",
        })
    return out


def search_movies(raw_term: str) -> List[dict]:
    """Return a ranked list of plausible movies for the disc (best guess first).

    Underscores are always converted to spaces before searching. Uses the
    progressively-trimmed fallback so trailing junk tokens (AC, 4K, ...) don't
    derail the search. Returns [] when nothing plausible is found.

    When a TMDb API key is configured, TheMovieDB is used first (accurate title
    + year + HD poster); otherwise Wikipedia is the no-key fallback.
    """
    term = clean_term(raw_term)
    if not term:
        return []
    for cand_term in _expand_terms(term):
        for ct in _trim_terms(cand_term):
            tmdb = _gather_tmdb(ct)
            if tmdb:
                # A TMDb key is configured and matched: use it as the clean source.
                return tmdb[:6]
            wiki = _gather_candidates(ct)
            if wiki:
                return wiki
    return []


def lookup_movie(raw_term: str) -> Optional[dict]:
    """Return the single best-guess movie, or None (kept for convenience)."""
    cands = search_movies(raw_term)
    return cands[0] if cands else None


_STOPWORDS = {"the", "a", "an", "of", "and", "for", "in", "on", "at", "to", "vs"}


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) >= 3 and w not in _STOPWORDS}


def _overlap(a: str, b: str) -> bool:
    """True if two strings share a meaningful (>=3-char, non-stopword) token."""
    ta, tb = _tokens(a), _tokens(b)
    if not tb:
        return False
    return bool(ta & tb) or (a.lower()[:3] in b.lower())


def _sidecar_path() -> str:
    """Per-user writable location for the user's own TMDb key.

    Uses %LOCALAPPDATA%\\FizzDisk (writable) rather than next to the exe, so an
    app installed under Program Files can still save the key without admin.
    """
    import sys
    base = os.environ.get("LOCALAPPDATA") or ""
    if base:
        d = os.path.join(base, "FizzDisk")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
                else os.path.dirname(os.path.abspath(__file__))
    else:
        d = os.path.join(os.path.expanduser("~"), ".fizzdisk")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
                else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(d, "tmdb_api_key.txt")


def set_tmdb_key(key: str) -> Optional[str]:
    """Persist a user-provided TMDb key to the sidecar file; returns path or None."""
    key = (key or "").strip()
    try:
        with open(_sidecar_path(), "w", encoding="utf-8") as f:
            f.write(key)
        return _sidecar_path()
    except Exception:
        return None


def tmdb_key() -> str:
    """First non-empty of env var, sidecar file, then the built-in key."""
    k = os.environ.get("TMDB_API_KEY", "").strip()
    if k:
        return k
    try:
        p = _sidecar_path()
        if p and os.path.exists(p):
            k = open(p, encoding="utf-8").read().strip()
            if k:
                return k
    except Exception:
        pass
    return _BUILTIN_TMDB_KEY


def has_user_key() -> bool:
    """True if the user supplied their OWN key (env var or sidecar file).

    Used to decide whether to show the optional 'enter your own API key'
    dialog on first run. The built-in key always works, so this only gates the
    sign-up prompt, not poster downloading.
    """
    if os.environ.get("TMDB_API_KEY", "").strip():
        return True
    try:
        p = _sidecar_path()
        if p and os.path.exists(p):
            if open(p, encoding="utf-8").read().strip():
                return True
    except Exception:
        pass
    return False


def highres_poster(title: str, year, fallback_url: Optional[str]) -> Optional[str]:
    """Return a large poster URL, using TheMovieDB when a key is configured.

    Title/year still come from the movie lookup; the poster may come from TMDb.
    If the candidate already carries a high-res TMDb URL, it is used as-is (no
    extra request). If no key is available (or the lookup fails) the Wikipedia
    ``fallback_url`` is returned unchanged.
    """
    # Already a high-res TMDb image? Use it directly.
    if fallback_url and fallback_url.startswith("https://image.tmdb.org/t/p/original"):
        return fallback_url
    key = tmdb_key()
    if not key:
        return fallback_url
    try:
        q = urllib.parse.quote((title or "").strip())
        url = f"https://api.themoviedb.org/3/search/movie?api_key={key}&query={q}&language=en-US"
        if year:
            url += f"&year={int(year)}"
        d = _get_json(url)
        res = d.get("results") or []
        if res:
            pp = (res[0].get("poster_path") or "").strip()
            if pp:
                return "https://image.tmdb.org/t/p/original" + pp
    except Exception:  # noqa: BLE001
        pass
    return fallback_url
