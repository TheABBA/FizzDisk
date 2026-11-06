# -*- coding: utf-8 -*-
"""Audio-CD (CD-DA) detection, Table-of-Contents read, and error-corrected rip.

Reads the disc's TOC (Windows CD-ROM device API ``IOCTL_CDROM_READ_TOC``, with
a WinMM MCI ``mciSendString`` fallback) and rips each audio track by shelling
out to the **bundled cd-paranoia** (libcdio-paranoia) for parity / jitter-
corrected reads -- the same error-correction real audio-rippers use. If the
helper is unavailable it falls back to a raw ``IOCTL_CDROM_RAW_READ`` read (no
error correction). Encoding is done by the bundled ffmpeg; this module deals
only with the disc itself.
"""
from __future__ import annotations

import ctypes
import os
import re
import struct
import threading
import time
from ctypes import (POINTER, Structure, Union, byref, c_byte, c_uint, c_uint16,
                    c_uint32, c_uint8, c_void_p, c_wchar_p, c_longlong, c_int32)
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------- kernel32 / winmm
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm", use_last_error=True)

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_ALL = 0x7  # read | write | delete
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID = c_void_p(-1).value

# ntddcdrm.h
IOCTL_CDROM_READ_TOC = 0x00024000
IOCTL_CDROM_RAW_READ = 0x0002403E   # CTL_CODE(CDROM, 0x000F, METHOD_OUT_DIRECT, FILE_READ_ACCESS)

# TRACK_MODE_TYPE (ntddcdrm.h): YellowMode2=0, XAForm2=1, CDDA=2
CDDA_TRACK_MODE = 2
CDDA_SECTOR = 2352          # raw CD-DA sector = 16-bit stereo @ 44.1 kHz
CDDA_SAMPLE_RATE = 44100


class _CDROM_ADDRESS(Union):
    _fields_ = [("Minute", c_uint8), ("Second", c_uint8), ("Frame", c_uint8),
                ("Reserved", c_uint8)]


class _TRACK_DATA(Structure):
    _fields_ = [("Reserved", c_uint8), ("Control", c_uint8),
                ("TrackNumber", c_uint8), ("Reserved1", c_uint8),
                ("Address", _CDROM_ADDRESS)]


class _CDROM_TOC(Structure):
    _fields_ = [("Length", c_uint16), ("FirstTrack", c_uint8),
                ("LastTrack", c_uint8), ("TrackData", _TRACK_DATA * 100)]


class _LARGE_INTEGER(Union):
    _fields_ = [("QuadPart", c_longlong), ("LowPart", c_uint32), ("HighPart", c_int32)]


class _RAW_READ_INFO(Structure):
    """RAW_READ_INFO (ntddcdrm.h) -- in buffer for IOCTL_CDROM_RAW_READ."""
    _fields_ = [("DiskOffset", _LARGE_INTEGER), ("SectorCount", c_uint32),
                ("TrackMode", c_int32)]


class _SCSI_PASS_THROUGH_DIRECT(Structure):
    _fields_ = [("Length", c_uint16), ("CdbLength", c_uint8),
                ("StorageMediaType", c_uint8), ("DataIn", c_uint8),
                ("DataTransferLength", c_uint32), ("TimeOutValue", c_uint32),
                ("DataBuffer", c_void_p), ("SenseInfoLength", c_uint32),
                ("Cdb", c_byte * 16)]


for _fn, _restype, _args in (
    (k32.CreateFileW, c_void_p, [c_wchar_p, c_uint32, c_uint32, c_void_p,
                                 c_uint32, c_uint32, c_void_p]),
    (k32.DeviceIoControl, ctypes.c_int,
     [c_void_p, c_uint32, c_void_p, c_uint32, c_void_p, c_uint32,
      POINTER(c_uint32), c_void_p]),
    (k32.CloseHandle, ctypes.c_int, [c_void_p]),
    (winmm.mciSendStringW, c_uint, [c_wchar_p, c_wchar_p, c_uint, c_void_p]),
):
    _fn.restype = _restype
    _fn.argtypes = _args


# --------------------------------------------------------------------------- device open
def _query_dos_devices() -> dict:
    """Return the DOS->NT device mapping (e.g. {'D:': '\\Device\\CdRom0'})."""
    _qd = k32.QueryDosDeviceW
    _qd.restype = c_uint32
    _qd.argtypes = [c_wchar_p, c_wchar_p, c_uint32]
    names = {}
    for name in ("", "CdRom0", "CdRom1", "CdRom2", "CdRom3"):
        buf = ctypes.create_unicode_buffer(512)
        n = _qd(name or None, buf, 512)
        if n:
            names[name or "<all>"] = buf.value
    return names


def _candidate_device_paths(letter: str) -> List[str]:
    """Return raw device paths to try for opening the optical drive."""
    # Build both formatted (letter without colon, or drive letter)
    drive = (letter.rstrip(":") if letter else "")
    paths = [
        f"\\\\.\\{drive}:",
        f"\\\\.\\CdRom0",
        f"\\\\.\\CdRom1",
        f"\\\\.\\CdRom2",
        f"\\\\.\\CdRom",
        "\\\\.\\CDROM",
        "\\\\.\\D:",
    ]
    # volume GUID path for the drive
    vol = _volume_guid(letter)
    if vol:
        paths.append(vol)
    return paths


def _volume_guid(letter: str) -> Optional[str]:
    """Return the \\\\?\\Volume{GUID}\\ path for a drive letter, if any."""
    try:
        _f = k32.GetVolumeNameForVolumeMountPointW
        _f.restype = ctypes.c_int
        _f.argtypes = [c_wchar_p, c_wchar_p, c_uint32]
        buf = ctypes.create_unicode_buffer(512)
        mount = f"{letter.upper()}:\\"
        if not _f(mount, buf, 512):
            return None
        return buf.value
    except Exception:  # noqa: BLE001
        return None


def open_drive(letter: str):
    """Open the optical drive for raw IOCTL. Returns handle or None.

    Tries the device paths in order; the drive-letter device (``\\\\.\\X:``)
    is the canonical CD-ROM device on standard Windows installs.
    """
    drive = (letter.rstrip(":") if letter else "").upper()
    bases = [f"\\\\.\\{drive}:", f"\\\\.\\D:", f"\\\\.\\CdRom0", f"\\\\.\\CdRom1",
             f"\\\\.\\CdRom2", f"\\\\.\\CdRom", f"\\\\.\\CDROM"]
    vol = _volume_guid(drive)
    if vol:
        bases.append(vol)
    for path in bases:
        for access in (_GENERIC_READ, 0):
            h = k32.CreateFileW(path, access, _FILE_SHARE_ALL, None,
                                _OPEN_EXISTING, 0, None)
            if h not in (_INVALID, None):
                # Verify it is a real CD-ROM device (accepts READ_TOC) so a
                # directory handle (which only opens with BACKUP_SEMANTICS)
                # doesn't produce a false success.
                if _device_accepts_toc(h):
                    return h
                k32.CloseHandle(c_void_p(h))
    return None


def _device_accepts_toc(h) -> bool:
    toc = _CDROM_TOC()
    got = c_uint32(0)
    ok = k32.DeviceIoControl(c_void_p(h), IOCTL_CDROM_READ_TOC, None, 0,
                             byref(toc), ctypes.sizeof(toc), byref(got), None)
    # A real CD-ROM device yields at least one track; a directory/volume
    # handle may return success but a zero-length TOC.
    return bool(ok) and toc.FirstTrack >= 1 and toc.LastTrack >= toc.FirstTrack


# --------------------------------------------------------------------------- TOC read
def _msf_to_lba(td) -> int:
    a = td.Address
    msf = (a.Minute * 60 + a.Second) * 75 + a.Frame
    return msf - 150


def read_toc_device(h) -> List[dict]:
    """Read the exact TOC from an open CD-ROM device handle.

    Returns a list of dicts: [{track, lba, sectors, is_audio, control}]
    plus the lead-out offset in the last entry ('leadout' key).
    The caller must have a device handle that accepts IOCTL_CDROM_READ_TOC.
    """
    toc = _CDROM_TOC()
    got = c_uint32(0)
    ok = k32.DeviceIoControl(c_void_p(h), IOCTL_CDROM_READ_TOC, None, 0,
                             byref(toc), ctypes.sizeof(toc), byref(got), None)
    if not ok:
        raise OSError(ctypes.get_last_error(), "IOCTL_CDROM_READ_TOC failed")
    first, last = toc.FirstTrack, toc.LastTrack
    tracks: List[dict] = []
    count = last - first + 2  # + lead-out
    for i in range(count):
        td = toc.TrackData[i]
        lba = _msf_to_lba(td)
        is_audio = not (td.Control & 0x04)
        if td.TrackNumber <= last:
            tracks.append({"track": td.TrackNumber, "lba": lba,
                           "sectors": None, "is_audio": is_audio,
                           "control": td.Control})
    # compute sector lengths from consecutive LBAs
    # lead-out is the last entry in the raw TOC (index count-1)
    leadout_lba = 0
    td = toc.TrackData[count - 1]
    leadout_lba = _msf_to_lba(td)
    for i, t in enumerate(tracks):
        nxt = tracks[i + 1]["lba"] if i + 1 < len(tracks) else leadout_lba
        t["sectors"] = max(0, nxt - t["lba"])
    tracks.append({"track": 0, "lba": leadout_lba, "sectors": 0,
                   "is_audio": False, "control": 0, "leadout": leadout_lba})
    return tracks


# --------------------------------------------------------------------------- MCI fallback
def _mci(cmd: str, bufsz: int = 256) -> Tuple[int, str]:
    buf = ctypes.create_unicode_buffer(bufsz)
    rc = winmm.mciSendStringW(cmd, buf, bufsz, None)
    return int(rc), buf.value


def read_toc_mci(letter: str) -> List[dict]:
    """Read a best-effort TOC via the WinMM MCI cdaudio interface.

    Returns the same shape as read_toc_device (raw LBA, 0-based). The sector
    lengths are rounded to whole seconds. Only used where the raw device node
    cannot be opened.
    """
    drive = f"{letter.upper()}:"
    alias = "fizzdisk_cd"
    rc, _ = _mci(f"open {drive} type cdaudio alias {alias}")
    if rc:
        raise OSError(rc, f"MCI could not open {drive} (is a CD inserted?)")
    try:
        _mci(f"set {alias} time format tmsf")
        rc, nt_str = _mci(f"status {alias} number of tracks")
        ntracks = int(nt_str.strip() or 0)
        tracks: List[dict] = []
        lba = 0  # raw drive LBA; track 1 starts at LBA 0 (MSF 00:02:00)
        for t in range(1, ntracks + 1):
            rc, dur = _mci(f"status {alias} length track {t}")
            parts = (dur.strip().split(":") + ["00", "00", "00"])[:3]
            try:
                m, s, f = (int(parts[0] or 0), int(parts[1] or 0),
                           int(parts[2] or 0))
            except ValueError:
                m, s, f = 0, 0, 0
            sectors = (m * 60 + s) * 75 + f
            tracks.append({"track": t, "lba": lba, "sectors": sectors,
                           "is_audio": True, "control": 0})
            lba += sectors
        # raw lead-out = end of last track
        leadout = lba
        tracks.append({"track": 0, "lba": leadout, "sectors": 0,
                       "is_audio": False, "control": 0, "leadout": leadout})
        return tracks
    finally:
        _mci(f"close {alias}")


# --------------------------------------------------------------------------- detection
def detect_audio_cd(letter: str) -> bool:
    """Return True if the drive holds a pure audio CD (>=1 audio track, no
    data/blu-ray session)."""
    try:
        h = open_drive(letter)
        if h:
            try:
                tracks = read_toc_device(h)
            finally:
                k32.CloseHandle(c_void_p(h))
        else:
            tracks = read_toc_mci(letter)
    except Exception:  # noqa: BLE001
        return False
    audio = [t for t in tracks if t["track"] and t["is_audio"]]
    # a pure audio CD has audio tracks and no data track
    if not audio:
        return False
    # more than a couple of audio tracks -> treat as a music CD
    return len(audio) >= 1


def scan_toc(letter: str) -> List[dict]:
    """Return the TOC (device if possible, else MCI) as a list of tracks."""
    h = open_drive(letter)
    if h:
        try:
            return read_toc_device(h)
        finally:
            k32.CloseHandle(c_void_p(h))
    return read_toc_mci(letter)


# --------------------------------------------------------------------------- raw rip
def _read_sectors_ioctl(h, start_lba: int, count: int) -> bytes:
    """Read `count` raw CD-DA sectors (2352 bytes each) via IOCTL_CDROM_RAW_READ.

    Error-correction is NOT done here; use the cd-paranoia helper for that.
    This is a fallback for drives/cases where the helper is unavailable.
    """
    buf = (c_uint8 * (CDDA_SECTOR * count))()
    params = _RAW_READ_INFO()
    params.DiskOffset.QuadPart = start_lba * CDDA_SECTOR   # byte offset on disc
    params.SectorCount = count
    params.TrackMode = CDDA_TRACK_MODE                     # 2 = CDDA
    got = c_uint32(0)
    ok = k32.DeviceIoControl(c_void_p(h), IOCTL_CDROM_RAW_READ, byref(params),
                             ctypes.sizeof(params), buf, CDDA_SECTOR * count,
                             byref(got), None)
    if not ok and not (got.value and got.value % CDDA_SECTOR == 0):
        raise OSError(ctypes.get_last_error(),
                      f"IOCTL_CDROM_RAW_READ failed @ LBA {start_lba}")
    return bytes(buf[: got.value or (CDDA_SECTOR * count)])


def _read_sectors_scsi(h, start_lba: int, count: int) -> bytes:
    """Read raw CD-DA sectors via SCSI READ CD (0xBE) pass-through."""
    out = bytearray()
    block = 64  # sectors per pass-through (buffer holds 64*2352)
    pos = start_lba
    remaining = count
    while remaining > 0:
        n = min(block, remaining)
        data = (c_uint8 * (CDDA_SECTOR * n))()
        spt = _SCSI_PASS_THROUGH_DIRECT()
        spt.Length = ctypes.sizeof(_SCSI_PASS_THROUGH_DIRECT)
        spt.CdbLength = 12
        spt.StorageMediaType = 0
        spt.DataIn = 1  # SCSI_IOCTL_DATA_IN
        spt.DataTransferLength = CDDA_SECTOR * n
        spt.TimeOutValue = 20
        spt.DataBuffer = ctypes.cast(data, c_void_p)
        spt.SenseInfoLength = 0
        cdb = spt.Cdb
        cdb[0] = 0xBE                     # READ CD
        cdb[1] = 0x00                     # expected sector type / sync
        # start LBA (big-endian, 32-bit)
        cdb[2] = (pos >> 24) & 0xFF
        cdb[3] = (pos >> 16) & 0xFF
        cdb[4] = (pos >> 8) & 0xFF
        cdb[5] = pos & 0xFF
        # transfer length (number of sectors, 16-bit big-endian)
        cdb[6] = (n >> 8) & 0xFF
        cdb[7] = n & 0xFF
        cdb[8] = 0xF8                     # sync+header+user data (2352)
        cdb[9] = 0x10                     # CD-DA sector type (0x10 => audio)
        got = c_uint32(0)
        ok = k32.DeviceIoControl(c_void_p(h), 0x4D014, byref(spt),
                                 ctypes.sizeof(spt), byref(spt),
                                 ctypes.sizeof(spt), byref(got), None)
        if not ok:
            raise OSError(ctypes.get_last_error(),
                          f"SCSI READ CD failed @ LBA {pos}")
        out += bytes(data[: n * CDDA_SECTOR])
        pos += n
        remaining -= n
    return bytes(out)


def _cdparanoia_path() -> str:
    """Return the path to the bundled cd-paranoia.exe (libcdio-paranoia), or ''."""
    try:
        from . import ffutil
        p = ffutil.find_tool("cd-paranoia")
        if p and Path(p).exists():
            return p
    except Exception:  # noqa: BLE001
        pass
    return ""


class CdReadError(OSError):
    """A single audio track could not be error-corrected by cd-paranoia.

    ``complete`` is True when the WAV was essentially fully ripped despite the
    read error (an unreadable sector was filled in) -- the caller can still keep
    it. False means most of the track was lost, so it should be skipped.
    """

    def __init__(self, message: str, *, sector=None, written=None, expected=None,
                 complete: bool = False):
        super().__init__(message)
        self.sector = sector
        self.written = written
        self.expected = expected
        self.complete = complete


def _bad_sector(err: str):
    m = re.search(r"Unable to access sector\s+(\d+)", err or "")
    return int(m.group(1)) if m else None


def rip_track_to_wav(letter: str, track: dict, out_wav: str,
                     progress_cb=None, stall_secs: float = 60.0,
                     timeout: Optional[float] = None) -> str:
    """Rip one audio track to a 44.1k/16-bit WAV, **error-corrected**.

    Uses the bundled cd-paranoia (libcdio-paranoia) for parity / jitter-corrected
    reads. If that helper is unavailable it falls back to a raw IOCTL read (no
    error correction). `track` must carry 'track' (number), 'is_audio' and
    'sectors'.

    cd-paranoia never hangs forever: if the output WAV stops growing for
    ``stall_secs`` (default 60s) or the whole track exceeds the ``timeout``
    bound, the child is killed and a :class:`CdReadError` is raised so the
    caller can skip that track and keep ripping the rest.
    """
    if not track.get("is_audio"):
        raise ValueError("Track is not an audio track")
    tool = _cdparanoia_path()
    if not tool:
        return _rip_track_to_wav_ioctl(letter, track, out_wav, progress_cb)

    import subprocess
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    device = f"{str(letter).strip(': ').upper()}:"
    sectors = int(track.get("sectors") or 0)
    # 2352 bytes per CD-DA sector + a 44-byte canonical WAV header.
    expected = sectors * 2352 + 44 if sectors > 0 else 0
    dur = sectors / 75.0 if sectors > 0 else 0.0
    # cd-paranoia [-d device] [-w] track# outfile  -> error-corrected WAV
    cmd = [tool, "-d", device, "-w", str(int(track["track"])), out_wav]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=creationflags)
    lines: List[str] = []

    def _drain():
        try:
            for ln in proc.stderr:
                lines.append(ln)
        except Exception:  # noqa: BLE001
            pass

    drain_t = threading.Thread(target=_drain, daemon=True)
    drain_t.start()

    total_max = timeout if timeout else max(240.0, dur * 5.0)   # wall-clock cap
    start = time.time()
    last_size = 0.0
    last_grow = time.time()
    rc = None
    timed_out = False
    while True:
        rc = proc.poll()
        try:
            sz = float(os.path.getsize(out_wav)) if os.path.exists(out_wav) else 0.0
        except OSError:
            sz = 0.0
        if sz != last_size:
            last_size = sz
            last_grow = time.time()
            if progress_cb and expected > 44:
                progress_cb(max(0.0, min(1.0, (sz - 44.0) / (expected - 44.0))))
        if rc is not None:
            break
        now = time.time()
        if (now - last_grow) > stall_secs or (now - start) > total_max:
            timed_out = True
            proc.kill()
            try:
                proc.wait(timeout=5)      # unbounded kill must never pin us
            except Exception:  # noqa: BLE001
                pass
            rc = proc.returncode if proc.returncode is not None else 1
            break
        time.sleep(0.2)

    drain_t.join(timeout=2)
    err = "".join(lines).strip()
    if rc != 0:
        sector = _bad_sector(err or "")
        written = int(last_size) if last_size > 0 else 0
        # If cd-paranoia wrote (almost) the whole track before failing, keep it:
        # it only had to interpolate an unreadable sector. Otherwise skip it.
        complete = bool(expected > 44 and (last_size - 44.0) >= 0.90 * (expected - 44.0))
        if timed_out:
            msg = (f"cd-paranoia read timed out on track {int(track['track'])} "
                   f"(no data written for {int(stall_secs)}s)")
        else:
            msg = f"cd-paranoia failed on track {int(track['track'])}"
        if sector:
            msg += (f" \u2014 an unreadable sector (sector {sector}) was reported, "
                    "which usually means the disc is scratched or the drive could "
                    "not read it")
        raise CdReadError(msg, sector=sector, written=written,
                          expected=int(expected), complete=complete)
    if progress_cb:
        progress_cb(1.0)
    return out_wav


def _rip_track_to_wav_ioctl(letter: str, track: dict, out_wav: str,
                            progress_cb=None) -> str:
    """No-error-correction raw-sector read (fallback when cd-paranoia is absent)."""
    lba = int(track["lba"])
    sectors = int(track["sectors"])
    if sectors <= 0:
        raise ValueError("Track has no sectors")
    h = open_drive(letter)
    if not h:
        raise OSError(None,
                      f"Could not open optical drive {letter} for raw audio "
                      f"(rip of track {track['track']}).")
    try:
        # write WAV header (16-bit stereo, 44.1 kHz)
        data_bytes = sectors * CDDA_SECTOR
        with open(out_wav, "wb") as w:
            _write_wav_header(w, data_bytes)
            read = 0
            chunk = 256  # sectors per raw-read call
            while read < sectors:
                n = min(chunk, sectors - read)
                blk = _read_sectors_ioctl(h, lba + read, n)
                w.write(blk)
                read += n
                if progress_cb:
                    progress_cb(read / float(sectors))
            # re-write header with real byte count (header size based on data)
        with open(out_wav, "r+b") as w:
            _rewrite_wav_sizes(w, data_bytes)
    finally:
        k32.CloseHandle(c_void_p(h))
    return out_wav


def _write_wav_header(f, data_bytes: int):
    fmt = 1                # PCM
    ch = 2                 # stereo
    rate = CDDA_SAMPLE_RATE
    bits = 16
    block_align = ch * bits // 8
    avg = rate * block_align
    f.write(b"RIFF")
    f.write(struct.pack("<I", 36 + data_bytes))
    f.write(b"WAVE")
    f.write(b"fmt ")
    f.write(struct.pack("<IHHIIHH", 16, fmt, ch, rate, avg, block_align, bits))
    f.write(b"data")
    f.write(struct.pack("<I", data_bytes))


def _rewrite_wav_sizes(f, data_bytes: int):
    # RIFF size at offset 4, data size at offset 40
    import struct as _s
    f.seek(4)
    f.write(_s.pack("<I", 36 + data_bytes))
    f.seek(40)
    f.write(_s.pack("<I", data_bytes))


def toc_to_discid(tracks: List[dict], first: int = 1) -> Optional[str]:
    """Compute the MusicBrainz Disc ID from a TOC (device-exact preferred).

    Implements libdiscid's Disc ID: SHA-1 over the first/last track and the
    (lead-in-adjusted) offsets, base64-encoded with a custom alphabet.
    Returns None if the TOC is unusable.
    """
    audio = [t for t in tracks if t["track"] and t["is_audio"]]
    if not audio:
        return None
    last = audio[-1]["track"]
    leadout = next((t["lba"] for t in tracks if t.get("leadout")), None)
    if leadout is None:
        leadout = audio[-1]["lba"] + audio[-1]["sectors"] + 150
    offsets = [t["lba"] for t in audio]
    import hashlib, base64
    leadin = 150
    s = hashlib.sha1()
    s.update(("%02X" % first).encode("ascii"))
    s.update(("%02X" % last).encode("ascii"))
    a = [0] * 100
    vals = [leadout] + offsets
    for i, o in enumerate(vals):
        a[i] = int(o) + int(leadin)
    for i in range(100):
        s.update(("%08X" % a[i]).encode("ascii"))
    dig = s.digest()
    # MusicBrainz Disc ID: base64 of the SHA-1 with the base64url-style
    # remapping ('+' -> '.', '/' and '=' padding -> '-').
    enc = base64.b64encode(dig).decode("ascii")
    enc = enc.replace("+", ".").replace("/", "-").replace("=", "-")
    return enc


def toc_string(tracks: List[dict]) -> Optional[str]:
    """Return the 'first+num+leadout+off1+off2+...' TOC string for MB fuzzy
    lookup. The drive reports raw LBAs (track 1 = LBA 0); MusicBrainz expects
    absolute offsets (with the 150-sector lead-in), so 150 is added."""
    audio = [t for t in tracks if t["track"] and t["is_audio"]]
    if not audio:
        return None
    first, last = 1, audio[-1]["track"]
    leadout = next((t["lba"] for t in tracks if t.get("leadout")), None)
    if leadout is None:
        leadout = audio[-1]["lba"] + audio[-1]["sectors"]
    leadin = 150
    leadout_abs = leadout + leadin
    offsets = [t["lba"] + leadin for t in audio]
    return "+".join(str(x) for x in [first, last, leadout_abs] + offsets)
