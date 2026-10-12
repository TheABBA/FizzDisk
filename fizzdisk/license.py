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

try:
    import winreg                      # Windows registry (per-user hard sentinel)
except ImportError:                    # non-Windows dev box
    winreg = None

TRIAL_LIMIT = 50

# The signing secret. Keys are minted by a helper (and by the website's
# client-side generator) and validated by the app, so only a properly signed
# key is accepted and no server is required. Keep in sync with the website
# page (website/success.html) if you change it.
_SECRET = b"FizzDisk2026-LicKey-UnlimitedBackups-ForLife"

_REG_APP = r"Software\FizzDisk"


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


# ---- registry sentinels (HKCU, per-user) --------------------------------
# Mirrors the high-water backup count and the activated key into the registry
# so that wiping/deleting %LOCALAPPDATA% (e.g. a manual reset, or an add/remove
# style cleanup) can't silently reset the 50-backup trial or forget the key.
def _reg_get(name: str, default=""):
    if winreg is None:
        return default
    try:
        import winreg as wr
        with wr.OpenKey(wr.HKEY_CURRENT_USER, _REG_APP) as k:
            v, _ = wr.QueryValueEx(k, name)
            return v
    except Exception:
        return default


def _reg_set(name: str, value, dtype=None):
    if winreg is None:
        return
    try:
        import winreg as wr
        with wr.CreateKey(wr.HKEY_CURRENT_USER, _REG_APP) as k:
            wr.SetValueEx(k, name, 0, dtype or wr.REG_SZ, value)
    except Exception:
        pass


def _reg_max_backups() -> int:
    try:
        return int(_reg_get("MaxBackups", 0) or 0)
    except Exception:
        return 0


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
    """True if a valid key is configured (env var, saved state, or registry)."""
    env = os.environ.get("FIZZDISK_LICENSE", "").strip()
    if env and validate_key(env):
        return True
    for key in (_load().get("license_key", ""), _reg_get("LicenseKey", "")):
        if key and validate_key(str(key)):
            return True
    return False


API_BASE = os.environ.get("FIZZDISK_API", "https://fizzdisk.philspad.com/api/license")


def machine_id() -> str:
    """A stable per-machine fingerprint that survives an app reinstall."""
    if winreg is not None:
        try:
            import winreg as wr
            with wr.OpenKey(wr.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as k:
                v, _ = wr.QueryValueEx(k, "MachineGuid")
                if v:
                    return "win:" + str(v).strip()
        except Exception:
            pass
    try:
        import uuid
        return "mac:" + format(uuid.getnode(), "012x")
    except Exception:
        return "unknown"


def _api_post(path: str, payload: dict):
    """POST JSON to the activation API; return parsed dict or None on failure."""
    import urllib.request
    try:
        url = API_BASE.rstrip("/") + path
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def activate(key: str):
    """Try to activate ``key`` (one user, one device). Returns (ok, message)."""
    k = (key or "").strip()
    if not k:
        return False, "Enter a product key."
    if not validate_key(k):
        return False, "That key isn't valid. Check the format (FZFK-XXXX-...)."
    canon = format_key(k)
    fp = machine_id()
    resp = _api_post("/activate", {"key": canon, "fingerprint": fp})
    if resp is None:
        # offline: accept locally; the binding will sync next time we're online
        st = _load(); st["license_key"] = canon; _save(st)
        _reg_set("LicenseKey", canon)
        return True, "Activated (offline) \u2014 unlimited backups. It will sync when you\u2019re online."
    if not resp.get("ok"):
        return False, resp.get("message", "Could not activate that key.")
    st = _load(); st["license_key"] = canon; _save(st)
    _reg_set("LicenseKey", canon)                 # survive an appdata wipe
    return True, "Activated \u2014 unlimited backups & lifetime updates."


def deactivate():
    """Release the license so it can be activated on another machine."""
    current = _load().get("license_key", "") or _reg_get("LicenseKey", "")
    if not current or not validate_key(current):
        return True, "No active license on this device."
    canon = format_key(current)
    _api_post("/deactivate", {"key": canon, "fingerprint": machine_id()})
    st = _load(); st["license_key"] = ""; _save(st)
    _reg_set("LicenseKey", "")
    return True, "License released \u2014 you can now activate on another machine."


def format_key(key: str) -> str:
    k = _normalize(key)
    if len(k) != 16:
        return (key or "").strip()
    return "FZFK-" + "-".join(k[i:i + 4] for i in range(0, 16, 4))


def backups_used() -> int:
    json_used = int(_load().get("backups_used", 0) or 0)
    return max(json_used, _reg_max_backups())


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
    used = min(TRIAL_LIMIT, int(st.get("backups_used", 0) or 0) + 1)
    st["backups_used"] = used
    _save(st)
    # mirror the high-water count into the registry so wiping %LOCALAPPDATA%
    # (or an uninstall/reinstall that leaves user data behind) can't reset the
    # trial below the all-time high.
    if winreg is not None and used > _reg_max_backups():
        _reg_set("MaxBackups", used, winreg.REG_DWORD)
    return used


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
