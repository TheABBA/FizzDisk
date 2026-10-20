# -*- coding: utf-8 -*-
"""TV show metadata lookup (show name, seasons, episodes, posters) via TMDb.

Complements ``movie.py`` (which handles movies).  A TV disc typically contains
3-6 episodes (a single season) or a full season; a movie disc has one long
feature.  The caller decides movie-vs-TV by looking at the scanned title set:
many short-ish titles (20-80 min each) => TV; one dominant long title => movie.

This module is stdlib-only (urllib), shares the same TMDb API key as ``movie.py``,
and produces the data the GUI needs to build:
    Show Name (Year)/
        Season XX/
            SxxExx - Episode Title.mkv
        Special Features/
            Show Name - Special Features N.mkv
"""
from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

from . import movie

# Reuse the same API key infrastructure (env var / sidecar / built-in).
_tmdb_key_cache: Optional[str] = None


def _key() -> str:
    global _tmdb_key_cache
    if _tmdb_key_cache is None:
        _tmdb_key_cache = movie.tmdb_key()
    return _tmdb_key_cache or ""


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "FizzDisk/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


import json  # noqa: E402  (kept here so the module header stays clean)

# Regex for stripping season/volume/disc tokens from disc labels.
# "Black Mirror s01" -> "Black Mirror", "Lost - S2 Disc 2" -> "Lost",
# "Breaking Bad Season 1" -> "Breaking Bad".
_SEASON_RE = re.compile(
    r"\b(?:s|season|vol|volume|disc|disk|complete|series)\s*\d+\b"
    r"|\b(?:bd|dvd|blu-?ray|uhd|4k)\b",
    re.IGNORECASE,
)


def clean_tv_term(term: str) -> str:
    """Strip season/volume/disc/format tokens from a disc label for TMDb search.

    TMDb's /search/tv cannot parse 'Black Mirror s01' (0 results), but
    'Black Mirror' matches.  This strips 's01', 'season 1', 'disc 2',
    'vol 3', 'bd', 'dvd', 'uhd', '4k' suffixes so the search hits the
    show name.
    """
    if not term:
        return ""
    t = term.strip()
    # Iteratively strip: "Black Mirror s01 disc 2 bd" -> "Black Mirror"
    changed = True
    while changed:
        new = _SEASON_RE.sub("", t).strip()
        changed = (new != t)
        t = new
    # Strip trailing punctuation left after token removal.
    t = re.sub(r"[\s\-_]+$", "", t).strip()
    # Collapse multiple spaces.
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ---------------------------------------------------------------------------
# TMDb API calls
# ---------------------------------------------------------------------------

def search_tv(term: str, limit: int = 6) -> List[dict]:
    """Search TMDb for a TV show by name.  Returns ranked candidates.

    Strips season/volume/disc tokens from the search term first so
    'Black Mirror s01' searches for 'Black Mirror'.

    Each candidate is a dict with keys:
        id, name, first_air_date, year, poster, overview, url, name (for display)
    """
    key = _key()
    if not key or not term:
        return []
    # Strip season/disc tokens so 'Black Mirror s01' -> 'Black Mirror'.
    clean = clean_tv_term(term)
    if not clean:
        clean = term
    try:
        url = ("https://api.themoviedb.org/3/search/tv?api_key=%s&query=%s"
               "&language=en-US&page=1"
               % (key, urllib.parse.quote(clean)))
        d = _get_json(url)
        res = d.get("results") or []
    except Exception:
        return []
    out: List[dict] = []
    for r in res[:limit]:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        year = _year(r.get("first_air_date") or "")
        poster = None
        if r.get("poster_path"):
            poster = "https://image.tmdb.org/t/p/original" + r["poster_path"]
        out.append({
            "id": r.get("id"),
            "title": name,
            "name": f"{name} ({year})" if year else name,
            "year": year,
            "first_air_date": r.get("first_air_date") or "",
            "poster": poster,
            "overview": r.get("overview") or "",
            "url": f"https://www.themoviedb.org/tv/{r.get('id')}",
            "is_tv": True,
        })
    return out


def get_show(series_id: int) -> Optional[dict]:
    """Get full show details including the season list."""
    key = _key()
    if not key:
        return None
    try:
        url = ("https://api.themoviedb.org/3/tv/%d?api_key=%s&language=en-US"
               % (series_id, key))
        d = _get_json(url)
    except Exception:
        return None
    name = (d.get("name") or "").strip()
    if not name:
        return None
    year = _year(d.get("first_air_date") or "")
    poster = None
    if d.get("poster_path"):
        poster = "https://image.tmdb.org/t/p/original" + d["poster_path"]
    seasons = []
    for s in d.get("seasons", []):
        seasons.append({
            "season_number": s.get("season_number", 0),
            "name": s.get("name") or "",
            "episode_count": s.get("episode_count", 0),
            "air_date": s.get("air_date") or "",
            "poster_path": s.get("poster_path"),
        })
    return {
        "id": d.get("id"),
        "title": name,
        "name": f"{name} ({year})" if year else name,
        "year": year,
        "first_air_date": d.get("first_air_date") or "",
        "last_air_date": d.get("last_air_date") or "",
        "number_of_seasons": d.get("number_of_seasons", 0),
        "number_of_episodes": d.get("number_of_episodes", 0),
        "poster": poster,
        "overview": d.get("overview") or "",
        "seasons": seasons,
        "is_tv": True,
    }


def get_season_episodes(series_id: int, season_number: int) -> List[dict]:
    """Get all episodes for a single season.

    Each episode dict:
        episode, season, name, runtime (sec), runtime_min, air_date,
        overview, still, id
    """
    key = _key()
    if not key:
        return []
    try:
        url = ("https://api.themoviedb.org/3/tv/%d/season/%d?api_key=%s"
               "&language=en-US" % (series_id, season_number, key))
        d = _get_json(url)
    except Exception:
        return []
    out = []
    for ep in d.get("episodes", []):
        rt = ep.get("runtime") or 0
        out.append({
            "id": ep.get("id"),
            "episode": ep.get("episode_number", 0),
            "season": season_number,
            "name": (ep.get("name") or "").strip() or f"Episode {ep.get('episode_number')}",
            "runtime_min": rt,
            "runtime_sec": int(rt * 60) if rt else 0,
            "air_date": ep.get("air_date") or "",
            "overview": ep.get("overview") or "",
            "still": ep.get("still_path"),
        })
    return out


def get_all_episodes(series_id: int, skip_specials: bool = True) -> List[dict]:
    """Get every episode across all seasons (one TMDb call per season)."""
    show = get_show(series_id)
    if not show:
        return []
    all_eps = []
    for s in show.get("seasons", []):
        sn = s["season_number"]
        if skip_specials and sn == 0:
            continue
        eps = get_season_episodes(series_id, sn)
        all_eps.extend(eps)
    return all_eps


# ---------------------------------------------------------------------------
# Disc-title → episode matching
# ---------------------------------------------------------------------------

# Minimum duration (in seconds) for a disc title to be considered a real
# episode rather than a menu/extra/bumper.  Most TV episodes are 20+ min;
# anything shorter is almost certainly not a full episode.
MIN_EPISODE_SECONDS = 15 * 60  # 15 minutes

# Tolerance for matching disc-title duration to episode runtime.
# ±4 min covers NTSC/PAL speedup, broadcast vs streaming cuts, and rounding.
DURATION_TOLERANCE_SEC = 4 * 60  # 4 minutes


def looks_like_tv(titles: List[dict]) -> bool:
    """Heuristic: does this disc's title set look like a TV show?

    A TV disc has *multiple* titles of similar episode-length duration
    (20-80 min), and NO single dominant feature title.  A movie disc has
    ONE very long feature (typically 90+ min) plus shorter bonus features
    (deleted scenes / making-ofs) — those extras can also fall in the
    20-90 min window, so a naive "2+ titles in range" check misclassifies
    movies with a couple of extras as TV shows.

    ``titles`` is a list of dicts with at least ``duration`` (seconds, float).
    """
    if len(titles) < 2:
        return False

    # Dominant-feature guard: a movie disc has exactly one title that is far
    # longer than everything else (the feature film). A TV disc's titles are
    # all episode-length and comparable, so no single title dominates.
    # If the longest title is feature-length AND at least 1.5x the
    # second-longest, this is a single movie with bonus features, not a set
    # of episodes. (TV episodes rarely exceed 90 min, so the length cap also
    # protects season discs that have one unusually long episode.)
    durs = sorted([(t.get("duration") or 0) for t in titles], reverse=True)
    longest, second = durs[0], durs[1]
    if longest >= 90 * 60 and longest >= 1.5 * second:
        return False

    # Count titles in the episode-length range (20-90 min).
    ep_like = [t for t in titles
               if 20 * 60 <= (t.get("duration") or 0) <= 90 * 60]
    if len(ep_like) >= 2:
        return True
    # Also detect: 3+ titles each >= 15 min (some shows have shorter eps).
    med = [t for t in titles if (t.get("duration") or 0) >= MIN_EPISODE_SECONDS]
    if len(med) >= 3:
        return True
    return False


def match_episodes(
    disc_titles: List[dict],
    episodes: List[dict],
    tolerance_sec: int = DURATION_TOLERANCE_SEC,
) -> List[dict]:
    """Match disc titles to TMDb episodes by duration + order.

    Strategy (multi-pass, best result wins):
      1. Filter disc titles to real episodes (>= MIN_EPISODE_SECONDS).
      2. For each *season* in the episode list, try an ordered pairing:
         disc candidates[i] ↔ season_eps[i].  Count how many match within
         tolerance.  The season with the most ordered matches wins — this
         keeps episodes in the same season and in order, which is the common
         case (a disc holds consecutive episodes from one season).
      3. If no season gets a majority of ordered matches, fall back to greedy
         nearest-duration matching across all episodes (handles anthology /
         out-of-order discs).

    Returns a list of match dicts (see ``_make_match``).
    """
    candidates = sorted(
        [t for t in disc_titles if (t.get("duration") or 0) >= MIN_EPISODE_SECONDS],
        key=lambda t: t.get("index", 0),
    )
    if not candidates or not episodes:
        return []

    # Group episodes by season.
    by_season: Dict[int, List[dict]] = {}
    for ep in episodes:
        sn = ep.get("season", 0)
        by_season.setdefault(sn, []).append(ep)
    for sn in by_season:
        by_season[sn].sort(key=lambda e: e.get("episode", 0))

    # Phase 1: for each season, try every starting offset (disc may start at
    # any episode, not just ep 1).  Pick the (season, offset) with the most
    # in-tolerance matches.  This keeps episodes in the same season and in
    # order, which is the common case (a disc holds consecutive episodes).
    best_score = 0
    best_matches: List[dict] = []
    for sn, eps in by_season.items():
        if sn == 0 or not eps:
            continue
        n_cand = len(candidates)
        for offset in range(max(1, len(eps) - n_cand + 1)):
            score = 0
            matches: List[dict] = []
            ok = True
            for i, t in enumerate(candidates):
                ei = offset + i
                if ei >= len(eps):
                    matches.append(_make_match(t, None, "unmatched"))
                    ok = False
                    break
                ep = eps[ei]
                t_dur = t.get("duration") or 0
                ep_dur = ep.get("runtime_sec") or 0
                if ep_dur and abs(t_dur - ep_dur) <= tolerance_sec:
                    score += 1
                elif not ep_dur:
                    score += 0.5
                else:
                    ok = False  # this offset doesn't work
                matches.append(_make_match(t, ep, "ordered"))
            if ok and score > best_score:
                best_score = score
                best_matches = matches
            elif score > best_score and not best_matches:
                # Even a non-perfect offset is better than nothing.
                best_score = score
                best_matches = matches

    # Accept ordered matching if the majority of candidates matched.
    if best_matches and best_score >= max(1, len(candidates) // 2):
        return best_matches

    # Phase 2: greedy nearest-duration across all episodes.
    eps_sorted = sorted(episodes, key=lambda e: (e.get("season", 0),
                                                   e.get("episode", 0)))
    used = set()
    matches = []
    for t in candidates:
        t_dur = t.get("duration") or 0
        best_ep = None
        best_diff = float("inf")
        for ep in eps_sorted:
            if id(ep) in used:
                continue
            ep_dur = ep.get("runtime_sec") or 0
            if not ep_dur:
                continue
            diff = abs(t_dur - ep_dur)
            if diff < best_diff:
                best_diff = diff
                best_ep = ep
        if best_ep and best_diff <= tolerance_sec:
            used.add(id(best_ep))
            matches.append(_make_match(t, best_ep, "duration"))
        else:
            matches.append(_make_match(t, None, "unmatched"))
    return matches


def _make_match(t: dict, ep: Optional[dict], method: str) -> dict:
    return {
        "disc_index": t.get("index", 0),
        "disc_title": t.get("name") or "",
        "disc_duration": t.get("duration") or 0,
        "episode": ep,
        "season": (ep or {}).get("season"),
        "episode_number": (ep or {}).get("episode"),
        "name": (ep or {}).get("name"),
        "runtime_sec": (ep or {}).get("runtime_sec"),
        "method": method,
    }


# ---------------------------------------------------------------------------
# Folder / filename builders
# ---------------------------------------------------------------------------

_BAD_CHARS = '<>:"/\\|?*'


def _safe(s: str) -> str:
    """Strip Windows-illegal characters."""
    return "".join("_" if c in _BAD_CHARS else c for c in (s or "")).strip() or "title"


def build_tv_folder(show: dict) -> str:
    """Show Name (Year) — the top-level folder name."""
    name = show.get("title") or show.get("name") or "TV Show"
    year = show.get("year")
    if year:
        return _safe(f"{name} ({year})")
    return _safe(name)


def build_season_folder(season_number: int) -> str:
    """Season XX."""
    return _safe(f"Season {season_number:02d}")


def build_episode_filename(season: int, episode: int, name: str,
                           ext: str = ".mkv") -> str:
    """SxxExx - Episode Title.ext."""
    base = _safe(f"S{season:02d}E{episode:02d} - {name}")
    if not base.endswith(ext):
        base += ext
    return base


def build_special_features_filename(show_name: str, index: int,
                                     ext: str = ".mkv") -> str:
    """Show Name - Special Features N.ext."""
    return _safe(f"{show_name} - Special Features {index}{ext}")


# ---------------------------------------------------------------------------
# Full identification pipeline
# ---------------------------------------------------------------------------

def identify_tv_disc(
    term: str,
    disc_titles: List[dict],
    series_id: Optional[int] = None,
) -> Optional[dict]:
    """Identify a TV disc and match its titles to episodes.

    1. Search TMDb for the show by name (or use ``series_id`` directly).
    2. Fetch all episodes.
    3. Match disc titles to episodes by duration + order.
    4. Return a structure the GUI can use to build folders + filenames.

    Returns None if the show cannot be found or no titles match.

    The returned dict:
        {
            "show": {show dict from get_show()},
            "matches": [match dicts],
            "folder": "Show Name (Year)",
            "seasons": {
                season_number: {
                    "folder": "Season XX",
                    "episodes": [match dicts for that season],
                },
            },
            "special_features": [disc_titles that didn't match any episode],
        }
    """
    # 1. Find the show.
    if series_id:
        show = get_show(series_id)
    else:
        cands = search_tv(term)
        if not cands:
            return None
        show = get_show(cands[0]["id"])
    if not show:
        return None

    # 2. Get all episodes.
    all_eps = get_all_episodes(show["id"])
    if not all_eps:
        return None

    # 3. Match.
    matches = match_episodes(disc_titles, all_eps)

    # 4. Build the season structure.
    seasons: Dict[int, dict] = {}
    sf_titles: List[dict] = []
    for t in disc_titles:
        dur = t.get("duration") or 0
        is_ep = any(m["disc_index"] == t.get("index") and m["episode"] is not None
                    for m in matches)
        if not is_ep:
            # Unmatched but could be a real short extra or a menu.
            sf_titles.append(t)

    for m in matches:
        if m["episode"] is None:
            continue
        sn = m["season"]
        if sn not in seasons:
            seasons[sn] = {
                "folder": build_season_folder(sn),
                "episodes": [],
            }
        seasons[sn]["episodes"].append(m)

    return {
        "show": show,
        "matches": matches,
        "folder": build_tv_folder(show),
        "seasons": seasons,
        "special_features": sf_titles,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _year(date_str: str) -> Optional[int]:
    if not date_str:
        return None
    m = re.match(r"(\d{4})", date_str)
    return int(m.group(1)) if m else None
