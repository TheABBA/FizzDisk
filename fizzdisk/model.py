# -*- coding: utf-8 -*-
"""Shared data model for FizzDisk."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class Stream:
    """One elementary stream discovered on a source / title."""
    index: int                 # index within the assembled input stream list
    codec_type: str            # 'video' | 'audio' | 'subtitle' | 'data' | 'attachment'
    codec_name: str = ""
    channels: Optional[int] = None
    language: Optional[str] = None
    title: Optional[str] = None
    default: bool = False
    forced: bool = False
    disposition: dict = field(default_factory=dict)
    id: Optional[str] = None   # ffprobe stream id (hex string, e.g. '0x1100')

    @property
    def label(self) -> str:
        if self.codec_type == 'audio':
            ch = f" {self.channels}ch" if self.channels else ""
            lang = f" [{self.language}]" if self.language else ""
            return f"Audio {self.codec_name}{ch}{lang}"
        if self.codec_type == 'subtitle':
            lang = f" [{self.language}]" if self.language else ""
            return f"Subtitle {self.codec_name}{lang}"
        if self.codec_type == 'video':
            return f"Video {self.codec_name}"
        return f"{self.codec_type} {self.codec_name}"


@dataclass
class Clip:
    """A contiguous media segment making up (part of) a title."""
    files: List[str]               # ordered list of files; usually length 1
    start: Optional[float] = None  # in-point seconds (None = beginning)
    end: Optional[float] = None    # out-point seconds (None = end)


@dataclass
class Title:
    """A disc/title the user can rip. Maps to one output MKV."""
    index: int
    name: str
    clips: List[Clip]
    duration: float = 0.0
    size_bytes: int = 0
    source_kind: str = ""          # 'file'|'dvd'|'bd'|'iso'
    is_main: bool = False
    video: Optional[Stream] = None
    audio: List[Stream] = field(default_factory=list)
    subtitles: List[Stream] = field(default_factory=list)
    data: List[Stream] = field(default_factory=list)
    chapters: List[dict] = field(default_factory=list)
    raw_streams: List[dict] = field(default_factory=list)   # raw ffprobe dicts
    info: Any = ""                # string log info, or a dict for cd/BD titles

    @property
    def selected_streams(self) -> List[Stream]:
        s: List[Stream] = []
        if self.video:
            s.append(self.video)
        s.extend(self.audio)
        s.extend(self.subtitles)
        return s

    @property
    def nice_duration(self) -> str:
        return _fmt_duration(self.duration)


def _fmt_duration(sec: float) -> str:
    if not sec:
        return "0:00"
    sec = int(round(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


@dataclass
class ScanResult:
    titles: List[Title]
    source: str
    source_kind: str
    info: str = ""
    errors: List[str] = field(default_factory=list)
    meta: Optional[dict] = None  # audio-CD album metadata (artist/album/tracks)
