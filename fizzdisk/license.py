"""FizzDisk licensing — stubbed out (free, no license required).

Previously a 50-backup trial with an offline product key. Now all rips are
unlimited and free — no trial, no key, no activation. The functions are
kept as no-op stubs so existing call sites (gui.py, the rip workers) don't
need to change.
"""

import os
import json
import hmac
import hashlib
import base64
import sys

try:
    import winreg                      # Windows registry (per-user hard sentinel)
except ImportError:                    # non-Windows dev box
    winreg = None

TRIAL_LIMIT = 0  # unused — no trial limit


def is_licensed() -> bool:
    return True

def can_rip() -> bool:
    return True

def record_backup() -> int:
    return 0

def remaining() -> int:
    return -1  # unlimited

def backups_used() -> int:
    return 0

def status_text() -> str:
    return "Free — unlimited backups."

def activate(key: str):
    return True, "Free software — no key needed."

def deactivate():
    return True, "Free software — nothing to deactivate."

def validate_key(key: str) -> bool:
    return True

def format_key(key: str) -> str:
    return (key or "").strip()

def generate_key() -> str:
    return "FZFK-FREE-SOFT-WARE-FREE"


def main_self_test():
    print("Licensing: free, no trial, no key required.")
    print("can_rip:", can_rip())
    print("status:", status_text())


if __name__ == "__main__":
    main_self_test()
