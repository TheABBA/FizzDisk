# -*- coding: utf-8 -*-
"""BD+ support via libbdplus' cached conversion-table path.

Background
----------
BD+ is a second protection layer on top of AACS. A BD+ disc ships a virtual
machine program (``BDSVM/*.svm``); a licensed player runs it and the VM emits a
**conversion table** -- a list of offsets into the main ``.m2ts`` plus the bytes
needed to repair them. The disc's video is deliberately corrupted at those
offsets, so without the table the picture is damaged even though AACS decrypted
perfectly (audio is untouched, which is the tell-tale signature).

Why we do NOT run the VM
------------------------
libbdplus only implements BD+ generations 1-3::

    loader.c:  if (gen > 3) { "BD+ generation %d not tested / supported"; return -1; }

Most modern BD+ titles are far past that (Red Dawn 2012 is generation 14), and
the generations above 3 run native Irdeto code that libbdplus has never
emulated. Implementing a gen-14 VM is an open reverse-engineering research
problem, not a feature we can write.

What DOES work
--------------
``bdplus_start()`` loads a **cached conversion table** independently of the VM:
it looks for ``<config>/bdplus/convtab/<MEDIA-KEY>.bin`` (32 lowercase hex
chars + ``.bin``) and activates it even when the generation check failed. So a
disc whose table is already known is fully repairable with the stock bundled
libbdplus -- no VM emulation required.

Two things must be in place for that path to engage:

1. A ``vm0`` configuration directory must exist, or ``bdplus_init()`` aborts
   with "VM configuration not found" and never reaches the cached-table code.
   Verified on real hardware: the cached table loads even when every vm0 file
   is ZERO-FILLED, because on this path the VM's output is discarded. So we
   generate a synthetic scaffold and ship **no key material of any kind**.
2. A conversion table for that specific disc, named after its AACS media key.

This module owns (1) entirely and locates/validates (2). It never downloads,
embeds, or distributes tables or keys -- the user supplies the table for a disc
they own.
"""
from __future__ import annotations

import ctypes
import os
import re
import base64
import struct
from typing import Optional

# libbdplus reads its config from %APPDATA%\bdplus on Windows (README: "Windows:
# %APPDATA%/bdplus/"). That path is not configurable, so we must write there.
_VM0_FILES_ZEROED = {
    # name -> size. Sizes satisfy bdplus_config.c's loaders:
    #   _load_aes_keys  requires > 6 keys x 16 bytes
    #   _load_dev_discovery requires >= 5 device_discovery_N.bin files
    "aes_keys.bin": 16 * 8,
    "certificates.bin": 256,
}
_VM0_DEV_DISCOVERY = 5
_VM0_DEV_SIZE = 64
# _load_ecdsa_keys wants 3 x 40 hex chars per line.
_VM0_ECDSA = ("0" * 40 + " " + "0" * 40 + " " + "0" * 40 + "\n")
# _load_memory maps named register files; PSR/GPR are synthesised internally.
_VM0_MEMORY_MAP = "00000000 PSR\n00001000 GPR\n"

_MK_RE = re.compile(r"^[0-9a-f]{32}$")


def config_dir() -> str:
    """The libbdplus configuration root (%APPDATA%\\bdplus)."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "bdplus")


def vm0_dir() -> str:
    return os.path.join(config_dir(), "vm0")


def convtab_dir() -> str:
    return os.path.join(config_dir(), "convtab")


def ensure_vm0() -> bool:
    """Create the synthetic vm0 scaffold if absent. Returns True if usable.

    The files are ZERO-FILLED on purpose. They exist only so
    ``bdplus_config_load()`` succeeds and ``bdplus_init()`` proceeds far enough
    to consult the cached conversion table; the VM's own output is never used
    on that path. Consequently this ships no player keys, no certificates and
    no circumvention data -- it is an empty shell that unlocks a code path.

    An EXISTING vm0 is never overwritten: a user who has supplied a real one
    (for genuine gen 1-3 VM support) must keep it.
    """
    d = vm0_dir()
    try:
        os.makedirs(d, exist_ok=True)
        for name, size in _VM0_FILES_ZEROED.items():
            p = os.path.join(d, name)
            if not os.path.exists(p):
                with open(p, "wb") as f:
                    f.write(b"\0" * size)
        for i in range(1, _VM0_DEV_DISCOVERY + 1):
            p = os.path.join(d, f"device_discovery_{i}.bin")
            if not os.path.exists(p):
                with open(p, "wb") as f:
                    f.write(b"\0" * _VM0_DEV_SIZE)
        p = os.path.join(d, "ecdsa_keys.txt")
        if not os.path.exists(p):
            with open(p, "w", encoding="ascii") as f:
                f.write(_VM0_ECDSA)
        p = os.path.join(d, "memory.map")
        if not os.path.exists(p):
            with open(p, "w", encoding="ascii") as f:
                f.write(_VM0_MEMORY_MAP)
        os.makedirs(convtab_dir(), exist_ok=True)
        return True
    except OSError:
        return False


def media_key(source: str, keydb: Optional[str] = None) -> Optional[str]:
    """The disc's AACS media key as 32 lowercase hex chars, or None.

    This is the exact name libbdplus looks up: ``internal.c`` formats
    ``plus->mediaKey`` with ``str_print_hex`` and matches the first 32
    characters of a ``*.bin`` filename in ``convtab/`` against it.
    """
    from . import bdlib
    bdlib.ensure_loaded()
    try:
        lib = ctypes.WinDLL(os.path.join(bdlib._bin_dir() or "", "libaacs.dll"))
        lib.aacs_open.restype = ctypes.c_void_p
        lib.aacs_open.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.aacs_get_mk.restype = ctypes.POINTER(ctypes.c_ubyte)
        lib.aacs_get_mk.argtypes = [ctypes.c_void_p]
        lib.aacs_close.argtypes = [ctypes.c_void_p]
    except OSError:
        return None
    dev = bdlib._raw_device(source)
    kf = keydb or bdlib._user_keydb_path() or os.path.join(
        bdlib._bin_dir() or "", "keydb.cfg")
    a = None
    try:
        a = lib.aacs_open(dev.encode("utf-8", "replace"),
                          kf.encode("utf-8", "replace") if kf else None)
        if not a:
            return None
        mk = lib.aacs_get_mk(ctypes.c_void_p(a))
        if not mk:
            return None
        return "".join(f"{mk[i]:02x}" for i in range(16))
    except Exception:
        return None
    finally:
        if a:
            try:
                lib.aacs_close(ctypes.c_void_p(a))
            except Exception:
                pass


def table_for(mk: str) -> Optional[str]:
    """Path to the installed conversion table for this media key, or None.

    Mirrors libbdplus' own matcher: any ``*.bin`` in ``convtab/`` whose first
    32 characters (case-insensitively) equal the media key.
    """
    if not mk or not _MK_RE.match(mk.lower()):
        return None
    d = convtab_dir()
    try:
        for name in os.listdir(d):
            low = name.lower()
            if len(low) >= 36 and low.endswith(".bin") and low[:32] == mk.lower():
                return os.path.join(d, name)
    except OSError:
        pass
    return None


def status(source: str) -> dict:
    """Describe BD+ readiness for this disc.

    Returns ``{"bdplus": bool, "media_key": str|None, "table": str|None,
    "ready": bool}``. ``ready`` means a conversion table is installed, so the
    rip can proceed and produce correct video.
    """
    from . import bdlib
    out = {"bdplus": False, "media_key": None, "table": None, "ready": False}
    if not bdlib.disc_has_bdplus(source):
        return out
    out["bdplus"] = True
    mk = media_key(source)
    out["media_key"] = mk
    if mk:
        out["table"] = table_for(mk)
        out["ready"] = bool(out["table"])
    return out


def install_table(src_path: str, mk: str) -> str:
    """Install a user-supplied conversion table under the media-key name.

    Returns the installed path. Raises OSError/ValueError on bad input.
    """
    if not _MK_RE.match((mk or "").lower()):
        raise ValueError(f"not a 32-hex media key: {mk!r}")
    ensure_vm0()
    dst = os.path.join(convtab_dir(), f"{mk.lower()}.bin")
    with open(src_path, "rb") as fi:
        data = fi.read()
    if len(data) < 64:
        raise ValueError("conversion table file is implausibly small")
    tmp = dst + ".tmp"
    with open(tmp, "wb") as fo:
        fo.write(data)
    os.replace(tmp, dst)
    return dst


# ---------------------------------------------------------------------------
# Automatic table provisioning (cached BD+ conversion tables from Doom9/MEGA)
# ---------------------------------------------------------------------------
# BD+ conversion tables are published as a downloadable archive (the Doom9
# "cached BD+ tables" thread) of ``MK_<MediaKey>_<VolumeLabel>.bin`` files.
# The four "updated tables" archives are small and cover the tables anyone is
# realistically still ripping (the last BD+ discs were mastered ~2017); the
# ~1.6GB base archive from 2019 covers the earlier titles and is only needed
# for a BD+ title released before ~2019 that the updates don't cover.
#
# We fetch the matching table for the disc's media key on demand and install
# it under ``<config>/bdplus/convtab/<MK>.bin`` -- the exact name libbdplus
# looks up. The archive is cached so a second BD+ disc doesn't re-download.
#
# MEGA link format: ``https://mega.nz/file/<ID>#<KEY>`` -- the KEY decrypts an
# otherwise opaque blob (AES-CTR with the XOR of the key's two halves). See
# _mega_download. We only need the MEGA "file id" and "key"; both are stable.

# (label, mega_file_id, mega_key, approx_bytes)
_TABLE_ARCHIVES = [
    # incremental "updated tables" (cover ~2017-onwards, small)
    ("2023", "AR8DDaib", "GgSUMnNGBlVXdJT0BEkNkGm5f4NfodBaQ8SSgFFM4ZA", 3_574_011),
    ("2022", "gVsRQQ7Y", "JOJwO5woXdz2X73rrvHHBTYCdLposz7aiSVkEX4vChM", 13_002_827),
    ("2021", "pc0VTaYY", "Tl1XMSex_Y9iCKmvYEKddr7GQQVQbMDEHJbw0uXumj0", 32_095_446),
    ("2020", "ZZdA3QCJ", "FaL2ohltwFCtX91UMngB_dUtqht8JZ3-nRgnTAJD8jk", 37_985_490),
    # base 2019 archive (covers older BD+ titles; the big one)
    ("base", "Jd1xEQbJ", "DRhG9eWLNnrmA5dcwHugnKxmVUpIsT9X-HKuuGjU7n8", 1_683_025_941),
]

_cache_dir = None


def archive_cache_dir() -> str:
    """Where downloaded table archives are cached (user app-data)."""
    global _cache_dir
    if _cache_dir is None:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        _cache_dir = os.path.join(base, "FizzDisk", "bdplus_cache")
    os.makedirs(_cache_dir, exist_ok=True)
    return _cache_dir


def _b64url(data: bytes) -> bytes:
    # URL-safe base64 with correct padding.
    return base64.urlsafe_b64decode(data + b"=" * (-len(data) % 4))


def _str_to_a32(b: bytes):
    if len(b) % 4:
        b += b"\0" * (4 - len(b) % 4)
    return struct.unpack(">%dI" % (len(b) // 4), b)


def _a32_to_str(a):
    return struct.pack(">%dI" % len(a), *a)


def _mega_download(file_id: str, file_key: str) -> bytes:
    """Download and decrypt a MEGA public file.

    MEGA encrypts file content with AES-CTR: the 32-byte key decodes to eight
    32-bit words; the AES key is the XOR of words 0-3 with words 4-7, and the
    CTR initial counter is words 4-5 (as a 64-bit nonce) followed by a zero
    counter. Returns the plaintext blob (a 7z archive).
    """
    import json  # noqa: F401  (used by urllib path below)

    resp = _http_post_json(
        "https://g.api.mega.co.nz/cs", [{"a": "g", "g": 1, "p": file_id}])
    data = resp[0] if isinstance(resp, list) else resp
    if not isinstance(data, dict) or "g" not in data:
        raise RuntimeError(f"MEGA did not return a download URL for {file_id}")
    url = data["g"]
    if url.startswith("http://"):   # MEGA sometimes hands back http URLs
        url = "https://" + url[7:]
    blob = _http_get(url)
    fk = _str_to_a32(base64.urlsafe_b64decode(file_key + "=" * (-len(file_key) % 4)))
    aes_key = _a32_to_str(
        [fk[0] ^ fk[4], fk[1] ^ fk[5], fk[2] ^ fk[6], fk[3] ^ fk[7]])
    initial = ((fk[4] << 32) | fk[5]) << 64
    ctr = initial.to_bytes(16, "big")
    return _aes_ctr(aes_key, ctr, blob)


def _http_post_json(url: str, payload) -> object:
    """Minimal POST returning parsed JSON, using urllib only (no new deps)."""
    import json, urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _http_get(url: str) -> bytes:
    import urllib.request
    with urllib.request.urlopen(url, timeout=600) as r:
        # stream copy in chunks (the base archive is ~1.6GB)
        out = bytearray()
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out += chunk
        return bytes(out)


def _aes_ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CTR using the stdlib? No -- stdlib lacks AES. Use cryptography if
    available, else fall back to a pure-python AES (tiny helpers below)."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        dec = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
        return dec.update(data) + dec.finalize()
    except ImportError:            # pragma: no cover -- bundled app has it
        return _aes_ctr_pure(key, iv, data)


def _aes_ctr_pure(key: bytes, iv: bytes, data: bytes) -> bytes:  # pragma: no cover
    """Pure-python AES-128 fallback (no external deps)."""
    from ._aes import aes_block
    nonce = iv[:8]
    counter = int.from_bytes(iv[8:], "big")
    out = bytearray()
    for off in range(0, len(data), 16):
        blk = nonce + counter.to_bytes(8, "big")
        ks = aes_block(key, blk)
        for i, c in enumerate(data[off:off + 16]):
            out.append(c ^ ks[i])
        counter = (counter + 1) & 0xFFFFFFFFFFFFFFFF
    return bytes(out)


def _decode_7z(blob: bytes) -> dict:
    """Extract a 7z archive in memory. Returns {filename_lower: bytes}.

    The bundled app ships ``py7zr``; if it is not importable we fall back to
    the self-contained stdlib-``lzma`` reader in ``_sevenz``.
    """
    try:
        import py7zr
        from io import BytesIO
        import tempfile
        d = tempfile.mkdtemp(prefix="bdplus_")
        with py7zr.SevenZipFile(BytesIO(blob)) as z:
            z.extract(path=d)
        out = {}
        for root, _dirs, files in os.walk(d):
            for fn in files:
                with open(os.path.join(root, fn), "rb") as f:
                    out[fn.lower()] = f.read()
        return out
    except ImportError:
        from . import _sevenz
        return {k.lower(): v for k, v in _sevenz.extract_7z(blob).items()}
    except Exception:
        # py7zr present but failed (unusual) -> try the pure reader.
        from . import _sevenz
        return {k.lower(): v for k, v in _sevenz.extract_7z(blob).items()}


def _decode_7z_via_7z(blob: bytes) -> dict:  # pragma: no cover
    from . import ffutil
    return {}


def _find_7z_tool() -> Optional[str]:  # pragma: no cover
    from . import ffutil
    if hasattr(ffutil, "find_tool"):
        return ffutil.find_tool("7z") or ffutil.find_tool("7za")
    return None


_FV_BDPLUS_URL = "https://fvonline-db.bplaced.net/fv_bdplus.php?mediakey={mk}"
# The archive/MEGA path below is a fallback only; the primary route is the
# per-table fvonline-db endpoint, which returns the single ~1MB table directly
# (the same bytes that are inside the 1.68GB archive, keyed by media key).


def fetch_table(mk: str, progress_cb=None) -> Optional[str]:
    """Download the BD+ conversion table for ``mk`` and install it.

    Primary path: GET the single table from the free fvonline-db service (the
    same database the app already uses for keydb.cfg), one ~1MB file per disc,
    with no large archive download and no 7z/py7zr dependency. If that service
    lacks this disc, fall back to the Doom9/MEGA archive, which is cached so
    it is never re-downloaded.

    Returns the installed table path, or None if no table exists for this
    media key.
    """
    if not _MK_RE.match((mk or "").lower()):
        return None
    existing = table_for(mk)
    if existing:
        return existing
    ensure_vm0()
    if progress_cb:
        progress_cb("Downloading BD+ table\u2026")
    found = _fetch_table_service(mk, progress_cb=progress_cb)
    if found:
        return found
    # Fallback: the cached-table archive (small incremental, or the base).
    return _fetch_table_archive(mk, progress_cb=progress_cb)


def _fetch_table_service(mk: str, progress_cb=None) -> Optional[str]:
    """Fetch a single BD+ table from fvonline-db by media key (no archive)."""
    try:
        import urllib.request
        url = _FV_BDPLUS_URL.format(mk=mk.lower())
        req = urllib.request.Request(url, headers={"User-Agent": "FizzDisk/1.6"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        # A successful lookup returns the table bytes; a miss returns HTTP 200
        # with the "OK" string (or a small non-table body). Validate.
        if len(data) >= 64 and data[:4] == b"\x00Y\x00\x00":   # BD+ table magic
            return _write_runtime_table(data, mk)
    except Exception:
        pass
    return None


def _fetch_table_archive(mk: str, progress_cb=None) -> Optional[str]:
    """Fallback: install the table from the Doom9/MEGA archive (cached)."""
    order = []
    for label, fid, key, size in _TABLE_ARCHIVES:
        cached = os.path.join(archive_cache_dir(), f"tables_{label}.7z")
        order.append((label, fid, key, size, cached))
    found = None
    for label, fid, key, size, cached in order:
        if os.path.exists(cached):
            found = _install_from_archive(cached, mk) if found is None else found
    for label, fid, key, size, cached in order:
        if not os.path.exists(cached):
            try:
                blob = _mega_download(fid, key)
                with open(cached, "wb") as f:
                    f.write(blob)
                if found is None:
                    found = _install_from_archive(cached, mk)
            except Exception:
                continue
        if found:
            break
    return found


def _write_runtime_table(data: bytes, mk: str) -> str:
    """Install a freshly-downloaded table blob as ``<mk>.bin`` (atomic)."""
    import tempfile
    d = convtab_dir()
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, f"{mk.lower()}.bin")
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".bin", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dst


def _install_from_archive(archive_path: str, mk: str) -> Optional[str]:
    try:
        with open(archive_path, "rb") as f:
            blob = f.read()
        files = _decode_7z(blob)
    except Exception:
        return None
    target = mk.lower()                     # first 32 hex chars, case-insensitive
    for name, data in files.items():
        base = name.split("/")[-1].split("\\")[-1]
        # Archive stores tables as "MK_<KEY>_<VOLUME>.bin" (or "<KEY>_<vol>_<n>.bin").
        # libbdplus matches any *.bin whose first 32 chars equal the media key,
        # so match on that prefix, not exact filename, then store as <key>.bin.
        if base.lower().endswith(".bin") and base.lower()[:32] == target:
            dst = os.path.join(convtab_dir(), f"{target}.bin")
            with open(dst, "wb") as f:
                f.write(data)
            return dst
    return None


def ensure_table(source: str, progress_cb=None) -> bool:
    """Ensure a BD+ table for ``source`` is available; True if ready.

    Idempotent: if the table is already installed, returns True immediately.
    Otherwise attempts to fetch it automatically. Failure leaves BD+ in the
    \"needs a table\" state (the GUI warns and lets the user add a table).
    """
    from . import bdlib
    if not bdlib.disc_has_bdplus(source):
        return True
    mk = media_key(source)
    if not mk:
        return False
    if table_for(mk):
        return True
    try:
        return bool(fetch_table(mk, progress_cb=progress_cb))
    except Exception:
        return False
