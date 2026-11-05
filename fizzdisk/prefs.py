"""Small per-user preferences (persisted to %LOCALAPPDATA%\\FizzDisk\\settings.json).

Kept deliberately tiny: a single JSON file of key/value options. Defaults are
the shipped behaviour, so a fresh install or a missing file always falls back
to the recommended setting.
"""

import json
import os

_DEFAULTS = {
    "auto_eject_after_rip": True,   # eject the optical drive when a rip finishes
    # CD multi-disc album layout: 'single' = one folder, continuous track
    # numbers 1..N across discs; 'disc_folders' = a "Disc N" subfolder per
    # disc with per-disc track numbers.
    "cd_multidisc_layout": "single",
}


def _data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or ""
    if base:
        d = os.path.join(base, "FizzDisk")
    else:
        d = os.path.join(os.path.expanduser("~"), ".fizzdisk")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.dirname(os.path.abspath(__file__))
    return d


def _path() -> str:
    return os.path.join(_data_dir(), "settings.json")


def get(key, default=None):
    """Return the stored value for *key*, or the default if unset / unreadable.

    Falls back to the shipped default in _DEFAULTS when present, otherwise to
    the caller-supplied *default* (or None). Never raises.
    """
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            st = json.load(f)
        return st.get(key, _DEFAULTS.get(key, default))
    except Exception:
        return _DEFAULTS.get(key, default)


def set_(key, val):
    """Persist a value; failures (e.g. read-only dir) are swallowed."""
    try:
        st = {}
        if os.path.exists(_path()):
            try:
                with open(_path(), "r", encoding="utf-8") as f:
                    st = json.load(f) or {}
            except Exception:
                st = {}
        st[key] = val
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass
