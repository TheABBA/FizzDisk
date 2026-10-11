# -*- coding: utf-8 -*-
"""Command-line harness for FizzDisk (also used to test the GUI engine).

Usage:
  python -m fizzdisk.cli scan <path>
  python -m fizzdisk.cli mux <path> [--title N] [--out DIR] [--audio i,j] [--subs i,j]
  python -m fizzdisk.cli rip <path> [--format flac|mp3] [--artist A] [--album B] [--track N] [--out DIR]
"""
from __future__ import annotations

import argparse
import os
import sys

from . import engine, ffutil
from .model import Title


def _print_titles(titles):
    print(f"Found {len(titles)} title(s):")
    for t in titles:
        v = t.video.codec_name if t.video else "-"
        a = len(t.audio)
        s = len(t.subtitles)
        mark = " [MAIN]" if t.is_main else ""
        print(f"  #{t.index}: {t.name}{mark}  {t.nice_duration}  video={v} audio={a} subs={s}")


def cmd_scan(args):
    res = engine.scan_source(args.path)
    print(res.info)
    _print_titles(res.titles)
    return 0


def cmd_mux(args):
    res = engine.scan_source(args.path)
    titles = res.titles
    if not titles:
        print("no titles")
        return 1
    idx = args.title if args.title is not None else 0
    title = titles[idx]
    out_dir = args.out or "."
    out = os.path.join(out_dir, f"{title.name}.mkv")
    video = not args.no_video
    audio_idx = [int(x) for x in args.audio.split(",")] if args.audio else list(range(len(title.audio)))
    sub_idx = [int(x) for x in args.subs.split(",")] if args.subs else list(range(len(title.subtitles)))
    print(f"Ripping {title.name} -> {out}")
    if args.backend:
        produced = engine.disc_rip(args.path, title.index, out_dir)
        print("produced:", produced)
        return 0 if produced else 1
    rc = ffutil.remux(title, out, video=video, audio_idx=audio_idx, sub_idx=sub_idx)
    print("ffmpeg rc:", rc)
    if rc == 0:
        val = ffutil.validate_mkv(out)
        print("validate:", {k: val[k] for k in ("ok", "duration")},
              "streams:", [(s.codec_type, s.codec_name) for s in val["streams"]])
    return 0 if rc == 0 else 1


def cmd_rip(args):
    res = engine.scan_source(args.path)
    if res.source_kind != "cd":
        print("Not an audio CD (kind=%r)" % res.source_kind)
        return 1
    meta = res.meta or {}
    artist = args.artist or meta.get("artist") or "Unknown Artist"
    album = args.album or meta.get("album") or "Unknown Album"
    fmt = args.format or "flac"
    selected = {args.track} if args.track is not None else None
    out = args.out or "."
    print(f"Ripping CD ({len(res.titles)} tracks) -> {out}/{artist}/{album} [{fmt}]")

    def _prog(f):
        print(f"\r   {int(f * 100)}%", end="", flush=True)

    try:
        produced = engine.rip_cd(
            engine._cd_letter(args.path), res.titles, out, fmt=fmt,
            artist=artist, album=album,
            release_mbid=meta.get("release_mbid"),
            selected=selected, progress_cb=_prog, stop_event=None,
        )
    except Exception as e:  # noqa: BLE001
        print(f"rip error: {e}")
        return 1
    print()
    print("produced:", produced)
    return 0 if produced else 1


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(prog="fizzdisk.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("path")
    m = sub.add_parser("mux")
    m.add_argument("path")
    m.add_argument("--title", type=int, default=0)
    m.add_argument("--out", default=".")
    m.add_argument("--audio", default=None)
    m.add_argument("--subs", default=None)
    m.add_argument("--no-video", action="store_true")
    m.add_argument("--backend", action="store_true")
    r = sub.add_parser("rip")
    r.add_argument("path")
    r.add_argument("--format", choices=["flac", "mp3"], default="flac")
    r.add_argument("--artist", default=None)
    r.add_argument("--album", default=None)
    r.add_argument("--track", type=int, default=None)
    r.add_argument("--out", default=".")
    args = p.parse_args(argv)
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "rip":
        return cmd_rip(args)
    return cmd_mux(args)


if __name__ == "__main__":
    sys.exit(main())
