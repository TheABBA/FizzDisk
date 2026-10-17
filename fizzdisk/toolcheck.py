# -*- coding: utf-8 -*-
"""Bundled-tool integrity checks.

Antivirus (notably Windows Defender) false-positives on the large UNSIGNED
binaries we ship and silently DELETES or quarantines them after install --
observed in the wild as `Trojan:Win32/Wacatac.B!ml` on the installer, leaving
`_internal/bin/` with ffmpeg.exe present but **ffprobe.exe gone**.

That failure is nasty because it does not look like a missing file: the rip
itself succeeds (ffmpeg is still there), then the duration VERIFICATION call
raises, the caller reads the failed probe as "the rip came out short", and
FizzDisk launches the slow disc-copy fallback -- which re-rips and verifies
with the same missing tool and fails again. The user sees an endless
"copying the disc" loop and a truncation warning on a rip that was perfect.

So: check the tools up front, name the missing one, and tell the user it is
an AV deletion with the exact remedy.
"""
from __future__ import annotations

import os
from typing import List, Tuple

from .ffutil import FFMPEG, FFPROBE

#: Tools that must exist for a rip + its verification to work.
REQUIRED_TOOLS: List[Tuple[str, str]] = [
    ("ffmpeg", FFMPEG),
    ("ffprobe", FFPROBE),
]


def missing_tools() -> List[Tuple[str, str]]:
    """Return [(name, path), ...] for every required tool that is absent."""
    out: List[Tuple[str, str]] = []
    for name, path in REQUIRED_TOOLS:
        try:
            if not path or not os.path.isfile(path):
                out.append((name, path or f"<{name} path unresolved>"))
        except OSError:
            out.append((name, path or f"<{name} path unresolved>"))
    return out


def tools_ok() -> bool:
    return not missing_tools()


def missing_tools_message() -> str:
    """User-facing explanation naming the missing tools and the fix."""
    miss = missing_tools()
    if not miss:
        return ""
    names = ", ".join(n for n, _ in miss)
    where = os.path.dirname(miss[0][1]) or "the FizzDisk program folder"
    return (
        f"FizzDisk is missing a required tool: {names}.\n\n"
        f"Expected in:\n{where}\n\n"
        "This is almost always antivirus. FizzDisk is not code-signed yet, so "
        "Windows Defender sometimes flags its bundled video tools as a false "
        "positive and deletes them after installation.\n\n"
        "To fix it:\n"
        "1. Open Windows Security > Virus & threat protection > Protection history "
        "and Restore any blocked FizzDisk item, or\n"
        "2. Add an exclusion for the FizzDisk program folder "
        "(Virus & threat protection > Manage settings > Exclusions), then "
        "reinstall FizzDisk.\n\n"
        "Until the tool is restored, rips cannot be verified and may be "
        "incorrectly reported as incomplete."
    )
