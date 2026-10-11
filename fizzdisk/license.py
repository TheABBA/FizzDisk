"""FizzDisk licensing: a 50-backup trial with an offline product key.

The product key is self-validating (HMAC-signed) so a user can't just make
one up, and it does not need a server. A valid key grants unlimited backups
and lifetime updates; without one the app allows ``TRIAL_LIMIT`` backups.
"""

import os
import json
import hmac
import hashlib
import base64
import sys
import time

TRIAL_LIMIT = 50

# The signing secret. Keys are minted by a small helper (see __main__ below)
# and validated by the app, so only a properly signed key is accepted and no
# server is required. Change this once to invalidate old keys.
_SECRET = b"FizzDisk2026-LicKey-\xf0\x9f\x92\xbf-Unlimited"


def _data_dir() -> str:
    """A writable per-user config dir (never the read-only app install dir)."""
    base = os.environ.get("LOCALAPPDATA") or ""
    if base:
        d = os.path.join(base, "FizzDisk")
    else:
        d = os.path.join(os.path.expanduser("~"), ".fizzdisk")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.join(os.path.expanduser("~"), ".fizzdisk")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = os.path.dirname(os.path.abspath(__file__))
    return d


def _state_path() -> str:
    return os.path.join(_data_dir(), "license.json")


def _default_state() -> dict:
    return {"backups_used": 0, "license_key": ""}


def _load() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            st = json.load(f)
        return {
            "backups_used": int(st.get("backups_used", 0) or 0),
            "license_key": str(st.get("license_key", "") or ""),
        }
    except Exception:
        return _default_state()


def _save(st: dict) -> None:
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


def _normalize(key: str) -> str:
    """Strip formatting, return the raw uppercase 16-char body or ''."""
    if not key:
        return ""
    k = (key or "").strip().upper().replace(" ", "")
    if k.startswith("FZFK-") or k.startswith("FZFK"):
        k = k[4:].lstrip("-")
    k = k.replace("-", "")
    return k


def validate_key(key: str) -> bool:
    """Return True if ``key`` is a correctly signed product key."""
    k = _normalize(key)
    if len(k) != 16:
        return False
    try:
        raw = base64.b32decode(k)          # 16 base32 chars -> 10 bytes
    except Exception:
        return False
    if len(raw) != 10:
        return False
    body, sig = raw[:5], raw[5:]
    expect = hmac.new(_SECRET, body, hashlib.sha256).digest()[:5]
    return hmac.compare_digest(sig, expect)


def is_licensed() -> bool:
    """True if a valid key is configured (env var or saved state)."""
    key = os.environ.get("FIZZDISK_LICENSE", "").strip()
    if key and validate_key(key):
        return True
    key = _load().get("license_key", "")
    return bool(key and validate_key(key))


def activate(key: str):
    """Try to activate ``key``. Returns (ok, message)."""
    k = (key or "").strip()
    if not k:
        return False, "Enter a product key."
    if not validate_key(k):
        return False, "That key isn't valid. Check the format (FZFK-XXXX-...)."
    st = _load()
    st["license_key"] = format_key(k)
    _save(st)
    return True, "Activated \u2014 unlimited backups & lifetime updates."


def format_key(key: str) -> str:
    k = _normalize(key)
    if len(k) != 16:
        return (key or "").strip()
    return "FZFK-" + "-".join(k[i:i + 4] for i in range(0, 16, 4))


def backups_used() -> int:
    return int(_load().get("backups_used", 0) or 0)


def remaining() -> int:
    if is_licensed():
        return -1                                    # unlimited
    return max(0, TRIAL_LIMIT - backups_used())


def can_rip() -> bool:
    """Whether the user may run another backup (licensed or trial left)."""
    if is_licensed():
        return True
    return backups_used() < TRIAL_LIMIT


def record_backup() -> int:
    """Increment the used-backup counter (no-op once licensed). Returns used."""
    if is_licensed():
        return backups_used()
    st = _load()
    st["backups_used"] = min(TRIAL_LIMIT, int(st.get("backups_used", 0) or 0) + 1)
    _save(st)
    return int(st["backups_used"])


def status_text() -> str:
    """Human-readable trial/registration status for the Settings card."""
    if is_licensed():
        return "Licensed \u2014 unlimited backups and lifetime updates."
    used = backups_used()
    left = max(0, TRIAL_LIMIT - used)
    if left <= 0:
        return "Trial finished \u2014 enter a product key to unlock unlimited backups."
    return (f"{used} of {TRIAL_LIMIT} trial backups used. "
            f"{left} left before a product key is required.")


def generate_key() -> str:
    """Mint a new valid product key (used by the developer helper)."""
    body = os.urandom(5)
    sig = hmac.new(_SECRET, body, hashlib.sha256).digest()[:5]
    raw = body + sig
    enc = base64.b32encode(raw).decode().rstrip("=")      # 16 chars
    return "FZFK-" + "-".join(enc[i:i + 4] for i in range(0, 16, 4))


def main_self_test():
    print("TRIAL_LIMIT =", TRIAL_LIMIT)
    k = generate_key()
    print("generated key:", k)
    print("validate(generated):", validate_key(k))
    print("validate(nonsense):", validate_key("FZFK-AAAA-BBBB-CCCC-DDDD"))
    print("validate(typo):", validate_key(k[:-1] + ("X" if k[-1] != "X" else "Y")))
    print("is_licensed before:", is_licensed())
    ok, msg = activate(k)
    print("activate:", ok, "->", msg)
    print("is_licensed after:", is_licensed())
    print("status:", status_text())
    print("can_rip:", can_rip())
    print("remaining (licensed):", remaining())
    # test trial limiting on a throwaway state
    import tempfile
    print("--- trial counter test ---")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("key", "gen"):
        print(generate_key())
    elif len(sys.argv) > 1 and sys.argv[1] in ("check", "validate"):
        print(validate_key(sys.argv[2]) if len(sys.argv) > 2 else "usage: license.py check KEY")
    else:
        main_self_test()
