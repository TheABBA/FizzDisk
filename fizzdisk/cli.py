# -*- coding: utf-8 -*-
"""Command-line harness for FizzDisk (also used to test the GUI engine).

Usage:
  python -m fizzdisk.cli scan <path>
  python -m fizzdisk.cli mux <path> [--title N] [--out DIR] [--audio i,j] [--subs i,j]
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
    args = p.parse_args(argv)
    return cmd_scan(args) if args.cmd == "scan" else cmd_mux(args)


if __name__ == "__main__":
    sys.exit(main())
